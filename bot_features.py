import random
import time
from collections import Counter, deque
from typing import Any, Iterable
from urllib.parse import urlparse


GALLERY_SORTS = {"random", "new", "popular"}
RATING_FILTERS = {"all", "s", "q", "e"}
MEDIA_TYPES = {"all", "images", "animations", "videos"}
ORIENTATIONS = {"any", "portrait", "landscape", "square"}
QUALITY_MODES = {"auto", "preview", "sample", "original"}
MEDIA_GROUP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".mp4")


def media_kind(post: dict) -> str:
    url = post.get("file_url") or post.get("sample_url") or post.get("preview_url") or ""
    path = urlparse(url).path.lower()
    if path.endswith((".mp4", ".webm")):
        return "videos"
    if path.endswith(".gif"):
        return "animations"
    return "images"


def post_matches_preferences(post: dict, settings: dict) -> bool:
    rating_filter = settings.get("rating_filter", "all")
    if rating_filter in RATING_FILTERS - {"all"} and post.get("rating") != rating_filter:
        return False

    wanted_type = settings.get("media_type", "all")
    if wanted_type in MEDIA_TYPES - {"all"} and media_kind(post) != wanted_type:
        return False

    try:
        width = int(post.get("width") or 0)
        height = int(post.get("height") or 0)
        min_width = max(0, int(settings.get("min_width") or 0))
        min_height = max(0, int(settings.get("min_height") or 0))
    except (TypeError, ValueError):
        return False
    if width and width < min_width or height and height < min_height:
        return False
    if (min_width and not width) or (min_height and not height):
        return False

    orientation = settings.get("orientation", "any")
    if orientation != "any" and width and height:
        if orientation == "portrait" and width >= height:
            return False
        if orientation == "landscape" and width <= height:
            return False
        if orientation == "square" and abs(width - height) > max(width, height) * 0.1:
            return False
    return True


def filter_and_sort_posts(
    posts: Iterable[dict], settings: dict, excluded_post_ids: set[int] | None = None
) -> list[dict]:
    excluded_post_ids = excluded_post_ids or set()
    filtered = []
    for post in posts:
        try:
            post_id = int(post.get("id"))
        except (TypeError, ValueError):
            continue
        if post_id in excluded_post_ids or not post.get("file_url"):
            continue
        if post_matches_preferences(post, settings):
            filtered.append(post)

    sort_mode = settings.get("gallery_sort", "random")
    if sort_mode == "new":
        filtered.sort(key=lambda post: int(post.get("id") or 0), reverse=True)
    elif sort_mode == "popular":
        filtered.sort(key=lambda post: int(post.get("score") or 0), reverse=True)
    else:
        random.shuffle(filtered)
    return filtered


def prepare_post_quality(post: dict, settings: dict) -> dict:
    prepared = dict(post)
    original = post.get("file_url") or ""
    sample = post.get("sample_url") or ""
    preview = post.get("preview_url") or ""
    mode = settings.get("quality_mode", "auto")
    if mode == "preview":
        order = (preview, sample, original)
    elif mode == "sample":
        order = (sample, preview, original)
    elif mode == "original":
        order = (original, sample, preview)
    else:
        try:
            file_size = int(post.get("file_size") or 0)
            max_bytes = max(1, int(settings.get("max_file_mb") or 10)) * 1024 * 1024
        except (TypeError, ValueError):
            file_size, max_bytes = 0, 10 * 1024 * 1024
        order = (sample, preview, original) if file_size and file_size > max_bytes else (
            original,
            sample,
            preview,
        )
    unique = []
    for url in order:
        if url and url not in unique:
            unique.append(url)
    prepared["file_url"] = unique[0] if unique else ""
    prepared["sample_url"] = unique[1] if len(unique) > 1 else ""
    prepared["preview_url"] = unique[2] if len(unique) > 2 else ""
    return prepared


def media_group_compatible_url(url: str) -> bool:
    return urlparse(url or "").path.lower().endswith(MEDIA_GROUP_EXTENSIONS)


def prepare_gallery_album_posts(
    posts: Iterable[dict], settings: dict, limit: int
) -> list[dict]:
    """Prepare up to ``limit`` posts that Telegram can place in one media group.

    Telegram does not support animations in media groups. For GIF posts we prefer
    a static sample/preview when the upstream API provides one, then continue
    scanning so one GIF does not force the whole gallery into sequential sends.
    """
    prepared_posts = []
    for post in posts:
        prepared = prepare_post_quality(post, settings)
        if not media_group_compatible_url(prepared.get("file_url", "")):
            fallback = next(
                (
                    post.get(key, "")
                    for key in ("sample_url", "preview_url")
                    if media_group_compatible_url(post.get(key, ""))
                ),
                "",
            )
            if not fallback:
                continue
            prepared["file_url"] = fallback
        prepared_posts.append(prepared)
        if len(prepared_posts) >= limit:
            break
    return prepared_posts


def normalize_feature_settings(settings: dict) -> dict:
    result = dict(settings)
    if result.get("gallery_sort") not in GALLERY_SORTS:
        result["gallery_sort"] = "random"
    if result.get("rating_filter") not in RATING_FILTERS:
        result["rating_filter"] = "all"
    if result.get("media_type") not in MEDIA_TYPES:
        result["media_type"] = "all"
    if result.get("orientation") not in ORIENTATIONS:
        result["orientation"] = "any"
    if result.get("quality_mode") not in QUALITY_MODES:
        result["quality_mode"] = "auto"
    for key, default, low, high in (
        ("gallery_size", 10, 2, 10),
        ("min_width", 0, 0, 10000),
        ("min_height", 0, 0, 10000),
        ("max_file_mb", 10, 1, 50),
    ):
        try:
            result[key] = max(low, min(int(result.get(key, default)), high))
        except (TypeError, ValueError):
            result[key] = default
    return result


class RuntimeMetrics:
    def __init__(self):
        self.started_at = time.monotonic()
        self.counters: Counter[str] = Counter()
        self.recent_failures: deque[tuple[float, str]] = deque(maxlen=50)

    def increment(self, name: str, count: int = 1):
        self.counters[name] += count

    def failure(self, message: str):
        self.counters["delivery_failed"] += 1
        self.recent_failures.append((time.time(), message[:300]))

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "counters": dict(self.counters),
            "recent_failures": list(self.recent_failures),
        }


runtime_metrics = RuntimeMetrics()
