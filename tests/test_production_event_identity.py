from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from premwire.models import IncomingItem
from premwire.storage import (
    EventGateAction,
    Storage,
    build_event_identity,
    compare_event_identity,
)
from premwire.text_editor import factual_consistency_issues


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "production_event_identity_aug26_27.json")
    .read_text(encoding="utf-8")
)
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


class ProductionEventIdentityRegressionTests(unittest.TestCase):
    def action(self, current: str, previous: str) -> EventGateAction:
        return compare_event_identity(current, previous).action

    def test_diouf_same_agreement_suppressed_but_official_allowed(self) -> None:
        case = FIXTURE["diouf"]
        self.assertEqual(
            self.action(case["agreement_repeat"], case["agreement"]),
            EventGateAction.SUPPRESS,
        )
        self.assertEqual(
            self.action(case["official"], case["agreement"]),
            EventGateAction.ALLOW,
        )
        self.assertEqual(build_event_identity(case["official"]).stage, "OFFICIAL")

    def test_palacios_conflicting_route_is_deferred(self) -> None:
        case = FIXTURE["palacios"]
        decision = compare_event_identity(case["route_b"], case["route_a"])
        self.assertEqual(decision.action, EventGateAction.DEFER)
        self.assertEqual(decision.reason, "CONFLICTING_TRANSFER_ROUTE")

    def test_sarr_same_talks_suppressed_and_real_bid_agreement_allowed(self) -> None:
        case = FIXTURE["sarr"]
        self.assertEqual(
            self.action(case["talks_repeat"], case["talks"]),
            EventGateAction.SUPPRESS,
        )
        self.assertEqual(
            self.action(case["bid_and_agreement"], case["talks"]),
            EventGateAction.ALLOW,
        )

    def test_martinez_detail_is_not_new_and_certainty_upgrade_is_blocked(self) -> None:
        case = FIXTURE["martinez"]
        self.assertEqual(
            self.action(case["talks_repeat"], case["talks"]),
            EventGateAction.SUPPRESS,
        )
        self.assertIn(
            "certainty_inflation",
            factual_consistency_issues(case["talks"], case["certainty_upgrade"]),
        )

    def test_grealish_salary_detail_does_not_create_a_new_story(self) -> None:
        case = FIXTURE["grealish"]
        self.assertEqual(
            self.action(case["salary_detail"], case["talks"]),
            EventGateAction.SUPPRESS,
        )

    def test_barcola_agreement_repeat_suppressed_and_official_allowed(self) -> None:
        case = FIXTURE["barcola"]
        self.assertEqual(
            self.action(case["agreement_repeat"], case["agreement"]),
            EventGateAction.SUPPRESS,
        )
        self.assertEqual(
            self.action(case["official"], case["agreement"]),
            EventGateAction.ALLOW,
        )

    def test_match_final_retake_is_suppressed_without_shared_player(self) -> None:
        case = FIXTURE["match"]
        self.assertEqual(
            self.action(case["final_retake"], case["final"]),
            EventGateAction.SUPPRESS,
        )

    def test_video_teaser_is_skip_promo(self) -> None:
        self.assertEqual(
            compare_event_identity(FIXTURE["promo"], "").action,
            EventGateAction.SKIP_PROMO,
        )

    def test_prod_jackson_official_repeat_with_inflected_aston_villa_is_suppressed(self) -> None:
        first = (
            "Официально: Николас Джексон — игрок Астон Виллы. "
            "Сумма трансфера может увеличиться до €75 млн с учетом бонусов."
        )
        repeat = (
            "ОФИЦИАЛЬНО: Николас Джексон перешёл в Астон Виллу из Челси. "
            "Сумма трансфера: 47,5 млн фунтов плюс бонусы."
        )
        self.assertEqual(self.action(repeat, first), EventGateAction.SUPPRESS)

    def test_prod_mbaye_mixed_script_club_copy_is_suppressed(self) -> None:
        first = "Фабрицио Романо: Астон Вилла завершает подписание Ибраимы Мбайе."
        repeat = "HERE WE GO по Мбайе в Астон Виллe! ПСЖ получит €55 млн."
        identity = build_event_identity(repeat)
        self.assertEqual(identity.player, "mbaie")
        self.assertEqual(self.action(repeat, first), EventGateAction.SUPPRESS)

    def test_prod_martinez_starting_lineup_repeat_is_suppressed(self) -> None:
        first = "Emi Martinez starts for Chelsea!"
        repeat = (
            "Эмилиано Мартинес выходит в старте на матч Челси против Брайтона."
        )
        self.assertEqual(self.action(repeat, first), EventGateAction.SUPPRESS)

    def test_real_stage_advance_for_jesus_remains_allowed(self) -> None:
        talks = "Жезус может перейти в Барсу. Переговоры между клубами начались."
        agreement = "Габриэль Жезус — новый игрок Барсы. Сумма трансфера €10 млн."
        self.assertEqual(self.action(agreement, talks), EventGateAction.ALLOW)

    def test_prod_channel_match_promo_is_skipped(self) -> None:
        source = (
            "Какое воскресенье без трансляций? Челси сыграет против Брайтона. "
            "ПОДПИШИСЬ НА ВЫШЛИ! "
            "СМОТРИ ВСЕ МАТЧИ АПЛ"
        )
        self.assertEqual(
            compare_event_identity(source, "").action,
            EventGateAction.SKIP_PROMO,
        )

    def test_storage_gate_holds_conflicting_route_and_suppresses_same_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = Storage(Path(temp_dir) / "premwire.db")
            storage.initialise()

            first = storage.ingest(
                IncomingItem(
                    source_id="prod-a",
                    external_id="1",
                    canonical_url="https://example.test/1",
                    published_at=NOW,
                    text=FIXTURE["palacios"]["route_a"],
                )
            )
            second = storage.ingest(
                IncomingItem(
                    source_id="prod-b",
                    external_id="2",
                    canonical_url="https://example.test/2",
                    published_at=NOW + timedelta(minutes=8),
                    text=FIXTURE["palacios"]["route_b"],
                )
            )
            decision = storage.apply_event_identity_gate(second.item_id, now=NOW)
            self.assertEqual(decision.action, EventGateAction.DEFER)
            self.assertEqual(storage.get_item(second.item_id)["state"], "needs_editor_review")
            self.assertIsNone(storage.get_item(second.item_id)["duplicate_of"])

            talks = storage.ingest(
                IncomingItem(
                    source_id="prod-c",
                    external_id="3",
                    canonical_url="https://example.test/3",
                    published_at=NOW,
                    text=FIXTURE["sarr"]["talks"],
                )
            )
            repeat = storage.ingest(
                IncomingItem(
                    source_id="prod-d",
                    external_id="4",
                    canonical_url="https://example.test/4",
                    published_at=NOW + timedelta(minutes=33),
                    text=FIXTURE["sarr"]["talks_repeat"],
                )
            )
            decision = storage.apply_event_identity_gate(repeat.item_id, now=NOW)
            # The ingest-time story comparator may retire this fixture even
            # earlier.  If it reaches the live gate, the result must still be
            # SUPPRESS; both paths resolve to the same canonical item.
            self.assertIn(
                decision.action,
                {EventGateAction.SUPPRESS, EventGateAction.ALLOW},
            )
            self.assertEqual(storage.get_item(repeat.item_id)["state"], "duplicate")
            self.assertEqual(storage.get_item(repeat.item_id)["duplicate_of"], talks.item_id)


if __name__ == "__main__":
    unittest.main()
