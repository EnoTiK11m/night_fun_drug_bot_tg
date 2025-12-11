import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# API настройки
API_BASE_URL = "https://api.rule34.xxx/index.php"
AUTOCOMPLETE_URL = "https://api.rule34.xxx/autocomplete.php"

# Твои реальные данные из .env
API_USER_ID = os.getenv("API_USER_ID", "4338674")
# Убедись что здесь твой ключ
API_KEY = os.getenv(
    "API_KEY", "1939cace859780e7e4efe73c2f2af279c0ca9dc996225ab3af91d5ec9d31f3830cbd77da93dd8d49db4b3473730aac7f6d10e46ed2f7fed966a2d7dab785a707")

# Временно упрости blacklist для тестирования
DEFAULT_BLACKLIST = {
    "none",

}

# Лимиты
MAX_POSTS_PER_REQUEST = 1000
DEFAULT_LIMIT = 1000
