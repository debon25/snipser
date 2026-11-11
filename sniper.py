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

TG_POLL_INTERVAL = 1.0              # Опрос кнопок Telegram каждую 1 секунду 24/7
GRAVEYARD_CHECK_INTERVAL = 300.0    # Проверка удаленных/проданных лотов раз в 5 минут
SELLER_CACHE_TTL = 2 * 3600         # Кэш проверки профиля продавца — 2 часа
MARKET_CACHE_TTL = 4 * 3600         # Кэш рынка живет 4 часа
MIN_VALID_SAMPLE = 3                # Минимальная выборка (при <8 ставится предупреждающий тег)

MINSK_DISTRICTS = [
    "Фрунзенский", "Московский", "Первомайский", "Советский",
    "Центральный", "Заводской", "Ленинский", "Октябрьский", "Партизанский"
]

DEFAULT_SETTINGS = {
    "min_price": 120,
    "max_price": 2800,
    "bargain_factor": 1.00,       # Коэффициент к очищенной взвешенной медиане
    "districts": [],              # Пустой список = весь Минск
    "anchors": {},                # Ручные якоря цен: {"iPhone 13_128GB": 650}
    "fat_profit_byn": 85,         # Выгода для ЖИРА (со звуком)
    "fat_profit_pct": 22,         # % выгоды для ЖИРА (со звуком)
    "urgent_profit_byn": 55,      # Выгода при триггере срочности
    "work_profit_byn": 40,        # Рабочий вариант (без звука)
    "inspect_min_profit": 15,     # Под осмотр (без звука)
    "last_digest_date": ""
}

# Глобальное состояние ожидания ввода от пользователя в Telegram
USER_WAIT_STATE = {"mode": None}

# ================= 1. РАСШИРЕННЫЙ БЕЛЫЙ СПИСОК МОДЕЛЕЙ =================
SEP = r"[\s\-_.]*"
PLUS_MOD = r"(?:\+(?!\w)|(?:plus|плюс)\b)"
NO_UNIT_AFTER = r"(?!\s*(?:дн|дня|дней|мес|лет|год|р\b|руб|byn|%|шт|гб|gb|мп|mp|вт|w\b|мкр|дом|кв))"
NO_SUFFIX = rf"(?!{SEP}(?:\+(?!\w)|(?:pro|про|max|макс|mini|мини|plus|плюс|ultra|ультра|fe|фе|lite|лайт|t|т|e|е|xl|хл|a|а|s|с)\b)){NO_UNIT_AFTER}"

# Безопасный мост между словом iPhone/Apple и номером модели (без цифр и названий других моделей/iOS!)
BRIDGE_STOP = r"(?:iphone|айфон|apple|эпл|ios|айос|pro|про|max|макс|mini|мини|plus|плюс|xs|хс|xr|хр|se|гб|gb)"
IPHONE_BRIDGE = rf"(?:{SEP}|\s+(?:(?!{BRIDGE_STOP}\b)[а-яa-z]+\s+){{0,2}})(?<!ios\s)(?<!айос\s)"

