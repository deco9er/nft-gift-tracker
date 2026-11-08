import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from pyrogram import Client
from pyrogram.enums import GiftForResaleOrder
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName, InputUser
from urllib.parse import unquote
from html import unescape
from math import floor
import re

from curl_cffi import requests as curl_requests
from pycoingecko import CoinGeckoAPI

from constants import (
    BOT_TOKEN, ADMIN_ID, API_ID, API_HASH, GOLDEN_KEY,
    DEFAULT_FP_STAR_PRICE,
    DEFAULT_FP_WITHDRAW_FEE_PCT, DEFAULT_FP_WITHDRAW_MIN_RUB,
    DEFAULT_TG_MARKET_COMMISSION,
    DEFAULT_PORTALS_WITHDRAW_FEE, DEFAULT_MRKT_WITHDRAW_FEE,
    MIN_PROFIT_PERCENT,
    portals_gifts_data, mrkt_gifts_data, tmarket_gifts_data
)

# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────
class AdminState(StatesGroup):
    waiting_fp_star_price        = State()
    waiting_tg_market_commission = State()
    waiting_portals_withdraw_fee = State()
    waiting_mrkt_withdraw_fee    = State()
    waiting_ton_balance          = State()
    waiting_min_profit           = State()

# ──────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────
settings = {
    "fp_star_price":        DEFAULT_FP_STAR_PRICE,
    "tg_market_commission": DEFAULT_TG_MARKET_COMMISSION,
    "portals_withdraw_fee": DEFAULT_PORTALS_WITHDRAW_FEE,
    "mrkt_withdraw_fee":    DEFAULT_MRKT_WITHDRAW_FEE,
    "ton_balance":          0.0,
    "min_profit":           MIN_PROFIT_PERCENT,
    "fp_usdt_rate":         0.0,
    "fp_withdraw_fee_pct":  DEFAULT_FP_WITHDRAW_FEE_PCT,
    "fp_withdraw_min_rub":  DEFAULT_FP_WITHDRAW_MIN_RUB,
}

mrkt_token      = None
cg              = CoinGeckoAPI()
bot             = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp              = Dispatcher(storage=MemoryStorage())
pyrogram_client = Client("main", api_id=API_ID, api_hash=API_HASH)

# ──────────────────────────────────────────────
# УТИЛИТЫ
# ──────────────────────────────────────────────
def e(emoji_id: str, fallback: str = "•") -> str:
    """Премиум эмодзи для сообщений"""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def _nano_to_ton(nano_value) -> float:
    decimal = float(nano_value) / 1e7
    return float("{:.2f}".format(floor(round(decimal, 10)) / 100))

def split_text(text: str, limit: int = 4096) -> list:
    if len(text) <= limit:
        return [text]
    parts    = text.split("┌")
    header   = parts[0].strip()
    blocks   = ["┌" + b for b in parts[1:] if b.strip()]
    messages = []
    current  = header
    for block in blocks:
        if len(current + "\n\n" + block) <= limit:
            current += "\n\n" + block
        else:
            messages.append(current)
            current = header + "\n\n" + block
    if current:
        messages.append(current)
    return messages

# ──────────────────────────────────────────────
# КЛАВИАТУРЫ
# ──────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    bal = settings["ton_balance"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Топ Portals", callback_data="top_portals",
                                 icon_custom_emoji_id="5870930636742595124"),
            InlineKeyboardButton(text="Топ MRKT",    callback_data="top_mrkt",
                                 icon_custom_emoji_id="5870921681735781843"),
        ],
        [InlineKeyboardButton(text=f"Баланс: {bal} TON", callback_data="set_balance",
                              icon_custom_emoji_id="5904462880941545555")],
        [InlineKeyboardButton(text="Настройки", callback_data="admin_menu",
                              icon_custom_emoji_id="5870982283724328568")],
    ])


def kb_back(to: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data=f"back_{to}",
                              icon_custom_emoji_id="5893057118545646106")]
    ])

