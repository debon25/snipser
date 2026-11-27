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
import zipfile
import datetime
import statistics
import requests
from reliability import (RetryAd, migrate, remember_ads, pending_ads, finish_ad,
                         queue_notification, drain_outbox, recover_outbox,
                         mark_unavailable, mark_active, settings_revision, watchdog, remember_detail)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')

try:
    from rapidfuzz import process as fuzz_process, fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ================= БАЗОВЫЕ НАСТРОЙКИ И СЕКРЕТЫ =================
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
MAX_RUN_HOURS = float(os.getenv("MAX_RUN_HOURS", "5.5"))  # 0 = continuously

DB_FILE = os.getenv("DB_FILE", "sniper.db")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")

SCAN_MIN_PRICE = 50                 # Нижняя граница сканирования для ML-базы (BYN)
SCAN_MAX_PRICE = 5500               # Верхняя граница сканирования для ML-базы (BYN)
FEED_PAGE_SIZE = 50                 # 50 лотов за 1 запрос (защита от пропусков в пики)
TG_POLL_INTERVAL = 1.0              # Опрос кнопок Telegram не чаще раза в 1 секунду
GRAVEYARD_CHECK_INTERVAL = 300.0    # Проверка удаленных/проданных лотов раз в 5 минут
MARKET_CACHE_TTL = 4 * 3600         # Кэш витринного рынка живет 4 часа
SOLD_LOOKBACK_DAYS = 30             # Совместимость полей прежнего формата; эвристика отключена
MIN_VALID_SAMPLE = 3                # Минимальная выборка (при <8 ставится предупреждающий тег)

MINSK_DISTRICTS = [
    "Фрунзенский", "Московский", "Первомайский", "Советский",
    "Центральный", "Заводской", "Ленинский", "Октябрьский", "Партизанский"
]

DEFAULT_SETTINGS = {
    "min_price": 120,             # Мин. цена для УВЕДОМЛЕНИЙ в Telegram
    "max_price": 750,             # Макс. цена для УВЕДОМЛЕНИЙ в Telegram (база собирается до 5500)
    "bargain_factor": 1.00,       # Коэффициент к рынку
    "districts": [],              # Пустой список = весь Минск
    "anchors": {},                # Ручные якоря цен: {"iPhone 13_128GB": 600}
    "fat_profit_byn": 85,         # Выгода для ЖИРА (со звуком)
    "fat_profit_pct": 22,         # % выгоды для ЖИРА (со звуком)
    "urgent_profit_byn": 55,      # Выгода при триггере срочности
    "work_profit_byn": 40,        # Рабочий вариант (без звука)
    "inspect_min_profit": 15,     # Под осмотр (без звука)
    "last_digest_date": ""
}

USER_WAIT_STATE = {"mode": None}
RUNNING = True
LAST_TG_POLL_TS = 0.0

# ================= 1. БЕЛЫЙ СПИСОК МОДЕЛЕЙ (iPHONE + ПОПУЛЯРНЫЕ ANDROID) =================
SEP = r"[\s\-_.]*"
TIGHT_SEP = r"[\-_.]*"
PLUS_MOD = r"(?:\+(?!\w)|(?:plus|плюс)\b)"
NO_UNIT_AFTER = r"(?!\.\d)(?!\s*(?:дн|дня|дней|мес|лет|год|р\b|руб|byn|%|шт|гб|gb|мп|mp|вт|w\b|мкр|дом|кв))"
MULTI_SUFFIX = r"(?:\+(?!\w)|(?:pro|про|max|макс|mini|мини|plus|плюс|ultra|ультра|fe|фе|lite|лайт|xl|хл)\b)"
SINGLE_SUFFIX = r"(?:t|т|e|е|a|а|s|с)\b"
NO_SUFFIX = rf"(?!{SEP}{MULTI_SUFFIX})(?!{TIGHT_SEP}{SINGLE_SUFFIX}){NO_UNIT_AFTER}"

BRIDGE_STOP = r"(?:iphone|айфон|apple|эпл|ios|айос|pro|про|max|макс|mini|мини|plus|плюс|xs|хс|xr|хр|se|x|х|гб|gb)"
IPHONE_BRIDGE = rf"(?:{SEP}|\s+(?:(?!{BRIDGE_STOP}\b)[а-яa-z]+\s+){{0,2}})(?<!ios\s)(?<!айос\s)"

