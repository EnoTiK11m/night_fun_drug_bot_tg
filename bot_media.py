import asyncio
import io
import logging
import ipaddress
import socket
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from telegram.error import RetryAfter

from bot_delivery import telegram_rate_limiter
from bot_features import runtime_metrics
from bot_formatting import md_text
from bot_keyboards import get_subscription_image_keyboard

logger = logging.getLogger(__name__)

DOWNLOADABLE_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_DOWNLOADED_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_DOWNLOAD_TIMEOUT_SECONDS = 20
PHOTO_DOWNLOAD_USER_AGENT = "night-fun-drug-bot/1.0"
ALLOWED_PHOTO_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/pjpeg",
    "image/png",
    "image/webp",
    "application/octet-stream",
}
PHOTO_DOWNLOAD_MAX_REDIRECTS = 5


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


def _looks_like_supported_photo(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _validate_public_photo_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Unsupported photo URL")
    if parsed.username or parsed.password:
        raise ValueError("Photo URL credentials are not allowed")
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise ValueError("Private or non-public photo host is not allowed")
        return
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses or any(not _is_public_ip(item[4][0]) for item in addresses):
        raise ValueError("Private or non-public photo host is not allowed")


async def _download_photo_file(
    url: str, max_bytes: int = MAX_DOWNLOADED_PHOTO_BYTES
) -> io.BytesIO:
    max_bytes = max(1, int(max_bytes))
    timeout = aiohttp.ClientTimeout(total=PHOTO_DOWNLOAD_TIMEOUT_SECONDS)
    headers = {"User-Agent": PHOTO_DOWNLOAD_USER_AGENT}
    current_url = url
    photo = io.BytesIO()
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for redirect_count in range(PHOTO_DOWNLOAD_MAX_REDIRECTS + 1):
                await _validate_public_photo_url(current_url)
                async with session.get(current_url, allow_redirects=False) as response:
                    if 300 <= response.status < 400 and response.headers.get("Location"):
                        if redirect_count >= PHOTO_DOWNLOAD_MAX_REDIRECTS:
                            raise ValueError("Too many photo redirects")
                        current_url = urljoin(current_url, response.headers["Location"])
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                    if content_type and content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
                        raise ValueError(f"Unsupported photo content-type: {content_type}")
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError("Downloaded photo exceeds the configured size limit")
                        except ValueError as exc:
                            if "configured size limit" in str(exc):
                                raise
                            raise ValueError("Invalid photo content-length") from exc

                    async for chunk in response.content.iter_chunked(64 * 1024):
                        photo.write(chunk)
                        if photo.tell() > max_bytes:
                            raise ValueError("Downloaded photo exceeds the configured size limit")
                    break
            else:
                raise ValueError("Too many photo redirects")
    except Exception:
        photo.close()
        raise

    if photo.tell() == 0:
        raise ValueError("Downloaded photo is empty")

    photo.seek(0)
    header = photo.read(12)
    if not _looks_like_supported_photo(header):
        photo.close()
        raise ValueError("Downloaded file is not a supported JPEG, PNG or WebP image")

    photo.seek(0)
    photo.name = _download_filename_from_url(url)
    return photo


async def reply_media_url(message, url: str, caption: str, reply_markup, has_spoiler: bool = False):
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
            has_spoiler=has_spoiler,
        )
    elif url_path.endswith(".gif"):
        await message.reply_animation(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
    else:
        await message.reply_photo(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
    return True


async def reply_downloaded_photo(message, url: str, caption: str, reply_markup, has_spoiler: bool = False):
    user_id = _message_user_id(message)
    photo = await _download_photo_file(url)
    try:
        if not await telegram_rate_limiter.wait_for_slot(user_id):
            return False
        await message.reply_photo(
            photo,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
        return True
    finally:
        photo.close()


async def send_media_url(bot, chat_id: int, url: str, caption: str, reply_markup, has_spoiler: bool = False):
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
            has_spoiler=has_spoiler,
        )
    elif url_path.endswith(".gif"):
        await bot.send_animation(
            chat_id=chat_id,
            animation=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
    else:
        await bot.send_photo(
            chat_id=chat_id,
            photo=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
    return True


async def send_downloaded_photo(bot, chat_id: int, url: str, caption: str, reply_markup, has_spoiler: bool = False):
    photo = await _download_photo_file(url)
    try:
        if not await telegram_rate_limiter.wait_for_slot(chat_id):
            return False
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
        return True
    finally:
        photo.close()


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
    has_spoiler: bool = False,
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
                sent = await reply_media_url(
                    message, media_url, caption, reply_markup, has_spoiler
                )
                if not sent:
                    return False
                logger.info(
                    "Media send ok post=%s url_kind=%s",
                    post.get("id"),
                    url_kind,
                )
                runtime_metrics.increment("media_direct_ok")
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
                            message, media_url, caption, reply_markup, has_spoiler
                        )
                        if sent:
                            logger.info(
                                "Media downloaded fallback send ok post=%s url_kind=%s",
                                post.get("id"),
                                url_kind,
                            )
                            runtime_metrics.increment("media_upload_fallback_ok")
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
        runtime_metrics.increment("media_text_fallback")
    else:
        runtime_metrics.failure(f"interactive post={post.get('id')}")
    return sent


async def send_post_media_to_chat(
    bot,
    chat_id: int,
    post: dict,
    caption: str = "",
    keyboard=None,
    retries: int = 2,
    has_spoiler: bool = False,
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
                sent = await send_media_url(
                    bot, chat_id, media_url, caption, reply_markup, has_spoiler
                )
                if not sent:
                    return False
                logger.info(
                    "Subscription media send ok user=%s post=%s url_kind=%s",
                    chat_id,
                    post.get("id"),
                    url_kind,
                )
                runtime_metrics.increment("media_direct_ok")
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
                            bot, chat_id, media_url, caption, reply_markup, has_spoiler
                        )
                        if sent:
                            logger.info(
                                "Subscription media downloaded fallback send ok user=%s post=%s url_kind=%s",
                                chat_id,
                                post.get("id"),
                                url_kind,
                            )
                            runtime_metrics.increment("media_upload_fallback_ok")
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
        runtime_metrics.increment("media_text_fallback")
    else:
        runtime_metrics.failure(f"subscription user={chat_id} post={post.get('id')}")
    return sent
