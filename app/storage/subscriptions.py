"""Domain implementation. Dependencies are supplied by the public facade."""

async def get_subscription_pause_until(runtime, user_id):
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            pause_until = await runtime._get_active_subscription_pause_until(db, user_id)
            await db.commit()
            return pause_until
        except BaseException:
            await db.rollback()
            raise


async def _subscription_counts(runtime, db, user_id):
    cursor = await db.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0)
        FROM subscriptions WHERE user_id = ?
    """, (user_id,))
    row = await cursor.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


async def get_subscription_usage(runtime, user_id):
    async with runtime.connect_db() as db:
        return await runtime._subscription_counts(db, user_id)


async def add_subscription(runtime, user_id, query, interval_minutes, *, interval_seconds, total_limit, active_limit, cooldown_seconds):
    seconds = interval_minutes * 60 if interval_seconds is None else interval_seconds
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < runtime.SUBSCRIPTION_MIN_INTERVAL_SECONDS:
        return runtime.SubscriptionAddResult('invalid_interval')
    legacy_minutes = seconds // 60 if seconds % 60 == 0 else None
    stripped_query = query.strip() if isinstance(query, str) else ""
    normalized_query, validation_error = runtime.validate_subscription_query(query)
    configured_total = max(1, int(
        runtime.SUBSCRIPTION_MAX_TOTAL if total_limit is None else total_limit
    ))
    configured_active = max(1, min(configured_total, int(
        runtime.SUBSCRIPTION_MAX_ACTIVE if active_limit is None else active_limit
    )))
    configured_cooldown = max(0, int(
        runtime.SUBSCRIPTION_CREATE_COOLDOWN_SECONDS
        if cooldown_seconds is None else cooldown_seconds
    ))
    if validation_error:
        return runtime.SubscriptionAddResult(
            validation_error,
            total_limit=configured_total,
            active_limit=configured_active,
        )

    try:
        async with runtime.connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT query, is_active FROM subscriptions
                    WHERE user_id = ? AND query = ?
                """, (user_id, stripped_query))
                existing = await cursor.fetchone()
                if existing is None:
                    cursor = await db.execute("""
                        SELECT query, is_active FROM subscriptions
                        WHERE user_id = ? ORDER BY query COLLATE BINARY
                    """, (user_id,))
                    canonical_matches = [
                        row for row in await cursor.fetchall()
                        if runtime.normalize_subscription_query(row[0]) == normalized_query
                    ]
                    if len(canonical_matches) > 1:
                        await db.rollback()
                        return runtime.SubscriptionAddResult(
                            "ambiguous_query",
                            total_limit=configured_total,
                            active_limit=configured_active,
                        )
                    existing = canonical_matches[0] if canonical_matches else None
                total_count, active_count = await runtime._subscription_counts(db, user_id)
                stored_query = existing[0] if existing is not None else normalized_query

                if existing is not None and bool(existing[1]):
                    await db.execute("""
                        UPDATE subscriptions SET interval_minutes = ?, interval_seconds = ?
                        WHERE user_id = ? AND query = ?
                    """, (legacy_minutes, seconds, user_id, stored_query))
                    await db.commit()
                    return runtime.SubscriptionAddResult(
                        "updated", total_count, active_count,
                        configured_total, configured_active,
                    )

                if existing is not None:
                    if total_count > configured_total:
                        await db.rollback()
                        return runtime.SubscriptionAddResult(
                            "total_limit_reached", total_count, active_count,
                            configured_total, configured_active,
                        )
                    if active_count >= configured_active:
                        await db.rollback()
                        return runtime.SubscriptionAddResult(
                            "active_limit_reached", total_count, active_count,
                            configured_total, configured_active,
                        )
                    await db.execute("""
                        UPDATE subscriptions
                        SET interval_minutes = ?, interval_seconds = ?, is_active = 1
                        WHERE user_id = ? AND query = ? AND is_active = 0
                    """, (legacy_minutes, seconds, user_id, stored_query))
                    await db.commit()
                    return runtime.SubscriptionAddResult(
                        "reactivated", total_count, active_count + 1,
                        configured_total, configured_active,
                    )

                if total_count >= configured_total:
                    await db.rollback()
                    return runtime.SubscriptionAddResult(
                        "total_limit_reached", total_count, active_count,
                        configured_total, configured_active,
                    )
                if active_count >= configured_active:
                    await db.rollback()
                    return runtime.SubscriptionAddResult(
                        "active_limit_reached", total_count, active_count,
                        configured_total, configured_active,
                    )

                cursor = await db.execute("""
                    SELECT MAX(
                        0,
                        ? - (
                            CAST(strftime('%s', 'now') AS INTEGER)
                            - CAST(strftime('%s', last_created_at) AS INTEGER)
                        )
                    )
                    FROM subscription_creation_state WHERE user_id = ?
                """, (configured_cooldown, user_id))
                row = await cursor.fetchone()
                retry_after = int((row[0] if row else 0) or 0)
                if retry_after > 0:
                    await db.rollback()
                    return runtime.SubscriptionAddResult(
                        "cooldown", total_count, active_count,
                        configured_total, configured_active, retry_after,
                    )

                pause_until = await runtime._get_active_subscription_pause_until(db, user_id)
                await db.execute("""
                    INSERT INTO subscriptions
                    (
                        user_id, query, interval_minutes, interval_seconds, is_active, last_sent,
                        no_new_posts_count, last_empty_at, next_check_at,
                        exhausted_notified, processing_until, processing_token
                    )
                    VALUES (
                        ?, ?, ?, ?, 1, datetime('now', '-1 hour'), 0, NULL,
                        COALESCE(?, datetime('now')), 0, NULL, NULL
                    )
                """, (user_id, normalized_query, legacy_minutes, seconds, pause_until))
                await db.execute("""
                    INSERT INTO subscription_creation_state(user_id, last_created_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_created_at = CURRENT_TIMESTAMP
                """, (user_id,))
                await db.commit()
                return runtime.SubscriptionAddResult(
                    "created", total_count + 1, active_count + 1,
                    configured_total, configured_active,
                )
            except BaseException:
                await db.rollback()
                raise
    except Exception as exc:
        runtime.logger.exception("Error adding subscription: %s", type(exc).__name__)
        return runtime.SubscriptionAddResult(
            "internal_error",
            total_limit=configured_total,
            active_limit=configured_active,
        )


