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
    from rapidfuzz import process as fuzz_process
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

MIN_INTERVAL = 6
MAX_INTERVAL = 12
MARKET_CACHE_TTL = 4 * 3600  # Кэш рынка живет 4 часа
MIN_VALID_SAMPLE = 8         # Минимум объявлений для признания модели ликвидной

# Официальные административные районы Минска в параметрах Куфара
MINSK_DISTRICTS = [
    "Фрунзенский", "Московский", "Первомайский", "Советский",
    "Центральный", "Заводской", "Ленинский", "Октябрьский", "Партизанский"
]

DEFAULT_SETTINGS = {
    "min_price": 120,
    "max_price": 750,
    "bargain_factor": 1.00,       # Коэффициент к очищенной взвешенной медиане
    "districts": [],              # Пустой список = ищем по всему Минску
    "fat_profit_byn": 85,         # Выгода для ЖИРА (со звуком)
    "fat_profit_pct": 22,         # % выгоды для ЖИРА (со звуком)
    "urgent_profit_byn": 55,      # Выгода при триггере срочности
    "work_profit_byn": 40,        # Рабочий вариант (без звука)
    "inspect_min_profit": 25,     # Под осмотр (без звука, строго плюсовая выгода!)
    "last_digest_date": ""
}

# ================= 1. БЕЛЫЙ СПИСОК МОДЕЛЕЙ (С ЗАЩИТОЙ ОТ МАСКИРОВКИ) =================
# Negative lookahead запрещает путать базовые версии с Pro/Max/Plus/Ultra/FE/Lite
NO_SUFFIX = r"(?!\s*(?:pro|про|max|макс|mini|мини|plus|плюс|\+|ultra|ультра|fe|фе|lite|лайт|s\b|t\b|c\b|a\b))"

