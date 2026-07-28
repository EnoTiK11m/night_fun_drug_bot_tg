import os
from dotenv import load_dotenv

load_dotenv()

_CONFIG_ERRORS: list[str] = []


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name} must be an integer")
        return default


def _get_int_set_env(name: str) -> set[int]:
    raw_value = os.getenv(name, "")
    values: set[int] = set()
    for item in raw_value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            _CONFIG_ERRORS.append(f"{name} must contain only integer IDs")
            break
    return values


def _get_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    _CONFIG_ERRORS.append(f"{name} must be a boolean")
    return default


BOT_TOKEN = os.getenv("BOT_TOKEN")

# API настройки
API_BASE_URL = "https://api.rule34.xxx/index.php"
AUTOCOMPLETE_URL = "https://api.rule34.xxx/autocomplete.php"

# API credentials are required in .env
API_USER_ID = os.getenv("API_USER_ID")
API_KEY = os.getenv("API_KEY")
SEARCH_COOLDOWN_SECONDS = _get_int_env("SEARCH_COOLDOWN_SECONDS", 3)
SUBSCRIPTION_CHECK_INTERVAL_SECONDS = max(
    30, _get_int_env("SUBSCRIPTION_CHECK_INTERVAL_SECONDS", 120)
)
SUBSCRIPTION_MAX_POSTS_PER_USER_PASS = max(
    1, min(45, _get_int_env("SUBSCRIPTION_MAX_POSTS_PER_USER_PASS", 45))
)
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
ADMIN_USER_IDS = _get_int_set_env("ADMIN_USER_IDS")
ALLOWED_USER_IDS = _get_int_set_env("ALLOWED_USER_IDS")
ALLOWED_CHAT_IDS = _get_int_set_env("ALLOWED_CHAT_IDS")
ALLOW_GROUP_CHATS = _get_bool_env("ALLOW_GROUP_CHATS", False)
TAG_TRANSLATION_ENABLED = _get_bool_env("TAG_TRANSLATION_ENABLED", True)
GIT_UPDATE_REMOTE = os.getenv("GIT_UPDATE_REMOTE", "origin").strip() or "origin"
GIT_UPDATE_BRANCH = os.getenv("GIT_UPDATE_BRANCH", "main").strip() or "main"
GIT_UPDATE_COMMAND_TIMEOUT_SECONDS = max(
    5, _get_int_env("GIT_UPDATE_COMMAND_TIMEOUT_SECONDS", 60)
)
SUBSCRIPTION_MAX_TOTAL = max(1, min(100, _get_int_env("SUBSCRIPTION_MAX_TOTAL", 20)))
SUBSCRIPTION_MAX_ACTIVE = min(
    SUBSCRIPTION_MAX_TOTAL,
    max(1, _get_int_env("SUBSCRIPTION_MAX_ACTIVE", 10)),
)
SUBSCRIPTION_QUERY_MAX_LENGTH = max(
    16, min(2000, _get_int_env("SUBSCRIPTION_QUERY_MAX_LENGTH", 256))
)
SUBSCRIPTION_QUERY_MAX_TAGS = max(
    1, min(100, _get_int_env("SUBSCRIPTION_QUERY_MAX_TAGS", 20))
)
SUBSCRIPTION_CREATE_COOLDOWN_SECONDS = max(
    0, min(3600, _get_int_env("SUBSCRIPTION_CREATE_COOLDOWN_SECONDS", 30))
)
SUBSCRIPTION_CACHE_MAX_PER_QUERY = max(
    20, min(5000, _get_int_env("SUBSCRIPTION_CACHE_MAX_PER_QUERY", 250))
)
SUBSCRIPTION_CACHE_MAX_ROWS = max(
    SUBSCRIPTION_CACHE_MAX_PER_QUERY,
    min(1_000_000, _get_int_env("SUBSCRIPTION_CACHE_MAX_ROWS", 100_000)),
)
SUBSCRIPTION_CACHE_CLEANUP_BATCH_SIZE = max(
    10, min(10_000, _get_int_env("SUBSCRIPTION_CACHE_CLEANUP_BATCH_SIZE", 500))
)
SUBSCRIPTION_CACHE_CLEANUP_INTERVAL_SECONDS = max(
    60, min(86_400, _get_int_env("SUBSCRIPTION_CACHE_CLEANUP_INTERVAL_SECONDS", 900))
)
POST_CACHE_TTL_HOURS = max(
    1, min(8760, _get_int_env("POST_CACHE_TTL_HOURS", 168))
)
POST_CACHE_MAX_ROWS = max(
    1000, min(2_000_000, _get_int_env("POST_CACHE_MAX_ROWS", 100_000))
)
USER_STATE_TTL_MINUTES = max(
    5, min(1440, _get_int_env("USER_STATE_TTL_MINUTES", 30))
)
USER_STATE_CLEANUP_INTERVAL_SECONDS = max(
    30, min(86_400, _get_int_env("USER_STATE_CLEANUP_INTERVAL_SECONDS", 300))
)
GLOBAL_DOWNLOAD_CONCURRENCY = max(
    1, min(32, _get_int_env("GLOBAL_DOWNLOAD_CONCURRENCY", 4))
)
ZIP_EXPORT_WORKERS = max(1, min(2, _get_int_env("ZIP_EXPORT_WORKERS", 1)))
ZIP_EXPORT_QUEUE_SIZE = max(1, min(100, _get_int_env("ZIP_EXPORT_QUEUE_SIZE", 8)))
ZIP_EXPORT_TIMEOUT_SECONDS = max(
    30, min(3600, _get_int_env("ZIP_EXPORT_TIMEOUT_SECONDS", 600))
)
ZIP_EXPORT_MAX_FILES = max(1, min(1000, _get_int_env("ZIP_EXPORT_MAX_FILES", 120)))
ZIP_EXPORT_MAX_FILE_BYTES = max(
    1024 * 1024,
    min(45 * 1024 * 1024, _get_int_env("ZIP_EXPORT_MAX_FILE_BYTES", 20 * 1024 * 1024)),
)
ZIP_EXPORT_MAX_TOTAL_BYTES = max(
    ZIP_EXPORT_MAX_FILE_BYTES,
    min(2 * 1024 * 1024 * 1024, _get_int_env("ZIP_EXPORT_MAX_TOTAL_BYTES", 200 * 1024 * 1024)),
)
ZIP_EXPORT_PART_BYTES = max(
    5 * 1024 * 1024,
    min(45 * 1024 * 1024, _get_int_env("ZIP_EXPORT_PART_BYTES", 45 * 1024 * 1024)),
)
ZIP_EXPORT_MAX_PARTS = max(1, min(20, _get_int_env("ZIP_EXPORT_MAX_PARTS", 5)))
ZIP_EXPORT_MAX_TEMP_BYTES = max(
    ZIP_EXPORT_PART_BYTES + ZIP_EXPORT_MAX_FILE_BYTES,
    min(512 * 1024 * 1024, _get_int_env("ZIP_EXPORT_MAX_TEMP_BYTES", 96 * 1024 * 1024)),
)
ZIP_EXPORT_PROGRESS_INTERVAL_SECONDS = max(
    1, min(30, _get_int_env("ZIP_EXPORT_PROGRESS_INTERVAL_SECONDS", 3))
)
GIT_UPDATE_PIP_TIMEOUT_SECONDS = max(
    30, _get_int_env("GIT_UPDATE_PIP_TIMEOUT_SECONDS", 300)
)

# Temporarily simplified blacklist for testing
DEFAULT_BLACKLIST = {
    "none",

}

# Лимиты
MAX_POSTS_PER_REQUEST = 1000
DEFAULT_LIMIT = 1000


def validate_config() -> list[str]:
    """Return names of required environment variables that are not set."""
    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "API_USER_ID": API_USER_ID,
        "API_KEY": API_KEY,
    }
    return [name for name, value in required.items() if not value] + _CONFIG_ERRORS