async def remove_subscription(runtime, user_id, query):
    async with runtime.connect_db() as db:
        normalized_query = query.strip()
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Pending and claimed digest rows belong to a deleted subscription.
            # Deactivation, in contrast, preserves them until reactivation.
            await db.execute(
                "DELETE FROM subscription_digest_queue WHERE user_id = ? AND query = ?",
                (user_id, normalized_query),
            )
            for table in ("subscription_posts", "subscription_cache", "subscription_delivery_history"):
                await db.execute(f"DELETE FROM {table} WHERE user_id = ? AND query = ?", (user_id, normalized_query))
            await db.execute("DELETE FROM query_progress WHERE kind='subscription' AND user_id=? AND query=?", (user_id, normalized_query))
            cursor = await db.execute(
                "DELETE FROM subscriptions WHERE user_id = ? AND query = ?",
                (user_id, normalized_query)
            )
            await db.commit()
            return cursor.rowcount > 0
        except BaseException:
            await db.rollback()
            raise


async def get_user_subscriptions(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT query, interval_seconds FROM subscriptions WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]


async def get_all_user_subscriptions(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT query, interval_seconds, is_active, no_new_posts_count, next_check_at
            FROM subscriptions
            WHERE user_id = ?
        """, (user_id,))
        rows = await cursor.fetchall()
        return [(row[0], row[1], bool(row[2]), row[3] or 0, row[4]) for row in rows]


async def update_subscription_time(runtime, user_id, query, processing_token):
    async with runtime.connect_db() as db:
        params: tuple[runtime.Any, ...]
        token_filter = ""
        if processing_token is not None:
            token_filter = " AND processing_token = ?"
            params = (user_id, query.strip(), processing_token)
        else:
            params = (user_id, query.strip())

        cursor = await db.execute(f"""
            UPDATE subscriptions
            SET last_sent = CURRENT_TIMESTAMP,
                no_new_posts_count = 0,
                last_empty_at = NULL,
                next_check_at = datetime('now', '+' || interval_seconds || ' seconds'),
                exhausted_notified = 0,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
            {token_filter}
        """, params)
        await db.commit()
        runtime.trace_event("db.subscription.update", level="normal", acknowledged=cursor.rowcount > 0)
        runtime.trace_event("subscription.schedule.updated", level="normal", acknowledged=cursor.rowcount > 0)
        return cursor.rowcount > 0


async def mark_subscription_empty(runtime, user_id, query, processing_token):
    query = query.strip()
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT interval_seconds, no_new_posts_count, exhausted_notified
            FROM subscriptions
            WHERE user_id = ? AND query = ?
        """, (user_id, query))
        row = await cursor.fetchone()
        if not row:
            return 0, 0, False

        interval_minutes = (int(row[0] or 600) + 59) // 60
        empty_count = int(row[1] or 0) + 1
        should_notify = not bool(row[2])
        backoff_minutes = runtime.get_empty_backoff_minutes(empty_count, interval_minutes)

        params: tuple[runtime.Any, ...]
        token_filter = ""
        if processing_token is not None:
            token_filter = " AND processing_token = ?"
            params = (empty_count, backoff_minutes, user_id, query, processing_token)
        else:
            params = (empty_count, backoff_minutes, user_id, query)

        cursor = await db.execute(f"""
            UPDATE subscriptions
            SET last_empty_at = CURRENT_TIMESTAMP,
                no_new_posts_count = ?,
                next_check_at = datetime('now', '+' || ? || ' minutes'),
                exhausted_notified = 1,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
            {token_filter}
        """, params)
        await db.commit()
        if cursor.rowcount != 1:
            return 0, 0, False
        return empty_count, backoff_minutes, should_notify


