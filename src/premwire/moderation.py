from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .config import ModerationConfig
from .storage import Storage
from .telegram_api import TelegramApiError, TelegramBotApi
from .x_manual import ManualXInbox


_TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_SPAM_PHRASES = (
    "заработок без вложений",
    "легкий заработок",
    "лёгкий заработок",
    "гарантированный доход",
    "пассивный доход",
    "пиши в лс",
    "пишите в лс",
    "мой канал",
    "наш канал",
    "подписывайся на",
    "подписывайтесь на",
    "ставки на спорт",
    "договорные матчи",
    "промокод",
    "букмекер",
    "казино",
    "раскрутка счета",
    "раскрутка счёта",
)


@dataclass(frozen=True, slots=True)
class ModerationAction:
    kind: str
    reason: str
    user_id: int
    message_id: int | None
    chat_id: int | str
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ModerationSummary:
    updates: int = 0
    deleted: int = 0
    timed_out: int = 0
    banned: int = 0
    ignored: int = 0
    manual_x_queued: int = 0


class ModerationEngine:
    """Conservative, in-memory spam classifier for one Telegram discussion chat."""

    def __init__(self, config: ModerationConfig) -> None:
        self.config = config
        self._messages: dict[int, deque[float]] = defaultdict(deque)
        self._repeats: dict[tuple[int, str], deque[float]] = defaultdict(deque)
        self._strikes: dict[int, int] = defaultdict(int)

    def _target_chat(self, message: dict[str, Any]) -> int | str | None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        configured = self.config.chat.lstrip("@").casefold()
        username = str(chat.get("username", "")).casefold()
        chat_id = chat.get("id")
        if username == configured:
            return int(chat_id) if isinstance(chat_id, int) else self.config.chat
        if str(chat_id) == self.config.chat:
            return int(chat_id) if isinstance(chat_id, int) else self.config.chat
        return None

    def _record_violation(
        self,
        user_id: int,
        message_id: int | None,
        chat_id: int | str,
        reason: str,
    ) -> ModerationAction:
        self._strikes[user_id] += 1
        strike = self._strikes[user_id]
        if strike == 1:
            return ModerationAction("delete", reason, user_id, message_id, chat_id)
        timeout_seconds = (
            self.config.second_timeout_seconds
            if strike == 2
            else self.config.third_timeout_seconds
        )
        return ModerationAction(
            "timeout",
            reason,
            user_id,
            message_id,
            chat_id,
            timeout_seconds,
        )

    def evaluate(
        self,
        message: dict[str, Any],
        *,
        now: float | None = None,
        record_activity: bool = True,
    ) -> list[ModerationAction]:
        chat_id = self._target_chat(message)
        if chat_id is None:
            return []
        timestamp = time.time() if now is None else now
        message_id_raw = message.get("message_id")
        message_id = int(message_id_raw) if isinstance(message_id_raw, int) else None

        actions: list[ModerationAction] = []
        for member in message.get("new_chat_members", ()):
            if not isinstance(member, dict) or not member.get("is_bot"):
                continue
            username = str(member.get("username", "")).casefold()
            user_id = member.get("id")
            if (
                isinstance(user_id, int)
                and user_id not in self.config.allowed_bot_ids
                and username not in self.config.allowed_bot_usernames
            ):
                actions.append(
                    ModerationAction(
                        "ban",
                        "неразрешённый бот добавлен в чат",
                        user_id,
                        message_id,
                        chat_id,
                    )
                )
        if actions:
            return actions

        sender = message.get("from")
        if not isinstance(sender, dict):
            return []
        user_id = sender.get("id")
        if not isinstance(user_id, int):
            return []
        username = str(sender.get("username", "")).casefold()
        if sender.get("is_bot"):
            if (
                user_id in self.config.allowed_bot_ids
                or username in self.config.allowed_bot_usernames
            ):
                return []
            return [
                ModerationAction(
                    "ban", "сообщение неразрешённого бота", user_id, message_id, chat_id
                )
            ]

        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
            return []
        folded = _SPACE_RE.sub(" ", text.casefold()).strip()

        external_handles = {
            match.group(1).casefold()
            for match in _TELEGRAM_LINK_RE.finditer(text)
            if match.group(1).casefold()
            not in self.config.allowed_telegram_handles
        }
        if external_handles:
            handles = ", ".join(sorted(external_handles))
            return [
                self._record_violation(
                    user_id,
                    message_id,
                    chat_id,
                    f"ссылка на чужой Telegram-ресурс: {handles}",
                )
            ]

        matched_phrase = next((phrase for phrase in _SPAM_PHRASES if phrase in folded), None)
        if matched_phrase is not None and (_URL_RE.search(text) or "@" in text):
            return [
                self._record_violation(
                    user_id,
                    message_id,
                    chat_id,
                    f"рекламный шаблон: {matched_phrase}",
                )
            ]

        if not record_activity:
            return []

        activity = self._messages[user_id]
        activity.append(timestamp)
        while activity and timestamp - activity[0] > self.config.flood_window_seconds:
            activity.popleft()
        if len(activity) > self.config.flood_max_messages:
            activity.clear()
            return [
                self._record_violation(
                    user_id, message_id, chat_id, "слишком много сообщений подряд"
                )
            ]

        fingerprint = hashlib.sha256(folded.encode("utf-8")).hexdigest()
        repeats = self._repeats[(user_id, fingerprint)]
        repeats.append(timestamp)
        while repeats and timestamp - repeats[0] > self.config.repeat_window_seconds:
            repeats.popleft()
        if len(repeats) >= 3:
            repeats.clear()
            return [
                self._record_violation(
                    user_id, message_id, chat_id, "одно сообщение повторено три раза"
                )
            ]
        return []


