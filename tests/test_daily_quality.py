import unittest
from datetime import date
from tests.test_daily_text_digest import item, NOW
from premwire.daily_quality import source_issues, relation_issues, fixture_pairs
from premwire.daily_text_digest import filter_items, collapse, DigestStory, SECTIONS, render_digest, digest_factual_issues

class DailyQualityTests(unittest.TestCase):
    def test_cs2_even_with_football_brand_rejected(self):
        self.assertTrue(source_issues('Арсенал: конкурс Counter-Strike 2, 50 призов, подписаться на FORCE BUY'))

    def test_live_real_cs2_source_rejected(self):
        import json
        from pathlib import Path
        values = json.loads((Path(__file__).resolve().parent / 'fixtures/daily_quality_oct11.json').read_text(encoding='utf-8'))
        bad = values['cs2_source']
        accepted, rejected = filter_items([item(bad)], NOW.replace(hour=0), NOW.replace(hour=21))
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]['reason'], 'FOOTBALL_RELEVANCE_PROMOTION')

    def test_no_sides_rejected(self):
        self.assertTrue(source_issues('48th minute: take the lead\n55th minute: equalise\nThe match has come to life in the second half'))

    def test_one_side_live_rejected(self):
        self.assertTrue(source_issues('Crystal Palace score twice late in the first half to lead at the break'))

    def test_club_alias_table_not_a_required_registry(self):
        self.assertEqual(source_issues('New Borough lead 10-man River Town at Meadow Park'), [])

    def test_match_variants_merge(self):
        sources = [item('Арсенал победил Лидс.', 1), item('Лидс уступил Арсеналу.', 2)]
        self.assertEqual(len(collapse(sources)[0]), 1)

    def test_independent_injury_not_match(self):
        self.assertFalse(fixture_pairs('Перед матчем Арсенал — Лидс у игрока выявлена травма.'))

    def test_lead_is_not_victory(self):
        self.assertIn('FACTUAL_MATCH_STATUS_UPGRADED', relation_issues('Man City lead 10-man Liverpool at Anfield', 'Манчестер Сити выиграл у Ливерпуля.'))

    def test_home_away_reversed_rejected(self):
        self.assertIn('ENTITY_RELATIONS_HOME_AWAY', relation_issues('Man City lead 10-man Liverpool at Anfield', 'Манчестер Сити увеличил преимущество хозяев.'))

    def test_winner_reversed(self):
        self.assertIn('ENTITY_RELATIONS_WINNER_REVERSED', relation_issues('Арсенал победил Лидс.', 'Лидс победил Арсенал.'))

    def test_english_source_winner_reversed(self):
        self.assertIn('ENTITY_RELATIONS_WINNER_REVERSED', relation_issues('Liverpool beat Chelsea 2-0.', 'Челси победил Ливерпуль.'))

    def test_red_card_side_reversed(self):
        self.assertIn('ENTITY_RELATIONS_RED_CARD_SIDE', relation_issues('Man City lead 10-man Liverpool at Anfield', 'Манчестер Сити играл вдесятером.'))

    def test_destination_first_route_reversed(self):
        self.assertIn('ENTITY_RELATIONS_TRANSFER_ROUTE', relation_issues('Гордон перешёл в Ливерпуль из Ньюкасла.', 'Гордон перешёл в Ньюкасл из Ливерпуля.'))

    def test_safe_result_clause_can_be_recovered(self):
        from premwire.daily_text_digest import validated_prefix
        self.assertEqual(validated_prefix('Ман Сити не заметил Ливерпуль! Голы на счету Холанда.',
                                         'Манчестер Сити обыграл Ливерпуль, забив голы на счету Холанда.'),
                         'Манчестер Сити обыграл Ливерпуль.')

    def test_transfer_status_upgrade_rejected(self):
        self.assertTrue(digest_factual_issues('Ливерпуль интересуется трансфером Энтони Гордона из Ньюкасла.', 'Ливерпуль подписал Энтони Гордона из Ньюкасла.'))

    def test_wrong_name_rejected(self):
        self.assertIn('LANGUAGE_QUALITY', relation_issues('Tchouaméni has signed.', 'Тюрамини подписал контракт.'))

    def test_double_source_rejected(self):
        self.assertIn('LANGUAGE_DUPLICATE_CREDIT', relation_issues('Новость', 'Источник SPORT сообщил. Источник: SPORT.'))

    def test_final_render_blocks_bad_cached_story(self):
        s = item('Counter-Strike 2: конкурс с призами')
        story = DigestStory(s, 'Конкурс Counter-Strike 2.', SECTIONS[1], 90, 90)
        self.assertEqual(render_digest([story], date(2026, 10, 11)), ('', []))

    def test_quality_news_allowed(self):
        self.assertEqual(source_issues('Арсенал победил Лидс со счётом 2:0 в АПЛ.'), [])

    def test_transfer_direction_reversed(self):
        self.assertTrue(digest_factual_issues('Энтони Гордон перешёл из Ньюкасла в Ливерпуль.', 'Энтони Гордон перешёл из Ливерпуля в Ньюкасл.'))

    def test_unknown_team_streak_rejected(self):
        self.assertTrue(source_issues('make it six wins from their opening six games!'))

    def test_unconfirmed_score_filler_rejected(self):
        self.assertIn('EMPTY_EDITORIAL_CONTEXT', digest_factual_issues('Ливерпуль сообщил о травме игрока.', 'Ливерпуль сообщил о травме игрока. Точный счет встречи неизвестен.'))

    def test_abbreviated_city_and_full_name_same_fixture(self):
        a = item('Ман Сити не заметил Ливерпуль! Голы на счету Холанда и Доку.', 1)
        b = item('Man City lead 10-man Liverpool at Anfield', 2, hours=1)
        self.assertEqual(len(collapse([a, b])[0]), 1)

    def test_disjoint_roundup_is_split(self):
        a = item('«Арсенал» вырвал победу у «Лидса», а «МЮ» умудрился не дожать оставшийся вдесятером «Тоттенхэм». Пересматриваем две главные развязки субботы.', 1)
        accepted, rejected = filter_items([a], NOW.replace(hour=0), NOW.replace(hour=21))
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(collapse(accepted)[0]), 2)

    def test_different_fixture_dates_preserved(self):
        a = item('Арсенал победил Лидс 11.10.', 1)
        b = item('Арсенал победил Лидс 10.10.', 2)
        self.assertEqual(len(collapse([a, b])[0]), 2)

if __name__ == '__main__':
    unittest.main()