async def update_subscription_interval(runtime, user_id, query, interval_minutes, *, interval_seconds):
    seconds = interval_minutes * 60 if interval_seconds is None else interval_seconds
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < runtime.SUBSCRIPTION_MIN_INTERVAL_SECONDS:
        raise ValueError('Subscription interval must be integer seconds >= minimum')
    legacy_minutes = seconds // 60 if seconds % 60 == 0 else None
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET interval_minutes = ?, interval_seconds = ?,
                no_new_posts_count = 0,
                last_empty_at = NULL,
                next_check_at = datetime('now', '+' || ? || ' seconds'),
                exhausted_notified = 0,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ?
        """, (legacy_minutes, seconds, seconds, user_id, query.strip()))
        await db.commit()
        return cursor.rowcount > 0


async def pause_all_active_subscriptions(runtime, user_id, pause_minutes):
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            pause_until = (
                runtime.datetime.now(runtime.UTC).replace(tzinfo=None) + runtime.timedelta(minutes=pause_minutes)
            ).strftime(runtime.SQLITE_TIMESTAMP_FORMAT)
            settings_json = await runtime._get_settings_json(db, user_id)
            settings_json[runtime.SUBSCRIPTION_PAUSE_SETTING] = pause_until
            await runtime._save_settings_json(db, user_id, settings_json)
            cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = CASE
                    WHEN datetime(COALESCE(next_check_at, last_sent)) >
                         datetime(?)
                    THEN next_check_at
                    ELSE ?
                END,
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND is_active = 1
            """, (pause_until, pause_until, user_id))
            await db.commit()
            return cursor.rowcount
        except BaseException:
            await db.rollback()
            raise


