import aiohttp
import asyncio
import random
import json
import logging
from typing import Optional, Set, List, Dict
from config import (
    API_BASE_URL,
    AUTOCOMPLETE_URL,
    API_USER_ID,
    API_KEY,
    DEFAULT_BLACKLIST,
    DEFAULT_LIMIT,
    MAX_POSTS_PER_REQUEST
)

logger = logging.getLogger(__name__)
API_SEARCH_RETRIES = 2
API_SEARCH_TIMEOUT_SECONDS = 45
INTERACTIVE_PAGE_LIMIT = 250
INTERACTIVE_SEARCH_TIMEOUT_SECONDS = 35
INTERACTIVE_MAX_PAGES_PER_SEARCH = 5
SUBSCRIPTION_CACHE_SEARCH_TIMEOUT_SECONDS = 120
INTERACTIVE_REQUEST_CONCURRENCY = 2
BACKGROUND_REQUEST_CONCURRENCY = 1
INTERACTIVE_MIN_REQUEST_INTERVAL_SECONDS = 0.5
BACKGROUND_MIN_REQUEST_INTERVAL_SECONDS = 1.5


class APITemporaryError(Exception):
    """Raised when the upstream API failed but the query should be retried later."""


class rule34API:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.interactive_semaphore = asyncio.Semaphore(INTERACTIVE_REQUEST_CONCURRENCY)
        self.background_semaphore = asyncio.Semaphore(BACKGROUND_REQUEST_CONCURRENCY)
        self.interactive_rate_limit_lock = asyncio.Lock()
        self.background_rate_limit_lock = asyncio.Lock()
        self.last_interactive_request_at = 0.0
        self.last_background_request_at = 0.0
        self.user_search_states = {}  # Храним состояние поиска для каждого пользователя

    async def _wait_for_rate_limit(self, kind: str):
        if kind == "background":
            lock = self.background_rate_limit_lock
            interval = BACKGROUND_MIN_REQUEST_INTERVAL_SECONDS
            attr = "last_background_request_at"
        else:
            lock = self.interactive_rate_limit_lock
            interval = INTERACTIVE_MIN_REQUEST_INTERVAL_SECONDS
            attr = "last_interactive_request_at"

        async with lock:
            now = asyncio.get_running_loop().time()
            delay = interval - (now - getattr(self, attr))
            if delay > 0:
                await asyncio.sleep(delay)
            setattr(self, attr, asyncio.get_running_loop().time())

    async def ensure_session(self):
        """Создать сессию если её нет"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        """Закрыть сессию"""
        if self.session and not self.session.closed:
            await self.session.close()

    def _build_params(self, **kwargs) -> Dict:
        """Построить параметры запроса с учётом API ключа"""
        params = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": 1,
        }

        # Добавляем API ключ - ОБЯЗАТЕЛЬНО!
        params["user_id"] = API_USER_ID
        params["api_key"] = API_KEY

        # Добавляем остальные параметры
        params.update(kwargs)
        return params

    def _build_search_tags(self, tags: str, blacklist: Set[str]) -> str:
        """Построить строку тегов с учётом blacklist"""
        if not tags.strip():
            return ""

        # Комбинируем пользовательский blacklist с дефолтным
        full_blacklist = blacklist | DEFAULT_BLACKLIST

        # Формируем теги с исключениями (минус перед тегом)
        search_tags = tags.strip()
        for blocked_tag in full_blacklist:
            if blocked_tag.strip():  # Проверяем что тег не пустой
                search_tags += f" -{blocked_tag}"

        return search_tags.strip()

    @staticmethod
    def _post_id(post: Dict) -> Optional[int]:
        try:
            return int(post.get("id"))
        except (TypeError, ValueError):
            return None

    async def save_search_state(
        self,
        user_id: int,
        tags: str,
        blacklist: Set[str],
        current_post_id: Optional[int] = None
    ):
        """Сохранить состояние поиска для кнопки 'ещё'"""
        self.user_search_states[user_id] = {
            'tags': tags,
            'blacklist': blacklist,
            'current_pid': 0,
            'used_posts': {current_post_id} if current_post_id is not None else set()
        }

    async def search(
        self,
        tags: str,
        blacklist: Set[str],
        limit: int = DEFAULT_LIMIT,
        pid: int = 0,
        timeout: int = API_SEARCH_TIMEOUT_SECONDS,
        request_kind: str = "interactive"
    ) -> Optional[List[Dict]]:
        """
        Поиск постов
        """
        await self.ensure_session()

        # Ограничиваем лимит
        limit = min(limit, MAX_POSTS_PER_REQUEST)

        search_tags = self._build_search_tags(tags, blacklist)

        # Если после blacklist не осталось тегов для поиска
        if not search_tags or search_tags.startswith('-'):
            return None

        params = self._build_params(
            tags=search_tags,
            limit=limit,
            pid=pid
        )

        logger.debug("Searching with tags %r, page %s", search_tags, pid)
        logger.debug("API URL: %s", API_BASE_URL)

        for attempt in range(1, API_SEARCH_RETRIES + 2):
            try:
                semaphore = (
                    self.background_semaphore
                    if request_kind == "background"
                    else self.interactive_semaphore
                )
                async with semaphore:
                    await self._wait_for_rate_limit(request_kind)
                    async with self.session.get(
                        API_BASE_URL,
                        params=params,
                        timeout=timeout
                    ) as response:
                        logger.debug("Response status: %s", response.status)
                        if response.status != 200:
                            raise APITemporaryError(f"Rule34 API HTTP {response.status}")
                        response_text = await response.text()

                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    raise APITemporaryError("Rule34 API returned invalid JSON") from e

                logger.debug(
                    "Found %s posts on page %s",
                    len(data) if isinstance(data, list) else 0,
                    pid
                )

                if isinstance(data, list):
                    return data

                if isinstance(data, dict) and data.get("success") is False:
                    error_msg = data.get("message", "Unknown error")
                    logger.warning("API Error: %s", error_msg)
                    return None

                return None

            except (asyncio.TimeoutError, aiohttp.ClientError, APITemporaryError) as e:
                if attempt <= API_SEARCH_RETRIES:
                    logger.warning(
                        "Rule34 API temporary error on page %s, retry %s/%s: %s",
                        pid,
                        attempt,
                        API_SEARCH_RETRIES,
                        e,
                    )
                    await asyncio.sleep(attempt)
                    continue

                logger.warning("Rule34 API temporary error on page %s, giving up: %s", pid, e)
                raise APITemporaryError("Rule34 API request failed") from e

    async def get_random_image(
        self,
        tags: str,
        blacklist: Set[str],
        excluded_post_ids: Optional[Set[int]] = None
    ) -> Optional[Dict]:
        """Получить случайное изображение (для нового поиска)"""
        logger.debug("get_random_image called with tags %r", tags)
        excluded_post_ids = excluded_post_ids or set()

        pid = 0
        scanned_pages = 0
        while scanned_pages < INTERACTIVE_MAX_PAGES_PER_SEARCH:
            posts = await self.search(
                tags=tags,
                blacklist=blacklist,
                limit=INTERACTIVE_PAGE_LIMIT,
                pid=pid,
                timeout=INTERACTIVE_SEARCH_TIMEOUT_SECONDS,
                request_kind="interactive"
            )

            if not posts:
                logger.debug("No posts found on page %s, stopping search", pid)
                return None

            logger.debug("Found %s posts on page %s", len(posts), pid)
            valid_posts = [
                post for post in posts
                if post.get("file_url") and self._post_id(post) not in excluded_post_ids
            ]
            if valid_posts:
                selected_post = random.choice(valid_posts)
                logger.debug("Selected post ID: %s", selected_post.get("id"))
                return selected_post

            logger.debug("All valid posts on page %s were already shown", pid)
            pid += 1
            scanned_pages += 1

        logger.debug("Interactive search stopped after %s pages", scanned_pages)
        return None

    async def get_next_image(
        self,
        user_id: int,
        tags: str,
        blacklist: Set[str],
        excluded_post_ids: Optional[Set[int]] = None
    ) -> Optional[Dict]:
        """Получить следующее изображение (для кнопки 'ещё')"""
        logger.debug("get_next_image for user %s with tags %r", user_id, tags)
        excluded_post_ids = excluded_post_ids or set()

        search_state = self.user_search_states.get(user_id)
        if not search_state or search_state['tags'] != tags:
            search_state = {
                'tags': tags,
                'blacklist': blacklist,
                'current_pid': 0,
                'used_posts': set()
            }
            self.user_search_states[user_id] = search_state

        scanned_pages = 0
        while scanned_pages < INTERACTIVE_MAX_PAGES_PER_SEARCH:
            posts = await self.search(
                tags=tags,
                blacklist=blacklist,
                limit=INTERACTIVE_PAGE_LIMIT,
                pid=search_state['current_pid'],
                timeout=INTERACTIVE_SEARCH_TIMEOUT_SECONDS,
                request_kind="interactive"
            )

            if not posts:
                logger.debug("No posts found on page %s, stopping search", search_state["current_pid"])
                return None

            used_posts = search_state['used_posts'] | excluded_post_ids
            valid_posts = [
                post for post in posts
                if post.get("file_url") and self._post_id(post) not in used_posts
            ]

            if valid_posts:
                selected_post = random.choice(valid_posts)
                selected_post_id = self._post_id(selected_post)
                if selected_post_id is not None:
                    search_state['used_posts'].add(selected_post_id)
                logger.debug(
                    "Selected next post ID %s from page %s",
                    selected_post.get("id"),
                    search_state["current_pid"]
                )
                return selected_post

            search_state['current_pid'] += 1
            scanned_pages += 1
            logger.debug(
                "All posts on page %s used, moving to page %s",
                search_state["current_pid"] - 1,
                search_state["current_pid"]
            )

        logger.debug("Next image search stopped after %s pages", scanned_pages)
        return None

    async def search_subscription_cache(
        self,
        tags: str,
        blacklist: Set[str],
        pid: int = 0
    ) -> Optional[List[Dict]]:
        return await self.search(
            tags=tags,
            blacklist=blacklist,
            limit=MAX_POSTS_PER_REQUEST,
            pid=pid,
            timeout=SUBSCRIPTION_CACHE_SEARCH_TIMEOUT_SECONDS,
            request_kind="background",
        )

    async def get_post_by_id(self, post_id: int, timeout: int = 8) -> Optional[Dict]:
        """Получить конкретный пост по ID"""
        await self.ensure_session()

        params = self._build_params(id=post_id)

        try:
            async with self.interactive_semaphore:
                await self._wait_for_rate_limit("interactive")
                async with self.session.get(
                    API_BASE_URL, params=params, timeout=timeout
                ) as response:
                    if response.status == 200:
                        response_text = await response.text()
                        try:
                            data = json.loads(response_text)
                            # API возвращает массив, даже для одного поста
                            if isinstance(data, list) and len(data) > 0:
                                return data[0]
                        except json.JSONDecodeError:
                            logger.warning("JSON decode error for post %s", post_id)
                return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning("Temporary API error in get_post_by_id post=%s: %s", post_id, e)
            return None

    async def autocomplete(self, query: str) -> List[str]:
        """Автодополнение тегов"""
        await self.ensure_session()

        try:
            async with self.session.get(
                AUTOCOMPLETE_URL,
                params={"q": query},
                timeout=10
            ) as response:
                if response.status == 200:
                    response_text = await response.text()
                    try:
                        data = json.loads(response_text)
                        if isinstance(data, list):
                            results = []
                            for item in data[:10]:
                                if isinstance(item, dict):
                                    if "value" in item:
                                        results.append(item["value"])
                                    elif "name" in item:
                                        results.append(item["name"])
                                elif isinstance(item, str):
                                    results.append(item)
                            return results
                    except json.JSONDecodeError:
                        pass
                return []
        except Exception as e:
            logger.exception("Autocomplete Error: %s", e)
            return []


# Глобальный экземпляр API
api = rule34API()
