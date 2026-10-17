"""Synthetic regression cases, no network or operator data."""
from dataclasses import replace
from datetime import datetime,date,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from premwire.daily_editorial_gates import freshness_gate,statistics_gate,editorial_rank,canonical,utc
from premwire.daily_fact_digest import ImmutableBundle,VerifiedFact,StatisticalObservation
from premwire.daily_fact_e2e import prepare_immutable_edition
from premwire.daily_match_data import MatchData,DataUnavailable,normalize_fixture,bind_fixture,STATUS
from premwire.daily_strategy import DailyState
from premwire.daily_text_digest import MOSCOW
from premwire.models import IncomingItem
from tests.test_daily_edition_gate import compiled

END=datetime(2026,10,17,20,56,tzinfo=MOSCOW)
def source(text='Проверенный факт.',hours=1,n='1'):
    return IncomingItem('test',n,text,'https://example.test/'+n,END-timedelta(hours=hours))
def bundle(kind='PLAYER_STATS',hours=1,n='1',period='CURRENT_LEAGUE_SEASON',value='37'):
    i=source(hours=hours,n=n)
    obs=StatisticalObservation('GOALS',value,'GOALS','Алекс Пример',period,i.canonical_url)
    f=VerifiedFact('stats',('Алекс Пример',value),(i.canonical_url,),i.text,(f'Алекс Пример — {value} голов.',)*2,(obs,))
    return ImmutableBundle(n,kind,('Алекс Пример',),(f,),(i.canonical_url,),80,('stats',),'📊 Статистика',source_published_at=utc(i.published_at)),i

