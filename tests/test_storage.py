from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from premwire.models import IncomingItem, IngestResult
from premwire.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tempdir.name) / "premwire.db")
        self.storage.initialise()

    def test_hot_publication_queries_have_dedicated_indexes(self) -> None:
        with self.storage.connect() as db:
            item_indexes = {
                row[1] for row in db.execute("PRAGMA index_list(items)").fetchall()
            }
            event_indexes = {
                row[1] for row in db.execute("PRAGMA index_list(events)").fetchall()
            }
        self.assertIn("idx_items_recent_publication", item_indexes)
        self.assertIn("idx_events_type_created", event_indexes)

    def test_runtime_state_roundtrip(self) -> None:
        self.assertIsNone(self.storage.get_runtime_state("telegram_offset"))
        self.storage.set_runtime_state("telegram_offset", "41")
        self.assertEqual(self.storage.get_runtime_state("telegram_offset"), "41")
        self.storage.set_runtime_state("telegram_offset", "42")
        self.assertEqual(self.storage.get_runtime_state("telegram_offset"), "42")

    def test_text_only_retirement_preserves_delivered_state_and_releases_new_text(self) -> None:
        delivered = self.storage.ingest(self.item(external="visual-delivered")).item_id
        self.storage.set_schedule(delivered, datetime.now(timezone.utc))
        self.storage.save_draft(delivered, "Уже опубликованный текст.")
        self.storage.prepare_visual_pending(delivered)
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET telegram_message_id=501 WHERE id=?",
                (delivered,),
            )

        self.assertEqual(self.storage.retire_visual_handoffs_for_text_only(), 1)
        self.assertEqual(self.storage.get_item(delivered)["state"], "published")

        undelivered = self.storage.ingest(self.item(external="visual-new")).item_id
        self.storage.set_schedule(undelivered, datetime.now(timezone.utc))
        self.storage.save_draft(undelivered, "Новый текст.")
        self.storage.prepare_visual_pending(undelivered)

        self.assertEqual(self.storage.retire_visual_handoffs_for_text_only(), 1)
        self.assertEqual(self.storage.get_item(undelivered)["state"], "approved")

    def test_publication_drain_roundtrip_and_auto_clear(self) -> None:
        self.storage.start_publication_drain([7, 3, 7], interval_minutes=2)
        self.assertEqual(self.storage.publication_drain(), ({3, 7}, 2))
        self.storage.finish_publication_drain_item(3)
        self.assertEqual(self.storage.publication_drain(), ({7}, 2))
        self.storage.finish_publication_drain_item(7)
        self.assertEqual(self.storage.publication_drain(), (set(), 0))

    def test_quality_rejection_is_terminal_not_live_review_queue(self) -> None:
        item_id = self.storage.ingest(self.item(external="quality")).item_id
        self.storage.set_schedule(item_id, datetime.now(timezone.utc))
        self.storage.mark_editor_failed(item_id, "new_named_entity")
        self.assertEqual(self.storage.get_item(item_id)["state"], "rejected_quality")
        self.assertEqual(self.storage.review_queue(), [])

    def test_quality_soft_pass_is_recorded_without_changing_state(self) -> None:
        item_id = self.storage.ingest(self.item(external="soft-pass")).item_id
        self.storage.record_quality_soft_pass(
            item_id, "resolution_only", stage="photo"
        )
        self.assertEqual(self.storage.get_item(item_id)["state"], "queued")
        with self.storage.connect() as db:
            event = db.execute(
                "SELECT event_type, payload_json FROM events WHERE item_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        self.assertEqual(event["event_type"], "quality_soft_pass")
        self.assertIn("resolution_only", event["payload_json"])

    def test_failed_revision_preserves_published_state(self) -> None:
        item_id = self.storage.ingest(self.item(external="published-revision")).item_id
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'needs_update', telegram_message_id = 222 "
                "WHERE id = ?",
                (item_id,),
            )

        self.storage.mark_editor_failed(item_id, "new_named_entity")

        row = self.storage.get_item(item_id)
        self.assertEqual(row["state"], "published")
        self.assertEqual(row["telegram_message_id"], 222)

    def test_archiving_holds_preserves_previously_published_items(self) -> None:
        unpublished_id = self.storage.ingest(self.item(external="held-new")).item_id
        published_id = self.storage.ingest(self.item(external="held-update")).item_id
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'needs_photo' WHERE id = ?",
                (unpublished_id,),
            )
            db.execute(
                "UPDATE items SET state = 'needs_headline_review', telegram_message_id = 333 "
                "WHERE id = ?",
                (published_id,),
            )

        self.assertEqual(self.storage.archive_quality_holds(), 2)
        self.assertEqual(self.storage.get_item(unpublished_id)["state"], "rejected_quality")
        self.assertEqual(self.storage.get_item(published_id)["state"], "published")

    def test_fresh_start_clears_pending_and_preserves_published_history(self) -> None:
        cutoff = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
        pending_id = self.storage.ingest(self.item(external="pending")).item_id
        published_id = self.storage.ingest(self.item(external="published", text="Other news")).item_id
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'published', telegram_message_id = 123 WHERE id = ?",
                (published_id,),
            )

        cleared = self.storage.clear_unpublished_backlog(cutoff)

        self.assertEqual(cleared, (pending_id,))
        self.assertEqual(self.storage.get_item(pending_id)["state"], "rejected_manual")
        self.assertEqual(self.storage.get_item(published_id)["state"], "published")
        self.assertEqual(self.storage.get_runtime_state("fresh_start_cutoff"), cutoff.isoformat())

    def test_fresh_start_returns_only_unpublished_ids_for_cleanup(self) -> None:
        cutoff = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
        pending_id = self.storage.ingest(self.item(external="pending-item")).item_id
        published_id = self.storage.ingest(
            self.item(external="published-item", text="Other news")
        ).item_id
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'ready_for_review' WHERE id = ?",
                (pending_id,),
            )
            db.execute(
                "UPDATE items SET state = 'published', telegram_message_id = 99 WHERE id = ?",
                (published_id,),
            )

        item_ids = self.storage.clear_unpublished_backlog(cutoff)

        self.assertEqual(item_ids, (pending_id,))
        self.assertEqual(self.storage.get_item(published_id)["state"], "published")

    def test_pre_cutoff_feed_item_cannot_return_to_queue(self) -> None:
        cutoff = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
        item_id = self.storage.ingest(self.item(external="old")).item_id
        self.assertTrue(self.storage.reject_unpublished_before_cutoff(item_id, cutoff))
        self.assertEqual(self.storage.get_item(item_id)["state"], "rejected_manual")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def item(
        source: str = "source-a",
        external: str = "1",
        text: str = "News",
    ) -> IncomingItem:
        return IncomingItem(
            source_id=source,
            external_id=external,
            text=text,
            canonical_url=f"https://example.test/{source}/{external}",
            published_at=datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc),
        )

    def test_new_unchanged_and_updated_item(self) -> None:
        first = self.storage.ingest(self.item())
        unchanged = self.storage.ingest(self.item())
        updated = self.storage.ingest(self.item(text="Corrected news"))
        self.assertEqual(first.result, IngestResult.NEW)
        self.assertEqual(unchanged.result, IngestResult.UNCHANGED)
        self.assertEqual(updated.result, IngestResult.UPDATED)
        self.assertEqual(first.item_id, updated.item_id)

    def test_media_url_is_persisted_and_late_media_does_not_requeue_item(self) -> None:
        first = self.storage.ingest(self.item())
        with_media = IncomingItem(
            source_id="source-a",
            external_id="1",
            text="News",
            canonical_url="https://example.test/source-a/1",
            published_at=datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc),
            media_url="https://cdn.example.test/player.jpg",
        )
        unchanged = self.storage.ingest(with_media)
        self.assertEqual(unchanged.result, IngestResult.UNCHANGED)
        self.assertEqual(
            self.storage.get_item(first.item_id)["media_url"],
            "https://cdn.example.test/player.jpg",
        )

    def test_exact_cross_source_duplicate_is_not_queued(self) -> None:
        first = self.storage.ingest(self.item())
        duplicate = self.storage.ingest(
            self.item(source="source-b", external="2", text="News")
        )
        self.assertEqual(duplicate.result, IngestResult.DUPLICATE)
        self.assertEqual(duplicate.duplicate_of, first.item_id)
        self.assertEqual(len(self.storage.queued_items()), 1)

    def test_initialise_repairs_legacy_active_state_with_duplicate_pointer(self) -> None:
        first = self.storage.ingest(self.item())
        duplicate = self.storage.ingest(
            self.item(source="source-b", external="2", text="News")
        )
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state='queued', scheduled_for=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), duplicate.item_id),
            )
        self.storage.initialise()
        row = self.storage.get_item(duplicate.item_id)
        self.assertEqual(row["state"], "duplicate")
        self.assertEqual(row["duplicate_of"], first.item_id)
        self.assertIsNone(row["scheduled_for"])

    def test_queue_uses_editorial_priority_without_transfer_or_source_bias(self) -> None:
        general_other = self.storage.ingest(
            self.item(source="source-other", external="10", text="Тренер подвёл итоги матча")
        ).item_id
        general_priority_source = self.storage.ingest(
            self.item(
                source="telegram_mtransfers",
                external="11",
                text="Футболист рассказал о подготовке к сезону",
            )
        ).item_id
        urgent_other = self.storage.ingest(
            self.item(source="source-other", external="12", text="СРОЧНО: клуб сменил тренера")
        ).item_id
        loan_priority_source = self.storage.ingest(
            self.item(
                source="telegram_mtransfers",
                external="13",
                text="Нападающий перейдёт в аренду до конца сезона",
            )
        ).item_id

        for item_id, priority in (
            (general_other, 75.0),
            (general_priority_source, 50.0),
            (urgent_other, 90.0),
            (loan_priority_source, 60.0),
        ):
            self.storage.record_editorial_selection(
                item_id,
                priority=priority,
                diversity_adjustment=0.0,
                selected=False,
            )

        ordered = [int(row["id"]) for row in self.storage.queued_items()]
        self.assertEqual(
            ordered,
            [urgent_other, general_other, loan_priority_source, general_priority_source],
        )

    def test_near_cross_source_duplicate_with_signature_is_not_queued(self) -> None:
        first = self.storage.ingest(
            self.item(
                text="Энцо Фернандес получил красную карточку в финале чемпионата мира"
            )
        )
        duplicate = self.storage.ingest(
            self.item(
                source="source-b",
                external="2",
                text=(
                    "🔴 Энцо Фернандес получил КРАСНУЮ КАРТОЧКУ "
                    "в финале чемпионата мира. Albion Main"
                ),
            )
        )
        self.assertEqual(duplicate.result, IngestResult.DUPLICATE)
        self.assertEqual(duplicate.duplicate_of, first.item_id)

    def test_different_news_about_same_club_are_not_merged(self) -> None:
        self.storage.ingest(
            self.item(text="Челси согласовал трансфер молодого защитника из Бразилии")
        )
        different = self.storage.ingest(
            self.item(
                source="source-b",
                external="2",
                text="Челси назначил нового главного тренера перед стартом сезона",
            )
        )
        self.assertEqual(different.result, IngestResult.NEW)

    def test_same_transfer_story_with_different_wording_is_not_queued(self) -> None:
        first = self.storage.ingest(
            self.item(
                text=(
                    "Morgan Rogers has signed a six-year Chelsea contract after "
                    "his transfer from Aston Villa was completed."
                )
            )
        )
        duplicate = self.storage.ingest(
            self.item(
                source="source-b",
                external="2",
                text=(
                    "Chelsea agreed the Morgan Rogers deal in 48 hours. The move "
                    "from Aston Villa is now official after talks were completed."
                ),
            )
        )
        self.assertEqual(duplicate.result, IngestResult.DUPLICATE)
        self.assertEqual(duplicate.duplicate_of, first.item_id)

    def test_different_rumours_about_same_player_are_not_collapsed(self) -> None:
        self.storage.ingest(
            self.item(text="Arsenal keep Julian Alvarez on their summer transfer radar.")
        )
        different = self.storage.ingest(
            self.item(
                source="source-b",
                external="2",
                text="Barcelona view Julian Alvarez as their dream striker for next season.",
            )
        )
        self.assertEqual(different.result, IngestResult.NEW)

    def test_deleted_transfer_story_still_blocks_cross_source_copy(self) -> None:
        first = self.storage.ingest(
            self.item(text="Morgan Rogers officially signed for Chelsea from Aston Villa.")
        )
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'published', telegram_message_id = 77 WHERE id = ?",
                (first.item_id,),
            )
        self.storage.mark_deleted_by_editor(first.item_id)
        duplicate = self.storage.ingest(
            self.item(
                source="source-b",
                external="2",
                text="Chelsea completed the Morgan Rogers transfer after a deal with Aston Villa.",
            )
        )
        self.assertEqual(duplicate.result, IngestResult.DUPLICATE)
        self.assertEqual(duplicate.duplicate_of, first.item_id)

    def test_deleted_source_is_flagged_but_item_is_kept(self) -> None:
        first = self.storage.ingest(self.item())
        changed = self.storage.mark_source_deleted("source-a", "1")
        self.assertTrue(changed)
        self.assertIn("cancelled_source_deleted", self.storage.counts_by_state())
        self.assertEqual(first.result, IngestResult.NEW)

    def test_published_item_is_queued_for_deletion_with_source(self) -> None:
        first = self.storage.ingest(self.item())
        with self.storage.connect() as db:
            db.execute("UPDATE items SET state = 'published' WHERE id = ?", (first.item_id,))
        self.storage.mark_source_deleted("source-a", "1", delete_mirror=True)
        self.assertEqual(self.storage.counts_by_state(), {"needs_delete": 1})

    def test_first_history_can_be_marked_as_baseline(self) -> None:
        self.storage.ingest(self.item())
        self.assertFalse(self.storage.source_is_initialised("source-a"))
        changed = self.storage.complete_source_baseline("source-a")
        self.assertEqual(changed, 1)
        self.assertTrue(self.storage.source_is_initialised("source-a"))
        self.assertEqual(self.storage.counts_by_state(), {"baseline": 1})

    def test_review_approval_and_publication_lifecycle(self) -> None:
        item_id = self.storage.ingest(self.item()).item_id
        self.storage.set_schedule(item_id, datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc))
        self.storage.save_draft(item_id, "Арсенал начал переговоры о трансфере.")
        self.storage.prepare_text_only(item_id)
        self.storage.replace_review_draft(
            item_id,
            "Арсенал действительно начал переговоры. Сделка ещё не закрыта.",
        )
        self.storage.approve_item(item_id)
        self.assertEqual(self.storage.get_item(item_id)["state"], "approved")
        self.storage.mark_published(item_id, 321)
        row = self.storage.get_item(item_id)
        self.assertEqual(row["state"], "published")
        self.assertEqual(row["telegram_message_id"], 321)
        self.assertIn("действительно", row["draft_text"])

    def test_publication_snapshot_survives_later_source_update(self) -> None:
        item_id = self.storage.ingest(
            self.item(text="Исходный подтверждённый факт.")
        ).item_id
        self.storage.set_schedule(item_id, datetime.now(timezone.utc))
        self.storage.save_draft(item_id, "Опубликованный подтверждённый факт.")
        self.storage.prepare_text_only(item_id)
        self.storage.approve_item(item_id)
        self.storage.mark_published(item_id, 654)

        published = self.storage.get_item(item_id)
        self.assertEqual(
            published["published_text"],
            "Опубликованный подтверждённый факт.",
        )
        self.assertEqual(
            published["published_source_text"],
            "Исходный подтверждённый факт.",
        )

        self.storage.ingest(self.item(text="Позднее исправление источника."))
        updated = self.storage.get_item(item_id)
        self.assertEqual(updated["state"], "needs_update")
        self.assertIsNone(updated["draft_text"])
        self.assertEqual(
            updated["published_text"],
            "Опубликованный подтверждённый факт.",
        )
        self.assertEqual(
            updated["published_source_text"],
            "Исходный подтверждённый факт.",
        )

    def test_unreviewed_item_cannot_be_marked_published(self) -> None:
        item_id = self.storage.ingest(self.item()).item_id
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.storage.mark_published(item_id, 1)

    def test_scheduled_low_value_item_can_be_rejected(self) -> None:
        item_id = self.storage.ingest(self.item()).item_id
        self.storage.set_schedule(
            item_id,
            datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
        )
        self.storage.reject_item(item_id, "Недостаточно контекста")
        self.assertEqual(
            self.storage.get_item(item_id)["state"],
            "rejected_manual",
        )

    def test_bad_headline_is_held_out_of_publication(self) -> None:
        item_id = self.storage.ingest(
            self.item(external="headline-hold", text="Ван Боммел может возглавить Бельгию")
        ).item_id
        self.storage.set_schedule(item_id, datetime.now(timezone.utc))
        self.storage.save_draft(item_id, "Ван Боммел может возглавить сборную Бельгии.")
        self.storage.mark_needs_headline_review(item_id, "Незаконченная фраза")
        row = self.storage.get_item(item_id)
        self.assertEqual(row["state"], "needs_headline_review")

        self.storage.reset_for_editing(item_id)
        retried = self.storage.get_item(item_id)
        self.assertEqual(retried["state"], "scheduled")
        self.assertIsNone(retried["draft_text"])
        self.assertEqual(self.storage.approved_due(datetime.now(timezone.utc)), [])

    def test_published_edit_returns_through_review_before_update(self) -> None:
        item_id = self.storage.ingest(self.item()).item_id
        self.storage.set_schedule(item_id, datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc))
        self.storage.save_draft(item_id, "Первая версия.")
        self.storage.prepare_text_only(item_id)
        self.storage.approve_item(item_id)
        self.storage.mark_published(item_id, 88)

        self.storage.ingest(self.item(text="Исправленная версия"))
        self.assertEqual(self.storage.get_item(item_id)["state"], "needs_update")
        self.storage.save_draft(item_id, "Исправленная версия.")
        self.storage.prepare_text_only(item_id)
        self.assertEqual(self.storage.get_item(item_id)["state"], "ready_update_review")
        self.storage.approve_item(item_id)
        self.assertEqual(self.storage.get_item(item_id)["state"], "approved_update")
        self.storage.mark_updated(item_id)
        self.assertEqual(self.storage.get_item(item_id)["state"], "published")

    def test_missing_post_inside_visible_window_is_reconciled(self) -> None:
        for external in ("100", "101", "102"):
            self.storage.ingest(self.item(external=external, text=f"News {external}"))
        middle = self.storage.get_item(2)
        with self.storage.connect() as db:
            db.execute(
                "UPDATE items SET state = 'published', telegram_message_id = 50 WHERE id = ?",
                (int(middle["id"]),),
            )
        changed = self.storage.reconcile_source_window(
            "source-a", ("100", "102"), delete_mirror=True
        )
        self.assertEqual(changed, 1)
        self.assertEqual(self.storage.get_item(int(middle["id"]))["state"], "needs_delete")


if __name__ == "__main__":
    unittest.main()
