import asyncio
from dataclasses import dataclass
import io
import logging
from app.observability.logic_trace import trace_event, annotate
import ipaddress
import os
import socket
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from app.telegram.delivery import execute_telegram_request
from app.services.media_preferences import runtime_metrics
from app.telegram.formatting import md_text
from app.config import GLOBAL_DOWNLOAD_CONCURRENCY

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
PHOTO_DOWNLOAD_CHUNK_SIZE = 64 * 1024
global_download_semaphore = asyncio.Semaphore(GLOBAL_DOWNLOAD_CONCURRENCY)


@dataclass(frozen=True, slots=True)
class DownloadedPhotoMeta:
    final_url: str
    content_type: str
    extension: str
    bytes_read: int
    filename: str


class PhotoDownloadLimitExceeded(ValueError):
    """Base class for typed streaming download limits."""


class FileDownloadLimitExceeded(PhotoDownloadLimitExceeded):
    pass


class TotalDownloadLimitExceeded(PhotoDownloadLimitExceeded):
    pass


class DeliveryPreconditionFailed(RuntimeError):
    """The recipient or claim became invalid before a Telegram request."""


@dataclass(slots=True)
class DownloadByteBudget:
    limit: int
    consumed: int = 0

    def __post_init__(self) -> None:
        self.limit = max(1, int(self.limit))
        self.consumed = max(0, int(self.consumed))

    def consume(self, byte_count: int) -> None:
        self.consumed += max(0, int(byte_count))
        if self.consumed > self.limit:
            raise TotalDownloadLimitExceeded(
                f"Total download budget exceeded: {self.consumed} > {self.limit}"
            )


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


def _download_filename_from_url(url: str, extension: str | None = None) -> str:
    path = unquote(urlparse(url).path)
    filename = path.rsplit("/", 1)[-1] or "image.jpg"
    stem, current_ext = os.path.splitext(filename)
    if extension:
        safe_stem = stem or "image"
        filename = f"{safe_stem}{extension}"
    elif current_ext.lower() not in DOWNLOADABLE_PHOTO_EXTENSIONS:
        filename += ".jpg"
    return filename