class ModerationService:
    OFFSET_KEY = "telegram_moderation_offset"

    def __init__(
        self,
        api: TelegramBotApi,
        storage: Storage,
        config: ModerationConfig,
        channel: str | None = None,
        auto_reaction: str = "",
        manual_x_inbox: ManualXInbox | None = None,
    ) -> None:
        self.api = api
        self.storage = storage
        self.config = config
        self.engine = ModerationEngine(config)
        self.channel = (channel or "").strip().lstrip("@").casefold()
        self.auto_reaction = auto_reaction.strip()
        self.manual_x_inbox = manual_x_inbox
        self._admin_cache: dict[tuple[int | str, int], tuple[bool, float]] = {}

    def _is_admin(self, chat_id: int | str, user_id: int) -> bool:
        key = (chat_id, user_id)
        now = time.time()
        cached = self._admin_cache.get(key)
        if cached is not None and now - cached[1] < 300:
            return cached[0]
        member = self.api.get_chat_member(chat_id, user_id)
        result = str(member.get("status", "")) in {"administrator", "creator"}
        self._admin_cache[key] = (result, now)
        return result

    def _apply(self, action: ModerationAction) -> tuple[int, int, int]:
        if self._is_admin(action.chat_id, action.user_id):
            print(
                f"moderation ignored admin user={action.user_id} reason={action.reason}"
            )
            return 0, 0, 0
        if action.message_id is not None:
            self.api.delete_message(action.chat_id, action.message_id)
        if action.kind == "ban":
            self.api.ban_chat_member(action.chat_id, action.user_id)
            print(f"moderation banned user={action.user_id} reason={action.reason}")
            return 1 if action.message_id is not None else 0, 0, 1
        if action.kind == "timeout":
            timeout_seconds = action.timeout_seconds or self.config.second_timeout_seconds
            self.api.restrict_chat_member(
                action.chat_id,
                action.user_id,
                timeout_seconds=timeout_seconds,
            )
            print(
                f"moderation timed_out user={action.user_id} "
                f"seconds={timeout_seconds} reason={action.reason}"
            )
            return 1 if action.message_id is not None else 0, 1, 0
        print(f"moderation deleted user={action.user_id} reason={action.reason}")
        return 1 if action.message_id is not None else 0, 0, 0

    def poll_once(self) -> ModerationSummary:
        raw_offset = self.storage.get_runtime_state(self.OFFSET_KEY)
        offset = int(raw_offset) if raw_offset is not None else None
        updates = self.api.get_updates(
            offset=offset,
            timeout=self.config.poll_timeout_seconds,
        )
        deleted = 0
        timed_out = 0
        banned = 0
        ignored = 0
        manual_x_queued = 0
        for update in updates:
            update_id = update.get("update_id")
            try:
                channel_post = update.get("channel_post")
                if isinstance(channel_post, dict):
                    chat = channel_post.get("chat")
                    username = (
                        str(chat.get("username", "")).casefold()
                        if isinstance(chat, dict)
                        else ""
                    )
                    message_id = channel_post.get("message_id")
                    if (
                        self.auto_reaction
                        and self.channel
                        and username == self.channel
                        and isinstance(message_id, int)
                    ):
                        chat_id = chat.get("id", f"@{self.channel}")
                        self.api.set_message_reaction(
                            chat_id,
                            message_id,
                            self.auto_reaction,
                        )
                    ignored += 1
                    continue
                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    ignored += 1
                    continue
                if self.manual_x_inbox is not None and "edited_message" not in update:
                    manual_result = self.manual_x_inbox.handle(message)
                    if manual_result is not None:
                        chat = message.get("chat")
                        chat_id = chat.get("id") if isinstance(chat, dict) else None
                        if isinstance(chat_id, int):
                            self.api.send_text(chat_id, manual_result.response_text())
                        manual_x_queued += manual_result.queued
                        continue
                actions = self.engine.evaluate(
                    message,
                    record_activity="edited_message" not in update,
                )
                if not actions:
                    ignored += 1
                for action in actions:
                    removed, restricted, blocked = self._apply(action)
                    deleted += removed
                    timed_out += restricted
                    banned += blocked
            except TelegramApiError as exc:
                print(f"moderation action failed update={update_id}: {exc}")
            finally:
                if isinstance(update_id, int):
                    self.storage.set_runtime_state(self.OFFSET_KEY, str(update_id + 1))
        return ModerationSummary(
            len(updates), deleted, timed_out, banned, ignored, manual_x_queued
        )
