import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import aiohttp

from access import PLANS, Access, access_db_path, matches_order

LOG = logging.getLogger(__name__)
INVOICE_TTL = 1800


class CryptoPayError(ValueError):
    pass


class CryptoPay:
    def __init__(self, http: aiohttp.ClientSession, token: str, testnet: bool):
        self.http = http
        self.token = token
        self.base = "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"
        self.bots = {"cryptotestnetbot"} if testnet else {"cryptobot", "send"}

    async def request(self, method: str, **body):
        async with self.http.post(f"{self.base}/{method}", json=body,
                                  headers={"Crypto-Pay-API-Token": self.token},
                                  allow_redirects=False) as response:
            status = response.status
            data = await response.json(content_type=None)
        if status != 200 or not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            name = error.get("name", "") if isinstance(error, dict) else error
            reason = name if isinstance(name, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", name) else f"HTTP {status}"
            raise CryptoPayError(f"{method}: {reason}")
        return data["result"]

    async def invoice(self, days: int, token: str) -> tuple[int, str]:
        result = await self.request(
            "createInvoice", currency_type="crypto", asset="TON", amount=PLANS[days],
            description=f"Доступ к NFT Tracker · {days} дн.", payload=token,
            allow_comments=False, allow_anonymous=False, expires_in=INVOICE_TTL,
        )
        try:
            valid = (result["status"] == "active" and result["asset"] == "TON"
                     and Decimal(str(result["amount"])) == Decimal(PLANS[days])
                     and result["payload"] == token)
        except InvalidOperation:
            valid = False
        if not valid:
            raise CryptoPayError("createInvoice: сумма, валюта или данные счёта не совпадают")
        url = result["bot_invoice_url"]
        parsed = urlsplit(url)
        start = parse_qs(parsed.query).get("start", [])
        if (parsed.scheme != "https" or parsed.netloc.lower() != "t.me"
                or parsed.path.strip("/").lower() not in self.bots
                or len(start) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", start[0])):
            raise CryptoPayError("createInvoice: некорректная ссылка bot_invoice_url")
        return int(result["invoice_id"]), url


async def poll_payments(access: Access, crypto: CryptoPay) -> None:
    while True:
        invoices = access.pending()
        if invoices:
            try:
                result = await crypto.request("getInvoices", invoice_ids=",".join(map(str, invoices)), count=len(invoices))
                items = result["items"] if isinstance(result, dict) else result
                for invoice in items:
                    if access.apply_invoice(invoice):
                        LOG.info("Доступ продлён по подтверждённой оплате Crypto Pay")
                access.checked(invoices)
            except CryptoPayError as exc:
                LOG.warning("Проверка Crypto Pay отложена (%s)", exc)
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOG.warning("Проверка Crypto Pay отложена (%s)", type(exc).__name__)
        await asyncio.sleep(15)


async def check_payments(root) -> int:
    token = os.getenv("CRYPTO_PAY_API_TOKEN", "").strip()
    if not token:
        print("Crypto Pay: в .env отсутствует CRYPTO_PAY_API_TOKEN.")
        return 1
    testnet = os.getenv("CRYPTO_PAY_TESTNET", "0") == "1"
    path = access_db_path(root).resolve()
    orders = {}
    if path.exists():
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
                orders = {invoice_id: (payload, amount, state) for invoice_id, payload, amount, state in db.execute("""
                    SELECT invoice_id, token, amount, state FROM access_orders WHERE invoice_id IS NOT NULL
                    ORDER BY rowid DESC LIMIT 100
                """)}
        except sqlite3.Error as exc:
            print(f"База оплаты недоступна: {exc}")
            return 1
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as http:
            crypto = CryptoPay(http, token, testnet)
            await crypto.request("getMe")
            print(f"Crypto Pay API: доступен ({'testnet' if testnet else 'mainnet'}).")
            if not orders:
                print("В базе пока нет созданных счетов.")
                return 0
            result = await crypto.request("getInvoices", invoice_ids=",".join(map(str, orders)), count=len(orders))
            invoices = result["items"] if isinstance(result, dict) else result
    except CryptoPayError as exc:
        print(f"Crypto Pay API: {exc}")
        return 1
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        print(f"Crypto Pay API: {type(exc).__name__}")
        return 1
    matched = [invoice for invoice in invoices if invoice.get("invoice_id") in orders
               and matches_order(invoice, *orders[invoice["invoice_id"]][:2])]
    paid = [invoice for invoice in matched if invoice.get("status") == "paid"]
    credited = sum(orders[invoice["invoice_id"]][2] == "paid" for invoice in paid)
    active = sum(invoice.get("status") == "active" for invoice in matched)
    print(f"Счетов в базе: {len(orders)}. Сверено с API: {len(matched)}. Ожидают оплаты: {active}.")
    print(f"Оплачено по API: {len(paid)}. Доступ начислен: {credited}.")
    if not paid:
        print("Для полной проверки оплатите один счёт в выбранной сети и повторите команду.")
    return int(credited != len(paid))
