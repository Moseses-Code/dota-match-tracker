import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STRATZ_TOKEN = os.getenv("STRATZ_TOKEN", "")          # необязательно, но нужен для средних по рангу
OPENDOTA_API_KEY = os.getenv("OPENDOTA_API_KEY", "")  # необязательно
# Telegram ID владельца бота: ему принадлежат паки эмодзи героев/предметов/рангов
OWNER_ID = int(os.getenv("OWNER_ID") or 0)

# Иконки на кнопках (премиум-эмодзи); 0 — выключить
BUTTON_ICONS = os.getenv("BUTTON_ICONS", "1") != "0"

# Как часто проверять новые матчи (секунды)
POLL_ACTIVE = int(os.getenv("POLL_ACTIVE", "180"))   # игрок недавно играл
POLL_IDLE = int(os.getenv("POLL_IDLE", "600"))       # давно не играл
# Сколько ждать парсинга матча, прежде чем прислать отчёт без него (минуты)
PARSE_WAIT_MIN = int(os.getenv("PARSE_WAIT_MIN", "25"))
# Примерный шаг MMR за рейтинговую игру
MMR_STEP = int(os.getenv("MMR_STEP", "25"))

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "bot.db"
CACHE_DIR = DATA_DIR / "cache"
EMOJI_FILE = BASE_DIR / "emoji.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
