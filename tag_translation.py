import asyncio
import html
import logging
import re
import time
from typing import Iterable

import aiohttp

from config import TAG_TRANSLATION_ENABLED
from database import (
    get_pending_tag_translations,
    get_tag_translation_states,
    get_tag_translations,
    mark_tag_translations_failed,
    queue_tag_translations,
    save_tag_translations_bulk,
    seed_tag_translation_queue,
)

logger = logging.getLogger(__name__)

TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATION_BATCH_SIZE = 12
TRANSLATION_IDLE_SECONDS = 30 * 60
TRANSLATION_BATCH_PAUSE_SECONDS = 3
TRANSLATION_REQUEST_INTERVAL_SECONDS = 0.25
ON_DEMAND_TRANSLATION_LIMIT = 30

EXACT_TRANSLATIONS = {
    "1girl": "одна девушка",
    "1boy": "один парень",
    "2girls": "две девушки",
    "2boys": "два парня",
    "solo": "соло",
    "female": "женщина",
    "male": "мужчина",
    "multiple_girls": "несколько девушек",
    "multiple_boys": "несколько парней",
    "blue_hair": "голубые волосы",
    "black_hair": "чёрные волосы",
    "blonde_hair": "светлые волосы",
    "brown_hair": "каштановые волосы",
    "red_hair": "рыжие волосы",
    "white_hair": "белые волосы",
    "long_hair": "длинные волосы",
    "short_hair": "короткие волосы",
    "blue_eyes": "голубые глаза",
    "green_eyes": "зелёные глаза",
    "brown_eyes": "карие глаза",
    "red_eyes": "красные глаза",
    "looking_at_viewer": "смотрит на зрителя",
    "smile": "улыбка",
    "animated": "анимация",
    "gif": "GIF-анимация",
    "webm": "видео WebM",
    "gore": "жестокий контент",
    "blood": "кровь",
}


def normalize_tags(tags: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(
        str(tag).strip().lower()
        for tag in tags
        if str(tag).strip()
    ))


class TagTranslationService:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.request_lock = asyncio.Lock()
        self.translation_lock = asyncio.Lock()
        self.last_request_at = 0.0

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _wait_for_request_slot(self):
        async with self.request_lock:
            delay = TRANSLATION_REQUEST_INTERVAL_SECONDS - (
                time.monotonic() - self.last_request_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request_at = time.monotonic()

    async def _translate_one(self, tag: str) -> str | None:
        if tag in EXACT_TRANSLATIONS:
            return EXACT_TRANSLATIONS[tag]
        if len(tag) > 120 or not re.search(r"[a-z]", tag):
            return ""
        await self.ensure_session()
        await self._wait_for_request_slot()
        try:
            async with self.session.get(
                TRANSLATE_URL,
                params={
                    "client": "gtx",
                    "sl": "en",
                    "tl": "ru",
                    "dt": "t",
                    "q": html.unescape(tag).replace("_", " "),
                },
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            return None

        try:
            translated = "".join(
                str(part[0] or "") for part in data[0] if part and part[0]
            ).strip()
        except (IndexError, TypeError):
            return None
        source_text = html.unescape(tag).replace("_", " ").strip().casefold()
        if not translated or translated.casefold() == source_text:
            return ""
        return translated

    async def translate_tags(
        self, tags: Iterable[str], immediate_limit: int = ON_DEMAND_TRANSLATION_LIMIT
    ) -> dict[str, str]:
        normalized = normalize_tags(tags)
        if not normalized:
            return {}
        if not TAG_TRANSLATION_ENABLED:
            return await get_tag_translations(normalized)
        await queue_tag_translations(normalized, source="display")
        cached = await get_tag_translations(normalized)
        known = await get_tag_translation_states(normalized)
        missing = [tag for tag in normalized if tag not in known][:max(0, immediate_limit)]
        if not missing:
            return cached

        async with self.translation_lock:
            cached = await get_tag_translations(normalized)
            known = await get_tag_translation_states(normalized)
            missing = [tag for tag in normalized if tag not in known][:max(0, immediate_limit)]
            if not missing:
                return cached
            results = await asyncio.gather(
                *(self._translate_one(tag) for tag in missing),
                return_exceptions=True,
            )
            ready = {}
            failed = []
            for tag, result in zip(missing, results):
                if isinstance(result, Exception) or result is None:
                    failed.append(tag)
                else:
                    ready[tag] = result
            await save_tag_translations_bulk(ready, source="google")
            await mark_tag_translations_failed(failed)
            cached.update({tag: value for tag, value in ready.items() if value})
            return cached

    async def background_worker(self):
        if not TAG_TRANSLATION_ENABLED:
            return
        initialized = False
        while True:
            try:
                if not initialized:
                    inserted = await seed_tag_translation_queue()
                    logger.info("Tag translation queue initialized new_tags=%s", inserted)
                    initialized = True
                pending = await get_pending_tag_translations(TRANSLATION_BATCH_SIZE)
                if not pending:
                    await asyncio.sleep(TRANSLATION_IDLE_SECONDS)
                    inserted = await seed_tag_translation_queue()
                    logger.info("Tag translation queue refreshed new_tags=%s", inserted)
                    continue
                await self.translate_tags(pending, immediate_limit=len(pending))
                await asyncio.sleep(TRANSLATION_BATCH_PAUSE_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Tag translation worker iteration failed")
                await asyncio.sleep(60)


tag_translation_service = TagTranslationService()
