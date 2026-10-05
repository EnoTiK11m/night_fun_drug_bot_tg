"""Domain implementation. Dependencies are supplied by the public facade."""

async def save_delivery_failure(runtime, user_id, post, caption, error):
    normalized = runtime._normalize_post(post)
    if normalized is None:
        return
    post_id = normalized[0]
    safe_post = {
        "id": post_id,
        "file_url": normalized[1],
        "sample_url": normalized[2],
        "preview_url": normalized[3],
        "tags": normalized[4],
        "rating": normalized[5],
        "score": normalized[6],
    }
    async with runtime.connect_db() as db:
        await db.execute("""
            INSERT INTO delivery_failures
                (user_id, post_id, post_json, caption, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, post_id) DO UPDATE SET
                post_json = excluded.post_json,
                caption = excluded.caption,
                attempts = delivery_failures.attempts + 1,
                last_error = excluded.last_error,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, post_id, runtime.json.dumps(safe_post), caption[:1024], error[:500]))
        await db.commit()


async def get_delivery_failures(runtime, limit):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT failure_id, user_id, post_id, post_json, caption, attempts,
                   last_error, created_at, updated_at
            FROM delivery_failures ORDER BY updated_at ASC LIMIT ?
        """, (max(1, min(limit, 100)),))
        result = []
        for row in await cursor.fetchall():
            try:
                post = runtime.json.loads(row[3])
            except runtime.json.JSONDecodeError:
                post = {"id": row[2]}
            result.append({
                "id": row[0], "user_id": row[1], "post_id": row[2],
                "post": post, "caption": row[4], "attempts": row[5],
                "last_error": row[6], "created_at": row[7], "updated_at": row[8],
            })
        return result


async def claim_delivery_failures(runtime, limit, lease_minutes):
    """Atomically lease delivery failures for one retry worker."""
    token = runtime.uuid.uuid4().hex
    bounded_limit = max(1, min(int(limit), 100))
    bounded_lease = max(1, min(int(lease_minutes), 60))
    async with runtime.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT failure_id FROM delivery_failures
                WHERE is_permanent = 0 AND (claim_token IS NULL OR claim_until IS NULL
                   OR datetime(claim_until) <= datetime('now'))
                ORDER BY updated_at, failure_id
                LIMIT ?
            """, (bounded_limit,))
            failure_ids = [int(row[0]) for row in await cursor.fetchall()]
            if failure_ids:
                await db.executemany("""
                    UPDATE delivery_failures
                    SET claim_token = ?, claimed_at = CURRENT_TIMESTAMP,
                        claim_until = datetime('now', '+' || ? || ' minutes')
                    WHERE failure_id = ?
                      AND (claim_token IS NULL OR claim_until IS NULL
                           OR datetime(claim_until) <= datetime('now'))
                """, [(token, bounded_lease, failure_id) for failure_id in failure_ids])
            cursor = await db.execute("""
                SELECT failure_id, user_id, post_id, post_json, caption, attempts,
                       last_error, created_at, updated_at
                FROM delivery_failures
                WHERE claim_token = ?
                ORDER BY updated_at, failure_id
            """, (token,))
            rows = await cursor.fetchall()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    failures = []
    for row in rows:
        try:
            post = runtime.json.loads(row[3])
        except runtime.json.JSONDecodeError:
            post = {"id": row[2]}
        failures.append({
            "id": row[0], "user_id": row[1], "post_id": row[2],
            "post": post, "caption": row[4], "attempts": row[5],
            "last_error": row[6], "created_at": row[7], "updated_at": row[8],
        })
    return (token if failures else None), failures


async def release_delivery_failure_claim(runtime, claim_token):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE delivery_failures
            SET claim_token = NULL, claimed_at = NULL, claim_until = NULL
            WHERE claim_token = ?
        """, (claim_token,))
        await db.commit()
        return max(0, cursor.rowcount)


async def renew_delivery_failure_claim_for_post(runtime, user_id, post_id, claim_token, lease_minutes):
    """Extend a still-owned item lease immediately before external delivery."""
    bounded_lease = max(1, min(int(lease_minutes), 60))
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            UPDATE delivery_failures
            SET claimed_at = CURRENT_TIMESTAMP,
                claim_until = datetime('now', '+' || ? || ' minutes')
            WHERE user_id = ? AND post_id = ? AND claim_token = ?
              AND claim_until IS NOT NULL
              AND datetime(claim_until) > datetime('now')
        """, (bounded_lease, user_id, post_id, claim_token))
        await db.commit()
        return cursor.rowcount == 1


async def delete_delivery_failure_for_post(runtime, user_id, post_id, claim_token):
    """Acknowledge confirmed delivery only when the retry lease is still owned."""
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM delivery_failures
            WHERE user_id = ? AND post_id = ? AND claim_token = ?
              AND claim_until IS NOT NULL
              AND datetime(claim_until) > datetime('now')
        """, (user_id, post_id, claim_token))
        await db.commit()
        return cursor.rowcount == 1


async def clear_delivery_failure_for_post(runtime, user_id, post_id):
    """Remove stale failure bookkeeping after any independently confirmed send."""
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM delivery_failures WHERE user_id = ? AND post_id = ?
        """, (user_id, post_id))
        await db.commit()
        return cursor.rowcount == 1


async def delete_delivery_failure(runtime, failure_id):
    async with runtime.connect_db() as db:
        await db.execute("DELETE FROM delivery_failures WHERE failure_id = ?", (failure_id,))
        await db.commit()


async def mark_delivery_failure_permanent(runtime, user_id, post_id, token, error):
    async with runtime.connect_db() as db:
        await db.execute('UPDATE delivery_failures SET is_permanent=1,last_error=? WHERE user_id=? AND post_id=? AND claim_token=?', (error[:500], user_id, post_id, token))
        await db.commit()
