import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# API настройки
API_BASE_URL = "https://api.rule34.xxx/index.php"
AUTOCOMPLETE_URL = "https://api.rule34.xxx/autocomplete.php"

# API credentials are required in .env
API_USER_ID = os.getenv("API_USER_ID")
API_KEY = os.getenv("API_KEY")
SEARCH_COOLDOWN_SECONDS = int(os.getenv("SEARCH_COOLDOWN_SECONDS", "3"))

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
    return [name for name, value in required.items() if not value]
