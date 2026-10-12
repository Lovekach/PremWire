from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from premwire.models import IncomingItem
from premwire.publisher import (
    _assert_source_factual_safety,
    _delete_already_completed,
    process_publishing,
    process_visual_text_previews,
)
from premwire.storage import Storage
from premwire.telegram_api import TelegramApiError


class PublisherTests(unittest.TestCase):
    def test_final_gate_blocks_monthly_award_voting_promotion(self) -> None:
        row = {
            "clean_source_text": None,
            "source_text": (
                "Guiding their teams to maximum points at the start of the season. "
                "Time to get voting for August's Manager of the Month."
            ),
        }

        with self.assertRaisesRegex(
            ValueError,
            "SOURCE_POLICY_BLOCKED:award_voting_promotion",
        ):
            _assert_source_factual_safety(
                row,
                "Кандидаты претендуют на награду лучшему тренеру августа.",
            )

    def test_cached_award_voting_draft_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="x_premier_league",
                    external_id="manager-vote-cached",
                    text=(
                        "Guiding their teams to maximum points at the start of "
                        "the season. Time to get voting for August's Manager "
                        "of the Month."
                    ),
                    canonical_url="https://example.test/manager-vote-cached",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(
                item_id,
                "Кандидаты претендуют на награду лучшему тренеру августа.",
            )
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            storage.save_editorial_decision(
                item_id,
                category="MANAGER_NEWS",
                importance=50,
                interest=50,
                novelty=30,
                mode="NORMAL",
                reason="cached before the terminal source policy",
            )
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )

            with patch("premwire.publisher.TelegramBotApi") as api_class:
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            self.assertEqual(summary.published, 0)
            api_class.return_value.send_text.assert_not_called()
            self.assertEqual(storage.get_item(item_id)["state"], "rejected_quality")
            self.assertIn(
                "SOURCE_POLICY_BLOCKED:award_voting_promotion",
                " ".join(summary.warnings),
            )

    def test_visual_text_publishes_early_without_queuing_x(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="hybrid-early",
                    text="Arsenal confirmed a transfer update.",
                    canonical_url="https://example.test/hybrid-early",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc))
            storage.save_draft(item_id, "Арсенал подтвердил трансферную новость.")
            storage.prepare_visual_pending(item_id)
            config = SimpleNamespace(
                imagegen_worker=SimpleNamespace(enabled=True),
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
                editor=SimpleNamespace(max_output_chars=1200),
                sources=(),
                vk_publishing=SimpleNamespace(enabled=True),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 444
                first = process_visual_text_previews(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )
                second = process_visual_text_previews(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            row = storage.get_item(item_id)
            self.assertEqual(first.published, 1)
            self.assertEqual(second.published, 0)
            self.assertEqual(row["state"], "visual_pending")
            self.assertEqual(row["telegram_message_id"], 444)
            self.assertEqual(row["vk_status"], "pending")
            self.assertNotEqual(row["x_status"], "pending")
            api_class.return_value.send_text.assert_called_once()

    def test_published_story_duplicate_is_blocked_before_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            now = datetime.now(timezone.utc)
            first = storage.ingest(
                IncomingItem(
                    source_id="x_ben_jacobs",
                    external_id="fabinho-first",
                    text="Report concerning Fabinho at Trabzonspor.",
                    canonical_url="https://example.test/fabinho-first",
                    published_at=now - timedelta(minutes=30),
                )
            )
            repeat = storage.ingest(
                IncomingItem(
                    source_id="telegram_mtransfers",
                    external_id="fabinho-repeat",
                    text="Короткое обновление о Фабиньо и «Трабзонспоре».",
                    canonical_url="https://example.test/fabinho-repeat",
                    published_at=now,
                )
            )
            with storage.connect() as db:
                db.execute(
                    "UPDATE items SET state='published', telegram_message_id=518, "
                    "telegram_published_at=?, draft_text=? WHERE id=?",
                    (
                        (now - timedelta(minutes=25)).isoformat(),
                        "Фабиньо едет в «Трабзонспор» для оформления перехода.",
                        first.item_id,
                    ),
                )
                db.execute(
                    "UPDATE items SET state='approved', scheduled_for=?, draft_text=? "
                    "WHERE id=?",
                    (
                        (now - timedelta(minutes=1)).isoformat(),
                        "«Трабзонспор» подписал Фабиньо.",
                        repeat.item_id,
                    ),
                )
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )

            with patch("premwire.publisher.TelegramBotApi") as api_class:
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            row = storage.get_item(repeat.item_id)
            api_class.return_value.send_text.assert_not_called()
            self.assertEqual(summary.published, 0)
            self.assertEqual(
                (row["state"], row["duplicate_of"]),
                ("duplicate", first.item_id),
            )
            self.assertTrue(
                any("PUBLISHED_STORY_DUPLICATE_SUPPRESSED" in value for value in summary.warnings)
            )

    def test_telegram_send_failure_records_original_error_without_canary_name_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="telegram-failure",
                    text="Arsenal transfer news",
                    canonical_url="https://example.test/telegram-failure",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(item_id, "Арсенал сообщил о трансферной новости.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.side_effect = TelegramApiError(
                    "ORIGINAL_TELEGRAM_FAILURE"
                )
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            self.assertEqual(summary.published, 0)
            self.assertIn("ORIGINAL_TELEGRAM_FAILURE", summary.warnings[0])
            self.assertEqual(
                storage.get_item(item_id)["last_error"], "ORIGINAL_TELEGRAM_FAILURE"
            )
            with storage.connect() as db:
                events = db.execute(
                    "SELECT event_type FROM events WHERE item_id=? ORDER BY id",
                    (item_id,),
                ).fetchall()
            self.assertEqual(events[-1]["event_type"], "publish_failed")

    def test_impossible_football_term_is_repaired_by_light_sanitizer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="bad-term",
                    text="Клуб подписывает правого защитника.",
                    canonical_url="https://example.test/bad-term",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(item_id, "Клуб подписывает правового защитника.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(dry_run=False, channel="@PremWire", auto_reaction=""),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 900
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            self.assertEqual(summary.published, 1)
            sent = api_class.return_value.send_text.call_args.args[1]
            self.assertIn("правого защитника", sent)
            self.assertNotIn("правового защитника", sent)

    def test_explicit_missing_telegram_message_is_an_idempotent_delete(self) -> None:
        self.assertTrue(
            _delete_already_completed(
                RuntimeError(
                    "Telegram API returned HTTP 400 for deleteMessage: Bad Request: message to delete not found"
                )
            )
        )
        self.assertTrue(
            _delete_already_completed(
                RuntimeError(
                    "Telegram API returned HTTP 400 for deleteMessage: Bad Request: message can't be deleted"
                )
            )
        )
        self.assertFalse(
            _delete_already_completed(
                RuntimeError("Telegram API returned HTTP 400 for deleteMessage: Bad Request: chat not found")
            )
        )

    def test_final_gate_removes_known_club_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="quoted-club",
                    text="Игрок перешёл из Арсенала в Челси.",
                    canonical_url="https://example.test/quoted-club",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(item_id, "Игрок перешёл из «Арсенала» в Челси.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(dry_run=False, channel="@PremWire", auto_reaction=""),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 101
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            self.assertEqual(summary.published, 1)
            caption = api_class.return_value.send_text.call_args.args[1]
            self.assertEqual(caption, "Игрок перешёл из Арсенала в Челси")
            self.assertEqual(storage.get_item(item_id)["state"], "published")

    def test_final_gate_blocks_new_known_club_not_present_in_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="invented-club",
                    text="Chelsea are discussing a player.",
                    canonical_url="https://example.test/invented-club",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(item_id, "Челси обсуждает игрока с Интером.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(dry_run=False, channel="@PremWire", auto_reaction=""),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 901
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            self.assertEqual(summary.published, 0)
            self.assertIn("UNSUPPORTED_CLUB_ENTITY", " ".join(summary.warnings))
            self.assertNotEqual(storage.get_item(item_id)["state"], "published")

    def test_final_gate_removes_links_handles_and_subscription_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="policy",
                    text="Arsenal transfer news",
                    canonical_url="https://example.test/policy",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(
                item_id,
                "Арсенал начал переговоры, сообщил @Reporter. "
                "Подробнее: https://example.com. Подписывайтесь на канал!",
            )
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False,
                    channel="@PremWire",
                    auto_reaction="",
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 102
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)

            self.assertEqual(summary.published, 1)
            caption = api_class.return_value.send_text.call_args.args[1]
            self.assertEqual(caption, "Арсенал начал переговоры, сообщил")
            self.assertEqual(
                storage.get_item(item_id)["draft_text"],
                "Арсенал начал переговоры, сообщил",
            )

    def test_final_gate_blocks_broadcast_promo_even_for_cached_approved_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="telegram_vishli_football",
                    external_id="broadcast-promo",
                    text=(
                        "Сегодняшняя игра станет для Артеты 250-й в качестве тренера. "
                        "Подключайтесь к трансляции матча в 22:00."
                    ),
                    canonical_url="https://example.test/broadcast-promo",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(
                item_id, datetime.now(timezone.utc) - timedelta(minutes=1)
            )
            storage.save_draft(
                item_id,
                "Сегодняшняя игра станет для Артеты 250-й в качестве тренера.",
            )
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            storage.save_editorial_decision(
                item_id,
                category="MATCH_NEWS",
                importance=50,
                interest=50,
                novelty=30,
                mode="NORMAL",
                reason="cached before the terminal source policy",
            )
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )

            with patch("premwire.publisher.TelegramBotApi") as api_class:
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            self.assertEqual(summary.published, 0)
            api_class.return_value.send_text.assert_not_called()
            self.assertEqual(storage.get_item(item_id)["state"], "rejected_quality")
            self.assertIn(
                "SOURCE_POLICY_BLOCKED:live_broadcast_promotion",
                " ".join(summary.warnings),
            )

    def test_final_gate_blocks_cached_audience_commentary_repost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="telegram_vishli_football",
                    external_id="audience-commentary-repost",
                    text=(
                        "Скаутский отдел Манчестер Юнайтед переехал в комментарии "
                        "к трансляциям матча против Ипсвича. Зрители предлагали "
                        "варианты трансферных усилений для защиты команды."
                    ),
                    canonical_url="https://example.test/audience-commentary-repost",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(
                item_id,
                "Манчестер Юнайтед рассматривает усиление защиты.",
            )
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            storage.save_editorial_decision(
                item_id,
                category="TRANSFER_UPDATE",
                importance=50,
                interest=50,
                novelty=30,
                mode="NORMAL",
                reason="cached before the terminal source policy",
            )
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )

            with patch("premwire.publisher.TelegramBotApi") as api_class:
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            self.assertEqual(summary.published, 0)
            api_class.return_value.send_text.assert_not_called()
            self.assertEqual(storage.get_item(item_id)["state"], "rejected_quality")
            self.assertIn(
                "SOURCE_POLICY_BLOCKED:audience_commentary_not_news",
                " ".join(summary.warnings),
            )

    def test_repeated_terminal_factual_failure_is_retired_after_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "premwire.db")
            storage.initialise()
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="certainty-loop",
                    text="Chelsea are discussing a player.",
                    canonical_url="https://example.test/certainty-loop",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=1))
            storage.save_draft(item_id, "Челси обсуждает игрока с Интером.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(dry_run=False, channel="@PremWire", auto_reaction=""),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                first = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            reason = str(storage.get_item(item_id)["last_error"])
            self.assertIn("UNSUPPORTED_CLUB_ENTITY", reason)
            self.assertEqual(storage.get_item(item_id)["state"], "scheduled")

            storage.save_draft(item_id, "Челси обсуждает игрока с Интером.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                second = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)

            self.assertEqual(first.published + second.published, 0)
            api_class.return_value.send_text.assert_not_called()
            self.assertEqual(storage.get_item(item_id)["state"], "rejected_quality")

    def test_ready_post_waits_for_ten_minute_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()

            def approved(external: str) -> int:
                item_id = storage.ingest(
                    IncomingItem(
                        source_id="source",
                        external_id=external,
                        text=f"News {external}",
                        canonical_url=f"https://example.test/{external}",
                        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    )
                ).item_id
                storage.set_schedule(item_id, datetime.now(timezone.utc) - timedelta(minutes=5))
                storage.save_draft(item_id, f"Готовый пост {external}.")
                storage.prepare_text_only(item_id)
                storage.approve_item(item_id)
                return item_id

            first = approved("1")
            storage.mark_published(first, 101)
            approved("2")
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False,
                    channel="@PremWire",
                    auto_reaction="🔥",
                ),
                queue=SimpleNamespace(interval_minutes=10),
            )
            config.queue.max_posts_per_hour = 6
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 102
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            self.assertEqual(summary.published, 0)
            api_class.return_value.send_text.assert_not_called()

            with storage.connect() as db:
                db.execute(
                    "UPDATE events SET created_at = ? WHERE event_type = 'published'",
                    ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),),
                )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 102
                summary = process_publishing(config, {"TELEGRAM_BOT_TOKEN": "token"}, storage)
            self.assertEqual(summary.published, 1)
            api_class.return_value.send_text.assert_called_once()

    def test_breaking_or_official_post_bypasses_ten_minute_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()

            previous = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="previous",
                    text="Previous news",
                    canonical_url="https://example.test/previous",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            with storage.connect() as db:
                db.execute(
                    "UPDATE items SET state = 'published', telegram_message_id = 100 WHERE id = ?",
                    (previous,),
                )
                db.execute(
                    "INSERT INTO events(item_id, event_type, payload_json, created_at) "
                    "VALUES (?, 'published', '{}', ?)",
                    (previous, datetime.now(timezone.utc).isoformat()),
                )
            item_id = storage.ingest(
                IncomingItem(
                    source_id="source",
                    external_id="breaking",
                    text="Here we go: Arsenal have completed the signing.",
                    canonical_url="https://example.test/breaking",
                    published_at=datetime.now(timezone.utc),
                )
            ).item_id
            storage.set_schedule(item_id, datetime.now(timezone.utc))
            storage.save_draft(item_id, "Срочно: Арсенал завершил подписание игрока.")
            storage.prepare_text_only(item_id)
            storage.approve_item(item_id)
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 101
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )

            self.assertEqual(summary.published, 1)
            api_class.return_value.send_text.assert_called_once()

    def test_drain_post_uses_two_minute_gap_and_clears_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "premwire.db")
            storage.initialise()

            def approved(external: str) -> int:
                item_id = storage.ingest(
                    IncomingItem(
                        source_id="source",
                        external_id=external,
                        text=f"News {external}",
                        canonical_url=f"https://example.test/{external}",
                        published_at=datetime.now(timezone.utc),
                    )
                ).item_id
                storage.set_schedule(item_id, datetime.now(timezone.utc))
                storage.save_draft(item_id, f"Готовый пост {external}.")
                storage.prepare_text_only(item_id)
                storage.approve_item(item_id)
                return item_id

            first = approved("first")
            storage.mark_published(first, 100)
            second = approved("second")
            storage.start_publication_drain([second], interval_minutes=2)
            with storage.connect() as db:
                db.execute(
                    "UPDATE events SET created_at = ? WHERE event_type = 'published'",
                    ((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),),
                )
            config = SimpleNamespace(
                publishing=SimpleNamespace(
                    dry_run=False, channel="@PremWire", auto_reaction=""
                ),
                queue=SimpleNamespace(interval_minutes=10, max_posts_per_hour=6),
            )
            with patch("premwire.publisher.TelegramBotApi") as api_class:
                api_class.return_value.send_text.return_value = 101
                summary = process_publishing(
                    config, {"TELEGRAM_BOT_TOKEN": "token"}, storage
                )
            self.assertEqual(summary.published, 1)
            self.assertEqual(storage.publication_drain(), (set(), 0))


if __name__ == "__main__":
    unittest.main()
