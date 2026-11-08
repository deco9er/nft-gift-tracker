import asyncio
import sqlite3

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient, errors, functions, types, utils

FEED_KEY = "__feed__"
FEED_NAME = "Лента изменений"


class TrackerForum:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.lock = asyncio.Lock()
        self.topic_lock = asyncio.Lock()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tracker_forum (
                id INTEGER PRIMARY KEY CHECK (id = 1), peer_id INTEGER NOT NULL,
                access_hash INTEGER NOT NULL, invite TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tracker_topics (
                collection TEXT PRIMARY KEY, thread_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracker_pins (thread_id INTEGER PRIMARY KEY);
        """)
        if "feed_ready" not in {row[1] for row in db.execute("PRAGMA table_info(tracker_forum)")}:
            with db:
                db.execute("ALTER TABLE tracker_forum ADD COLUMN feed_ready INTEGER NOT NULL DEFAULT 0")
        if "join_ready" not in {row[1] for row in db.execute("PRAGMA table_info(tracker_forum)")}:
            with db:
                db.execute("ALTER TABLE tracker_forum ADD COLUMN join_ready INTEGER NOT NULL DEFAULT 0")

    def state(self) -> tuple[int, int, str] | None:
        return self.db.execute(
            "SELECT peer_id, access_hash, invite FROM tracker_forum WHERE id = 1"
        ).fetchone()

    def destination(self) -> tuple[int, str] | None:
        state = self.state()
        if state and state[2]:
            return utils.get_peer_id(types.PeerChannel(state[0])), state[2]
        return None

    def join_link(self) -> str | None:
        row = self.db.execute("SELECT invite FROM tracker_forum WHERE id = 1 AND join_ready = 1").fetchone()
        return row[0] if row and row[0] else None

    async def secure_invite(self, client: TelegramClient | None, bot: Bot) -> None:
        async with self.lock:
            state = self.state()
            if state is None or not state[2]:
                return
            channel_id, access_hash, old_invite = state
            chat_id = utils.get_peer_id(types.PeerChannel(channel_id))
            me = await bot.get_me()
            member = await bot.get_chat_member(chat_id, me.id)
            if not (getattr(member, "can_restrict_members", False) and getattr(member, "can_invite_users", False)):
                if client is None or not client.is_connected():
                    raise ValueError("Сессия ещё не подключена для выдачи прав в группе.")
                bot_peer = await client.get_input_entity(f"@{me.username}")
                await client(functions.channels.EditAdminRequest(
                    types.InputChannel(channel_id, access_hash), bot_peer,
                    types.ChatAdminRights(manage_topics=True, invite_users=True, ban_users=True), "Трекер",
                ))
            if self.join_link():
                return
            try:
                invite = await bot.edit_chat_invite_link(chat_id, old_invite, creates_join_request=True)
            except TelegramBadRequest as exc:
                if "expired" not in exc.message.lower():
                    await bot.revoke_chat_invite_link(chat_id, old_invite)
                invite = await bot.create_chat_invite_link(chat_id, name="Трекер NFT", creates_join_request=True)
            with self.db:
                self.db.execute("UPDATE tracker_forum SET invite = ?, join_ready = 1 WHERE id = 1",
                                (invite.invite_link,))

    def feed_ready(self) -> bool:
        row = self.db.execute("SELECT feed_ready FROM tracker_forum WHERE id = 1").fetchone()
        return bool(row and row[0])

    def mark_feed_ready(self) -> None:
        with self.db:
            self.db.execute("UPDATE tracker_forum SET feed_ready = 1 WHERE id = 1")

    async def setup(self, client: TelegramClient | None, bot: Bot) -> str:
        async with self.lock:
            if destination := self.destination():
                return destination[1]
            if client is None or not client.is_connected():
                raise ValueError("Пользовательская сессия ещё не подключена. Повторите /group позже.")
            me = await bot.get_me()
            if not me.username:
                raise ValueError("У бота отсутствует username.")
            if getattr(me, "can_join_groups", None) is False:
                raise ValueError("В @BotFather разрешите боту вступать в группы, затем повторите /group.")
            bot_peer = await client.get_input_entity(f"@{me.username}")
            if not isinstance(bot_peer, types.InputPeerUser) or bot_peer.user_id != me.id:
                raise ValueError("Не удалось найти Telegram-аккаунт бота.")
            state = self.state()
            if state is None:
                result = await client(functions.channels.CreateChannelRequest(
                    title="Трекер NFT-подарков",
                    about="Новые листинги и изменения цен по коллекциям.",
                    megagroup=True,
                    forum=True,
                ))
                channel = next((chat for chat in result.chats if isinstance(chat, types.Channel) and chat.forum), None)
                if channel is None or channel.access_hash is None:
                    raise ValueError("Telegram не вернул доступ к созданной форум-группе.")
                with self.db:
                    self.db.execute(
                        "INSERT INTO tracker_forum (id, peer_id, access_hash) VALUES (1, ?, ?)",
                        (channel.id, channel.access_hash),
                    )
                state = self.state()
            channel_id, access_hash, _ = state
            channel = types.InputChannel(channel_id, access_hash)
            chat_id = utils.get_peer_id(types.PeerChannel(channel_id))
            try:
                await client(functions.channels.InviteToChannelRequest(channel, [bot_peer]))
            except errors.UserAlreadyParticipantError:
                pass
            await client(functions.channels.EditAdminRequest(
                channel, bot_peer, types.ChatAdminRights(manage_topics=True, invite_users=True, ban_users=True), "Трекер",
            ))
            invite = await bot.create_chat_invite_link(chat_id, name="Трекер NFT", creates_join_request=True)
            with self.db:
                self.db.execute("UPDATE tracker_forum SET invite = ?, join_ready = 1 WHERE id = 1",
                                (invite.invite_link,))
            return invite.invite_link

    async def topic(self, bot: Bot, chat_id: int, collection: str, title: str) -> int:
        async with self.topic_lock:
            saved = self.db.execute(
                "SELECT thread_id FROM tracker_topics WHERE collection = ?", (collection,)
            ).fetchone()
            if saved:
                return saved[0]
            topic = await bot.create_forum_topic(chat_id, " ".join(title.split())[:128] or collection[:128])
            with self.db:
                self.db.execute("INSERT INTO tracker_topics VALUES (?, ?)", (collection, topic.message_thread_id))
            return topic.message_thread_id

    async def pin_topics(self, client: TelegramClient, bot: Bot, topics: dict[str, str]) -> None:
        state = self.state()
        destination = self.destination()
        if not state or not destination:
            return
        peer = types.InputPeerChannel(state[0], state[1])
        for key, title in topics.items():
            thread_id = await self.topic(bot, destination[0], key, title)
            if self.db.execute("SELECT 1 FROM tracker_pins WHERE thread_id = ?", (thread_id,)).fetchone():
                continue
            await client(functions.messages.UpdatePinnedForumTopicRequest(peer, thread_id, True))
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO tracker_pins VALUES (?)", (thread_id,))

    def forget_missing_topic(self, collection: str, error: TelegramBadRequest) -> bool:
        if "thread not found" not in error.message.lower():
            return False
        with self.db:
            self.db.execute("DELETE FROM tracker_topics WHERE collection = ?", (collection,))
            if collection == FEED_KEY:
                self.db.execute("UPDATE tracker_forum SET feed_ready = 0 WHERE id = 1")
        return True
