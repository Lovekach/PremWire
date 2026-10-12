from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from premwire.editorial_desk import (
    EditorialMode,
    StoryCategory,
    classify_story,
    rank_candidates,
    recommended_mode,
    validated_decision,
)
from premwire.simple_editorial import SKIP_MULTI_STORY, assess_simple_source
from premwire.simple_entity_hygiene import check_source_local_output_quality
from premwire.simple_factual_guard import check_simple_factual_safety
from premwire.text_editor import OllamaTextEditor

from tests.helpers import editor_config


NOW = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)


def row(
    item_id: int,
    source: str,
    *,
    category: StoryCategory,
    importance: int,
    interest: int | None = None,
    novelty: int = 60,
    age_minutes: int = 5,
    mode: EditorialMode = EditorialMode.NORMAL,
) -> dict[str, object]:
    return {
        "id": item_id,
        "source_text": source,
        "clean_source_text": source,
        "source_published_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
        "discovered_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
        "editorial_category": category.value,
        "editorial_importance": importance,
        "editorial_interest": importance if interest is None else interest,
        "editorial_novelty": novelty,
        "editorial_reason": "test decision",
        "editorial_mode": mode.value,
    }


class EditorialDeskTests(unittest.TestCase):
    def test_match_result_is_football_and_first_class_category(self) -> None:
        source = (
            "Arsenal beat Leeds 3-1. Saka scored twice, Rice assisted, "
            "Arsenal had 17 shots, 68% possession and 2.4 xG."
        )
        self.assertTrue(assess_simple_source(source).allowed)
        self.assertEqual(classify_story(source), StoryCategory.MATCH_RESULT)

    def test_complex_match_uses_safe_summary_mode(self) -> None:
        source = (
            "Arsenal beat Leeds 3-1. Saka scored twice, Rice assisted, "
            "Arsenal had 17 shots, 68% possession and 2.4 xG."
        )
        self.assertEqual(recommended_mode(source), EditorialMode.SAFE_SUMMARY)

    def test_primary_result_wins_over_secondary_injury_detail(self) -> None:
        source = (
            "Халл Сити грохнул Манчестер Юнайтед со счётом 2:0. "
            "После матча три игрока команды травмировались."
        )
        self.assertEqual(classify_story(source), StoryCategory.MATCH_RESULT)

    def test_official_transfer_wins_over_background_goal_statistics(self) -> None:
        source = (
            "Астон Вилла подписала Леона Горецку. В прошлом сезоне он набрал "
            "восемь очков по системе гол плюс пас."
        )
        self.assertEqual(classify_story(source), StoryCategory.TRANSFER_OFFICIAL)

    def test_routine_transfer_is_lower_priority_than_major_match_result(self) -> None:
        candidates = (
            row(
                1,
                "Club signed midfielder Unknown Player on loan.",
                category=StoryCategory.TRANSFER_OFFICIAL,
                importance=42,
                interest=38,
            ),
            row(
                2,
                "Arsenal beat Leeds 3-1 in the Premier League.",
                category=StoryCategory.MATCH_RESULT,
                importance=90,
                interest=88,
            ),
        )
        ranked = rank_candidates(candidates, now=NOW, freshness_minutes=180)
        self.assertEqual(ranked[0].item_id, 2)

    def test_major_transfer_can_outrank_ordinary_match(self) -> None:
        candidates = (
            row(
                1,
                "OFFICIAL: Major star completes a record transfer.",
                category=StoryCategory.TRANSFER_OFFICIAL,
                importance=98,
                interest=96,
                novelty=90,
            ),
            row(
                2,
                "Two mid-table teams drew 1-1.",
                category=StoryCategory.MATCH_RESULT,
                importance=63,
                interest=60,
            ),
        )
        ranked = rank_candidates(candidates, now=NOW, freshness_minutes=180)
        self.assertEqual(ranked[0].item_id, 1)

    def test_qwen_ten_point_scale_is_normalized_and_obvious_category_is_stable(self) -> None:
        source = (
            "OFFICIAL: Vancouver Whitecaps have signed midfielder Sebastian Ramirez "
            "from Fortaleza on loan until June 2027."
        )
        decision = validated_decision(
            source,
            {
                "category": "TRANSFER_UPDATE",
                "importance": 3,
                "interest": 4,
                "novelty": 2,
                "reason": "Routine official signing",
                "recommended_mode": "SAFE_SUMMARY",
            },
        )
        self.assertEqual(decision.category, StoryCategory.TRANSFER_OFFICIAL)
        self.assertEqual((decision.importance, decision.interest, decision.novelty), (30, 40, 20))
        self.assertEqual(decision.mode, EditorialMode.NORMAL)

    def test_diversity_breaks_close_transfer_match_scores(self) -> None:
        candidates = (
            row(
                1,
                "Club signed another player.",
                category=StoryCategory.TRANSFER_OFFICIAL,
                importance=71,
                interest=70,
            ),
            row(
                2,
                "Team won 2-1.",
                category=StoryCategory.MATCH_RESULT,
                importance=68,
                interest=68,
            ),
        )
        ranked = rank_candidates(
            candidates,
            now=NOW,
            freshness_minutes=180,
            recent_categories=(StoryCategory.TRANSFER_OFFICIAL,) * 3,
        )
        self.assertEqual(ranked[0].item_id, 2)
        self.assertGreater(ranked[0].diversity_adjustment, 0)

    def test_diversity_does_not_suppress_huge_breaking_story(self) -> None:
        candidates = (
            row(
                1,
                "OFFICIAL: Major star completed a record transfer.",
                category=StoryCategory.TRANSFER_OFFICIAL,
                importance=98,
                interest=95,
                novelty=90,
            ),
            row(
                2,
                "Team won an ordinary match 2-1.",
                category=StoryCategory.MATCH_RESULT,
                importance=68,
                interest=68,
            ),
        )
        ranked = rank_candidates(
            candidates,
            now=NOW,
            freshness_minutes=180,
            recent_categories=(StoryCategory.TRANSFER_OFFICIAL,) * 3,
        )
        self.assertEqual(ranked[0].item_id, 1)

    def test_player_word_does_not_turn_transfer_into_match_news(self) -> None:
        source = (
            "Фабрицио Романо: Лейпциг лидирует в борьбе за подписание "
            "Кристофера Нкунку. Игрок хотел бы вернуться в Германию."
        )
        self.assertEqual(classify_story(source), StoryCategory.TRANSFER_UPDATE)

    def test_career_match_statistics_do_not_override_transfer_offer(self) -> None:
        source = (
            "Арсенал близок к продаже Габриэла Мартинелли в Аль-Хилаль. "
            "Саудовский клуб предлагает €50 млн. За Арсенал вингер провёл "
            "278 матчей и забил 62 гола."
        )
        self.assertEqual(classify_story(source), StoryCategory.TRANSFER_UPDATE)

    def test_recent_same_story_gets_soft_repetition_penalty(self) -> None:
        candidates = (
            row(
                1,
                "Фабрицио Романо: Леон Горецка переходит в Астон Виллу.",
                category=StoryCategory.TRANSFER_UPDATE,
                importance=90,
                interest=88,
            ),
            row(
                2,
                "Арсенал победил Лидс 3-1.",
                category=StoryCategory.MATCH_RESULT,
                importance=72,
                interest=72,
            ),
        )
        ranked = rank_candidates(
            candidates,
            now=NOW,
            freshness_minutes=180,
            recent_story_texts=(
                "Aston Villa have signed midfielder Leon Goretzka on a free transfer.",
            ),
        )
        self.assertEqual(ranked[0].item_id, 2)
        repeated = next(candidate for candidate in ranked if candidate.item_id == 1)
        self.assertEqual(repeated.repetition_penalty, -30.0)

    def test_high_risk_complexity_is_mode_not_automatic_defer(self) -> None:
        source = (
            "Chelsea beat Arsenal 4-3. Palmer scored twice. Chelsea had 21 shots, "
            "62% possession and 3.1 xG while Arsenal had 14 shots and 2.2 xG."
        )
        self.assertTrue(assess_simple_source(source).allowed)
        self.assertEqual(recommended_mode(source), EditorialMode.SAFE_SUMMARY)

    def test_ambiguous_headline_shorthand_uses_safe_summary(self) -> None:
        source = "Бальде и Барселона — ВСЁ! Жорже Мендеш ищет клиенту новый клуб."
        self.assertEqual(recommended_mode(source), EditorialMode.SAFE_SUMMARY)

    def test_broken_multi_story_source_still_defers_before_qwen(self) -> None:
        source = (
            "Arsenal beat Leeds 3-1. 🦁 Albion "
            "Chelsea signed another player. 🦁 Albion"
        )
        decision = assess_simple_source(source)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, SKIP_MULTI_STORY)

    def test_editorial_knowledge_reason_cannot_enter_article_rewrite(self) -> None:
        source = "Manchester United beat Hull City 2-0."
        decision = validated_decision(
            source,
            {
                "category": "MATCH_RESULT",
                "importance": 92,
                "interest": 90,
                "novelty": 80,
                "reason": "Manchester United is globally famous and needs context",
                "recommended_mode": "SAFE_SUMMARY",
            },
        )
        editor = OllamaTextEditor(editor_config())
        with patch.object(
            editor,
            "_generate_simple_safe_summary",
            return_value="Манчестер Юнайтед победил Халл Сити — 2:0.",
        ) as generate:
            output = editor.simple_rewrite(
                "Test source",
                source,
                editorial_mode=decision.mode.value,
            )
        generate.assert_called_once_with(source)
        self.assertNotIn("globally", output.casefold())
        self.assertNotIn("famous", output.casefold())

    def test_existing_numeric_status_and_name_guards_still_pass(self) -> None:
        source = (
            "OFFICIAL: Al Wasl have signed striker Mehdi Taremi from Olympiacos "
            "for an undisclosed fee."
        )
        output = (
            "Аль-Васл подписал нападающего Мехди Тареми из Олимпиакоса. "
            "Сумма трансфера не разглашается."
        )
        self.assertEqual(check_simple_factual_safety(source, output), ())
        self.assertEqual(check_source_local_output_quality(source, output), ())


if __name__ == "__main__":
    unittest.main()