async def resume_all_active_subscriptions(runtime, user_id):
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            settings_json = await runtime._get_settings_json(db, user_id)
            settings_json.pop(runtime.SUBSCRIPTION_PAUSE_SETTING, None)
            await runtime._save_settings_json(db, user_id, settings_json)
            cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = datetime('now'),
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND is_active = 1
            """, (user_id,))
            await db.commit()
            return cursor.rowcount
        except BaseException:
            await db.rollback()
            raise


async def get_due_subscriptions(runtime):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT s.user_id, s.query, s.interval_seconds, s.no_new_posts_count
            FROM subscriptions s
            WHERE s.is_active = 1
            AND datetime(COALESCE(next_check_at, last_sent)) <= datetime('now')
            AND (
                processing_until IS NULL
                OR datetime(processing_until) <= datetime('now')
            )
            AND NOT EXISTS (
                SELECT 1 FROM user_settings us
                WHERE us.user_id = s.user_id
                  AND datetime(json_extract(
                      CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                           THEN us.settings_json ELSE '{}' END,
                      '$.subscription_pause_until'
                  )) > datetime('now')
            )
            ORDER BY datetime(COALESCE(s.next_check_at, s.last_sent)),
                     s.user_id, s.query COLLATE BINARY
            LIMIT 50
        """)
        return await cursor.fetchall()


async def claim_due_subscription(runtime, user_id, query):
    token = runtime.uuid.uuid4().hex
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET processing_until = datetime('now', '+' || ? || ' minutes'),
                processing_token = ?
            WHERE user_id = ?
              AND query = ?
              AND is_active = 1
              AND datetime(COALESCE(next_check_at, last_sent)) <= datetime('now')
              AND (
                processing_until IS NULL
                  OR datetime(processing_until) <= datetime('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = subscriptions.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (runtime.SUBSCRIPTION_CLAIM_MINUTES, token, user_id, query.strip()))
        await db.commit()
        runtime.trace_event('claim.created' if cursor.rowcount == 1 else 'claim.creation_rejected', level='normal', claim_token_hash=runtime.safe_hash(token), acknowledged=cursor.rowcount == 1)
        return token if cursor.rowcount == 1 else None


async def renew_subscription_claim(runtime, user_id, query, processing_token):
    """Extend only the still-live, unpaused lease owned by this worker."""
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET processing_until = datetime('now', '+' || ? || ' minutes')
            WHERE user_id = ? AND query = ? AND processing_token = ?
              AND is_active = 1
              AND datetime(processing_until) > datetime('now')
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = subscriptions.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (runtime.SUBSCRIPTION_CLAIM_MINUTES, user_id, query.strip(), processing_token))
        await db.commit()
        return cursor.rowcount == 1


async def defer_subscription_after_transient_failure(runtime, user_id, query, processing_token, backoff_seconds):
    """Persist a short retry delay and release only the caller's live claim."""
    bounded_backoff = max(1, min(int(backoff_seconds), 3600))
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscriptions
            SET next_check_at = datetime('now', '+' || ? || ' seconds'),
                processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ? AND query = ? AND processing_token = ?
        """, (bounded_backoff, user_id, query.strip(), processing_token))
        await db.commit()
        return cursor.rowcount == 1


async def is_subscription_claim_active(runtime, user_id, query, processing_token):
    """Check that a subscription claim is live, active, and not globally paused."""
    async with runtime.connect_db() as db:
        if runtime.trace_enabled('normal'):
            diagnostic = await (await db.execute("""
                SELECT s.is_active, s.processing_token = ?,
                    s.processing_until IS NOT NULL AND datetime(s.processing_until)>datetime('now'),
                    EXISTS(SELECT 1 FROM user_settings us WHERE us.user_id=s.user_id
                        AND datetime(json_extract(CASE WHEN json_valid(COALESCE(us.settings_json,'{}')) THEN us.settings_json ELSE '{}' END,
                            '$.subscription_pause_until'))>datetime('now'))
                FROM subscriptions s WHERE s.user_id=? AND s.query=?
            """, (processing_token, user_id, query.strip()))).fetchone()
            active, matches, live, paused = diagnostic if diagnostic else (False, False, False, False)
            valid = bool(active and matches and live and not paused)
            runtime.annotate(claim_active=bool(active), claim_paused=bool(paused))
            runtime.trace_event('claim.revalidated' if valid else 'claim.invalidated', level='normal', active=bool(active), paused=bool(paused), claim_valid=valid, exists=diagnostic is not None, claim_token_hash=runtime.safe_hash(processing_token))
            runtime.trace_event('claim.snapshot', level='verbose', active=bool(active), paused=bool(paused), claim_valid=valid)
            if diagnostic and not live:
                runtime.trace_event('claim.expired', level='normal')
            return valid
        cursor = await db.execute("""
            SELECT 1 FROM subscriptions s
            WHERE s.user_id = ? AND s.query = ?
              AND s.processing_token = ?
              AND s.processing_until IS NOT NULL
              AND datetime(s.processing_until) > datetime('now')
              AND s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = s.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (user_id, query.strip(), processing_token))
        return await cursor.fetchone() is not None