class FreshnessTests(unittest.TestCase):
    def test_unknown_event_instant_not_midnight_or_source_clock(self):
        b,i=bundle()
        result,checks=freshness_gate([b],[i],END)
        b=result[0]
        self.assertEqual(b.event_occurred_at,'');self.assertEqual(b.event_updated_at,'')
        self.assertEqual(b.freshness_category,'FRESH_TODAY');self.assertEqual(b.freshness_confidence,'SOURCE_REPORT_ONLY')
        self.assertTrue(b.source_published_at.endswith('+00:00'))
    def test_moscow_midnight_not_utc_calendar(self):
        b,i=bundle();i=replace(i,published_at=datetime(2026,10,16,21,1,tzinfo=timezone.utc))
        self.assertEqual(freshness_gate([b],[i],END)[0][0].freshness_category,'FRESH_TODAY')
    def test_source_after_cutoff_excluded(self):
        b,i=bundle();i=replace(i,published_at=END+timedelta(seconds=1))
        self.assertFalse(freshness_gate([b],[i],END)[0])
    def test_recent_official_date_label_not_event_date_invention(self):
        i=source('ОФИЦИАЛЬНО: Алекс Пример — новый главный тренер «Восток».',hours=25)
        b=compiled(i.text);b=replace(b,source_refs=(i.canonical_url,),verified_facts=tuple(replace(f,source_refs=(i.canonical_url,),evidence=i.text) for f in b.verified_facts))
        out,_=freshness_gate([b],[i],END)
        self.assertIn('16 октября',out[0].render());self.assertEqual(out[0].event_date,'')
    def test_event_specific_age_policies(self):
        b,i=bundle(kind='TRANSFER_CONTACT',hours=25)
        self.assertFalse(freshness_gate([b],[i],END)[0])
        b=replace(b,event_type='TRANSFER_OFFICIAL')
        self.assertTrue(freshness_gate([b],[i],END)[0])
    def test_published_not_collected_is_dedupe_proof(self):
        b,i=bundle();kept,_=freshness_gate([b],[i],END)
        self.assertTrue(kept)
        self.assertFalse(freshness_gate([b],[i],END,kept)[0])
    def test_status_advance_keeps_canonical_link(self):
        old=compiled('«Восток» хочет подписать Алекса Примера. 📰 Example News')
        new=compiled('ОФИЦИАЛЬНО: Алекса Примера перешёл в «Восток» из «Запад».')
        self.assertIsNotNone(old);self.assertIsNotNone(new)
        i=source(n='new');new=replace(new,source_refs=(i.canonical_url,),verified_facts=tuple(replace(f,source_refs=(i.canonical_url,),evidence=i.text) for f in new.verified_facts))
        old=replace(old,canonical_event_id=canonical(old))
        kept,_=freshness_gate([new],[i],END,[old])
        self.assertEqual(kept[0].freshness_category,'UPDATED_STORY');self.assertEqual(kept[0].canonical_event_id,old.canonical_event_id)
    def test_first_seen_preserved_in_existing_payload_no_schema_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=DailyState(Path(tmp)/'copy.sqlite3');i=source()
            state.accumulate([i]);first=state.items()[0]
            state.accumulate([replace(i,text='Уточнённый исходный текст.')]);second=state.items()[0]
            self.assertEqual(first.first_seen_at,second.first_seen_at)
            self.assertGreaterEqual(second.last_seen_at,first.last_seen_at);state.db.close()
    def test_final_result_not_reposted_for_extra_context(self):
        old=compiled('Arsenal beat Chelsea 3-0.')
        i=source();new=replace(old,event_id='new',source_refs=(i.canonical_url,),verified_facts=tuple(replace(f,source_refs=(i.canonical_url,),evidence=i.text) for f in old.verified_facts))
        context=VerifiedFact('scorers',('Алекс Пример',),(i.canonical_url,),i.text,('Алекс Пример забил гол.',)*2)
        new=replace(new,verified_facts=new.verified_facts+(context,))
        self.assertFalse(freshness_gate([new],[i],END,[old])[0])
    def test_undated_distinct_later_fixture_not_lifetime_pair_suppression(self):
        old=replace(compiled('Arsenal beat Chelsea 3-0.'),source_published_at=utc(END-timedelta(days=30)))
        new=compiled('Arsenal beat Chelsea 4-0.')
        i=source();new=replace(new,source_refs=(i.canonical_url,),verified_facts=tuple(replace(f,source_refs=(i.canonical_url,),evidence=i.text) for f in new.verified_facts))
        kept,_=freshness_gate([new],[i],END,[old]);self.assertTrue(kept)
        self.assertEqual(kept[0].event_date,'');self.assertNotEqual(kept[0].canonical_event_id,canonical(old))
    def test_new_confirmed_contract_term_is_material_without_false_signing(self):
        old=compiled('Фабрицио Романо: «Восток» и Алекс Пример достигли договорённости о продлении контракта до лета 2028 года.')
        new=compiled('Фабрицио Романо: «Восток» и Алекс Пример достигли договорённости о продлении контракта до лета 2029 года.')
        i=source();new=replace(new,source_refs=(i.canonical_url,),verified_facts=tuple(replace(f,source_refs=(i.canonical_url,),evidence=i.text) for f in new.verified_facts))
        kept,_=freshness_gate([new],[i],END,[old]);self.assertEqual(kept[0].freshness_category,'UPDATED_STORY')
        self.assertNotIn('подписал',kept[0].render())