def kb_admin() -> InlineKeyboardMarkup:
    s = settings
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Курс звезды на FP: {s['fp_star_price']} ₽",
            callback_data="set_fp_star_price",
            icon_custom_emoji_id="5769126056262898415"
        )],
        [InlineKeyboardButton(
            text=f"Комиссия TG Market: {round(s['tg_market_commission']*100, 1)}%",
            callback_data="set_tg_market_commission",
            icon_custom_emoji_id="6032644646587338669"
        )],
        [InlineKeyboardButton(
            text=f"Вывод с Portals: {s['portals_withdraw_fee']} TON",
            callback_data="set_portals_fee",
            icon_custom_emoji_id="5890848474563352982"
        )],
        [InlineKeyboardButton(
            text=f"Вывод с MRKT: {s['mrkt_withdraw_fee']} TON",
            callback_data="set_mrkt_fee",
            icon_custom_emoji_id="5890848474563352982"
        )],
        [InlineKeyboardButton(
            text=f"Мин. прибыль: {s['min_profit']}%",
            callback_data="set_min_profit",
            icon_custom_emoji_id="5870930636742595124"
        )],
        [InlineKeyboardButton(text="Назад", callback_data="back_main",
                              icon_custom_emoji_id="5893057118545646106")],
    ])

# ──────────────────────────────────────────────
# РАСЧЁТ ПРИБЫЛИ
#   cost_ton    = buy_ton + withdraw_fee_ton
#   count       = int(balance / cost_ton)
#   tba         = cost_ton × count
#   sp_net      = stars × (1 − tg_comm) × fp_price        (₽ с 1 подарка)
#   revenue_rub = sp_net × count
#   fee_rub     = max(revenue_rub × 6%, 150)              (вывод на ФП)
#   usdt        = (revenue_rub − fee_rub) / fp_usdt_rate
#   ton_out     = usdt / ton_usd                          (своп в TON)
#   profit      = ton_out − tba
# ──────────────────────────────────────────────
def calculate_profit(buy_ton: float, stars: int, ton_rub: float, ton_usd: float, withdraw_fee_ton: float) -> dict:
    fp_price   = settings["fp_star_price"]
    tg_comm    = settings["tg_market_commission"]
    balance    = settings["ton_balance"]
    rate       = settings["fp_usdt_rate"]
    w_pct      = settings["fp_withdraw_fee_pct"]
    w_min      = settings["fp_withdraw_min_rub"]

    cost_ton   = buy_ton + withdraw_fee_ton
    cost_rub   = cost_ton * ton_rub

    count      = int(balance / cost_ton) if cost_ton > 0 else 0
    tba        = cost_ton * count

    stars_net  = round(stars * (1 - tg_comm), 1)
    sp_net     = round(stars * (1 - tg_comm) * fp_price, 3)
    revenue_rub = sp_net * count

    if revenue_rub > 0:
        fee_rub = revenue_rub * w_pct
        if fee_rub < w_min:
            fee_rub = w_min
    else:
        fee_rub = 0.0

    usdt    = (revenue_rub - fee_rub) / rate if rate > 0 else 0.0
    ton_out = usdt / ton_usd if ton_usd > 0 else 0.0

    profit_ton = round(ton_out - tba, 4)
    profit_pct = round((ton_out / tba - 1) * 100, 4) if tba > 0 else 0.0

    star_price = round(cost_rub / (stars * (1 - tg_comm)), 2) if stars > 0 else 0.0

    return {
        "cost_ton":     round(cost_ton, 4),
        "cost_rub":     round(cost_rub, 2),
        "stars_net":    stars_net,
        "sp_net":       sp_net,
        "star_price":   star_price,
        "revenue_rub":  round(revenue_rub, 2),
        "fee_rub":      round(fee_rub, 2),
        "usdt":         round(usdt, 2),
        "ton_out":      round(ton_out, 4),
        "profit_ton":   profit_ton,
        "profit_pct":   profit_pct,
        "count":        count,
        "tba":          round(tba, 4),
    }

