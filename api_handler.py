import aiohttp
import random
import json
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


class rule34API:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.user_search_states = {}  # Храним состояние поиска для каждого пользователя

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

    async def save_search_state(self, user_id: int, tags: str, blacklist: Set[str]):
        """Сохранить состояние поиска для кнопки 'ещё'"""
        self.user_search_states[user_id] = {
            'tags': tags,
            'blacklist': blacklist,
            'current_pid': 0,
            'used_posts': set()  # ID уже показанных постов
        }

    async def search(
        self,
        tags: str,
        blacklist: Set[str],
        limit: int = DEFAULT_LIMIT,
        pid: int = 0
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

        print(f"DEBUG: Searching with tags: '{search_tags}', page: {pid}")
        print(f"DEBUG: API URL: {API_BASE_URL}")

        try:
            async with self.session.get(API_BASE_URL, params=params, timeout=30) as response:
                print(f"DEBUG: Response status: {response.status}")

                if response.status == 200:
                    # Получаем текст ответа
                    response_text = await response.text()

                    # Пробуем распарсить как JSON
                    try:
                        data = json.loads(response_text)
                        print(
                            f"DEBUG: Found {len(data) if isinstance(data, list) else 0} posts on page {pid}")

                        # API возвращает сразу массив постов
                        if isinstance(data, list):
                            return data

                        # Если это словарь с ошибкой
                        if isinstance(data, dict) and data.get("success") is False:
                            error_msg = data.get("message", "Unknown error")
                            print(f"API Error: {error_msg}")
                            return None

                        return None

                    except json.JSONDecodeError as e:
                        print(f"JSON decode error: {e}")
                        return None

                else:
                    print(f"HTTP Error: {response.status}")
                    return None

        except Exception as e:
            print(f"API Error: {e}")
            return None

    async def get_random_image(
        self,
        tags: str,
        blacklist: Set[str]
    ) -> Optional[Dict]:
        """Получить случайное изображение (для нового поиска)"""
        print(f"DEBUG: get_random_image called with tags: '{tags}'")

        # Пробуем несколько случайных страниц
        for attempt in range(3):
            random_pid = random.randint(0, 10)  # Страницы 0-10
            posts = await self.search(
                tags=tags,
                blacklist=blacklist,
                limit=100,
                pid=random_pid
            )

            if posts:
                print(f"DEBUG: Found {len(posts)} posts on page {random_pid}")
                # Фильтруем посты с валидными file_url
                valid_posts = [post for post in posts if post.get("file_url")]
                if valid_posts:
                    selected_post = random.choice(valid_posts)
                    print(
                        f"DEBUG: Selected post ID: {selected_post.get('id')}")
                    return selected_post

        print("DEBUG: No posts found after all attempts")
        return None

    async def get_next_image(
        self,
        user_id: int,
        tags: str,
        blacklist: Set[str]
    ) -> Optional[Dict]:
        """Получить следующее изображение (для кнопки 'ещё')"""
        print(f"DEBUG: get_next_image for user {user_id} with tags: '{tags}'")

        # Получаем или создаем состояние поиска
        search_state = self.user_search_states.get(user_id)
        if not search_state or search_state['tags'] != tags:
            # Новый поиск, инициализируем состояние
            search_state = {
                'tags': tags,
                'blacklist': blacklist,
                'current_pid': 0,
                'used_posts': set()
            }
            self.user_search_states[user_id] = search_state

        # Пробуем найти новый пост на текущей или следующих страницах
        for page_attempt in range(5):  # Пробуем 5 страниц
            posts = await self.search(
                tags=tags,
                blacklist=blacklist,
                limit=100,
                pid=search_state['current_pid']
            )

            if posts:
                # Фильтруем посты с валидными file_url и которые еще не показывались
                valid_posts = [
                    post for post in posts
                    if post.get("file_url") and post.get("id") not in search_state['used_posts']
                ]

                if valid_posts:
                    selected_post = random.choice(valid_posts)
                    search_state['used_posts'].add(selected_post.get('id'))
                    print(
                        f"DEBUG: Selected next post ID: {selected_post.get('id')} from page {search_state['current_pid']}")
                    return selected_post
                else:
                    # Все посты на этой странице уже показаны, переходим к следующей
                    search_state['current_pid'] += 1
                    print(
                        f"DEBUG: All posts on page {search_state['current_pid'] - 1} used, moving to page {search_state['current_pid']}")
            else:
                # На этой странице нет постов, пробуем следующую
                search_state['current_pid'] += 1

        # Если ничего не нашли, сбрасываем состояние и начинаем заново
        print("DEBUG: No new posts found, resetting search state")
        self.user_search_states[user_id] = {
            'tags': tags,
            'blacklist': blacklist,
            'current_pid': 0,
            'used_posts': set()
        }

        # Пробуем найти любой случайный пост
        return await self.get_random_image(tags, blacklist)

    async def get_post_by_id(self, post_id: int) -> Optional[Dict]:
        """Получить конкретный пост по ID"""
        await self.ensure_session()

        params = self._build_params(id=post_id)

        try:
            async with self.session.get(API_BASE_URL, params=params, timeout=30) as response:
                if response.status == 200:
                    response_text = await response.text()
                    try:
                        data = json.loads(response_text)
                        # API возвращает массив, даже для одного поста
                        if isinstance(data, list) and len(data) > 0:
                            return data[0]
                    except json.JSONDecodeError:
                        print(f"JSON decode error for post {post_id}")
                return None
        except Exception as e:
            print(f"API Error in get_post_by_id: {e}")
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
            print(f"Autocomplete Error: {e}")
            return []


# Глобальный экземпляр API
api = rule34API()