class StatisticsTests(unittest.TestCase):
    def test_yesterday_dynamic_total_rejected_no_arithmetic(self):
        b,_=bundle(hours=25);out,checks=statistics_gate([b],END)
        self.assertFalse(out);self.assertEqual(checks[0]['status'],'STALE');self.assertEqual(b.verified_facts[0].values[-1],'37')
    def test_today_fidelity_is_not_independent_confirmation(self):
        b,_=bundle();self.assertEqual(statistics_gate([b],END)[1][0]['status'],'SOURCE_VERIFIED')
    def test_closed_historical_period_not_live_total(self):
        b,_=bundle(hours=25,period='FIRST_6_PREMIER_LEAGUE_ROUNDS');self.assertTrue(statistics_gate([b],END)[0])
    def test_same_time_conflict_rejects_both(self):
        a,_=bundle(n='a');b,_=bundle(n='b',value='38')
        out,checks=statistics_gate([a,b],END);self.assertFalse(out);self.assertEqual({c['status'] for c in checks},{'CONFLICTING'})
    def test_newer_comparable_total_wins_without_guess(self):
        a,_=bundle(n='a',hours=2);b,_=bundle(n='b',value='38')
        self.assertEqual(statistics_gate([a,b],END)[0],[b])
    def test_different_periods_not_conflicting(self):
        a,_=bundle(n='a',period='FIRST_6_PREMIER_LEAGUE_ROUNDS');b,_=bundle(n='b',value='38')
        self.assertEqual(len(statistics_gate([a,b],END)[0]),2)
    def test_historical_window_conflict_is_not_excused_as_non_live(self):
        a,_=bundle(n='a',period='FIRST_6_PREMIER_LEAGUE_ROUNDS')
        b,_=bundle(n='b',value='38',period='FIRST_6_PREMIER_LEAGUE_ROUNDS')
        self.assertFalse(statistics_gate([a,b],END)[0])
    def test_external_confirmation_requires_independence_and_cutoff(self):
        from premwire.daily_editorial_gates import stat_scope
        b,_=bundle();s=b.verified_facts[0].statistics[0]
        e=dict(scope=stat_scope(s),value='37',independent=True,observed_at=utc(END-timedelta(minutes=2)),available_at=utc(END-timedelta(minutes=1)))
        self.assertEqual(statistics_gate([b],END,[e])[1][0]['status'],'EXTERNALLY_CONFIRMED')
        e['available_at']=utc(END+timedelta(seconds=1))
        self.assertEqual(statistics_gate([b],END,[e])[1][0]['status'],'SOURCE_VERIFIED')
    def test_later_relevant_fixture_invalidates_total(self):
        b,_=bundle(period='CLUB_CHAMPIONS_LEAGUE_CAREER')
        s=b.verified_facts[0].statistics[0];b=replace(b,verified_facts=(replace(b.verified_facts[0],statistics=(replace(s,subject='Восток / Алекс Пример'),)),))
        match=compiled('«Восток» разгромил «Запад» на своём поле 4:1.')
        competition=VerifiedFact('competition',('CHAMPIONS_LEAGUE',),match.source_refs,'Источник.',('Лига чемпионов.',)*2)
        match=replace(match,source_published_at=utc(END-timedelta(minutes=1)),verified_facts=match.verified_facts+(competition,))
        self.assertNotIn(b,statistics_gate([b,match],END)[0])
    def test_stale_optional_context_does_not_destroy_match_essentials(self):
        from tests.test_daily_fact_digest import match_bundle
        b=replace(match_bundle(),source_published_at=utc(END-timedelta(days=1)))
        kept,checks=statistics_gate([b],END)
        self.assertEqual(kept[0].essential_facts,b.essential_facts)
        self.assertNotIn('table',[f.key for f in kept[0].verified_facts])
    def test_new_context_cannot_refresh_the_clock_of_an_old_stat(self):
        b,i=bundle(hours=25);b=replace(b,source_published_at=utc(END-timedelta(minutes=1)))
        self.assertEqual(statistics_gate([b],END,inputs=[i])[1][0]['status'],'STALE')

class RankingTests(unittest.TestCase):
    def test_today_over_unprinted_and_transparent_score(self):
        b,_=bundle(kind='TRANSFER_OFFICIAL');today=replace(b,event_id='today',freshness_category='FRESH_TODAY')
        old=replace(b,event_id='old',freshness_category='DATE_UNKNOWN')
        ranked,checks=editorial_rank([old,today]);self.assertEqual(ranked[0],today)
        self.assertEqual(checks[0]['score'],sum(checks[0]['components'].values()))
    def test_diversity_is_soft_not_category_veto(self):
        b,_=bundle(kind='TRANSFER_INTEREST');items=[replace(b,event_id=str(i),freshness_category='FRESH_TODAY') for i in range(4)]
        _,checks=editorial_rank(items);self.assertLess(checks[-1]['components']['topic_diversity'],0)
    def test_deterministic_repeat(self):
        b,_=bundle();self.assertEqual(editorial_rank([b]),editorial_rank([b]))