# ──────────────────────────────────────────────
# ТЕКСТ ТОПА
# ──────────────────────────────────────────────
async def build_top_text(market: str):
    market_data  = portals_gifts_data if market == "portals" else mrkt_gifts_data
    fee          = settings["portals_withdraw_fee"] if market == "portals" else settings["mrkt_withdraw_fee"]
    min_profit   = settings["min_profit"]
    s            = settings

    if not market_data or not tmarket_gifts_data or len(tmarket_gifts_data) < 5:
        return None

    if settings["fp_usdt_rate"] <= 0:
        return 'Курс вывода FunPay ещё не получен. Подожди немного.'

    try:
        price   = cg.get_price(ids="the-open-network", vs_currencies="rub,usd")["the-open-network"]
        ton_rub = price["rub"]
        ton_usd = price["usd"]
    except Exception as ex:
        print("Ошибка курса TON:", ex)
        return None

    rate = settings["fp_usdt_rate"]

    results = []
    for name, buy_ton in market_data.items():
        stars = tmarket_gifts_data.get(name)
        if not stars:
            continue
        try:
            p = calculate_profit(buy_ton, int(stars), ton_rub, ton_usd, fee)
            if p["profit_pct"] >= min_profit:
                results.append((name, buy_ton, int(stars), p))
        except Exception as ex:
            print(f"Расчёт {name}:", ex)

    if not results:
        return f'Нет подарков с прибылью ≥ {min_profit}%'

    results.sort(key=lambda x: x[3]["profit_pct"], reverse=True)

    label = "Portals" if market == "portals" else "MRKT"
    text = (
        f'{e("5870921681735781843","📊")} <b>Топ подарков — {label}</b>\n\n'
        f'{e("5904462880941545555","🪙")} TON = {round(ton_rub, 1)} ₽ / {round(ton_usd, 3)} $\n'
        f'{e("5769126056262898415","👛")} FP курс звезды = {s["fp_star_price"]} ₽\n'
        f'{e("5879814368572478751","💵")} Вывод ФП: {round(s["fp_withdraw_fee_pct"]*100,1)}% (мин {s["fp_withdraw_min_rub"]} ₽) | курс {rate}\n'
        f'{e("6032644646587338669","🎁")} Комиссия TG Market = {round(s["tg_market_commission"]*100,1)}%\n'
        f'{e("5890848474563352982","🪙")} Вывод подарка = {fee} TON\n'
        f'{e("5904462880941545555","🪙")} Баланс: <b>{s["ton_balance"]} TON</b>\n'
    )

    for i, (name, buy_ton, stars, p) in enumerate(results, 1):
        text += (
            f'\n{e("6028435952299413210","ℹ")} <b>{i}. {name}</b>\n'
            f'{e("5890848474563352982","🪙")} <b>{p["cost_ton"]} TON</b> ({p["cost_rub"]} ₽)  {e("5884479287171485878","📦")} {p["count"]} шт\n'
            f'{e("5769289093221454192","⭐")} {stars} ⭐  {e("5870676941614354370","🖋")} 1⭐ = {p["star_price"]} ₽\n'
            f'{e("5879814368572478751","💵")} {p["revenue_rub"]} ₽ → {p["usdt"]} USDT → {p["ton_out"]} TON\n'
            f'{e("5870930636742595124","📈")} <b>{p["profit_pct"]}%</b> (+{p["profit_ton"]} TON)\n'
        )

    return text

# ──────────────────────────────────────────────
# ХЭНДЛЕР СТАРТ
# ──────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f'{e("5870657884844462243","👋")} <b>Привет!</b>\n'
        f'Баланс: <b>{settings["ton_balance"]} TON</b>\n\n'
        f'Выбери действие:',
        reply_markup=kb_main()
    )



async def send_top(target, market: str):
    is_msg = isinstance(target, Message)
    msg = await (target.answer if is_msg else target.answer)("⏳ Считаю прибыль...")
    text = await build_top_text(market)
    if not text:
        await msg.edit_text(
            f'{e("5870657884844462243","❌")} Данные ещё собираются. Подожди немного.'
        )
        return
    parts = split_text(text)
    await msg.edit_text(parts[0])
    for part in parts[1:]:
        await (target.answer if is_msg else target.message.answer)(part)