async def release_subscription_claim(runtime, user_id, query, processing_token):
    async with runtime.connect_db() as db:
        await db.execute("""
            UPDATE subscriptions
            SET processing_until = NULL,
                processing_token = NULL
            WHERE user_id = ?
              AND query = ?
              AND processing_token = ?
        """, (user_id, query.strip(), processing_token))
        await db.commit()
        runtime.trace_event("claim.released", level="normal", claim_token_hash=runtime.safe_hash(processing_token))
        runtime.trace_event("subscription.claim.released", level="normal")


async def release_stale_subscription_claims(runtime):
    async with runtime.connect_db() as db:
        await db.execute("""
            UPDATE subscriptions
            SET processing_until = NULL,
                processing_token = NULL
            WHERE processing_until IS NOT NULL
              AND datetime(processing_until) <= datetime('now')
        """)
        await db.commit()


async def toggle_subscription(runtime, user_id, query, *, active_limit, total_limit):
    configured_total = max(1, int(
        runtime.SUBSCRIPTION_MAX_TOTAL if total_limit is None else total_limit
    ))
    configured_active = max(1, min(configured_total, int(
        runtime.SUBSCRIPTION_MAX_ACTIVE if active_limit is None else active_limit
    )))
    stored_query = query.strip() if isinstance(query, str) else ""
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT is_active FROM subscriptions WHERE user_id = ? AND query = ?",
                (user_id, stored_query)
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return runtime.SubscriptionToggleResult(
                    "not_found", active_limit=configured_active,
                    total_limit=configured_total,
                )

            current_state = bool(row[0])
            total_count, active_count = await runtime._subscription_counts(db, user_id)
            if not current_state and total_count > configured_total:
                await db.rollback()
                return runtime.SubscriptionToggleResult(
                    "total_limit_reached", False, active_count,
                    configured_active, total_count, configured_total,
                )
            if not current_state and active_count >= configured_active:
                await db.rollback()
                return runtime.SubscriptionToggleResult(
                    "active_limit_reached", False, active_count,
                    configured_active, total_count, configured_total,
                )

            new_state = not current_state
            await db.execute("""
                UPDATE subscriptions
                SET is_active = ?,
                    next_check_at = CASE
                        WHEN ? = 1 THEN datetime('now')
                        ELSE next_check_at
                    END
                WHERE user_id = ? AND query = ?
            """, (
                new_state,
                int(new_state),
                user_id,
                stored_query,
            ))
            await db.commit()
            return runtime.SubscriptionToggleResult(
                "reactivated" if new_state else "deactivated",
                new_state,
                active_count + (1 if new_state else -1),
                configured_active,
                total_count,
                configured_total,
            )
        except BaseException:
            await db.rollback()
            raise


