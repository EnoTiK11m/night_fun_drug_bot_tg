"""Domain implementation. Dependencies are supplied by the public facade."""

async def ensure_subscription_columns(runtime, db):
    cursor = await db.execute("PRAGMA table_info(subscriptions)")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "no_new_posts_count": "INTEGER DEFAULT 0",
        "last_empty_at": "TIMESTAMP",
        "next_check_at": "TIMESTAMP",
        "exhausted_notified": "BOOLEAN DEFAULT 0",
        "processing_until": "TIMESTAMP",
        "processing_token": "TEXT",
        "settings_json": "TEXT DEFAULT '{}'",
        "digest_mode": "TEXT DEFAULT 'instant'",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} {definition}")

    interval_expression = "interval_seconds" if "interval_seconds" in columns else "interval_minutes * 60"
    await db.execute(f"""
        UPDATE subscriptions
        SET next_check_at = COALESCE(
            next_check_at,
            datetime(last_sent, '+' || ({interval_expression}) || ' seconds')
        )
    """)


async def ensure_subscription_cache_columns(runtime, db):
    cursor = await db.execute("PRAGMA table_info(subscription_cache)")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "sample_url": "TEXT DEFAULT ''",
        "preview_url": "TEXT DEFAULT ''",
        "width": "INTEGER DEFAULT 0",
        "height": "INTEGER DEFAULT 0",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE subscription_cache ADD COLUMN {column} {definition}")


async def ensure_media_post_columns(runtime, db, table_name):
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in await cursor.fetchall()}
    column_defs = {
        "sample_url": "TEXT DEFAULT ''",
        "preview_url": "TEXT DEFAULT ''",
    }
    for column, definition in column_defs.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")


async def ensure_blacklist_columns(runtime, db):
    cursor = await db.execute("PRAGMA table_info(blacklist)")
    columns = {row[1] for row in await cursor.fetchall()}
    for column, definition in {
        "expires_at": "TIMESTAMP",
        "source": "TEXT DEFAULT 'manual'",
    }.items():
        if column not in columns:
            await db.execute(f"ALTER TABLE blacklist ADD COLUMN {column} {definition}")


