from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time as clock
import unittest
from unittest.mock import Mock, patch

from premwire.daily_text_digest import (
    MOSCOW, SECTIONS, DigestLedger, DigestStory, collapse, day_window,
    daily_priority, digest_factual_issues, filter_items, publication_window, publish_prepared, render_digest, rewrite_batch, same_event, telegram_length,
    conflict_signature, SourceSuppression, suppression_matches,
    validated_prefix, same_health_story,
)
from premwire.daily_collection import collect_day, telegram_day, x_day
from urllib.parse import parse_qs, urlsplit
from premwire.models import IncomingItem, Source
from premwire.config import load_config
from premwire.editorial_desk import fallback_decision

NOW = datetime(2026, 10, 11, 20, 56, tzinfo=MOSCOW)


def item(text, i=1, hours=0):
    return IncomingItem("test", str(i), text, f"https://example.test/{i}", NOW - timedelta(hours=hours))


class DailyDigestTests(unittest.TestCase):
    def test_window_is_moscow_calendar_day(self):
        start, end = day_window(NOW.astimezone(timezone.utc))
        self.assertEqual(start.isoformat(), "2026-10-11T00:00:00+03:00")
        self.assertEqual(end, NOW)

    def test_naive_start_rejected(self):
        with self.assertRaises(ValueError):
            day_window(datetime(2026, 10, 11))

    def test_prominence_is_soft_ranking_not_gate(self):
        known = item("Liverpool football news.")
        other = item("Unknown football news.", 2)
        equal_score = fallback_decision("Football news.")
        self.assertGreater(daily_priority(known, equal_score), daily_priority(other, equal_score))

    def test_publish_window(self):
        for hour, minute, expected in [(20, 55, False), (20, 56, True), (21, 4, True), (21, 5, False)]:
            self.assertEqual(publication_window(NOW.replace(hour=hour, minute=minute)), expected)

    def test_same_event_higher_stage_wins(self):
        low = item("Liverpool are interested in signing Anthony Gordon from Newcastle.", 1, 10)
        high = item("Liverpool have reached an agreement to sign Anthony Gordon from Newcastle.", 2, 1)
        self.assertTrue(same_event(low, high))
        winners, conflicts = collapse([low, high, low])
        self.assertEqual(winners, [high])
        self.assertEqual(conflicts, [])

    def test_regressive_later_stage_not_winner(self):
        high = item("Liverpool have officially signed Anthony Gordon from Newcastle.", 1, 2)
        low = item("Liverpool are interested in signing Anthony Gordon from Newcastle.", 2, 1)
        self.assertEqual(collapse([low, high])[0], [high])

    def test_different_destination_not_collapsed(self):
        a = item("Liverpool are interested in signing Anthony Gordon from Newcastle.")
        b = item("Chelsea are interested in signing Anthony Gordon from Newcastle.", 2)
        self.assertEqual(len(collapse([a, b])[0]), 2)

    def test_final_conflicting_scores_fail_closed(self):
        a = item("Liverpool beat Chelsea 2-0 in the Premier League.")
        b = item("Liverpool beat Chelsea 3-0 in the Premier League.", 2)
        winners, rejects = collapse([a, b])
        self.assertEqual(winners, [])
        self.assertEqual(rejects[0]["reason"], "CONFLICTING_SCORE_OR_FEE")

    def test_no_news_no_publication_text(self):
        self.assertEqual(render_digest([], NOW.date()), ("", []))

    def test_one_news_empty_categories_disappear(self):
        story = DigestStory(item("Liverpool beat Chelsea 2-0."), "Ливерпуль победил Челси со счётом 2:0.",
                            SECTIONS[2], 80, 80)
        body, selected = render_digest([story], NOW.date())
        self.assertEqual(len(selected), 1)
        self.assertIn(SECTIONS[2], body)
        self.assertNotIn(SECTIONS[1], body)
        self.assertTrue(body.endswith("@PremWire"))
        self.assertNotIn("В вечернем обзоре", body)

    def test_busy_day_length_no_truncated_sentence(self):
        stories = [DigestStory(item(f"Unrelated story {i} result.", i), f"Новость {i}. " + "Важные сведения. " * 16,
                               SECTIONS[4], 70, 70) for i in range(30)]
        body, selected = render_digest(stories, NOW.date())
        self.assertLessEqual(telegram_length(body), 3900)
        self.assertLessEqual(len(selected), 20)
        self.assertGreater(len(selected), 1)
        for story in selected:
            self.assertIn(story.text, body)

    def test_utf16_astral_characters(self):
        self.assertEqual(telegram_length("⚽🔥"), 3)

    def test_render_rechecks_dedupe(self):
        source = item("Liverpool beat Chelsea 2-0.")
        stories = [DigestStory(source, "Ливерпуль победил Челси 2:0.", SECTIONS[2], 80, 80)] * 2
        self.assertEqual(len(render_digest(stories, NOW.date())[1]), 1)

    def test_ledger_daily_once(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            self.assertTrue(ledger.claim("2026-10-11"))
            ledger.state("2026-10-11", "PUBLISHED", "Digest", 123)
            self.assertFalse(ledger.claim("2026-10-11"))
            self.assertTrue(ledger.claim("2026-10-12"))
            ledger.db.close()

    def test_ambiguous_send_never_retried_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            ledger = DigestLedger(path)
            ledger.claim("2026-10-11")
            ledger.state("2026-10-11", "SENDING", "Digest")
            ledger.db.close()
            reopened = DigestLedger(path)
            self.assertFalse(reopened.claim("2026-10-11"))
            reopened.db.close()

    def test_empty_day_handled_once(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim("2026-10-11")
            ledger.state("2026-10-11", "NO_NEWS")
            self.assertFalse(ledger.claim("2026-10-11"))
            ledger.db.close()

    def test_delivery_once_and_html_escaped(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim(NOW.date().isoformat())
            api = Mock()
            api.send_text.return_value = 123
            self.assertEqual(publish_prepared(ledger, NOW.date(), "Новость < & >", api, "@test", now=NOW), 123)
            api.send_text.assert_called_once_with("@test", "Новость &lt; &amp; &gt;")
            with self.assertRaises(RuntimeError):
                publish_prepared(ledger, NOW.date(), "Новость", api, "@test", now=NOW)
            self.assertEqual(api.send_text.call_count, 1)
            ledger.db.close()

    def test_network_error_keeps_sending_and_never_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim(NOW.date().isoformat())
            api = Mock()
            api.send_text.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                publish_prepared(ledger, NOW.date(), "Новость", api, "@test", now=NOW)
            self.assertEqual(ledger.get_state(NOW.date().isoformat()), "SENDING")
            self.assertFalse(ledger.claim(NOW.date().isoformat()))
            api.send_text.assert_called_once()
            ledger.db.close()

    def test_late_or_wrong_day_never_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim(NOW.date().isoformat())
            api = Mock()
            for current in [NOW.replace(hour=21, minute=5), NOW + timedelta(days=1)]:
                with self.assertRaises(RuntimeError):
                    publish_prepared(ledger, NOW.date(), "Новость", api, "@test", now=current)
            api.send_text.assert_not_called()
            ledger.db.close()

    def test_delivery_rechecks_utf16_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim(NOW.date().isoformat())
            api = Mock()
            with self.assertRaises(RuntimeError):
                publish_prepared(ledger, NOW.date(), "🔥" * 2050, api, "@test", now=NOW)
            api.send_text.assert_not_called()
            ledger.db.close()

    def test_failed_generation_can_be_retried_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DigestLedger(Path(directory) / "ledger.sqlite3")
            ledger.claim("2026-10-11")
            ledger.state("2026-10-11", "FAILED_PRE_SEND")
            self.assertTrue(ledger.claim("2026-10-11"))
            ledger.db.close()

    def test_filter_promo_and_other_day(self):
        news = item("Ливерпуль победил Челси со счётом 2:0 в чемпионате Англии.", 1, 1)
        old = item(news.text, 2, 25)
        promo = item("Подключайтесь к трансляции матча Ливерпуля!", 3, 1)
        accepted, rejected = filter_items([news, old, promo], *day_window(NOW))
        self.assertEqual(accepted, [news])
        self.assertEqual(len(rejected), 1)

    def test_owner_suppression_kept(self):
        news = item("Ливерпуль победил Челси со счётом 2:0 в чемпионате Англии.", hours=1)
        self.assertEqual(filter_items([news], *day_window(NOW), suppressed=[news.text])[0], [])

    def test_player_intent_and_release_condition(self):
        source = "Барселона планирует подписать Дэвиса. Бавария готова отпустить футболиста, если поступит выгодное предложение."
        bad = "Дэвис готов перейти из Баварии в Барселону."
        issues = digest_factual_issues(source, bad)
        self.assertIn("UNSUPPORTED_PLAYER_INTENT", issues)
        self.assertIn("TRANSFER_RELEASE_CONDITION_REMOVED", issues)
        good = "Барселона планирует подписать Дэвиса. Бавария готова его отпустить при выгодном предложении."
        self.assertNotIn("UNSUPPORTED_PLAYER_INTENT", digest_factual_issues(source, good))
        self.assertNotIn("TRANSFER_RELEASE_CONDITION_REMOVED", digest_factual_issues(source, good))

    def test_near_age_not_exact_age(self):
        source = "Габи Жезус, почти 30-летний бразилец, забил 7 голов."
        self.assertIn("AGE_APPROXIMATION_REMOVED", digest_factual_issues(source, "30-летний Габи Жезус забил 7 голов."))

    def test_fan_preview_skipped(self):
        news = item("Фанаты Ливерпуля начинают собираться у стадиона с баннерами перед матчем.", hours=1)
        self.assertEqual(filter_items([news], *day_window(NOW))[0], [])

    def test_routine_coach_quotes_and_birthdays_skipped(self):
        for text in ['Michael Carrick: “We must start winning matches again”.',
                     'Today we remember Sir Bobby Charlton on his birthday. A true football legend']:
            self.assertEqual(filter_items([item(text, hours=1)], *day_window(NOW))[0], [])

    def test_short_goal_milestone_not_mistaken_for_caption(self):
        news = item("Lionel Messi with a brace for Inter Miami tonight to reach 934 career goals.", hours=1)
        self.assertEqual(filter_items([news], *day_window(NOW))[0], [news])

    def test_empty_teaser_not_a_digest_story(self):
        news = item("Уникальный случай во вчерашнем матче", hours=1)
        self.assertEqual(filter_items([news], *day_window(NOW))[0], [])

    def test_each_source_isolated_and_retry_bounded(self):
        editor = Mock()
        editor.config = load_config(Path(__file__).resolve().parents[1]).editor
        source = item("Ливерпуль победил Челси со счётом 2:0 в чемпионате Англии.")
        editor._simple_chat.side_effect = ["Ливерпуль победил Челси со счётом 7:0.",
                                          "Ливерпуль победил Челси со счётом 2:0 в чемпионате Англии."]
        stories, rejected = rewrite_batch(editor, [source], clock.monotonic() + 60)
        self.assertEqual(len(stories), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(editor._simple_chat.call_count, 2)

    def test_second_bad_rewrite_not_published(self):
        editor = Mock()
        editor.config = load_config(Path(__file__).resolve().parents[1]).editor
        source = item("Ливерпуль победил Челси со счётом 2:0 в чемпионате Англии.")
        editor._simple_chat.return_value = "Ливерпуль победил Челси со счётом 7:0."
        stories, rejected = rewrite_batch(editor, [source], clock.monotonic() + 60)
        self.assertEqual(stories, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(editor._simple_chat.call_count, 2)

    def test_source_error_isolated(self):
        sources = (Source("a", "telegram", "a", "a", "https://t.me/a"),
                   Source("b", "telegram", "b", "b", "https://t.me/b"))
        def fetch(source, *args):
            if source.id == "a":
                raise OSError("unavailable")
            return [item("Liverpool beat Chelsea 2-0.")], []
        with patch("premwire.daily_collection.telegram_day", side_effect=fetch):
            found, warnings = collect_day(sources, {}, *day_window(NOW))
        self.assertEqual(len(found), 1)
        self.assertEqual(warnings, ["a: unavailable (OSError)"])

    def test_media_only_pagination_bounded(self):
        source = Source("a", "telegram", "a", "a", "https://t.me/a")
        page = '<div data-post="a/100"><time datetime="2026-10-11T15:00:00Z"></time></div>'
        with patch("premwire.daily_collection.get_text", return_value=page) as mock:
            found, warnings = telegram_day(source, *day_window(NOW), float("inf"))
        self.assertEqual(found, [])
        self.assertEqual(mock.call_count, 2)
        self.assertTrue(warnings)

    def test_morning_pagination_ignores_unrelated_old_time(self):
        source = Source("a", "telegram", "a", "a", "https://t.me/a")
        def page(id_, date):
            return (f'<div class="tgme_widget_message" data-post="a/{id_}">'
                    f'<div class="tgme_widget_message_text">Liverpool signed a new player.</div>'
                    f'<time datetime="{date}"></time></div>')
        pages = [page(100, "2026-10-11T15:00:00Z") + '<time datetime="2025-01-01T00:00:00Z"></time>',
                 page(80, "2026-10-11T03:00:00Z"), page(60, "2026-10-10T20:00:00Z")]
        with patch("premwire.daily_collection.get_text", side_effect=pages) as fetch:
            found, warnings = telegram_day(source, *day_window(NOW), float("inf"))
        self.assertEqual({i.external_id for i in found}, {"100", "80"})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(warnings, [])

    def test_actual_injury_survives_lineup_ttl_and_routine_quote_filter(self):
        for text in ['Andoni Iraola: “Szoboszlai has a tight hamstring, this is why he’s not available today”.',
                     'Стартовые составы на матч Ливерпуль vs Ман Сити. Доминик Собослаи не попал в заявку из-за травмы задней поверхности бедра.']:
            with self.subTest(text=text):
                self.assertEqual(len(filter_items([item(text, hours=4)], *day_window(NOW))[0]), 1)
        bare = item("Dominik Szoboszlai, not in the squad for Liverpool today against Manchester City.", hours=4)
        self.assertEqual(filter_items([bare], *day_window(NOW))[1][0]["reason"], "EXPIRED_TRANSIENT_STORY")

    def test_source_backed_alias_and_club_case_not_new_entity(self):
        source = 'Арсенал вырвал победу у Лидса, а МЮ не смог дожать Тоттенхэм.'
        draft = 'Арсенал одержал победу над Лидсом, а Манчестер Юнайтед не смог дожать Тоттенхэм.'
        self.assertNotIn("new_named_entity", digest_factual_issues(source, draft))
        self.assertIn("new_named_entity", digest_factual_issues(source, draft + " Тренером стал Джанни."))

    def test_semantic_absence_and_football_affiliation(self):
        source = 'Доминик Собослаи не попал в заявку Ливерпуля из-за травмы.'
        self.assertIn("SQUAD_ABSENCE_NARROWED", digest_factual_issues(source,
                      'Доминик Собослаи не попал в стартовый состав Ливерпуля из-за травмы.'))
        self.assertIn("CLUB_AFFILIATION_REVERSED", digest_factual_issues(
            'Seedorf won the Champions League with three different clubs.',
            'Седорф выигрывал Лигу чемпионов у трёх разных команд.'))

    def test_referee_translation_preserves_attribution(self):
        source = 'Vinicius at risk for El Clásico following referee report saying he had an aggressive attitude.'
        good = 'Винисиус рискует пропустить Эль Класико. Арбитр сообщил об агрессивном поведении.'
        self.assertNotIn("ATTRIBUTION_DROPPED", digest_factual_issues(source, good))
        self.assertIn("ATTRIBUTION_DROPPED", digest_factual_issues(source, 'Винисиус агрессивно протестовал.'))
        self.assertIn("ABSENCE_MEANING_REVERSED", digest_factual_issues(source, 'Винисиус рискует сыграть в Эль Класико.'))

    def test_no_invented_medical_diagnosis_or_empty_context(self):
        self.assertIn("UNSUPPORTED_MEDICAL_DIAGNOSIS", digest_factual_issues(
            'Szoboszlai has a tight hamstring.', 'У Собослаи растяжение задней поверхности бедра.'))
        self.assertIn("EMPTY_EDITORIAL_CONTEXT", digest_factual_issues(
            'Liverpool signed Anthony Gordon.', 'Ливерпуль подписал Гордона. Подробности остаются неизвестными.'))
        self.assertIn('UNSUPPORTED_SPEECH_ATTRIBUTION', digest_factual_issues(
            'Игроки «Манчестер Юнайтед» не смогли обыграть трёх соперников с удалениями.',
            'Манчестер Юнайтед не смог обыграть трёх соперников с удалениями. Игроки клуба отмечают, что это неудача.'))

    def test_fee_units_and_score_typography_not_conflicts(self):
        self.assertEqual(conflict_signature('Fee €12m, final score 2-0.'),
                         conflict_signature('Сумма 12 млн евро, счёт 2:0.'))

    def test_independent_competitions_not_collapsed(self):
        a = item('Liverpool beat Chelsea 2-0 in the Premier League.')
        b = item('Liverpool beat Chelsea 3-0 in the FA Cup.', 2)
        self.assertFalse(same_event(a, b))
        self.assertEqual(len(collapse([a, b])[0]), 2)

    def test_exact_tombstone_not_future_similar_event(self):
        old = SourceSuppression('test', '1', 'Liverpool beat Chelsea 2-0 in the Premier League.')
        self.assertTrue(suppression_matches(item(old.text), old))
        self.assertFalse(suppression_matches(item('Liverpool beat Chelsea 3-0 in the FA Cup.', 2), old))

    def test_tombstone_same_day_same_stage_not_real_advance(self):
        old = SourceSuppression('test', '1', 'Liverpool are interested in signing Anthony Gordon from Newcastle.', NOW.isoformat())
        copy = item('Liverpool are interested in signing Anthony Gordon from Newcastle this summer.', 2)
        advanced = item('Liverpool have officially signed Anthony Gordon from Newcastle.', 3)
        self.assertTrue(suppression_matches(copy, old))
        self.assertFalse(suppression_matches(advanced, old))

    def test_kickoff_time_not_conflicting_score(self):
        self.assertEqual(conflict_signature('Medical at 18:30. Fee €12m.'), conflict_signature('Fee 12 млн евро.'))

    def test_stage_consolidation_is_explainable(self):
        audit = []
        a = item('Liverpool are interested in signing Anthony Gordon from Newcastle.', 1, 10)
        b = item('Liverpool have officially signed Anthony Gordon from Newcastle.', 2, 1)
        winners, conflicts = collapse([a, b], audit=audit)
        self.assertEqual(winners, [b])
        self.assertEqual(conflicts, [])
        self.assertEqual(audit[0]['winner'], b.canonical_url)

    def test_busy_day_can_reach_target_without_padding(self):
        stories = [DigestStory(item(f'Independent story {i} result.', i),
                               f'Самостоятельное событие {i}. ' + 'Поддержанные источником существенные детали. ' * 5,
                               SECTIONS[4], 70, 70) for i in range(14)]
        body, selected = render_digest(stories, NOW.date())
        self.assertGreaterEqual(telegram_length(body), 2500)
        self.assertLessEqual(telegram_length(body), 3900)
        self.assertEqual(body.count('• '), len(selected))

    def test_factual_prefix_keeps_story_not_invented_detail(self):
        source = 'Кларенс Седорф стал главным тренером Коринтианс до конца 2027 года.'
        valid = 'Кларенс Седорф стал главным тренером Коринтианс до конца 2027 года.'
        self.assertEqual(validated_prefix(source, valid + ' Подробности остаются неизвестными.'), valid)
        self.assertEqual(validated_prefix(source, 'Седорф подписал контракт до 2030 года. Подробности неизвестны.'), '')

    def test_prefix_cannot_drop_transfer_release_condition(self):
        source = 'Барселона планирует подписать Дэвиса. Бавария готова его отпустить, если поступит выгодное предложение.'
        self.assertEqual(validated_prefix(source, 'Барселона планирует подписать Дэвиса. Бавария готова отпустить футболиста.'), '')

    def test_english_source_not_converted_to_russian_name_heuristic(self):
        source = 'Lionel Messi with a brace for Inter Miami tonight to reach 934 career goals.'
        self.assertEqual(digest_factual_issues(source, 'Лионель Месси оформил дубль за Интер Майами и достиг отметки в 934 гола за карьеру.'), [])
        self.assertIn('SOURCE_NAME_COMPLETED', digest_factual_issues(
            'Real Madrid renewed Arda and Tchouaméni contracts.', 'Реал Мадрид продлил контракт с Арда Гюлером.'))
        self.assertNotIn('SOURCE_NAME_COMPLETED', digest_factual_issues(
            'Real Madrid renewed Arda Guler contract.', 'Реал Мадрид продлил контракт с Арда Гюлером.'))

    def test_medical_same_club_different_players_kept(self):
        a = DigestStory(item('First injury.'), 'Доминик Собослаи пропускает матч из-за травмы задней поверхности бедра.', SECTIONS[3], 70, 70)
        b = DigestStory(item('Second injury.', 2), 'Исак пропускает матч из-за травмы задней поверхности бедра.', SECTIONS[3], 70, 70)
        c = DigestStory(item('Updated injury.', 3), 'Собослаи не играет из-за травмы задней поверхности бедра.', SECTIONS[3], 70, 70)
        self.assertFalse(same_health_story(a, b))
        self.assertTrue(same_health_story(a, c))

    def test_known_quoted_bookmaker_tail_removed_not_primary_news(self):
        text = ('Real Madrid work on new contracts and midfield options for 2027. '
                'After Arda’s new deal, talks will follow with Bellingham — still early stages.'
                '\n@DAZNFutbol ⤵️\nDAZN Bet España (@DAZNBetES)\nBET NOW')
        source = IncomingItem('x_test', '1', text, 'https://x.com/test/status/1', NOW - timedelta(hours=1))
        accepted, rejected = filter_items([source], *day_window(NOW))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])
        self.assertNotIn('DAZN', accepted[0].text)
        self.assertIn('still early stages', accepted[0].text)

    def test_factual_stat_not_rejected_by_meme_framing(self):
        source = item('Игроки Манчестер Юнайтед, как только соперник получает красную карточку. '
                      'Манчестер Юнайтед не смог обыграть ни одного из трёх последних соперников, которые получали красную карточку.', hours=1)
        self.assertEqual(len(filter_items([source], *day_window(NOW))[0]), 1)

    def test_later_telegram_failure_preserves_fetched_posts(self):
        source = Source('a', 'telegram', 'a', 'a', 'https://t.me/a')
        page = ('<div class="tgme_widget_message" data-post="a/100">'
                '<div class="tgme_widget_message_text">Liverpool signed a new player.</div>'
                '<time datetime="2026-10-11T15:00:00Z"></time></div>')
        with patch('premwire.daily_collection.get_text', side_effect=[page, TimeoutError('private detail')]):
            found, warnings = telegram_day(source, *day_window(NOW), float('inf'))
        self.assertEqual(len(found), 1)
        self.assertIn('incomplete', warnings[0])
        self.assertNotIn('private detail', warnings[0])

    def test_x_api_midnight_and_morning_paginated(self):
        source = Source('a', 'x', 'a', 'a', 'https://x.com/a')
        def payload(id_, date, cursor=None):
            return {'data': [{'id': id_, 'text': 'Liverpool signed a player.', 'created_at': date, 'author_id': '7'}],
                    'includes': {'users': [{'id': '7', 'username': 'a'}]}, 'meta': {'next_token': cursor} if cursor else {}}
        pages = [payload('100', '2026-10-11T12:00:00Z', 'next'), payload('80', '2026-10-10T22:00:00Z')]
        with patch('premwire.daily_collection.get_json', side_effect=pages) as fetch:
            found, warnings = x_day((source,), 'test-placeholder', *day_window(NOW), float('inf'))
        self.assertEqual(len(found), 2)
        self.assertEqual(warnings, [])
        params = parse_qs(urlsplit(fetch.call_args_list[0].args[0]).query)
        self.assertEqual(params['start_time'], ['2026-10-10T21:00:00Z'])
        self.assertNotIn('since_id', params)
        self.assertEqual(parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)['next_token'], ['next'])

    def test_x_later_failure_keeps_partial_data_and_warns(self):
        source = Source('a', 'x', 'a', 'a', 'https://x.com/a')
        payload = {'data': [{'id': '100', 'text': 'Liverpool signed a player.', 'created_at': '2026-10-11T12:00:00Z', 'author_id': '7'}],
                   'includes': {'users': [{'id': '7', 'username': 'a'}]}, 'meta': {'next_token': 'next'}}
        with patch('premwire.daily_collection.get_json', side_effect=[payload, TimeoutError('private detail')]):
            found, warnings = x_day((source,), 'test-placeholder', *day_window(NOW), float('inf'))
        self.assertEqual(len(found), 1)
        self.assertIn('incomplete', warnings[0])
        self.assertNotIn('private detail', warnings[0])

    def test_coverage_manifest_separately_records_sources(self):
        source = Source('a', 'telegram', 'a', 'a', 'https://t.me/a')
        incoming = IncomingItem('a', '1', 'Liverpool signed a player.', 'https://t.me/a/1', NOW - timedelta(hours=1))
        coverage = []
        with patch('premwire.daily_collection.telegram_day', return_value=([incoming], [])):
            collect_day((source,), {}, *day_window(NOW), coverage=coverage)
        self.assertEqual(coverage[0]['source_id'], 'a')
        self.assertEqual(coverage[0]['count'], 1)
        self.assertEqual(coverage[0]['status'], 'MIDNIGHT_BOUNDARY_VERIFIED')

    def test_enabled_manual_route_is_explicit_not_silently_covered(self):
        source = Source('manual', 'manual_x', 'manual', 'manual', '')
        coverage = []
        found, warnings = collect_day((source,), {}, *day_window(NOW), coverage=coverage)
        self.assertEqual(found, [])
        self.assertEqual(coverage[0]['status'], 'NOT_POLLED')
        self.assertIn('manual_x not polled', warnings[0])


if __name__ == "__main__":
    unittest.main()