# ──────────────────────────────────────────────
# CALLBACK
# ──────────────────────────────────────────────
@dp.callback_query()
async def callback_handler(call: CallbackQuery, state: FSMContext):
    d   = call.data
    uid = call.from_user.id

    if d == "top_portals":
        await call.answer()
        await send_top(call.message, "portals")
        return

    if d == "top_mrkt":
        await call.answer()
        await send_top(call.message, "mrkt")
        return

    if d == "back_main":
        await state.clear()
        await call.message.edit_text(
            f'{e("5870657884844462243","👋")} <b>Привет!</b>\n'
            f'Баланс: <b>{settings["ton_balance"]} TON</b>\n\n'
            f'Выбери действие:',
            reply_markup=kb_main()
        )
        await call.answer()
        return

    if d == "back_admin":
        await state.clear()
        await call.message.edit_text(
            f'{e("5870982283724328568","⚙")} <b>Настройки</b>',
            reply_markup=kb_admin()
        )
        await call.answer()
        return

    if d == "admin_menu":
        if not is_admin(uid):
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.message.edit_text(
            f'{e("5870982283724328568","⚙")} <b>Настройки</b>',
            reply_markup=kb_admin()
        )
        await call.answer()
        return

    if d == "set_balance":
        if not is_admin(uid):
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.message.edit_text(
            f'Текущий баланс: <b>{settings["ton_balance"]} TON</b>\n\nОтправь новое значение:',
            reply_markup=kb_back("main")
        )
        await state.set_state(AdminState.waiting_ton_balance)
        await call.answer()
        return

    admin_map = {
        "set_fp_star_price":        (AdminState.waiting_fp_star_price,
            f'{e("5769126056262898415","👛")} Текущий курс 1 ⭐ на FunPay: <b>{settings["fp_star_price"]} ₽</b>\n\nОтправь новый курс (₽):'),
        "set_tg_market_commission": (AdminState.waiting_tg_market_commission,
            f'{e("6032644646587338669","🎁")} Текущая комиссия TG Market: <b>{round(settings["tg_market_commission"]*100,1)}%</b>\n\nОтправь комиссию в % (например: 20):'),
        "set_portals_fee":          (AdminState.waiting_portals_withdraw_fee,
            f'{e("5890848474563352982","🪙")} Текущая комиссия вывода с Portals: <b>{settings["portals_withdraw_fee"]} TON</b>\n\nОтправь новое значение (TON):'),
        "set_mrkt_fee":             (AdminState.waiting_mrkt_withdraw_fee,
            f'{e("5890848474563352982","🪙")} Текущая комиссия вывода с MRKT: <b>{settings["mrkt_withdraw_fee"]} TON</b>\n\nОтправь новое значение (TON):'),
        "set_min_profit":           (AdminState.waiting_min_profit,
            f'{e("5870930636742595124","📈")} Текущий мин. порог: <b>{settings["min_profit"]}%</b>\n\nОтправь новый порог (%):'),
    }

    if d in admin_map:
        if not is_admin(uid):
            await call.answer("Нет доступа", show_alert=True)
            return
        st, prompt = admin_map[d]
        await call.message.edit_text(prompt, reply_markup=kb_back("admin"))
        await state.set_state(st)
        await call.answer()
        return

    await call.answer()

# ──────────────────────────────────────────────
# FSM ХЭНДЛЕРЫ
# ──────────────────────────────────────────────
async def _bad(msg: Message):
    await msg.answer(f'{e("5870657884844462243","❌")} Отправь корректное число.')

@dp.message(AdminState.waiting_ton_balance)
async def fsm_balance(message: Message, state: FSMContext):
    try:
        settings["ton_balance"] = round(float(message.text.replace(",", ".")), 4)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Баланс: <b>{settings["ton_balance"]} TON</b>',
            reply_markup=kb_main()
        )
    except ValueError:
        await _bad(message)

@dp.message(AdminState.waiting_fp_star_price)
async def fsm_fp_star(message: Message, state: FSMContext):
    try:
        settings["fp_star_price"] = round(float(message.text.replace(",", ".")), 4)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Курс 1 ⭐ на FP: <b>{settings["fp_star_price"]} ₽</b>',
            reply_markup=kb_admin()
        )
    except ValueError:
        await _bad(message)