async def apply_versioned_migrations(runtime, db):
    """Apply small, restart-safe schema migrations after the base schema commit."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (runtime.DIGEST_CLAIM_MIGRATION_VERSION,),
        )
        already_applied = await cursor.fetchone() is not None
        cursor = await db.execute("PRAGMA table_info(subscription_digest_queue)")
        columns = {row[1] for row in await cursor.fetchall()}
        required_columns = {
            "claim_token": "TEXT",
            "claimed_at": "TIMESTAMP",
            "claim_until": "TIMESTAMP",
        }

        if not already_applied:
            for column, definition in required_columns.items():
                if column not in columns:
                    await db.execute(
                        f"ALTER TABLE subscription_digest_queue "
                        f"ADD COLUMN {column} {definition}"
                    )
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (runtime.DIGEST_CLAIM_MIGRATION_VERSION,),
            )
        elif not required_columns.keys() <= columns:
            missing = sorted(required_columns.keys() - columns)
            raise RuntimeError(
                "Digest claim migration is recorded but columns are missing: "
                + ", ".join(missing)
            )

        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (runtime.SUBSCRIPTION_QUOTA_MIGRATION_VERSION,),
        )
        quota_migration_applied = await cursor.fetchone() is not None
        cursor = await db.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'subscription_creation_state'
        """)
        quota_table_exists = await cursor.fetchone() is not None
        if not quota_migration_applied:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscription_creation_state (
                    user_id INTEGER PRIMARY KEY,
                    last_created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (runtime.SUBSCRIPTION_QUOTA_MIGRATION_VERSION,),
            )
        elif not quota_table_exists:
            raise RuntimeError(
                "Subscription quota migration is recorded but state table is missing"
            )

        cache_indexes = {
            "idx_subscription_cache_lookup": (
                """
                    CREATE INDEX IF NOT EXISTS idx_subscription_cache_lookup
                    ON subscription_cache (user_id, query, cached_at)
                """,
                ("user_id", "query", "cached_at"),
            ),
            "idx_subscription_cache_cached": (
                """
                    CREATE INDEX IF NOT EXISTS idx_subscription_cache_cached
                    ON subscription_cache (cached_at)
                """,
                ("cached_at",),
            ),
            "idx_post_cache_cached": (
                """
                    CREATE INDEX IF NOT EXISTS idx_post_cache_cached
                    ON post_cache (cached_at)
                """,
                ("cached_at",),
            ),
        }
        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (runtime.CACHE_RETENTION_MIGRATION_VERSION,),
        )
        cache_migration_applied = await cursor.fetchone() is not None
        if not cache_migration_applied:
            for statement, _expected_columns in cache_indexes.values():
                await db.execute(statement)
            await db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (runtime.CACHE_RETENTION_MIGRATION_VERSION,),
            )
        else:
            invalid_indexes = []
            for index_name, (_statement, expected_columns) in cache_indexes.items():
                cursor = await db.execute(f"PRAGMA index_info({index_name})")
                actual_columns = tuple(row[2] for row in await cursor.fetchall())
                if actual_columns != expected_columns:
                    invalid_indexes.append(index_name)
            if invalid_indexes:
                raise RuntimeError(
                    "Cache retention migration is recorded but indexes are invalid: "
                    + ", ".join(sorted(invalid_indexes))
                )

        for version, table_name, required_columns in (
            (
                runtime.DIGEST_RETRY_MIGRATION_VERSION,
                "subscription_digest_queue",
                {
                    "delivery_state": "TEXT NOT NULL DEFAULT 'pending'",
                    "retry_after": "TIMESTAMP",
                },
            ),
            (
                runtime.DELIVERY_FAILURE_CLAIM_MIGRATION_VERSION,
                "delivery_failures",
                {
                    "claim_token": "TEXT",
                    "claimed_at": "TIMESTAMP",
                    "claim_until": "TIMESTAMP",
                },
            ),
        ):
            cursor = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            )
            migration_applied = await cursor.fetchone() is not None
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1] for row in await cursor.fetchall()}
            if not migration_applied:
                for column, definition in required_columns.items():
                    if column not in columns:
                        await db.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"
                        )
                await db.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
            elif not required_columns.keys() <= columns:
                missing = sorted(required_columns.keys() - columns)
                raise RuntimeError(
                    f"Migration {version} for {table_name} is recorded but columns "
                    "are missing: " + ", ".join(missing)
                )
        recorded = await (await db.execute('SELECT 1 FROM schema_migrations WHERE version=6')).fetchone()
        if recorded:
            for table, expected in {
                'query_progress': {'kind', 'user_id', 'query', 'signature', 'pid', 'high_water', 'page_json', 'used_json', 'updated_at'},
                'subscription_delivery_history': {'user_id', 'query', 'post_id', 'sent_at'},
                'delivery_failures': {'is_permanent'},
            }.items():
                actual = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
                if not expected <= actual:
                    raise RuntimeError(f'Migration 6 is recorded but {table} columns are missing')
        await db.execute("""CREATE TABLE IF NOT EXISTS query_progress (
            kind TEXT NOT NULL, user_id INTEGER NOT NULL, query TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '', pid INTEGER NOT NULL DEFAULT 0,
            high_water INTEGER NOT NULL DEFAULT 0, page_json TEXT NOT NULL DEFAULT '[]',
            used_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(kind, user_id, query))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS subscription_delivery_history (
            user_id INTEGER NOT NULL, query TEXT NOT NULL, post_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, query, post_id),
            FOREIGN KEY(user_id, query) REFERENCES subscriptions(user_id, query) ON DELETE CASCADE)""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_query_progress_updated ON query_progress(kind, user_id, updated_at)")
        await db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
        columns = {row[1] for row in await (await db.execute('PRAGMA table_info(delivery_failures)')).fetchall()}
        if 'is_permanent' not in columns:
            await db.execute('ALTER TABLE delivery_failures ADD COLUMN is_permanent INTEGER NOT NULL DEFAULT 0')
        recorded_seconds = await (await db.execute('SELECT 1 FROM schema_migrations WHERE version=7')).fetchone()
        columns = {row[1] for row in await (await db.execute('PRAGMA table_info(subscriptions)')).fetchall()}
        if recorded_seconds and 'interval_seconds' not in columns:
            raise RuntimeError('Migration 7 recorded but interval_seconds missing')
        if not recorded_seconds:
            if 'interval_seconds' not in columns:
                await db.execute('ALTER TABLE subscriptions ADD COLUMN interval_seconds INTEGER CHECK(interval_seconds IS NULL OR (typeof(interval_seconds) = "integer" AND interval_seconds >= 30))')
            await db.execute('UPDATE subscriptions SET interval_seconds = interval_minutes * 60 WHERE interval_seconds IS NULL')
            await db.execute('INSERT INTO schema_migrations(version) VALUES (7)')
        # Compatibility for old minute-based callers/SQL; scheduling reads seconds only.
        await db.execute("""CREATE TRIGGER IF NOT EXISTS subscription_legacy_interval_insert
            AFTER INSERT ON subscriptions WHEN NEW.interval_seconds IS NULL
            BEGIN UPDATE subscriptions SET interval_seconds = NEW.interval_minutes * 60
            WHERE user_id = NEW.user_id AND query = NEW.query; END""")
        await db.execute("""CREATE TRIGGER IF NOT EXISTS subscription_legacy_interval_update
            AFTER UPDATE OF interval_minutes ON subscriptions
            WHEN NEW.interval_minutes IS NOT OLD.interval_minutes AND NEW.interval_seconds IS OLD.interval_seconds
            BEGIN UPDATE subscriptions SET interval_seconds = NEW.interval_minutes * 60
            WHERE user_id = NEW.user_id AND query = NEW.query; END""")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def init_db(runtime):
    async with runtime.connect_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_query TEXT DEFAULT '',
                last_pid INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER,
                tag TEXT,
                PRIMARY KEY (user_id, tag)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER,
                query TEXT,
                interval_minutes INTEGER DEFAULT 10,
                is_active BOOLEAN DEFAULT 1,
                last_sent TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                no_new_posts_count INTEGER DEFAULT 0,
                last_empty_at TIMESTAMP,
                next_check_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                exhausted_notified BOOLEAN DEFAULT 0,
                processing_until TIMESTAMP,
                processing_token TEXT,
                PRIMARY KEY (user_id, query)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                user_id INTEGER,
                query TEXT,
                searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                post_id INTEGER,
                file_url TEXT,
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_posts (
                user_id INTEGER,
                post_id INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_posts (
                user_id INTEGER,
                query TEXT,
                post_id INTEGER,
                file_url TEXT,
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, query, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS post_cache (
                post_id INTEGER PRIMARY KEY,
                file_url TEXT DEFAULT '',
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_cache (
                user_id INTEGER,
                query TEXT,
                post_id INTEGER,
                file_url TEXT,
                sample_url TEXT DEFAULT '',
                preview_url TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                rating TEXT DEFAULT '',
                score INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, query, post_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                show_caption BOOLEAN DEFAULT 1,
                show_search_query BOOLEAN DEFAULT 1,
                show_subscription_label BOOLEAN DEFAULT 1,
                show_id BOOLEAN DEFAULT 1,
                show_score BOOLEAN DEFAULT 1,
                show_rating BOOLEAN DEFAULT 1,
                show_tags BOOLEAN DEFAULT 1,
                settings_json TEXT DEFAULT '{}'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS callback_payloads (
                action TEXT NOT NULL,
                token TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (action, token)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_collections (
                collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_collection_items (
                collection_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(collection_id, post_id),
                FOREIGN KEY(collection_id) REFERENCES favorite_collections(collection_id)
                    ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_notes (
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS delivery_failures (
                failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                caption TEXT DEFAULT '',
                attempts INTEGER DEFAULT 1,
                last_error TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claim_token TEXT,
                claimed_at TIMESTAMP,
                claim_until TIMESTAMP,
                UNIQUE(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                post_id INTEGER,
                query TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS search_presets (
                preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                query TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS read_later (
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                PRIMARY KEY(user_id, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_digest_queue (
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                post_json TEXT NOT NULL,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claim_token TEXT,
                claimed_at TIMESTAMP,
                claim_until TIMESTAMP,
                delivery_state TEXT NOT NULL DEFAULT 'pending',
                retry_after TIMESTAMP,
                PRIMARY KEY(user_id, query, post_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tag_translations (
                tag TEXT PRIMARY KEY,
                translation_ru TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'queue',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_history_user_query
            ON search_history (user_id, query)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_history_user_searched
            ON search_history (user_id, searched_at)
        """)
        await runtime.ensure_subscription_columns(db)
        await runtime.ensure_blacklist_columns(db)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_next_check
            ON subscriptions (is_active, next_check_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_posts_user_sent
            ON sent_posts (user_id, sent_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_favorites_user_added
            ON favorites (user_id, added_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_posts_user_query_sent
            ON subscription_posts (user_id, query, sent_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_read_later_expiry
            ON read_later (user_id, expires_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_digest_queue_user
            ON subscription_digest_queue (user_id, queued_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_translations_pending
            ON tag_translations (status, next_retry_at, updated_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_collection_items_user
            ON favorite_collection_items (user_id, collection_id, added_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_events_user_created
            ON bot_events (user_id, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_delivery_failures_created
            ON delivery_failures (created_at)
        """)
        await db.execute("""
            DELETE FROM bot_events
            WHERE event_id NOT IN (
                SELECT event_id FROM bot_events ORDER BY event_id DESC LIMIT 100000
            )
        """)
        await runtime.ensure_media_post_columns(db, "favorites")
        await runtime.ensure_media_post_columns(db, "subscription_posts")
        await runtime.ensure_subscription_cache_columns(db)

        await db.commit()
        await runtime.apply_versioned_migrations(db)
