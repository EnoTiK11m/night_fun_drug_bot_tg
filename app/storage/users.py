"""Domain implementation. Dependencies are supplied by the public facade."""
from app.observability.db_diagnostics import db_operation

async def get_user_blacklist(runtime, user_id):
    async with runtime.connect_db() as db:
        await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND expires_at IS NOT NULL "
            "AND expires_at <= CURRENT_TIMESTAMP",
            (user_id,),
        )
        cursor = await db.execute(
            "SELECT tag FROM blacklist WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        await db.commit()
        return {row[0] for row in rows}


async def add_to_blacklist(runtime, user_id, tag):
    tag = tag.lower().strip()
    async with runtime.connect_db() as db:
        try:
            await db.execute(
                "INSERT INTO blacklist (user_id, tag) VALUES (?, ?)",
                (user_id, tag)
            )
            await db.commit()
            return True
        except runtime.aiosqlite.IntegrityError:
            return False


async def add_temporary_blacklist_tag(runtime, user_id, tag, minutes):
    tag = tag.lower().strip()
    minutes = max(1, min(int(minutes), 30 * 24 * 60))
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT COALESCE(source, 'manual') FROM blacklist WHERE user_id = ? AND tag = ?",
            (user_id, tag),
        )
        row = await cursor.fetchone()
        if row and row[0] != "temporary":
            return False
        await db.execute("""
            INSERT INTO blacklist (user_id, tag, expires_at, source)
            VALUES (?, ?, datetime('now', '+' || ? || ' minutes'), 'temporary')
            ON CONFLICT(user_id, tag) DO UPDATE SET
                expires_at = excluded.expires_at,
                source = 'temporary'
        """, (user_id, tag, minutes))
        await db.commit()
        return True


async def get_blacklist_entries(runtime, user_id):
    await runtime.get_user_blacklist(user_id)
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT tag, expires_at, COALESCE(source, 'manual')
            FROM blacklist WHERE user_id = ? ORDER BY tag
        """, (user_id,))
        return [
            {"tag": row[0], "expires_at": row[1], "source": row[2]}
            for row in await cursor.fetchall()
        ]


async def apply_blacklist_preset(runtime, user_id, preset):
    tags = runtime.BLACKLIST_PRESETS.get(preset, set())
    added_tags = []
    for tag in tags:
        if await runtime.add_to_blacklist(user_id, tag):
            added_tags.append(tag)
    if added_tags:
        async with runtime.connect_db() as db:
            placeholders = ",".join("?" for _ in added_tags)
            await db.execute(
                f"UPDATE blacklist SET source = ? WHERE user_id = ? AND tag IN ({placeholders})",
                (f"preset:{preset}", user_id, *added_tags),
            )
            await db.commit()
    return len(added_tags)


async def remove_blacklist_preset(runtime, user_id, preset):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND source = ?",
            (user_id, f"preset:{preset}"),
        )
        await db.commit()
        return cursor.rowcount


async def replace_user_blacklist(runtime, user_id, tags):
    normalized = sorted({tag.lower().strip() for tag in tags if tag.strip()})[:500]
    async with runtime.connect_db() as db:
        await db.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        await db.executemany(
            "INSERT INTO blacklist (user_id, tag, source) VALUES (?, ?, 'import')",
            [(user_id, tag) for tag in normalized],
        )
        await db.commit()
    return len(normalized)


async def remove_from_blacklist(runtime, user_id, tag):
    tag = tag.lower().strip()
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM blacklist WHERE user_id = ? AND tag = ?",
            (user_id, tag)
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_user_query(runtime, user_id, query, pid):
    async with runtime.connect_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, last_query, last_pid)
            VALUES (?, ?, ?)
        """, (user_id, query, pid))
        await db.execute("""
            INSERT INTO search_history (user_id, query)
            VALUES (?, ?)
        """, (user_id, query.strip()))
        await db.execute("""
            INSERT INTO bot_events (user_id, event_type, query)
            VALUES (?, 'search', ?)
        """, (user_id, query.strip()))
        await db.execute("""
            DELETE FROM search_history
            WHERE user_id = ?
              AND rowid NOT IN (
                SELECT rowid
                FROM search_history
                WHERE user_id = ?
                ORDER BY searched_at DESC, rowid DESC
                LIMIT ?
              )
        """, (user_id, user_id, runtime.SEARCH_HISTORY_RETENTION_PER_USER))
        await db.commit()


