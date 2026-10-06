"""Domain implementation. Dependencies are supplied by the public facade."""
from app.observability.db_diagnostics import db_operation, query as diagnostic_query, metadata

def _deleted_row_count(runtime, cursor):
    return max(0, int(cursor.rowcount or 0))


async def _cleanup_expired_caches_in_connection(runtime, db, *, subscription_ttl_minutes, subscription_max_per_query, subscription_max_rows, post_ttl_hours, post_max_rows, batch_size):
    started_at = runtime.time.perf_counter()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_cache")
        subscription_initial = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("SELECT COUNT(*) FROM post_cache")
        post_initial = int((await cursor.fetchone())[0] or 0)

        subscription_deleted = 0
        cursor = await db.execute("""
            DELETE FROM subscription_cache
            WHERE rowid IN (
                SELECT sc.rowid
                FROM subscription_cache sc
                WHERE datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00'))
                      < datetime('now', '-' || ? || ' minutes')
                  AND NOT EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = sc.user_id
                        AND s.query = sc.query
                        AND s.processing_token IS NOT NULL
                        AND s.processing_until IS NOT NULL
                        AND datetime(s.processing_until) > datetime('now')
                  )
                ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')), sc.rowid
                LIMIT ?
            )
        """, (max(1, int(subscription_ttl_minutes)), batch_size))
        subscription_deleted += runtime._deleted_row_count(cursor)

        subscription_budget = max(0, batch_size - subscription_deleted)
        if subscription_budget:
            cursor = await db.execute("""
                DELETE FROM subscription_cache
                WHERE rowid IN (
                    SELECT candidate_rowid
                    FROM (
                        SELECT
                            sc.rowid AS candidate_rowid,
                            sc.cached_at AS candidate_cached_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY sc.user_id, sc.query
                                ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')) DESC,
                                         sc.rowid DESC
                            ) AS position
                        FROM subscription_cache sc
                        WHERE NOT EXISTS (
                            SELECT 1 FROM subscriptions s
                            WHERE s.user_id = sc.user_id
                              AND s.query = sc.query
                              AND s.processing_token IS NOT NULL
                              AND s.processing_until IS NOT NULL
                              AND datetime(s.processing_until) > datetime('now')
                        )
                    ) ranked
                    WHERE position > ?
                    ORDER BY datetime(COALESCE(candidate_cached_at, '1970-01-01 00:00:00')),
                             candidate_rowid
                    LIMIT ?
                )
            """, (subscription_max_per_query, subscription_budget))
            subscription_deleted += runtime._deleted_row_count(cursor)

        subscription_budget = max(0, batch_size - subscription_deleted)
        subscription_overflow = max(
            0, subscription_initial - subscription_deleted - subscription_max_rows
        )
        if subscription_budget and subscription_overflow:
            cursor = await db.execute("""
                DELETE FROM subscription_cache
                WHERE rowid IN (
                    SELECT sc.rowid
                    FROM subscription_cache sc
                    WHERE NOT EXISTS (
                        SELECT 1 FROM subscriptions s
                        WHERE s.user_id = sc.user_id
                          AND s.query = sc.query
                          AND s.processing_token IS NOT NULL
                          AND s.processing_until IS NOT NULL
                          AND datetime(s.processing_until) > datetime('now')
                    )
                    ORDER BY datetime(COALESCE(sc.cached_at, '1970-01-01 00:00:00')), sc.rowid
                    LIMIT ?
                )
            """, (min(subscription_budget, subscription_overflow),))
            subscription_deleted += runtime._deleted_row_count(cursor)

        post_deleted = 0
        cursor = await db.execute("""
            DELETE FROM post_cache
            WHERE rowid IN (
                SELECT rowid FROM post_cache
                WHERE datetime(COALESCE(cached_at, '1970-01-01 00:00:00'))
                      < datetime('now', '-' || ? || ' hours')
                ORDER BY datetime(COALESCE(cached_at, '1970-01-01 00:00:00')), rowid
                LIMIT ?
            )
        """, (max(1, int(post_ttl_hours)), batch_size))
        post_deleted += runtime._deleted_row_count(cursor)

        post_budget = max(0, batch_size - post_deleted)
        post_overflow = max(0, post_initial - post_deleted - post_max_rows)
        if post_budget and post_overflow:
            cursor = await db.execute("""
                DELETE FROM post_cache
                WHERE rowid IN (
                    SELECT rowid FROM post_cache
                    ORDER BY datetime(COALESCE(cached_at, '1970-01-01 00:00:00')), rowid
                    LIMIT ?
                )
            """, (min(post_budget, post_overflow),))
            post_deleted += runtime._deleted_row_count(cursor)

        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    return runtime.CacheCleanupResult(
        subscription_cache_deleted=subscription_deleted,
        post_cache_deleted=post_deleted,
        subscription_cache_remaining=max(0, subscription_initial - subscription_deleted),
        post_cache_remaining=max(0, post_initial - post_deleted),
        elapsed_ms=(runtime.time.perf_counter() - started_at) * 1000,
    )


