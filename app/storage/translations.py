"""Domain implementation. Dependencies are supplied by the public facade."""

def _normalize_translation_tags(runtime, tags):
    if isinstance(tags, str):
        tags = tags.split()
    return sorted({
        str(tag).strip().lower()
        for tag in tags
        if tag is not None and str(tag).strip()
    })


async def queue_tag_translations(runtime, tags, source):
    normalized = runtime._normalize_translation_tags(tags)
    if not normalized:
        return 0
    async with runtime.connect_db() as db:
        before = db.total_changes
        await db.executemany("""
            INSERT OR IGNORE INTO tag_translations (tag, source)
            VALUES (?, ?)
        """, [(tag, source[:30]) for tag in normalized])
        await db.commit()
        return db.total_changes - before


async def get_tag_translations(runtime, tags):
    normalized = runtime._normalize_translation_tags(tags)
    if not normalized:
        return {}
    result: runtime.Dict[str, str] = {}
    async with runtime.connect_db() as db:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await db.execute(f"""
                SELECT tag, translation_ru
                FROM tag_translations
                WHERE tag IN ({placeholders})
                  AND status = 'ready'
                  AND translation_ru <> ''
            """, tuple(chunk))
            result.update(await cursor.fetchall())
    return result


async def get_tag_translation_states(runtime, tags):
    normalized = runtime._normalize_translation_tags(tags)
    if not normalized:
        return {}
    result: runtime.Dict[str, str] = {}
    async with runtime.connect_db() as db:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await db.execute(f"""
                SELECT tag, status
                FROM tag_translations
                WHERE tag IN ({placeholders})
                  AND (
                    status IN ('ready', 'unchanged')
                    OR (status = 'failed' AND next_retry_at > CURRENT_TIMESTAMP)
                  )
            """, tuple(chunk))
            result.update(await cursor.fetchall())
    return result


async def get_pending_tag_translations(runtime, limit):
    limit = max(1, min(int(limit), 100))
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT tag FROM tag_translations
            WHERE status = 'pending'
               OR (status = 'failed' AND (
                    next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP
               ))
            ORDER BY CASE source
                        WHEN 'blacklist' THEN 0
                        WHEN 'display' THEN 1
                        ELSE 2
                     END,
                     CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                     updated_at ASC, tag ASC
            LIMIT ?
        """, (limit,))
        return [row[0] for row in await cursor.fetchall()]


async def save_tag_translations_bulk(runtime, translations, source):
    rows = []
    for tag, translation in translations.items():
        normalized_tag = str(tag).strip().lower()
        normalized_translation = " ".join(str(translation).split()).strip()
        if normalized_tag:
            rows.append((
                normalized_tag,
                normalized_translation,
                "ready" if normalized_translation else "unchanged",
                source[:30],
            ))
    if not rows:
        return
    async with runtime.connect_db() as db:
        await db.executemany("""
            INSERT INTO tag_translations
                (tag, translation_ru, status, source, attempts, next_retry_at, updated_at)
            VALUES (?, ?, ?, ?, 1, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(tag) DO UPDATE SET
                translation_ru = excluded.translation_ru,
                status = excluded.status,
                source = excluded.source,
                attempts = tag_translations.attempts + 1,
                next_retry_at = NULL,
                updated_at = CURRENT_TIMESTAMP
        """, rows)
        await db.commit()


async def mark_tag_translations_failed(runtime, tags):
    normalized = runtime._normalize_translation_tags(tags)
    if not normalized:
        return
    async with runtime.connect_db() as db:
        await db.executemany("""
            INSERT INTO tag_translations
                (tag, status, source, attempts, next_retry_at, updated_at)
            VALUES (?, 'failed', 'network', 1, datetime('now', '+6 hours'), CURRENT_TIMESTAMP)
            ON CONFLICT(tag) DO UPDATE SET
                status = 'failed',
                source = 'network',
                attempts = tag_translations.attempts + 1,
                next_retry_at = datetime(
                    'now', '+' || MIN(24, 6 * (tag_translations.attempts + 1)) || ' hours'
                ),
                updated_at = CURRENT_TIMESTAMP
        """, [(tag,) for tag in normalized])
        await db.commit()


async def seed_tag_translation_queue(runtime):
    """Collect known tags without delaying database initialization."""
    inserted = 0
    async with runtime.connect_db() as db:
        cursor = await db.execute("SELECT DISTINCT tag FROM blacklist WHERE tag <> ''")
        blacklist_tags = [row[0] for row in await cursor.fetchall()]
    inserted += await runtime.queue_tag_translations(blacklist_tags, source="blacklist")

    for table in ("post_cache", "subscription_cache", "subscription_posts", "favorites"):
        async with runtime.connect_db() as db:
            cursor = await db.execute(
                f"SELECT tags FROM {table} WHERE COALESCE(tags, '') <> ''"
            )
            while True:
                rows = await cursor.fetchmany(250)
                if not rows:
                    break
                batch = {
                    tag
                    for row in rows
                    for tag in str(row[0] or "").split()
                    if tag
                }
                inserted += await runtime.queue_tag_translations(batch, source=table)
    return inserted