async def get_user_query(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT last_query, last_pid FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()


async def get_sent_post_ids(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT post_id FROM sent_posts WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


@db_operation("subscription.sent_history")
async def mark_post_sent(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO sent_posts (user_id, post_id)
            VALUES (?, ?)
        """, (user_id, post_id))
        if cursor.rowcount:
            await db.execute("""
                INSERT INTO bot_events (user_id, event_type, post_id)
                VALUES (?, 'viewed', ?)
            """, (user_id, post_id))
        await db.execute("""
            DELETE FROM sent_posts
            WHERE user_id = ?
              AND post_id NOT IN (
                SELECT post_id
                FROM sent_posts
                WHERE user_id = ?
                ORDER BY sent_at DESC, rowid DESC
                LIMIT ?
              )
        """, (user_id, user_id, runtime.SENT_POSTS_RETENTION_PER_USER))
        await db.commit()


def _post_from_row(runtime, row):
    return {
        "id": row[0],
        "file_url": row[1],
        "sample_url": row[2] or "",
        "preview_url": row[3] or "",
        "tags": row[4] or "",
        "rating": row[5] or "",
        "score": row[6] or 0,
    }


def _subscription_post_from_row(runtime, row):
    post = runtime._post_from_row(row)
    post["width"] = int(row[7] or 0)
    post["height"] = int(row[8] or 0)
    return post


def _normalize_post(runtime, post):
    try:
        post_id = int(post.get("id"))
    except (TypeError, ValueError):
        return None

    return (
        post_id,
        post.get("file_url", "") or "",
        post.get("sample_url", "") or "",
        post.get("preview_url", "") or "",
        post.get("tags", "") or "",
        post.get("rating", "") or "",
        int(post.get("score") or 0),
    )


async def get_search_history(runtime, user_id, limit):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT query
            FROM search_history
            WHERE user_id = ? AND query <> ''
            GROUP BY query
            ORDER BY MAX(rowid) DESC
            LIMIT ?
        """, (user_id, limit))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


@db_operation("user.settings")
async def get_user_settings(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()

        if row:
            settings = runtime.DEFAULT_USER_SETTINGS.copy()
            settings.update({
                "show_caption": bool(row[1]),
                "show_search_query": bool(row[2]),
                "show_subscription_label": bool(row[3]),
                "show_id": bool(row[4]),
                "show_score": bool(row[5]),
                "show_rating": bool(row[6]),
                "show_tags": bool(row[7]),
            })

            if row[8]:
                try:
                    json_settings = runtime._json_object(row[8])
                    settings.update(json_settings)
                except runtime.json.JSONDecodeError:
                    runtime.logger.warning("Invalid settings JSON for user %s", user_id)

            return settings

        await db.execute("INSERT INTO user_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING", (user_id,))
        await db.commit()
    return await runtime.get_user_settings(user_id)


@db_operation("user.settings")
async def save_user_settings(runtime, user_id, settings):
    """Atomically merge a whitelisted partial settings patch."""
    unknown_fields = set(settings) - runtime.USER_SETTING_FIELDS
    if unknown_fields:
        raise ValueError(
            f"Unsupported user setting fields: {', '.join(sorted(unknown_fields))}"
        )
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            merged_settings = runtime.DEFAULT_USER_SETTINGS.copy()
            cursor = await db.execute(
                "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                merged_settings.update({
                    "show_caption": bool(row[1]),
                    "show_search_query": bool(row[2]),
                    "show_subscription_label": bool(row[3]),
                    "show_id": bool(row[4]),
                    "show_score": bool(row[5]),
                    "show_rating": bool(row[6]),
                    "show_tags": bool(row[7]),
                })
                if row[8]:
                    try:
                        merged_settings.update(runtime._json_object(row[8]))
                    except runtime.json.JSONDecodeError:
                        runtime.logger.warning("Invalid settings JSON for user %s", user_id)

            merged_settings.update(settings)
            json_settings = {
                key: value
                for key, value in merged_settings.items()
                if key not in runtime.MAIN_SETTING_FIELDS
            }
            await db.execute("""
                INSERT INTO user_settings
                (user_id, show_caption, show_search_query, show_subscription_label,
                 show_id, show_score, show_rating, show_tags, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    show_caption = excluded.show_caption,
                    show_search_query = excluded.show_search_query,
                    show_subscription_label = excluded.show_subscription_label,
                    show_id = excluded.show_id,
                    show_score = excluded.show_score,
                    show_rating = excluded.show_rating,
                    show_tags = excluded.show_tags,
                    settings_json = excluded.settings_json
            """, (
                user_id,
                bool(merged_settings["show_caption"]),
                bool(merged_settings["show_search_query"]),
                bool(merged_settings["show_subscription_label"]),
                bool(merged_settings["show_id"]),
                bool(merged_settings["show_score"]),
                bool(merged_settings["show_rating"]),
                bool(merged_settings["show_tags"]),
                runtime.json.dumps(json_settings),
            ))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def update_user_setting(runtime, user_id, setting_name, value):
    await runtime.save_user_settings(user_id, {setting_name: value})


def _parse_sqlite_timestamp(runtime, value):
    if not value:
        return None
    try:
        return runtime.datetime.strptime(value, runtime.SQLITE_TIMESTAMP_FORMAT)
    except ValueError:
        runtime.logger.warning("Invalid SQLite timestamp value: %s", value)
        return None


async def _get_settings_json(runtime, db, user_id):
    cursor = await db.execute(
        "SELECT settings_json FROM user_settings WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return {}
    try:
        return runtime._json_object(row[0])
    except runtime.json.JSONDecodeError:
        runtime.logger.warning("Invalid settings JSON for user %s", user_id)
        return {}


async def _save_settings_json(runtime, db, user_id, settings_json):
    await db.execute("""
        INSERT INTO user_settings (user_id, settings_json)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json
    """, (user_id, runtime.json.dumps(settings_json)))


async def _get_active_subscription_pause_until(runtime, db, user_id):
    settings_json = await runtime._get_settings_json(db, user_id)
    pause_until = settings_json.get(runtime.SUBSCRIPTION_PAUSE_SETTING)
    pause_until_dt = runtime._parse_sqlite_timestamp(pause_until)
    if pause_until_dt and pause_until_dt > runtime.datetime.now(runtime.UTC).replace(tzinfo=None):
        return pause_until
    if pause_until:
        settings_json.pop(runtime.SUBSCRIPTION_PAUSE_SETTING, None)
        await runtime._save_settings_json(db, user_id, settings_json)
    return None


async def get_user_activity_stats(runtime, user_id):
    async with runtime.connect_db() as db:
        async def scalar(sql: str, params=()) -> int:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            return int(row[0] or 0)

        stats = {
            "viewed_total": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ?", (user_id,)
            ),
            "favorites_total": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,)
            ),
            "searches_total": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ?", (user_id,)
            ),
            "subscriptions_active": await scalar(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND is_active = 1",
                (user_id,),
            ),
            "viewed_week": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ? AND sent_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "favorites_week": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ? AND added_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "favorites_month": await scalar(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ? AND added_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
            "searches_week": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND searched_at >= datetime('now', '-7 days')",
                (user_id,),
            ),
            "searches_month": await scalar(
                "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND searched_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
            "viewed_month": await scalar(
                "SELECT COUNT(*) FROM sent_posts WHERE user_id = ? AND sent_at >= datetime('now', '-30 days')",
                (user_id,),
            ),
        }
        event_windows = {
            "viewed_total": ("viewed", ""),
            "viewed_week": ("viewed", "AND created_at >= datetime('now', '-7 days')"),
            "viewed_month": ("viewed", "AND created_at >= datetime('now', '-30 days')"),
            "searches_total": ("search", ""),
            "searches_week": ("search", "AND created_at >= datetime('now', '-7 days')"),
            "searches_month": ("search", "AND created_at >= datetime('now', '-30 days')"),
        }
        for key, (event_type, time_where) in event_windows.items():
            event_count = await scalar(
                f"SELECT COUNT(*) FROM bot_events WHERE user_id = ? AND event_type = ? {time_where}",
                (user_id, event_type),
            )
            stats[key] = max(stats[key], event_count)
        cursor = await db.execute("""
            SELECT query, COUNT(*) AS uses
            FROM search_history
            WHERE user_id = ? AND query <> ''
            GROUP BY query ORDER BY uses DESC, MAX(searched_at) DESC LIMIT 5
        """, (user_id,))
        stats["top_queries"] = await cursor.fetchall()
        cursor = await db.execute("""
            SELECT COALESCE(NULLIF(pc.tags, ''), f.tags)
            FROM favorites f LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ?
        """, (user_id,))
        tag_counts: runtime.Dict[str, int] = {}
        for row in await cursor.fetchall():
            for tag in (row[0] or "").split():
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        stats["top_tags"] = sorted(
            tag_counts.items(), key=lambda item: (-item[1], item[0])
        )[:8]
        return stats