@db_operation("cache.cleanup")
async def cleanup_expired_caches(runtime, *, subscription_ttl_minutes, subscription_max_per_query, subscription_max_rows, post_ttl_hours, post_max_rows, batch_size, db):
    """Delete a bounded cache batch in one short transaction."""
    normalized_batch = max(1, int(batch_size))
    options = {
        "subscription_ttl_minutes": max(1, int(subscription_ttl_minutes)),
        "subscription_max_per_query": max(1, int(subscription_max_per_query)),
        "subscription_max_rows": max(1, int(subscription_max_rows)),
        "post_ttl_hours": max(1, int(post_ttl_hours)),
        "post_max_rows": max(1, int(post_max_rows)),
        "batch_size": normalized_batch,
    }
    if db is not None:
        return await runtime._cleanup_expired_caches_in_connection(db, **options)
    async with runtime.connect_db() as owned_db:
        return await runtime._cleanup_expired_caches_in_connection(owned_db, **options)


async def get_cache_storage_stats(runtime):
    async with runtime.connect_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM subscription_cache")
        subscription_rows = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("SELECT COUNT(*) FROM post_cache")
        post_rows = int((await cursor.fetchone())[0] or 0)
    return {
        "subscription_cache_rows": subscription_rows,
        "post_cache_rows": post_rows,
    }


@db_operation("cache.post")
async def cache_post(runtime, post):
    normalized = runtime._normalize_post(post)
    if normalized is None:
        return False

    async with runtime.connect_db() as db:
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
        await db.commit()
        return True


async def get_cached_post(runtime, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, sample_url, preview_url, tags, rating, score
            FROM post_cache
            WHERE post_id = ?
        """, (post_id,))
        row = await cursor.fetchone()
        return runtime._post_from_row(row) if row else None


@db_operation("subscription.cache.lookup")
async def get_subscription_cache(runtime, user_id, query):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT post_id, file_url, sample_url, preview_url, tags, rating, score,
                   width, height
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
            ORDER BY cached_at DESC, post_id DESC LIMIT ?
        """, (user_id, query.strip(), min(250, runtime.SUBSCRIPTION_CACHE_MAX_PER_QUERY)))
        posts = [runtime._subscription_post_from_row(row) for row in await cursor.fetchall()]

        cursor = await db.execute("""
            SELECT MIN(cached_at)
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query.strip()))
        row = await cursor.fetchone()
        return posts, row[0] if row and row[0] else None


async def is_subscription_cache_stale(runtime, user_id, query):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT 1
            FROM subscription_cache
            WHERE user_id = ?
              AND query = ?
              AND datetime(cached_at) >= datetime('now', '-' || ? || ' minutes')
            LIMIT 1
        """, (user_id, query.strip(), runtime.SUBSCRIPTION_CACHE_TTL_MINUTES))
        return await cursor.fetchone() is None


@db_operation("subscription.cache.replace")
async def replace_subscription_cache(runtime, user_id, query, posts):
    query = query.strip()
    seen_post_ids: set[int] = set()
    rows = []
    posts_count = 0
    for post in posts:
        posts_count += 1
        try:
            post_id = int(post.get("id"))
        except (TypeError, ValueError):
            continue

        file_url = post.get("file_url")
        if not file_url or post_id in seen_post_ids:
            continue

        seen_post_ids.add(post_id)
        rows.append((
            user_id,
            query,
            post_id,
            file_url,
            post.get("sample_url", "") or "",
            post.get("preview_url", "") or "",
            post.get("tags", "") or "",
            post.get("rating", "") or "",
            int(post.get("score") or 0),
            int(post.get("width") or 0),
            int(post.get("height") or 0),
        ))

    async with runtime.connect_db() as db:
        metadata(db, posts_count=posts_count, batch_count=len(rows))
        existing_ids: set[int] = set()
        if rows:
            cursor = await diagnostic_query(db, "cache.exists").execute("""
                SELECT post_id
                FROM subscription_cache
                WHERE user_id = ? AND query = ?
                  AND post_id IN (SELECT value FROM json_each(?))
            """, (user_id, query, runtime.json.dumps([row[2] for row in rows])))
            existing_ids = {int(row[0]) for row in await cursor.fetchall()}

        if rows:
            await diagnostic_query(db, "cache.subscription.upsert.batch").executemany("""
                INSERT OR REPLACE INTO subscription_cache
                (user_id, query, post_id, file_url, sample_url, preview_url, tags, rating, score,
                 width, height)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            await diagnostic_query(db, "cache.post.upsert.batch").executemany("""
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
            """, [
                (post_id, file_url, sample_url, preview_url, tags, rating, score)
                for (
                    _user_id,
                    _query,
                    post_id,
                    file_url,
                    sample_url,
                    preview_url,
                    tags,
                    rating,
                    score,
                    _width,
                    _height,
                ) in rows
            ])
        cursor = await diagnostic_query(db, "cache.count").execute("""
            SELECT COUNT(*)
            FROM subscription_cache
            WHERE user_id = ? AND query = ?
        """, (user_id, query))
        row = await cursor.fetchone()
        await db.commit()
        return {
            "api": len(rows),
            "new": sum(1 for row in rows if row[2] not in existing_ids),
            "total": int(row[0] or 0),
        }