class SourceShapesTests(unittest.TestCase):
    def test_instrumental_contract_agreement_not_signed_or_role_invented(self):
        b=compiled('Фабрицио Романо: «Восток» достиг договорённости с Иваном Примером о продлении контракта до лета 2029 года.')
        self.assertEqual(b.event_type,'CONTRACT_AGREEMENT');self.assertIn('с Иваном Примером',b.render())
        self.assertNotIn('подписал',b.render());self.assertNotIn('тренер',b.render())
    def test_loss_recap_score_orientation_and_bound_scorer(self):
        b=compiled('39-летний Иван Пример тем временем забил свой первый гол за «Восток». «Восток» так и не удалось удержать преимущество – команда уступила «Западу» со счётом 1:2.')
        self.assertEqual(next(f.values[-1] for f in b.verified_facts if f.key=='result'),'2:1')
        self.assertIn('Иван Пример',b.render());self.assertIn('1:2',b.render());self.assertNotIn('Лиг',b.render())
    def test_asking_valuation_not_sale_or_destination(self):
        b=compiled('«ВОСТОК» ХОЧЕТ €90 МЛН ЗА ИВАНА ПРИМЕРА! Клуб намерено сохранить игрока. 📰 Example News')
        self.assertEqual(b.event_type,'TRANSFER_VALUATION');self.assertIn('90 млн евро',b.render())
        self.assertNotIn('перешёл',b.render());self.assertNotIn('продал',b.render())

class FixtureTests(unittest.TestCase):
    def raw(self,status='FT'):
        return dict(fixture=dict(id=1,date='2026-10-16T18:00:00+03:00',status={'short':status}),
                    teams={'home':{'id':1,'name':'Арсенал'},'away':{'id':2,'name':'Челси'}},goals={'home':3,'away':0},league={'id':2,'name':'UEFA Champions League'})
    def test_all_required_states(self):
        self.assertEqual({STATUS[v] for v in ('NS','1H','HT','FT','PST','CANC')},{'SCHEDULED','LIVE','HALF_TIME','FULL_TIME','POSTPONED','CANCELLED'})
    def test_future_snapshot_never_historical_evidence(self):
        with self.assertRaises(DataUnavailable):normalize_fixture(self.raw(),utc(END+timedelta(seconds=1)),END,historical=True)
    def test_kickoff_not_finish_or_event_publication_clock(self):
        f=normalize_fixture(self.raw(),utc(END-timedelta(minutes=1)),END)
        self.assertEqual(f['event_date'],'2026-10-16');self.assertNotIn('finished_at',f)
    def test_live_not_final(self):
        f=normalize_fixture(self.raw('HT'),utc(END-timedelta(minutes=1)),END)
        self.assertEqual(f['status'],'HALF_TIME')
    def test_exact_binding_conflict_and_no_scorer_invention(self):
        b=compiled('Arsenal beat Chelsea 3-0.')
        comp=VerifiedFact('competition',('CHAMPIONS_LEAGUE',),b.source_refs,'SOURCE',('Лига чемпионов.',)*2)
        b=replace(b,event_date='2026-10-16',verified_facts=b.verified_facts+(comp,))
        f=normalize_fixture(self.raw(),utc(END-timedelta(minutes=1)),END)
        bound,reason=bind_fixture(b,[f]);self.assertEqual(reason,'EXTERNALLY_CONFIRMED');self.assertNotIn('scorers',bound)
        f['score']=(4,0);self.assertEqual(bind_fixture(b,[f])[1],'MATCH_SOURCE_API_CONFLICT')
    def test_cache_budget_and_secret_not_serialized(self):
        import io
        class Response(io.BytesIO):
            def geturl(self):return 'https://v3.football.api-sports.io/fixtures/events?fixture=1'
        opener=Mock(side_effect=lambda *a,**k:Response(b'{"response":[],"errors":[]}'))
        with tempfile.TemporaryDirectory() as tmp:
            api=MatchData('SYNTHETIC_SECRET',tmp,opener=opener,max_calls=1)
            a=api.events(1);self.assertEqual(api.events(1),a);self.assertEqual(opener.call_count,1)
            self.assertNotIn('SYNTHETIC_SECRET',''.join(p.read_text() for p in Path(tmp).glob('*.json')))
            with self.assertRaises(DataUnavailable):api.events(2)
    def test_non_final_api_cannot_confirm_final_source(self):
        from premwire.daily_match_data import match_context_gate
        b=compiled('Arsenal beat Chelsea 3-0.')
        comp=VerifiedFact('competition',('CHAMPIONS_LEAGUE',),b.source_refs,'SOURCE',('Лига чемпионов.',)*2)
        b=replace(b,event_date='2026-10-16',verified_facts=b.verified_facts+(comp,))
        f=normalize_fixture(self.raw('HT'),utc(END-timedelta(minutes=1)),END)
        self.assertFalse(match_context_gate([b],[],END,[f])[0])

if __name__=='__main__':unittest.main()
