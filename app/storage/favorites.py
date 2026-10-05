"""Domain implementation. Dependencies are supplied by the public facade."""

async def add_favorite(runtime, user_id, post):
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
                INSERT INTO favorites
                (user_id, post_id, file_url, sample_url, preview_url, tags, rating, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                post_id,
                file_url,
                sample_url,
                preview_url,
                tags,
                rating,
                score,
            ))
            await db.execute("""
                INSERT INTO bot_events (user_id, event_type, post_id)
                VALUES (?, 'favorite_added', ?)
            """, (user_id, post_id))
            await db.commit()
            return True
        except runtime.aiosqlite.IntegrityError:
            return False


async def remove_favorite(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        await db.execute(
            "DELETE FROM favorite_collection_items WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        await db.execute(
            "DELETE FROM favorite_notes WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        cursor = await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id)
        )
        await db.commit()
        return cursor.rowcount > 0


def _normalize_collection_name(runtime, name):
    return " ".join(name.strip().split())[:40]


async def create_favorite_collection(runtime, user_id, name):
    name = runtime._normalize_collection_name(name)
    if not name:
        return None
    async with runtime.connect_db() as db:
        try:
            cursor = await db.execute(
                "INSERT INTO favorite_collections (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            await db.commit()
            return int(cursor.lastrowid)
        except runtime.aiosqlite.IntegrityError:
            return None


async def get_favorite_collections(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT c.collection_id, c.name, COUNT(i.post_id), c.created_at
            FROM favorite_collections c
            LEFT JOIN favorite_collection_items i
              ON i.collection_id = c.collection_id AND i.user_id = c.user_id
            WHERE c.user_id = ?
            GROUP BY c.collection_id, c.name, c.created_at
            ORDER BY lower(c.name), c.collection_id
        """, (user_id,))
        return [
            {"id": row[0], "name": row[1], "count": row[2], "created_at": row[3]}
            for row in await cursor.fetchall()
        ]


async def get_favorite_collection(runtime, user_id, collection_id):
    collections = await runtime.get_favorite_collections(user_id)
    return next((item for item in collections if item["id"] == collection_id), None)


async def rename_favorite_collection(runtime, user_id, collection_id, name):
    name = runtime._normalize_collection_name(name)
    if not name:
        return False
    async with runtime.connect_db() as db:
        try:
            cursor = await db.execute(
                "UPDATE favorite_collections SET name = ? WHERE user_id = ? AND collection_id = ?",
                (name, user_id, collection_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except runtime.aiosqlite.IntegrityError:
            return False


async def delete_favorite_collection(runtime, user_id, collection_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM favorite_collections WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def add_favorite_to_collection(runtime, user_id, collection_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM favorite_collections WHERE user_id = ? AND collection_id = ?",
            (user_id, collection_id),
        )
        if not await cursor.fetchone():
            return False
        cursor = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        if not await cursor.fetchone():
            return False
        try:
            await db.execute("""
                INSERT INTO favorite_collection_items (collection_id, user_id, post_id)
                VALUES (?, ?, ?)
            """, (collection_id, user_id, post_id))
            await db.commit()
            return True
        except runtime.aiosqlite.IntegrityError:
            return False


async def remove_favorite_from_collection(runtime, user_id, collection_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM favorite_collection_items
            WHERE user_id = ? AND collection_id = ? AND post_id = ?
        """, (user_id, collection_id, post_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_collection_favorites(runtime, user_id, collection_id, limit, offset):
    params: list[runtime.Any] = [user_id, collection_id]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    async with runtime.connect_db() as db:
        cursor = await db.execute(f"""
            SELECT f.post_id,
                   COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                   COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                   COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                   COALESCE(NULLIF(pc.tags, ''), f.tags),
                   COALESCE(NULLIF(pc.rating, ''), f.rating),
                   COALESCE(pc.score, f.score), i.added_at
            FROM favorite_collection_items i
            JOIN favorites f ON f.user_id = i.user_id AND f.post_id = i.post_id
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE i.user_id = ? AND i.collection_id = ?
            ORDER BY i.added_at DESC, i.post_id DESC
            {limit_clause}
        """, tuple(params))
        posts = []
        for row in await cursor.fetchall():
            post = runtime._post_from_row(row)
            post["added_at"] = row[7]
            posts.append(post)
        return posts


async def count_collection_favorites(runtime, user_id, collection_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorite_collection_items
            WHERE user_id = ? AND collection_id = ?
        """, (user_id, collection_id))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def set_favorite_note(runtime, user_id, post_id, note):
    note = note.strip()[:1000]
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        if not await cursor.fetchone():
            return False
        if note:
            await db.execute("""
                INSERT INTO favorite_notes (user_id, post_id, note, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, post_id) DO UPDATE SET
                    note = excluded.note, updated_at = CURRENT_TIMESTAMP
            """, (user_id, post_id, note))
        else:
            await db.execute(
                "DELETE FROM favorite_notes WHERE user_id = ? AND post_id = ?",
                (user_id, post_id),
            )
        await db.commit()
        return True


async def get_favorite_note(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "SELECT note FROM favorite_notes WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""


async def get_favorites(runtime, user_id, limit, offset, tag_filter):
    tag_filter = tag_filter.strip().lower()
    tag_where = ""
    params: list[runtime.Any] = [user_id]
    if tag_filter:
        tag_where = """
            AND lower(' ' || COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ')
                LIKE ?
        """
        params.append(f"% {tag_filter} %")

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    async with runtime.connect_db() as db:
        cursor = await db.execute(f"""
            SELECT
                f.post_id,
                COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                COALESCE(NULLIF(pc.tags, ''), f.tags),
                COALESCE(NULLIF(pc.rating, ''), f.rating),
                COALESCE(pc.score, f.score),
                f.added_at
            FROM favorites
            f LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE user_id = ?
            {tag_where}
            ORDER BY added_at DESC, f.post_id DESC
            {limit_clause}
        """, tuple(params))
        rows = await cursor.fetchall()
        posts = []
        for row in rows:
            post = runtime._post_from_row(row)
            post["added_at"] = row[7]
            posts.append(post)
        return posts


async def get_favorite_by_index(runtime, user_id, index, tag_filter):
    posts = await runtime.get_favorites(
        user_id,
        limit=1,
        offset=max(0, index),
        tag_filter=tag_filter,
    )
    return posts[0] if posts else None


async def get_favorite(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            SELECT
                f.post_id,
                COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                COALESCE(NULLIF(pc.tags, ''), f.tags),
                COALESCE(NULLIF(pc.rating, ''), f.rating),
                COALESCE(pc.score, f.score),
                f.added_at
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ? AND f.post_id = ?
        """, (user_id, post_id))
        row = await cursor.fetchone()
        if not row:
            return None
        post = runtime._post_from_row(row)
        post["added_at"] = row[7]
        return post


async def count_favorites(runtime, user_id, tag_filter):
    tag_filter = tag_filter.strip().lower()
    tag_where = ""
    params: list[runtime.Any] = [user_id]
    if tag_filter:
        tag_where = """
            AND lower(' ' || COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ')
                LIKE ?
        """
        params.append(f"% {tag_filter} %")

    async with runtime.connect_db() as db:
        cursor = await db.execute(f"""
            SELECT COUNT(*)
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE user_id = ?
            {tag_where}
        """, tuple(params))
        row = await cursor.fetchone()
        return int(row[0] or 0)


async def add_read_later(runtime, user_id, post, retention_days):
    normalized = runtime._normalize_post(post)
    if normalized is None:
        return False
    post_id = normalized[0]
    safe_post = dict(post)
    safe_post["id"] = post_id
    days = max(1, min(int(retention_days), 365))
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO read_later
            (user_id, post_id, post_json, expires_at)
            VALUES (?, ?, ?, datetime('now', '+' || ? || ' days'))
        """, (user_id, post_id, runtime.json.dumps(safe_post), days))
        await db.commit()
        return cursor.rowcount > 0


async def get_read_later(runtime, user_id, limit):
    async with runtime.connect_db() as db:
        await db.execute(
            "DELETE FROM read_later WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')"
        )
        cursor = await db.execute("""
            SELECT post_json FROM read_later WHERE user_id = ?
            ORDER BY added_at DESC LIMIT ?
        """, (user_id, max(1, min(limit, 100))))
        await db.commit()
        result = []
        for (raw_post,) in await cursor.fetchall():
            try:
                result.append(runtime.json.loads(raw_post))
            except runtime.json.JSONDecodeError:
                continue
        return result


async def remove_read_later(runtime, user_id, post_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM read_later WHERE user_id = ? AND post_id = ?",
            (user_id, post_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_favorite_tag_profile(runtime, user_id, limit):
    posts = await runtime.get_favorites(user_id, limit=None)
    counts: runtime.Dict[str, int] = {}
    ignored = {"solo", "1girl", "1boy", "highres", "absurdres", "explicit", "safe"}
    for post in posts:
        for tag in str(post.get("tags") or "").lower().split():
            if len(tag) > 2 and tag not in ignored:
                counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


async def search_favorites(runtime, user_id, query, limit):
    terms = [term.lower() for term in query.split() if term][:8]
    conditions = "".join(
        " AND lower(COALESCE(NULLIF(pc.tags, ''), f.tags) || ' ' || COALESCE(fn.note, '')) LIKE ?"
        for _term in terms
    )
    params: list[runtime.Any] = [user_id, *(f"%{term}%" for term in terms), max(1, min(limit, 100))]
    async with runtime.connect_db() as db:
        cursor = await db.execute(f"""
            SELECT f.post_id,
                   COALESCE(NULLIF(pc.file_url, ''), f.file_url),
                   COALESCE(NULLIF(pc.sample_url, ''), f.sample_url),
                   COALESCE(NULLIF(pc.preview_url, ''), f.preview_url),
                   COALESCE(NULLIF(pc.tags, ''), f.tags),
                   COALESCE(NULLIF(pc.rating, ''), f.rating),
                   COALESCE(pc.score, f.score), f.added_at
            FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            LEFT JOIN favorite_notes fn ON fn.user_id = f.user_id AND fn.post_id = f.post_id
            WHERE f.user_id = ? {conditions}
            ORDER BY f.added_at DESC LIMIT ?
        """, tuple(params))
        result = []
        for row in await cursor.fetchall():
            post = runtime._post_from_row(row)
            post["added_at"] = row[7]
            result.append(post)
        return result


async def get_user_storage_stats(runtime, user_id):
    async with runtime.connect_db() as db:
        counts = {}
        for name, table in (
            ("favorites", "favorites"),
            ("collections", "favorite_collections"),
            ("history", "search_history"),
            ("viewed", "sent_posts"),
            ("read_later", "read_later"),
            ("presets", "search_presets"),
            ("digest", "subscription_digest_queue"),
        ):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,))
            counts[name] = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorite_collections c
            WHERE c.user_id = ? AND NOT EXISTS (
                SELECT 1 FROM favorite_collection_items i
                WHERE i.collection_id = c.collection_id
            )
        """, (user_id,))
        counts["empty_collections"] = int((await cursor.fetchone())[0] or 0)
        cursor = await db.execute("""
            SELECT COUNT(*) FROM favorites f
            LEFT JOIN post_cache pc ON pc.post_id = f.post_id
            WHERE f.user_id = ?
              AND COALESCE(NULLIF(pc.file_url, ''), f.file_url, '') = ''
        """, (user_id,))
        counts["favorites_without_url"] = int((await cursor.fetchone())[0] or 0)
        return counts


async def cleanup_empty_collections(runtime, user_id):
    async with runtime.connect_db() as db:
        cursor = await db.execute("""
            DELETE FROM favorite_collections
            WHERE user_id = ? AND NOT EXISTS (
                SELECT 1 FROM favorite_collection_items i
                WHERE i.collection_id = favorite_collections.collection_id
            )
        """, (user_id,))
        await db.commit()
        return max(0, cursor.rowcount)


async def cleanup_user_storage(runtime, user_id, days):
    days = max(7, min(int(days), 3650))
    async with runtime.connect_db() as db:
        removed = {}
        for name, table, column in (
            ("history", "search_history", "searched_at"),
            ("viewed", "sent_posts", "sent_at"),
            ("events", "bot_events", "created_at"),
        ):
            cursor = await db.execute(f"""
                DELETE FROM {table} WHERE user_id = ?
                AND datetime({column}) < datetime('now', '-' || ? || ' days')
            """, (user_id, days))
            removed[name] = max(0, cursor.rowcount)
        await db.commit()
        return removed
