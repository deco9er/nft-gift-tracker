import asyncio
import logging
from datetime import datetime
from time import time

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from telethon import errors, types

from access import MOSCOW

LOG = logging.getLogger(__name__)
MEMBER_ROLES = {"member", "administrator", "creator"}


class GroupAccess:
    def __init__(self, forum, access, admins: set[int]):
        self.forum = forum
        self.access = access
        self.admins = admins
        self.moderators = set(admins)
        self.ready = False
        self.lock = asyncio.Lock()
        self.db = forum.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tracker_members (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tracker_acl (
                user_id INTEGER PRIMARY KEY, banned INTEGER NOT NULL DEFAULT 0,
                allowed INTEGER NOT NULL DEFAULT 0, action TEXT NOT NULL DEFAULT ''
            );
        """)

    def flags(self, user_id: int) -> tuple[bool, bool]:
        row = self.db.execute("SELECT banned, allowed FROM tracker_acl WHERE user_id = ?", (user_id,)).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def allowed(self, user_id: int) -> bool:
        if user_id in self.admins:
            return True
        banned, white = self.flags(user_id)
        return not banned and (white or self.access.until(user_id) > time())

    def set_rule(self, user_id: int, action: str) -> None:
        if user_id in self.moderators:
            raise ValueError("Нельзя изменить доступ администратора.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tracker_acl (user_id) VALUES (?)", (user_id,))
            if action in ("ban", "unban"):
                self.db.execute("UPDATE tracker_acl SET banned = ?, action = ? WHERE user_id = ?",
                                (action == "ban", action, user_id))
            else:
                self.db.execute("UPDATE tracker_acl SET allowed = ? WHERE user_id = ?", (action == "allow", user_id))

    def member(self, user_id: int, present: bool) -> None:
        with self.db:
            if present:
                self.db.execute("INSERT OR IGNORE INTO tracker_members VALUES (?)", (user_id,))
            else:
                self.db.execute("DELETE FROM tracker_members WHERE user_id = ?", (user_id,))

    def owns_chat(self, chat_id: int) -> bool:
        destination = self.forum.destination()
        return bool(destination and destination[0] == chat_id)

    async def join(self, bot, chat_id: int, user_id: int) -> None:
        async with self.lock:
            if self.allowed(user_id):
                self.member(user_id, True)
                await bot.approve_chat_join_request(chat_id, user_id)
            else:
                await bot.decline_chat_join_request(chat_id, user_id)

    def update_member(self, update) -> None:
        member = update.new_chat_member
        user = member.user
        if member.status in {"administrator", "creator"}:
            self.moderators.add(user.id)
        elif user.id not in self.admins:
            self.moderators.discard(user.id)
        present = member.status in MEMBER_ROLES or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        self.member(user.id, present and not user.is_bot)

    async def apply_rules(self, bot, chat_id: int) -> None:
        rows = self.db.execute("SELECT user_id, action FROM tracker_acl WHERE action <> '' LIMIT 20").fetchall()
        for user_id, action in rows:
            async with self.lock:
                current = self.db.execute("SELECT action FROM tracker_acl WHERE user_id = ?", (user_id,)).fetchone()
                if not current or current[0] != action:
                    continue
                try:
                    if action == "ban":
                        await bot.ban_chat_member(chat_id, user_id)
                        self.member(user_id, False)
                    else:
                        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                except TelegramRetryAfter:
                    raise
                except TelegramAPIError as exc:
                    LOG.warning("Группа: действие %s отложено для %d (%s)", action, user_id, exc.message)
                    continue
                with self.db:
                    self.db.execute("UPDATE tracker_acl SET action = '' WHERE user_id = ? AND action = ?",
                                    (user_id, action))

    async def kick_expired(self, bot, chat_id: int) -> None:
        members = self.db.execute("SELECT user_id FROM tracker_members").fetchall()
        due = [uid for uid, in members if uid not in self.moderators and not self.allowed(uid)][:20]
        for user_id in due:
            async with self.lock:
                if self.allowed(user_id) or user_id in self.moderators or self.flags(user_id)[0]:
                    continue
                try:
                    member = await bot.get_chat_member(chat_id, user_id)
                    present = member.status == "member" or (
                        member.status == "restricted" and getattr(member, "is_member", False)
                    )
                    if present:
                        await bot.unban_chat_member(chat_id, user_id, only_if_banned=False)
                except TelegramRetryAfter:
                    raise
                except TelegramAPIError as exc:
                    LOG.warning("Группа: удаление участника %d отложено (%s)", user_id, exc.message)
                else:
                    self.member(user_id, False)

    async def sync_members(self, client, bot, chat_id: int) -> None:
        state = self.forum.state()
        if not state or client is None or not client.is_connected():
            return
        admins = await bot.get_chat_administrators(chat_id)
        self.moderators = self.admins | {admin.user.id for admin in admins}
        async for user in client.iter_participants(types.InputPeerChannel(state[0], state[1])):
            if not user.bot:
                self.member(user.id, True)

    async def watch(self, bot, get_client) -> None:
        next_sync = 0.0
        loop = asyncio.get_running_loop()
        while True:
            destination = self.forum.destination()
            if not destination:
                await asyncio.sleep(5)
                continue
            chat_id = destination[0]
            try:
                if not self.ready:
                    await self.forum.secure_invite(get_client(), bot)
                    self.ready = True
                if loop.time() >= next_sync and get_client() is not None:
                    next_sync = loop.time() + 900
                    try:
                        await self.sync_members(get_client(), bot, chat_id)
                    except errors.FloodWaitError as exc:
                        next_sync = loop.time() + exc.seconds + 1
                    except (OSError, TimeoutError, errors.RPCError):
                        next_sync = loop.time() + 30
                await self.apply_rules(bot, chat_id)
                await self.kick_expired(bot, chat_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except (TelegramAPIError, errors.RPCError, ValueError, OSError, TimeoutError) as exc:
                LOG.warning("Группа: проверка доступа отложена (%s)", type(exc).__name__)
            await asyncio.sleep(5)

    async def notify_payments(self, bot, group_keyboard) -> None:
        while True:
            for invoice_id, user_id, until in self.access.payment_notices():
                if not self.allowed(user_id):
                    self.access.mark_notified(invoice_id)
                    continue
                try:
                    await bot.send_message(
                        user_id,
                        f"Оплата подтверждена. Доступ до {datetime.fromtimestamp(until, MOSCOW):%d.%m %H:%M} МСК.",
                        reply_markup=group_keyboard(),
                    )
                except TelegramForbiddenError:
                    pass
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                    break
                except TelegramAPIError:
                    break
                self.access.mark_notified(invoice_id)
            await asyncio.sleep(5)
