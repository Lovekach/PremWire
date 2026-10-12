from contextlib import nullcontext
from dataclasses import asdict,replace
from datetime import date,datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from premwire.daily_fact_digest import compile_bundle,render,audit_digest,STATS_SECTION,merge_health_context,attach_match_context
from premwire.daily_fact_e2e import ShadowState,collect_shadow,edit_once,prepare_payload,run_shadow,select_for_telegram,quality_issues
from premwire.models import Source,IncomingItem
from premwire.config import load_config
from tests.test_daily_fact_digest import candidate,match_bundle


class GeneralPatternTests(unittest.TestCase):
    def bundle(self,text):return compile_bundle(candidate([text]),date(2026,10,11))

    def test_interest_reported_price_not_agreement(self):
        b=self.bundle('Northbridge are interested in Harbor FC defender Pablo Example, with the local club reportedly open to offers of £45.5m. (News Desk)')
        self.assertEqual(b.event_type,'TRANSFER_INTEREST')
        self.assertIn('45,5 млн фунтов',b.render())
        self.assertIn('По сообщению источника',b.render())
        self.assertNotIn('перешёл',b.render())
        self.assertIn('News Desk',b.render())
        self.assertEqual(b.verified_facts[0].values[:4],('Pablo Example','Harbor FC','Northbridge','INTEREST'))

    def test_attribution_split_by_source_clauses(self):
        b=self.bundle('Northbridge have joined the race to sign Harbor FC midfielder Pablo Example.\n(News Desk)')
        self.assertEqual(b.event_type,'TRANSFER_INTEREST')

    def test_release_clause_not_transfer_price(self):
        b=self.bundle('Harbor FC have increased the release clause of winger Pablo Example to €85m amid interest from Northbridge. (News Desk)')
        self.assertEqual(b.event_type,'RELEASE_CLAUSE')
        self.assertIn('отступные',b.render());self.assertNotIn('купил',b.render())
        self.assertEqual(b.verified_facts[0].values[2:5],('€','85','million'))

    def test_talks_no_offer_belief_not_confirmed_preference(self):
        b=self.bundle("Northbridge have held fresh talks with Harbor FC's 17-year-old attacker Pablo Example, but they have not made him an offer as they believe he is favouring a move to Eastern FC or Westside. (News Desk)")
        self.assertEqual(b.event_type,'TRANSFER_TALKS')
        self.assertIn('Предложения игроку пока не было',b.render())
        self.assertIn('В клубе считают',b.render());self.assertNotIn('согласовал',b.render())
        self.assertEqual(b.verified_facts[0].values[3],'17')

    def test_generic_contract_plan_preserves_uncertainty(self):
        b=self.bundle('Harbor FC work on new contracts but also on midfield options for 2028, more likely summer than January.\nAfter Pablo’s new deal and Richard done, talks will follow with Alex — still early stages.')
        self.assertEqual(b.event_type,'CONTRACT_PLANS')
        self.assertIn('Более вероятный',b.render());self.assertIn('ранней стадии',b.render())
        self.assertIn('2028',b.render());self.assertNotIn('официально',b.render())
        self.assertIn('над новыми контрактами. Усиление полузащиты',b.render())

    def test_league_stat_generic_player(self):
        b=self.bundle('Pablo Example reaches 7 Premier League goals in 8 games.')
        self.assertEqual(b.event_type,'PLAYER_STATS');self.assertEqual(b.section,STATS_SECTION)
        self.assertEqual(b.verified_facts[0].values,('Pablo Example','7','8','PREMIER_LEAGUE'))

    def test_live_combined_post_only_supported_season_stat(self):
        b=self.bundle('Northbridge make it three against Harbor FC!\nPablo Example with 7 goals in 8 PL games this season, 6 G/A for Another Player.')
        self.assertEqual(b.event_type,'PLAYER_STATS')
        self.assertNotIn('3:0',b.render());self.assertNotIn('Another Player',b.render())

    def test_health_quote_no_diagnosis_or_coach_role(self):
        b=self.bundle('Richard Example: “Pablo Example has a tight hamstring, this is why he’s not available today”.')
        self.assertEqual(b.event_type,'INJURY');self.assertIn('По словам',b.render())
        self.assertNotIn('тренер',b.render());self.assertNotIn('разрыв',b.render())

    def test_incomplete_money_or_transfer_unsupported(self):
        for text in ('Harbor FC have increased the release clause of winger Pablo Example.',
                     'Northbridge have agreed a deal for somebody.'):
            self.assertIsNone(self.bundle(text))

    def test_promo_quoted_footer_not_rendered(self):
        b=self.bundle('Harbor FC work on new contracts but also on midfield options for 2028, more likely summer than January.\nAfter Pablo’s new deal and Richard done, talks will follow with Alex — still early stages.\n@BetExample\nSubscribe to our offer!')
        self.assertNotIn('BetExample',b.render());self.assertNotIn('Subscribe',b.render())

    def test_render_does_not_mutate_fact_payload(self):
        b=self.bundle('Pablo Example reaches 7 Premier League goals in 8 games.')
        frozen=asdict(b);render([b],date(2026,10,11));self.assertEqual(frozen,asdict(b))

    def test_final_promotion_and_wrong_category_are_rejected(self):
        b=self.bundle('Pablo Example reaches 7 Premier League goals in 8 games.')
        self.assertIn('PROMOTION',quality_issues(b.render()+' Подключайтесь к трансляции!',[b]))
        self.assertIn('PLAYER_STATS_CATEGORY',quality_issues(b.render(),[replace(b,section='💬 Клубы и тренеры')]))

    def test_selection_reserves_longer_variants(self):
        b=match_bundle();selected=select_for_telegram([b,b],date(2026,10,11))
        self.assertEqual(len(selected),1)

    def test_original_source_not_translated_cleaner_role_is_parsed(self):
        original=candidate(['Harbor FC have increased the release clause of winger Pablo Example to €85m amid interest from Northbridge. (News Desk)']).source
        cleaned=candidate(['Harbor FC have increased the release clause of вингер Pablo Example to €85m amid interest from Northbridge. (News Desk)'])
        b=compile_bundle(cleaned,date(2026,10,11),{original.canonical_url:original})
        self.assertEqual(b.event_type,'RELEASE_CLAUSE')
        self.assertEqual(b.verified_facts[0].evidence,original.text)

    def test_related_mu_style_no_fact_payload_mutation(self):
        fixture=replace(self.bundle('«МЮ» не смогли обыграть «Тоттенхэм».'),participants=('Манчестер Юнайтед','Тоттенхэм'))
        stats=self.bundle('«Манчестер Юнайтед» набрал всего 6 очков в первых 6 турах чемпионата.\nЭто худший старт клуба в лиге за последние 40 лет.')
        merged=attach_match_context([fixture,stats],date(2026,10,11))[0]
        frozen=asdict(merged);text=merged.render()
        self.assertEqual(text.count('Манчестер Юнайтед'),1)
        self.assertIn('В первых 6 турах набрано 6 очков',text)
        self.assertEqual(frozen,asdict(merged))

    def test_unique_same_surname_health_context_only(self):
        base=self.bundle('Доминик Собослаи не попал в заявку «Ливерпуля» на матч из-за травмы задней поверхности бедра')
        quote=self.bundle('Andoni Example: “Szoboszlai has a tight hamstring, this is why he’s not available today”.')
        merged=merge_health_context([base,quote],date(2026,10,11))
        self.assertEqual(len(merged),1)
        self.assertEqual({f.key for f in merged[0].verified_facts},{'injury','availability'})
        self.assertIn('сегодня',merged[0].render())
        other=replace(base,event_id='other',participants=('Другой Собослаи',))
        self.assertEqual(len(merge_health_context([base,other,quote],date(2026,10,11))),3)

    def test_main_match_preserves_all_values_with_natural_streak(self):
        b=match_bundle();text=b.render()
        self.assertIn('Это 6-я победа в 6 матчах',text)
        for value in ('3:0','Холанд','Доку','Черки','62','Гравенберх'):self.assertIn(value,text)


class ShadowSafetyTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,11,tzinfo=timezone.utc);self.end=self.start+timedelta(days=1)
        self.source=Source('test','telegram','Test','test','https://t.me/test')
        self.config=SimpleNamespace(sources=(self.source,))
        self.item=IncomingItem('test','1','Original source','https://t.me/test/1',self.start+timedelta(hours=1))

    def test_state_never_uses_production_filename(self):
        with self.assertRaises(ValueError):ShadowState(Path('daily-text-digest.sqlite3'))

    def test_unavailable_source_uses_isolated_same_day_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            state=ShadowState(Path(folder)/'immutable-shadow.sqlite3')
            try:
                state.store([self.item,replace(self.item,external_id='old',published_at=self.start-timedelta(days=1))],'seed')
                def failed(*a,**k):raise TimeoutError('secret must not be exposed')
                items,audit=collect_shadow(self.config,{},self.start,self.end,state,failed)
                self.assertEqual(items,[self.item]);self.assertEqual(audit['cache_fallback_count'],1)
                self.assertNotIn('secret',json.dumps(audit))
            finally:state.close()

    def test_verified_empty_source_does_not_resurrect_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            state=ShadowState(Path(folder)/'immutable-shadow.sqlite3')
            try:
                state.store([self.item],'seed')
                def empty(*a,coverage,**k):
                    coverage.append(dict(source_id='test',status='MIDNIGHT_BOUNDARY_VERIFIED'));return [],[]
                items,audit=collect_shadow(self.config,{},self.start,self.end,state,empty)
                self.assertEqual(items,[]);self.assertEqual(audit['cache_fallback_count'],0)
            finally:state.close()

    def test_fresh_source_update_wins_and_new_day_not_leaked(self):
        with tempfile.TemporaryDirectory() as folder:
            state=ShadowState(Path(folder)/'immutable-shadow.sqlite3')
            try:
                state.store([self.item],'seed');new=replace(self.item,text='Updated source')
                items,_=collect_shadow(self.config,{},self.start,self.end,state,
                                       lambda *a,**k:([new,replace(new,external_id='tomorrow',published_at=self.end)],[]))
                self.assertEqual(items,[new])
            finally:state.close()

    def test_ollama_unavailable_falls_back_without_call(self):
        def failed_session(editor):raise ConnectionError()
        choices,trace=edit_once([match_bundle()],None,999,failed_session)
        self.assertTrue(all(v==0 for v in choices.values()));self.assertEqual(trace[0]['calls'],0)

    def test_qwen_timeout_one_call_no_retry(self):
        editor=SimpleNamespace(_simple_chat=lambda *a:(_ for _ in ()).throw(TimeoutError()))
        choices,trace=edit_once([match_bundle()],editor,float('inf'),lambda editor:nullcontext())
        self.assertEqual(sum(t['calls'] for t in trace),1);self.assertTrue(all(v==0 for v in choices.values()))

    def test_session_cleanup_failure_counts_actual_call(self):
        b=match_bundle()
        class Session:
            def __enter__(self):return None
            def __exit__(self,*a):raise OSError('unload failed')
        editor=SimpleNamespace(_simple_chat=lambda *a:json.dumps([dict(event_id=b.event_id,variant=1)]))
        choices,trace=edit_once([b],editor,float('inf'),lambda editor:Session())
        self.assertEqual(trace[0]['calls'],1);self.assertEqual(choices[b.event_id],0)

    def test_payload_transport_html_and_utf16_limit(self):
        text='Факт <проверен> & подтверждён ⚽'
        payload=prepare_payload('@shadow',text)
        self.assertEqual(payload['parse_mode'],'HTML');self.assertIn('&lt;',payload['text'])
        with self.assertRaises(ValueError):prepare_payload('@shadow','⚽'*4097)
        self.assertIsNone(prepare_payload('@shadow',''))

    def test_factual_corruption_refuses_payload(self):
        config=load_config(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as folder:
            state=ShadowState(Path(folder)/'immutable-shadow.sqlite3')
            try:
                with patch('premwire.daily_fact_e2e.build_bundles',return_value=([match_bundle()],{})),\
                     patch('premwire.daily_fact_e2e.edit_once',return_value=({},[])),\
                     patch('premwire.daily_fact_e2e.audit_digest',return_value=dict(essential_facts_lost=1,distorted_events=0,provenance_issues=[])):
                    with self.assertRaises(ValueError):run_shadow(config,{},self.start,self.end,state,lambda *a,**k:([],[]))
                self.assertEqual(state.db.execute('SELECT COUNT(*) FROM editions').fetchone()[0],0)
            finally:state.close()


if __name__=='__main__':unittest.main()
