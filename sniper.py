import os
import re
import sys
import json
import time
import random
import html
import sqlite3
import signal
import atexit
import datetime
import statistics
import requests

try:
    from rapidfuzz import process as fuzz_process, fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ================= БАЗОВЫЕ НАСТРОЙКИ И СЕКРЕТЫ =================
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "1091251853").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
MAX_RUN_HOURS = float(os.getenv("MAX_RUN_HOURS", "5.8"))

DB_FILE = "sniper.db"
SETTINGS_FILE = "settings.json"

MIN_INTERVAL = 7
MAX_INTERVAL = 12
TG_POLL_INTERVAL = 1.0       # Кнопки в ТГ опрашиваются каждую 1 секунду
MARKET_CACHE_TTL = 4 * 3600  # Кэш рынка живет 4 часа
MIN_VALID_SAMPLE = 8         # Минимум объявлений для признания модели ликвидной

MINSK_DISTRICTS = [
    "Фрунзенский", "Московский", "Первомайский", "Советский",
    "Центральный", "Заводской", "Ленинский", "Октябрьский", "Партизанский"
]

DEFAULT_SETTINGS = {
    "min_price": 120,
    "max_price": 1500,
    "bargain_factor": 1.00,       # Коэффициент к очищенной взвешенной медиане
    "districts": [],              # Пустой список = весь Минск
    "anchors": {},                # Ручные якоря цен: {"iPhone 13_128GB": 650}
    "fat_profit_byn": 85,         # Выгода для ЖИРА (со звуком)
    "fat_profit_pct": 22,         # % выгоды для ЖИРА (со звуком)
    "urgent_profit_byn": 55,      # Выгода при триггере срочности
    "work_profit_byn": 40,        # Рабочий вариант (без звука)
    "inspect_min_profit": 25,     # Под осмотр (без звука)
    "last_digest_date": ""
}

# Глобальное состояние ожидания ввода от пользователя в Telegram
USER_WAIT_STATE = {"mode": None}

# ================= 1. РАСШИРЕННЫЙ БЕЛЫЙ СПИСОК МОДЕЛЕЙ =================
# Универсальный разделитель (пробел, дефис, точка, подчеркивание или слитно)
SEP = r"[\s\-_.]*"
NO_SUFFIX = rf"(?!{SEP}(?:pro|про|max|макс|mini|мини|plus|плюс|\+|ultra|ультра|fe|фе|lite|лайт)\b)"

