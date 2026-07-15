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
ALLOWED_USER_IDS = _get_int_set_env("ALLOWED_USER_IDS") | ADMIN_USER_IDS
ALLOWED_CHAT_IDS = _get_int_set_env("ALLOWED_CHAT_IDS")
ALLOW_GROUP_CHATS = _get_bool_env("ALLOW_GROUP_CHATS", False)
TAG_TRANSLATION_ENABLED = _get_bool_env("TAG_TRANSLATION_ENABLED", True)

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