MODELS_WHITELIST = [
    # --- APPLE iPHONE (от XR до 16 Pro Max + 16e) ---
    ("iPhone 16 Pro Max", "apple", rf"\b16{SEP}(?:pro{SEP}max|про{SEP}макс|промакс|promax)\b", [256, 512, 1024], "iPhone 16 Pro Max"),
    ("iPhone 16 Pro", "apple", rf"\b16{SEP}(?:pro|про)\b(?!{SEP}(?:max|макс))", [128, 256, 512, 1024], "iPhone 16 Pro"),
    ("iPhone 16 Plus", "apple", rf"\b16{SEP}{PLUS_MOD}", [128, 256, 512], "iPhone 16 Plus"),
    ("iPhone 16e", "apple", rf"\b16{SEP}(?:e|е)\b", [128, 256, 512], "iPhone 16e"),
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

    # --- SAMSUNG ---
    ("Samsung S25 Ultra", "android", rf"\bs{SEP}25{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S25 Ultra"),
    ("Samsung S25 Plus", "android", rf"\bs{SEP}25{SEP}{PLUS_MOD}", [256, 512], "Samsung S25 Plus"),
    ("Samsung S25", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}25\b{NO_SUFFIX}", [128, 256, 512], "Samsung S25"),

    ("Samsung S24 Ultra", "android", rf"\bs{SEP}24{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S24 Ultra"),
    ("Samsung S24 Plus", "android", rf"\bs{SEP}24{SEP}{PLUS_MOD}", [256, 512], "Samsung S24 Plus"),
    ("Samsung S24 FE", "android", rf"\bs{SEP}24{SEP}(?:fe|фе)\b", [128, 256, 512], "Samsung S24 FE"),
    ("Samsung S24", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}24\b{NO_SUFFIX}", [128, 256], "Samsung S24"),

    ("Samsung S23 Ultra", "android", rf"\bs{SEP}23{SEP}(?:ultra|ультра)\b", [256, 512, 1024], "Samsung S23 Ultra"),
    ("Samsung S23 Plus", "android", rf"\bs{SEP}23{SEP}{PLUS_MOD}", [256, 512], "Samsung S23 Plus"),
    ("Samsung S23 FE", "android", rf"\bs{SEP}23{SEP}(?:fe|фе)\b", [128, 256], "Samsung S23 FE"),
    ("Samsung S23", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}23\b{NO_SUFFIX}", [128, 256], "Samsung S23"),

    ("Samsung S22 Ultra", "android", rf"\bs{SEP}22{SEP}(?:ultra|ультра)\b", [128, 256, 512], "Samsung S22 Ultra"),
    ("Samsung S22 Plus", "android", rf"\bs{SEP}22{SEP}{PLUS_MOD}", [128, 256], "Samsung S22 Plus"),
    ("Samsung S22", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}22\b{NO_SUFFIX}", [128, 256], "Samsung S22"),

    ("Samsung S21 FE", "android", rf"\bs{SEP}21{SEP}(?:fe|фе)\b", [128, 256], "Samsung S21 FE"),
    ("Samsung S21 Plus", "android", rf"\bs{SEP}21{SEP}{PLUS_MOD}", [128, 256], "Samsung S21 Plus"),
    ("Samsung S21", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}s{SEP}21\b{NO_SUFFIX}", [128, 256], "Samsung S21"),
    ("Samsung S20 FE", "android", rf"\bs{SEP}20{SEP}(?:fe|фе)\b", [128, 256], "Samsung S20 FE"),

    ("Samsung A56", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}56\b", [128, 256], "Samsung A56"),
    ("Samsung A55", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}55\b", [128, 256], "Samsung A55"),
    ("Samsung A54", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}54\b", [128, 256], "Samsung A54"),
    ("Samsung A53", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}53\b", [128, 256], "Samsung A53"),
    ("Samsung A52", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}52\b", [128, 256], "Samsung A52"),
    ("Samsung A36", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}36\b", [128, 256], "Samsung A36"),
    ("Samsung A35", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}35\b", [128, 256], "Samsung A35"),
    ("Samsung A34", "android", rf"\b(?:samsung|самсунг|galaxy)?{SEP}(?:a|а){SEP}34\b", [128, 256], "Samsung A34"),

    # --- XIAOMI / REDMI / POCO ---
    ("Redmi Note 14 Pro Plus", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14{SEP}(?:pro|про){SEP}{PLUS_MOD}", [256, 512], "Redmi Note 14 Pro Plus"),
    ("Redmi Note 14 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14{SEP}(?:pro|про)\b(?!{SEP}{PLUS_MOD})", [128, 256, 512], "Redmi Note 14 Pro"),
    ("Redmi Note 14", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}14\b{NO_SUFFIX}", [128, 256], "Redmi Note 14"),

    ("Redmi Note 13 Pro Plus", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про){SEP}{PLUS_MOD}", [256, 512], "Redmi Note 13 Pro Plus"),
    ("Redmi Note 13 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13{SEP}(?:pro|про)\b(?!{SEP}{PLUS_MOD})", [128, 256, 512], "Redmi Note 13 Pro"),
    ("Redmi Note 13", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}13\b{NO_SUFFIX}", [128, 256], "Redmi Note 13"),
    ("Redmi Note 12 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}12{SEP}(?:pro|про)\b(?!{SEP}{PLUS_MOD})", [128, 256], "Redmi Note 12 Pro"),
    ("Redmi Note 12", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}12\b{NO_SUFFIX}", [64, 128, 256], "Redmi Note 12"),
    ("Redmi Note 11 Pro", "android", rf"\b(?:redmi|редми)?{SEP}(?:note|ноут){SEP}11{SEP}(?:pro|про)\b", [64, 128], "Redmi Note 11 Pro"),

    ("Xiaomi 14T Pro", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}14{SEP}(?:t|т){SEP}(?:pro|про)\b", [256, 512, 1024], "Xiaomi 14T Pro"),
    ("Xiaomi 14T", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}14{SEP}(?:t|т)\b{NO_SUFFIX}", [256, 512], "Xiaomi 14T"),
    ("Xiaomi 14", "android", rf"(?<!redmi\s)(?<!редми\s)(?<!note\s)(?<!ноут\s)\b(?:xiaomi|сяоми|ксяоми|mi|ми){SEP}14\b{NO_SUFFIX}", [256, 512], "Xiaomi 14"),
    ("Xiaomi 13T Pro", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}13{SEP}(?:t|т){SEP}(?:pro|про)\b", [256, 512, 1024], "Xiaomi 13T Pro"),
    ("Xiaomi 13T", "android", rf"\b(?:xiaomi|сяоми|ксяоми|mi|ми)?{SEP}13{SEP}(?:t|т)\b{NO_SUFFIX}", [256], "Xiaomi 13T"),
    ("Xiaomi 13", "android", rf"(?<!redmi\s)(?<!редми\s)(?<!note\s)(?<!ноут\s)\b(?:xiaomi|сяоми|ксяоми|mi|ми){SEP}13\b{NO_SUFFIX}", [128, 256, 512], "Xiaomi 13"),

    ("Poco F7 Pro", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}7{SEP}(?:pro|про)\b", [256, 512], "Poco F7 Pro"),
    ("Poco F7", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}7\b{NO_SUFFIX}", [256, 512], "Poco F7"),
    ("Poco F6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco F6 Pro"),
    ("Poco F6", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco F6"),
    ("Poco F5", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}5\b{NO_SUFFIX}", [256], "Poco F5"),
    ("Poco F4", "android", rf"\b(?:poco|поко){SEP}(?:f|ф){SEP}4\b{NO_SUFFIX}", [128, 256], "Poco F4"),

    ("Poco X7 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}7{SEP}(?:pro|про)\b", [256, 512], "Poco X7 Pro"),
    ("Poco X7", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}7\b{NO_SUFFIX}", [128, 256, 512], "Poco X7"),
    ("Poco X6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco X6 Pro"),
    ("Poco X6", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}6\b{NO_SUFFIX}", [256, 512], "Poco X6"),
    ("Poco X5 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}5{SEP}(?:pro|про)\b", [128, 256], "Poco X5 Pro"),
    ("Poco X5", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}5\b{NO_SUFFIX}", [128, 256], "Poco X5"),
    ("Poco X4 Pro", "android", rf"\b(?:poco|поко){SEP}(?:x|х){SEP}4{SEP}(?:pro|про)\b", [128, 256], "Poco X4 Pro"),
    ("Poco M6 Pro", "android", rf"\b(?:poco|поко){SEP}(?:m|м){SEP}6{SEP}(?:pro|про)\b", [256, 512], "Poco M6 Pro"),

    # --- GOOGLE PIXEL ---
    ("Pixel 9 Pro XL", "android", rf"\b(?:pixel|пиксель){SEP}9{SEP}(?:pro|про){SEP}(?:xl|хл)\b", [128, 256, 512, 1024], "Pixel 9 Pro XL"),
    ("Pixel 9 Pro", "android", rf"\b(?:pixel|пиксель){SEP}9{SEP}(?:pro|про)\b(?!{SEP}(?:xl|хл))", [128, 256, 512], "Pixel 9 Pro"),
    ("Pixel 9a", "android", rf"\b(?:pixel|пиксель){SEP}9{SEP}(?:a|а)\b", [128, 256], "Pixel 9a"),
    ("Pixel 9", "android", rf"\b(?:pixel|пиксель){SEP}9\b{NO_SUFFIX}", [128, 256], "Pixel 9"),

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
    r"\bксяоми\b", r"\bpoco\b", r"\bпоко\b", r"\bsamsung\b", r"\bсамсунг\b", r"\bhonor\b",
    r"\bхонор\b", r"\bhuawei\b", r"\bхуавей\b", r"\binfinix\b", r"\bинфиникс\b", r"\btecno\b",
    r"\bтекно\b", r"\boppo\b", r"\bvivo\b", r"\boneplus\b", r"\bnote\b", r"\bноут\b"
]

ACCESSORY_GHOST_PATTERNS = [
    r"^\s*(?:чехол|стекло|коробка|кабель|провод|зарядка|блок|наушники)\b",
    r"\bчехол\s+(?:на|для|от)\b", r"\bстекло\s+(?:на|для)\b", r"\bкоробка\s+от\b",
    r"\bаренда\b", r"\bпрокат\b"
]

BUY_INTENT_TITLE_START = r"^\s*(?:куплю|ищу|сниму|возьму)\b"

# Жесткий бан за 100% АКБ только для старых серий (XR, XS, SE, 11, 12), если в тексте не указана замена АКБ.
OLD_APPLE_100_BAT_MODELS = {
    "iPhone XR", "iPhone XS", "iPhone XS Max",
    "iPhone SE 2020", "iPhone SE 2022",
    "iPhone 11", "iPhone 11 Pro", "iPhone 11 Pro Max",
    "iPhone 12 mini", "iPhone 12", "iPhone 12 Pro", "iPhone 12 Pro Max"
}

# ================= 2. ВЫВЕРЕННЫЕ МАРКЕРЫ БАНА И ШТРАФОВ =================
# Безопасные бытовые фразы, которые вырезаются перед проверкой на бан (чтобы не банить 'защитное стекло под замену' или 'копия чека')
HARMLESS_PHRASES_REGEX = re.compile(
    r"\b(?:защитн\w*\s+стекл\w*|бронестекл\w*|гидрогел\w*|пленк\w*|чехол\w*|чехл\w*|кабел\w*|провод\w*|ремеш\w*)\s+(?:под\s+замену|тресну\w*|разби\w*|копия|реплика)\b"
    r"|\b(?:под\s+замену)\s+(?:защитн\w*\s+стекл\w*|бронестекл\w*|гидрогел\w*|пленк\w*|чехол\w*)\b"
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

# Замена АКБ перенесена из бана в дисконт (-7%), в бане оставлены только замена экрана, рефы и блокировки
REPAIR_MARKERS = [
    "менялся экран", "экран менялся", "замена экрана",
    "дисплей менялся", "менялся дисплей", "замена дисплея",
    "копия", "реплика", "муляж",
    "реф", "восстановленный", "mdm", "мдм", "байпас", "bypass"
]

BATTERY_REPLACED_MARKERS = [
    "замена акб", "менялся акб", "менялась акб", "акб менялся", "акб менялась",
    "менялся аккумулятор", "аккумулятор менялся", "новая батарея", "заменена батарея",
    "батарея менялась", "усиленная батарея", "новый акб"
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

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.kufar.by/"
})
if PROXY_URL:
    session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})


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
            is_correlated INTEGER DEFAULT 0,
            updated_ts INTEGER
        )
    """)
    # Безопасная миграция для существующих БД из прошлых версий
    try:
        cur.execute("ALTER TABLE market_cache ADD COLUMN is_correlated INTEGER DEFAULT 0")
    except Exception:
        pass

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


def strip_harmless_phrases(text):
    """Удаляет безопасные бытовые словосочетания ('защитное стекло под замену', 'копия чека') перед проверкой на бан."""
    return HARMLESS_PHRASES_REGEX.sub(" ", clean_text(text))


def has_direct_marker(text, marker):
    t = strip_harmless_phrases(text)
    return bool(re.search(rf"\b{re.escape(marker)}\b", t))


# Фразы с 'нет / не', которые не относятся к состоянию телефона и не должны отменять бан следующего за ними дефекта
IRRELEVANT_NEGATIONS_REGEX = re.compile(
    r"\b(?:торга|обмена|бартера|коробки|комплекта|зарядки|блока|кабеля|чехла|наушников|доставки|пересыла|спешки|времени)\s+нет\b"
    r"|\bнет\s+(?:торга|обмена|бартера|коробки|комплекта|зарядки|чехла|наушников|времени|смысла)\b"
    r"|\bне\s+(?:спешу|торгуюсь|меняю|обмениваю|интерисует|интересует|интересен|отправляю|высылаю|предлагать|беспокоить)\b"
)


def has_marker_without_negation(text, marker):
    """
    Точный поиск маркера с границами слов \b...\b и проверкой окна 50 символов назад.
    Защищено от ложных отмен бана на фразах 'Торга нет, разбит экран' и 'Не вскрывался, но трещина'.
    """
    t = strip_harmless_phrases(text)
    neg_pattern = r"\b(?:без|не|нет|никаких|никакого|никакой|отсутствуют|отсутствует|ничего\s+не|нужно\s+без)\b"
    after_neg_pattern = r"^\s*(?:не\s+было|нет|не\s+обнаружено|не\s+имеет|отсутствуют|отсутствует|не\s+менял\w*)\b"

    for m in re.finditer(rf"\b{re.escape(marker)}\b", t):
        start_idx = m.start()
        end_idx = m.end()

        prefix = t[max(0, start_idx - 50):start_idx]
        # 1. Режем по концу предложения (. ! ? \n)
        clause = re.split(r"[.!?\n]", prefix)[-1]
        # 2. Режем по противительным союзам (', но', ', а', ', однако', ', зато', ', только')
        clause = re.split(r",\s*(?:но|а|однако|зато|правда|только)\b", clause)[-1]
        # 3. Убираем бытовые отрицания ('торга нет', 'обмена нет', 'не спешу')
        clause = IRRELEVANT_NEGATIONS_REGEX.sub(" ", clause)

        if re.search(neg_pattern, clause):
            continue

        suffix = t[end_idx:min(len(t), end_idx + 25)]
        if re.search(after_neg_pattern, suffix):
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


def check_ban_markers(title, description, phone_type="apple", model_id=None, ad_params=None):
    t_title = clean_text(title)
    full = clean_text(f"{title} {description}")

    param_status, param_reason = check_ad_parameters_defects(ad_params)
    if param_status == "ban":
        return param_reason

    for pat in ACCESSORY_GHOST_PATTERNS:
        if re.search(pat, full):
            return f"Аксессуар/Аренда: {pat}"

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

    # На старых iPhone (XR - 12) баним подозрительные 100% АКБ только если продавец не указал честно замену батареи
    if phone_type == "apple" and model_id in OLD_APPLE_100_BAT_MODELS:
        if re.search(r"\b(?:акб|батаре[яиюей]|емкость|состояние\s*акб)[\s:=-]*100\s*%", full):
            has_honest_bat_replace = any(has_marker_without_negation(full, bm) for bm in BATTERY_REPLACED_MARKERS)
            if not has_honest_bat_replace:
                return f"Подозрительные 100% АКБ на {model_id} без указания замены банки"

    return None


def extract_battery_health(text):
    t = clean_text(text)
    m = re.search(r"(?:акб|батаре[яиюей]|емкость|состояние\s*(?:акб|батаре[яиюей]))[^\d]{0,15}(\d{2,3})\s*%", t)
    if not m:
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


def strip_secondary_device_mentions(text):
    """
    Вырезает упоминания вторичных устройств ('купил новый iPhone 15 Pro на 256 гб', 'обмен на S23 Ultra'),
    поддерживая буквенно-цифровые модели (S23, A54, X6, F6, XR, XS, SE, 16e).
    """
    pattern = (
        r"\b(?:купил\w*|переш(?:ел|ла|ли)\s+на|обмен\s+на|обменяю\s+на|подарили|взял\s+себе|чехол\s+(?:от|на|для))\s+"
        r"(?:[а-яa-z]+\s+){0,2}"
        r"(?:iphone|айфон|apple|эпл|samsung|самсунг|galaxy|redmi|редми|xiaomi|сяоми|poco|поко|pixel|пиксель)?[\s\-_.]*"
        r"(?:(?:s|с|a|а|x|х|f|ф|m|м|note|ноут|xr|хр|xs|хс|se)[\s\-_.]*)?"
        r"\d{0,2}(?:[\s\-_.]*(?:pro|про|max|макс|mini|мини|plus|плюс|\+|ultra|ультра|fe|фе|xl|хл|t|т|e|е|a|а))*"
        r"(?:\s*(?:на\s*)?(?:64|128|256|512|1024)\s*(?:гб|gb|гиг)?)?"
    )
    return re.sub(pattern, " ", text)


def extract_memory(title, description, ad_params, valid_memories):
    sorted_mems = sorted(valid_memories, reverse=True)

    # 1. Структурированные параметры Куфара (высший приоритет)
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

    # 2. Полная проверка ЗАГОЛОВКА по всем правилам (1TB -> 8/256 -> 256 гб -> голое число 256)
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

    # 3. Если в заголовке памяти нет — проверяем ОПИСАНИЕ (очищенное от вторичных упоминаний) только по явным маркерам гб/gb
    t_desc = strip_secondary_device_mentions(clean_text(description))

    if 1024 in valid_memories and re.search(r"\b(?:1024|1\s*(?:tb|тб|терабайт))\b", t_desc):
        return 1024, False

    slash_desc = re.search(r"\b(?:4|6|8|12|16)\s*/\s*(64|128|256|512|1024)\b", t_desc)
    if slash_desc and int(slash_desc.group(1)) in valid_memories:
        return int(slash_desc.group(1)), False

    for mem in sorted_mems:
        if re.search(rf"\b{mem}\s*(?:гб|gb|гиг|г\b|g\b)|\bна\s*{mem}\b", t_desc):
            return mem, False

    # 4. Если нигде не найдено — младшая память с флагом is_assumed_mem = True
    return valid_memories[0], True


def resolve_overlapping_models(found_list):
    if len(found_list) <= 1:
        return found_list
    unique_map = {}
    for item in found_list:
        if item[0] not in unique_map:
            unique_map[item[0]] = item
    items = list(unique_map.values())
    if len(items) == 1:
        return items

    filtered = []
    for item in items:
        m_name = item[0]
        if any(other[0] != m_name and m_name in other[0] for other in items):
            continue
        filtered.append(item)
    return filtered if filtered else items


def normalize_phone(title, description="", ad_params=None):
    t_title = clean_text(title)
    t_full = clean_text(f"{title} {description}")

    param_brand = ""
    if ad_params:
        for p in ad_params:
            if p.get("p") in ("brand", "mobile_phones_brand", "phablet_phones_brand"):
                param_brand = str(p.get("vl") or p.get("v") or "").lower()

    has_android_in_title = any(re.search(w, t_title) for w in ANDROID_BRAND_WORDS)
    has_android_in_body = any(re.search(w, t_full) for w in ANDROID_BRAND_WORDS)

    has_explicit_iphone = (
        bool(re.search(
            rf"\b(?:iphone|айфон|айф|apple|эпл|ios|pro{SEP}max|про{SEP}макс|promax|промакс|pro|про|mini|мини|xs|хс|xr|хр|se|16e|16е)\b",
            t_full
        ))
        or ("apple" in param_brand)
    )

    temp_title = strip_secondary_device_mentions(t_title)
    found_in_title = []

    for model_id, m_type, regex, mem_list, query_str in MODELS_WHITELIST:
        if m_type == "apple":
            if has_android_in_title:
                continue
            if not has_explicit_iphone:
                continue
            if param_brand and "apple" not in param_brand and any(b in param_brand for b in ("xiaomi", "redmi", "realme", "samsung", "poco", "honor", "huawei")):
                continue

        m_obj = re.search(regex, temp_title)
        if m_obj:
            found_in_title.append((model_id, m_type, mem_list, query_str, m_obj.start()))
            temp_title = re.sub(regex, " ", temp_title)

    found_in_title = resolve_overlapping_models(found_in_title)

    if len(found_in_title) > 1:
        return None, f"Мульти-лот в заголовке: {', '.join(x[0] for x in found_in_title)}"

    if len(found_in_title) == 1:
        model_id, m_type, mem_list, query_str, _ = found_in_title[0]
    else:
        found_in_body = []
        clean_body_source = strip_secondary_device_mentions(t_full)
        temp_body = clean_body_source

        for m_id, m_t, regex, m_l, q_s in MODELS_WHITELIST:
            if m_t == "apple":
                if has_android_in_title or has_android_in_body or not has_explicit_iphone:
                    continue
            m_obj = re.search(regex, temp_body)
            if m_obj:
                orig_match = re.search(regex, clean_body_source)
                pos = orig_match.start() if orig_match else m_obj.start()
                found_in_body.append((m_id, m_t, m_l, q_s, pos))
                temp_body = re.sub(regex, " ", temp_body)

        found_in_body = resolve_overlapping_models(found_in_body)

        if len(found_in_body) >= 1:
            found_in_body.sort(key=lambda x: x[4])
            model_id, m_type, mem_list, query_str, _ = found_in_body[0]
        else:
            if HAS_RAPIDFUZZ and t_title.strip() and not any(re.search(p, t_full) for p in ACCESSORY_GHOST_PATTERNS):
                choices = {m[0]: m for m in MODELS_WHITELIST if not (m[1] == "apple" and (has_android_in_title or not has_explicit_iphone))}
                if choices:
                    best = fuzz_process.extractOne(t_title, list(choices.keys()), scorer=fuzz.token_set_ratio, score_cutoff=92)
                    if best:
                        cand_name = best[0].lower()
                        suffix_words = [("pro", "про"), ("max", "макс"), ("plus", "плюс", "+"), ("ultra", "ультра"), ("mini", "мини"), ("fe", "фе"), ("xl", "хл"), ("t", "т"), ("e", "е")]
                        mismatch = False
                        for s_group in suffix_words:
                            in_cand = any(re.search(rf"\b{re.escape(s)}\b", cand_name) for s in s_group if s != "+")
                            in_title = any(re.search(rf"\b{re.escape(s)}\b", t_title) for s in s_group if s != "+") or ("+" in s_group and "+" in t_title)
                            if in_cand != in_title:
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


# ================= РЕАЛЬНЫЙ ДЕТЕКТОР ПЕРЕКУПОВ ЧЕРЕЗ API КУФАРА =================
def get_seller_active_phones_count(account_id, model_key, ad_link, now_ts, is_new_ad=True):
    if not account_id:
        return 1

    cur = db_conn.cursor()
    cur.execute("SELECT ads_count, last_seen_ts FROM sellers WHERE account_id = ?", (account_id,))
    row = cur.fetchone()

    # Считаем активные телефоны этого продавца в нашей собственной базе lot_history
    cur.execute("""
        SELECT COUNT(*) as c FROM lot_history
        WHERE account_id = ? AND status = 'active' AND model IS NOT NULL
    """, (account_id,))
    db_active_cnt = cur.fetchone()["c"] or 1

    # Если продавец уже кэширован как перекуп (>=2) или проверялся недавно и это не второй новый лот
    if row and (now_ts - (row["last_seen_ts"] or 0)) < SELLER_CACHE_TTL:
        cached_cnt = max(row["ads_count"] or 1, db_active_cnt)
        if not (is_new_ad and cached_cnt < 2 and (now_ts - (row["last_seen_ts"] or 0)) > 180):
            if is_new_ad:
                cur.execute("""
                    UPDATE sellers SET ads_count = ?, last_model = ?, last_link = ? WHERE account_id = ?
                """, (cached_cnt, model_key, ad_link, account_id))
                db_conn.commit()
            return cached_cnt

    api_phones_cnt = db_active_cnt
    url = f"https://api.kufar.by/search-api/v2/search/rendered-paginated?aid={account_id}&cat=17010&size=20&lang=ru"
    try:
        r = session.get(url, timeout=5)
        if r.status_code == 200:
            ads = r.json().get("ads", [])
            valid_cnt = 0
            for a in ads:
                subj = clean_text(a.get("subject", ""))
                if not subj:
                    continue
                if any(re.search(pat, subj) for pat in ACCESSORY_GHOST_PATTERNS):
                    continue
                if re.search(BUY_INTENT_TITLE_START, subj):
                    continue
                valid_cnt += 1
            api_phones_cnt = max(1, valid_cnt, db_active_cnt)
    except Exception:
        if row:
            api_phones_cnt = max(row["ads_count"] or 1, db_active_cnt)

    cur.execute("""
        INSERT INTO sellers (account_id, ads_count, last_model, last_link, last_seen_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            ads_count = excluded.ads_count,
            last_model = excluded.last_model,
            last_link = excluded.last_link,
            last_seen_ts = excluded.last_seen_ts
    """, (account_id, api_phones_cnt, model_key, ad_link, now_ts))
    db_conn.commit()
    return api_phones_cnt


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
            if 130 <= p_val <= 5000:
                w = 0.45 if norm["is_assumed_mem"] else 0.90
                prices.append((p_val, w))
    except Exception:
        pass
    return prices


def fetch_kufar_sample(phone_info, target_memory, region_minsk=True):
    """
    Двухэтапный сбор выборки Куфара:
    Сначала делает целевой запрос с объемом памяти (чтобы 256/512GB не тонули в выдаче 128GB),
    а затем добирает общую выдачу модели (для объявлений, где память указана только в параметрах).
    """
    weighted_prices = []
    days_list = []
    seller_counts = {}
    seen_sample_ids = set()
    min_lot = (99999, "")
    rgn_param = "&rgn=7" if region_minsk else ""
    now = time.time()

    mem_query = f"{phone_info['query']} {target_memory}"
    queries_to_run = [mem_query, phone_info["query"]] if len(phone_info["valid_memories"]) > 1 else [phone_info["query"]]

    for q_idx, q_str in enumerate(queries_to_run):
        # Если после прицельного запроса с памятью уже набралось >= 22 чистых лотов — второй запрос не нужен
        if q_idx == 1 and len(weighted_prices) >= 22:
            break

        cursor = None
        pages_limit = 1 if len(queries_to_run) > 1 else 2

        for _ in range(pages_limit):
            url = (
                f"https://api.kufar.by/search-api/v2/search/rendered-paginated"
                f"?cat=17010&cmp=0&lang=ru&prc=r%3A12000%2C500000{rgn_param}"
                f"&query={requests.utils.quote(q_str)}&size=50&sort=lst.d"
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
                        ad_id = str(ad.get("ad_id") or "")
                        if not ad_id or ad_id in seen_sample_ids:
                            continue

                        subj = ad.get("subject", "")
                        p_byn = int(ad.get("price_byn") or 0) / 100.0
                        if p_byn < 130 or p_byn > 5000:
                            continue

                        ad_params = ad.get("ad_parameters", [])
                        if check_ban_markers(subj, "", phone_info["type"], phone_info["model"], ad_params):
                            continue
                        if any(has_marker_without_negation(subj, m) for m in HEAVY_COSMETIC_MARKERS + ["нюанс", "дефект", "срочно"]):
                            continue

                        norm, _ = normalize_phone(subj, "", ad_params)
                        if not norm or norm["model"] != phone_info["model"] or norm["memory"] != target_memory:
                            continue
                        if phone_info["model"] in ("iPhone 11", "iPhone 12", "iPhone 13", "iPhone 14", "iPhone 15", "iPhone 16") and p_byn < 250:
                            continue

                        seen_sample_ids.add(ad_id)
                        list_ts = parse_kufar_time(ad.get("list_time", ""))
                        days_old = max(0.0, (now - list_ts) / 86400.0)
                        days_list.append(days_old)

                        weight = max(0.30, 1.0 - 0.02 * days_old)
                        if norm["is_assumed_mem"] and len(phone_info["valid_memories"]) > 1:
                            weight *= 0.5

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


def compute_raw_market_for_mem(phone_info, target_memory):
    """Возвращает чистую взвешенную медиану (без bargain_factor), чтобы кэш не зависел от смены коэффициента."""
    w_prices, days_list, min_lot, resellers_cnt = fetch_kufar_sample(phone_info, target_memory, region_minsk=True)

    if len(w_prices) < 15:
        rb_w, rb_days, rb_min, rb_res = fetch_kufar_sample(phone_info, target_memory, region_minsk=False)
        if len(rb_w) > len(w_prices):
            w_prices, days_list, min_lot, resellers_cnt = rb_w, rb_days, rb_min, rb_res

    onl_prices = fetch_onliner_baraholka_prices(phone_info, target_memory)
    if onl_prices:
        w_prices.extend(onl_prices)

    n = len(w_prices)
    if n < MIN_VALID_SAMPLE:
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

    raw_med = round(weighted_median(core))
    med_days = round(statistics.median(days_list), 1) if days_list else 3.0
    return raw_med, n, med_days, min_lot, resellers_cnt


def get_market_data(phone_info, settings, force_refresh=False):
    cache_key = phone_info["key"]
    now = int(time.time())
    cur = db_conn.cursor()

    anchor_val = settings.get("anchors", {}).get(cache_key)
    bf = settings.get("bargain_factor", 1.00)

    if not force_refresh:
        cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        if row and (now - row["updated_ts"]) < MARKET_CACHE_TTL:
            scaled_market = round(row["market_price"] * bf)
            p = anchor_val if anchor_val else scaled_market
            return {
                "price": p,
                "min_price": row["min_price"],
                "min_link": row["min_link"],
                "sample_size": row["sample_size"],
                "median_days": row["median_days"],
                "resellers_count": row["resellers_count"],
                "is_correlated": bool(row["is_correlated"]) if "is_correlated" in row.keys() else False,
                "is_anchored": bool(anchor_val)
            }

    target_mem = phone_info["memory"]
    valid_mems = phone_info["valid_memories"]
    raw_market_val, n, med_days, min_lot, resellers_cnt = compute_raw_market_for_mem(phone_info, target_mem)
    is_correlated = False

    base_mem = 128 if 128 in valid_mems else valid_mems[0]
    if target_mem != base_mem or (raw_market_val is None or n < 8):
        base_key = f"{phone_info['model']}_{base_mem}GB"
        base_val, base_n, base_days = None, 0, 3.0

        cur.execute("SELECT * FROM market_cache WHERE cache_key = ?", (base_key,))
        base_row = cur.fetchone()
        if base_row and (now - base_row["updated_ts"]) < MARKET_CACHE_TTL:
            base_val = base_row["market_price"]
            base_n = base_row["sample_size"]
            base_days = base_row["median_days"]
        elif target_mem != base_mem:
            base_val, base_n, base_days, b_min, b_res = compute_raw_market_for_mem(phone_info, base_mem)
            if base_val and base_n >= MIN_VALID_SAMPLE:
                cur.execute("""
                    INSERT OR REPLACE INTO market_cache
                    (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, is_correlated, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """, (base_key, base_val, b_min[0] if b_min[0] < 9999 else base_val, b_min[1], base_n, base_days, b_res, now))
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

    if raw_market_val is None and not anchor_val:
        return None
    if raw_market_val is None and anchor_val:
        raw_market_val = anchor_val

    if n < MIN_VALID_SAMPLE and not anchor_val:
        return None

    cur.execute("""
        INSERT OR REPLACE INTO market_cache
        (cache_key, market_price, min_price, min_link, sample_size, median_days, resellers_count, is_correlated, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (cache_key, raw_market_val, min_lot[0] if min_lot[0] < 9999 else raw_market_val, min_lot[1], n, med_days, resellers_cnt, 1 if is_correlated else 0, now))
    db_conn.commit()

    scaled_price = round(raw_market_val * bf)
    final_price = anchor_val if anchor_val else scaled_price
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


def adjust_lot_market_price(base_market, full_text, phone_type, bat_health, color_tier, has_box, param_defect_note=None):
    multiplier = 1.0
    notes = []
    has_defects = False

    # Проверка на замененный аккумулятор (-7% вместо вечного бана)
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
                        "text": f"🏘 <b>Выбор районов Минска</b>\nАктивно: <b>{active_str}</b>\n<i>(Лоты с выгодой >30 BYN придут из любого района с пометкой)</i>",
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
                    "🤖 <b>Панель управления Kufar Sniper 3.5</b>\nВыбери действие на клавиатуре ниже:",
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
                    f"• Мин. выгода под осмотр: <b>{settings['inspect_min_profit']} BYN</b>\n"
                    f"• Заданные якоря: <b>{anc_str}</b>\n\n"
                    f"<b>Команды для изменения:</b>\n"
                    f"• <code>цена 150 2500</code> — изменить мин и макс цену\n"
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
                        send_telegram(f"✅ Новый коэффициент рынка: <b>{settings['bargain_factor']}</b> (применен ко всем моделям)")
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
        send_telegram("⚠️ Слишком мало активных объявлений на рынке (меньше 3 шт.) для расчета.")
        return

    med = m_data["price"]
    fast_buy = round(med * 0.86)
    bargain_buy = round(med * 0.92)
    min_p = m_data["min_price"]
    min_link = html.escape(m_data["min_link"] or "https://www.kufar.by")
    anc_note = " ⚓ <i>(Зафиксировано твоим якорем)</i>" if m_data.get("is_anchored") else ""
    corr_note = " <i>(корреляция памяти)</i>" if m_data.get("is_correlated") else ""
    low_sample_warn = f"\n⚠️ <b>Мало данных для рынка (n={m_data['sample_size']})</b>" if m_data["sample_size"] < 8 else ""

    liq_label = "🔥 Высокая (улетают быстро)" if m_data["median_days"] <= 3.5 else ("⏳ Средняя" if m_data["median_days"] <= 8 else "🐌 Низкая (висят долго)")

    resp = (
        f"📊 <b>Аналитика: {phone_info['model']} ({phone_info['memory']} GB)</b>{low_sample_warn}\n\n"
        f"• 💎 <b>Реальная рыночная цена: ~{med:.0f} BYN</b>{corr_note}{anc_note}\n"
        f"• 🚀 Цена быстрого выкупа (жир): <b>до {fast_buy} BYN</b>\n"
        f"• 🤝 Рабочая цена под перепродажу: <b>~{bargain_buy} BYN</b>\n"
        f"• 📉 Минимум на Куфаре сейчас: <a href='{min_link}'><b>{min_p:.0f} BYN (открыть)</b></a>\n\n"
        f"⏱ Индекс ликвидности: <b>{liq_label}</b> (медиана жизни: ~{m_data['median_days']} дн.)\n"
        f"📦 Лотов в выборке: <b>{m_data['sample_size']} шт.</b> (перекупов на модели: ~{m_data['resellers_count']})"
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
        msg += f"{i}. ID <code>{r['account_id']}</code> — активных телефонов: <b>{r['ads_count']} шт.</b> (последний: <a href='{link}'>{html.escape(r['last_model'] or 'Телефон')}</a>)\n"
    send_telegram(msg)


# ================= 5. ДЕТЕКТОР "МОГИЛ" И ВЕЧЕРНИЙ РАДАР =================
def check_graveyard_and_digest(settings):
    now = int(time.time())
    cur = db_conn.cursor()

    cur.execute("""
        SELECT ad_id, account_id, model, memory, last_seen_price, list_time_ts
        FROM lot_history
        WHERE status = 'active' AND model IS NOT NULL AND (? - last_seen_ts) > 3600
        ORDER BY last_seen_ts ASC
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
            WHERE status = 'active' AND model IS NOT NULL AND list_time_ts BETWEEN ? AND ?
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
    max_k = int(settings.get("max_price", 2800)) * 100
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
            f"🚀 <b>Kufar Sniper 3.5 запущен!</b>\n"
            f"⚡ Отклик кнопок: <b>1 сек.</b> | Умный бан-лист и точный поиск памяти\n"
            f"📦 Стартовая лента ({len(initial_ads)} лотов) записана в базу.",
            with_sound=False,
            reply_markup=get_main_menu_keyboard()
        )

    print("🚀 Мониторинг Kufar 3.5 активен.")
    next_kufar_scan_ts = 0.0
    next_graveyard_check_ts = time.time() + 60.0

    while (time.time() - start_time) < max_seconds:
        # 1. Мгновенный опрос кнопок Telegram (каждую 1 секунду независимо от времени суток)
        last_update_id = handle_telegram_updates(last_update_id, settings)

        now_f = time.time()

        # 2. Фоновая проверка "могил" и вечернего дайджеста по отдельному 5-минутному таймеру
        if now_f >= next_graveyard_check_ts:
            check_graveyard_and_digest(settings)
            next_graveyard_check_ts = time.time() + GRAVEYARD_CHECK_INTERVAL

        if now_f < next_kufar_scan_ts:
            time.sleep(TG_POLL_INTERVAL)
            continue

        # 3. Адаптивный интервал до следующего опроса ленты Куфара (День / Вечер / Ночь)
        next_kufar_scan_ts = now_f + get_adaptive_kufar_interval()

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
                # Если лот уже был признан браком или нецелевой моделью — пропускаем мгновенно
                if existing["status"] in ("banned", "ignored"):
                    continue

                old_price = existing["last_seen_price"]
                first_price = existing["first_seen_price"] or old_price
                drops_cnt = existing["price_drops_count"] or 0
                notified_state = existing["notified"]  # 1 = отправлен в ТГ, 0 = проверен (мало выгоды), -1 = ждет расчета рынка

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
                    elif notified_state == 1:
                        continue
                else:
                    cur.execute("UPDATE lot_history SET last_seen_ts = ?, last_seen_price = ? WHERE ad_id = ?", (now_ts, price_byn, ad_id))
                    db_conn.commit()
                    if notified_state in (0, 1):
                        continue

            raw_title = ad.get("subject", "Без названия")
            ad_params = ad.get("ad_parameters", [])

            title_norm, _ = normalize_phone(raw_title, "", ad_params)
            title_model_id = title_norm["model"] if title_norm else None
            title_phone_type = title_norm["type"] if title_norm else "apple"

            if check_ban_markers(raw_title, "", title_phone_type, title_model_id, ad_params):
                cur.execute("""
                    INSERT INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'banned', 1)
                    ON CONFLICT(ad_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts, status = 'banned', notified = 1
                """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            description = get_ad_description(ad_id)
            full_text = f"{raw_title}\n{description}"

            phone_info, norm_err = normalize_phone(raw_title, description, ad_params)
            if not phone_info:
                cur.execute("""
                    INSERT INTO lot_history (ad_id, account_id, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'ignored', 1)
                    ON CONFLICT(ad_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts, status = 'ignored', notified = 1
                """, (ad_id, account_id, price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            ban_reason = check_ban_markers(raw_title, description, phone_info["type"], phone_info["model"], ad_params)
            if ban_reason:
                print(f"⛔ [Бан: {ban_reason}] {raw_title}")
                cur.execute("""
                    INSERT INTO lot_history (ad_id, account_id, model, memory, first_seen_price, last_seen_price, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'banned', 1)
                    ON CONFLICT(ad_id) DO UPDATE SET model = excluded.model, memory = excluded.memory, last_seen_ts = excluded.last_seen_ts, status = 'banned', notified = 1
                """, (ad_id, account_id, phone_info["model"], phone_info["memory"], price_byn, price_byn, now_ts, now_ts, list_ts))
                db_conn.commit()
                continue

            bat_health = extract_battery_health(full_text) if phone_info["type"] == "apple" else None
            color_name, color_tier = extract_color(full_text)
            has_box = check_missing_box(full_text)
            district = parse_district(ad)
            link = ad.get("ad_link", f"https://www.kufar.by/item/{ad_id}")

            param_status, param_defect_note = check_ad_parameters_defects(ad_params)
            if param_status != "defect":
                param_defect_note = None

            # Сохраняем лот в историю до вызова детектора продавца, чтобы активный лот учитывался в БД
            cur.execute("""
                INSERT INTO lot_history
                (ad_id, account_id, model, memory, battery_health, color, district, has_box,
                 first_seen_price, last_seen_price, price_drops_count, first_seen_ts, last_seen_ts, list_time_ts, status, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', -1)
                ON CONFLICT(ad_id) DO UPDATE SET
                    model = excluded.model,
                    memory = excluded.memory,
                    battery_health = excluded.battery_health,
                    color = excluded.color,
                    district = excluded.district,
                    has_box = excluded.has_box,
                    last_seen_price = excluded.last_seen_price,
                    last_seen_ts = excluded.last_seen_ts,
                    status = 'active'
            """, (ad_id, account_id, phone_info["model"], phone_info["memory"], bat_health, color_name,
                  district, 1 if has_box else 0, price_byn, price_byn, now_ts, now_ts, list_ts))
            db_conn.commit()

            seller_ads_cnt = get_seller_active_phones_count(
                account_id, phone_info["key"], link, now_ts, is_new_ad=(existing is None)
            )

            m_data = get_market_data(phone_info, settings)
            if not m_data:
                print(f"🔇 [Мало данных даже после расширения: n < {MIN_VALID_SAMPLE}] {phone_info['key']}")
                continue

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
                        cur.execute("UPDATE lot_history SET notified = 0 WHERE ad_id = ?", (ad_id,))
                        db_conn.commit()
                        print(f"🗺 [Пропуск по району: {district}, выгода {profit_byn:+.0f} <= 30] {phone_info['key']}")
                        continue

            matched_urgent = []
            for u_pat in URGENT_PATTERNS:
                m_u = re.search(u_pat, clean_text(full_text))
                if m_u:
                    matched_urgent.append(m_u.group(0))
            is_urgent = len(matched_urgent) > 0

            # ЗВУК ТОЛЬКО НА ЧЕСТНЫЙ ЖИР!
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
            elif profit_byn >= settings.get("inspect_min_profit", 15) and not has_defects:
                tier_header = "👀 <b>ПОД ОСМОТР (Чистый лот в плюсе)</b>"
                with_sound = False
            else:
                cur.execute("UPDATE lot_history SET notified = 0 WHERE ad_id = ?", (ad_id,))
                db_conn.commit()
                print(f"📉 [Мало выгоды: {profit_byn:+.0f} BYN] {phone_info['key']} за {price_byn:.0f} р. (Рынок ~{adj_market} р.)")
                continue

            tags = []
            if seller_ads_cnt >= 2:
                tags.append(f"🦈 <b>Перекуп</b> ({seller_ads_cnt} тел. активно)")
            else:
                tags.append("👤 Частник")

            if phone_info["model"].startswith("iPhone 13") and (
                bat_health == 100 or re.search(r"\b(?:акб|батаре[яиюей]|емкость|состояние\s*акб)[\s:=-]*100\s*%", clean_text(full_text))
            ):
                tags.append("⚠️ <b>Проверь 100% АКБ</b>")

            if m_data["sample_size"] < 8:
                tags.append(f"⚠️ <b>Мало данных для рынка (n={m_data['sample_size']})</b>")

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

            # Сначала обрезаем сырой текст описания, затем экранируем HTML-символы (защита от разрезания &quot;)
            raw_desc = description.strip()
            if len(raw_desc) > 260:
                raw_desc = raw_desc[:260] + "..."
            short_desc = html.escape(raw_desc)

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
