# ===== ОСНОВНЫЕ НАСТРОЙКИ =====
BOT_TOKEN = ""
ADMIN_ID  = 0  # Твой Telegram user_id (узнать: @userinfobot)

# ===== PYROGRAM (для парсинга TG Market) =====
API_ID   = 0   # https://my.telegram.org
API_HASH = ""  # https://my.telegram.org

GOLDEN_KEY = ""

# ===== ДЕФОЛТНЫЕ НАСТРОЙКИ (меняются через админку в боте) =====
DEFAULT_FP_STAR_PRICE         = 2.5    # Курс 1 звезды на FunPay (₽)
DEFAULT_FP_WITHDRAW_FEE_PCT   = 0.06
DEFAULT_FP_WITHDRAW_MIN_RUB   = 150.0
DEFAULT_TG_MARKET_COMMISSION   = 0.20  # Комиссия TG Market при продаже подарка (20% → 0.20)
DEFAULT_PORTALS_WITHDRAW_FEE   = 0.16  # Фикс. комиссия вывода подарка с Portals (TON)
DEFAULT_MRKT_WITHDRAW_FEE      = 0.16  # Фикс. комиссия вывода подарка с MRKT (TON)

# ===== ФИЛЬТРЫ =====
MIN_PROFIT_PERCENT = 5.0  # Мин. % прибыли для отображения в топе

# ===== ГЛОБАЛЬНЫЕ СЛОВАРИ ДАННЫХ (не трогать) =====
portals_gifts_data  = {}  # gift_name → floor_price (TON)
mrkt_gifts_data     = {}  # gift_name → floor_price (TON)
tmarket_gifts_data  = {}  # gift_name → stars_count (floor TG Market)