MODELS_WHITELIST = [
    # --- APPLE iPHONE ---
    ("iPhone 13 Pro Max", "apple", r"\b13\s*(?:pro\s*max|про\s*макс|промакс|promax)\b", [128, 256, 512], "iPhone 13 Pro Max"),
    ("iPhone 13 Pro", "apple", r"\b13\s*(?:pro|про)\b(?!\s*(?:max|макс))", [128, 256, 512], "iPhone 13 Pro"),
    ("iPhone 13 mini", "apple", r"\b13\s*(?:mini|мини)\b", [128, 256, 512], "iPhone 13 mini"),
    ("iPhone 13", "apple", rf"(?:(?:iphone|айфон|айф)\s*13\b|\b13\s*(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 13"),

    ("iPhone 12 Pro Max", "apple", r"\b12\s*(?:pro\s*max|про\s*макс|промакс|promax)\b", [128, 256, 512], "iPhone 12 Pro Max"),
    ("iPhone 12 Pro", "apple", r"\b12\s*(?:pro|про)\b(?!\s*(?:max|макс))", [128, 256, 512], "iPhone 12 Pro"),
    ("iPhone 12 mini", "apple", r"\b12\s*(?:mini|мини)\b", [64, 128, 256], "iPhone 12 mini"),
    ("iPhone 12", "apple", rf"(?:(?:iphone|айфон|айф)\s*12\b|\b12\s*(?:айфон|iphone)\b|\bдвенадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 12"),

    ("iPhone 11 Pro Max", "apple", r"\b11\s*(?:pro\s*max|про\s*макс|промакс|promax)\b", [64, 256, 512], "iPhone 11 Pro Max"),
    ("iPhone 11 Pro", "apple", r"\b11\s*(?:pro|про)\b(?!\s*(?:max|макс))", [64, 256, 512], "iPhone 11 Pro"),
    ("iPhone 11", "apple", rf"(?:(?:iphone|айфон|айф)\s*11\b|\b11\s*(?:айфон|iphone)\b|\bодиннадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 11"),

    ("iPhone XS Max", "apple", r"\b(?:xs|хс|икс\s*эс)\s*(?:max|макс)\b", [64, 256, 512], "iPhone XS Max"),
    ("iPhone XS", "apple", r"\b(?:xs|хс|икс\s*эс)\b(?!\s*(?:max|макс))", [64, 256], "iPhone XS"),
    ("iPhone XR", "apple", r"\b(?:xr|хр|икс\s*эр|хr)\b", [64, 128, 256], "iPhone XR"),
    ("iPhone SE 2022", "apple", r"\bse\s*(?:2022|3|22)\b", [64, 128, 256], "iPhone SE 2022"),
    ("iPhone SE 2020", "apple", r"\bse\s*(?:2020|2|20)\b", [64, 128, 256], "iPhone SE 2020"),

    # --- SAMSUNG ---
    ("Samsung S22 Plus", "android", r"\bs\s*22\s*(?:\+|plus|плюс)\b", [128, 256], "Samsung S22 Plus"),
    ("Samsung S22", "android", rf"\b(?:samsung|самсунг|galaxy)?\s*s\s*22\b{NO_SUFFIX}", [128, 256], "Samsung S22"),
    ("Samsung S21 FE", "android", r"\bs\s*21\s*(?:fe|фе)\b", [128, 256], "Samsung S21 FE"),
    ("Samsung S21 Plus", "android", r"\bs\s*21\s*(?:\+|plus|плюс)\b", [128, 256], "Samsung S21 Plus"),
    ("Samsung S21", "android", rf"\b(?:samsung|самсунг|galaxy)?\s*s\s*21\b{NO_SUFFIX}", [128, 256], "Samsung S21"),
    ("Samsung S20 FE", "android", r"\bs\s*20\s*(?:fe|фе)\b", [128, 256], "Samsung S20 FE"),
    ("Samsung A55", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*55\b", [128, 256], "Samsung A55"),
    ("Samsung A54", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*54\b", [128, 256], "Samsung A54"),
    ("Samsung A53", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*53\b", [128, 256], "Samsung A53"),
    ("Samsung A52", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*52\b", [128, 256], "Samsung A52"),
    ("Samsung A35", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*35\b", [128, 256], "Samsung A35"),
    ("Samsung A34", "android", r"\b(?:samsung|самсунг|galaxy)?\s*(?:a|а)\s*34\b", [128, 256], "Samsung A34"),

    # --- XIAOMI / REDMI / POCO ---
    ("Redmi Note 13 Pro", "android", r"\b(?:redmi|редми)?\s*(?:note|ноут)\s*13\s*(?:pro|про)\b(?!\s*(?:\+|plus|плюс))", [128, 256, 512], "Redmi Note 13 Pro"),
    ("Redmi Note 13", "android", rf"\b(?:redmi|редми)?\s*(?:note|ноут)\s*13\b{NO_SUFFIX}", [128, 256], "Redmi Note 13"),
    ("Redmi Note 12 Pro", "android", r"\b(?:redmi|редми)?\s*(?:note|ноут)\s*12\s*(?:pro|про)\b(?!\s*(?:\+|plus|плюс))", [128, 256], "Redmi Note 12 Pro"),
    ("Redmi Note 12", "android", rf"\b(?:redmi|редми)?\s*(?:note|ноут)\s*12\b{NO_SUFFIX}", [64, 128, 256], "Redmi Note 12"),
    ("Redmi Note 11 Pro", "android", r"\b(?:redmi|редми)?\s*(?:note|ноут)\s*11\s*(?:pro|про)\b", [64, 128], "Redmi Note 11 Pro"),

    ("Poco X6 Pro", "android", r"\b(?:poco|поко)\s*(?:x|х)\s*6\s*(?:pro|про)\b", [256, 512], "Poco X6 Pro"),
    ("Poco X6", "android", rf"\b(?:poco|поко)\s*(?:x|х)\s*6\b{NO_SUFFIX}", [256, 512], "Poco X6"),
    ("Poco X5 Pro", "android", r"\b(?:poco|поко)\s*(?:x|х)\s*5\s*(?:pro|про)\b", [128, 256], "Poco X5 Pro"),
    ("Poco X5", "android", rf"\b(?:poco|поко)\s*(?:x|х)\s*5\b{NO_SUFFIX}", [128, 256], "Poco X5"),
    ("Poco X4 Pro", "android", r"\b(?:poco|поко)\s*(?:x|х)\s*4\s*(?:pro|про)\b", [128, 256], "Poco X4 Pro"),
    ("Poco M6 Pro", "android", r"\b(?:poco|поко)\s*(?:m|м)\s*6\s*(?:pro|про)\b", [256, 512], "Poco M6 Pro"),
    ("Poco M5s", "android", r"\b(?:poco|поко)\s*(?:m|м)\s*5\s*(?:s|с)\b", [64, 128, 256], "Poco M5s"),
    ("Poco F5", "android", rf"\b(?:poco|поко)\s*(?:f|ф)\s*5\b{NO_SUFFIX}", [256], "Poco F5"),
    ("Poco F4", "android", rf"\b(?:poco|поко)\s*(?:f|ф)\s*4\b{NO_SUFFIX}", [128, 256], "Poco F4"),

    # --- GOOGLE PIXEL ---
    ("Pixel 7a", "android", r"\b(?:pixel|пиксель)\s*7\s*(?:a|а)\b", [128], "Pixel 7a"),
    ("Pixel 7", "android", rf"\b(?:pixel|пиксель)\s*7\b{NO_SUFFIX}", [128, 256], "Pixel 7"),
    ("Pixel 6a", "android", r"\b(?:pixel|пиксель)\s*6\s*(?:a|а)\b", [128], "Pixel 6a"),
    ("Pixel 6", "android", rf"\b(?:pixel|пиксель)\s*6\b{NO_SUFFIX}", [128, 256], "Pixel 6"),
]

# Запрещенные бренды при распознавании iPhone (чтобы Realme 12 Pro / Redmi 11 Pro не стали iPhone)
ANDROID_BRAND_WORDS = [
    r"\brealme\b", r"\bреалми\b", r"\bredmi\b", r"\bредми\b", r"\bxiaomi\b", r"\bсяоми\b",
    r"\bkсяоми\b", r"\bpoco\b", r"\bпоко\b", r"\bsamsung\b", r"\bсамсунг\b", r"\bhonor\b",
    r"\bхонор\b", r"\bhuawei\b", r"\bхуавей\b", r"\binfinix\b", r"\bинфиникс\b", r"\btecno\b",
    r"\bтекно\b", r"\boppo\b", r"\bvivo\b", r"\boneplus\b", r"\bnote\b", r"\bноут\b"
]

# Запрещенные слова в целом (аксессуары, чужие линейки)
FORBIDDEN_GHOST_MODELS = [
    r"\bs\s*2[0123]\s*ultra\b", r"\bультра\b", r"\bчехол\b", r"\bстекло\s+для\b",
    r"\bкоробка\s+от\b", r"\bкуплю\b", r"\bищу\b", r"\bаренда\b", r"\bпрокат\b"
]

# ================= 2. ЖЕСТКИЕ МАРКЕРЫ БАНА И ШТРАФОВ =================
# Маркеры проверяются с защитой от частицы "не / без / никаких" перед ними
DEAD_MARKERS = [
    "под замену", "на запчасти", "не включается", "не работает", "не заряжается",
    "только через magsafe", "не реагирует на касания", "битая крышка", "разбит",
    "разбита", "разбито", "трещина", "трещины", "треснут", "утопленник", "после воды",
    "порван шлейф", "шлейф", "проблема в порте", "порт зарядки", "гнездо зарядки",
    "разъем зарядки", "отходит зарядка", "поплыл экран", "пятно на экране",
    "пятна на экране", "полоса на экране", "полосы на экране", "выгорание", "выгорел",
    "материнская плата", "кз", "бутлуп", "перезагружается сам", "не видит сим", "нет сети",
    "не работает face", "не работает фейс", "без face id", "без фейс", "донор",
    "рассрочка", "кредит", "лизинг", "скупка", "трейд ин", "trade in", "магазин",
    "только обмен", "обмен только"
]

REPAIR_MARKERS = [
    "замена акб", "менялся акб", "менялась акб", "акб менялся", "акб менялась",
    "менялся аккумулятор", "аккумулятор менялся", "новая батарея", "заменена батарея",
    "батарея менялась", "менялся экран", "экран менялся", "замена экрана",
    "дисплей менялся", "менялся дисплей", "неизвестная деталь", "ошибка акб",
    "ошибка аккумулятора", "копия", "реплика", "муляж", "реф", "восстановленный",
    "не оплачен", "неоплачен", "mdm", "мдм", "байпас", "bypass"
]

# Косметика: легкая (-3%) и тяжелая (-10%)
LIGHT_COSMETIC_MARKERS = ["потертости", "потёртости", "микроцарапины", "мелкие царапины", "следы от чехла"]
HEAVY_COSMETIC_MARKERS = ["скол", "сколы", "глубокие царапины", "царапины на экране", "вмятина", "коцки", "требует внимания"]

# NLP-конструкции скрытого подвоха
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


def close_db(*args):
    try:
        if db_conn:
            db_conn.commit()
            db_conn.close()
    except Exception:
        pass


atexit.register(close_db)
signal.signal(signal.SIGTERM, close_db)


def calculate_estimated_sold_price(last_price, days_on_market):
    """Target-функция оценки реальной цены закрытия сделки для будущего ML."""
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
    t = text.lower().replace("ё", "е")
    t = re.sub(r"\b(?:ifon|iphon|ipone|ipohne|aifon|айфоша)\b", "iphone", t)
    t = re.sub(r"\b(?:sumsung|samsyng|самс)\b", "samsung", t)
    t = re.sub(r"\b(?:ксиоми|ксяоми|сяоми|ксиаоми)\b", "xiaomi", t)
    return t


def has_marker_without_negation(text, marker):
    """Проверяет наличие маркера, исключая конструкции 'без ...', 'не ...', 'нет ...' за 1-2 слова до него."""
    t = clean_text(text)
    pattern = rf"(?<!без\s)(?<!не\s)(?<!нет\s)(?<!никаких\s)(?<!никакого\s)\b{re.escape(marker)}"
    return bool(re.search(pattern, t))


def check_ban_markers(title, description, phone_type="apple"):
    full = clean_text(f"{title} {description}")

    # 1. Запрещенные призраки (S22 Ultra, чехлы, куплю)
    for pat in FORBIDDEN_GHOST_MODELS:
        if re.search(pat, full):
            return f"Стоп-паттерн: {pat}"

    # 2. Проверка маркеров трупа
    for m in DEAD_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер брака: '{m}'"

    # 3. Проверка маркеров ремонта
    for m in REPAIR_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер ремонта: '{m}'"

    # 4. Умная проверка iCloud / блокировок
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

    # 5. Детектор 100% АКБ на старых iPhone (XR - 12 Pro Max)
    if phone_type == "apple":
        if re.search(r"\b(?:акб|батаре[яиюей]|емкость|состояние\s*акб)[\s:=-]*100\s*%", full):
            return "Подозрительные 100% АКБ на б/у iPhone (вероятна замена банки)"

    return None


def extract_battery_health(text):
    t = clean_text(text)
    # Ищем цифры 60-100% рядом со словами акб/батарея/емкость или просто X%
    m = re.search(r"(?:акб|батаре[яиюей]|емкость|состояние)[^\d]{0,15}(\d{2,3})\s*%", t)
    if not m:
        m = re.search(r"\b(6\d|7\d|8\d|9\d|100)\s*%\s*(?:акб|батаре[яиюей]|родн\w*)?", t)
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
    premium_colors = ["черный", "black", "midnight", "белый", "white", "starlight", "графит", "graphite", "синий", "blue", "фиолетовый", "purple", "зеленый", "green"]
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
            return False  # Явно нет коробки
    return True  # По умолчанию считаем, что коробка есть


def extract_memory(text, ad_params, valid_memories):
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("phablet_phones_memory", "mobile_phones_memory", "memory"):
                val_str = str(p.get("vl") or p.get("v") or "")
                for mem in valid_memories:
                    if re.search(rf"\b{mem}\b", val_str):
                        return mem, False

    t = clean_text(text)
    slash_match = re.search(r"\b(?:4|6|8|12|16)\s*/\s*(64|128|256|512)\b", t)
    if slash_match:
        mem = int(slash_match.group(1))
        if mem in valid_memories:
            return mem, False

    for mem in valid_memories:
        if re.search(rf"\b{mem}\s*(?:гб|gb|гиг|г|g)\b|\bна\s*{mem}\b", t):
            return mem, False

    return valid_memories[0], True


def normalize_phone(title, description="", ad_params=None):
    t_title = clean_text(title)
    t_full = clean_text(f"{title} {description}")

    # Проверяем бренд в параметрах Куфара, если есть
    param_brand = ""
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("brand", "mobile_phones_brand", "phablet_phones_brand"):
                param_brand = str(p.get("vl") or p.get("v") or "").lower()

    has_android_word = any(re.search(w, t_full) for w in ANDROID_BRAND_WORDS)
    has_explicit_iphone = bool(re.search(r"\b(?:iphone|айфон|айф|apple|эпл|ios)\b", t_full)) or ("apple" in param_brand)

    found_in_title = []
    temp_title = t_title

    for model_id, m_type, regex, mem_list, query_str in MODELS_WHITELIST:
        # Защита: если это модель Apple, но в тексте есть Realme/Redmi/Xiaomi или нет слова iPhone — пропускаем!
        if m_type == "apple":
            if has_android_word:
                continue
            if not has_explicit_iphone:
                continue

        if re.search(regex, temp_title):
            found_in_title.append((model_id, m_type, mem_list, query_str))
            temp_title = re.sub(regex, " ", temp_title)

    if len(found_in_title) > 1:
        return None, f"Мульти-лот в заголовке: {', '.join(x[0] for x in found_in_title)}"

    if len(found_in_title) == 1:
        model_id, m_type, mem_list, query_str = found_in_title[0]
    else:
        found_in_body = []
        temp_body = t_full
        for m_id, m_t, regex, m_l, q_s in MODELS_WHITELIST:
            if m_t == "apple" and (has_android_word or not has_explicit_iphone):
                continue
            if re.search(regex, temp_body):
                found_in_body.append((m_id, m_t, m_l, q_s))
                temp_body = re.sub(regex, " ", temp_body)

        if len(found_in_body) == 1:
            model_id, m_type, mem_list, query_str = found_in_body[0]
        elif len(found_in_body) > 1:
            return None, "Несколько моделей в описании"
        else:
            # Fuzzy fallback только при отсутствии запрещенных брендов
            if HAS_RAPIDFUZZ and not any(re.search(p, t_full) for p in FORBIDDEN_GHOST_MODELS):
                choices = {m[0]: m for m in MODELS_WHITELIST if not (m[1] == "apple" and has_android_word)}
                best = fuzz_process.extractOne(t_title, list(choices.keys()), score_cutoff=90)
                if best:
                    model_id, m_type, mem_list, query_str = choices[best[0]]
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


# ================= 3. ПРОДВИНУТАЯ МАТЕМАТИКА РЫНКА =================
def parse_kufar_time(iso_str):
    if not iso_str:
        return time.time()
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()


def weighted_median(items):
    """
    items: список кортежей (price, weight).
    Считает взвешенную медиану: свежие объявления имеют вес 1.0, старые висяки стремительно теряют вес.
    """
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


def fetch_kufar_sample(phone_info, target_memory, region_minsk=True):
    """
    Собирает до 100 объявлений (2 страницы по 50), возвращает:
    weighted_prices: [(price, weight)], days_list: [days], min_lot: (price, link), resellers_cnt
    """
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
            f"?cat=17010&cmp=0&lang=ru&prc=r%3A12000%2C180000{rgn_param}"
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
                    if p_byn < 130 or p_byn > 2200:
                        continue

                    if check_ban_markers(subj, "", phone_info["type"]):
                        continue
                    if any(has_marker_without_negation(subj, m) for m in HEAVY_COSMETIC_MARKERS + ["нюанс", "дефект", "срочно"]):
                        continue

                    norm, _ = normalize_phone(subj, "", ad.get("ad_parameters", []))
                    if not norm or norm["model"] != phone_info["model"] or norm["memory"] != target_memory:
                        continue
                    # При сборе статистики рынка берем только лоты с явно указанной памятью (или если это единственная память)
                    if norm["is_assumed_mem"] and len(phone_info["valid_memories"]) > 1:
                        continue
                    if phone_info["model"] in ("iPhone 11", "iPhone 12", "iPhone 13") and p_byn < 250:
                        continue

                    list_ts = parse_kufar_time(ad.get("list_time", ""))
                    days_old = max(0.0, (now - list_ts) / 86400.0)
                    days_list.append(days_old)

                    # Крутое затухание веса (-8% за каждый день простоя, после 11 дней минимальный вес 0.05)
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
    """Считает чистую взвешенную медиану для конкретной памяти с обрезкой 20% низа и 35% верха (хотелок)."""
    w_prices, days_list, min_lot, resellers_cnt = fetch_kufar_sample(phone_info, target_memory, region_minsk=True)

    # Авто-расширение на всю Беларусь, если в Минске мало лотов (< 15)
    if len(w_prices) < 15:
        rb_w, rb_days, rb_min, rb_res = fetch_kufar_sample(phone_info, target_memory, region_minsk=False)
        if len(rb_w) > len(w_prices):
            w_prices, days_list, min_lot, resellers_cnt = rb_w, rb_days, rb_min, rb_res

    n = len(w_prices)
    if n < 3:
        return None, n, 0, min_lot, resellers_cnt

    w_prices.sort(key=lambda x: x[0])
    if n >= 7:
        # Отрезаем 20% самого низа и 35% самого верха (перекупские висяки)
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
    """
    Возвращает словарь рыночной аналитики с проверкой монотонности памяти (256GB всегда дороже 128GB).
    """
    cache_key = phone_info["key"]
    now = int(time.time())
    cur = db_conn.cursor()

    if not force_refresh:
        cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        if row and (now - row["updated_ts"]) < MARKET_CACHE_TTL:
            return {
                "price": row["market_price"],
                "min_price": row["min_price"],
                "min_link": row["min_link"],
                "sample_size": row["sample_size"],
                "median_days": row["median_days"],
                "resellers_count": row["resellers_count"],
                "is_correlated": False
            }

    target_mem = phone_info["memory"]
    valid_mems = phone_info["valid_memories"]
    market_val, n, med_days, min_lot, resellers_cnt = compute_raw_market_for_mem(phone_info, target_mem, settings)
    is_correlated = False

    # ПРАВИЛО МОНОТОННОСТИ ПАМЯТИ И МАЛОЙ ВЫБОРКИ
    # Сравниваем с базовой (массовой) памятью модели (например, 128GB или 64GB)
    base_mem = 128 if 128 in valid_mems else valid_mems[0]
    if target_mem != base_mem or (market_val is None or n < 10):
        base_val, base_n, base_days, base_min, base_res = compute_raw_market_for_mem(phone_info, base_mem, settings)
        if base_val and base_n >= 6:
            # Коэффициент шага памяти (~12% за ступень)
            mem_steps = {64: 0, 128: 1, 256: 2, 512: 3}
            step_diff = mem_steps.get(target_mem, 1) - mem_steps.get(base_mem, 1)
            correlated_val = round(base_val * (1.0 + 0.12 * step_diff))

            if market_val is None or n < 8:
                market_val = correlated_val
                n = base_n
                med_days = base_days
                is_correlated = True
            elif target_mem > base_mem and market_val < round(base_val * 1.07):
                # Если 256GB посчитался дешевле или почти равно 128GB — принудительно поднимаем!
                market_val = correlated_val
                is_correlated = True
            elif target_mem < base_mem and market_val > round(base_val * 0.93):
                market_val = correlated_val
                is_correlated = True

    if market_val is None or n < MIN_VALID_SAMPLE:
        return None

    cur.execute("""
        INSERT OR REPLACE INTO market_cache
        (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (cache_key, market_val, min_lot[0] if min_lot[0] < 9999 else market_val, min_lot[1], n, med_days, resellers_cnt, now))
    db_conn.commit()

    return {
        "price": market_val,
        "min_price": min_lot[0] if min_lot[0] < 9999 else market_val,
        "min_link": min_lot[1],
        "sample_size": n,
        "median_days": med_days,
        "resellers_count": resellers_cnt,
        "is_correlated": is_correlated
    }


def adjust_lot_market_price(base_market, full_text, phone_type, bat_health, color_tier, has_box):
    """Индивидуальная корректировка рыночной цены конкретного экземпляра (АКБ, коробка, цвет, косметика)."""
    multiplier = 1.0
    notes = []
    has_defects = False

    # 1. Динамический штраф за АКБ (для iPhone)
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

    # 2. Косметика: сколы (-10%) или мелкие потертости (-3%)
    heavy_hit = next((m for m in HEAVY_COSMETIC_MARKERS if has_marker_without_negation(full_text, m)), None)
    light_hit = next((m for m in LIGHT_COSMETIC_MARKERS if has_marker_without_negation(full_text, m)), None)

    if heavy_hit:
        multiplier -= 0.10
        notes.append(f"«{heavy_hit}» (-10%)")
        has_defects = True
    elif light_hit:
        multiplier -= 0.03
        notes.append(f"«{light_hit}» (-3%)")

    # 3. Коробка (если явно написано 'нет коробки' -> -4%)
    if not has_box:
        multiplier -= 0.04
        notes.append("нет коробки (-4%)")

    # 4. Цвет (неликвидный красный/желтый/розовый -> -3%)
    if color_tier == "discount":
        multiplier -= 0.03
        notes.append("цвет (-3%)")

    # Ограничиваем максимальный суммарный дисконт разумным пределом -28%
    multiplier = max(0.72, multiplier)
    adj_price = round(base_market * multiplier)
    note_str = ", ".join(notes) if notes else "Стандарт (без штрафов)"
    return adj_price, note_str, has_defects


# ================= 4. TELEGRAM API И ИНТЕРФЕЙС КНОПОК (UI) =================
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


def get_main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Оценка / Тест-драйв"}, {"text": "📈 Отчет за сегодня"}],
            [{"text": "🏘 Районы Минска"}, {"text": "⚙️ Настройки цен"}],
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


USER_WAIT_STATE = {"mode": None}


def handle_telegram_updates(last_update_id, settings):
    """Опрашивает кнопки и команды владельца в Telegram (защищено по TG_CHAT_ID)."""
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

            # Обработка нажатий inline-кнопок (выбор районов)
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
                continue  # Жесткий фейс-контроль: чужие пользователи игнорируются

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            if text in ("/start", "/menu", "Меню"):
                USER_WAIT_STATE["mode"] = None
                send_telegram(
                    "🤖 <b>Панель управления Kufar Sniper 3.0</b>\nВыбери действие на клавиатуре ниже:",
                    reply_markup=get_main_menu_keyboard()
                )

            elif text == "🏘 Районы Минска":
                USER_WAIT_STATE["mode"] = None
                active_str = ", ".join(settings["districts"]) if settings["districts"] else "Весь Минск"
                send_telegram(
                    f"🏘 <b>Выбор районов Минска</b>\nСейчас выбрано: <b>{active_str}</b>\nНажимай на кнопки ниже, чтобы включить/выключить район:",
                    reply_markup=get_districts_inline_keyboard(settings["districts"])
                )

            elif text == "⚙️ Настройки цен":
                USER_WAIT_STATE["mode"] = "settings_input"
                send_telegram(
                    f"⚙️ <b>Текущие настройки:</b>\n"
                    f"• Бюджет поиска: <b>{settings['min_price']} – {settings['max_price']} BYN</b>\n"
                    f"• Коэффициент рынка: <b>{settings['bargain_factor']}</b>\n"
                    f"• Порог ЖИРА: <b>{settings['fat_profit_byn']} BYN / {settings['fat_profit_pct']}%</b>\n\n"
                    f"Чтобы изменить, отправь мне сообщение в формате:\n"
                    f"<code>цена 150 800</code> (изменит мин и макс цену)\n"
                    f"или <code>коэф 1.02</code> (изменит коэффициент рынка)"
                )

            elif text == "📊 Оценка / Тест-драйв":
                USER_WAIT_STATE["mode"] = "price_check"
                send_telegram(
                    "🔎 <b>Узнать рынок / Тест-драйв своей цены</b>\n\n"
                    "Напиши модель и память (а через пробел можешь добавить свою цену продажи для проверки).\n"
                    "Примеры:\n"
                    "• <code>iPhone 12 128</code>\n"
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
                USER_WAIT_STATE["mode"] = None

            elif USER_WAIT_STATE["mode"] == "price_check":
                handle_interactive_price_check(text, settings)
                USER_WAIT_STATE["mode"] = None

    except Exception as e:
        print(f"⚠️ Ошибка getUpdates: {e}")
    return last_update_id


def handle_interactive_price_check(query_text, settings):
    """Интерактивная оценка модели + Тест-драйв цены пользователя."""
    # Проверяем, указал ли пользователь свою цену в конце (например: iPhone 12 128 480)
    tokens = query_text.strip().split()
    user_test_price = None
    if len(tokens) >= 3 and tokens[-1].isdigit():
        val = int(tokens[-1])
        if val not in (64, 128, 256, 512, 2020, 2022):
            user_test_price = val
            query_text = " ".join(tokens[:-1])

    phone_info, err = normalize_phone(query_text)
    if not phone_info:
        send_telegram(f"❌ Не удалось распознать модель из белого списка ({err}). Попробуй написать точнее, например: <code>iPhone 12 128</code>")
        return

    send_telegram(f"⏳ Сканирую рынок Минска и РБ для <b>{phone_info['model']} ({phone_info['memory']} GB)</b>...")
    m_data = get_market_data(phone_info, settings, force_refresh=True)
    if not m_data:
        send_telegram("⚠️ Слишком мало активных объявлений на рынке для точного расчета.")
        return

    med = m_data["price"]
    fast_buy = round(med * 0.86)
    bargain_buy = round(med * 0.92)
    min_p = m_data["min_price"]
    min_link = html.escape(m_data["min_link"] or "https://www.kufar.by")

    liq_label = "🔥 Высокая (улетают быстро)" if m_data["median_days"] <= 3.5 else ("⏳ Средняя" if m_data["median_days"] <= 8 else "🐌 Низкая (висят долго)")

    resp = (
        f"📊 <b>Аналитика: {phone_info['model']} ({phone_info['memory']} GB)</b>\n\n"
        f"• 💎 <b>Реальная рыночная цена: ~{med:.0f} BYN</b>\n"
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


# ================= 5. ДЕТЕКТОР "МОГИЛ" (ПРОДАННЫХ ЛОТОВ) И ВЕЧЕРНИЙ РАДАР =================
def check_graveyard_and_digest(settings):
    """
    Проверяет активные лоты в SQLite, которые давно не мелькали, выясняет:
    реально продан телефон или просто снят, и раз в сутки в 20:00 шлет дайджест 'Дожима висяков'.
    """
    now = int(time.time())
    cur = db_conn.cursor()

    # Проверяем до 5 активных лотов за цикл, которые не обновлялись > 3 часов
    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, list_time_ts
        FROM lot_history
        WHERE status = 'active' AND (? - last_seen_ts) > 10800
        LIMIT 5
    """, (now,))
    stale_rows = cur.fetchall()

    for row in stale_rows:
        ad_id = row["ad_id"]
        url = f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru"
        try:
            r = session.get(url, timeout=5)
            if r.status_code == 404 or (r.status_code == 200 and not r.json().get("result")):
                # Объявление удалено с Куфара! Вычисляем сколько дней оно прожило
                days_lived = max(0.1, round((now - (row["list_time_ts"] or now)) / 86400.0, 1))
                est_sold = calculate_estimated_sold_price(row["last_seen_price"], days_lived)

                # Проверяем: перевыставил ли этот же продавец такую же модель за последние сутки
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

    # Вечерний дайджест 'Дожим висяков' (один раз в день после 20:00 по Минску = UTC+3)
    minsk_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    today_str = minsk_dt.strftime("%Y-%m-%d")
    if minsk_dt.hour == 20 and settings.get("last_digest_date") != today_str:
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
    max_k = int(settings.get("max_price", 750)) * 100
    # Без фильтра cnd=1 (ловим и б/у, и новые телефоны от частников cmp=0)
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


# ================= ТЕСТОВЫЙ РЕЖИМ (--test) =================
def run_self_test(settings):
    print("🧪 ЗАПУСК ПОЛНОЙ САМОПРОВЕРКИ KUFAR SNIPER 3.0...")

    # 1. Тест защиты от Realme / Redmi / S22+
    r1, err1 = normalize_phone("Продам Realme 12 Pro 8/256 в идеале")
    r2, err2 = normalize_phone("Xiaomi Redmi Note 11 Pro 128gb")
    r3, err3 = normalize_phone("Samsung S22+ 128gb черный")
    print(f"1. Защита от Realme 12 Pro -> {r1} ({err1}) [Должен быть None!]")
    print(f"2. Защита от Redmi Note 11 Pro -> {r2['model'] if r2 else None} [Должен быть Redmi Note 11 Pro, НЕ iPhone!]")
    print(f"3. Защита от Samsung S22+ -> {r3['model'] if r3 else None} [Должен быть Samsung S22 Plus, НЕ обычный S22!]")

    # 2. Тест контекстного бана iCloud и ремонтов
    b1 = check_ban_markers("iPhone 12 128gb", "Состояние идеал, айклауд чистый, выйду при встрече, экран не менялся")
    b2 = check_ban_markers("iPhone 13 128gb", "Все работает, но проблема в порте зарядки, забыл пароль от айклауд")
    print(f"4. Контекстный фильтр: честный лот -> {b1} (Ожидается None) | проблемный лот -> {b2}")

    # 3. Тест живого расчета рынка iPhone 13 128GB
    t_iphone, _ = normalize_phone("Продам iPhone 13 128gb")
    print("5. Живой расчет взвешенного рынка для iPhone 13 128GB...")
    m_data = get_market_data(t_iphone, settings, force_refresh=True)
    if m_data:
        print(f"   📊 Рынок iPhone 13 128GB: ~{m_data['price']} BYN | База: {m_data['sample_size']} шт. | Мин. лот: {m_data['min_price']} BYN | Ср. время жизни: {m_data['median_days']} дн.")
        send_telegram(
            f"✅ <b>Kufar Sniper 3.0 готов к бою!</b>\n\n"
            f"📱 Тест рынка <b>iPhone 13 (128GB)</b>:\n"
            f"• Расчетная медиана: <b>~{m_data['price']} BYN</b> (база {m_data['sample_size']} шт.)\n"
            f"• Минимум в рынке сейчас: <b>{m_data['min_price']} BYN</b>\n"
            f"• Среднее время жизни: <b>~{m_data['median_days']} дн.</b>\n"
            f"• Перекупов на модели: <b>~{m_data['resellers_count']}</b>\n\n"
            f"👇 Кнопки управления активированы!",
            with_sound=True,
            reply_markup=get_main_menu_keyboard()
        )
    print("✅ Самопроверка успешно завершена!")


# ================= ГЛАВНЫЙ ЦИКЛ =================
def main():
    settings = load_settings()

    if "--test" in sys.argv:
        run_self_test(settings)
        return

    start_time = time.time()
    max_seconds = MAX_RUN_HOURS * 3600
    last_update_id = 0
    cur = db_conn.cursor()

    # Первичная инициализация базы при самом первом запуске (чтобы не спамить старой лентой)
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
            f"🚀 <b>Kufar Sniper 3.0 (SQLite + UI + Smart Market) запущен!</b>\n"
            f"📦 Стартовая лента ({len(initial_ads)} лотов) записана в базу.\n"
            f"Используй кнопки внизу для управления ботом.",
            with_sound=False,
            reply_markup=get_main_menu_keyboard()
        )

    print("🚀 Мониторинг Kufar 3.0 активен.")

    while (time.time() - start_time) < max_seconds:
        # 1. Проверяем нажатия кнопок в Telegram
        last_update_id = handle_telegram_updates(last_update_id, settings)

        # 2. Фоновая проверка проданных лотов и вечернего дайджеста
        check_graveyard_and_digest(settings)

        # 3. Опрос свежей ленты Куфара
        ads = get_latest_ads(settings)
        now_ts = int(time.time())

        for ad in reversed(ads):
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
                drops_cnt = existing["price_drops_count"]
                # Проверяем: снизил ли продавец цену минимум на 5% (и минимум на 15 BYN)
                if old_price > 0 and (old_price - price_byn) >= 15 and ((old_price - price_byn) / old_price) >= 0.05:
                    drops_cnt += 1
                    price_drop_tag = f"🔥 <b>Продавец ломается!</b> (Цена упала: {old_price:.0f} ➡️ {price_byn:.0f} BYN)\n"
                    cur.execute("""
                        UPDATE lot_history
                        SET last_seen_price = ?, price_drops_count = ?, last_seen_ts = ?, status = 'active'
                        WHERE ad_id = ?
                    """, (price_byn, drops_cnt, now_ts, ad_id))
                    db_conn.commit()
                else:
                    cur.execute("UPDATE lot_history SET last_seen_ts = ? WHERE ad_id = ?", (now_ts, ad_id))
                    db_conn.commit()
                    continue

            raw_title = ad.get("subject", "Без названия")

            # Быстрый фильтр заголовка до загрузки описания
            if check_ban_markers(raw_title, ""):
                if not existing:
                    cur.execute("""
                        INSERT OR IGNORE INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                    db_conn.commit()
                continue

            description = get_ad_description(ad_id)
            full_text = f"{raw_title}\n{description}"

            # Распознаем модель и память
            phone_info, norm_err = normalize_phone(raw_title, description, ad.get("ad_parameters", []))
            if not phone_info:
                if not existing:
                    cur.execute("""
                        INSERT OR IGNORE INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                    db_conn.commit()
                continue

            # Полная проверка маркеров бана
            ban_reason = check_ban_markers(raw_title, description, phone_info["type"])
            if ban_reason:
                print(f"⛔ [Бан: {ban_reason}] {raw_title}")
                if not existing:
                    cur.execute("""
                        INSERT OR IGNORE INTO lot_history (ad_id, account_id, model, memory, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, notified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """, (ad_id, account_id, phone_info["model"], phone_info["memory"], price_byn, price_byn, now_ts, now_ts, list_ts))
                    db_conn.commit()
                continue

            # Извлекаем параметры экземпляра для SQLite и корректировки цены
            bat_health = extract_battery_health(full_text) if phone_info["type"] == "apple" else None
            color_name, color_tier = extract_color(full_text)
            has_box = check_missing_box(full_text)
            district = parse_district(ad)
            link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")

            # Обновляем статистику продавца (ТОЛЬКО по валидным смартфонам из белого списка!)
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

            # Записываем сам лот в lot_history
            if not existing:
                cur.execute("""
                    INSERT OR REPLACE INTO lot_history
                    (ad_id, account_id, model, memory, battery_health, color, district, has_box,
                     first_seen_price, last_seen_price, price_drops_count, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', 0)
                """, (ad_id, account_id, phone_info["model"], phone_info["memory"], bat_health, color_name,
                      district, 1 if has_box else 0, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()

            # Считаем рынок модели (редкие модели с выборкой < MIN_VALID_SAMPLE возвращают None и НЕ присылаются!)
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

            # Проверка фильтра районов Минска (с исключением для ЖИРА >25% и >80 BYN)
            selected_dists = settings.get("districts", [])
            outside_district_tag = ""
            if selected_dists:
                in_selected = any(d.lower() in district.lower() for d in selected_dists)
                if not in_selected:
                    if profit_byn >= 80 and profit_pct >= 25:
                        outside_district_tag = "🚀 <b>ЖИР ВНЕ ТВОЕГО РАЙОНА!</b>\n"
                    else:
                        print(f"🗺 [Пропуск по району: {district}] {phone_info['key']}")
                        continue

            # Триггеры срочности
            matched_urgent = []
            for u_pat in URGENT_PATTERNS:
                m_u = re.search(u_pat, clean_text(full_text))
                if m_u:
                    matched_urgent.append(m_u.group(0))
            is_urgent = len(matched_urgent) > 0

            # Классификация лота и проверка на аномально дешевый скам (>50% ниже рынка)
            is_scam_suspect = profit_pct >= 50.0
            with_sound = False

            if is_scam_suspect:
                tier_header = "⚠️ <b>ПОДОЗРИТЕЛЬНО ДЕШЕВО (>50% ниже рынка — возможен скам/труп на фото)</b>"
                with_sound = False  # Аномально дешевое шлем БЕЗ звука!
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
                print(f"📉 [Мало выгоды: {profit_byn:+.0f} BYN] {phone_info['key']} за {price_byn:.0f} р. (Рынок ~{adj_market} р.)")
                continue

            # Дополнительные умные теги
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

            # Формируем чистую карточку (без шаблона ЛС и без 'торгуй до')
            safe_title = html.escape(raw_title)
            safe_link = html.escape(link)
            short_desc = html.escape(description.strip())
            if len(short_desc) > 260:
                short_desc = short_desc[:260] + "..."

            bat_str = f" | 🔋 АКБ: <b>{bat_health}%</b>" if bat_health else ""
            corr_str = " <i>(корреляция памяти)</i>" if m_data["is_correlated"] else ""

            msg = f"{tier_header}\n{outside_district_tag}{price_drop_tag}"
            if is_urgent:
                msg += f"⚡ Срочность: <b>{', '.join(matched_urgent).upper()}</b>\n"

            msg += (
                f"📱 <b>{phone_info['model']} ({phone_info['memory']} GB)</b>{bat_str}\n"
                f"🏷 {safe_title}\n"
                f"📍 Район: <b>Минск ({html.escape(district)})</b>\n"
                f"🏷 Теги: {' | '.join(tags)}\n\n"
                f"💰 Цена продавца: <b>{price_byn:.0f} BYN</b>\n"
                f"📊 Рынок экземпляра: <b>~{adj_market} BYN</b>{corr_str}\n"
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

        time.sleep(random.uniform(MIN_INTERVAL, MAX_INTERVAL))

    close_db()
    print("🔄 Смена 5.8 ч завершена.")


if __name__ == "__main__":
    main()
