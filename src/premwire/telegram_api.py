from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from .http_transport import urlopen

import truststore


class TelegramApiError(RuntimeError):
    def __init__(self, message: str, *, rejected: bool = False,
                 retry_after: int | None = None) -> None:
        super().__init__(message)
        self.rejected = rejected
        self.retry_after = retry_after


def unicode_transport_roundtrip(text: str) -> bool:
    """Require strict UTF-8 preservation at the Telegram transport boundary."""
    if "\ufffd" in text:
        return False
    try:
        return text.encode("utf-8", errors="strict").decode("utf-8", errors="strict") == text
    except UnicodeError:
        return False


@dataclass(frozen=True, slots=True)
class BotVerification:
    bot_id: int
    username: str
    channel: str
    status: str
    can_post_messages: bool
    can_edit_messages: bool
    can_delete_messages: bool

    @property
    def ready_to_publish(self) -> bool:
        return (
            self.status in {"administrator", "creator"}
            and self.can_post_messages
            and self.can_edit_messages
            and self.can_delete_messages
        )


@dataclass(frozen=True, slots=True)
class ModeratorVerification:
    bot_id: int
    username: str
    chat: str
    status: str
    can_delete_messages: bool
    can_restrict_members: bool

    @property
    def ready_to_moderate(self) -> bool:
        return (
            self.status in {"administrator", "creator"}
            and self.can_delete_messages
            and self.can_restrict_members
        )


