import asyncio
import io
import logging
from urllib.parse import unquote, urlparse

import aiohttp
from telegram.error import RetryAfter

from bot_delivery import telegram_rate_limiter
from bot_formatting import md_text
from bot_keyboards import get_subscription_image_keyboard

logger = logging.getLogger(__name__)

DOWNLOADABLE_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_DOWNLOADED_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_DOWNLOAD_TIMEOUT_SECONDS = 20
PHOTO_DOWNLOAD_USER_AGENT = "night-fun-drug-bot/1.0"


def _message_user_id(message) -> int:
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    user_id = getattr(user, "id", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int):
        return chat_id
    if isinstance(user_id, int):
        return user_id
    return id(message)


def get_media_url_candidates(post: dict) -> list[tuple[str, str]]:
    candidates = []
    for key in ("file_url", "sample_url", "preview_url"):
        url = post.get(key)
        if url and all(url != existing_url for _, existing_url in candidates):
            candidates.append((key, url))
    return candidates


def media_url_path_lower(url: str) -> str:
    return urlparse(url).path.lower()


def _is_downloadable_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return media_url_path_lower(url).endswith(DOWNLOADABLE_PHOTO_EXTENSIONS)


def _telegram_url_fetch_failed(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "failed to get http url content",
            "wrong type of the web page content",
            "invalid file http url specified",
        )
    )


def _download_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    filename = path.rsplit("/", 1)[-1] or "image.jpg"
    if not filename.lower().endswith(DOWNLOADABLE_PHOTO_EXTENSIONS):
        filename += ".jpg"
    return filename


async def _download_photo_file(url: str) -> io.BytesIO:
    timeout = aiohttp.ClientTimeout(total=PHOTO_DOWNLOAD_TIMEOUT_SECONDS)
    headers = {"User-Agent": PHOTO_DOWNLOAD_USER_AGENT}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if content_type.startswith("text/"):
                raise ValueError(f"Unsupported photo content-type: {content_type}")

            photo = io.BytesIO()
            async for chunk in response.content.iter_chunked(64 * 1024):
                photo.write(chunk)
                if photo.tell() > MAX_DOWNLOADED_PHOTO_BYTES:
                    raise ValueError("Downloaded photo is too large for Telegram sendPhoto")

    if photo.tell() == 0:
        raise ValueError("Downloaded photo is empty")

    photo.seek(0)
    photo.name = _download_filename_from_url(url)
    return photo