async def add_subscription_post(runtime, user_id, query, post):
    normalized = runtime._normalize_post(post)
    if normalized is None:
        return False
    post_id, file_url, sample_url, preview_url, tags, rating, score = normalized

    async with runtime.connect_db() as db:
        try:
            await db.execute("""
                INSERT INTO post_cache
                (post_id, file_url, sample_url, preview_url, tags, rating, score, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(post_id) DO UPDATE SET
                    file_url = COALESCE(NULLIF(excluded.file_url, ''), post_cache.file_url),
                    sample_url = COALESCE(NULLIF(excluded.sample_url, ''), post_cache.sample_url),
                    preview_url = COALESCE(NULLIF(excluded.preview_url, ''), post_cache.preview_url),
                    tags = COALESCE(NULLIF(excluded.tags, ''), post_cache.tags),
                    rating = COALESCE(NULLIF(excluded.rating, ''), post_cache.rating),
                    score = CASE WHEN excluded.score != 0 THEN excluded.score ELSE post_cache.score END,
                    cached_at = CURRENT_TIMESTAMP
            """, normalized)
            await db.execute("""
                INSERT INTO subscription_posts
                (user_id, query, post_id, file_url, sample_url, preview_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                query.strip(),
                post_id,
                file_url,
                sample_url,
                preview_url,
                tags,
                rating,
                score,
            ))
            await db.commit()
            return True
        except runtime.aiosqlite.IntegrityError:
            return False


async def get_subscription_posts(runtime, user_id, query, limit, offset):
    params: list[runtime.Any] = [user_id, query.strip()]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    async with runtime.connect_db() as db:
        cursor = await db.execute(f"""
            SELECT
                sp.post_id,
                COALESCE(NULLIF(pc.file_url, ''), sp.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), sp.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), sp.preview_url),
                COALESCE(NULLIF(pc.tags, ''), sp.tags),
                COALESCE(NULLIF(pc.rating, ''), sp.rating),
                COALESCE(pc.score, sp.score),
                sp.sent_at
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            LEFT JOIN post_cache pc ON pc.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
            ORDER BY sp.sent_at DESC, sp.post_id DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = runtime._post_from_row(row)
            post["sent_at"] = row[7]
            posts.append(post)
        return posts