class TelegramBotApi:
    def __init__(self, token: str, timeout: int = 20) -> None:
        self.token = token.strip()
        self.timeout = timeout
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def call(self, method: str, **parameters: object) -> Any:
        body = urlencode(
            {
                key: json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                for key, value in parameters.items()
                if value is not None
            },
            encoding="utf-8",
            errors="strict",
        ).encode("utf-8", errors="strict")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Extract only delivery metadata. Never echo bodies/descriptions,
            # request URLs, network reasons or chained credential-bearing errors.
            error_payload = {}
            try:
                error_payload = json.loads(exc.read().decode('utf-8'))
            except (OSError, ValueError):
                pass
            if not isinstance(error_payload, dict):
                error_payload = {}
            parameters = error_payload.get('parameters', {})
            if not isinstance(parameters, dict):
                parameters = {}
            raise TelegramApiError(
                f"Telegram API returned HTTP {exc.code} for {method}",
                rejected=(error_payload.get('ok') is False and
                          error_payload.get('error_code') == exc.code and 400 <= exc.code < 500),
                retry_after=parameters.get('retry_after') if exc.code == 429 else None,
            ) from None
        except URLError as exc:
            raise TelegramApiError(
                f"Telegram API network error for {method}"
            ) from None
        except json.JSONDecodeError as exc:
            raise TelegramApiError(
                f"Telegram API returned invalid JSON for {method}"
            ) from None
        if not payload.get("ok"):
            code = payload.get('error_code')
            parameters = payload.get('parameters', {})
            if not isinstance(parameters, dict):
                parameters = {}
            raise TelegramApiError(f"{method} failed: API rejected request",
                rejected=(payload.get('ok') is False and type(code) is int and 400 <= code < 500),
                retry_after=parameters.get('retry_after') if code == 429 else None)
        return payload.get("result")

    def verify_channel(self, channel: str) -> BotVerification:
        bot = self.call("getMe")
        if not isinstance(bot, dict):
            raise TelegramApiError("getMe returned an unexpected result")
        membership = self.call(
            "getChatMember", chat_id=channel, user_id=int(bot["id"])
        )
        if not isinstance(membership, dict):
            raise TelegramApiError("getChatMember returned an unexpected result")
        return BotVerification(
            bot_id=int(bot["id"]),
            username=str(bot.get("username", "")),
            channel=channel,
            status=str(membership.get("status", "unknown")),
            can_post_messages=bool(membership.get("can_post_messages", False)),
            can_edit_messages=bool(membership.get("can_edit_messages", False)),
            can_delete_messages=bool(membership.get("can_delete_messages", False)),
        )

    def verify_moderator(self, chat: str) -> ModeratorVerification:
        bot = self.call("getMe")
        if not isinstance(bot, dict):
            raise TelegramApiError("getMe returned an unexpected result")
        membership = self.call("getChatMember", chat_id=chat, user_id=int(bot["id"]))
        if not isinstance(membership, dict):
            raise TelegramApiError("getChatMember returned an unexpected result")
        return ModeratorVerification(
            bot_id=int(bot["id"]),
            username=str(bot.get("username", "")),
            chat=chat,
            status=str(membership.get("status", "unknown")),
            can_delete_messages=bool(membership.get("can_delete_messages", False)),
            can_restrict_members=bool(membership.get("can_restrict_members", False)),
        )

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 20,
    ) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=[
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
            ],
        )
        if not isinstance(result, list):
            raise TelegramApiError("getUpdates returned an unexpected result")
        return [value for value in result if isinstance(value, dict)]

    def get_chat_member(self, chat: str | int, user_id: int) -> dict[str, Any]:
        result = self.call("getChatMember", chat_id=chat, user_id=user_id)
        if not isinstance(result, dict):
            raise TelegramApiError("getChatMember returned an unexpected result")
        return result

    def ban_chat_member(
        self,
        chat: str | int,
        user_id: int,
        revoke_messages: bool = True,
    ) -> None:
        self.call(
            "banChatMember",
            chat_id=chat,
            user_id=user_id,
            revoke_messages="true" if revoke_messages else "false",
        )

    def unban_chat_member(self, chat: str | int, user_id: int) -> None:
        self.call(
            "unbanChatMember",
            chat_id=chat,
            user_id=user_id,
            only_if_banned="true",
        )

    def restrict_chat_member(
        self,
        chat: str | int,
        user_id: int,
        timeout_seconds: int,
    ) -> None:
        until_date = int(time.time()) + int(timeout_seconds)
        self.call(
            "restrictChatMember",
            chat_id=chat,
            user_id=user_id,
            permissions={
                "can_send_messages": False,
                "can_send_audios": False,
                "can_send_documents": False,
                "can_send_photos": False,
                "can_send_videos": False,
                "can_send_video_notes": False,
                "can_send_voice_notes": False,
                "can_send_polls": False,
                "can_send_other_messages": False,
                "can_add_web_page_previews": False,
            },
            use_independent_chat_permissions="true",
            until_date=until_date,
        )

    def send_text(self, channel: str, text: str) -> int:
        message = self.call(
            "sendMessage",
            chat_id=channel,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )
        if not isinstance(message, dict) or "message_id" not in message:
            raise TelegramApiError("sendMessage returned an unexpected result")
        return int(message["message_id"])

    def send_photo(self, channel: str, photo: Path, caption: str) -> int:
        """Send one explicitly approved media canary without enabling visual production."""
        selected = photo.resolve()
        if not selected.is_file():
            raise ValueError(f"Telegram photo does not exist: {selected}")
        if not unicode_transport_roundtrip(caption):
            raise ValueError("Telegram caption is not valid UTF-8")
        boundary = f"PremWire{secrets.token_hex(12)}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )

        field("chat_id", channel)
        field("caption", caption)
        field("parse_mode", "HTML")
        media_type = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
        parts.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    'Content-Disposition: form-data; name="photo"; '
                    f'filename="{selected.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
                selected.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            )
        )
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendPhoto",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TelegramApiError(
                f"Telegram API returned HTTP {exc.code} for sendPhoto"
            ) from None
        except URLError as exc:
            raise TelegramApiError(
                "Telegram API network error for sendPhoto"
            ) from None
        except json.JSONDecodeError as exc:
            raise TelegramApiError("Telegram API returned invalid JSON for sendPhoto") from None
        if not payload.get("ok"):
            raise TelegramApiError(
                "sendPhoto failed: Telegram API rejected the request"
            )
        message = payload.get("result")
        if not isinstance(message, dict) or "message_id" not in message:
            raise TelegramApiError("sendPhoto returned an unexpected result")
        return int(message["message_id"])

    def edit_message_media(
        self,
        channel: str,
        message_id: int,
        photo: Path,
        caption: str,
    ) -> int:
        """Upgrade an existing text post to a photo post without a second message."""

        selected = photo.resolve()
        if not selected.is_file():
            raise ValueError(f"Telegram photo does not exist: {selected}")
        if not unicode_transport_roundtrip(caption):
            raise ValueError("Telegram caption is not valid UTF-8")
        boundary = f"PremWire{secrets.token_hex(12)}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                (
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                        "ascii"
                    ),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )

        field("chat_id", channel)
        field("message_id", str(int(message_id)))
        field(
            "media",
            json.dumps(
                {
                    "type": "photo",
                    "media": "attach://photo",
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                ensure_ascii=False,
            ),
        )
        media_type = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
        parts.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    'Content-Disposition: form-data; name="photo"; '
                    f'filename="{selected.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
                selected.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            )
        )
        request = Request(
            f"https://api.telegram.org/bot{self.token}/editMessageMedia",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TelegramApiError(
                f"Telegram API returned HTTP {exc.code} for editMessageMedia"
            ) from None
        except URLError as exc:
            raise TelegramApiError(
                "Telegram API network error for editMessageMedia"
            ) from None
        except json.JSONDecodeError as exc:
            raise TelegramApiError(
                "Telegram API returned invalid JSON for editMessageMedia"
            ) from None
        if not payload.get("ok"):
            raise TelegramApiError(
                "editMessageMedia failed: Telegram API rejected the request"
            )
        message = payload.get("result")
        if not isinstance(message, dict) or "message_id" not in message:
            raise TelegramApiError("editMessageMedia returned an unexpected result")
        return int(message["message_id"])

    def set_message_reaction(
        self,
        channel: str,
        message_id: int,
        emoji: str,
        is_big: bool = False,
    ) -> None:
        reaction = emoji.strip()
        if not reaction:
            return
        self.call(
            "setMessageReaction",
            chat_id=channel,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": reaction}],
            is_big="true" if is_big else "false",
        )

    def edit_text(self, channel: str, message_id: int, text: str) -> None:
        self.call(
            "editMessageText",
            chat_id=channel,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )

    def delete_message(self, channel: str, message_id: int) -> None:
        self.call("deleteMessage", chat_id=channel, message_id=message_id)