async def reply_media_url(message, url: str, caption: str, reply_markup):
    user_id = _message_user_id(message)
    if not await telegram_rate_limiter.wait_for_slot(user_id):
        return False
    url_path = media_url_path_lower(url)
    if url_path.endswith((".mp4", ".webm")):
        await message.reply_video(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    elif url_path.endswith(".gif"):
        await message.reply_animation(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    else:
        await message.reply_photo(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    return True


async def reply_downloaded_photo(message, url: str, caption: str, reply_markup):
    user_id = _message_user_id(message)
    if not await telegram_rate_limiter.wait_for_slot(user_id):
        return False
    photo = await _download_photo_file(url)
    await message.reply_photo(
        photo,
        caption=caption if caption else None,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return True


async def send_media_url(bot, chat_id: int, url: str, caption: str, reply_markup):
    if not await telegram_rate_limiter.wait_for_slot(chat_id):
        return False
    url_path = media_url_path_lower(url)
    if url_path.endswith((".mp4", ".webm")):
        await bot.send_video(
            chat_id=chat_id,
            video=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    elif url_path.endswith(".gif"):
        await bot.send_animation(
            chat_id=chat_id,
            animation=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    else:
        await bot.send_photo(
            chat_id=chat_id,
            photo=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    return True


async def send_downloaded_photo(bot, chat_id: int, url: str, caption: str, reply_markup):
    if not await telegram_rate_limiter.wait_for_slot(chat_id):
        return False
    photo = await _download_photo_file(url)
    await bot.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=caption if caption else None,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return True


async def _reply_text(message, text: str, **kwargs) -> bool:
    user_id = _message_user_id(message)
    if not await telegram_rate_limiter.wait_for_slot(user_id):
        return False
    try:
        await message.reply_text(text, **kwargs)
        return True
    except RetryAfter as exc:
        telegram_rate_limiter.apply_retry_after(user_id, exc)
        return False


async def send_text_to_chat(bot, chat_id: int, **kwargs) -> bool:
    if not await telegram_rate_limiter.wait_for_slot(chat_id):
        return False
    try:
        await bot.send_message(chat_id=chat_id, **kwargs)
        return True
    except RetryAfter as exc:
        telegram_rate_limiter.apply_retry_after(chat_id, exc)
        return False


async def send_post_media(
    message,
    post: dict,
    caption: str = "",
    keyboard=None,
    retries: int = 2,
):
    reply_markup = keyboard or get_subscription_image_keyboard(post.get("id", 0))
    candidates = get_media_url_candidates(post)
    fallback_url = candidates[0][1] if candidates else ""
    if not fallback_url:
        await _reply_text(
            message,
            "⚠️ У этого поста нет сохранённой ссылки на файл. "
            "Попробуйте открыть свежий пост или найти его через `/id`.",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        logger.warning("Media fallback missing url post=%s", post.get("id"))
        return False

    for url_kind, media_url in candidates:
        for attempt in range(1, retries + 1):
            try:
                sent = await reply_media_url(message, media_url, caption, reply_markup)
                if not sent:
                    return False
                logger.info(
                    "Media send ok post=%s url_kind=%s",
                    post.get("id"),
                    url_kind,
                )
                return True
            except RetryAfter as exc:
                telegram_rate_limiter.apply_retry_after(_message_user_id(message), exc)
                if attempt == retries:
                    return False
            except Exception as exc:
                logger.warning(
                    "Media send failed post=%s url_kind=%s attempt=%s/%s: %s",
                    post.get("id"),
                    url_kind,
                    attempt,
                    retries,
                    exc,
                )
                if _telegram_url_fetch_failed(exc) and _is_downloadable_photo_url(media_url):
                    try:
                        sent = await reply_downloaded_photo(
                            message, media_url, caption, reply_markup
                        )
                        if sent:
                            logger.info(
                                "Media downloaded fallback send ok post=%s url_kind=%s",
                                post.get("id"),
                                url_kind,
                            )
                            return True
                    except RetryAfter as retry_exc:
                        telegram_rate_limiter.apply_retry_after(
                            _message_user_id(message), retry_exc
                        )
                        if attempt == retries:
                            return False
                    except Exception as fallback_exc:
                        logger.warning(
                            "Media downloaded fallback failed post=%s url_kind=%s attempt=%s/%s: %s",
                            post.get("id"),
                            url_kind,
                            attempt,
                            retries,
                            fallback_exc,
                        )
                if attempt < retries:
                    await asyncio.sleep(1)

    fallback = (
        "⚠️ Не удалось отправить файл напрямую. "
        "Возможна проблема с размером, форматом, сетью или сервером.\n"
        f"Открыть файл: {md_text(fallback_url)}"
    )
    if caption:
        fallback += f"\n\n{caption}"
    sent = await _reply_text(
        message,
        fallback,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    if sent:
        logger.warning("Media fallback sent post=%s", post.get("id"))
    return sent


async def send_post_media_to_chat(
    bot,
    chat_id: int,
    post: dict,
    caption: str = "",
    keyboard=None,
    retries: int = 2,
):
    reply_markup = keyboard or get_subscription_image_keyboard(post.get("id", 0))
    candidates = get_media_url_candidates(post)
    fallback_url = candidates[0][1] if candidates else ""
    if not fallback_url:
        await send_text_to_chat(
            bot,
            chat_id,
            text=(
                "⚠️ У этого поста нет сохранённой ссылки на файл. "
                "Попробуйте открыть свежий пост или найти его через `/id`."
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        logger.warning(
            "Subscription media fallback missing url user=%s post=%s",
            chat_id,
            post.get("id"),
        )
        return False

    for url_kind, media_url in candidates:
        for attempt in range(1, retries + 1):
            try:
                sent = await send_media_url(bot, chat_id, media_url, caption, reply_markup)
                if not sent:
                    return False
                logger.info(
                    "Subscription media send ok user=%s post=%s url_kind=%s",
                    chat_id,
                    post.get("id"),
                    url_kind,
                )
                return True
            except RetryAfter as exc:
                telegram_rate_limiter.apply_retry_after(chat_id, exc)
                if attempt == retries:
                    return False
            except Exception as exc:
                logger.warning(
                    "Subscription media send failed user=%s post=%s url_kind=%s attempt=%s/%s: %s",
                    chat_id,
                    post.get("id"),
                    url_kind,
                    attempt,
                    retries,
                    exc,
                )
                if _telegram_url_fetch_failed(exc) and _is_downloadable_photo_url(media_url):
                    try:
                        sent = await send_downloaded_photo(
                            bot, chat_id, media_url, caption, reply_markup
                        )
                        if sent:
                            logger.info(
                                "Subscription media downloaded fallback send ok user=%s post=%s url_kind=%s",
                                chat_id,
                                post.get("id"),
                                url_kind,
                            )
                            return True
                    except RetryAfter as retry_exc:
                        telegram_rate_limiter.apply_retry_after(chat_id, retry_exc)
                        if attempt == retries:
                            return False
                    except Exception as fallback_exc:
                        logger.warning(
                            "Subscription media downloaded fallback failed user=%s post=%s url_kind=%s attempt=%s/%s: %s",
                            chat_id,
                            post.get("id"),
                            url_kind,
                            attempt,
                            retries,
                            fallback_exc,
                        )
                if attempt < retries:
                    await asyncio.sleep(1)

    fallback = (
        "⚠️ Не удалось отправить файл напрямую. "
        "Возможна проблема с размером, форматом, сетью или сервером.\n"
        f"Открыть файл: {md_text(fallback_url)}"
    )
    if caption:
        fallback += f"\n\n{caption}"
    sent = await send_text_to_chat(
        bot,
        chat_id,
        text=fallback,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    if sent:
        logger.warning(
            "Subscription media fallback sent user=%s post=%s",
            chat_id,
            post.get("id"),
        )
    return sent