MODELS_WHITELIST = [
    # --- APPLE iPHONE (от XR / SE 2020 до 16 Pro Max + 16e) ---
    ("iPhone 16 Pro Max", "apple", rf"\b16{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [256, 512, 1024], "iPhone 16 Pro Max"),
    ("iPhone 16 Pro", "apple", rf"\b16{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 16 Pro"),
    ("iPhone 16 Plus", "apple", rf"\b16{SEP}{PLUS_MOD}", [128, 256, 512], "iPhone 16 Plus"),
    ("iPhone 16e", "apple", rf"\b16{TIGHT_SEP}(?:e|е)\b(?!\.)", [128, 256, 512], "iPhone 16e"),
    ("iPhone 16", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}16\b|\b16{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 16"),

    ("iPhone 15 Pro Max", "apple", rf"\b15{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [256, 512, 1024], "iPhone 15 Pro Max"),
    ("iPhone 15 Pro", "apple", rf"\b15{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 15 Pro"),
    ("iPhone 15 Plus", "apple", rf"\b15{SEP}{PLUS_MOD}", [128, 256, 512], "iPhone 15 Plus"),
    ("iPhone 15", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}15\b|\b15{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 15"),

    ("iPhone 14 Pro Max", "apple", rf"\b14{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512, 1024], "iPhone 14 Pro Max"),
    ("iPhone 14 Pro", "apple", rf"\b14{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 14 Pro"),
    ("iPhone 14 Plus", "apple", rf"\b14{SEP}{PLUS_MOD}", [128, 256, 512], "iPhone 14 Plus"),
    ("iPhone 14", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}14\b|\b14{SEP}(?:айфон|iphone)\b){NO_SUFFIX}", [128, 256, 512], "iPhone 14"),

    ("iPhone 13 Pro Max", "apple", rf"\b13{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512, 1024], "iPhone 13 Pro Max"),
    ("iPhone 13 Pro", "apple", rf"\b13{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 13 Pro"),
    ("iPhone 13 mini", "apple", rf"\b13{SEP}(?:mini|мини)\b", [128, 256, 512], "iPhone 13 mini"),
    ("iPhone 13", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}13\b|\b13{SEP}(?:айфон|iphone)\b|\bтринадцатый\s*айфон\b){NO_SUFFIX}", [128, 256, 512], "iPhone 13"),

    ("iPhone 12 Pro Max", "apple", rf"\b12{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [128, 256, 512], "iPhone 12 Pro Max"),
    ("iPhone 12 Pro", "apple", rf"\b12{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512], "iPhone 12 Pro"),
    ("iPhone 12 mini", "apple", rf"\b12{SEP}(?:mini|мини)\b", [64, 128, 256], "iPhone 12 mini"),
    ("iPhone 12", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}12\b|\b12{SEP}(?:айфон|iphone)\b|\bдвенадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 12"),

    ("iPhone 11 Pro Max", "apple", rf"\b11{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [64, 256, 512], "iPhone 11 Pro Max"),
    ("iPhone 11 Pro", "apple", rf"\b11{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [64, 256, 512], "iPhone 11 Pro"),
    ("iPhone 11", "apple", rf"(?:(?:iphone|айфон|айф|apple|эпл){IPHONE_BRIDGE}11\b|\b11{SEP}(?:айфон|iphone)\b|\bодиннадцатый\s*айфон\b){NO_SUFFIX}", [64, 128, 256], "iPhone 11"),

    ("iPhone XS Max", "apple", rf"\b(?:xs|хс|икс{SEP}эс){SEP}(?:max|макс)\b", [64, 256, 512], "iPhone XS Max"),
    ("iPhone XS", "apple", rf"\b(?:xs|хс|икс{SEP}эс)\b(?!{SEP}(?:max|макс))", [64, 256, 512], "iPhone XS"),
    ("iPhone XR", "apple", rf"\b(?:xr|хр|икс{SEP}эр|хr)\b", [64, 128, 256], "iPhone XR"),
    ("iPhone SE 2022", "apple", rf"\bse{SEP}(?:2022|3|22)\b", [64, 128, 256], "iPhone SE 2022"),
    ("iPhone SE 2020", "apple", rf"\bse{SEP}(?:2020|2|20)\b", [64, 128, 256], "iPhone SE 2020"),

    # --- SAMSUNG (актуальные S22–S25 и A54–A56) ---
    ("Samsung S25 Ultra", "android", rf"\bs{SEP}25{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S25 Ultra"),
    ("Samsung S25 Plus", "android", rf"\bs{SEP}25{SEP}{PLUS_MOD}", [256, 512], "Samsung S25 Plus"),
    ("Samsung S25", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{TIGHT_SEP}25\b{NO_SUFFIX}", [128, 256, 512], "Samsung S25"),

    ("Samsung S24 Ultra", "android", rf"\bs{SEP}24{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S24 Ultra"),
    ("Samsung S24 Plus", "android", rf"\bs{SEP}24{SEP}{PLUS_MOD}", [256, 512], "Samsung S24 Plus"),
    ("Samsung S24 FE", "android", rf"\bs{SEP}24{SEP}(?:fe|фе)\b", [128, 256, 512], "Samsung S24 FE"),
    ("Samsung S24", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{TIGHT_SEP}24\b{NO_SUFFIX}", [128, 256], "Samsung S24"),

    ("Samsung S23 Ultra", "android", rf"\bs{SEP}23{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S23 Ultra"),
    ("Samsung S23 Plus", "android", rf"\bs{SEP}23{SEP}{PLUS_MOD}", [256, 512], "Samsung S23 Plus"),
    ("Samsung S23 FE", "android", rf"\bs{SEP}23{SEP}(?:fe|фе)\b", [128, 256], "Samsung S23 FE"),
    ("Samsung S23", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{TIGHT_SEP}23\b{NO_SUFFIX}", [128, 256], "Samsung S23"),

    ("Samsung S22 Ultra", "android", rf"\bs{SEP}22{SEP}(?:ultra|ультра)\b", [128, 256, 512], "Samsung S22 Ultra"),
    ("Samsung S22 Plus", "android", rf"\bs{SEP}22{SEP}{PLUS_MOD}", [128, 256], "Samsung S22 Plus"),
    ("Samsung S22", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{TIGHT_SEP}22\b{NO_SUFFIX}", [128, 256], "Samsung S22"),

    ("Samsung A56", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){TIGHT_SEP}56\b{NO_UNIT_AFTER}", [128, 256], "Samsung A56"),
    ("Samsung A55", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){TIGHT_SEP}55\b{NO_UNIT_AFTER}", [128, 256], "Samsung A55"),
    ("Samsung A54", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){TIGHT_SEP}54\b{NO_UNIT_AFTER}", [128, 256], "Samsung A54"),

    # --- XIAOMI / REDMI / POCO (ходовые серии) ---
    ("Redmi Note 14 Pro Plus", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14{SEP}(?:pro|про){SEP}{PLUS_MOD}", [256, 512], "Redmi Note 14 Pro Plus"),
    ("Redmi Note 14 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14{SEP}(?:pro|про)\b(?!{SEP}{PLUS_MOD})", [128, 256, 512], "Redmi Note 14 Pro"),
    ("Redmi Note 14", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14\b{NO_SUFFIX}", [128, 256], "Redmi Note 14"),

    ("Redmi Note 13 Pro Plus", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про){SEP}{PLUS_MOD}", [256, 512], "Redmi Note 13 Pro Plus"),
    ("Redmi Note 13 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про)\b(?!{SEP}{PLUS_MOD})", [128, 256, 512], "Redmi Note 13 Pro"),
    ("Redmi Note 13", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13\b{NO_SUFFIX}", [128, 256], "Redmi Note 13"),

    ("Xiaomi 14T Pro", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}14{TIGHT_SEP}(?:t|т){SEP}(?:pro|про)\b", [256, 512, 1024], "Xiaomi 14T Pro"),
    ("Xiaomi 14T", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}14{TIGHT_SEP}(?:t|т)\b(?!\.){NO_SUFFIX}", [256, 512], "Xiaomi 14T"),
    ("Xiaomi 14", "android", rf"(?<!redmi\s)(?<!редми\s)(?<!note\s)(?<!ноут\s)\b(?:xiaomi|сяоми|ксяоми|mi|ми){SEP}14\b{NO_SUFFIX}", [256, 512], "Xiaomi 14"),
    ("Xiaomi 13T Pro", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}13{TIGHT_SEP}(?:t|т){SEP}(?:pro|про)\b", [256, 512, 1024], "Xiaomi 13T Pro"),
    ("Xiaomi 13T", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}13{TIGHT_SEP}(?:t|т)\b(?!\.){NO_SUFFIX}", [256], "Xiaomi 13T"),
    ("Xiaomi 13", "android", rf"(?<!redmi\s)(?<!редми\s)(?<!note\s)(?<!ноут\s)\b(?:xiaomi|сяоми|ксяоми|mi|ми){SEP}13\b{NO_SUFFIX}", [128, 256, 512], "Xiaomi 13"),

    ("Poco F7 Pro", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}7{SEP}(?:pro|про)\b", [256, 512], "Poco F7 Pro"),
    ("Poco F7", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}7\b{NO_SUFFIX}", [256, 512], "Poco F7"),
    ("Poco F6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco F6 Pro"),
    ("Poco F6", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco F6"),
    ("Poco F5", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}5\b{NO_SUFFIX}", [256], "Poco F5"),

    ("Poco X7 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}7{SEP}(?:pro|про)\b", [256, 512], "Poco X7 Pro"),
    ("Poco X7", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}7\b{NO_SUFFIX}", [128, 256, 512], "Poco X7"),
    ("Poco X6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco X6 Pro"),
    ("Poco X6", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco X6"),

    # --- GOOGLE PIXEL (7–9 серии) ---
    ("Pixel 9 Pro XL", "android", rf"\b(?:pixel|пиксель){SEP}9{SEP}(?:pro|про){SEP}(?:xl|хл)\b", [128, 256, 512, 1024], "Pixel 9 Pro XL"),
    ("Pixel 9 Pro", "android", rf"\b(?:pixel|пиксель){SEP}9{SEP}(?:pro|про)\b(?!{SEP}(?:xl|хл))", [128, 256, 512], "Pixel 9 Pro"),
    ("Pixel 9a", "android", rf"\b(?:pixel|пиксель){SEP}9{TIGHT_SEP}(?:a|а)\b", [128, 256], "Pixel 9a"),
    ("Pixel 9", "android", rf"\b(?:pixel|пиксель){SEP}9\b{NO_SUFFIX}", [128, 256], "Pixel 9"),

    ("Pixel 8 Pro", "android", rf"\b(?:pixel|пиксель){SEP}8{SEP}(?:pro|про)\b", [128, 256, 512], "Pixel 8 Pro"),
    ("Pixel 8a", "android", rf"\b(?:pixel|пиксель){SEP}8{TIGHT_SEP}(?:a|а)\b", [128, 256], "Pixel 8a"),
    ("Pixel 8", "android", rf"\b(?:pixel|пиксель){SEP}8\b{NO_SUFFIX}", [128, 256], "Pixel 8"),
    ("Pixel 7 Pro", "android", rf"\b(?:pixel|пиксель){SEP}7{SEP}(?:pro|про)\b", [128, 256, 512], "Pixel 7 Pro"),
    ("Pixel 7a", "android", rf"\b(?:pixel|пиксель){SEP}7{TIGHT_SEP}(?:a|а)\b", [128], "Pixel 7a"),
    ("Pixel 7", "android", rf"\b(?:pixel|пиксель){SEP}7\b{NO_SUFFIX}", [128, 256], "Pixel 7"),
]

ANDROID_BRAND_WORDS = [
    r"\brealme\b", r"\bреалми\b", r"\bredmi\b", r"\bредми\b", r"\bxiaomi\b", r"\bсяоми\b",
    r"\bксяоми\b", r"\bpoco\b", r"\bпоко\b", r"\bsamsung\b", r"\bсамсунг\b", r"\bgalaxy\b",
    r"\bhonor\b", r"\bхонор\b", r"\bhuawei\b", r"\bхуавей\b", r"\binfinix\b", r"\bинфиникс\b",
    r"\btecno\b", r"\bтекно\b", r"\boppo\b", r"\bvivo\b", r"\boneplus\b", r"\bnote\b", r"\bноут\b",
    r"\bpixel\b", r"\bпиксель\b", r"\bgoogle\b", r"\bblackview\b", r"\bcubot\b", r"\bdoogee\b",
    r"\bumidigi\b", r"\bmeizu\b", r"\biqoo\b", r"\bmotorola\b", r"\bmoto\b", r"\bzte\b",
    r"\bnubia\b", r"\bnothing\b", r"\bsony\b", r"\bxperia\b", r"\bnokia\b", r"\basus\b",
    r"\blenovo\b", r"\bulefone\b", r"\boukitel\b", r"\boscal\b", r"\bitel\b", r"\bcmf\b"
]

NON_APPLE_PARAM_BRANDS = (
    "xiaomi", "redmi", "realme", "samsung", "poco", "honor", "huawei",
    "tecno", "infinix", "oppo", "vivo", "oneplus", "google", "pixel",
    "motorola", "sony", "nokia", "asus", "lenovo", "zte", "nubia",
    "meizu", "nothing", "blackview", "cubot", "doogee", "umidigi",
    "oukitel", "ulefone", "iqoo", "itel", "alcatel", "tcl"
)

ACCESSORY_TITLE_PATTERNS = [
    r"^\s*(?:чехол|стекло|коробка|кабель|провод|зарядка|блок|наушники)\b",
    r"\bчехол\s+(?:на|для|от)\b", r"\bстекло\s+(?:на|для)\b", r"\bкоробка\s+от\b"
]

RENTAL_PATTERNS = [
    r"\bаренда\b", r"\bпрокат\b"
]

BUY_INTENT_TITLE_START = r"^\s*(?:куплю|ищу|сниму|возьму)\b"

OLD_APPLE_100_BAT_MODELS = {
    "iPhone XR", "iPhone XS", "iPhone XS Max",
    "iPhone SE 2020", "iPhone SE 2022",
    "iPhone 11", "iPhone 11 Pro", "iPhone 11 Pro Max",
    "iPhone 12 mini", "iPhone 12", "iPhone 12 Pro", "iPhone 12 Pro Max"
}

# ================= 2. МАРКЕРЫ БАНА И ШТРАФОВ =================
ACCESSORY_NOUNS = r"(?:защитн\w*\s+стекл\w*|бронестекл\w*|гидрогел\w*|пленк\w*|чехол\w*|чехл\w*|кабел\w*|провод\w*|ремеш\w*|бампер\w*)"

HARMLESS_PHRASES_REGEX = re.compile(
    rf"\b{ACCESSORY_NOUNS}\s+(?:под\s+замену|тресну\w*|разби\w*|копия|реплика|с\s+трещин\w*|в\s+трещин\w*)\b"
    rf"|\b(?:под\s+замену|тресну\w*|разбит\w*|скол\w*|царапин\w*)\s+(?:на\s+|в\s+)?{ACCESSORY_NOUNS}\b"
    rf"|\bтрещин\w*\s+(?:на|в)\s+{ACCESSORY_NOUNS}\b"
    r"|\b(?:копия|реплика)\s+(?:чека|гаранти\w*|чехл\w*|коробк\w*|наушник\w*|кабел\w*|блока)\b"
    r"|\b(?:чехол|коробка|кабель|блок|наушники)\s+(?:копия|реплика)\b"
    r"|\b(?:доступ|пароль|аккаунт|номер)\s+восстановлен\w*\b"
)

DIRECT_DEAD_MARKERS = [
    "на запчасти", "не включается", "не работает", "не заряжается",
    "экран под замену", "дисплей под замену", "матрица под замену",
    "корпус под замену", "крышка под замену", "плата под замену",
    "шлейф под замену", "камера под замену", "акб под замену", "батарея под замену",
    "проблема в порте", "проблема с гнездом", "разъем под замену", "гнездо под замену",
    "отходит зарядка", "только через magsafe", "не реагирует на касания",
    "порван шлейф", "проблема со шлейфом", "поплыл экран", "перезагружается сам",
    "не видит сим", "нет сети", "не работает face", "не работает фейс",
    "без face id", "без фейс", "не оплачен", "неоплачен", "не выплачен", "невыплачен",
    "в лизинге", "телефон в залоге", "оформим в рассрочку", "продажа в рассрочку",
    "в кредит без", "ошибка акб", "ошибка аккумулятора", "неизвестная деталь",
    "короткое замыкание", "плата в кз", "кз на плате",
    "только обмен", "обмен только"
]

NEGATABLE_DEAD_MARKERS = [
    "битая крышка", "разбит", "разбита", "разбито", "разбитый",
    "трещина", "трещины", "трещин", "треснут", "треснутый",
    "утопленник", "после воды", "пятно на экране", "пятна на экране",
    "полоса на экране", "полосы на экране", "выгорание", "выгорания", "выгорел",
    "материнская плата", "бутлуп", "донор",
    "трейд ин", "trade in"
]

REPAIR_MARKERS = [
    "менялся экран", "экран менялся", "замена экрана",
    "дисплей менялся", "менялся дисплей", "замена дисплея",
    "копия", "реплика", "муляж",
    "реф", "восстановленный", "mdm", "мдм", "байпас", "bypass"
]

BATTERY_REPLACED_MARKERS = [
    "замена акб", "менялся акб", "менялась акб", "акб менялся", "акб менялась",
    "менял акб", "поменял акб", "поменяли акб", "заменил акб", "заменили акб",
    "акб заменен", "акб заменена", "акб поменян", "акб поменяна",
    "поставил новый акб", "поставлен новый акб", "установлен новый акб", "новый акб",
    "менялся аккумулятор", "аккумулятор менялся", "менял аккумулятор",
    "поменял аккумулятор", "заменил аккумулятор", "заменен аккумулятор", "новый аккумулятор",
    "новая батарея", "заменена батарея", "батарея заменена", "батарея менялась",
    "менял батарею", "поменял батарею", "поменяли батарею", "заменил батарею",
    "заменили батарею", "батарея поменяна", "установлена новая батарея",
    "поставили новую батарею", "усиленная батарея"
]

LIGHT_COSMETIC_MARKERS = [
    "потертости", "потёртости", "потертость", "потёртость",
    "микроцарапины", "микроцарапина", "мелкие царапины", "следы от чехла"
]
HEAVY_COSMETIC_MARKERS = [
    "скол", "сколы", "сколов", "сколами",
    "глубокие царапины", "царапины на экране", "царапина на экране",
    "вмятина", "вмятины", "коцки", "коцка", "требует внимания"
]

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

# Раздельные HTTP-сессии: прокси и Referer только для Куфара, Telegram напрямую
kufar_session = requests.Session()
kufar_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.kufar.by/"
})
if PROXY_URL:
    kufar_session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})

tg_session = requests.Session()


# ================= АДАПТИВНЫЙ ТАЙМЕР ДЕНЬ / НОЧЬ (UTC+3 МИНСК) =================
def get_adaptive_kufar_interval():
    minsk_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    t_val = minsk_dt.hour + minsk_dt.minute / 60.0

    if 10.0 <= t_val < 22.5:
        return random.uniform(5.0, 9.0)
    elif (7.5 <= t_val < 10.0) or (t_val >= 22.5) or (t_val < 1.0):
        return random.uniform(12.0, 18.0)
    else:
        return random.uniform(45.0, 75.0)


# ================= НАСТРОЙКИ И SQLITE БАЗА ДАННЫХ =================
def load_settings():
    s = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                s.update(data)
        except Exception as e:
            raise RuntimeError('Settings are unreadable; refusing to use silent defaults') from e
    if "anchors" not in s or not isinstance(s["anchors"], dict):
        s["anchors"] = {}
    return s


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        settings_revision(db_conn, s)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения {SETTINGS_FILE}: {e}")


def init_db():
    if not os.path.isfile(DB_FILE) and os.getenv('SNIPER_ALLOW_NEW_DB') != '1':
        raise RuntimeError('DB missing: restore a snapshot or use runner.py --initialize explicitly')
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

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
            has_defects INTEGER DEFAULT 0,
            first_seen_price REAL,
            last_seen_price REAL,
            last_drop_ref_price REAL,
            price_drops_count INTEGER DEFAULT 0,
            first_seen_ts INTEGER,
            last_seen_ts INTEGER,
            list_time_ts INTEGER,
            status TEXT DEFAULT 'active',
            days_on_market REAL DEFAULT 0,
            estimated_sold_price REAL,
            notified INTEGER DEFAULT 0,
            title TEXT,
            description TEXT
        )
    """)
    for col_sql in (
        "ALTER TABLE lot_history ADD COLUMN title TEXT",
        "ALTER TABLE lot_history ADD COLUMN description TEXT",
        "ALTER TABLE lot_history ADD COLUMN last_drop_ref_price REAL",
        "ALTER TABLE lot_history ADD COLUMN has_defects INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(col_sql)
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_cache (
            cache_key TEXT PRIMARY KEY,
            market_price REAL,
            min_price REAL,
            min_link TEXT,
            sample_size INTEGER,
            median_days REAL,
            resellers_count INTEGER,
            is_correlated INTEGER DEFAULT 0,
            updated_ts INTEGER
        )
    """)
    try:
        cur.execute("ALTER TABLE market_cache ADD COLUMN is_correlated INTEGER DEFAULT 0")
    except Exception:
        pass

    # --- ПОЛНАЯ ML-ТАБЛИЦА (КАЖДОЕ ОБЪЯВЛЕНИЕ КУФАРА БЕЗ ПОТЕРЬ И ОБРЕЗОК) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_ads (
            ad_id TEXT PRIMARY KEY,
            account_id TEXT,
            subject TEXT,
            body TEXT,
            price_byn REAL,
            first_price_byn REAL,
            price_usd REAL,
            currency TEXT,
            company_ad INTEGER,
            images_count INTEGER,
            region TEXT,
            district TEXT,
            ad_parameters_json TEXT,
            account_parameters_json TEXT,
            paid_services_json TEXT,
            model TEXT,
            memory INTEGER,
            is_assumed_mem INTEGER,
            battery_health INTEGER,
            color TEXT,
            has_box INTEGER,
            has_defects INTEGER,
            status TEXT,
            ban_reason TEXT,
            list_time_ts INTEGER,
            first_seen_ts INTEGER,
            last_seen_ts INTEGER,
            sold_ts INTEGER,
            days_on_market REAL,
            estimated_sold_price REAL,
            price_drops_count INTEGER DEFAULT 0,
            ad_link TEXT
        )
    """)

    # --- ИСТОРИЯ ИЗМЕНЕНИЯ ЦЕН ДЛЯ ML (ВРЕМЕННОЙ РЯД) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            price_byn REAL NOT NULL,
            ts INTEGER NOT NULL,
            source TEXT DEFAULT 'feed'
        )
    """)

    # Индексы для ускорения аналитики, Graveyard и ML-выгрузок
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lot_sold_stats ON lot_history (model, memory, status, last_seen_ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lot_status_seen ON lot_history (status, last_seen_ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lot_relist ON lot_history (account_id, model, first_seen_ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ml_ads_status_seen ON ml_ads (status, last_seen_ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ml_ads_model_mem ON ml_ads (model, memory)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_price_history_ad ON price_history (ad_id, ts)")

    cur.execute("UPDATE lot_history SET status = 'ignored' WHERE status = 'active' AND model IS NULL")
    conn.commit()
    migrate(conn)
    recover_outbox(conn)
    return conn


db_conn = init_db()


def get_meta(key, default=None):
    try:
        cur = db_conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_meta(key, value):
    try:
        cur = db_conn.cursor()
        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        db_conn.commit()
    except Exception:
        pass


def record_price_event(cur, ad_id, price_byn, ts, source="feed"):
    """Записывает событие цены в price_history, только если цена новая или изменилась."""
    if not ad_id or price_byn is None or price_byn <= 0:
        return
    cur.execute("SELECT price_byn FROM price_history WHERE ad_id = ? ORDER BY id DESC LIMIT 1", (ad_id,))
    last = cur.fetchone()
    if last is None or abs((last["price_byn"] or 0) - price_byn) >= 0.01:
        cur.execute(
            "INSERT INTO price_history (ad_id, price_byn, ts, source) VALUES (?, ?, ?, ?)",
            (ad_id, price_byn, int(ts), source)
        )


def parse_region_and_district(ad_params):
    region = None
    district = "Не указан"
    for param in ad_params or []:
        p = param.get("p")
        val = str(param.get("vl") or param.get("v") or "").strip()
        if not val:
            continue
        if p == "region":
            region = val
        elif p == "area":
            district = val
    return region or "Минск", district


def extract_price_usd(ad_obj, detail_obj=None):
    for src in (detail_obj, ad_obj):
        if not isinstance(src, dict):
            continue
        raw_usd = src.get("price_usd")
        if raw_usd is not None and str(raw_usd).isdigit():
            val = int(raw_usd) / 100.0
            if val > 0:
                return val
    return None


def upsert_ml_ad(
    cur, ad, detail_res=None, body=None, phone_info=None,
    bat_health=None, color_name=None, box_state=None, has_defects=None,
    status="active", ban_reason=None, now_ts=None, price_byn=None, list_ts=None
):
    """Сохраняет КАЖДУЮ запись Куфара со всеми сырыми параметрами и текстом в ml_ads."""
    ad_id = str(ad.get("ad_id") or "")
    if not ad_id:
        return
    now_ts = int(now_ts or time.time())
    if price_byn is None:
        price_byn = int(ad.get("price_byn") or 0) / 100.0
    if list_ts is None:
        list_ts = int(parse_kufar_time(ad.get("list_time", "")))

    merged_params = (detail_res.get("ad_parameters") if isinstance(detail_res, dict) and detail_res.get("ad_parameters") else ad.get("ad_parameters")) or []
    acc_params = (detail_res.get("account_parameters") if isinstance(detail_res, dict) and detail_res.get("account_parameters") else ad.get("account_parameters")) or []
    paid_services = (detail_res.get("paid_services") if isinstance(detail_res, dict) and detail_res.get("paid_services") else ad.get("paid_services")) or {}
    images = (detail_res.get("images") if isinstance(detail_res, dict) and detail_res.get("images") else ad.get("images")) or []

    region, district = parse_region_and_district(merged_params)
    company_raw = detail_res.get("company_ad") if (isinstance(detail_res, dict) and "company_ad" in detail_res) else ad.get("company_ad")
    company_ad = 1 if company_raw else (0 if company_raw is not None else None)
    price_usd = extract_price_usd(ad, detail_res)
    currency = ad.get("currency") or (detail_res.get("currency") if isinstance(detail_res, dict) else None) or "BYN"
    subject = (ad.get("subject") or (detail_res.get("subject") if isinstance(detail_res, dict) else "") or "").strip()
    full_body = body.strip() if isinstance(body, str) else None
    link = ad.get("ad_link") or f"https://www.kufar.by/item/{ad_id}"

    # box_state: 1 = явно есть, 0 = явно нет, -1 / None = не упомянуто -> NULL для ML
    ml_box = box_state if box_state in (0, 1) else None
    ml_defects = (1 if has_defects else 0) if has_defects is not None else None
    model = phone_info["model"] if phone_info else None
    memory = phone_info["memory"] if phone_info else None
    is_assumed_mem = (1 if phone_info["is_assumed_mem"] else 0) if phone_info else None

    cur.execute("""
        INSERT INTO ml_ads (
            ad_id, account_id, subject, body, price_byn, first_price_byn, price_usd, currency,
            company_ad, images_count, region, district, ad_parameters_json, account_parameters_json,
            paid_services_json, model, memory, is_assumed_mem, battery_health, color,
            has_box, has_defects, status, ban_reason, list_time_ts, first_seen_ts, last_seen_ts, ad_link
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ad_id) DO UPDATE SET
            subject = COALESCE(NULLIF(excluded.subject, ''), ml_ads.subject),
            body = COALESCE(excluded.body, ml_ads.body),
            price_byn = excluded.price_byn,
            price_usd = COALESCE(excluded.price_usd, ml_ads.price_usd),
            company_ad = COALESCE(excluded.company_ad, ml_ads.company_ad),
            images_count = COALESCE(excluded.images_count, ml_ads.images_count),
            region = COALESCE(excluded.region, ml_ads.region),
            district = COALESCE(excluded.district, ml_ads.district),
            ad_parameters_json = CASE
                WHEN excluded.ad_parameters_json IS NOT NULL AND excluded.ad_parameters_json != '[]'
                THEN excluded.ad_parameters_json ELSE ml_ads.ad_parameters_json END,
            account_parameters_json = CASE
                WHEN excluded.account_parameters_json IS NOT NULL AND excluded.account_parameters_json != '[]'
                THEN excluded.account_parameters_json ELSE ml_ads.account_parameters_json END,
            paid_services_json = CASE
                WHEN excluded.paid_services_json IS NOT NULL AND excluded.paid_services_json != '{}'
                THEN excluded.paid_services_json ELSE ml_ads.paid_services_json END,
            model = COALESCE(excluded.model, ml_ads.model),
            memory = COALESCE(excluded.memory, ml_ads.memory),
            is_assumed_mem = COALESCE(excluded.is_assumed_mem, ml_ads.is_assumed_mem),
            battery_health = COALESCE(excluded.battery_health, ml_ads.battery_health),
            color = COALESCE(excluded.color, ml_ads.color),
            has_box = COALESCE(excluded.has_box, ml_ads.has_box),
            has_defects = COALESCE(excluded.has_defects, ml_ads.has_defects),
            status = excluded.status,
            ban_reason = excluded.ban_reason,
            last_seen_ts = excluded.last_seen_ts
    """, (
        ad_id, str(ad.get("account_id") or ""), subject, full_body, price_byn, price_byn, price_usd, currency,
        company_ad, len(images), region, district,
        json.dumps(merged_params, ensure_ascii=False),
        json.dumps(acc_params, ensure_ascii=False),
        json.dumps(paid_services, ensure_ascii=False),
        model, memory, is_assumed_mem, bat_health, color_name,
        ml_box, ml_defects, status, ban_reason, list_ts, now_ts, now_ts, link
    ))
    record_price_event(cur, ad_id, price_byn, now_ts, source="feed")


def close_db():
    global db_conn
    try:
        if db_conn:
            db_conn.commit()
            db_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            db_conn.execute("PRAGMA journal_mode=DELETE;")
            db_conn.commit()
            db_conn.close()
            db_conn = None
    except Exception:
        pass


def handle_shutdown_signal(signum, frame):
    global RUNNING
    RUNNING = False


atexit.register(close_db)
signal.signal(signal.SIGTERM, handle_shutdown_signal)
signal.signal(signal.SIGINT, handle_shutdown_signal)


# ================= ТЕКСТОВЫЕ АНАЛИЗАТОРЫ И ФИЛЬТРЫ =================
def clean_text(text):
    if not text:
        return ""
    t = text.replace("\u00a0", " ").lower().replace("ё", "е")
    t = re.sub(r"\b(?:ifon|iphon|ipone|ipohne|aifon|айфоша)\b", "iphone", t)
    t = re.sub(r"\b(?:sumsung|samsyng|самс)\b", "samsung", t)
    t = re.sub(r"\b(?:ксиоми|ксяоми|сяоми|ксиаоми)\b", "xiaomi", t)
    return t


def strip_harmless_phrases(text):
    return HARMLESS_PHRASES_REGEX.sub(" ", clean_text(text))


def has_direct_marker(text, marker):
    t = strip_harmless_phrases(text)
    return bool(re.search(rf"\b{re.escape(marker)}\b", t))


IRRELEVANT_NEGATIONS_REGEX = re.compile(
    r"\b(?:торга|обмена|бартера|коробки|комплекта|зарядки|блока|кабеля|чехла|наушников|доставки|пересыла|спешки|времени)\s+нет\b"
    r"|\bнет\s+(?:торга|обмена|бартера|коробки|комплекта|зарядки|чехла|наушников|времени|смысла)\b"
    r"|\bне\s+(?:спешу|торгуюсь|меняю|обмениваю|интерисует|интересует|интересен|отправляю|высылаю|предлагать|беспокоить)\b"
)


def has_marker_without_negation(text, marker):
    t = strip_harmless_phrases(text)
    neg_pattern = r"\b(?:без|не|нет|никаких|никакого|никакой|отсутствуют|отсутствует|ничего\s+не|нужно\s+без)\b"
    # Поддерживаем перечисления вроде «трещин и сколов нет»
    after_neg_pattern = (
        r"^(?:\s*(?:и|,|/|или)\s*[а-яa-z0-9\-]+){0,3}\s+"
        r"(?:не\s+было|нет|не\s+обнаружено|не\s+имеет|отсутствуют|отсутствует|не\s+менял\w*)\b"
    )

    for m in re.finditer(rf"\b{re.escape(marker)}\b", t):
        start_idx = m.start()
        end_idx = m.end()

        prefix = t[max(0, start_idx - 55):start_idx]
        clause = re.split(r"[.!?\n]", prefix)[-1]
        clause = re.split(r"(?:,\s*|\s+)(?:но|однако|зато|правда|только|а(?!\s+также))\b", clause)[-1]
        clause = IRRELEVANT_NEGATIONS_REGEX.sub(" ", clause)

        if re.search(neg_pattern, clause):
            continue

        suffix = t[end_idx:min(len(t), end_idx + 45)]
        suffix_clause = re.split(r"[.!?\n]", suffix)[0]
        if re.search(after_neg_pattern, suffix_clause):
            continue

        return True

    return False


def check_ad_parameters_defects(ad_params):
    if not ad_params:
        return None, None

    for p in ad_params:
        p_key = str(p.get("p") or "").lower()
        p_label = str(p.get("pl") or "").lower()
        p_val = str(p.get("vl") or p.get("v") or "").lower().strip()

        if not p_val:
            continue

        is_state_or_defect_param = (
            p_key in ("condition", "state", "phablet_phones_state", "mobile_phones_state",
                      "defects", "phablet_phones_defects", "mobile_phones_defects", "damage")
            or any(w in p_label for w in ("состояние", "дефект", "нюанс", "неисправн", "работоспособн", "поврежд"))
        )

        if not is_state_or_defect_param:
            continue

        if any(bad in p_val for bad in ("требует ремонта", "на запчасти", "неисправн", "не работает", "не включается")):
            return "ban", f"Параметр Куфара «{p.get('pl') or p_key}»: {p_val}"

        if any(ok in p_val for ok in ("без дефект", "нет дефект", "отличное", "идеальное", "хорошее", "новое")):
            continue

        if any(def_w in p_val for def_w in ("дефект", "нюанс", "поврежд", "разбит", "трещин", "скол", "царапин", "выгоран")) or (
            ("дефект" in p_label or "поврежд" in p_label) and p_val in ("да", "есть", "имеются", "присутствуют", "1", "true")
        ):
            return "defect", f"параметр «{p_val}» (-8%)"

    return None, None


def extract_battery_health(text):
    t = clean_text(text)
    m = re.search(
        r"(?:акб|батаре[яиюей]|емкость|состояние\s*(?:акб|батаре[яиюей]))[^\d]{0,15}\b(\d{2,3})\b"
        r"(?:\s*%|(?!\s*(?:гб|gb|р\b|руб|byn|mah|мач|вт|w\b|цикл)))",
        t
    )
    if not m:
        m = re.search(r"\b(6\d|7\d|8\d|9\d|100)\s*%\s*(?:акб|батаре[яиюей]|емкост\w*|родн\w*)", t)
    if m:
        val = int(m.group(1))
        if 50 <= val <= 100:
            return val
    return None


def check_ban_markers(title, description, phone_type="apple", model_id=None, ad_params=None):
    t_title = clean_text(title)
    full = clean_text(f"{title} {description}")

    param_status, param_reason = check_ad_parameters_defects(ad_params)
    if param_status == "ban":
        return param_reason

    for pat in ACCESSORY_TITLE_PATTERNS:
        if re.search(pat, t_title):
            return f"Аксессуар в заголовке: {pat}"

    for pat in RENTAL_PATTERNS:
        if re.search(pat, full):
            return f"Аренда/Прокат: {pat}"

    if re.search(BUY_INTENT_TITLE_START, t_title):
        return "Объявление о покупке (Куплю/Ищу в начале заголовка)"
    if not model_id and re.search(r"\b(?:куплю|ищу)\b", t_title):
        return "Объявление о покупке (Куплю/Ищу)"

    for m in DIRECT_DEAD_MARKERS:
        if has_direct_marker(full, m):
            return f"Прямой маркер брака: '{m}'"

    for m in NEGATABLE_DEAD_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер брака: '{m}'"

    for m in REPAIR_MARKERS:
        if has_marker_without_negation(full, m):
            return f"Маркер ремонта/копии: '{m}'"

    if re.search(r"\b(?:icloud|айклауд|айклауде)\b", full):
        safe_icloud = re.search(
            r"\b(?:чист\w*|отвязан\w*|отвяжу|выйду|выключен\w*|свобод\w*|мой|личный)\b.{0,30}\b(?:icloud|айклауд)"
            r"|\b(?:icloud|айклауд\w*)\b.{0,30}\b(?:чист\w*|отвязан\w*|отвяжу|выйду|выключен\w*|свобод\w*|нет|без)\b",
            full
        )
        if not safe_icloud:
            return "Подозрение на блокировку iCloud (нет слов 'чистый/отвязан/выйду')"

    if has_marker_without_negation(full, "заблокирован") or has_marker_without_negation(full, "на пароле"):
        return "Заблокирован / На пароле"

    if phone_type == "apple" and model_id in OLD_APPLE_100_BAT_MODELS:
        bat_val = extract_battery_health(full)
        if bat_val == 100:
            has_honest_bat_replace = any(has_marker_without_negation(full, bm) for bm in BATTERY_REPLACED_MARKERS)
            if not has_honest_bat_replace:
                return f"Подозрительные 100% АКБ на {model_id} без указания замены банки"

    return None


def extract_color(title, description=""):
    discount_colors = [
        ("красный", r"\b(?:красн\w*|red|product\s*red)\b"),
        ("розовый", r"\b(?:розов\w*|pink)\b"),
        ("желтый", r"\b(?:желт\w*|yellow)\b"),
        ("коралловый", r"\b(?:кораллов\w*|coral)\b"),
    ]
    premium_colors = [
        ("черный", r"\b(?:черн\w*|black|midnight)\b"),
        ("белый", r"\b(?:бел\w*|white|starlight)\b"),
        ("графит", r"\b(?:графит\w*|graphite)\b"),
        ("синий", r"\b(?:син(?:ий|его|ем|яя|юю|им|ие)|blue)\b"),
        ("фиолетовый", r"\b(?:фиолетов\w*|purple)\b"),
        ("зеленый", r"\b(?:зелен\w*|green)\b"),
        ("титан", r"\b(?:титан\w*|titanium)\b"),
    ]

    t_title = clean_text(title)
    for cname, pat in premium_colors:
        if re.search(pat, t_title):
            return cname, "standard"
    for cname, pat in discount_colors:
        if re.search(pat, t_title):
            return cname, "discount"

    t_desc = clean_text(description)
    acc_color_re = re.compile(
        r"\b(?:красн\w*|розов\w*|желт\w*|кораллов\w*|red|pink|yellow|черн\w*|бел\w*|син\w*|зелен\w*|фиолетов\w*)\s+"
        r"(?:чех\w*|кабел\w*|провод\w*|ремеш\w*|бампер\w*|накладк\w*|коробк\w*)\b"
        r"|\b(?:чех\w*|кабел\w*|провод\w*|ремеш\w*|бампер\w*|накладк\w*|коробк\w*)\s+"
        r"(?:красн\w*|розов\w*|желт\w*|кораллов\w*|red|pink|yellow|черн\w*|бел\w*|син\w*|зелен\w*|фиолетов\w*)\b"
    )
    t_desc = acc_color_re.sub(" ", t_desc)

    for cname, pat in premium_colors:
        if re.search(pat, t_desc):
            return cname, "standard"
    for cname, pat in discount_colors:
        if re.search(pat, t_desc):
            return cname, "discount"

    return "не указан", "standard"


def detect_box_state(text):
    """
    Возвращает троичный признак комплекта для ML:
    0  = явно без коробки
    1  = явно есть коробка / полный комплект
    -1 = не указано
    """
    t = clean_text(text)
    no_box_patterns = [
        r"\bнет\s+коробк[иа]\b", r"\bбез\s+коробк[иа]\b",
        r"\bкоробк[аи]\s+не\s+сохранил\w*\b", r"\bкоробк[аи]\s+утерян\w*\b",
        r"\bтолько\s+телефон\b", r"\bв\s+комплекте\s+только\s+(?:телефон|провод|зарядка)\b"
    ]
    for p in no_box_patterns:
        if re.search(p, t):
            return 0

    yes_box_patterns = [
        r"\bполн\w*\s+комплект\w*\b", r"\bродн\w*\s+коробк\w*\b",
        r"\bкоробк[аеу]\s+(?:имеется|есть|в\s+наличии|в\s+комплекте)\b",
        r"\bс\s+коробк\w*\b", r"\bв\s+коробк\w*\b"
    ]
    for p in yes_box_patterns:
        if re.search(p, t):
            return 1

    return -1


def strip_secondary_device_mentions(text):
    pattern = (
        r"\b(?:переш(?:ел|ла|ли)\s+на|перехожу\s+на|обмен\s+на|обменяю\s+на|чехол\s+(?:от|на|для)|стекло\s+(?:от|на|для))\s+"
        r"(?:[а-яa-z]+\s+){0,2}"
        r"(?:iphone|айфон|apple|эпл|samsung|самсунг|galaxy|redmi|редми|xiaomi|сяоми|poco|поко|pixel|пиксель)?[\s\-_.]*"
        r"(?:(?:s|с|a|а|x|х|f|ф|m|м|note|ноут|xr|хр|xs|хс|se)[\s\-_.]*)?"
        r"\d{0,2}(?:[\s\-_.]*(?:pro|про|max|макс|mini|мини|plus|плюс|\+|ultra|ультра|fe|фе|xl|хл|t|т|e|е|a|а))*"
        r"(?:\s*(?:на\s*)?(?:64|128|256|512|1024)\s*(?:гб|gb|гиг)?)?"
    )
    return re.sub(pattern, " ", text)


def extract_memory(title, description, ad_params, valid_memories):
    sorted_mems = sorted(valid_memories, reverse=True)

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

    t_title = clean_text(title)

    if 1024 in valid_memories and re.search(r"\b(?:1024|1\s*(?:tb|тб|терабайт))\b", t_title):
        return 1024, False

    slash_title = re.search(r"\b(?:4|6|8|12|16)\s*/\s*(64|128|256|512|1024)\b", t_title)
    if slash_title and int(slash_title.group(1)) in valid_memories:
        return int(slash_title.group(1)), False

    for mem in sorted_mems:
        if re.search(rf"\b{mem}\s*(?:гб|gb|гиг|г\b|g\b)|\bна\s*{mem}\b", t_title):
            return mem, False

    for mem in sorted_mems:
        pattern = rf"(?<!дом\s)(?<!кв\s)(?<!квартира\s)\b{mem}\b(?!\s*(?:р\b|руб|byn|дол|уе|\$|%|вт|w\b|дн|мп|mp|mah|мач|гц|hz))"
        if re.search(pattern, t_title):
            return mem, False

    t_desc = strip_secondary_device_mentions(clean_text(description))

    if 1024 in valid_memories and re.search(r"\b(?:1024|1\s*(?:tb|тб|терабайт))\b", t_desc):
        return 1024, False

    slash_desc = re.search(r"\b(?:4|6|8|12|16)\s*/\s*(64|128|256|512|1024)\b", t_desc)
    if slash_desc and int(slash_desc.group(1)) in valid_memories:
        return int(slash_desc.group(1)), False

    for mem in sorted_mems:
        if re.search(rf"\b{mem}\s*(?:гб|gb|гиг|г\b|g\b)|\bна\s*{mem}\b", t_desc):
            return mem, False

    return valid_memories[0], True


def deduplicate_models(found_list):
    unique_map = {}
    for item in found_list:
        if item[0] not in unique_map or item[4] < unique_map[item[0]][4]:
            unique_map[item[0]] = item
    return list(unique_map.values())


def normalize_phone(title, description="", ad_params=None):
    t_title = clean_text(title)
    t_full = clean_text(f"{title} {description}")

    param_brand = ""
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("brand", "mobile_phones_brand", "phablet_phones_brand"):
                param_brand = str(p.get("vl") or p.get("v") or "").lower().strip()

    has_android_param = bool(
        param_brand and "apple" not in param_brand and any(b in param_brand for b in NON_APPLE_PARAM_BRANDS)
    )
    has_android_in_title = any(re.search(w, t_title) for w in ANDROID_BRAND_WORDS)
    has_android_in_body = any(re.search(w, t_full) for w in ANDROID_BRAND_WORDS)

    has_strong_apple_cue = (
        bool(re.search(
            rf"\b(?:iphone|айфон|айф|apple|эпл|ios|айос|xs|хс|xr|хр|16{TIGHT_SEP}[eе]|face\s*id|фейс\s*айди|true\s*tone|тру\s*тон|icloud|айклауд|magsafe|магсейф)\b",
            t_full
        ))
        or ("apple" in param_brand)
    )
    has_soft_apple_cue = bool(re.search(rf"\b(?:pro{SEP}max|про{SEP}макс|promax|промакс|pro|про|mini|мини|se)\b", t_full))

    allow_apple_title = (
        not has_android_in_title
        and not has_android_param
        and (has_strong_apple_cue or (has_soft_apple_cue and not has_android_in_body))
    )
    allow_apple_body = (
        not has_android_in_title
        and not has_android_in_body
        and not has_android_param
        and (has_strong_apple_cue or has_soft_apple_cue)
    )

    temp_title = strip_secondary_device_mentions(t_title)
    found_in_title = []

    for model_id, m_type, regex, mem_list, query_str in MODELS_WHITELIST:
        if m_type == "apple" and not allow_apple_title:
            continue

        m_obj = re.search(regex, temp_title)
        if m_obj:
            found_in_title.append((model_id, m_type, mem_list, query_str, m_obj.start()))
            temp_title = re.sub(regex, " ", temp_title)

    found_in_title = deduplicate_models(found_in_title)

    if len(found_in_title) > 1:
        return None, f"Мульти-лот в заголовке: {', '.join(x[0] for x in found_in_title)}"

    if len(found_in_title) == 1:
        model_id, m_type, mem_list, query_str, _ = found_in_title[0]
    else:
        found_in_body = []
        clean_body_source = strip_secondary_device_mentions(t_full)
        temp_body = clean_body_source

        for m_id, m_t, regex, m_l, q_s in MODELS_WHITELIST:
            if m_t == "apple" and not allow_apple_body:
                continue
            m_obj = re.search(regex, temp_body)
            if m_obj:
                orig_match = re.search(regex, clean_body_source)
                pos = orig_match.start() if orig_match else m_obj.start()
                found_in_body.append((m_id, m_t, m_l, q_s, pos))
                temp_body = re.sub(regex, " ", temp_body)

        found_in_body = deduplicate_models(found_in_body)

        if len(found_in_body) >= 1:
            found_in_body.sort(key=lambda x: x[4])
            model_id, m_type, mem_list, query_str, _ = found_in_body[0]
        else:
            is_acc_or_rent = (
                any(re.search(p, t_title) for p in ACCESSORY_TITLE_PATTERNS)
                or any(re.search(p, t_full) for p in RENTAL_PATTERNS)
            )
            if HAS_RAPIDFUZZ and t_title.strip() and not is_acc_or_rent:
                fuzz_title = re.sub(r"\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", " ", t_title)
                choices = {m[0]: m for m in MODELS_WHITELIST if not (m[1] == "apple" and not allow_apple_title)}
                if choices:
                    best = fuzz_process.extractOne(fuzz_title, list(choices.keys()), scorer=fuzz.token_set_ratio, score_cutoff=92)
                    if best:
                        cand_name = best[0].lower()

                        cand_nums = re.findall(r"\d+", cand_name)
                        title_nums = re.findall(r"\d+", fuzz_title)
                        if not all(n in title_nums for n in cand_nums):
                            return None, "Модель не в Белом списке (несовпадение поколения)"

                        suffix_words = [
                            ("pro", "про"), ("max", "макс"), ("plus", "плюс", "+"),
                            ("ultra", "ультра"), ("mini", "мини"), ("fe", "фе"), ("xl", "хл")
                        ]
                        mismatch = False
                        for s_group in suffix_words:
                            in_cand = any(re.search(rf"\b{re.escape(s)}\b", cand_name) for s in s_group if s != "+")
                            in_title = any(re.search(rf"\b{re.escape(s)}\b", t_title) for s in s_group if s != "+") or ("+" in s_group and "+" in t_title)
                            if in_cand != in_title:
                                mismatch = True
                                break

                        if not mismatch:
                            for tight_s in (r"\d[eе]\b", r"\d[tт]\b", r"\d[aа]\b"):
                                if bool(re.search(tight_s, cand_name)) != bool(re.search(tight_s, t_title)):
                                    mismatch = True
                                    break

                        if not mismatch:
                            model_id, m_type, mem_list, query_str = choices[best[0]][:4]
                        else:
                            return None, "Модель не в Белом списке (несовпадение модификации)"
                    else:
                        return None, "Модель не в Белом списке"
                else:
                    return None, "Модель не в Белом списке"
            else:
                return None, "Модель не в Белом списке"

    memory, is_assumed_mem = extract_memory(title, description, ad_params, mem_list)
    return {
        "model": model_id,
        "type": m_type,
        "memory": memory,
        "valid_memories": mem_list,
        "is_assumed_mem": is_assumed_mem,
        "query": query_str,
        "key": f"{model_id}_{memory}GB"
    }, None


# ================= 3. МАТЕМАТИКА РЫНКА (ВИТРИНА 5%/35% + ЧИСТАЯ БАЗА ПРОДАЖ) =================
def parse_kufar_time(iso_str):
    if not iso_str:
        return time.time()
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()


def get_model_sample_floor(model_name):
    if model_name.startswith("iPhone 16"):
        return 650
    if model_name.startswith("iPhone 15"):
        return 500
    if model_name.startswith("iPhone 14"):
        return 380
    if model_name.startswith("iPhone 13"):
        return 300
    if model_name.startswith("iPhone 12"):
        return 240
    if model_name.startswith("iPhone 11"):
        return 200
    if model_name.startswith(("Samsung S25", "Samsung S24 Ultra", "Pixel 9 Pro")):
        return 500
    if model_name.startswith(("Samsung S24", "Samsung S23 Ultra", "Pixel 9", "Pixel 8 Pro")):
        return 350
    if model_name.startswith(("Samsung S23", "Pixel 8")):
        return 260
    return 130


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


def get_sold_market_stats(model, memory, showcase_price=None):
    # No independently confirmed transactions are available. Disappearances
    # and old heuristic prices MUST NOT calibrate the asking-price estimate.
    return None, 0, 0


def blend_with_sold_history(showcase_price, sold_med, sold_cnt, fast_cnt):
    return showcase_price, 0


def fetch_kufar_sample(phone_info, target_memory, region_minsk=True):
    weighted_prices = []
    days_list = []
    seen_sample_ids = set()
    min_lot = (99999, "")
    rgn_param = "&rgn=7" if region_minsk else ""
    now = time.time()
    min_floor = get_model_sample_floor(phone_info["model"])

    mem_query = f"{phone_info['query']} {target_memory}"
    queries_to_run = [mem_query, phone_info["query"]] if len(phone_info["valid_memories"]) > 1 else [phone_info["query"]]

    for q_idx, q_str in enumerate(queries_to_run):
        if q_idx == 1 and len(weighted_prices) >= 22:
            break

        cursor = None
        pages_limit = 1 if len(queries_to_run) > 1 else 2

        for _ in range(pages_limit):
            url = (
                f"https://api.kufar.by/search-api/v2/search/rendered-paginated"
                f"?cat=17010&cmp=0&lang=ru&prc=r%3A12000%2C550000{rgn_param}"
                f"&query={requests.utils.quote(q_str)}&size=50&sort=lst.d"
            )
            if cursor:
                url += f"&cursor={requests.utils.quote(cursor, safe='')}"

            for attempt in range(2):
                try:
                    resp = kufar_session.get(url, timeout=8)
                    if resp.status_code == 429:
                        time.sleep(4)
                        continue
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    ads = data.get("ads", [])
                    for ad in ads:
                        ad_id = str(ad.get("ad_id") or "")
                        if not ad_id or ad_id in seen_sample_ids:
                            continue

                        subj = ad.get("subject", "")
                        p_byn = int(ad.get("price_byn") or 0) / 100.0
                        if p_byn < min_floor or p_byn > 5500:
                            continue

                        ad_params = ad.get("ad_parameters", [])
                        if check_ban_markers(subj, "", phone_info["type"], phone_info["model"], ad_params):
                            continue
                        if any(has_marker_without_negation(subj, m) for m in HEAVY_COSMETIC_MARKERS + ["нюанс", "дефект", "срочно"]):
                            continue

                        norm, _ = normalize_phone(subj, "", ad_params)
                        if not norm or norm["model"] != phone_info["model"] or norm["memory"] != target_memory:
                            continue

                        seen_sample_ids.add(ad_id)
                        list_ts = parse_kufar_time(ad.get("list_time", ""))
                        days_old = max(0.0, (now - list_ts) / 86400.0)
                        days_list.append(days_old)

                        weight = max(0.30, 1.0 - 0.02 * days_old)
                        if norm["is_assumed_mem"] and len(phone_info["valid_memories"]) > 1:
                            weight *= 0.5

                        weighted_prices.append((p_byn, weight))

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

    return weighted_prices, days_list, min_lot


def compute_raw_market_for_mem(phone_info, target_memory):
    w_prices, days_list, min_lot = fetch_kufar_sample(phone_info, target_memory, region_minsk=True)

    if len(w_prices) < 15:
        rb_w, rb_days, rb_min = fetch_kufar_sample(phone_info, target_memory, region_minsk=False)
        if len(rb_w) > len(w_prices):
            w_prices, days_list, min_lot = rb_w, rb_days, rb_min

    n = len(w_prices)
    if n < MIN_VALID_SAMPLE:
        return None, n, 0, min_lot

    w_prices.sort(key=lambda x: x[0])
    if n >= 7:
        cut_low = max(1, int(n * 0.05))
        cut_high = max(1, int(n * 0.35))
        core = w_prices[cut_low: n - cut_high]
        if not core:
            core = w_prices
    else:
        core = w_prices

    raw_med = round(weighted_median(core))
    med_days = round(statistics.median(days_list), 1) if days_list else 3.0
    return raw_med, n, med_days, min_lot


def get_market_data(phone_info, settings, force_refresh=False):
    cache_key = phone_info["key"]
    now = int(time.time())
    cur = db_conn.cursor()

    anchor_val = settings.get("anchors", {}).get(cache_key)
    bf = settings.get("bargain_factor", 1.00)
    target_mem = phone_info["memory"]

    if not force_refresh:
        cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        if row and (now - row["updated_ts"]) < MARKET_CACHE_TTL:
            showcase_price = row["market_price"]
            sold_med, sold_cnt, fast_cnt = get_sold_market_stats(phone_info["model"], target_mem, showcase_price)
            blended_raw, sold_weight_pct = blend_with_sold_history(showcase_price, sold_med, sold_cnt, fast_cnt)
            if blended_raw is not None or anchor_val:
                scaled_market = round(blended_raw * bf) if blended_raw is not None else anchor_val
                p = anchor_val if anchor_val else scaled_market
                return {
                    "price": p,
                    "showcase_price": showcase_price,
                    "sold_median": sold_med,
                    "sold_count": sold_cnt,
                    "fast_sold_count": fast_cnt,
                    "sold_weight_pct": sold_weight_pct,
                    "min_price": row["min_price"] or p,
                    "min_link": row["min_link"] or "",
                    "sample_size": row["sample_size"] or 0,
                    "median_days": row["median_days"] if row["median_days"] is not None else "нет данных",
                    "is_correlated": bool(row["is_correlated"]) if "is_correlated" in row.keys() else False,
                    "is_anchored": bool(anchor_val)
                }

    valid_mems = phone_info["valid_memories"]
    raw_market_val, n, med_days, min_lot = compute_raw_market_for_mem(phone_info, target_mem)
    is_correlated = False

    base_mem = 128 if 128 in valid_mems else valid_mems[0]
    if target_mem != base_mem:
        base_key = f"{phone_info['model']}_{base_mem}GB"
        base_val, base_n, base_days = None, 0, 3.0

        if not force_refresh:
            cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (base_key,))
            base_row = cur.fetchone()
            if base_row and base_row["market_price"] and (now - base_row["updated_ts"]) < MARKET_CACHE_TTL:
                base_val = base_row["market_price"]
                base_n = base_row["sample_size"]
                base_days = base_row["median_days"]

        if base_val is None:
            base_val, base_n, base_days, b_min = compute_raw_market_for_mem(phone_info, base_mem)
            if base_val and base_n >= MIN_VALID_SAMPLE:
                cur.execute("""
                    INSERT OR REPLACE INTO market_cache
                    (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, is_correlated, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
                """, (base_key, base_val, b_min[0] if b_min[0] < 99999 else base_val, b_min[1], base_n, base_days, now))
                db_conn.commit()

        if base_val and base_n >= 5:
            mem_steps = {64: 0, 128: 1, 256: 2, 512: 3, 1024: 4}
            step_diff = mem_steps.get(target_mem, 1) - mem_steps.get(base_mem, 1)
            correlated_val = round(base_val * (1.0 + 0.08 * step_diff))

            if raw_market_val is None or n < MIN_VALID_SAMPLE:
                raw_market_val = correlated_val
                n = base_n
                med_days = base_days
                is_correlated = True
            elif target_mem > base_mem and raw_market_val < round(base_val * 1.04):
                raw_market_val = correlated_val
                is_correlated = True
            elif target_mem < base_mem and raw_market_val > round(base_val * 0.95):
                raw_market_val = correlated_val
                is_correlated = True

    sold_med, sold_cnt, fast_cnt = get_sold_market_stats(phone_info["model"], target_mem, raw_market_val)
    blended_raw, sold_weight_pct = blend_with_sold_history(raw_market_val, sold_med, sold_cnt, fast_cnt)

    if blended_raw is None and not anchor_val:
        return None
    if blended_raw is None:
        blended_raw = anchor_val

    if n < MIN_VALID_SAMPLE and sold_cnt < MIN_VALID_SAMPLE and not anchor_val:
        return None

    cached_min_p = min_lot[0] if min_lot[0] < 99999 else (raw_market_val or blended_raw)
    cur.execute("""
        INSERT OR REPLACE INTO market_cache
        (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, is_correlated, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (cache_key, raw_market_val, cached_min_p, min_lot[1], n, med_days if med_days is not None else 0.0, 1 if is_correlated else 0, now))
    db_conn.commit()

    scaled_price = round(blended_raw * bf)
    final_price = anchor_val if anchor_val else scaled_price
    return {
        "price": final_price,
        "showcase_price": raw_market_val,
        "sold_median": sold_med,
        "sold_count": sold_cnt,
        "fast_sold_count": fast_cnt,
        "sold_weight_pct": sold_weight_pct,
        "min_price": min_lot[0] if min_lot[0] < 99999 else final_price,
        "min_link": min_lot[1],
        "sample_size": n,
        "median_days": med_days if med_days is not None else 0.0,
        "is_correlated": is_correlated,
        "is_anchored": bool(anchor_val)
    }


def adjust_lot_market_price(base_market, full_text, phone_type, bat_health, color_tier, has_box, param_defect_note=None):
    multiplier = 1.0
    notes = []
    has_defects = False

    bat_replaced_hit = next((bm for bm in BATTERY_REPLACED_MARKERS if has_marker_without_negation(full_text, bm)), None)
    if bat_replaced_hit:
        multiplier -= 0.07
        notes.append("заменен АКБ (-7%)")
        has_defects = True
    elif phone_type == "apple" and bat_health is not None:
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
    elif param_defect_note:
        multiplier -= 0.08
        notes.append(param_defect_note)
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


# ================= 4. TELEGRAM API, ИНТЕРФЕЙС И HTML-ОТЧЕТ ПО БАЗЕ (/db) =================
def visible_html_len(html_text):
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain)


def send_telegram(text, photo_url=None, with_sound=False, reply_markup=None):
    if not TG_TOKEN:
        print("⚠️ TG_TOKEN не задан!")
        return False

    disable_sound = not with_sound
    markup_json = json.dumps(reply_markup) if reply_markup else None

    for attempt in range(2):
        try:
            if photo_url and visible_html_len(text) <= 1020:
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
                r = tg_session.post(url, data=payload, timeout=10)
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
            r = tg_session.post(url, data=payload, timeout=10)
            if r.status_code == 200:
                return True
            elif r.status_code == 429:
                retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(retry_after)
        except Exception as e:
            print(f"⚠️ Ошибка Telegram API: {type(e).__name__}")
            time.sleep(2)
    return False


def send_notification(payload):
    if not TG_TOKEN or not TG_CHAT_ID:
        return dict(ok=False, error='telegram_credentials_missing', retry_after=300)
    if payload.get('ad_id'):
        _, details, ok = get_ad_details(payload['ad_id'])
        if not ok:
            return dict(ok=False, error='preflight_details_unavailable')
        status = str(details.get('status') or details.get('ad_status') or '').lower()
        if details.get('is_active') is False or status in ('deleted','removed','sold','inactive','archived','deactivated'):
            with db_conn:
                mark_unavailable(db_conn, payload['ad_id'], int(time.time()), details.get('_unavailable_reason', 'preflight_inactive'))
            return dict(cancelled=True, error='ad_unavailable')
        if details.get('price_byn') is not None:
            current = int(details['price_byn']) / 100.0
            if current <= 0 or abs(current - payload['asking_price']) >= 0.01:
                cached = db_conn.execute('SELECT payload FROM feed_inbox WHERE ad_id=?', (payload['ad_id'],)).fetchone()
                if cached:
                    ad = json.loads(cached[0])
                    ad['price_byn'] = str(round(current * 100))
                    with db_conn:
                        remember_ads(db_conn, [ad], source='detail_refresh')
                return dict(cancelled=True, error='asking_price_changed')
    photo = payload.get('photo_url') if visible_html_len(payload['text']) <= 1020 else None
    method = 'sendPhoto' if photo else 'sendMessage'
    data = dict(chat_id=TG_CHAT_ID, parse_mode='HTML', disable_notification=not payload.get('with_sound'))
    data.update(dict(photo=photo, caption=payload['text']) if photo else dict(text=payload['text'], disable_web_page_preview=True))
    try:
        response = tg_session.post(f'https://api.telegram.org/bot{TG_TOKEN}/{method}', data=data, timeout=10)
        result = response.json()
        if response.status_code == 200 and result.get('ok') is True:
            return dict(ok=True, message_id=result.get('result', {}).get('message_id'))
        return dict(ok=False, error=f"telegram_http_{response.status_code}",
                    retry_after=result.get('parameters', {}).get('retry_after', 0),
                    drop_photo=bool(photo and response.status_code == 400))
    except Exception as exc:
        return dict(ok=False, error='delivery_unknown_' + type(exc).__name__)


def send_telegram_document(file_path, caption=""):
    if not TG_TOKEN or not os.path.exists(file_path):
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
        with open(file_path, "rb") as f:
            r = tg_session.post(
                url,
                data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"document": (os.path.basename(file_path), f)},
                timeout=90
            )
        return r.status_code == 200
    except Exception as e:
        print(f"⚠️ Ошибка отправки файла: {type(e).__name__}")
        return False


def init_telegram_update_id():
    saved = get_meta("tg_last_update_id")
    if saved is not None and str(saved).isdigit():
        return int(saved)

    if not TG_TOKEN:
        return 0
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-1&timeout=0"
        r = tg_session.get(url, timeout=8)
        if r.status_code == 200:
            res = r.json().get("result", [])
            if res:
                uid = res[-1]["update_id"]
                set_meta("tg_last_update_id", uid)
                return uid
    except Exception:
        pass
    return 0


def get_main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Оценка / Тест-драйв"}, {"text": "📈 Отчет за сегодня"}],
            [{"text": "🏘 Районы Минска"}, {"text": "⚙️ Настройки / Якоря"}],
            [{"text": "📦 База / Стата"}]
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


def format_desc_for_html(title, desc):
    t = (title or "").strip()
    d = (desc or "").strip()
    if not t and not d:
        return "<span class='muted'>—</span>"
    parts = []
    if t:
        parts.append(f"<b>{html.escape(t)}</b>")
    if d:
        short_d = d[:260] + ("..." if len(d) > 260 else "")
        parts.append(html.escape(short_d).replace("\n", " "))
    return "<br>".join(parts)


def generate_html_db_report(settings):
    from reporting import generate_report
    report_path = os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), 'kufar_db_report.html')
    return generate_report(db_conn, settings, report_path)


def send_db_report(settings):
    try:
        report_path, stats = generate_html_db_report(settings)
        pending = db_conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE state!='sent' AND state!='cancelled'").fetchone()[0]
        send_telegram(f"📦 <b>База:</b> {stats['total']} записей; активны {stats['active']}; недоступны {stats['unavailable']}.\n"
                      f"Ожидают отправки: {pending}.\nПродажи и цены сделок не подтверждены.")
        send_telegram_document(report_path, 'Наблюдения и цены объявлений')
        # SQLite backup gives a consistent copy even while the bot is running.
        from state_store import make_snapshot
        from pathlib import Path
        snapshot = make_snapshot(Path(DB_FILE), Path(SETTINGS_FILE), Path(DB_FILE).resolve().parent / 'backups')
        if snapshot.stat().st_size < 49_000_000:
            send_telegram_document(str(snapshot), 'Проверенный снимок SQLite и настроек')
        else:
            send_telegram('Снимок больше лимита отправки. Он сохранён в каталоге backups.')
    except Exception as exc:
        print(f"⚠️ Отчёт: {type(exc).__name__}")


def handle_telegram_updates(last_update_id, settings, force=False):
    global LAST_TG_POLL_TS
    if not TG_TOKEN:
        return last_update_id

    now_f = time.time()
    if not force and (now_f - LAST_TG_POLL_TS) < TG_POLL_INTERVAL:
        return last_update_id
    LAST_TG_POLL_TS = now_f

    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=0"
    try:
        r = tg_session.get(url, timeout=8)
        if r.status_code != 200:
            return last_update_id
        updates = r.json().get("result", [])
        for u in updates:
            last_update_id = u["update_id"]
            set_meta("tg_last_update_id", last_update_id)

            if "callback_query" in u:
                cb = u["callback_query"]
                cb_id = cb.get("id")
                chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if chat_id != str(TG_CHAT_ID):
                    continue

                if cb_id:
                    try:
                        tg_session.post(
                            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                            data={"callback_query_id": cb_id},
                            timeout=5
                        )
                    except Exception:
                        pass

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
                    tg_session.post(edit_url, data={
                        "chat_id": TG_CHAT_ID,
                        "message_id": cb["message"]["message_id"],
                        "text": f"🏘 <b>Выбор районов Минска</b>\nАктивно: <b>{active_str}</b>\n<i>(Лоты с выгодой &gt;30 BYN придут из любого района с пометкой)</i>",
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
                    "🤖 <b>Панель управления Kufar Sniper 4.0</b>\nВыбери действие на клавиатуре ниже:",
                    reply_markup=get_main_menu_keyboard()
                )

            elif text in ("/db", "📦 База / Стата"):
                USER_WAIT_STATE["mode"] = None
                send_db_report(settings)

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
                    f"• Бюджет уведомлений в TG: <b>{settings['min_price']} – {settings['max_price']} BYN</b>\n"
                    f"• Сбор ML-базы в фоне: <b>{SCAN_MIN_PRICE} – {max(SCAN_MAX_PRICE, settings['max_price'])} BYN</b>\n"
                    f"• Коэффициент рынка: <b>{settings['bargain_factor']}</b>\n"
                    f"• Порог ЖИРА: <b>{settings['fat_profit_byn']} BYN / {settings['fat_profit_pct']}%</b>\n"
                    f"• Мин. выгода под осмотр: <b>{settings['inspect_min_profit']} BYN</b>\n"
                    f"• Заданные якоря: <b>{anc_str}</b>\n\n"
                    f"<b>Команды для изменения:</b>\n"
                    f"• <code>цена 120 750</code> — изменить бюджет уведомлений\n"
                    f"• <code>коэф 1.00</code> — изменить коэффициент рынка (0.5–1.5)\n"
                    f"• <code>жир 75 20</code> — порог жира (75 BYN и 20%)\n"
                    f"• <code>якорь iPhone 12 64 340</code> — жестко зафиксировать рынок модели (0 чтобы удалить)"
                )

            elif text == "📊 Оценка / Тест-драйв":
                USER_WAIT_STATE["mode"] = "price_check"
                send_telegram(
                    "🔎 <b>Узнать оценку / Тест цены</b>\n\n"
                    "Напиши модель и память (через пробел можешь добавить свою цену для проверки).\n"
                    "Примеры:\n"
                    "• <code>iPhone 13 128</code>\n"
                    "• <code>iPhone 12 64 350</code>\n"
                    "• <code>S24 1200</code>"
                )

            elif text == "📈 Отчет за сегодня":
                USER_WAIT_STATE["mode"] = None
                send_daily_report()

            elif USER_WAIT_STATE["mode"] == "settings_input":
                low = text.lower()
                if low.startswith("цена"):
                    parts = re.findall(r"\d+", low)
                    if len(parts) >= 2:
                        min_p, max_p = int(parts[0]), int(parts[1])
                        if 10 <= min_p < max_p <= 50000:
                            settings["min_price"] = min_p
                            settings["max_price"] = max_p
                            save_settings(settings)
                            send_telegram(
                                f"✅ Новый бюджет уведомлений в TG: <b>{settings['min_price']} – {settings['max_price']} BYN</b>\n"
                                f"<i>(Сбор статистики в базу продолжается до {max(SCAN_MAX_PRICE, settings['max_price'])} BYN)</i>"
                            )
                            USER_WAIT_STATE["mode"] = None
                        else:
                            send_telegram("❌ Ошибка: минимальная цена должна быть меньше максимальной (от 10 до 50000 BYN).")
                    else:
                        send_telegram("❌ Укажи два числа, например: <code>цена 120 750</code>")
                elif low.startswith("коэф"):
                    m = re.search(r"(\d+(?:\.\d+)?)", low)
                    if m:
                        val = float(m.group(1))
                        if 0.5 <= val <= 1.5:
                            settings["bargain_factor"] = val
                            save_settings(settings)
                            send_telegram(f"✅ Новый коэффициент рынка: <b>{settings['bargain_factor']}</b>")
                            USER_WAIT_STATE["mode"] = None
                        else:
                            send_telegram("❌ Коэффициент должен быть в диапазоне от <b>0.5</b> до <b>1.5</b>.")
                    else:
                        send_telegram("❌ Пример: <code>коэф 0.95</code>")
                elif low.startswith("жир"):
                    parts = re.findall(r"\d+", low)
                    if len(parts) >= 2:
                        byn_v, pct_v = int(parts[0]), int(parts[1])
                        if 10 <= byn_v <= 2000 and 5 <= pct_v <= 80:
                            settings["fat_profit_byn"] = byn_v
                            settings["fat_profit_pct"] = pct_v
                            save_settings(settings)
                            send_telegram(f"✅ Новый порог ЖИРА: <b>{settings['fat_profit_byn']} BYN / {settings['fat_profit_pct']}%</b>")
                            USER_WAIT_STATE["mode"] = None
                        else:
                            send_telegram("❌ Порог BYN должен быть 10–2000, процент — 5–80%.")
                    else:
                        send_telegram("❌ Пример: <code>жир 80 20</code>")
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
                            USER_WAIT_STATE["mode"] = None
                        else:
                            send_telegram(f"❌ Не удалось распознать модель ({html.escape(str(err))}).")
                    else:
                        send_telegram("❌ Пример: <code>якорь iPhone 12 64 340</code>")
                else:
                    send_telegram("❌ Неизвестная команда настроек. Используй <code>цена</code>, <code>коэф</code>, <code>жир</code> или <code>якорь</code>.")
                    USER_WAIT_STATE["mode"] = None

            elif USER_WAIT_STATE["mode"] == "price_check":
                handle_interactive_price_check(text, settings)
                USER_WAIT_STATE["mode"] = None

    except Exception as e:
        print(f"⚠️ Ошибка getUpdates: {type(e).__name__}")
    return last_update_id


def handle_interactive_price_check(query_text, settings):
    tokens = query_text.strip().split()
    user_test_price = None

    if len(tokens) >= 2 and tokens[-1].isdigit():
        last_val = int(tokens[-1])
        prefix_query = " ".join(tokens[:-1])
        prefix_info, _ = normalize_phone(prefix_query)
        if prefix_info and last_val >= 50:
            is_mem_already_explicit = not prefix_info["is_assumed_mem"]
            is_not_valid_mem_or_year = (last_val not in prefix_info["valid_memories"]) and (last_val not in (2020, 2022))
            if is_mem_already_explicit or is_not_valid_mem_or_year:
                user_test_price = last_val
                query_text = prefix_query

    phone_info, err = normalize_phone(query_text)
    if not phone_info:
        send_telegram(f"❌ Не удалось распознать модель из белого списка ({html.escape(str(err))}). Например: <code>iPhone 13 128</code>")
        return

    send_telegram(f"⏳ Сканирую цены активных объявлений Куфара для <b>{phone_info['model']} ({phone_info['memory']} GB)</b>...")
    m_data = get_market_data(phone_info, settings, force_refresh=True)
    if not m_data:
        send_telegram("⚠️ Слишком мало активных объявлений (меньше 3 шт.) для расчета.")
        return

    med = m_data["price"]
    showcase_p = m_data["showcase_price"]
    fast_buy = round(med * 0.80)
    bargain_buy = round(med * 0.88)
    min_p = m_data["min_price"]
    min_link = html.escape(m_data["min_link"] or "https://www.kufar.by")
    anc_note = " ⚓ <i>(Зафиксировано якорем)</i>" if m_data.get("is_anchored") else ""
    corr_note = " <i>(корреляция памяти)</i>" if m_data.get("is_correlated") else ""
    low_sample_warn = f"\n⚠️ <b>Мало данных для рынка (n={m_data['sample_size']})</b>" if m_data["sample_size"] < 8 and m_data["sold_count"] < 6 else ""

    if showcase_p is not None:
        showcase_line = f"• 📐 По формуле (витрина 5%/35%): <b>~{showcase_p:.0f} BYN</b> (n={m_data['sample_size']})\n"
    else:
        showcase_line = f"• 📐 Витрина: <i>мало объявлений (n={m_data['sample_size']})</i>\n"

    if m_data["sold_count"] > 0 and m_data["sold_median"]:
        sold_line = (
            f"• 📊 Подтверждённые сделки (30 дн.): <b>~{m_data['sold_median']:.0f} BYN</b> "
            f"({m_data['sold_count']} прод., ⚡≤1д: {m_data['fast_sold_count']}, вес {m_data['sold_weight_pct']}%)\n"
        )
    else:
        sold_line = "• 📊 Подтверждённые сделки (30 дн.): <i>пока нет данных</i>\n"

    resp = (
        f"📊 <b>Аналитика: {phone_info['model']} ({phone_info['memory']} GB)</b>{low_sample_warn}\n\n"
        f"{showcase_line}"
        f"{sold_line}"
        f"• 💎 <b>Оценка по ценам объявлений: ~{med:.0f} BYN</b>{corr_note}{anc_note}\n\n"
        f"• 🚀 Комфортный выкуп (жир -20%): <b>до {fast_buy} BYN</b>\n"
        f"• 🤝 Потолок выкупа (-12%): <b>до {bargain_buy} BYN</b>\n"
        f"• 📉 Минимум на Куфаре сейчас: <a href='{min_link}'><b>{min_p:.0f} BYN (открыть)</b></a>\n"
        f"• ⏱ Медианный возраст выборки активных объявлений: <b>~{m_data['median_days']} дн.</b> (не срок продажи)"
    )

    if user_test_price:
        diff = user_test_price - med
        if diff > 25:
            advice = f"⚠️ Твоя цена (<b>{user_test_price} BYN</b>) выше рынка на {diff:.0f} р. Рекомендую снизить до <b>{med - 10:.0f}–{med:.0f} BYN</b>."
        elif diff >= -15:
            advice = f"✅ Твоя цена (<b>{user_test_price} BYN</b>) в рынке! Гипотеза цены для проверки спроса: <b>{med - 25:.0f} BYN</b>."
        else:
            advice = f"🔥 Твоя цена (<b>{user_test_price} BYN</b>) ниже рынка на {abs(diff):.0f} р.! Скорость продажи неизвестна."
        resp += f"\n🧪 <b>Тест-драйв твоей цены:</b>\n{advice}"

    send_telegram(resp)


def send_daily_report():
    since = int(time.time()) - 86400
    new_count = db_conn.execute('SELECT COUNT(*) FROM ml_ads WHERE first_seen_ts>=?', (since,)).fetchone()[0]
    gone = db_conn.execute("SELECT COUNT(*) FROM availability_history WHERE observation='unavailable' AND observed_ts>=?", (since,)).fetchone()[0]
    pending = db_conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE state IN ('pending','sending')").fetchone()[0]
    send_telegram(f"📈 <b>Наблюдения за 24 часа:</b>\nНовых объявлений: {new_count}.\n"
                  f"Проверок с недоступностью: {gone}. Это не подтверждённые продажи.\n"
                  f"Уведомлений в очереди: {pending}.")


def check_graveyard_and_digest(settings, last_update_id):
    now = int(time.time())
    cur = db_conn.cursor()

    # 0. Ультра-свежие лоты (моложе 2 ч) проверяем раз в 10 мин — чтобы работал removed_mistake (<15 мин)
    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, price_drops_count, list_time_ts, first_seen_ts, last_seen_ts
        FROM lot_history
        WHERE status = 'active' AND model IS NOT NULL
          AND (? - first_seen_ts) <= 2 * 3600
          AND (? - last_seen_ts) > 600
        ORDER BY last_seen_ts ASC
        LIMIT 10
    """, (now, now))
    ultra_fresh_rows = cur.fetchall()

    # 1. Свежие лоты (2–48 ч) проверяем раз в 30 мин
    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, price_drops_count, list_time_ts, first_seen_ts, last_seen_ts
        FROM lot_history
        WHERE status = 'active' AND model IS NOT NULL
          AND (? - first_seen_ts) > 2 * 3600
          AND (? - first_seen_ts) <= 48 * 3600
          AND (? - last_seen_ts) > 1800
        ORDER BY last_seen_ts ASC
        LIMIT 15
    """, (now, now, now))
    fresh_rows = cur.fetchall()

    # 2. Более старые лоты проверяем раз в 4 часа
    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, price_drops_count, list_time_ts, first_seen_ts, last_seen_ts
        FROM lot_history
        WHERE status = 'active' AND model IS NOT NULL
          AND (? - first_seen_ts) > 48 * 3600
          AND (? - last_seen_ts) > 4 * 3600
        ORDER BY last_seen_ts ASC
        LIMIT 15
    """, (now, now))
    older_rows = cur.fetchall()

    stale_rows = list(ultra_fresh_rows) + list(fresh_rows) + list(older_rows)

    gone_cnt = 0
    alive_cnt = 0
    price_upd_cnt = 0
    err_cnt = 0

    for row in stale_rows:
        if not RUNNING:
            break
        watchdog()
        last_update_id = handle_telegram_updates(last_update_id, settings)

        ad_id = row["ad_id"]
        url = f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru"
        try:
            r = kufar_session.get(url, timeout=6)
            is_gone = False
            is_alive = False
            res_dict = None

            if r.status_code == 429:
                print("⚠️ Graveyard получил HTTP 429 — прерываем пачку и делаем паузу 5 сек.")
                time.sleep(5)
                break
            elif r.status_code in (404, 410):
                is_gone = True
            elif r.status_code == 200:
                data = r.json()
                res = data.get("result")
                if not isinstance(res, dict) or not res:
                    raise RetryAd("invalid_item_response")
                elif isinstance(res, dict):
                    res_dict = res
                    st = str(res.get("status") or res.get("ad_status") or "").lower()
                    if res.get("is_active") is False or st in ("deleted", "removed", "sold", "inactive", "archived", "deactivated"):
                        is_gone = True
                    else:
                        is_alive = True
                else:
                    raise RetryAd("invalid_item_response")
            else:
                err_cnt += 1
                print(f"⚠️ Graveyard {ad_id}: неожиданный HTTP {r.status_code}")

            if is_gone:
                gone_cnt += 1
                evidence = f"http_{r.status_code}" if r.status_code != 200 else "api_inactive"
                mark_unavailable(db_conn, ad_id, now, evidence)
                print(f"🪦 Лот {ad_id}: недоступен ({evidence}); факт продажи неизвестен.")

            elif is_alive:
                mark_active(db_conn, ad_id, now)
                alive_cnt += 1
                old_p = row["last_seen_price"] or 0
                new_p = old_p
                if res_dict and res_dict.get("price_byn") is not None:
                    try:
                        parsed_p = int(res_dict.get("price_byn")) / 100.0
                        if parsed_p > 0:
                            new_p = parsed_p
                    except Exception:
                        pass

                if new_p > 0 and abs(new_p - old_p) >= 1.0:
                    price_upd_cnt += 1
                    drops_inc = 1 if (old_p - new_p) >= 5.0 else 0
                    cur.execute("""
                        UPDATE lot_history
                        SET last_seen_ts = ?, last_seen_price = ?,
                            last_drop_ref_price = CASE WHEN ? = 1 THEN ? ELSE COALESCE(last_drop_ref_price, ?) END,
                            price_drops_count = COALESCE(price_drops_count, 0) + ?
                        WHERE ad_id = ?
                    """, (now, new_p, drops_inc, new_p, new_p, drops_inc, ad_id))
                    cur.execute("""
                        UPDATE ml_ads
                        SET last_seen_ts = ?, price_byn = ?, price_drops_count = COALESCE(price_drops_count, 0) + ?
                        WHERE ad_id = ?
                    """, (now, new_p, drops_inc, ad_id))
                    record_price_event(cur, ad_id, new_p, now, source="graveyard")
                    cached = cur.execute("SELECT payload FROM feed_inbox WHERE ad_id=?", (ad_id,)).fetchone()
                    if cached:
                        changed = json.loads(cached[0])
                        changed['price_byn'] = str(round(new_p * 100))
                        remember_ads(db_conn, [changed], now, source='detail_refresh')
                else:
                    cur.execute("UPDATE lot_history SET last_seen_ts = ? WHERE ad_id = ?", (now, ad_id))
                    cur.execute("UPDATE ml_ads SET last_seen_ts = ? WHERE ad_id = ?", (now, ad_id))

            db_conn.commit()
        except Exception as e:
            err_cnt += 1
            print(f"⚠️ Ошибка проверки {ad_id}: {type(e).__name__}")
        time.sleep(0.25)

    # 3. Лёгкий трекинг жизненного цикла нецелевых моделей (Honor/Realme/Huawei и др.) для ML
    cur.execute("""
        SELECT ad_id, price_byn, list_time_ts, first_seen_ts, last_seen_ts
        FROM ml_ads
        WHERE status = 'ignored' AND model IS NULL
          AND (? - first_seen_ts) <= 14 * 86400
          AND (? - last_seen_ts) > 6 * 3600
        ORDER BY last_seen_ts ASC
        LIMIT 8
    """, (now, now))
    ml_other_rows = cur.fetchall()
    for mrow in ml_other_rows:
        if not RUNNING:
            break
        watchdog()
        last_update_id = handle_telegram_updates(last_update_id, settings)
        ad_id = mrow["ad_id"]
        try:
            r = kufar_session.get(f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru", timeout=6)
            if r.status_code == 429:
                break
            is_gone = r.status_code in (404, 410)
            res_dict = None
            if r.status_code == 200:
                res = r.json().get("result")
                if not isinstance(res, dict) or not res:
                    raise RetryAd("invalid_item_response")
                elif isinstance(res, dict):
                    res_dict = res
                    st = str(res.get("status") or res.get("ad_status") or "").lower()
                    if res.get("is_active") is False or st in ("deleted", "removed", "sold", "inactive", "archived", "deactivated"):
                        is_gone = True
            if is_gone:
                evidence = f"http_{r.status_code}" if r.status_code != 200 else "api_inactive"
                mark_unavailable(db_conn, ad_id, now, evidence)
            elif res_dict:
                mark_active(db_conn, ad_id, now)
                old_p = mrow["price_byn"] or 0
                new_p = old_p
                if res_dict.get("price_byn") is not None:
                    try:
                        p = int(res_dict.get("price_byn")) / 100.0
                        if p > 0:
                            new_p = p
                    except Exception:
                        pass
                if new_p > 0 and abs(new_p - old_p) >= 1.0:
                    cur.execute("UPDATE ml_ads SET price_byn = ?, last_seen_ts = ? WHERE ad_id = ?", (new_p, now, ad_id))
                    record_price_event(cur, ad_id, new_p, now, source="graveyard_ml")
                else:
                    cur.execute("UPDATE ml_ads SET last_seen_ts = ? WHERE ad_id = ?", (now, ad_id))
            db_conn.commit()
        except Exception:
            pass
        time.sleep(0.25)

    if stale_rows:
        print(
            f"🪦 Graveyard: проверено {len(stale_rows)} (свежих: {len(fresh_rows)}) | "
            f"ушло: {gone_cnt} | активно: {alive_cnt} | цен обновлено: {price_upd_cnt} | ошибок: {err_cnt}"
        )

    # Вечерний радар «Дожим висяков»
    minsk_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    today_str = minsk_dt.strftime("%Y-%m-%d")
    if minsk_dt.hour >= 20 and settings.get("last_digest_date") != today_str:
        settings["last_digest_date"] = today_str
        save_settings(settings)

        five_days_ago = now - 5 * 86400
        ten_days_ago = now - 10 * 86400
        min_notify_p = settings.get("min_price", 120)
        max_notify_p = settings.get("max_price", 750)
        cur.execute("""
            SELECT lh.ad_id, lh.model, lh.memory, lh.last_seen_price, lh.list_time_ts, mc.market_price
            FROM lot_history lh
            LEFT JOIN market_cache mc ON mc.cache_key = (lh.model || '_' || lh.memory || 'GB')
            WHERE lh.status = 'active' AND lh.model IS NOT NULL
              AND lh.last_seen_price BETWEEN ? AND ?
              AND lh.list_time_ts BETWEEN ? AND ?
              AND (mc.market_price IS NULL OR lh.last_seen_price <= mc.market_price * 1.05)
            ORDER BY
              CASE WHEN mc.market_price IS NOT NULL THEN (lh.last_seen_price / mc.market_price) ELSE 1.0 END ASC,
              lh.list_time_ts ASC
            LIMIT 5
        """, (min_notify_p, max_notify_p, ten_days_ago, five_days_ago))
        hangers = cur.fetchall()
        if hangers:
            msg = "🎯 <b>Вечерний радар «Дожим висяков» (висят 5–10 дней, в твоем бюджете):</b>\n<i>Продавцы уже созрели для торга в ЛС!</i>\n\n"
            for h in hangers:
                d_cnt = round((now - h["list_time_ts"]) / 86400.0, 1)
                link = f"https://www.kufar.by/item/{h['ad_id']}"
                mkt_str = f", рынок ~{h['market_price']:.0f}р" if h["market_price"] else ""
                msg += f"• <a href='{link}'><b>{h['model']} ({h['memory']}GB)</b></a> — {(h['last_seen_price'] or 0):.0f} BYN ({d_cnt} дн.{mkt_str})\n"
            send_telegram(msg, with_sound=False)

    return last_update_id


# ================= 6. ОСНОВНОЙ ПАРСЕР ЛЕНТЫ КУФАРА =================
def get_latest_ads(settings):
    """Read the head every cycle and resume a bounded backfill independently.

    Store each page and its next cursor atomically. Unknown/malformed responses
    never advance the cursor. A deep overlapping sweep checks older edits too.
    """
    settings_revision(db_conn, settings)
    if time.time() < float(get_meta('feed_retry_until', '0')):
        return pending_ads(db_conn)
    min_k = min(SCAN_MIN_PRICE, int(settings.get("min_price", SCAN_MIN_PRICE))) * 100
    max_k = max(SCAN_MAX_PRICE, int(settings.get("max_price", 750))) * 100
    base = ("https://api.kufar.by/search-api/v2/search/rendered-paginated"
            f"?cat=17010&cmp=0&lang=ru&prc=r%3A{min_k}%2C{max_k}&rgn=7&size={FEED_PAGE_SIZE}&sort=lst.d")
    saved_cursor = get_meta('feed_backfill_cursor', '')
    cursor = None
    max_pages = max(2, min(20, int(os.getenv('FEED_MAX_PAGES', '4'))))
    for page_idx in range(max_pages):
        url = base + (f"&cursor={requests.utils.quote(cursor, safe='')}" if cursor else '')
        try:
            response = kufar_session.get(url, timeout=10)
            if response.status_code != 200:
                failures = int(get_meta('feed_failures', '0')) + 1
                set_meta('feed_failures', failures)
                set_meta('feed_retry_until', time.time() + min(300, 15 * 2 ** min(failures, 5)))
                if cursor and response.status_code in (400, 404, 410):
                    set_meta('feed_backfill_cursor', '')
                print(f"⚠️ Лента: HTTP {response.status_code}; курсор сохранён, повтор позже.")
                break
            data = response.json()
            ads = data.get('ads')
            if not isinstance(ads, list) or not isinstance(data.get('pagination'), dict):
                raise RetryAd('invalid_feed_response')
            next_cursor = next((p.get('token') for p in data['pagination'].get('pages', [])
                                if p.get('label') == 'next'), None)
            if next_cursor and next_cursor == cursor:
                raise RetryAd('repeated_feed_cursor')
            with db_conn:
                remember_ads(db_conn, ads)
                # Head and backfill are two lanes: never overwrite an unfinished
                # backfill with a fresh head token.
                if page_idx > 0 or not saved_cursor:
                    db_conn.execute("INSERT OR REPLACE INTO meta VALUES ('feed_backfill_cursor',?)", (next_cursor or '',))
                db_conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_feed_ok_ts',?)", (str(int(time.time())),))
                db_conn.execute("INSERT OR REPLACE INTO meta VALUES ('feed_failures','0')")
            if page_idx == 0 and saved_cursor:
                cursor = saved_cursor
            else:
                cursor = next_cursor
            if not cursor:
                break
            time.sleep(0.3)
        except Exception as exc:
            set_meta('feed_retry_until', time.time() + 30)
            print(f"⚠️ Лента: {type(exc).__name__}; страница не подтверждена.")
            # A cursor rejected by the server must not stall the backfill forever.
            if 'response' in locals() and response.status_code in (400, 404, 410):
                set_meta('feed_backfill_cursor', '')
            break
    return pending_ads(db_conn)


def get_ad_details(ad_id):
    """
    За один запрос возвращает (body, full_result_dict, ok).
    При HTTP 429 делает повторную попытку вместо тихой потери описания.
    """
    url = f"https://api.kufar.by/search-api/v2/item/{ad_id}/rendered?lang=ru"
    for attempt in range(2):
        try:
            r = kufar_session.get(url, timeout=7)
            if r.status_code == 200:
                res = r.json().get("result")
                if isinstance(res, dict) and res:
                    with db_conn:
                        remember_detail(db_conn, str(ad_id), res)
                    return (res.get("body") or ""), res, True
                return "", {}, False
            elif r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            elif r.status_code in (404, 410):
                # The caller records the observation once, with its HTTP evidence.
                return "", {"is_active": False, "_unavailable_reason": f"http_{r.status_code}"}, True
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return "", {}, False


def parse_district(ad):
    for param in ad.get("ad_parameters", []):
        if param.get("p") == "area":
            val = str(param.get("vl") or param.get("v") or "")
            if val:
                return val
    return "Не указан"


def has_real_exchange_offer(text):
    t = clean_text(text)
    if not re.search(r"\b(?:обмен|обменяю|бартер)\b", t):
        return False
    no_exchange_pats = [
        r"\bобмен\w*\s+(?:не|нет)\b",
        r"\b(?:без|нет)\s+обмен\w*\b",
        r"\bне\s+(?:интересует|интерисует|рассматриваю|предлагать|нужен)\s+обмен\b",
        r"\bобмен\s+только\s+на\s+деньги\b",
        r"\bтолько\s+продажа\b"
    ]
    return not any(re.search(p, t) for p in no_exchange_pats)


# ================= ГЛАВНЫЙ ЦИКЛ =================
def process_ad(ad, settings, last_update_id):
    cur = db_conn.cursor()
    now_ts = int(time.time())
    ad_id = str(ad.get("ad_id") or "")
    if not ad_id:
        return last_update_id

    price_byn = int(ad.get("price_byn") or 0) / 100.0
    if price_byn <= 0:
        return last_update_id

    # A queued listing can be old. Verify the current item before changing any
    # asking-price history or cancelling an otherwise valid notification.
    description, detail_res, details_ok = get_ad_details(ad_id)
    if not details_ok:
        raise RetryAd('details_unavailable')
    if detail_res.get('is_active') is False or str(detail_res.get('status') or detail_res.get('ad_status') or '').lower() in ('deleted', 'removed', 'sold', 'inactive', 'archived', 'deactivated'):
        with db_conn:
            mark_unavailable(db_conn, ad_id, now_ts, detail_res.get('_unavailable_reason', 'api_inactive'))
        return last_update_id
    # Prefer the current title/attributes to an older feed payload too.
    ad = dict(ad, **{key: detail_res[key] for key in
                    ('subject', 'ad_parameters', 'images', 'account_id', 'ad_link', 'list_time')
                    if detail_res.get(key) is not None})
    if detail_res.get('price_byn') is not None:
        current_price = int(detail_res['price_byn']) / 100.0
        if current_price <= 0:
            return last_update_id
        if abs(current_price - price_byn) >= 0.01:
            ad = dict(ad, price_byn=str(round(current_price * 100)))
            price_byn = current_price
            with db_conn:
                remember_ads(db_conn, [ad], source='detail_refresh')
    with db_conn:
        cur.execute("UPDATE notification_outbox SET state='cancelled',last_error='price_changed' WHERE ad_id=? AND price!=? AND state='pending'", (ad_id, price_byn))

    list_ts = int(parse_kufar_time(ad.get("list_time", "")))
    days_old = max(0.0, (now_ts - list_ts) / 86400.0)
    account_id = str(ad.get("account_id") or "")

    cur.execute("SELECT * FROM lot_history WHERE ad_id = ?", (ad_id,))
    existing = cur.fetchone()

    price_drop_tag = ""
    drops_cnt = 0

    if existing:
        ref_price = existing["last_drop_ref_price"] or existing["last_seen_price"] or price_byn
        first_price = existing["first_seen_price"] or ref_price
        drops_cnt = existing["price_drops_count"] or 0
        notified_state = existing["notified"]

        if price_byn < ref_price and (ref_price - price_byn) >= 5:
            drops_cnt += 1
            total_drop = first_price - price_byn
            total_drop_pct = (total_drop / first_price) * 100.0 if first_price > 0 else 0

            cur.execute("""
                UPDATE lot_history
                SET last_seen_price = ?, last_drop_ref_price = ?, price_drops_count = ?,
                    last_seen_ts = ?, status = 'active', estimated_sold_price = NULL
                WHERE ad_id = ?
            """, (price_byn, price_byn, drops_cnt, now_ts, ad_id))
            cur.execute("""
                UPDATE ml_ads
                SET price_byn = ?, price_drops_count = ?, last_seen_ts = ?,
                    status = 'active', sold_ts = NULL, estimated_sold_price = NULL
                WHERE ad_id = ?
            """, (price_byn, drops_cnt, now_ts, ad_id))
            record_price_event(cur, ad_id, price_byn, now_ts, source="item_drop")
            db_conn.commit()

            if (total_drop >= 15 and total_drop_pct >= 5.0) or drops_cnt >= 2:
                price_drop_tag = (
                    f"🔥 <b>Продавец ломается! (Снижение #{drops_cnt})</b>\n"
                    f"📉 История цены: {first_price:.0f} ➡️ <b>{price_byn:.0f} BYN</b> (-{total_drop:.0f} р. / -{total_drop_pct:.0f}%)\n"
                )

        else:
            new_ref = price_byn if price_byn > ref_price else ref_price
            if existing["status"] != "active":
                cur.execute("""
                    UPDATE lot_history
                    SET last_seen_ts = ?, last_seen_price = ?, last_drop_ref_price = ?,
                        status = 'active', estimated_sold_price = NULL, days_on_market = 0
                    WHERE ad_id = ?
                """, (now_ts, price_byn, new_ref, ad_id))
                cur.execute("""
                    UPDATE ml_ads
                    SET last_seen_ts = ?, price_byn = ?, status = 'active',
                        sold_ts = NULL, estimated_sold_price = NULL, days_on_market = NULL
                    WHERE ad_id = ?
                """, (now_ts, price_byn, ad_id))
            else:
                cur.execute("""
                    UPDATE lot_history
                    SET last_seen_ts = ?, last_seen_price = ?, last_drop_ref_price = ?
                    WHERE ad_id = ?
                """, (now_ts, price_byn, new_ref, ad_id))
                cur.execute(
                    "UPDATE ml_ads SET last_seen_ts = ?, price_byn = ? WHERE ad_id = ?",
                    (now_ts, price_byn, ad_id)
                )
            record_price_event(cur, ad_id, price_byn, now_ts, source="item")
            db_conn.commit()

    raw_title = ad.get("subject", "Без названия")
    ad_params = ad.get("ad_parameters", [])

    title_norm, _ = normalize_phone(raw_title, "", ad_params)
    title_model_id = title_norm["model"] if title_norm else None
    title_phone_type = title_norm["type"] if title_norm else "apple"

    title_ban_reason = check_ban_markers(raw_title, "", title_phone_type, title_model_id, ad_params)
    if title_ban_reason:
        cur.execute("UPDATE notification_outbox SET state='cancelled',last_error='filtered' WHERE ad_id=? AND state='pending'", (ad_id,))
        cur.execute("""
            INSERT INTO lot_history
            (ad_id, account_id, model, memory, district, first_seen_price, last_seen_price, last_drop_ref_price,
             first_seen_ts, last_seen_ts, list_time_ts, status, notified, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'banned', 1, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                last_seen_ts = excluded.last_seen_ts, status = 'banned', notified = 1, title = excluded.title
        """, (
            ad_id, account_id, title_model_id,
            title_norm["memory"] if title_norm else None,
            parse_district(ad),
            price_byn, price_byn, price_byn, now_ts, now_ts, list_ts, raw_title.strip()
        ))
        upsert_ml_ad(
            cur, ad, phone_info=title_norm, status="banned", ban_reason=title_ban_reason,
            now_ts=now_ts, price_byn=price_byn, list_ts=list_ts
        )
        db_conn.commit()
        return last_update_id

    full_params = (detail_res.get("ad_parameters") if isinstance(detail_res, dict) and detail_res.get("ad_parameters") else ad_params) or []
    full_text = f"{raw_title}\n{description}"

    phone_info, norm_err = normalize_phone(raw_title, description, full_params)
    if not phone_info:
        box_st_other = detect_box_state(full_text)
        color_other, _ = extract_color(raw_title, description)
        cur.execute("""
            INSERT INTO lot_history
            (ad_id, account_id, district, first_seen_price, last_seen_price, last_drop_ref_price,
             first_seen_ts, last_seen_ts, list_time_ts, status, notified, title, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ignored', 1, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                last_seen_ts = excluded.last_seen_ts, status = 'ignored', notified = 1,
                title = excluded.title, description = excluded.description
        """, (
            ad_id, account_id, parse_district(ad),
            price_byn, price_byn, price_byn, now_ts, now_ts, list_ts,
            raw_title.strip(), description.strip()
        ))
        upsert_ml_ad(
            cur, ad, detail_res=detail_res, body=description, phone_info=None,
            color_name=color_other, box_state=box_st_other,
            status="ignored", ban_reason=norm_err,
            now_ts=now_ts, price_byn=price_byn, list_ts=list_ts
        )
        db_conn.commit()
        return last_update_id

    ban_reason = check_ban_markers(raw_title, description, phone_info["type"], phone_info["model"], full_params)
    if ban_reason:
        cur.execute("UPDATE notification_outbox SET state='cancelled',last_error='filtered' WHERE ad_id=? AND state='pending'", (ad_id,))
        print(f"⛔ [Бан: {ban_reason}] {raw_title}")
        bat_ban = extract_battery_health(full_text) if phone_info["type"] == "apple" else None
        color_ban, _ = extract_color(raw_title, description)
        box_ban = detect_box_state(full_text)
        cur.execute("""
            INSERT INTO lot_history
            (ad_id, account_id, model, memory, battery_health, district, first_seen_price, last_seen_price, last_drop_ref_price,
             first_seen_ts, last_seen_ts, list_time_ts, status, notified, title, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'banned', 1, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                model = excluded.model, memory = excluded.memory, last_seen_ts = excluded.last_seen_ts,
                status = 'banned', notified = 1, title = excluded.title, description = excluded.description
        """, (
            ad_id, account_id, phone_info["model"], phone_info["memory"], bat_ban, parse_district(ad),
            price_byn, price_byn, price_byn, now_ts, now_ts, list_ts, raw_title.strip(), description.strip()
        ))
        upsert_ml_ad(
            cur, ad, detail_res=detail_res, body=description, phone_info=phone_info,
            bat_health=bat_ban, color_name=color_ban, box_state=box_ban, has_defects=True,
            status="banned", ban_reason=ban_reason,
            now_ts=now_ts, price_byn=price_byn, list_ts=list_ts
        )
        db_conn.commit()
        return last_update_id

    bat_health = extract_battery_health(full_text) if phone_info["type"] == "apple" else None
    color_name, color_tier = extract_color(raw_title, description)
    box_state = detect_box_state(full_text)
    has_box = (box_state != 0)
    district = parse_district(ad)
    link = ad.get("ad_link") or f"https://www.kufar.by/item/{ad_id}"
    saved_title = raw_title.strip()
    saved_desc = description.strip()

    param_status, param_defect_note = check_ad_parameters_defects(full_params)
    if param_status != "defect":
        param_defect_note = None

    _, _, lot_has_defects = adjust_lot_market_price(
        1000, full_text, phone_info["type"], bat_health, color_tier, has_box, param_defect_note
    )

    cur.execute("""
        INSERT INTO lot_history
        (ad_id, account_id, model, memory, battery_health, color, district, has_box, has_defects,
         first_seen_price, last_seen_price, last_drop_ref_price, price_drops_count,
         first_seen_ts, last_seen_ts, list_time_ts, status, notified, title, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', 0, ?, ?)
        ON CONFLICT(ad_id) DO UPDATE SET
            model = excluded.model,
            memory = excluded.memory,
            battery_health = excluded.battery_health,
            color = excluded.color,
            district = excluded.district,
            has_box = excluded.has_box,
            has_defects = excluded.has_defects,
            last_seen_price = excluded.last_seen_price,
            last_drop_ref_price = excluded.last_drop_ref_price,
            last_seen_ts = excluded.last_seen_ts,
            title = excluded.title,
            description = excluded.description,
            status = 'active',
            estimated_sold_price = NULL
    """, (ad_id, account_id, phone_info["model"], phone_info["memory"], bat_health, color_name,
          district, 1 if has_box else 0, 1 if lot_has_defects else 0,
          price_byn, price_byn, price_byn, now_ts, now_ts, list_ts,
          saved_title, saved_desc))

    upsert_ml_ad(
        cur, ad, detail_res=detail_res, body=description, phone_info=phone_info,
        bat_health=bat_health, color_name=color_name, box_state=box_state, has_defects=lot_has_defects,
        status="active", ban_reason=None,
        now_ts=now_ts, price_byn=price_byn, list_ts=list_ts
    )
    mark_active(db_conn, ad_id, now_ts)
    db_conn.commit()

    if price_byn < settings.get("min_price", 120) or price_byn > settings.get("max_price", 750):
        print(f"💼 [В базу, вне бюджета TG ({settings['min_price']}–{settings['max_price']}): {price_byn:.0f} р.] {phone_info['key']}")
        return last_update_id

    m_data = get_market_data(phone_info, settings)
    if not m_data:
        raise RetryAd("market_data_unavailable")

    base_market = m_data["price"]
    adj_market, cond_note, has_defects = adjust_lot_market_price(
        base_market, full_text, phone_info["type"], bat_health, color_tier, has_box, param_defect_note
    )

    profit_byn = adj_market - price_byn
    profit_pct = (profit_byn / adj_market) * 100.0 if adj_market > 0 else 0.0

    selected_dists = settings.get("districts", [])
    outside_district_tag = ""
    if selected_dists:
        in_selected = any(d.lower() in district.lower() for d in selected_dists)
        if not in_selected:
            if profit_byn > 30:
                outside_district_tag = "📍 <b>Вне твоего района</b>\n"
            else:
                print(f"🗺 [Пропуск по району: {district}, выгода {profit_byn:+.0f} <= 30] {phone_info['key']}")
                return last_update_id

    matched_urgent = []
    for u_pat in URGENT_PATTERNS:
        m_u = re.search(u_pat, clean_text(full_text))
        if m_u:
            matched_urgent.append(m_u.group(0))
    is_urgent = len(matched_urgent) > 0

    is_scam_suspect = profit_pct >= 50.0
    with_sound = False

    if is_scam_suspect:
        tier_header = "⚠️ <b>ПОДОЗРИТЕЛЬНО ДЕШЕВО (&gt;50% ниже рынка — возможен скам/труп на фото)</b>"
        with_sound = False
    elif profit_byn >= settings["fat_profit_byn"] or profit_pct >= settings["fat_profit_pct"] or (is_urgent and profit_byn >= settings["urgent_profit_byn"]):
        tier_header = "🚨🔥 <b>ЖИРНЫЙ ЛОТ! (ЗВОНИ ПЕРВЫМ)</b> 🔥🚨"
        with_sound = True
    elif profit_byn >= settings["work_profit_byn"]:
        tier_header = "🟢 <b>РАБОЧИЙ ВАРИАНТ (Хорошая маржа)</b>"
        with_sound = False
    elif profit_byn >= settings.get("inspect_min_profit", 15) and not has_defects:
        tier_header = "👀 <b>ПОД ОСМОТР (Чистый лот в плюсе)</b>"
        with_sound = False
    else:
        print(f"📉 [Мало выгоды: {profit_byn:+.0f} BYN] {phone_info['key']} за {price_byn:.0f} р. (Рынок ~{adj_market} р.)")
        return last_update_id

    tags = []
    if phone_info["model"].startswith("iPhone 13") and bat_health == 100:
        tags.append("⚠️ <b>Проверь 100% АКБ</b>")

    if m_data["sample_size"] < 8 and m_data["sold_count"] < 6:
        tags.append(f"⚠️ <b>Мало данных для рынка (n={m_data['sample_size']})</b>")

    if days_old >= 14 and "торг" in clean_text(full_text):
        tags.append(f"💤 <b>Висяк ({days_old:.0f} дн. + торг)</b>")

    if has_real_exchange_offer(full_text):
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

    bat_str = f" | 🔋 АКБ: <b>{bat_health}%</b>" if bat_health else ""
    corr_str = " <i>(корреляция памяти)</i>" if m_data["is_correlated"] else ""
    anc_str = " ⚓" if m_data.get("is_anchored") else ""

    if m_data["showcase_price"] is not None:
        showcase_line = f"📐 Формула (витрина 5%/35%): <b>~{m_data['showcase_price']:.0f} BYN</b> (n={m_data['sample_size']})"
    else:
        showcase_line = f"📐 Витрина: <i>мало объявлений (n={m_data['sample_size']})</i>"

    if m_data["sold_median"] and m_data["sold_count"] > 0:
        ml_line = (
            f"📊 Подтверждённые сделки (30 дн.): <b>~{m_data['sold_median']:.0f} BYN</b> "
            f"({m_data['sold_count']} прод., ⚡≤1д: {m_data['fast_sold_count']}, вес {m_data['sold_weight_pct']}%)"
        )
    else:
        ml_line = "📊 Подтверждённые сделки (30 дн.): <i>пока нет данных</i>"

    msg_head = f"{tier_header}\n{outside_district_tag}{price_drop_tag}"
    if is_urgent:
        msg_head += f"⚡ Срочность: <b>{', '.join(matched_urgent).upper()}</b>\n"

    msg_head += (
        f"📱 <b>{phone_info['model']} ({phone_info['memory']} GB)</b>{bat_str}\n"
        f"🏷 {safe_title}\n"
        f"📍 Район: <b>Минск ({html.escape(district)})</b>\n"
    )
    if tags:
        msg_head += f"🏷 Теги: {' | '.join(tags)}\n"

    msg_head += (
        f"\n💰 Цена продавца: <b>{price_byn:.0f} BYN</b>\n"
        f"{showcase_line}\n"
        f"{ml_line}\n"
        f"💎 Оценка по ценам объявлений: <b>~{adj_market} BYN</b>{corr_str}{anc_str}\n"
        f"🛠 Коррекция: <i>{cond_note}</i>\n"
        f"💵 Разница с оценкой (до расходов): <b>{profit_byn:+.0f} BYN ({profit_pct:+.0f}%)</b>\n"
        f"⏱ Медианный возраст выборки активных объявлений: <b>~{m_data['median_days']} дн.</b>\n"
    )
    msg_foot = f"\n👉 <a href='{safe_link}'><b>ОТКРЫТЬ ОБЪЯВЛЕНИЕ НА КУФАРЕ</b></a>"

    raw_desc = description.strip()
    msg_desc = ""
    if raw_desc:
        base_len = visible_html_len(msg_head + msg_foot)
        max_desc_len = min(260, max(0, 990 - base_len))
        if max_desc_len >= 25:
            clipped = raw_desc[:max_desc_len] + ("..." if len(raw_desc) > max_desc_len else "")
            msg_desc = f"\n📝 <i>{html.escape(clipped)}</i>\n"

    msg = msg_head + msg_desc + msg_foot

    with db_conn:
        queue_notification(db_conn, ad_id, price_byn, msg, photo_url, with_sound)
    return last_update_id


def main():
    settings = load_settings()
    settings_revision(db_conn, settings)

    start_time = time.time()
    max_seconds = MAX_RUN_HOURS * 3600
    last_update_id = init_telegram_update_id()
    cur = db_conn.cursor()

    cur.execute("SELECT COUNT(*) as c FROM ml_ads")
    ml_start_cnt = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM lot_history")
    lot_start_cnt = cur.fetchone()["c"]
    print(
        f"🚀 Мониторинг Kufar 4.0 активен | В базе сейчас: lot_history={lot_start_cnt}, ml_ads={ml_start_cnt} | "
        f"Сбор: {SCAN_MIN_PRICE}–{max(SCAN_MAX_PRICE, settings['max_price'])} BYN, TG: {settings['min_price']}–{settings['max_price']} BYN."
    )

    next_kufar_scan_ts = 0.0
    next_graveyard_check_ts = time.time() + 60.0

    while RUNNING and (MAX_RUN_HOURS <= 0 or (time.time() - start_time) < max_seconds):
        watchdog()
        last_update_id = handle_telegram_updates(last_update_id, settings)

        drain_outbox(db_conn, send_notification)
        set_meta('heartbeat_ts', int(time.time()))
        now_f = time.time()

        if now_f >= next_graveyard_check_ts:
            last_update_id = check_graveyard_and_digest(settings, last_update_id)
            next_graveyard_check_ts = time.time() + GRAVEYARD_CHECK_INTERVAL

        if now_f < next_kufar_scan_ts:
            time.sleep(TG_POLL_INTERVAL)
            continue

        next_kufar_scan_ts = now_f + get_adaptive_kufar_interval()

        ads = get_latest_ads(settings)
        now_ts = int(time.time())

        batch_deadline = time.monotonic() + 30
        for processed, ad in enumerate(ads[:max(2,min(25,int(os.getenv('PROCESS_BATCH_SIZE','10'))))], 1):
            if not RUNNING:
                break
            watchdog()
            last_update_id = handle_telegram_updates(last_update_id, settings)
            set_meta('heartbeat_ts', int(time.time()))
            try:
                last_update_id = process_ad(ad, settings, last_update_id)
                finish_ad(db_conn, str(ad['ad_id']))
            except Exception as exc:
                db_conn.rollback()
                finish_ad(db_conn, str(ad['ad_id']), type(exc).__name__ + (':' + str(exc) if isinstance(exc, RetryAd) else ''))
                print(f"⚠️ Лот {ad.get('ad_id')}: {type(exc).__name__}; остаётся в очереди.")
            drain_outbox(db_conn, send_notification, limit=1)
            if processed >= 2 and time.monotonic() >= batch_deadline:
                break
        set_meta('heartbeat_ts', int(time.time()))

    close_db()
    print("🔄 Смена завершена корректно.")


if __name__ == "__main__":
    main()