def _looks_like_supported_photo(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _is_ambiguous_network_error(error: Exception) -> bool:
    return isinstance(error, NetworkError) and not isinstance(error, BadRequest)


def _photo_extension_from_header(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _photo_extension_from_content_type(content_type: str) -> str | None:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/pjpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(content_type.lower())


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


class PublicPhotoResolver(AbstractResolver):
    """Resolve once and give aiohttp only addresses already proven public.

    The connector consumes these exact results, closing the validation/request
    DNS-rebinding window without rewriting the URL hostname (and therefore
    preserving the HTTP Host header, TLS SNI and certificate verification).
    """

    def __init__(self, resolver: AbstractResolver | None = None):
        self._resolver = resolver or aiohttp.resolver.DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        addresses = await self._resolver.resolve(host, port, family)
        if not addresses or any(not _is_public_ip(item["host"]) for item in addresses):
            raise ValueError("Private or non-public photo host is not allowed")
        return addresses

    async def close(self) -> None:
        await self._resolver.close()


class PublicPhotoConnector(aiohttp.TCPConnector):
    """Marker connector whose DNS results are validated before connection."""

    def __init__(self, **kwargs):
        kwargs.setdefault("resolver", PublicPhotoResolver())
        kwargs.setdefault("use_dns_cache", False)
        super().__init__(**kwargs)


def create_public_photo_session(
    *,
    timeout: aiohttp.ClientTimeout | None = None,
    headers: dict[str, str] | None = None,
) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=PublicPhotoConnector(),
        timeout=timeout,
        headers=headers,
    )


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


async def download_photo_to_path(
    url: str,
    destination_path: str,
    *,
    session: aiohttp.ClientSession | None = None,
    max_bytes: int = MAX_DOWNLOADED_PHOTO_BYTES,
    semaphore: asyncio.Semaphore | None = None,
    cancel_event: asyncio.Event | None = None,
    byte_budget: DownloadByteBudget | None = None,
) -> DownloadedPhotoMeta:
    max_bytes = max(1, int(max_bytes))
    timeout = aiohttp.ClientTimeout(total=PHOTO_DOWNLOAD_TIMEOUT_SECONDS)
    headers = {"User-Agent": PHOTO_DOWNLOAD_USER_AGENT}
    current_url = url
    # Never trust a caller-supplied ordinary ClientSession: its connector would
    # resolve the hostname again without the public-address invariant.
    supplied_session_is_safe = (
        isinstance(session, aiohttp.ClientSession)
        and isinstance(session.connector, PublicPhotoConnector)
    )
    use_injected_transport = session is not None and not isinstance(
        session, aiohttp.ClientSession
    )
    if supplied_session_is_safe or use_injected_transport:
        active_session = session
        close_session = False
    else:
        active_session = create_public_photo_session(timeout=timeout, headers=headers)
        close_session = True
    limiter = semaphore or global_download_semaphore

    try:
        async with limiter:
            for redirect_count in range(PHOTO_DOWNLOAD_MAX_REDIRECTS + 1):
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError
                await _validate_public_photo_url(current_url)
                async with active_session.get(current_url, allow_redirects=False) as response:
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
                                raise FileDownloadLimitExceeded(
                                    "Downloaded photo exceeds the configured per-file size limit"
                                )
                        except ValueError as exc:
                            if isinstance(exc, FileDownloadLimitExceeded):
                                raise
                            raise ValueError("Invalid photo content-length") from exc

                    bytes_read = 0
                    header = bytearray()
                    with open(destination_path, "wb") as destination:
                        async for chunk in response.content.iter_chunked(PHOTO_DOWNLOAD_CHUNK_SIZE):
                            if byte_budget is not None:
                                byte_budget.consume(len(chunk))
                            if cancel_event and cancel_event.is_set():
                                raise asyncio.CancelledError
                            bytes_read += len(chunk)
                            if bytes_read > max_bytes:
                                raise FileDownloadLimitExceeded(
                                    "Downloaded photo exceeds the configured per-file size limit"
                                )
                            destination.write(chunk)
                            if len(header) < 12:
                                header.extend(chunk[: 12 - len(header)])
                    break
            else:
                raise ValueError("Too many photo redirects")
    except BaseException:
        try:
            if os.path.exists(destination_path):
                os.remove(destination_path)
        except OSError:
            logger.warning("Failed to remove partial downloaded photo at %s", destination_path)
        raise
    finally:
        if close_session and not active_session.closed:
            await active_session.close()

    if bytes_read == 0:
        try:
            os.remove(destination_path)
        except OSError:
            logger.warning("Failed to remove empty downloaded photo at %s", destination_path)
        raise ValueError("Downloaded photo is empty")

    header_bytes = bytes(header)
    extension = _photo_extension_from_header(header_bytes) or _photo_extension_from_content_type(
        content_type
    )
    if not extension or not _looks_like_supported_photo(header_bytes):
        try:
            os.remove(destination_path)
        except OSError:
            logger.warning("Failed to remove unsupported downloaded photo at %s", destination_path)
        raise ValueError("Downloaded file is not a supported JPEG, PNG or WebP image")

    return DownloadedPhotoMeta(
        final_url=current_url,
        content_type=content_type,
        extension=extension,
        bytes_read=bytes_read,
        filename=_download_filename_from_url(current_url, extension=extension),
    )


async def _download_photo_file(
    url: str, max_bytes: int = MAX_DOWNLOADED_PHOTO_BYTES
) -> io.BytesIO:
    photo = io.BytesIO()
    try:
        with tempfile.NamedTemporaryFile(prefix="download_photo_", suffix=".tmp", delete=False) as temp_file:
            destination_path = temp_file.name
        meta = await download_photo_to_path(url, destination_path, max_bytes=max_bytes)
        with open(destination_path, "rb") as source:
            photo.write(source.read())
        photo.name = meta.filename
    except BaseException:
        photo.close()
        raise
    finally:
        if "destination_path" in locals():
            try:
                if os.path.exists(destination_path):
                    os.remove(destination_path)
            except OSError:
                logger.warning("Failed to remove temporary downloaded photo at %s", destination_path)

    photo.seek(0)
    return photo


def _is_seekable_upload(upload) -> bool:
    seekable = getattr(upload, "seekable", None)
    if not callable(seekable):
        return False
    try:
        return bool(seekable())
    except (OSError, ValueError):
        return False


async def reply_media_url(message, url: str, caption: str, reply_markup, has_spoiler: bool = False):
    user_id = _message_user_id(message)
    url_path = media_url_path_lower(url)
    if url_path.endswith((".mp4", ".webm")):
        await execute_telegram_request(
            lambda: message.reply_video(
                url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            ),
            operation_name="reply_video",
            chat_id=user_id,
        )
    elif url_path.endswith(".gif"):
        await execute_telegram_request(
            lambda: message.reply_animation(
                url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            ),
            operation_name="reply_animation",
            chat_id=user_id,
        )
    else:
        await execute_telegram_request(
            lambda: message.reply_photo(
                url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            ),
            operation_name="reply_photo",
            chat_id=user_id,
        )
    return True


async def reply_downloaded_photo(message, url: str, caption: str, reply_markup, has_spoiler: bool = False):
    user_id = _message_user_id(message)
    photo = await _download_photo_file(url)
    seekable = _is_seekable_upload(photo)

    async def upload_photo():
        if seekable:
            photo.seek(0)
        return await message.reply_photo(
            photo,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )

    try:
        await execute_telegram_request(
            upload_photo,
            operation_name="reply_photo_upload",
            chat_id=user_id,
            max_retry_after_attempts=None if seekable else 0,
        )
        return True
    finally:
        photo.close()


async def _ensure_delivery_precondition(before_send) -> None:
    if before_send is not None and not await before_send():
        trace_event("telegram.send.skipped", reason="delivery_precondition_failed")
        raise DeliveryPreconditionFailed("Delivery precondition is no longer valid")
    trace_event('telegram.request.start', precondition_passed=True)


async def send_media_url(
    bot, chat_id: int, url: str, caption: str, reply_markup,
    has_spoiler: bool = False, before_send=None,
):
    url_path = media_url_path_lower(url)
    if url_path.endswith((".mp4", ".webm")):
        async def operation():
            await _ensure_delivery_precondition(before_send)
            return await bot.send_video(
                chat_id=chat_id,
                video=url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            )

        await execute_telegram_request(
            operation,
            operation_name="send_video",
            chat_id=chat_id,
        )
    elif url_path.endswith(".gif"):
        async def operation():
            await _ensure_delivery_precondition(before_send)
            return await bot.send_animation(
                chat_id=chat_id,
                animation=url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            )

        await execute_telegram_request(
            operation,
            operation_name="send_animation",
            chat_id=chat_id,
        )
    else:
        async def operation():
            await _ensure_delivery_precondition(before_send)
            return await bot.send_photo(
                chat_id=chat_id,
                photo=url,
                caption=caption if caption else None,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            )

        await execute_telegram_request(
            operation,
            operation_name="send_photo",
            chat_id=chat_id,
        )
    return True


async def send_downloaded_photo(
    bot, chat_id: int, url: str, caption: str, reply_markup,
    has_spoiler: bool = False, before_send=None,
):
    photo = await _download_photo_file(url)
    seekable = _is_seekable_upload(photo)

    async def upload_photo():
        await _ensure_delivery_precondition(before_send)
        if seekable:
            photo.seek(0)
        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )

    try:
        await execute_telegram_request(
            upload_photo,
            operation_name="send_photo_upload",
            chat_id=chat_id,
            max_retry_after_attempts=None if seekable else 0,
        )
        return True
    finally:
        photo.close()