async def clear_user_activity_stats(runtime, user_id):
    async with runtime.connect_db() as db:
        await db.execute("DELETE FROM sent_posts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM bot_events WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_admin_database_stats(runtime):
    async with runtime.connect_db() as db:
        cursor = await db.execute("PRAGMA quick_check")
        quick_check = (await cursor.fetchone())[0]
        counts = {}
        for table in (
            "users", "favorites", "subscriptions", "sent_posts",
            "delivery_failures", "bot_events", "tag_translations",
        ):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int((await cursor.fetchone())[0] or 0)
        return {"quick_check": quick_check, "counts": counts}


async def create_search_preset(runtime, user_id, name, query, settings):
    name, query = name.strip()[:40], query.strip()[:500]
    if not name or not query:
        return None
    async with runtime.connect_db() as db:
        try:
            cursor = await db.execute("""
                INSERT INTO search_presets (user_id, name, query, settings_json)
                VALUES (?, ?, ?, ?)
            """, (user_id, name, query, runtime.json.dumps(settings)))
            await db.commit()
            return int(cursor.lastrowid)
        except runtime.aiosqlite.IntegrityError:
            return None


async def get_search_presets(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT preset_id, name, query, settings_json
            FROM search_presets WHERE user_id = ? ORDER BY name COLLATE NOCASE
        """, (user_id,))
        result = []
        for preset_id, name, query, raw_settings in await cursor.fetchall():
            try:
                settings = runtime._json_object(raw_settings or "{}")
            except runtime.json.JSONDecodeError:
                settings = {}
            result.append({"id": preset_id, "name": name, "query": query, "settings": settings})
        return result


async def get_search_preset(runtime, user_id, preset_id):
    presets = await runtime.get_search_presets(user_id)
    return next((item for item in presets if item["id"] == preset_id), None)


async def delete_search_preset(runtime, user_id, preset_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM search_presets WHERE user_id = ? AND preset_id = ?",
            (user_id, preset_id),
        )
        await db.commit()
        return cursor.rowcount > 0