@dp.message(AdminState.waiting_tg_market_commission)
async def fsm_tg_comm(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",", "."))
        if not (0 <= v <= 100): raise ValueError
        settings["tg_market_commission"] = round(v / 100, 6)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Комиссия TG Market: <b>{round(settings["tg_market_commission"]*100, 2)}%</b>',
            reply_markup=kb_admin()
        )
    except ValueError:
        await message.answer(f'{e("5870657884844462243","❌")} Введи число от 0 до 100.')

@dp.message(AdminState.waiting_portals_withdraw_fee)
async def fsm_portals_fee(message: Message, state: FSMContext):
    try:
        settings["portals_withdraw_fee"] = round(float(message.text.replace(",", ".")), 6)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Вывод с Portals: <b>{settings["portals_withdraw_fee"]} TON</b>',
            reply_markup=kb_admin()
        )
    except ValueError:
        await _bad(message)

@dp.message(AdminState.waiting_mrkt_withdraw_fee)
async def fsm_mrkt_fee(message: Message, state: FSMContext):
    try:
        settings["mrkt_withdraw_fee"] = round(float(message.text.replace(",", ".")), 6)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Вывод с MRKT: <b>{settings["mrkt_withdraw_fee"]} TON</b>',
            reply_markup=kb_admin()
        )
    except ValueError:
        await _bad(message)

@dp.message(AdminState.waiting_min_profit)
async def fsm_min_profit(message: Message, state: FSMContext):
    try:
        settings["min_profit"] = round(float(message.text.replace(",", ".")), 2)
        await state.clear()
        await message.answer(
            f'{e("5870633910337015697","✅")} Мин. порог: <b>{settings["min_profit"]}%</b>',
            reply_markup=kb_admin()
        )
    except ValueError:
        await _bad(message)

# ──────────────────────────────────────────────
# ПАРСЕРЫ
# ──────────────────────────────────────────────
def get_gifts_mapping(only_names: bool = True):
    r = curl_requests.get("https://cdn.changes.tg/gifts/id-to-name.json", verify=True)
    return [v for v in r.json().values()] if only_names else r.json()

async def parse_portals():
    try:
        r = curl_requests.get(
            "https://portal-market.com/api/collections?offset=0&limit=150", timeout=45
        )
        rj   = r.json()
        data = rj["collections"]
        for i in data:
            portals_gifts_data[i["name"]] = float(i["floor_price"])
        print(f"[Portals] {len(portals_gifts_data)} подарков")
    except Exception as ex:
        print("[Portals] Ошибка:", ex)

async def parse_mrkt(token: str):
    try:
        r  = curl_requests.get(
            "https://api.tgmrkt.io/api/v1/gifts/collections",
            headers={"Authorization": token}, json=None
        )
        rj = r.json()
        mrkt_gifts_data.clear()
        for g in rj:
            if g["name"].isdigit():
                continue
            mrkt_gifts_data[g["name"]] = _nano_to_ton(g["floorPriceNanoTons"])
        print(f"[MRKT] {len(mrkt_gifts_data)} подарков")
    except Exception as ex:
        print("[MRKT] Ошибка:", ex)

async def parse_tmarket():
    """Парсит TG Market через уже запущенный pyrogram_client"""
    try:
        gifts_mapping = get_gifts_mapping(only_names=False)
        total = len(gifts_mapping)
        for idx_g, (gift_id, gift_name) in enumerate(gifts_mapping.items(), 1):
            if idx_g % 20 == 0 or idx_g == 1:
                print(f"[TG Market] Прогресс: {idx_g}/{total}")
            tmarket_gifts = [
                g async for g in pyrogram_client.search_gifts_for_resale(
                    gift_id=int(gift_id), limit=110, order=GiftForResaleOrder.PRICE
                )
            ]
            for tg in tmarket_gifts:
                raw = getattr(tg, "raw", None)
                if raw is None:
                    continue
                resell = getattr(raw, "resell_amount", None)
                if not resell:
                    continue
                # Ищем StarsAmount (цена в звёздах, не TON)
                stars = None
                for item in resell:
                    type_name = type(item).__name__
                    if type_name == "StarsAmount":
                        stars = item.amount
                        break
                if stars:
                    tmarket_gifts_data[gift_name] = stars
                    break
            await asyncio.sleep(1.1)
        print(f"[TG Market] {len(tmarket_gifts_data)} подарков")
    except Exception as ex:
        print("[TG Market] Ошибка:", ex)