MODELS_WHITELIST = [
    # --- APPLE iPHONE (от XR до 15 Pro Max) ---
    ("iPhone 15 Pro Max", "apple", rf"\b15{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [256, 512, 1024], "iPhone 15 Pro Max"),
    ("iPhone 15 Pro", "apple", rf"\b15{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 15 Pro"),
    ("iPhone 15 Plus", "apple", rf"\b15{SEP}(?:\+|plus|плюс)\b", [128, 256, 512], "iPhone 15 Plus"),
    ("iPhone 15", "apple", rf"(?:(?:iphone|айфон|айф){SEP}15\b|\b15{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 15"),

    ("iPhone 14 Pro Max", "apple", rf"\b14{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512, 1024], "iPhone 14 Pro Max"),
    ("iPhone 14 Pro", "apple", rf"\b14{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 14 Pro"),
    ("iPhone 14 Plus", "apple", rf"\b14{SEP}(?:\+|plus|плюс)\b", [128, 256, 512], "iPhone 14 Plus"),
    ("iPhone 14", "apple", rf"(?:(?:iphone|айфон|айф){SEP}14\b|\b14{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 14"),

    ("iPhone 13 Pro Max", "apple", rf"\b13{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512, 1024], "iPhone 13 Pro Max"),
    ("iPhone 13 Pro", "apple", rf"\b13{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 13 Pro"),
    ("iPhone 13 mini", "apple", rf"\b13{SEP}(?:mini|мини)\b", [128, 256, 512], "iPhone 13 mini"),
    ("iPhone 13", "apple", rf"(?:(?:iphone|айфон|айф){SEP}13\b|\b13{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 13"),

    ("iPhone 12 Pro Max", "apple", rf"\b12{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512], "iPhone 12 Pro Max"),
    ("iPhone 12 Pro", "apple", rf"\b12{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512], "iPhone 12 Pro"),
    ("iPhone 12 mini", "apple", rf"\b12{SEP}(?:mini|мини)\b", [64, 128, 256], "iPhone 12 mini"),
    ("iPhone 12", "apple", rf"(?:(?:iphone|айфон|айф){SEP}12\b|\b12{SEP}(?:айфон|iphone)\b|\bдвенадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 12"),

    ("iPhone 11 Pro Max", "apple", rf"\b11{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [64, 256, 512], "iPhone 11 Pro Max"),
    ("iPhone 11 Pro", "apple", rf"\b11{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [64, 256, 512], "iPhone 11 Pro"),
    ("iPhone 11", "apple", rf"(?:(?:iphone|айфон|айф){SEP}11\b|\b11{SEP}(?:айфон|iphone)\b|\bодиннадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 11"),

    ("iPhone XS Max", "apple", rf"\b(?:xs|хс|икс{SEP}эс){SEP}(?:max|макс)\b", [64, 256, 512], "iPhone XS Max"),
    ("iPhone XS", "apple", rf"\b(?:xs|хс|икс{SEP}эс)\b(?!{SEP}(?:max|макс))", [64, 256, 512], "iPhone XS"),
    ("iPhone XR", "apple", rf"\b(?:xr|хр|икс{SEP}эр|хr)\b", [64, 128, 256], "iPhone XR"),
    ("iPhone SE 2022", "apple", rf"\bse{SEP}(?:2022|3|22)\b", [64, 128, 256], "iPhone SE 2022"),
    ("iPhone SE 2020", "apple", rf"\bse{SEP}(?:2020|2|20)\b", [64, 128, 256], "iPhone SE 2020"),

    # --- SAMSUNG ---
    ("Samsung S24 Ultra", "android", rf"\bs{SEP}24{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S24 Ultra"),
    ("Samsung S24 Plus", "android", rf"\bs{SEP}24{SEP}(?:\+|plus|плюс)\b", [256, 512], "Samsung S24 Plus"),
    ("Samsung S24", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}24\b{NO_SUFFIX}", [128, 256], "Samsung S24"),

    ("Samsung S23 Ultra", "android", rf"\bs{SEP}23{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S23 Ultra"),
    ("Samsung S23 Plus", "android", rf"\bs{SEP}23{SEP}(?:\+|plus|плюс)\b", [256, 512], "Samsung S23 Plus"),
    ("Samsung S23 FE", "android", rf"\bs{SEP}23{SEP}(?:fe|фе)\b", [128, 256], "Samsung S23 FE"),
    ("Samsung S23", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}23\b{NO_SUFFIX}", [128, 256], "Samsung S23"),

    ("Samsung S22 Ultra", "android", rf"\bs{SEP}22{SEP}(?:ultra|ультра)\b", [128, 256, 512], "Samsung S22 Ultra"),
    ("Samsung S22 Plus", "android", rf"\bs{SEP}22{SEP}(?:\+|plus|плюс)\b", [128, 256], "Samsung S22 Plus"),
    ("Samsung S22", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}22\b{NO_SUFFIX}", [128, 256], "Samsung S22"),

    ("Samsung S21 FE", "android", rf"\bs{SEP}21{SEP}(?:fe|фе)\b", [128, 256], "Samsung S21 FE"),
    ("Samsung S21 Plus", "android", rf"\bs{SEP}21{SEP}(?:\+|plus|плюс)\b", [128, 256], "Samsung S21 Plus"),
    ("Samsung S21", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}21\b{NO_SUFFIX}", [128, 256], "Samsung S21"),
    ("Samsung S20 FE", "android", rf"\bs{SEP}20{SEP}(?:fe|фе)\b", [128, 256], "Samsung S20 FE"),

    ("Samsung A55", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}55\b", [128, 256], "Samsung A55"),
    ("Samsung A54", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}54\b", [128, 256], "Samsung A54"),
    ("Samsung A53", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}53\b", [128, 256], "Samsung A53"),
    ("Samsung A52", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}52\b", [128, 256], "Samsung A52"),
    ("Samsung A35", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}35\b", [128, 256], "Samsung A35"),
    ("Samsung A34", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}34\b", [128, 256], "Samsung A34"),

    # --- XIAOMI / REDMI / POCO ---
    ("Redmi Note 13 Pro Plus", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про){SEP}(?:\+|plus|плюс)\b", [256, 512], "Redmi Note 13 Pro Plus"),
    ("Redmi Note 13 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про)\b(?!{SEP}(?:\+|plus|плюс))", [128, 256, 512], "Redmi Note 13 Pro"),
    ("Redmi Note 13", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13\b{NO_SUFFIX}", [128, 256], "Redmi Note 13"),
    ("Redmi Note 12 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}12{SEP}(?:pro|про)\b(?!{SEP}(?:\+|plus|плюс))", [128, 256], "Redmi Note 12 Pro"),
    ("Redmi Note 12", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}12\b{NO_SUFFIX}", [64, 128, 256], "Redmi Note 12"),
    ("Redmi Note 11 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}11{SEP}(?:pro|про)\b", [64, 128], "Redmi Note 11 Pro"),

    ("Poco F6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco F6 Pro"),
    ("Poco F6", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco F6"),
    ("Poco F5", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}5\b{NO_SUFFIX}", [256], "Poco F5"),
    ("Poco F4", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}4\b{NO_SUFFIX}", [128, 256], "Poco F4"),
    ("Poco X6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco X6 Pro"),
    ("Poco X6", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco X6"),
    ("Poco X5 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}5{SEP}(?:pro|про)\b", [128, 256], "Poco X5 Pro"),
    ("Poco X5", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}5\b{NO_SUFFIX}", [128, 256], "Poco X5"),
    ("Poco X4 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}4{SEP}(?:pro|про)\b", [128, 256], "Poco X4 Pro"),
    ("Poco M6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:m|м){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco M6 Pro"),

    # --- GOOGLE PIXEL ---
    ("Pixel 8 Pro", "android", rf"\b(?:pixel|пиксель){SEP}8{SEP}(?:pro|про)\b", [128, 256, 512], "Pixel 8 Pro"),
    ("Pixel 8a", "android", rf"\b(?:pixel|пиксель){SEP}8{SEP}(?:a|а)\b", [128, 256], "Pixel 8a"),
    ("Pixel 8", "android", rf"\b(?:pixel|пиксель){SEP}8\b{NO_SUFFIX}", [128, 256], "Pixel 8"),
    ("Pixel 7 Pro", "android", rf"\b(?:pixel|пиксель){SEP}7{SEP}(?:pro|про)\b", [128, 256, 512], "Pixel 7 Pro"),
    ("Pixel 7a", "android", rf"\b(?:pixel|пиксель){SEP}7{SEP}(?:a|а)\b", [128], "Pixel 7a"),
    ("Pixel 7", "android", rf"\b(?:pixel|пиксель){SEP}7\b{NO_SUFFIX}", [128, 256], "Pixel 7"),
    ("Pixel 6a", "android", rf"\b(?:pixel|пиксель){SEP}6{SEP}(?:a|а)\b", [128], "Pixel 6a"),
    ("Pixel 6", "android", rf"\b(?:pixel|пиксель){SEP}6\b{NO_SUFFIX}", [128, 256], "Pixel 6"),
]

ANDROID_BRAND_WORDS = [
    r"\brealme\b", r"\bреалми\b", r"\bredmi\b", r"\bредми\b", r"\bxiaomi\b", r"\bсяоми\b",
    r"\bkсяоми\b", r"\bpoco\b", r"\bпоко\b", r"\bsamsung\b", r"\bсамсунг\b", r"\bhonor\b",
    r"\bхонор\b", r"\bhuawei\b", r"\bхуавей\b", r"\binfinix\b", r"\bинфиникс\b", r"\btecno\b",
    r"\bтекно\b", r"\boppo\b", r"\bvivo\b", r"\boneplus\b", r"\bnote\b", r"\bноут\b"
]

# Безусловный мусор (чехлы, стекла, коробки, аренда)
ACCESSORY_GHOST_PATTERNS = [
    r"^\s*(?:чехол|стекло|коробка|кабель|провод|зарядка|блок|наушники)\b",
    r"\bчехол\s+(?:на|для|от)\b", r"\bстекло\s+(?:на|для)\b", r"\bкоробка\s+от\b",
    r"\bаренда\b", r"\bпрокат\b"
]

# Покупка/поиск в самом начале заголовка
BUY_INTENT_TITLE_START = r"^\s*(?:куплю|ищу|сниму|возьму)\b"

# Поколения iPhone, где 100% АКБ на вторичке практически всегда означает замененную банку
OLD_APPLE_100_BAT_MODELS = {
    "iPhone XR", "iPhone XS", "iPhone XS Max",
    "iPhone SE 2020", "iPhone SE 2022",
    "iPhone 11", "iPhone 11 Pro", "iPhone 11 Pro Max",
    "iPhone 12 mini", "iPhone 12", "iPhone 12 Pro", "iPhone 12 Pro Max",
    "iPhone 13 mini", "iPhone 13", "iPhone 13 Pro", "iPhone 13 Pro Max"
}

# ================= 2. РАЗДЕЛЕННЫЕ МАРКЕРЫ БАНА И ШТРАФОВ =================
# Группа 1: Прямые маркеры (уже содержат "не" или однозначный приговор — банятся напрямую без lookbehind)
DIRECT_DEAD_MARKERS = [
    "под замену", "на запчасти", "не включается", "не работает", "не заряжается",
    "проблема в порте", "проблема с гнездом", "разъем под замену", "гнездо под замену",
    "отходит зарядка", "только через magsafe", "не реагирует на касания",
    "порван шлейф", "проблема со шлейфом", "поплыл экран", "перезагружается сам",
    "не видит сим", "нет сети", "не работает face", "не работает фейс",
    "без face id", "без фейс", "не оплачен", "неоплачен", "ошибка акб",
    "ошибка аккумулятора", "неизвестная деталь", "только обмен", "обмен только"
]

# Группа 2: Маркеры, которые проверяются с защитой от отрицания ("не разбит", "без трещин", "не в кредите")
NEGATABLE_DEAD_MARKERS = [
    "битая крышка", "разбит", "разбита", "разбито", "трещина", "трещины", "треснут",
    "утопленник", "после воды", "пятно на экране", "пятна на экране",
    "полоса на экране", "полосы на экране", "выгорание", "выгорел",
    "материнская плата", "кз", "бутлуп", "донор",
    "рассрочка", "рассрочке", "кредит", "кредите", "лизинг", "лизинге",
    "трейд ин", "trade in"
]

# Группа 3: Маркеры ремонта и неоригинальных деталей (тоже проверяются с защитой от "не / без")
REPAIR_MARKERS = [
    "замена акб", "менялся акб", "менялась акб", "акб менялся", "акб менялась",
    "менялся аккумулятор", "аккумулятор менялся", "новая батарея", "заменена батарея",
    "батарея менялась", "менялся экран", "экран менялся", "замена экрана",
    "дисплей менялся", "менялся дисплей", "копия", "реплика", "муляж",
    "реф", "восстановленный", "mdm", "мдм", "байпас", "bypass"
]

LIGHT_COSMETIC_MARKERS = ["потертости", "потёртости", "микроцарапины", "мелкие царапины", "следы от чехла"]
HEAVY_COSMETIC_MARKERS = ["скол", "сколы", "глубокие царапины", "царапины на экране", "вмятина", "коцки", "требует внимания"]

NLP_TRAP_PATTERNS = [
    r"\bвсе\s+(?:супер|отлично|хорошо|работает|идеально)[\s,]+но\b",
    r"\bвсё\s+(?:супер|отлично|хорошо|работает|идеально)[\s,]+но\b",
    r"\bвсе\s+работает[\s,]+кроме\b", r"\bвсё\s+работает[\s,]+кроме\b",
    r"\bединственный\s+(?:минус|нюанс|косяк)\b", r"\bиз\s+(?:минусов|нюансов|косяков)\b",
    r"\bна\s+работу\s+не\s+влияет\b"
]

URGENT_PATTERNS = [
    r"(?<!не\s)\bсрочно\b", r"\bочень\s+срочно\b", r"\bтолько\s+сегодня\b",
    r"\bдо\s+вечера\b", r"\bдо\s+завтра\b", r"\bбыстрым\s+торг\b",
    r"\bуезжаю\b", r"\bпереезд\b", r"\bнужны\s+деньги\b"
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.kufar.by/"
})
if PROXY_URL:
    session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})


# ================= НАСТРОЙКИ И SQLITE БАЗА ДАННЫХ =================
def load_settings():
    s = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                s.update(data)
        except Exception as e:
            print(f"⚠️ Ошибка чтения {SETTINGS_FILE}: {e}")
    if "anchors" not in s or not isinstance(s["anchors"], dict):
        s["anchors"] = {}
    return s


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения {SETTINGS_FILE}: {e}")


def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lot_history (
            ad_id TEXT PRIMARY KEY,
            account_id TEXT,
            model TEXT,
            memory INTEGER,
            battery_health INTEGER,
            color TEXT,
            district TEXT,
            has_box INTEGER,
            first_seen_price REAL,
            last_seen_price REAL,
            price_drops_count INTEGER DEFAULT 0,
            first_seen_ts INTEGER,
            last_seen_ts INTEGER,
            list_time_ts INTEGER,
            status TEXT DEFAULT 'active',
            days_on_market REAL DEFAULT 0,
            estimated_sold_price REAL,
            notified INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_cache (
            cache_key TEXT PRIMARY KEY,
            market_price REAL,
            min_price REAL,
            min_link TEXT,
            sample_size INTEGER,
            median_days REAL,
            resellers_count INTEGER,
            updated_ts INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            account_id TEXT PRIMARY KEY,
            ads_count INTEGER DEFAULT 1,
            last_model TEXT,
            last_link TEXT,
            last_seen_ts INTEGER
        )
    """)
    conn.commit()
    return conn


db_conn = init_db()


def close_db():
    try:
        if db_conn:
            db_conn.commit()
            db_conn.close()
    except Exception:
        pass


def handle_sigterm(signum, frame):
    """Корректно закрывает БД и завершает процесс при получении SIGTERM от GitHub Actions."""
    close_db()
    sys.exit(0)


atexit.register(close_db)
signal.signal(signal.SIGTERM, handle_sigterm)


def calculate_estimated_sold_price(last_price, days_on_market):
    if days_on_market <= 2:
        return round(last_price * 0.98)
    elif days_on_market <= 7:
        return round(last_price * 0.94)
    elif days_on_market <= 14:
        return round(last_price * 0.90)
    return round(last_price * 0.85)


# ================= ТЕКСТОВЫЕ АНАЛИЗАТОРЫ И ФИЛЬТРЫ =================
def clean_text(text):
    if not text:
        return ""
    t = text.replace("\u00a0", " ").lower().replace("ё", "е")
    t = re.sub(r"\b(?:ifon|iphon|ipone|ipohne|aifon|айфоша)\b", "iphone", t)
    t = re.sub(r"\b(?:sumsung|samsyng|самс)\b", "samsung", t)
    t = re.sub(r"\b(?:ксиоми|ксяоми|сяоми|ксиаоми)\b", "xiaomi", t)
    return t


def has_direct_marker(text, marker):
    """Прямая проверка для маркеров, которые уже содержат отрицание ('не работает', 'под замену')."""
    t = clean_text(text)
    return bool(re.search(rf"\b{re.escape(marker)}", t))


def has_marker_without_negation(text, marker):
    """Проверка маркеров с учетом отрицаний и предлогов ('не разбит', 'не в кредите', 'без сколов')."""
    t = clean_text(text)
    pattern = (
        r"(?<!без\s)(?<!не\s)(?<!не\sв\s)(?<!не\sна\s)"
        r"(?<!нет\s)(?<!никаких\s)(?<!никакого\s)(?<!точно\sне\s)"
        rf"\b{re.escape(marker)}"
    )
    return bool(re.search(pattern, t))


def check_ban_markers(title, description, phone_type="apple", model_id=None):
    t_title = clean_text(title)
    full = clean_text(f"{title} {description}")

    # 1. Аксессуары и аренда
    for pat in ACCESSORY_GHOST_PATTERNS:
        if re.search(pat, full):
            return f"Аксессуар/Аренда: {pat}"

    # 2. Проверка 'куплю / ищу' (баним, если стоит в начале заголовка или если модель не распознана)
    if re.search(BUY_INTENT_TITLE_START, t_title):
        return "Объявление о покупке (Куплю/Ищу в начале заголовка)"
    if not model_id and re.search(r"\b(?:куплю|ищу)\b", t_title):
        return "Объявление о покупке (Куплю/Ищу)"

    # 3. Прямые маркеры трупа (без lookbehind на 'не')
    for m in DIRECT_DEAD_MARKERS:
        if has_direct_marker(full, m):
            return f"Прямой маркер брака: '{m}'"

    # 4. Маркеры трупа с проверкой отрицаний ('не разбит', 'не в рассрочке')
    for m in NEGATABLE_DEAD_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер брака/кредита: '{m}'"

    # 5. Маркеры ремонта (с проверкой отрицаний 'экран не менялся', 'не реф')
    for m in REPAIR_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер ремонта: '{m}'"

    # 6. Умная проверка iCloud / блокировок
    if re.search(r"\b(?:icloud|айклауд|айклауде)\b", full):
        safe_icloud = re.search(
            r"\b(?:чист\w*|отвязан\w*|отвяжу|выйду|выключен\w*|свобод\w*|мой|личный)\b.{0,25}\b(?:icloud|айклауд)"
            r"|\b(?:icloud|айклауд\w*)\b.{0,25}\b(?:чист\w*|отвязан\w*|отвяжу|выйду|выключен\w*|свобод\w*|нет|без)\b",
            full
        )
        if not safe_icloud:
            return "Подозрение на блокировку iCloud (нет слов 'чистый/отвязан/выйду')"

    if has_marker_without_negation(full, "заблокирован") or has_marker_without_negation(full, "на пароле"):
        return "Заблокирован / На пароле"

    # 7. Фильтр 100% АКБ только для старых поколений iPhone (XR - 13, SE)
    if phone_type == "apple" and (model_id is None or model_id in OLD_APPLE_100_BAT_MODELS):
        if re.search(r"\b(?:акб|батаре[яиюей]|емкость|состояние\s*акб)[\s:=-]*100\s*%", full):
            return f"Подозрительные 100% АКБ на {model_id or 'б/у iPhone'} (вероятна замена банки)"

    return None


def extract_battery_health(text):
    t = clean_text(text)
    # 1. Ищем 'акб / батарея / состояние акб' -> X%
    m = re.search(r"(?:акб|батаре[яиюей]|емкость|состояние\s*(?:акб|батаре[яиюей]))[^\d]{0,15}(\d{2,3})\s*%", t)
    if not m:
        # 2. Ищем X% -> 'акб / батарея / родной' (привязка к слову обязательна!)
        m = re.search(r"\b(6\d|7\d|8\d|9\d|100)\s*%\s*(?:акб|батаре[яиюей]|емкост\w*|родн\w*)", t)
    if m:
        val = int(m.group(1))
        if 50 <= val <= 100:
            return val
    return None


def extract_color(text):
    t = clean_text(text)
    discount_colors = ["красный", "red", "product red", "розовый", "pink", "желтый", "yellow", "коралловый"]
    for c in discount_colors:
        if re.search(rf"\b{c}\b", t):
            return c, "discount"
    premium_colors = ["черный", "black", "midnight", "белый", "white", "starlight", "графит", "graphite", "синий", "blue", "фиолетовый", "purple", "зеленый", "green", "титан", "titanium"]
    for c in premium_colors:
        if re.search(rf"\b{c}\b", t):
            return c, "standard"
    return "не указан", "standard"


def check_missing_box(text):
    t = clean_text(text)
    no_box_patterns = [
        r"\bнет\s+коробк[иа]\b", r"\bбез\s+коробк[иа]\b",
        r"\bкоробк[аи]\s+не\s+сохранил\w*\b", r"\bкоробк[аи]\s+утерян\w*\b",
        r"\bтолько\s+телефон\b", r"\bв\s+комплекте\s+только\s+(?:телефон|провод|зарядка)\b"
    ]
    for p in no_box_patterns:
        if re.search(p, t):
            return False
    return True


def extract_memory(text, ad_params, valid_memories):
    sorted_mems = sorted(valid_memories, reverse=True)

    # 1. Сначала смотрим параметры объявления Куфара (перебор по убыванию)
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("phablet_phones_memory", "mobile_phones_memory", "memory"):
                val_str = str(p.get("vl") or p.get("v") or "")
                if "1 тб" in val_str.lower() or "1 tb" in val_str.lower() or "1024" in val_str:
                    if 1024 in valid_memories:
                        return 1024, False
                for mem in sorted_mems:
                    if re.search(rf"\b{mem}\b", val_str):
                        return mem, False

    t = clean_text(text)

    # Проверка на 1 ТБ / 1024 ГБ
    if 1024 in valid_memories and re.search(r"\b(?:1024|1\s*(?:tb|тб|терабайт))\b", t):
        return 1024, False

    # 2. Формат 8/256
    slash_match = re.search(r"\b(?:4|6|8|12|16)\s*/\s*(64|128|256|512|1024)\b", t)
    if slash_match:
        mem = int(slash_match.group(1))
        if mem in valid_memories:
            return mem, False

    # 3. Явное указание с единицами измерения (перебор по убыванию: 'бывают на 64, но у меня на 256 гб')
    for mem in sorted_mems:
        if re.search(rf"\b{mem}\s*(?:гб|gb|гиг|г\b|g\b)|\bна\s*{mem}\b", t):
            return mem, False

    # 4. Голое число памяти (с защитой от 'дом 64', 'кв 128', 'зарядка 64W', '64$', '64 дня')
    for mem in sorted_mems:
        pattern = rf"(?<!дом\s)(?<!кв\s)(?<!квартира\s)\b{mem}\b(?!\s*(?:р\b|руб|byn|дол|уе|\$|%|вт|w\b|дн))"
        if re.search(pattern, t):
            return mem, False

    return valid_memories[0], True


def resolve_overlapping_models(found_list):
    """
    Если нашлось несколько моделей одной линейки, где одна является подмножеством другой
    (например, 'iPhone 13 Pro' и 'iPhone 13' из-за 'чехол от iPhone 13'), оставляет старшую/специфичную.
    """
    if len(found_list) <= 1:
        return found_list
    unique_map = {}
    for item in found_list:
        unique_map[item[0]] = item
    items = list(unique_map.values())
    if len(items) == 1:
        return items

    filtered = []
    for item in items:
        m_name = item[0]
        # Если название этой модели целиком входит в более длинное название другой найденной модели — отбрасываем короткую
        if any(other[0] != m_name and m_name in other[0] for other in items):
            continue
        filtered.append(item)
    return filtered if filtered else items


def strip_secondary_device_mentions(text):
    """Вырезает фразы вроде 'купил iPhone 15 Pro', 'перешел на ...', 'обмен на ...', 'чехол от ...' при поиске модели в описании."""
    pattern = (
        r"\b(?:купил\w*|переш(?:ел|ла|ли)\s+на|обмен\s+на|обменяю\s+на|подарили|взял\s+себе|чехол\s+от)\s+"
        r"(?:iphone|айфон|samsung|самсунг|redmi|редми|poco|поко|pixel|пиксель)?[\s\-_.]*\d{1,2}(?:[\s\-_.]*(?:pro|про|max|макс|mini|мини|plus|плюс|ultra|ультра|fe|фе))*"
    )
    return re.sub(pattern, " ", text)


def normalize_phone(title, description="", ad_params=None):
    t_title = clean_text(title)
    t_full = clean_text(f"{title} {description}")

    param_brand = ""
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("brand", "mobile_phones_brand", "phablet_phones_brand"):
                param_brand = str(p.get("vl") or p.get("v") or "").lower()

    has_android_in_title = any(re.search(w, t_title) for w in ANDROID_BRAND_WORDS)
    has_explicit_iphone = bool(re.search(r"\b(?:iphone|айфон|айф|apple|эпл|ios)\b", t_full)) or ("apple" in param_brand)

    # Убираем упоминания 'чехол от ... / обмен на ...' из копии заголовка перед поиском модели
    temp_title = strip_secondary_device_mentions(t_title)
    found_in_title = []

    for model_id, m_type, regex, mem_list, query_str in MODELS_WHITELIST:
        if m_type == "apple":
            if has_android_in_title:
                continue
            if not has_explicit_iphone:
                continue

        if re.search(regex, temp_title):
            found_in_title.append((model_id, m_type, mem_list, query_str))
            temp_title = re.sub(regex, " ", temp_title)

    found_in_title = resolve_overlapping_models(found_in_title)

    if len(found_in_title) > 1:
        return None, f"Мульти-лот в заголовке: {', '.join(x[0] for x in found_in_title)}"

    if len(found_in_title) == 1:
        model_id, m_type, mem_list, query_str = found_in_title[0]
    else:
        found_in_body = []
        temp_body = strip_secondary_device_mentions(t_full)
        for m_id, m_t, regex, m_l, q_s in MODELS_WHITELIST:
            if m_t == "apple" and (has_android_in_title or not has_explicit_iphone):
                continue
            if re.search(regex, temp_body):
                found_in_body.append((m_id, m_t, m_l, q_s))
                temp_body = re.sub(regex, " ", temp_body)

        found_in_body = resolve_overlapping_models(found_in_body)

        if len(found_in_body) == 1:
            model_id, m_type, mem_list, query_str = found_in_body[0]
        elif len(found_in_body) > 1:
            # Берем первую упомянутую модель, если продавец сначала описал свой телефон, а в конце упомянул другие
            model_id, m_type, mem_list, query_str = found_in_body[0]
        else:
            # Безопасный Fuzzy-поиск с проверкой пустого заголовка и защитой от путаницы Pro/Max/Plus
            if HAS_RAPIDFUZZ and t_title.strip() and not any(re.search(p, t_full) for p in ACCESSORY_GHOST_PATTERNS):
                choices = {m[0]: m for m in MODELS_WHITELIST if not (m[1] == "apple" and (has_android_in_title or not has_explicit_iphone))}
                if choices:
                    best = fuzz_process.extractOne(t_title, list(choices.keys()), scorer=fuzz.token_set_ratio, score_cutoff=92)
                    if best:
                        cand_name = best[0].lower()
                        # Защита: чтобы 'iPhone 13' никогда не сматчился с 'iPhone 13 Pro' через token_set_ratio
                        suffix_words = [("pro", "про"), ("max", "макс"), ("plus", "плюс", "+"), ("ultra", "ультра"), ("mini", "мини"), ("fe", "фе")]
                        mismatch = False
                        for s_group in suffix_words:
                            in_cand = any(s in cand_name for s in s_group)
                            in_title = any(re.search(rf"\b{re.escape(s)}\b", t_title) for s in s_group if s != "+") or ("+" in s_group and "+" in t_title)
                            if in_cand != in_title:
                                mismatch = True
                                break
                        if not mismatch:
                            model_id, m_type, mem_list, query_str = choices[best[0]]
                        else:
                            return None, "Модель не в Белом списке (несовпадение модификации)"
                    else:
                        return None, "Модель не в Белом списке"
                else:
                    return None, "Модель не в Белом списке"
            else:
                return None, "Модель не в Белом списке"

    memory, is_assumed_mem = extract_memory(t_full, ad_params, mem_list)
    return {
        "model": model_id,
        "type": m_type,
        "memory": memory,
        "valid_memories": mem_list,
        "is_assumed_mem": is_assumed_mem,
        "query": query_str,
        "key": f"{model_id}_{memory}GB"
    }, None


# ================= 3. ПРОДВИНУТАЯ МАТЕМАТИКА РЫНКА (+ ONLINER + ЯКОРЯ) =================
def parse_kufar_time(iso_str):
    if not iso_str:
        return time.time()
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()


def weighted_median(items):
    if not items:
        return 0
    items_sorted = sorted(items, key=lambda x: x[0])
    total_weight = sum(w for _, w in items_sorted)
    if total_weight <= 0:
        return statistics.median([p for p, _ in items_sorted])

    acc = 0.0
    for price, weight in items_sorted:
        acc += weight
        if acc >= total_weight / 2.0:
            return price
    return items_sorted[-1][0]


def fetch_onliner_baraholka_prices(phone_info, target_memory):
    """Тихий фоновый опрос Барахолки Onliner (дополнительный источник хозяйских цен)."""
    prices = []
    q = f"{phone_info['query']} {target_memory}"
    url = f"https://baraholka.onliner.by/search.php?q={requests.utils.quote(q)}&cat=1"
    try:
        r = session.get(url, headers={"Accept": "text/html"}, timeout=6)
        if r.status_code != 200:
            return []
        blocks = re.findall(r'<td class="txt-wb">.*?</tr>', r.text, re.DOTALL)
        for b in blocks[:20]:
            t_m = re.search(r'class="wraptxt"[^>]*>(.*?)</a>', b, re.DOTALL)
            p_m = re.search(r'class="price-primary"[^>]*>\s*([\d\s,\.]+)\s*р', b)
            if not t_m or not p_m:
                continue
            title_raw = re.sub(r"<[^>]+>", "", t_m.group(1)).strip()
            if "куплю" in title_raw.lower() or "обмен" in title_raw.lower():
                continue
            if check_ban_markers(title_raw, "", phone_info["type"], phone_info["model"]):
                continue
            norm, _ = normalize_phone(title_raw, "")
            if not norm or norm["model"] != phone_info["model"] or norm["memory"] != target_memory:
                continue
            p_str = p_m.group(1).replace(" ", "").replace(",", ".")
            p_val = float(p_str)
            if 130 <= p_val <= 3500:
                prices.append((p_val, 0.90))
    except Exception:
        pass
    return prices


def fetch_kufar_sample(phone_info, target_memory, region_minsk=True):
    weighted_prices = []
    days_list = []
    seller_counts = {}
    min_lot = (99999, "")
    cursor = None
    rgn_param = "&rgn=7" if region_minsk else ""
    now = time.time()

    for _ in range(2):
        url = (
            f"https://api.kufar.by/search-api/v2/search/rendered-paginated"
            f"?cat=17010&cmp=0&lang=ru&prc=r%3A12000%2C350000{rgn_param}"
            f"&query={requests.utils.quote(phone_info['query'])}&size=50&sort=lst.d"
        )
        if cursor:
            url += f"&cursor={cursor}"

        for attempt in range(2):
            try:
                resp = session.get(url, timeout=8)
                if resp.status_code == 429:
                    time.sleep(4)
                    continue
                if resp.status_code != 200:
                    break
                data = resp.json()
                ads = data.get("ads", [])
                for ad in ads:
                    subj = ad.get("subject", "")
                    p_byn = int(ad.get("price_byn") or 0) / 100.0
                    if p_byn < 130 or p_byn > 3500:
                        continue

                    if check_ban_markers(subj, "", phone_info["type"], phone_info["model"]):
                        continue
                    if any(has_marker_without_negation(subj, m) for m in HEAVY_COSMETIC_MARKERS + ["нюанс", "дефект", "срочно"]):
                        continue

                    norm, _ = normalize_phone(subj, "", ad.get("ad_parameters", []))
                    if not norm or norm["model"] != phone_info["model"] or norm["memory"] != target_memory:
                        continue
                    if norm["is_assumed_mem"] and len(phone_info["valid_memories"]) > 1:
                        continue
                    if phone_info["model"] in ("iPhone 11", "iPhone 12", "iPhone 13", "iPhone 14", "iPhone 15") and p_byn < 250:
                        continue

                    list_ts = parse_kufar_time(ad.get("list_time", ""))
                    days_old = max(0.0, (now - list_ts) / 86400.0)
                    days_list.append(days_old)

                    # Плавное затухание веса (-2% за каждый день простоя, минимальный вес старых лотов 0.30)
                    weight = max(0.30, 1.0 - 0.02 * days_old)
                    weighted_prices.append((p_byn, weight))

                    acc_id = str(ad.get("account_id") or "")
                    if acc_id:
                        seller_counts[acc_id] = seller_counts.get(acc_id, 0) + 1

                    if p_byn < min_lot[0]:
                        min_lot = (p_byn, ad.get("ad_link", ""))

                next_token = None
                for page in data.get("pagination", {}).get("pages", []):
                    if page.get("label") == "next":
                        next_token = page.get("token")
                        break
                cursor = next_token
                break
            except Exception:
                break
        if not cursor:
            break

    resellers_cnt = sum(1 for c in seller_counts.values() if c >= 2)
    return weighted_prices, days_list, min_lot, resellers_cnt


def compute_raw_market_for_mem(phone_info, target_memory, settings):
    w_prices, days_list, min_lot, resellers_cnt = fetch_kufar_sample(phone_info, target_memory, region_minsk=True)

    if len(w_prices) < 15:
        rb_w, rb_days, rb_min, rb_res = fetch_kufar_sample(phone_info, target_memory, region_minsk=False)
        if len(rb_w) > len(w_prices):
            w_prices, days_list, min_lot, resellers_cnt = rb_w, rb_days, rb_min, rb_res

    onl_prices = fetch_onliner_baraholka_prices(phone_info, target_memory)
    if onl_prices:
        w_prices.extend(onl_prices)

    n = len(w_prices)
    if n < 3:
        return None, n, 0, min_lot, resellers_cnt

    w_prices.sort(key=lambda x: x[0])
    if n >= 7:
        cut_low = max(1, int(n * 0.20))
        cut_high = max(1, int(n * 0.35))
        core = w_prices[cut_low: n - cut_high]
        if not core:
            core = w_prices
    else:
        core = w_prices

    w_med = weighted_median(core)
    market_val = round(w_med * settings.get("bargain_factor", 1.00))
    med_days = round(statistics.median(days_list), 1) if days_list else 3.0
    return market_val, n, med_days, min_lot, resellers_cnt


def get_market_data(phone_info, settings, force_refresh=False):
    cache_key = phone_info["key"]
    now = int(time.time())
    cur = db_conn.cursor()

    anchor_val = settings.get("anchors", {}).get(cache_key)

    if not force_refresh:
        cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        if row and (now - row["updated_ts"]) < MARKET_CACHE_TTL:
            p = anchor_val if anchor_val else row["market_price"]
            return {
                "price": p,
                "min_price": row["min_price"],
                "min_link": row["min_link"],
                "sample_size": row["sample_size"],
                "median_days": row["median_days"],
                "resellers_count": row["resellers_count"],
                "is_correlated": False,
                "is_anchored": bool(anchor_val)
            }

    target_mem = phone_info["memory"]
    valid_mems = phone_info["valid_memories"]
    market_val, n, med_days, min_lot, resellers_cnt = compute_raw_market_for_mem(phone_info, target_mem, settings)
    is_correlated = False

    base_mem = 128 if 128 in valid_mems else valid_mems[0]
    if target_mem != base_mem or (market_val is None or n < 10):
        base_val, base_n, base_days, base_min, base_res = compute_raw_market_for_mem(phone_info, base_mem, settings)
        if base_val and base_n >= 6:
            mem_steps = {64: 0, 128: 1, 256: 2, 512: 3, 1024: 4}
            step_diff = mem_steps.get(target_mem, 1) - mem_steps.get(base_mem, 1)
            correlated_val = round(base_val * (1.0 + 0.12 * step_diff))

            if market_val is None or n < 8:
                market_val = correlated_val
                n = base_n
                med_days = base_days
                is_correlated = True
            elif target_mem > base_mem and market_val < round(base_val * 1.07):
                market_val = correlated_val
                is_correlated = True
            elif target_mem < base_mem and market_val > round(base_val * 0.93):
                market_val = correlated_val
                is_correlated = True

    if market_val is None or (n < MIN_VALID_SAMPLE and not anchor_val):
        return None

    final_price = anchor_val if anchor_val else market_val

    cur.execute("""
        INSERT OR REPLACE INTO market_cache
        (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (cache_key, final_price, min_lot[0] if min_lot[0] < 9999 else final_price, min_lot[1], n, med_days, resellers_cnt, now))
    db_conn.commit()

    return {
        "price": final_price,
        "min_price": min_lot[0] if min_lot[0] < 9999 else final_price,
        "min_link": min_lot[1],
        "sample_size": n,
        "median_days": med_days,
        "resellers_count": resellers_cnt,
        "is_correlated": is_correlated,
        "is_anchored": bool(anchor_val)
    }


def adjust_lot_market_price(base_market, full_text, phone_type, bat_health, color_tier, has_box):
    multiplier = 1.0
    notes = []
    has_defects = False

    if phone_type == "apple" and bat_health is not None:
        if bat_health < 75:
            multiplier -= 0.16
            notes.append(f"АКБ {bat_health}% (-16%)")
            has_defects = True
        elif bat_health < 80:
            multiplier -= 0.11
            notes.append(f"АКБ {bat_health}% (-11%)")
            has_defects = True
        elif bat_health < 85:
            multiplier -= 0.05
            notes.append(f"АКБ {bat_health}% (-5%)")

    heavy_hit = next((m for m in HEAVY_COSMETIC_MARKERS if has_marker_without_negation(full_text, m)), None)
    light_hit = next((m for m in LIGHT_COSMETIC_MARKERS if has_marker_without_negation(full_text, m)), None)

    if heavy_hit:
        multiplier -= 0.10
        notes.append(f"«{heavy_hit}» (-10%)")
        has_defects = True
    elif light_hit:
        multiplier -= 0.03
        notes.append(f"«{light_hit}» (-3%)")

    if not has_box:
        multiplier -= 0.04
        notes.append("нет коробки (-4%)")

    if color_tier == "discount":
        multiplier -= 0.03
        notes.append("цвет (-3%)")

    multiplier = max(0.72, multiplier)
    adj_price = round(base_market * multiplier)
    note_str = ", ".join(notes) if notes else "Стандарт (без штрафов)"
    return adj_price, note_str, has_defects


# ================= 4. TELEGRAM API И БЫСТРЫЙ ИНТЕРФЕЙС КНОПОК (UI) =================
def send_telegram(text, photo_url=None, with_sound=False, reply_markup=None):
    if not TG_TOKEN:
        print("⚠️ TG_TOKEN не задан!")
        return False

    disable_sound = not with_sound
    markup_json = json.dumps(reply_markup) if reply_markup else None

    for attempt in range(2):
        try:
            if photo_url:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
                payload = {
                    "chat_id": TG_CHAT_ID,
                    "photo": photo_url,
                    "caption": text,
                    "parse_mode": "HTML",
                    "disable_notification": disable_sound
                }
                if markup_json:
                    payload["reply_markup"] = markup_json
                r = session.post(url, data=payload, timeout=10)
                if r.status_code == 200:
                    return True
                elif r.status_code == 429:
                    retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(retry_after)
                    continue

            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            payload = {
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": disable_sound
            }
            if markup_json:
                payload["reply_markup"] = markup_json
            r = session.post(url, data=payload, timeout=10)
            if r.status_code == 200:
                return True
            elif r.status_code == 429:
                retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(retry_after)
        except Exception as e:
            print(f"⚠️ Ошибка Telegram API: {e}")
            time.sleep(2)
    return False


def flush_old_telegram_updates():
    if not TG_TOKEN:
        return 0
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-1&timeout=0"
        r = session.get(url, timeout=4)
        if r.status_code == 200:
            res = r.json().get("result", [])
            if res:
                return res[-1]["update_id"]
    except Exception:
        pass
    return 0


def get_main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Оценка / Тест-драйв"}, {"text": "📈 Отчет за сегодня"}],
            [{"text": "🏘 Районы Минска"}, {"text": "⚙️ Настройки / Якоря"}],
            [{"text": "🦈 Топ перекупов"}]
        ],
        "resize_keyboard": True
    }


def get_districts_inline_keyboard(selected_districts):
    rows = []
    temp_row = []
    for d in MINSK_DISTRICTS:
        mark = "✅ " if d in selected_districts else ""
        temp_row.append({"text": f"{mark}{d}", "callback_data": f"dist:{d}"})
        if len(temp_row) == 2:
            rows.append(temp_row)
            temp_row = []
    if temp_row:
        rows.append(temp_row)
    rows.append([{"text": "🌍 Весь Минск (Сбросить фильтр)", "callback_data": "dist:CLEAR"}])
    return {"inline_keyboard": rows}


def handle_telegram_updates(last_update_id, settings):
    if not TG_TOKEN:
        return last_update_id
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=0"
    try:
        r = session.get(url, timeout=4)
        if r.status_code != 200:
            return last_update_id
        updates = r.json().get("result", [])
        for u in updates:
            last_update_id = u["update_id"]

            if "callback_query" in u:
                cb = u["callback_query"]
                chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if chat_id != str(TG_CHAT_ID):
                    continue
                data = cb.get("data", "")
                if data.startswith("dist:"):
                    d_name = data.split(":", 1)[1]
                    if d_name == "CLEAR":
                        settings["districts"] = []
                    else:
                        if d_name in settings["districts"]:
                            settings["districts"].remove(d_name)
                        else:
                            settings["districts"].append(d_name)
                    save_settings(settings)
                    active_str = ", ".join(settings["districts"]) if settings["districts"] else "Весь Минск"
                    edit_url = f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText"
                    session.post(edit_url, data={
                        "chat_id": TG_CHAT_ID,
                        "message_id": cb["message"]["message_id"],
                        "text": f"🏘 <b>Выбор районов Минска</b>\nАктивно: <b>{active_str}</b>\n<i>(Жирные лоты >25% и >80 BYN придут из любого района)</i>",
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(get_districts_inline_keyboard(settings["districts"]))
                    }, timeout=5)
                continue

            msg = u.get("message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id", "")) != str(TG_CHAT_ID):
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            if text in ("/start", "/menu", "Меню"):
                USER_WAIT_STATE["mode"] = None
                send_telegram(
                    "🤖 <b>Панель управления Kufar Sniper 3.2</b>\nВыбери действие на клавиатуре ниже:",
                    reply_markup=get_main_menu_keyboard()
                )

            elif text == "🏘 Районы Минска":
                USER_WAIT_STATE["mode"] = None
                active_str = ", ".join(settings["districts"]) if settings["districts"] else "Весь Минск"
                send_telegram(
                    f"🏘 <b>Выбор районов Минска</b>\nСейчас выбрано: <b>{active_str}</b>\nНажимай на кнопки ниже:",
                    reply_markup=get_districts_inline_keyboard(settings["districts"])
                )

            elif text in ("⚙️ Настройки / Якоря", "⚙️ Настройки цен"):
                USER_WAIT_STATE["mode"] = "settings_input"
                anchors = settings.get("anchors", {})
                anc_str = ", ".join(f"{k}: {v}р" for k, v in anchors.items()) if anchors else "нет"
                send_telegram(
                    f"⚙️ <b>Текущие настройки:</b>\n"
                    f"• Бюджет поиска: <b>{settings['min_price']} – {settings['max_price']} BYN</b>\n"
                    f"• Коэффициент рынка: <b>{settings['bargain_factor']}</b>\n"
                    f"• Порог ЖИРА: <b>{settings['fat_profit_byn']} BYN / {settings['fat_profit_pct']}%</b>\n"
                    f"• Заданные якоря: <b>{anc_str}</b>\n\n"
                    f"<b>Команды для изменения:</b>\n"
                    f"• <code>цена 150 1200</code> — изменить мин и макс цену\n"
                    f"• <code>коэф 1.02</code> — изменить коэффициент рынка\n"
                    f"• <code>жир 75 20</code> — порог жира (75 BYN и 20%)\n"
                    f"• <code>якорь iPhone 13 128 630</code> — жестко зафиксировать рынок модели (0 чтобы удалить)"
                )

            elif text == "📊 Оценка / Тест-драйв":
                USER_WAIT_STATE["mode"] = "price_check"
                send_telegram(
                    "🔎 <b>Узнать рынок / Тест-драйв своей цены</b>\n\n"
                    "Напиши модель и память (а через пробел можешь добавить свою цену продажи для проверки).\n"
                    "Примеры:\n"
                    "• <code>iPhone 12 pro 256</code>\n"
                    "• <code>iPhone 13 128 640</code> (проверит, быстро ли купят за 640 BYN)"
                )

            elif text == "📈 Отчет за сегодня":
                USER_WAIT_STATE["mode"] = None
                send_daily_report()

            elif text == "🦈 Топ перекупов":
                USER_WAIT_STATE["mode"] = None
                send_top_resellers()

            elif USER_WAIT_STATE["mode"] == "settings_input":
                low = text.lower()
                if low.startswith("цена"):
                    parts = re.findall(r"\d+", low)
                    if len(parts) >= 2:
                        settings["min_price"] = int(parts[0])
                        settings["max_price"] = int(parts[1])
                        save_settings(settings)
                        send_telegram(f"✅ Новый бюджет поиска: <b>{settings['min_price']} – {settings['max_price']} BYN</b>")
                elif low.startswith("коэф"):
                    m = re.search(r"(\d+(?:\.\d+)?)", low)
                    if m:
                        settings["bargain_factor"] = float(m.group(1))
                        save_settings(settings)
                        send_telegram(f"✅ Новый коэффициент рынка: <b>{settings['bargain_factor']}</b>")
                elif low.startswith("жир"):
                    parts = re.findall(r"\d+", low)
                    if len(parts) >= 2:
                        settings["fat_profit_byn"] = int(parts[0])
                        settings["fat_profit_pct"] = int(parts[1])
                        save_settings(settings)
                        send_telegram(f"✅ Новый порог ЖИРА: <b>{settings['fat_profit_byn']} BYN / {settings['fat_profit_pct']}%</b>")
                elif low.startswith("якорь"):
                    body = text[5:].strip()
                    tokens = body.split()
                    if len(tokens) >= 2 and tokens[-1].isdigit():
                        price_val = int(tokens[-1])
                        model_q = " ".join(tokens[:-1])
                        p_info, err = normalize_phone(model_q)
                        if p_info:
                            k = p_info["key"]
                            if price_val <= 0:
                                settings["anchors"].pop(k, None)
                                save_settings(settings)
                                send_telegram(f"🗑 Якорь для <b>{k}</b> удален.")
                            else:
                                settings["anchors"][k] = price_val
                                save_settings(settings)
                                send_telegram(f"⚓ Установлен якорь для <b>{p_info['model']} ({p_info['memory']}GB)</b>: <b>{price_val} BYN</b>")
                        else:
                            send_telegram(f"❌ Не удалось распознать модель ({err}).")
                USER_WAIT_STATE["mode"] = None

            elif USER_WAIT_STATE["mode"] == "price_check":
                handle_interactive_price_check(text, settings)
                USER_WAIT_STATE["mode"] = None

    except Exception as e:
        print(f"⚠️ Ошибка getUpdates: {e}")
    return last_update_id


def handle_interactive_price_check(query_text, settings):
    tokens = query_text.strip().split()
    user_test_price = None
    if len(tokens) >= 3 and tokens[-1].isdigit():
        val = int(tokens[-1])
        if val >= 120 and val not in (128, 256, 512, 1024, 2020, 2022):
            user_test_price = val
            query_text = " ".join(tokens[:-1])

    phone_info, err = normalize_phone(query_text)
    if not phone_info:
        send_telegram(f"❌ Не удалось распознать модель из белого списка ({err}). Попробуй написать точнее, например: <code>iPhone 12 pro 256</code>")
        return

    send_telegram(f"⏳ Сканирую рынок Минска, РБ и Onliner для <b>{phone_info['model']} ({phone_info['memory']} GB)</b>...")
    m_data = get_market_data(phone_info, settings, force_refresh=True)
    if not m_data:
        send_telegram("⚠️ Слишком мало активных объявлений на рынке для точного расчета.")
        return

    med = m_data["price"]
    fast_buy = round(med * 0.86)
    bargain_buy = round(med * 0.92)
    min_p = m_data["min_price"]
    min_link = html.escape(m_data["min_link"] or "https://www.kufar.by")
    anc_note = " ⚓ <i>(Зафиксировано твоим якорем)</i>" if m_data.get("is_anchored") else ""

    liq_label = "🔥 Высокая (улетают быстро)" if m_data["median_days"] <= 3.5 else ("⏳ Средняя" if m_data["median_days"] <= 8 else "🐌 Низкая (висят долго)")

    resp = (
        f"📊 <b>Аналитика: {phone_info['model']} ({phone_info['memory']} GB)</b>\n\n"
        f"• 💎 <b>Реальная рыночная цена: ~{med:.0f} BYN</b>{anc_note}\n"
        f"• 🚀 Цена быстрого выкупа (жир): <b>до {fast_buy} BYN</b>\n"
        f"• 🤝 Рабочая цена под перепродажу: <b>~{bargain_buy} BYN</b>\n"
        f"• 📉 Минимум на Куфаре сейчас: <a href='{min_link}'><b>{min_p:.0f} BYN (открыть)</b></a>\n\n"
        f"⏱ Индекс ликвидности: <b>{liq_label}</b> (медиана жизни: ~{m_data['median_days']} дн.)\n"
        f"📦 Чистых лотов в выборке: <b>{m_data['sample_size']} шт.</b> (перекупов на модели: ~{m_data['resellers_count']})"
    )

    if user_test_price:
        diff = user_test_price - med
        if diff > 25:
            advice = f"⚠️ Твоя цена (<b>{user_test_price} BYN</b>) выше рынка на {diff:.0f} р. Звонков будет мало. Рекомендую снизить до <b>{med - 10:.0f}–{med:.0f} BYN</b>."
        elif diff >= -15:
            advice = f"✅ Твоя цена (<b>{user_test_price} BYN</b>) ровно в рынке! Должен уйти за ~{m_data['median_days']:.0f} дн. Для быстрой продажи за 1 день ставь <b>{med - 25:.0f} BYN</b>."
        else:
            advice = f"🔥 Твоя цена (<b>{user_test_price} BYN</b>) ниже рынка на {abs(diff):.0f} р.! Заберут очень быстро, не продешеви."
        resp += f"\n\n🧪 <b>Тест-драйв твоей цены:</b>\n{advice}"

    send_telegram(resp)


def send_daily_report():
    now = int(time.time())
    day_ago = now - 86400
    cur = db_conn.cursor()

    cur.execute("SELECT COUNT(*) as c FROM lot_history WHERE first_seen_ts >= ?", (day_ago,))
    new_cnt = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM lot_history WHERE status = 'sold' AND last_seen_ts >= ?", (day_ago,))
    sold_cnt = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM lot_history WHERE status = 'removed_overpriced' AND last_seen_ts >= ?", (day_ago,))
    removed_cnt = cur.fetchone()["c"]

    cur.execute("SELECT model, memory, last_seen_price, days_on_market, estimated_sold_price FROM lot_history WHERE status = 'sold' AND last_seen_ts >= ? LIMIT 5", (day_ago,))
    sold_rows = cur.fetchall()

    details = ""
    for r in sold_rows:
        details += f"• {r['model']} ({r['memory']}GB) — стоял за {r['last_seen_price']:.0f} р., ушел за ~{r['days_on_market']:.1f} дн. (оценка сделки: ~{r['estimated_sold_price']:.0f} р.)\n"

    msg = (
        f"📈 <b>Отчет по рынку за последние 24 часа:</b>\n\n"
        f"📥 Новых телефонов выложено: <b>{new_cnt} шт.</b>\n"
        f"🏁 Реально продано (выкуплено): <b>{sold_cnt} шт.</b>\n"
        f"🗑 Снято без продажи / перевыставлено: <b>{removed_cnt} шт.</b>\n"
    )
    if details:
        msg += f"\n<b>Примеры закрытых сделок:</b>\n{details}"
    send_telegram(msg)


def send_top_resellers():
    cur = db_conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE ads_count >= 2 ORDER BY ads_count DESC LIMIT 10")
    rows = cur.fetchall()
    if not rows:
        send_telegram("🦈 Пока в базе нет перекупов с 2+ активными телефонами (база наполняется по мере работы).")
        return
    msg = "🦈 <b>Топ активных перекупов на Куфаре:</b>\n\n"
    for i, r in enumerate(rows, 1):
        link = html.escape(r["last_link"] or "https://www.kufar.by")
        msg += f"{i}. ID <code>{r['account_id']}</code> — замечено лотов: <b>{r['ads_count']} шт.</b> (последний: <a href='{link}'>{html.escape(r['last_model'] or 'Телефон')}</a>)\n"
    send_telegram(msg)


# ================= 5. ДЕТЕКТОР "МОГИЛ" И ВЕЧЕРНИЙ РАДАР =================
def check_graveyard_and_digest(settings):
    now = int(time.time())
    cur = db_conn.cursor()

    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, list_time_ts
        FROM lot_history
        WHERE status = 'active' AND (? - last_seen_ts) > 3600
        LIMIT 10
    """, (now,))
    stale_rows = cur.fetchall()

    for row in stale_rows:
        ad_id = row["ad_id"]
        url = f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru"
        try:
            r = session.get(url, timeout=5)
            if r.status_code == 404 or (r.status_code == 200 and not r.json().get("result")):
                days_lived = max(0.1, round((now - (row["list_time_ts"] or now)) / 86400.0, 1))
                est_sold = calculate_estimated_sold_price(row["last_seen_price"], days_lived)

                cur.execute("""
                    SELECT COUNT(*) as c FROM lot_history
                    WHERE account_id = ? AND model = ? AND ad_id != ? AND first_seen_ts > ?
                """, (row["account_id"], row["model"], ad_id, now - 86400))
                relisted = cur.fetchone()["c"] > 0

                new_status = "removed_overpriced" if (relisted or days_lived > 18) else "sold"
                cur.execute("""
                    UPDATE lot_history
                    SET status = ?, days_on_market = ?, estimated_sold_price = ?, last_seen_ts = ?
                    WHERE ad_id = ?
                """, (new_status, days_lived, est_sold, now, ad_id))
            else:
                cur.execute("UPDATE lot_history SET last_seen_ts = ? WHERE ad_id = ?", (now, ad_id))
            db_conn.commit()
        except Exception:
            pass

    minsk_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    today_str = minsk_dt.strftime("%Y-%m-%d")
    if minsk_dt.hour >= 20 and settings.get("last_digest_date") != today_str:
        settings["last_digest_date"] = today_str
        save_settings(settings)

        five_days_ago = now - 5 * 86400
        ten_days_ago = now - 10 * 86400
        cur.execute("""
            SELECT ad_id, model, memory, last_seen_price, list_time_ts
            FROM lot_history
            WHERE status = 'active' AND list_time_ts BETWEEN ? AND ?
            LIMIT 5
        """, (ten_days_ago, five_days_ago))
        hangers = cur.fetchall()
        if hangers:
            msg = "🎯 <b>Вечерний радар «Дожим висяков» (висят 5–10 дней):</b>\n<i>Продавцы уже созрели для хорошего торга в ЛС!</i>\n\n"
            for h in hangers:
                d_cnt = round((now - h["list_time_ts"]) / 86400.0, 1)
                link = f"https://www.kufar.by/item/{h['ad_id']}"
                msg += f"• <a href='{link}'><b>{h['model']} ({h['memory']}GB)</b></a> — {h['last_seen_price']:.0f} BYN (висит {d_cnt} дн.)\n"
            send_telegram(msg, with_sound=False)


# ================= 6. ОСНОВНОЙ ПАРСЕР ЛЕНТЫ КУФАРА =================
def get_latest_ads(settings):
    min_k = int(settings.get("min_price", 120)) * 100
    max_k = int(settings.get("max_price", 1500)) * 100
    url = (
        f"https://api.kufar.by/search-api/v2/search/rendered-paginated"
        f"?cat=17010&cmp=0&lang=ru&prc=r%3A{min_k}%2C{max_k}&rgn=7&size=25&sort=lst.d"
    )
    try:
        r = session.get(url, timeout=8)
        if r.status_code == 200:
            return r.json().get("ads", [])
        elif r.status_code == 429:
            time.sleep(25)
    except Exception as e:
        print(f"⚠️ Ошибка ленты Куфара: {e}")
    return []


def get_ad_description(ad_id):
    url = f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru"
    try:
        r = session.get(url, timeout=6)
        if r.status_code == 200:
            return r.json().get("result", {}).get("body", "")
    except Exception:
        pass
    return ""


def parse_district(ad):
    for param in ad.get("ad_parameters", []):
        if param.get("p") == "area":
            val = str(param.get("vl") or param.get("v") or "")
            if val:
                return val
    return "Не указан"


# ================= ГЛАВНЫЙ ЦИКЛ =================
def main():
    settings = load_settings()

    start_time = time.time()
    max_seconds = MAX_RUN_HOURS * 3600
    last_update_id = flush_old_telegram_updates()
    cur = db_conn.cursor()

    cur.execute("SELECT COUNT(*) as c FROM lot_history")
    if cur.fetchone()["c"] == 0:
        initial_ads = get_latest_ads(settings)
        now_ts = int(time.time())
        for ad in initial_ads:
            ad_id = str(ad.get("ad_id") or "")
            p_byn = int(ad.get("price_byn") or 0) / 100.0
            list_ts = int(parse_kufar_time(ad.get("list_time", "")))
            if ad_id and p_byn > 0:
                cur.execute("""
                    INSERT OR IGNORE INTO lot_history
                    (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """, (ad_id, str(ad.get("account_id") or ""), p_byn, p_byn, now_ts, now_ts, list_ts))
        db_conn.commit()
        send_telegram(
            f"🚀 <b>Kufar Sniper 3.2 запущен!</b>\n"
            f"⚡ Отклик кнопок: <b>1 сек.</b>\n"
            f"📦 Стартовая лента ({len(initial_ads)} лотов) записана в базу.",
            with_sound=False,
            reply_markup=get_main_menu_keyboard()
        )

    print("🚀 Мониторинг Kufar 3.2 активен (Быстрый отклик Telegram включен).")
    next_kufar_scan_ts = 0.0

    while (time.time() - start_time) < max_seconds:
        # 1. Мгновенный опрос кнопок Telegram (каждую 1 секунду)
        last_update_id = handle_telegram_updates(last_update_id, settings)

        now_f = time.time()
        if now_f < next_kufar_scan_ts:
            time.sleep(TG_POLL_INTERVAL)
            continue

        next_kufar_scan_ts = now_f + random.uniform(MIN_INTERVAL, MAX_INTERVAL)

        # 2. Фоновая проверка проданных лотов и вечернего дайджеста
        check_graveyard_and_digest(settings)

        # 3. Опрос свежей ленты Куфара
        ads = get_latest_ads(settings)
        now_ts = int(time.time())

        for ad in reversed(ads):
            last_update_id = handle_telegram_updates(last_update_id, settings)

            ad_id = str(ad.get("ad_id") or "")
            if not ad_id:
                continue

            price_byn = int(ad.get("price_byn") or 0) / 100.0
            if price_byn <= 0:
                continue

            list_ts = int(parse_kufar_time(ad.get("list_time", "")))
            days_old = max(0.0, (now_ts - list_ts) / 86400.0)
            account_id = str(ad.get("account_id") or "")

            cur.execute("SELECT * FROM lot_history WHERE ad_id = ?", (ad_id,))
            existing = cur.fetchone()

            price_drop_tag = ""
            drops_cnt = 0

            if existing:
                old_price = existing["last_seen_price"]
                first_price = existing["first_seen_price"] or old_price
                drops_cnt = existing["price_drops_count"] or 0
                was_notified = existing["notified"] == 1

                if price_byn < old_price and (old_price - price_byn) >= 5:
                    drops_cnt += 1
                    total_drop = first_price - price_byn
                    total_drop_pct = (total_drop / first_price) * 100.0 if first_price > 0 else 0

                    cur.execute("""
                        UPDATE lot_history
                        SET last_seen_price = ?, price_drops_count = ?, last_seen_ts = ?, status = 'active'
                        WHERE ad_id = ?
                    """, (price_byn, drops_cnt, now_ts, ad_id))
                    db_conn.commit()

                    if (total_drop >= 15 and total_drop_pct >= 5.0) or drops_cnt >= 2:
                        price_drop_tag = (
                            f"🔥 <b>Продавец ломается! (Снижение #{drops_cnt})</b>\n"
                            f"📉 История цены: {first_price:.0f} ➡️ <b>{price_byn:.0f} BYN</b> (-{total_drop:.0f} р. / -{total_drop_pct:.0f}%)\n"
                        )
                    elif was_notified:
                        continue
                else:
                    cur.execute("UPDATE lot_history SET last_seen_ts = ? WHERE ad_id = ?", (now_ts, ad_id))
                    db_conn.commit()
                    if was_notified:
                        continue

            raw_title = ad.get("subject", "Без названия")

            # Быстрая проверка только явных браков в заголовке (сперва узнаем, распознается ли модель в заголовке)
            title_norm, _ = normalize_phone(raw_title, "", ad.get("ad_parameters", []))
            title_model_id = title_norm["model"] if title_norm else None
            title_phone_type = title_norm["type"] if title_norm else "apple"

            if check_ban_markers(raw_title, "", title_phone_type, title_model_id):
                cur.execute("""
                    INSERT OR REPLACE INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            description = get_ad_description(ad_id)
            full_text = f"{raw_title}\n{description}"

            phone_info, norm_err = normalize_phone(raw_title, description, ad.get("ad_parameters", []))
            if not phone_info:
                cur.execute("""
                    INSERT OR REPLACE INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            ban_reason = check_ban_markers(raw_title, description, phone_info["type"], phone_info["model"])
            if ban_reason:
                print(f"⛔ [Бан: {ban_reason}] {raw_title}")
                cur.execute("""
                    INSERT OR REPLACE INTO lot_history (ad_id, account_id, model, memory, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (ad_id, account_id, phone_info["model"], phone_info["memory"], price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            bat_health = extract_battery_health(full_text) if phone_info["type"] == "apple" else None
            color_name, color_tier = extract_color(full_text)
            has_box = check_missing_box(full_text)
            district = parse_district(ad)
            link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")

            seller_ads_cnt = 1
            if account_id:
                cur.execute("SELECT ads_count FROM sellers WHERE account_id = ?", (account_id,))
                s_row = cur.fetchone()
                if s_row and not existing:
                    seller_ads_cnt = s_row["ads_count"] + 1
                    cur.execute("""
                        UPDATE sellers SET ads_count = ?, last_model = ?, last_link = ?, last_seen_ts = ?
                        WHERE account_id = ?
                    """, (seller_ads_cnt, phone_info["key"], link, now_ts, account_id))
                elif not s_row:
                    cur.execute("""
                        INSERT INTO sellers (account_id, ads_count, last_model, last_link, last_seen_ts)
                        VALUES (?, 1, ?, ?, ?)
                    """, (account_id, phone_info["key"], link, now_ts))
                elif s_row:
                    seller_ads_cnt = s_row["ads_count"]

            if not existing:
                cur.execute("""
                    INSERT OR REPLACE INTO lot_history
                    (ad_id, account_id, model, memory, battery_health, color, district, has_box,
                     first_seen_price, last_seen_price, price_drops_count, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', 0)
                """, (ad_id, account_id, phone_info["model"], phone_info["memory"], bat_health, color_name,
                      district, 1 if has_box else 0, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()

            m_data = get_market_data(phone_info, settings)
            if not m_data:
                print(f"🔇 [Пропуск редкой/неликвидной модели] {phone_info['key']}")
                continue

            base_market = m_data["price"]
            adj_market, cond_note, has_defects = adjust_lot_market_price(
                base_market, full_text, phone_info["type"], bat_health, color_tier, has_box
            )

            profit_byn = adj_market - price_byn
            profit_pct = (profit_byn / adj_market) * 100.0 if adj_market > 0 else 0.0

            selected_dists = settings.get("districts", [])
            outside_district_tag = ""
            if selected_dists:
                in_selected = any(d.lower() in district.lower() for d in selected_dists)
                if not in_selected:
                    if profit_byn >= 80 and profit_pct >= 25:
                        outside_district_tag = "🚀 <b>ЖИР ВНЕ ТВОЕГО РАЙОНА!</b>\n"
                    else:
                        cur.execute("UPDATE lot_history SET notified = 1 WHERE ad_id = ?", (ad_id,))
                        db_conn.commit()
                        print(f"🗺 [Пропуск по району: {district}] {phone_info['key']}")
                        continue

            matched_urgent = []
            for u_pat in URGENT_PATTERNS:
                m_u = re.search(u_pat, clean_text(full_text))
                if m_u:
                    matched_urgent.append(m_u.group(0))
            is_urgent = len(matched_urgent) > 0

            # ЗВУК ТОЛЬКО НА ЧЕСТНЫЙ ЖИР! Аномально дешевое (>50% ниже рынка) идет без звука.
            is_scam_suspect = profit_pct >= 50.0
            with_sound = False

            if is_scam_suspect:
                tier_header = "⚠️ <b>ПОДОЗРИТЕЛЬНО ДЕШЕВО (>50% ниже рынка — возможен скам/труп на фото)</b>"
                with_sound = False
            elif profit_byn >= settings["fat_profit_byn"] or profit_pct >= settings["fat_profit_pct"] or (is_urgent and profit_byn >= settings["urgent_profit_byn"]):
                tier_header = "🚨🔥 <b>ЖИРНЫЙ ЛОТ! (ЗВОНИ ПЕРВЫМ)</b> 🔥🚨"
                with_sound = True
            elif profit_byn >= settings["work_profit_byn"]:
                tier_header = "🟢 <b>РАБОЧИЙ ВАРИАНТ (Хорошая маржа)</b>"
                with_sound = False
            elif profit_byn >= settings["inspect_min_profit"] and not has_defects:
                tier_header = "👀 <b>ПОД ОСМОТР (Чистый лот в плюсе)</b>"
                with_sound = False
            else:
                cur.execute("UPDATE lot_history SET notified = 1 WHERE ad_id = ?", (ad_id,))
                db_conn.commit()
                print(f"📉 [Мало выгоды: {profit_byn:+.0f} BYN] {phone_info['key']} за {price_byn:.0f} р. (Рынок ~{adj_market} р.)")
                continue

            tags = []
            if seller_ads_cnt >= 3:
                tags.append(f"🦈 <b>Перекуп</b> ({seller_ads_cnt} лотов в базе)")
            else:
                tags.append("👤 Частник")

            if days_old >= 14 and "торг" in clean_text(full_text):
                tags.append(f"💤 <b>Висяк ({days_old:.0f} дн. + торг)</b>")

            if re.search(r"\b(?:обмен|обменяю|бартер)\b", clean_text(full_text)) and not re.search(r"\bобмен\s+не\b", clean_text(full_text)):
                tags.append("🔄 Возможен обмен")

            if any(re.search(p, clean_text(full_text)) for p in NLP_TRAP_PATTERNS):
                tags.append("⚠️ <b>Скрытый нюанс («все работает, НО...»)</b>")

            images = ad.get("images", [])
            if not images:
                tags.append("📸 <b>БЕЗ ФОТО</b>")

            if phone_info["is_assumed_mem"]:
                tags.append(f"ℹ️ Память по низу ({phone_info['memory']}GB)")

            photo_url = f"https://rms.kufar.by/v1/gallery/{images[0]['path']}" if (images and "path" in images[0]) else None

            safe_title = html.escape(raw_title)
            safe_link = html.escape(link)
            short_desc = html.escape(description.strip())
            if len(short_desc) > 260:
                short_desc = short_desc[:260] + "..."

            bat_str = f" | 🔋 АКБ: <b>{bat_health}%</b>" if bat_health else ""
            corr_str = " <i>(корреляция памяти)</i>" if m_data["is_correlated"] else ""
            anc_str = " ⚓" if m_data.get("is_anchored") else ""

            msg = f"{tier_header}\n{outside_district_tag}{price_drop_tag}"
            if is_urgent:
                msg += f"⚡ Срочность: <b>{', '.join(matched_urgent).upper()}</b>\n"

            msg += (
                f"📱 <b>{phone_info['model']} ({phone_info['memory']} GB)</b>{bat_str}\n"
                f"🏷 {safe_title}\n"
                f"📍 Район: <b>Минск ({html.escape(district)})</b>\n"
                f"🏷 Теги: {' | '.join(tags)}\n\n"
                f"💰 Цена продавца: <b>{price_byn:.0f} BYN</b>\n"
                f"📊 Рынок экземпляра: <b>~{adj_market} BYN</b>{corr_str}{anc_str}\n"
                f"🛠 Коррекция: <i>{cond_note}</i>\n"
                f"💵 Чистая выгода: <b>{profit_byn:+.0f} BYN ({profit_pct:+.0f}%)</b>\n"
                f"⏱ Ликвидность: <b>~{m_data['median_days']} дн.</b> (база: {m_data['sample_size']} шт., перекупов: ~{m_data['resellers_count']})\n"
            )

            if short_desc:
                msg += f"\n📝 <i>{short_desc}</i>\n"

            msg += f"\n👉 <a href='{safe_link}'><b>ОТКРЫТЬ ОБЪЯВЛЕНИЕ НА КУФАРЕ</b></a>"

            if send_telegram(msg, photo_url, with_sound=with_sound):
                cur.execute("UPDATE lot_history SET notified = 1 WHERE ad_id = ?", (ad_id,))
                db_conn.commit()
                print(f"✅ ОТПРАВЛЕНО [{phone_info['key']}]: {price_byn:.0f} BYN (Рынок ~{adj_market} BYN | Выгода {profit_byn:+.0f})")

    close_db()
    print("🔄 Смена 5.8 ч завершена.")


if __name__ == "__main__":
    main()
