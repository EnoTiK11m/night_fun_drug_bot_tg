from datetime import UTC, datetime
from html import unescape

from telegram.helpers import escape_markdown

MAX_CAPTION_LENGTH = 1024
SUBSCRIPTION_MIN_INTERVAL = 1
SUBSCRIPTION_MAX_INTERVAL = 120
SUBSCRIPTION_DEFAULT_INTERVAL = 10
FAVORITES_PAGE_SIZE = 10
SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def md_code(value) -> str:
    return unescape(str(value)).replace("`", "'")


def md_text(value) -> str:
    return escape_markdown(unescape(str(value)), version=1)


def clamp_caption(caption: str) -> str:
    if len(caption) <= MAX_CAPTION_LENGTH:
        return caption
    return caption[: MAX_CAPTION_LENGTH - 3] + "..."


def parse_subscription_interval(value: str) -> int:
    if not value.isdigit():
        return SUBSCRIPTION_DEFAULT_INTERVAL

    interval = int(value)
    return max(SUBSCRIPTION_MIN_INTERVAL, min(interval, SUBSCRIPTION_MAX_INTERVAL))


def parse_pause_minutes(value: str) -> int:
    parts = value.strip().lower().replace(",", ".").split()
    if not parts:
        return 60

    raw_number = parts[0]
    raw_unit = parts[1] if len(parts) > 1 else ""
    suffixes = (
        ("минут", 1),
        ("мин", 1),
        ("m", 1),
        ("час", 60),
        ("ч", 60),
        ("h", 60),
        ("день", 1440),
        ("ден", 1440),
        ("д", 1440),
        ("d", 1440),
    )

    for suffix, multiplier in suffixes:
        if raw_number.endswith(suffix):
            raw_unit = suffix
            raw_number = raw_number[: -len(suffix)]
            break

    try:
        amount = float(raw_number)
    except (TypeError, ValueError):
        return 60

    multiplier = 1
    for suffix, candidate in suffixes:
        if raw_unit.startswith(suffix):
            multiplier = candidate
            break

    pause_minutes = round(amount * multiplier)
    return max(1, min(pause_minutes, 10080))


def format_pause_duration(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} дн."
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} ч."
    return f"{minutes} мин."


def parse_pause_until(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, SQLITE_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def format_remaining_pause(pause_until: str | None) -> str:
    pause_until_dt = parse_pause_until(pause_until)
    if not pause_until_dt:
        return ""

    now = datetime.now(UTC).replace(tzinfo=None)
    remaining_seconds = int((pause_until_dt - now).total_seconds())
    if remaining_seconds <= 0:
        return ""

    minutes = max(1, (remaining_seconds + 59) // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes and not days:
        parts.append(f"{minutes} мин.")
    return " ".join(parts)


def clamp_page(page: int, total_items: int, page_size: int = FAVORITES_PAGE_SIZE) -> int:
    if total_items <= 0:
        return 0
    max_page = (total_items - 1) // page_size
    return max(0, min(page, max_page))


def build_subscription_gallery_caption(
    sub_query: str, post: dict, index: int, total: int
) -> str:
    tags = post.get("tags", "")
    if len(tags) > 120:
        tags = tags[:120] + "..."

    return clamp_caption(
        f"🔔 Подписка: `{md_code(sub_query)}`\n"
        f"Фото {index + 1}/{total}\n"
        f"ID: `{md_code(post.get('id', 0))}`\n"
        f"Rating: {md_text(post.get('rating', ''))} | Score: {post.get('score', 0)}\n"
        f"Tags: `{md_code(tags)}`"
    )


def build_favorites_gallery_caption(post: dict, index: int, total: int) -> str:
    tags = post.get("tags", "")
    if len(tags) > 120:
        tags = tags[:120] + "..."

    return clamp_caption(
        f"⭐ Избранное\n"
        f"Фото {index + 1}/{total}\n"
        f"ID: `{md_code(post.get('id', 0))}`\n"
        f"Rating: {md_text(post.get('rating', ''))} | Score: {post.get('score', 0)}\n"
        f"Tags: `{md_code(tags)}`"
    )


async def build_caption(
    settings: dict, result: dict, query: str = "", is_subscription: bool = False
) -> str:
    caption_parts = []

    if is_subscription and settings.get("show_subscription_label", True):
        caption_parts.append("🔔 *Автоматическая рассылка*")

    if query and settings.get("show_search_query", True):
        caption_parts.append(f"Запрос: `{md_code(query)}`")

    if settings.get("show_id", True):
        caption_parts.append(f"🆔 ID: `{md_code(result.get('id', 0))}`")

    if settings.get("show_score", True):
        caption_parts.append(f"📊 Score: {result.get('score', 0)}")

    if settings.get("show_rating", True):
        caption_parts.append(
            f"🏷 Rating: {md_text(result.get('rating', 'unknown'))}")

    if settings.get("show_tags", True):
        post_tags = result.get("tags", "")
        if len(post_tags) > 150:
            post_tags = post_tags[:150] + "..."
        caption_parts.append(f"🔖 Tags: `{md_code(post_tags)}`")

    if not caption_parts:
        return ""

    if len(caption_parts) == 1:
        return clamp_caption(caption_parts[0])
    if len(caption_parts) == 2:
        return clamp_caption(f"{caption_parts[0]}\n{caption_parts[1]}")
    return clamp_caption(f"{caption_parts[0]}\n" + "\n".join(caption_parts[1:]))


def build_full_tags_messages(post: dict) -> list[str]:
    post_id = post.get("id", 0)
    tags = [tag for tag in str(post.get("tags", "")).split() if tag]
    if not tags:
        return [f"🏷 У поста `{md_code(post_id)}` нет сохранённых тегов."]

    header = f"🏷 *Все теги поста* `{md_code(post_id)}`:\n\n"
    messages: list[str] = []
    current = header
    for tag in tags:
        line = f"• `{md_code(tag)}`\n"
        if len(current) + len(line) > 3900:
            messages.append(current.rstrip())
            current = line
        else:
            current += line

    if current.strip():
        messages.append(current.rstrip())
    return messages