FP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

def parse_fp_rate():
    try:
        r = curl_requests.get(
            "https://funpay.com/account/balance",
            headers={"User-Agent": FP_UA, "Accept-Language": "ru,en;q=0.9"},
            cookies={"golden_key": GOLDEN_KEY},
            timeout=30,
        )
        page = unescape(r.text)
        target = None
        is_usdt = False
        for m in re.finditer(r'"extCurrency":"usdt_trc","feeInfo":"([^"]*)"', page):
            info = m.group(1)
            if "₽" in info:
                target = info
                is_usdt = True
                break
        if not target:
            for m in re.finditer(r'"extCurrency":"fps","feeInfo":"([^"]*)"', page):
                info = m.group(1)
                if "курс" in info and "$" in info:
                    target = info
                    break
        if not target:
            print("[FunPay] курс не найден (usdt_trc/fps)")
            return
        rate = re.search(r'курс\s+([\d.]+)', target)
        if not rate:
            print("[FunPay] курс не распарсился из:", target)
            return
        settings["fp_usdt_rate"] = float(rate.group(1))
        if is_usdt:
            pct = re.search(r'(\d+(?:\.\d+)?)\s*%', target)
            mn  = re.search(r'([\d.]+)\s*₽', target)
            if pct:
                settings["fp_withdraw_fee_pct"] = float(pct.group(1)) / 100
            if mn:
                settings["fp_withdraw_min_rub"] = float(mn.group(1))
        print(f"[FunPay] курс usdt: {settings['fp_usdt_rate']} | комиссия {settings['fp_withdraw_fee_pct']*100}% мин {settings['fp_withdraw_min_rub']}₽")
    except Exception as ex:
        print("[FunPay] курс не спарсился:", ex)

async def fetch_mrkt_token() -> str | None:
    """Получает токен MRKT через уже запущенный pyrogram_client"""
    try:
        peer    = await pyrogram_client.resolve_peer("mrkt")
        bot_raw = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
        app     = InputBotAppShortName(bot_id=bot_raw, short_name="app")
        web_view = await pyrogram_client.invoke(
            RequestAppWebView(peer=peer, app=app, platform="android")
        )
        init_data = unquote(
            web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0]
        )
        r  = curl_requests.post("https://api.tgmrkt.io/api/v1/auth", json={"data": init_data})
        rj = r.json()
        return rj.get("token") if rj else None
    except Exception as ex:
        print("[MRKT токен] Ошибка:", ex)
        return None

# ──────────────────────────────────────────────
# ГЛАВНЫЙ ЦИКЛ ПАРСИНГА
# Pyrogram запускается ОДИН РАЗ и не закрывается
# ──────────────────────────────────────────────
async def parse_loop():
    global mrkt_token

    await pyrogram_client.start()
    print("[Pyrogram] Клиент запущен")

    mrkt_token = await fetch_mrkt_token()
    print(f"[MRKT] Токен получен: {bool(mrkt_token)}")

    while True:
        try:
            parse_fp_rate()
            if mrkt_token:
                await parse_mrkt(mrkt_token)
                await parse_portals()
                await parse_tmarket()
            else:
                print("[MRKT] Нет токена — ждём...")
                await asyncio.sleep(2)
                mrkt_token = await fetch_mrkt_token()
                continue
        except Exception as ex:
            print(f"[Parser] Ошибка: {ex}")
            await asyncio.sleep(60)

        await asyncio.sleep(20)

# ──────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────
async def main():
    asyncio.create_task(parse_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
