"""Offline contract tests; all identities below synthetic, no secrets/network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone, date
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from premwire.football_data import FootballData, normalize, source_item, compile_sources, merge_results, collect
from premwire.daily_match_data import DataUnavailable, bind_fixture
from premwire.daily_fact_e2e import prepare_immutable_edition, publication_adapter
from premwire.daily_text_digest import MOSCOW, telegram_length
from premwire.config import FilterConfig

END=datetime(2026,10,18,20,56,tzinfo=MOSCOW)
OBS=(END-timedelta(minutes=5)).isoformat()


def raw(**extra):
    row=dict(id=1234,utcDate=(END-timedelta(hours=4)).isoformat(),status='FINISHED',lastUpdated=OBS,
             competition=dict(id=2021,code='PL',name='Premier League'),
             season=dict(startDate='2026-08-01',endDate='2027-06-01'),
             homeTeam=dict(id=11,name='Восток',shortName='Восток'),awayTeam=dict(id=12,name='Запад',shortName='Запад'),
             score=dict(winner='HOME_TEAM',duration='REGULAR',fullTime=dict(home=2,away=0)))
    row.update(extra);return row


class Response(io.BytesIO):
    def __init__(self,payload,url):super().__init__(json.dumps(payload).encode());self.url=url
    def geturl(self):return self.url


class ProviderTests(unittest.TestCase):
    def test_normalize(self):
        row=normalize(raw(),OBS,END)
        self.assertEqual(row['status'],'FULL_TIME');self.assertEqual(row['score'],(2,0))
        self.assertEqual(row['home_id'],11);self.assertEqual(row['source_timestamp'],datetime.fromisoformat(OBS).astimezone(timezone.utc).isoformat())
    def test_future_observation_rejected(self):
        with self.assertRaises(ValueError):normalize(raw(),END.isoformat(),END)
    def test_future_update_rejected(self):
        with self.assertRaises(ValueError):normalize(raw(lastUpdated=END.isoformat()),OBS,END)
    def test_unzoned_rejected(self):
        with self.assertRaises(ValueError):normalize(raw(), '2026-10-18T17:50:00',END)
    def test_kickoff_is_not_completion(self):
        r=raw(status='IN_PLAY');r['score']['fullTime']={'home':None,'away':None}
        item=source_item(r,OBS);b,dec=compile_sources([item],END)
        self.assertFalse(b);self.assertEqual(dec[0]['decision'],'REJECT')
    def test_halftime_never_final(self):
        bundles=compile_sources([source_item(raw(status='PAUSED'),OBS)],END)[0]
        self.assertTrue(all(b.event_type!='MATCH_RESULT' for b in bundles))
        if bundles:self.assertIn('не окончательный',bundles[0].render())
    def test_future_kickoff_rejected(self):
        with self.assertRaises(ValueError):normalize(raw(utcDate=END.isoformat()),OBS,END)
    def test_awarded_not_played_final(self):
        with self.assertRaises(ValueError):normalize(raw(status='AWARDED'),OBS,END)
    def test_extra_time_not_mislabelled(self):
        r=raw();r['score']['duration']='EXTRA_TIME'
        self.assertFalse(compile_sources([source_item(r,OBS)],END)[0])
    def test_penalties_not_mixed(self):
        r=raw();r['score']['duration']='PENALTY_SHOOTOUT'
        self.assertFalse(compile_sources([source_item(r,OBS)],END)[0])
    def test_missing_final_rejected(self):
        r=raw();r['score']['fullTime']['home']=None
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_boolean_score_rejected(self):
        r=raw();r['score']['fullTime']['home']=True
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_winner_conflict_rejected(self):
        r=raw();r['score']['winner']='AWAY_TEAM'
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_identical_team_ids_rejected(self):
        r=raw();r['awayTeam']['id']=11
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_outside_season_rejected(self):
        r=raw();r['season']['endDate']='2026-06-01'
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_injected_name_rejected(self):
        r=raw();r['homeTeam']['shortName']='<b>unknown</b>'
        with self.assertRaises(ValueError):normalize(r,OBS,END)
    def test_invalid_nested_schema_rejected(self):
        for field in ('competition','season','homeTeam','awayTeam','score'):
            with self.subTest(field=field),self.assertRaises(ValueError):normalize(raw(**{field:None}),OBS,END)
    def test_next_moscow_day_not_counted_in_today_calendar(self):
        from premwire.daily_match_coverage import assess_match_coverage
        row=normalize(raw(status='TIMED',utcDate=(END+timedelta(hours=5)).isoformat()),OBS,END)
        calendar=dict(available_at=OBS,window_start=END.replace(hour=0,minute=0).isoformat(),window_end=(END+timedelta(days=1)).isoformat(),
                      requested_competitions=['PREMIER_LEAGUE'],complete_competitions=['PREMIER_LEAGUE'],fixtures=[row])
        result=assess_match_coverage([],[],{},END,calendar=calendar)
        self.assertEqual(result['status'],'COVERAGE_OK');self.assertEqual(result['scheduled_matches'],0)
    def test_source_identity_tamper_rejected(self):
        item=replace(source_item(raw(),OBS),external_id='different')
        self.assertFalse(compile_sources([item],END)[0])
    def test_draw(self):
        r=raw();r['score'].update(winner='DRAW',fullTime=dict(home=1,away=1))
        b=compile_sources([source_item(r,OBS)],END)[0][0]
        self.assertIn('вничью — 1:1',b.render());self.assertNotIn('обыграл',b.render())
        self.assertTrue(bind_fixture(b,[normalize(r,OBS,END)])[0])
    def test_away_winner(self):
        r=raw();r['score'].update(winner='AWAY_TEAM',fullTime=dict(home=0,away=2))
        b=compile_sources([source_item(r,OBS)],END)[0][0]
        self.assertIn('«Запад» обыграл «Восток» — 2:0',b.render());self.assertTrue(bind_fixture(b,[normalize(r,OBS,END)])[0])
    def test_same_fixture_deduped(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        self.assertEqual(len(merge_results([b,b])[0]),1)
    def test_source_without_competition_does_not_repeat_provider_result(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        weak=replace(b,event_id='caption',verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity')),
                     essential_facts=('result','match_date'))
        self.assertEqual(len(merge_results([b,weak])[0]),1)
    def test_undated_caption_does_not_repeat_unique_observed_final(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        weak=replace(b,event_id='caption',event_date='',verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity','match_date')),
                     essential_facts=('result',))
        self.assertEqual(len(merge_results([b,weak])[0]),1)
    def test_prior_undated_result_no_repost_from_new_provider(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        old=replace(b,event_id='caption',event_date='',verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity','match_date')),
                     essential_facts=('result',))
        self.assertFalse(merge_results([b],[old])[0])
    def test_new_undated_caption_no_repost_after_provider_receipt(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        weak=replace(b,event_id='caption',event_date='',verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity','match_date')),
                     essential_facts=('result',))
        self.assertFalse(merge_results([weak],[b])[0])
    def test_different_explicit_competition_not_suppressed(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        other=replace(b,event_id='other',verified_facts=tuple(replace(f,values=('CHAMPIONS_LEAGUE',)) if f.key=='competition' else f for f in b.verified_facts if f.key!='fixture_identity'))
        self.assertEqual(len(merge_results([b,other])[0]),2)
    def test_pre_kickoff_caption_not_bound_to_future_fixture(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        weak=replace(b,event_id='caption',event_date='',source_published_at=(END-timedelta(hours=6)).isoformat(),
            verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity','match_date')),essential_facts=('result',))
        self.assertEqual(len(merge_results([b,weak])[0]),2)
    def test_exact_dated_caption_keeps_confirmed_context(self):
        from premwire.daily_fact_digest import VerifiedFact
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        fact=VerifiedFact('scorers',('Алекс Пример',),b.source_refs,'SYNTHETIC_CONFIRMED_SCORER',('Гол забил Алекс Пример.',)*2)
        rich=replace(b,event_id='caption',verified_facts=tuple(f for f in b.verified_facts if f.key not in ('competition','fixture_identity'))+(fact,),
                     essential_facts=('result','match_date','scorers'))
        result=merge_results([b,rich])[0]
        self.assertEqual(len(result),1);self.assertIn('scorers',result[0].essential_facts)
        self.assertIn('fixture_identity',{f.key for f in result[0].verified_facts})
    def test_cross_day_provider_id_no_repost(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        self.assertFalse(merge_results([b],[replace(b,source_published_at=(END-timedelta(days=1)).isoformat())])[0])
    def test_score_conflict_both_removed(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        r=raw(id=1235);r['score']['fullTime']['home']=3
        other=compile_sources([source_item(r,OBS)],END)[0][0]
        self.assertFalse(merge_results([b,other])[0])
    def test_different_date_not_duplicate(self):
        b=compile_sources([source_item(raw(),OBS)],END)[0][0]
        self.assertEqual(len(merge_results([b,replace(b,event_id='other',event_date='2026-10-17')])[0]),2)
    def test_pipeline_factual_payload(self):
        # Newsworthy score: ordinary API results are deliberately not filler.
        row=raw();row['score']['fullTime']['home']=5
        item=source_item(row,OBS)
        result=prepare_immutable_edition([item],END.replace(hour=0,minute=0),END,FilterConfig(True,()))
        self.assertEqual(len(result['selected']),1)
        self.assertEqual(result['essential_facts_lost'],0);self.assertEqual(result['distorted_events'],0)
        self.assertFalse(result['provenance_issues']);self.assertFalse(result['trace'])
        self.assertIn('Football data provided by the Football-Data.org API',result['body'])
        self.assertLessEqual(telegram_length(result['body']),4096)
        bridge=publication_adapter(result,[item],'@shadow')
        self.assertEqual(bridge['payload']['chat_id'],'@shadow')
    def test_historical_replay_no_future_leak(self):
        item=source_item(raw(),OBS)
        result=prepare_immutable_edition([item],END-timedelta(days=1,hours=20),END-timedelta(days=1,minutes=1),FilterConfig(True,()))
        self.assertFalse(result['selected'])
    def test_credentials_header_only_cache_clean(self):
        requests=[]
        def opener(req,timeout):
            requests.append(req)
            return Response(dict(competition=dict(code='PL'),resultSet=dict(count=1),matches=[raw()]),req.full_url)
        with tempfile.TemporaryDirectory() as tmp:
            client=FootballData('TEST_SECRET_SENTINEL',tmp,opener=opener)
            client.matches('PL',date(2026,10,17),date(2026,10,19))
            client.matches('PL',date(2026,10,17),date(2026,10,19))
            self.assertEqual(client.calls,1);self.assertEqual(len(requests),1)
            self.assertNotIn('TEST_SECRET_SENTINEL',requests[0].full_url)
            self.assertTrue(all('TEST_SECRET_SENTINEL' not in p.read_text(encoding='utf-8') for p in Path(tmp).iterdir()))
    def test_batch_filter_acknowledged(self):
        def opener(req,timeout):
            return Response(dict(filters=dict(competitions='PL,CL,PD,SA,BL1,FL1'),resultSet=dict(count=1),matches=[raw()]),req.full_url)
        with tempfile.TemporaryDirectory() as tmp:
            client=FootballData('TEST_SECRET_SENTINEL',tmp,opener=opener)
            self.assertEqual(len(client.matches('ALL',date(2026,10,17),date(2026,10,19))['matches']),1)
            self.assertEqual(client.calls,1)
    def test_ignored_batch_filter_rejected(self):
        def opener(req,timeout):return Response(dict(filters={},resultSet=dict(count=1),matches=[raw()]),req.full_url)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataUnavailable):FootballData('TEST_SECRET_SENTINEL',tmp,opener=opener).matches('ALL',date(2026,10,17),date(2026,10,19))
    def test_accumulation_preserves_provider_availability(self):
        from premwire.daily_strategy import DailyState
        with tempfile.TemporaryDirectory() as tmp:
            state=DailyState(Path(tmp)/'copy.sqlite3')
            item=source_item(raw(),OBS)
            # Ledger observation may occur later; API receipt remains unchanged.
            state.accumulate([item])
            saved=replace(state.items()[0],source_collected_at=(END-timedelta(minutes=1)).isoformat())
            self.assertEqual(len(compile_sources([saved],END)[0]),1)
            state.db.close()
    def test_rate_limit_no_retry_no_secret_error(self):
        def opener(req,timeout):raise HTTPError(req.full_url,429,'private server body',{},None)
        with tempfile.TemporaryDirectory() as tmp:
            client=FootballData('TEST_SECRET_SENTINEL',tmp,opener=opener)
            with self.assertRaisesRegex(DataUnavailable,'FOOTBALL_DATA_HTTP_429'):
                client.matches('PL',date(2026,10,17),date(2026,10,19))
            self.assertEqual(client.calls,1);self.assertFalse(list(Path(tmp).iterdir()))
    def test_timeout_bounded_partial(self):
        def opener(req,timeout):self.assertLessEqual(timeout,4);raise TimeoutError()
        with tempfile.TemporaryDirectory() as tmp:
            client=FootballData('TEST_SECRET_SENTINEL',tmp,opener=opener,sleep=lambda _:None)
            items,calendar,cutoff=collect(client,END-timedelta(days=1),END)
            self.assertFalse(items);self.assertEqual(len(calendar['errors']),1);self.assertEqual(client.calls,1)
    def test_missing_key_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=FootballData('',tmp)
            items,calendar,_=collect(client,END-timedelta(days=1),END)
            self.assertFalse(items);self.assertEqual(client.calls,0);self.assertEqual(len(calendar['errors']),1)


if __name__=='__main__':unittest.main()