async def count_subscription_posts(runtime, user_id, query):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT COUNT(*)
            FROM subscription_posts sp
            INNER JOIN favorites f
                ON f.user_id = sp.user_id
                AND f.post_id = sp.post_id
            WHERE sp.user_id = ? AND sp.query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def get_subscription_queries_for_post(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT DISTINCT sc.query
            FROM subscription_cache sc
            INNER JOIN subscriptions s
                ON s.user_id = sc.user_id
                AND s.query = sc.query
            WHERE sc.user_id = ?
              AND sc.post_id = ?
            ORDER BY sc.query
        """, (user_id, post_id))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_subscription_post_by_index(runtime, user_id, query, index):
    posts = await runtime.get_subscription_posts(
        user_id,
        query,
        limit=1,
        offset=max(0, index),
    )
    return posts[0] if posts else None


async def remove_subscription_post(runtime, user_id, query, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM subscription_posts
            WHERE user_id = ? AND query = ? AND post_id = ?
        """, (user_id, query.strip(), post_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_subscription_options(runtime, user_id, query):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT settings_json, digest_mode FROM subscriptions
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        if not row:
            return {"digest_mode": "instant"}
        try:
            options = runtime._json_object(row[0] or "{}")
        except runtime.json.JSONDecodeError:
            options = {}
        options["digest_mode"] = row[1] or "instant"
        return options


async def update_subscription_options(runtime, user_id, query, options):
    allowed = {"digest_mode", "extra_blacklist", "rating_filter", "media_type", "orientation", "min_width", "min_height", "quality_mode"}
    if set(options) - allowed:
        raise ValueError("Unsupported subscription option fields")
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT settings_json, digest_mode FROM subscriptions WHERE user_id=? AND query=?", (user_id, query.strip()))).fetchone()
        if row is None:
            await db.rollback()
            return False
        stored = runtime._json_object(row[0] or "{}")
        stored.update({key: value for key, value in options.items() if key != "digest_mode"})
        digest_mode = options.get("digest_mode", row[1] or "instant")
        if digest_mode not in {"instant", "digest"}:
            digest_mode = "instant"
        cursor = await db.execute("""
            UPDATE subscriptions SET settings_json = ?, digest_mode = ?
            WHERE user_id = ? AND query = ?
        """, (runtime.json.dumps(stored), digest_mode, user_id, query.strip()))
        await db.commit()
        return cursor.rowcount > 0


async def enqueue_subscription_digest(runtime, user_id, query, post):
    normalized = runtime._normalize_post(post)
    if normalized is None:
        return False
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO subscription_digest_queue
            (user_id, query, post_id, post_json)
            SELECT ?, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM subscriptions
                WHERE user_id = ? AND query = ? AND is_active = 1
            )
        """, (
            user_id,
            query.strip(),
            normalized[0],
            runtime.json.dumps(post),
            user_id,
            query.strip(),
        ))
        await db.commit()
        return cursor.rowcount > 0


async def count_subscription_digest(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM subscription_digest_queue WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def claim_subscription_digest(runtime, user_id, limit, lease_minutes):
    """Atomically lease a stable digest batch without deleting it."""
    token = runtime.uuid.uuid4().hex
    bounded_limit = max(1, min(int(limit), 10))
    bounded_lease = max(1, int(lease_minutes))
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
            SELECT q.query, q.post_id, q.post_json
            FROM subscription_digest_queue q
            INNER JOIN subscriptions s
              ON s.user_id = q.user_id AND s.query = q.query
            WHERE q.user_id = ?
              AND s.is_active = 1
              AND (q.retry_after IS NULL OR datetime(q.retry_after) <= datetime('now'))
              AND (
                  q.claim_token IS NULL
                  OR q.claim_until IS NULL
                  OR datetime(q.claim_until) <= datetime('now')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = q.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
            ORDER BY q.queued_at, q.query, q.post_id
            LIMIT ?
        """, (user_id, bounded_limit))
            selected = await cursor.fetchall()
            if selected:
                await db.executemany("""
                UPDATE subscription_digest_queue
                SET claim_token = ?,
                    claimed_at = CURRENT_TIMESTAMP,
                    claim_until = datetime('now', '+' || ? || ' minutes'),
                    delivery_state = 'pending',
                    retry_after = NULL
                WHERE user_id = ? AND query = ? AND post_id = ?
                  AND (
                      claim_token IS NULL
                      OR claim_until IS NULL
                      OR datetime(claim_until) <= datetime('now')
                  )
            """, [
                    (token, bounded_lease, user_id, row[0], row[1])
                    for row in selected
                ])
            cursor = await db.execute("""
                SELECT query, post_id, post_json
                FROM subscription_digest_queue q
                WHERE q.user_id = ? AND q.claim_token = ?
                  AND EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = q.user_id AND s.query = q.query
                        AND s.is_active = 1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM user_settings us
                      WHERE us.user_id = q.user_id
                        AND datetime(json_extract(
                            CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                                 THEN us.settings_json ELSE '{}' END,
                            '$.subscription_pause_until'
                        )) > datetime('now')
                  )
                ORDER BY q.queued_at, q.query, q.post_id
            """, (user_id, token))
            rows = await cursor.fetchall()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

        result = []
        invalid_keys = []
        for query, post_id, raw_post in rows:
            try:
                post = runtime.json.loads(raw_post)
                post["subscription_query"] = query
                post["digest_item_key"] = (query, int(post_id))
                result.append(post)
            except runtime.json.JSONDecodeError:
                invalid_keys.append((query, int(post_id)))
        if invalid_keys:
            async with runtime.connect_db() as cleanup_db:
                for query, post_id in invalid_keys:
                    await cleanup_db.execute("""
                        DELETE FROM subscription_digest_queue
                        WHERE user_id = ? AND query = ? AND post_id = ?
                          AND claim_token = ?
                    """, (user_id, query, post_id, token))
                await cleanup_db.commit()
        return (token if result else None), result


