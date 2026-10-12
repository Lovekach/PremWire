import unittest
from datetime import datetime, timedelta, timezone
from premwire.models import IncomingItem
from premwire.config import FilterConfig
from premwire.daily_fact_shadow import prepare, normalize_handles, coverage_issues


class FactBundleTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026,10,11,tzinfo=timezone.utc)
        self.end = self.start + timedelta(hours=20)

    def item(self, text, n):
        return IncomingItem('x_premier_league',str(n),text,f'https://x.com/test/status/{n}',self.start+timedelta(hours=18,minutes=n))

    def prepare(self, texts):
        return prepare([self.item(t,n) for n,t in enumerate(texts)],self.start,self.end,FilterConfig(True,()))

    def test_handle_is_explicit_not_emoji(self):
        self.assertIn('Manchester City',normalize_handles('@ManCity make it six wins'))
        self.assertEqual(normalize_handles('🔵 six wins'),'🔵 six wins')

    def test_result_goals_and_provenance(self):
        bundles,_,_,_=self.prepare([
            'Manchester City beat Liverpool 3-0. Haaland and Doku scored.',
            '«Ман Сити» не заметил «Ливерпуль»! Голы на счету Холанда, Доку и Шерки. Гравенберх удалился на 62-й минуте.'
        ])
        self.assertEqual(len(bundles),1)
        self.assertEqual(len(bundles[0].sources),2)
        self.assertTrue(all(f.url for f in bundles[0].facts))
        self.assertIn('3-0',bundles[0].source.text)
        self.assertIn('62',bundles[0].source.text)
        self.assertEqual(coverage_issues(bundles[0],'Сити победил Ливерпуль.'),['CONFIRMED_RESULT_SCORE_DROPPED'])
        self.assertEqual(coverage_issues(bundles[0],'Манчестер Сити победил Ливерпуль 3:0.'),[])

    def test_live_score_not_final(self):
        bundles,_,_,_=self.prepare(['Man City lead Liverpool 2-0 at Anfield.'])
        self.assertFalse(any(f.kind=='result' for b in bundles for f in b.facts))

    def test_unnamed_streak_not_inferred(self):
        bundles,_,_,_=self.prepare(['🔵 make it six wins from their opening six games!'])
        self.assertEqual(bundles,[])

    def test_short_appointment(self):
        bundles,_,_,_=self.prepare(['Clarence Seedorf becomes new head coach at Corinthians until the end of 2027.'])
        self.assertEqual(len(bundles),1)

    def test_ads_never_rescued(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0. CS2 giveaway subscribe to win!'])
        self.assertEqual(bundles,[])

    def test_separate_fixtures(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0.', 'Arsenal beat Leeds 2-0.'])
        self.assertEqual(len(bundles),2)

    def test_day_boundary(self):
        item=self.item('Manchester City beat Liverpool 3-0.',0)
        item=IncomingItem(item.source_id,item.external_id,item.text,item.canonical_url,self.start-timedelta(seconds=1))
        self.assertEqual(prepare([item],self.start,self.end,FilterConfig(True,()))[0],[])

    def test_conflicting_final_scores_fail_closed(self):
        bundles,_,_,conflicts=self.prepare(['Manchester City beat Liverpool 3-0.', 'Manchester City beat Liverpool 2-0.'])
        self.assertEqual(bundles,[])
        self.assertEqual(conflicts[0]['reason'],'CONFLICTING_FINAL_SCORES')

    def test_aliased_streak_joins_only_unique_match(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0.', '@ManCity make it six wins from their opening six games!'])
        self.assertEqual(len(bundles),1)
        self.assertIn('six wins',bundles[0].source.text)

    def test_different_dates_not_merged(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0 on 10.10.', 'Manchester City beat Liverpool 2-0 on 11.10.'])
        self.assertEqual(len(bundles),2)

    def test_compact_prompt_keeps_exact_russian_names(self):
        bundles,_,_,_=self.prepare([
            'Manchester City make it six wins out of six after a 3-0 victory over ten-man Liverpool at Anfield.',
            '«Ман Сити» не заметил «Ливерпуль»! Голы на счету Холанда, Доку и Шерки. А Гравенберх подставил «Ливерпуль» и удалился на 62-й минуте.'
        ])
        prompt=bundles[0].prompt()
        self.assertIn('3:0',prompt)
        self.assertIn('Шерки',prompt)
        self.assertNotIn('подставил',prompt)
        self.assertIn('62',prompt)
        self.assertNotIn('подряд',prompt)

    def test_single_club_live_caption_is_context_only(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0.', 'Haaland and Doku quality strikes: @ManCity 2-0 up at Anfield!'])
        self.assertEqual(len(bundles),1)
        self.assertEqual(len(bundles[0].sources),2)
        self.assertNotIn('2-0',bundles[0].source.text)

    def test_single_club_caption_not_guessed_between_two_fixtures(self):
        bundles,_,_,_=self.prepare(['Manchester City beat Liverpool 3-0.', 'Manchester City beat Arsenal 2-0.', 'Haaland and Doku quality strikes: @ManCity 2-0 up at Anfield!'])
        self.assertTrue(all(len(b.sources)==1 for b in bundles))


if __name__=='__main__':
    unittest.main()
