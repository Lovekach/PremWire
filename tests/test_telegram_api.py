from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

from premwire.telegram_api import (
    BotVerification,
    ModeratorVerification,
    TelegramBotApi,
    unicode_transport_roundtrip,
)


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"ok": True, "result": {"message_id": 77}}).encode()


class TelegramApiTests(unittest.TestCase):
    def test_delete_permission_is_required(self) -> None:
        verification = BotVerification(
            bot_id=1,
            username="PremWireBot",
            channel="@PremWire",
            status="administrator",
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=False,
        )
        self.assertFalse(verification.ready_to_publish)

    def test_required_permissions_are_accepted(self) -> None:
        verification = BotVerification(
            bot_id=1,
            username="PremWireBot",
            channel="@PremWire",
            status="administrator",
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
        )
        self.assertTrue(verification.ready_to_publish)

    def test_moderator_requires_delete_and_restrict_permissions(self) -> None:
        verification = ModeratorVerification(
            bot_id=1,
            username="PremWireBot",
            chat="@PremWireChat",
            status="administrator",
            can_delete_messages=True,
            can_restrict_members=False,
        )
        self.assertFalse(verification.ready_to_moderate)

    def test_moderator_permissions_are_accepted(self) -> None:
        verification = ModeratorVerification(
            bot_id=1,
            username="PremWireBot",
            chat="@PremWireChat",
            status="administrator",
            can_delete_messages=True,
            can_restrict_members=True,
        )
        self.assertTrue(verification.ready_to_moderate)

    @patch("premwire.telegram_api.urlopen")
    def test_exact_cyrillic_text_is_utf8_and_lossless(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse()
        headline = "Филипп Йоргенсен стал игроком «Страсбура»"
        body = "Аренда рассчитана на сезон."
        text = f"{headline}\n\n{body}\nАБВГД абвгд Й ё «» — €25 млн"
        TelegramBotApi("123:token").send_text("@PremWire", text)
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("context", mocked_urlopen.call_args.kwargs)
        parameters = parse_qs(request.data.decode("utf-8"))
        self.assertTrue(unicode_transport_roundtrip(text))
        self.assertEqual(parameters["text"], [text])

    @patch("premwire.telegram_api.urlopen")
    def test_message_reaction_is_sent(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse()
        TelegramBotApi("123:token").set_message_reaction(
            "@PremWire", 77, "🔥"
        )
        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/setMessageReaction"))
        parameters = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(parameters["message_id"], ["77"])
        self.assertEqual(
            json.loads(parameters["reaction"][0]),
            [{"type": "emoji", "emoji": "🔥"}],
        )

    @patch("premwire.telegram_api.urlopen")
    def test_photo_canary_uses_multipart_and_preserves_caption(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as temporary:
            photo = Path(temporary) / "card.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            caption = "Тест визуального формата\n\nИсточник: PremWire"
            message_id = TelegramBotApi("123:token").send_photo(
                "@PremWire", photo, caption
            )
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(message_id, 77)
        self.assertTrue(request.full_url.endswith("/sendPhoto"))
        self.assertIn("multipart/form-data", request.headers["Content-type"])
        self.assertIn(caption.encode("utf-8"), request.data)
        self.assertIn(b'filename="card.png"', request.data)

    @patch("premwire.telegram_api.urlopen")
    def test_text_message_can_be_upgraded_to_photo_in_place(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as temporary:
            photo = Path(temporary) / "card.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            caption = "Новость\n\nИсточник: PremWire"
            message_id = TelegramBotApi("123:token").edit_message_media(
                "@PremWire", 77, photo, caption
            )
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(message_id, 77)
        self.assertTrue(request.full_url.endswith("/editMessageMedia"))
        self.assertIn(b'"media": "attach://photo"', request.data)
        self.assertIn(b'Content-Disposition: form-data; name="message_id"', request.data)
        self.assertIn(b"\r\n77\r\n", request.data)
        self.assertIn(caption.replace("\n", "\\n").encode("utf-8"), request.data)

    @patch("premwire.telegram_api.time.time", return_value=1_700_000_000)
    @patch("premwire.telegram_api.urlopen")
    def test_timeout_uses_restrict_chat_member(
        self, mocked_urlopen, _mocked_time
    ) -> None:
        mocked_urlopen.return_value = FakeResponse()
        TelegramBotApi("123:token").restrict_chat_member(
            "@PremWireChat", 42, timeout_seconds=300
        )
        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/restrictChatMember"))
        self.assertIn(b"until_date=1700000300", request.data)
        self.assertIn(b"can_send_messages", request.data)


if __name__ == "__main__":
    unittest.main()