async def release_subscription_digest_claim(runtime, user_id, claim_token):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscription_digest_queue
            SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
            WHERE user_id = ? AND claim_token = ?
        """, (user_id, claim_token))
        await db.commit()
        return cursor.rowcount


async def renew_subscription_digest_claim(runtime, user_id, claim_token, lease_minutes):
    """Extend a live digest lease; false means the batch is no longer owned."""
    bounded_lease = max(1, int(lease_minutes))
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE subscription_digest_queue
            SET claim_until = datetime('now', '+' || ? || ' minutes')
            WHERE user_id = ? AND claim_token = ?
              AND claim_until IS NOT NULL
              AND datetime(claim_until) > datetime('now')
              AND EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = subscription_digest_queue.user_id
                    AND s.query = subscription_digest_queue.query
                    AND s.is_active = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = subscription_digest_queue.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (bounded_lease, user_id, claim_token))
        await db.commit()
        return cursor.rowcount > 0


async def get_subscription_digest_claim_keys(runtime, user_id, claim_token):
    """Return only still-owned items whose source subscription still exists."""
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT q.query, q.post_id
            FROM subscription_digest_queue q
            WHERE q.user_id = ? AND q.claim_token = ?
              AND q.claim_until IS NOT NULL
              AND datetime(q.claim_until) > datetime('now')
              AND EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = q.user_id AND s.query = q.query
                    AND s.is_active = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_settings us
                  WHERE us.user_id = q.user_id
                    AND datetime(json_extract(
                        CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                             THEN us.settings_json ELSE '{}' END,
                        '$.subscription_pause_until'
                    )) > datetime('now')
              )
        """, (user_id, claim_token))
        rows = await cursor.fetchall()
        return {(str(query), int(post_id)) for query, post_id in rows}


async def finish_subscription_digest_claim(runtime, user_id, claim_token, delivered_keys, ambiguous_keys, ambiguous_backoff_seconds):
    """Delete confirmed items, defer ambiguous items, and release the remainder."""
    normalized_keys = []
    for query, post_id in delivered_keys:
        try:
            normalized_keys.append((str(query), int(post_id)))
        except (TypeError, ValueError):
            continue
    normalized_ambiguous_keys = []
    for query, post_id in ambiguous_keys:
        try:
            normalized_ambiguous_keys.append((str(query), int(post_id)))
        except (TypeError, ValueError):
            continue
    bounded_backoff = max(1, min(int(ambiguous_backoff_seconds), 86400))

    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            delivered = 0
            for query, post_id in normalized_keys:
                cursor = await db.execute("""
                    DELETE FROM subscription_digest_queue
                    WHERE user_id = ? AND query = ? AND post_id = ?
                      AND claim_token = ?
                """, (user_id, query, post_id, claim_token))
                delivered += max(0, cursor.rowcount)
            for query, post_id in normalized_ambiguous_keys:
                await db.execute("""
                    UPDATE subscription_digest_queue
                    SET delivery_state = 'ambiguous',
                        retry_after = datetime('now', '+' || ? || ' seconds'),
                        claim_token = NULL,
                        claimed_at = NULL,
                        claim_until = NULL
                    WHERE user_id = ? AND query = ? AND post_id = ?
                      AND claim_token = ?
                """, (bounded_backoff, user_id, query, post_id, claim_token))
            cursor = await db.execute("""
                UPDATE subscription_digest_queue
                SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
                WHERE user_id = ? AND claim_token = ?
            """, (user_id, claim_token))
            released = max(0, cursor.rowcount)
            await db.commit()
            return delivered, released
        except BaseException:
            await db.rollback()
            raise


async def get_due_digest_users(runtime):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
                SELECT q.user_id FROM subscription_digest_queue q
                WHERE EXISTS (
                    SELECT 1 FROM subscriptions s
                    WHERE s.user_id = q.user_id
                      AND s.query = q.query
                      AND s.is_active = 1
                )
                  AND (q.retry_after IS NULL OR datetime(q.retry_after) <= datetime('now'))
                  AND (
                    q.claim_token IS NULL
                    OR q.claim_until IS NULL
                    OR datetime(q.claim_until) <= datetime('now')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM user_settings us
                    WHERE us.user_id = q.user_id
                      AND datetime(json_extract(
                          CASE WHEN json_valid(COALESCE(us.settings_json, '{}'))
                               THEN us.settings_json ELSE '{}' END,
                          '$.subscription_pause_until'
                      )) > datetime('now')
                  )
                GROUP BY q.user_id
                HAVING COUNT(*) >= 5
                   OR datetime(MIN(q.queued_at)) <= datetime('now', '-6 hours')
                ORDER BY MIN(q.queued_at), q.user_id
                LIMIT 20
        """)
        return [int(row[0]) for row in await cursor.fetchall()]
