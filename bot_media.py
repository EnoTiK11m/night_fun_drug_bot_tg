import asyncio
import logging

from bot_formatting import md_text
from bot_keyboards import get_subscription_image_keyboard

logger = logging.getLogger(__name__)


def get_media_url_candidates(post: dict) -> list[tuple[str, str]]:
    candidates = []
    for key in ("file_url", "sample_url", "preview_url"):
        url = post.get(key)
        if url and all(url != existing_url for _, existing_url in candidates):
            candidates.append((key, url))
    return candidates


async def reply_media_url(message, url: str, caption: str, reply_markup):
    if url.lower().endswith((".mp4", ".webm")):
        await message.reply_video(
            url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    elif url.lower().endswith(".gif"):
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


async def send_media_url(bot, chat_id: int, url: str, caption: str, reply_markup):
    if url.lower().endswith((".mp4", ".webm")):
        await bot.send_video(
            chat_id=chat_id,
            video=url,
            caption=caption if caption else None,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    elif url.lower().endswith(".gif"):
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
        await message.reply_text(
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
                await reply_media_url(message, media_url, caption, reply_markup)
                logger.info(
                    "Media send ok post=%s url_kind=%s",
                    post.get("id"),
                    url_kind,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "Media send failed post=%s url_kind=%s attempt=%s/%s: %s",
                    post.get("id"),
                    url_kind,
                    attempt,
                    retries,
                    exc,
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
    await message.reply_text(
        fallback,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    logger.warning("Media fallback sent post=%s", post.get("id"))
    return True


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
        await bot.send_message(
            chat_id=chat_id,
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
                await send_media_url(bot, chat_id, media_url, caption, reply_markup)
                logger.info(
                    "Subscription media send ok user=%s post=%s url_kind=%s",
                    chat_id,
                    post.get("id"),
                    url_kind,
                )
                return True
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
                if attempt < retries:
                    await asyncio.sleep(1)

    fallback = (
        "⚠️ Не удалось отправить файл напрямую. "
        "Возможна проблема с размером, форматом, сетью или сервером.\n"
        f"Открыть файл: {md_text(fallback_url)}"
    )
    if caption:
        fallback += f"\n\n{caption}"
    await bot.send_message(
        chat_id=chat_id,
        text=fallback,
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    logger.warning(
        "Subscription media fallback sent user=%s post=%s",
        chat_id,
        post.get("id"),
    )
    return True