async def _reply_text(message, text: str, **kwargs) -> bool:
    user_id = _message_user_id(message)
    try:
        await execute_telegram_request(
            lambda: message.reply_text(text, **kwargs),
            operation_name="reply_text",
            chat_id=user_id,
        )
        return True
    except RetryAfter:
        return False


async def send_text_to_chat(bot, chat_id: int, *, before_send=None, **kwargs) -> bool:
    async def operation():
        await _ensure_delivery_precondition(before_send)
        return await bot.send_message(chat_id=chat_id, **kwargs)

    try:
        await execute_telegram_request(
            operation,
            operation_name="send_message",
            chat_id=chat_id,
        )
        return True
    except (RetryAfter, DeliveryPreconditionFailed):
        return False


async def send_post_media(
    message,
    post: dict,
    caption: str = "",
    keyboard=None,
    retries: int = 2,
    has_spoiler: bool = False,
    raise_on_timeout: bool = False,
):
    reply_markup = keyboard
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

    for source_index, (url_kind, media_url) in enumerate(candidates):
        trace_event("media.source.attempt", level="normal", post_id=post.get("id"), url_kind=url_kind)
        if source_index:
            trace_event("telegram.send.fallback", post_id=post.get("id"), fallback_source=url_kind, reason="previous_source_rejected")
        annotate(post_id=post.get("id"), media_type="video" if media_url_path_lower(media_url).endswith((".mp4", ".webm")) else "animation" if media_url_path_lower(media_url).endswith(".gif") else "photo")
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
            except RetryAfter:
                return False
            except TimedOut:
                raise
            except Exception as exc:
                if _is_ambiguous_network_error(exc):
                    raise
                logger.warning(
                    "Media send failed post=%s url_kind=%s attempt=%s/%s: %s",
                    post.get("id"),
                    url_kind,
                    attempt,
                    retries,
                    exc,
                )
                if _telegram_url_fetch_failed(exc) and _is_downloadable_photo_url(media_url):
                    trace_event("telegram.send.fallback", post_id=post.get("id"), fallback_source="download_upload")
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
                    except RetryAfter:
                        return False
                    except TimedOut:
                        raise
                    except Exception as fallback_exc:
                        if _is_ambiguous_network_error(fallback_exc):
                            raise
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
    raise_on_timeout: bool = False,
    before_send=None,
):
    reply_markup = keyboard
    candidates = get_media_url_candidates(post)
    fallback_url = candidates[0][1] if candidates else ""
    if not fallback_url:
        await send_text_to_chat(
            bot,
            chat_id,
            before_send=before_send,
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

    for source_index, (url_kind, media_url) in enumerate(candidates):
        trace_event("media.source.attempt", level="normal", post_id=post.get("id"), url_kind=url_kind)
        if source_index:
            trace_event("telegram.send.fallback", post_id=post.get("id"), fallback_source=url_kind, reason="previous_source_rejected")
        annotate(post_id=post.get("id"), media_type="video" if media_url_path_lower(media_url).endswith((".mp4", ".webm")) else "animation" if media_url_path_lower(media_url).endswith(".gif") else "photo")
        for attempt in range(1, retries + 1):
            try:
                sent = await send_media_url(
                    bot, chat_id, media_url, caption, reply_markup, has_spoiler,
                    before_send,
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
            except RetryAfter:
                return False
            except TimedOut:
                raise
            except DeliveryPreconditionFailed:
                return False
            except Exception as exc:
                if _is_ambiguous_network_error(exc):
                    raise
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
                    trace_event("telegram.send.fallback", post_id=post.get("id"), fallback_source="download_upload")
                    try:
                        sent = await send_downloaded_photo(
                            bot, chat_id, media_url, caption, reply_markup,
                            has_spoiler, before_send,
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
                    except RetryAfter:
                        return False
                    except TimedOut:
                        raise
                    except DeliveryPreconditionFailed:
                        return False
                    except Exception as fallback_exc:
                        if _is_ambiguous_network_error(fallback_exc):
                            raise
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
        before_send=before_send,
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
