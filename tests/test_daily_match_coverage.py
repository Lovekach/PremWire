"""Synthetic calendar snapshots; no network, secrets or production state."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
import unittest

from premwire.daily_match_coverage import assess_match_coverage
from premwire.daily_text_digest import MOSCOW
from premwire.daily_fact_e2e import prepare_immutable_edition
from premwire.models import IncomingItem
from tests.test_daily_edition_gate import compiled
from tests.test_daily_editorial_gates import source
from premwire.config import FilterConfig

END=datetime(2026,10,17,20,56,tzinfo=MOSCOW)
START=END.replace(hour=0,minute=0)

def fixture(n=1,status='FULL_TIME',**extra):
    return dict(fixture_id=n,home='Восток',away='Запад',home_id=11,away_id=12,
        status=status,kickoff_at=(END-timedelta(hours=4)).isoformat(),
        event_date=END.date().isoformat(),score=[2,0],competition='Premier League',
        available_at=(END-timedelta(minutes=10)).isoformat(),source='https://example.test/fixture/'+str(n),**extra)

def calendar(*rows,**extra):
    value=dict(available_at=(END-timedelta(minutes=5)).isoformat(),
        requested_competitions=['PREMIER_LEAGUE'],complete_competitions=['PREMIER_LEAGUE'],
        window_start=START.isoformat(),window_end=END.isoformat(),fixtures=list(rows))
    value.update(extra);return value

def result_bundle():
    from premwire.daily_fact_digest import ImmutableBundle,VerifiedFact
    refs=('https://example.test/result',)
    f=VerifiedFact('result',('Восток','Запад','2:0'),refs,'Восток beat Запад 2-0.',('Восток победил Запад — 2:0.',)*2)
    return ImmutableBundle('fixture-test','MATCH_RESULT',('Восток','Запад'),(f,),refs,95,('result',),'🔥 Главное')

class CoverageTests(unittest.TestCase):
    def assess(self,c=None,selected=()):return assess_match_coverage([],selected,{},END,calendar=c)
    def test_absent_calendar_unknown_not_zero(self):
        r=self.assess();self.assertEqual(r['status'],'COVERAGE_UNKNOWN');self.assertIsNone(r['scheduled_matches'])
        self.assertTrue(r['does_not_block_publication'])
    def test_complete_empty_calendar_is_not_unknown(self):
        r=self.assess(calendar());self.assertEqual(r['status'],'COVERAGE_OK');self.assertEqual(r['finished_before_cutoff'],0)
    def test_partial_empty_calendar_not_zero_total(self):
        r=self.assess(calendar(complete_competitions=[]));self.assertEqual(r['status'],'COVERAGE_PARTIAL');self.assertIsNone(r['scheduled_matches'])
    def test_missing_window_cannot_prove_coverage(self):
        c=calendar();c.pop('window_start');self.assertEqual(self.assess(c)['status'],'COVERAGE_PARTIAL')
    def test_malformed_calendar_cannot_break_other_news(self):
        for c in (calendar(fixtures=None),calendar(complete_competitions=None),calendar(requested_competitions=None)):
            self.assertEqual(self.assess(c)['status'],'COVERAGE_UNKNOWN')
    def test_one_final_missing(self):
        r=self.assess(calendar(fixture()));self.assertEqual(r['missing_results'],1);self.assertEqual(r['fixtures'][0]['reason'],'NOT_COLLECTED')
    def test_two_missing_finals_critical_but_nonblocking(self):
        other=fixture(2);other.update(home='Север',away='Юг',home_id=21,away_id=22)
        r=self.assess(calendar(fixture(),other));self.assertEqual(r['status'],'COVERAGE_CRITICAL');self.assertTrue(r['does_not_block_publication'])
    def test_same_match_under_different_source_ids_not_double_counted(self):
        r=self.assess(calendar(fixture(),fixture(2)));self.assertEqual(r['confirmed_results'],1)
    def test_live_halftime_not_final(self):
        for state in ('LIVE','HALF_TIME','SCHEDULED','POSTPONED','CANCELLED'):
            r=self.assess(calendar(fixture(status=state)));self.assertEqual(r['confirmed_results'],0)
    def test_same_fixture_does_not_double_count(self):
        f=fixture();r=self.assess(calendar(f,deepcopy(f)));self.assertEqual(r['confirmed_results'],1)
    def test_conflicting_same_id_never_complete(self):
        f=fixture();other=fixture();other['score']=[3,0]
        r=self.assess(calendar(f,other));self.assertEqual(r['status'],'COVERAGE_PARTIAL');self.assertIsNone(r['scheduled_matches'])
    def test_future_snapshot_never_historical_evidence(self):
        c=calendar(fixture(),available_at=(END+timedelta(seconds=1)).isoformat())
        r=self.assess(c);self.assertEqual(r['status'],'COVERAGE_UNKNOWN');self.assertIsNone(r['confirmed_results'])
    def test_exact_cutoff_is_exclusive(self):
        r=self.assess(calendar(fixture(),available_at=END.isoformat()));self.assertEqual(r['status'],'COVERAGE_UNKNOWN')
    def test_future_row_not_counted(self):
        f=fixture();f['available_at']=(END+timedelta(seconds=1)).isoformat()
        r=self.assess(calendar(f));self.assertEqual(r['confirmed_results'],0);self.assertIsNone(r['scheduled_matches'])
    def test_future_kickoff_is_not_finished_after_cutoff(self):
        f=fixture(status='SCHEDULED');f['kickoff_at']=(END+timedelta(hours=1)).isoformat()
        r=self.assess(calendar(f));self.assertEqual(r['fixtures'][0]['reason'],'SCHEDULED_AFTER_CUTOFF');self.assertEqual(r['confirmed_results'],0)
    def test_final_before_its_kickoff_rejected(self):
        f=fixture();f['kickoff_at']=(END+timedelta(hours=1)).isoformat();self.assertEqual(self.assess(calendar(f))['confirmed_results'],0)
    def test_wrong_scope_is_partial(self):
        c=calendar(fixture(),requested_competitions=['PREMIER_LEAGUE','CHAMPIONS_LEAGUE'])
        self.assertEqual(self.assess(c)['status'],'COVERAGE_PARTIAL')
    def test_unzoned_snapshot_rejected(self):
        self.assertEqual(self.assess(calendar(available_at='2026-10-17T17:00:00'))['status'],'COVERAGE_UNKNOWN')
    def test_missing_or_same_team_ids_rejected(self):
        for ids in ((None,12),(11,11)):
            f=fixture();f['home_id'],f['away_id']=ids
            self.assertEqual(self.assess(calendar(f))['confirmed_results'],0)
    def test_invalid_score_not_confirmed(self):
        for score in ([None,0],[-1,0],[True,0],['2',0],[],None):
            f=fixture();f['score']=score;self.assertEqual(self.assess(calendar(f))['confirmed_results'],0)
    def test_explicit_exact_match_can_be_included(self):
        b=result_bundle()
        from premwire.daily_fact_digest import VerifiedFact
        cf=VerifiedFact('competition',('PREMIER_LEAGUE',),b.source_refs,'АПЛ',('В АПЛ.',)*2)
        b=replace(b,event_date='2026-10-17',verified_facts=b.verified_facts+(cf,))
        r=self.assess(calendar(fixture()),[b]);self.assertEqual(r['included_results'],1);self.assertEqual(r['status'],'COVERAGE_OK')
    def test_unknown_match_date_cannot_be_guessed(self):
        b=result_bundle()
        self.assertEqual(self.assess(calendar(fixture()),[b])['included_results'],0)
    def test_no_fixture_data_does_not_modify_digest(self):
        i=source('ОФИЦИАЛЬНО: Алекс Пример — новый главный тренер «Восток».')
        r=prepare_immutable_edition([i],START,END,FilterConfig(True,()),edition_day=END.date())
        self.assertTrue(r['body']);self.assertEqual(r['audit']['match_coverage']['status'],'COVERAGE_UNKNOWN')
        self.assertFalse(r['essential_facts_lost']);self.assertFalse(r['distorted_events'])

class InjuryTests(unittest.TestCase):
    def test_stretcher_literal_person_no_guessed_club_or_diagnosis(self):
        text='Алекса Примера уносят с поля на носилках после столкновения головами с игроком «Восток».'
        b=compiled(text);self.assertIsNotNone(b);self.assertEqual(b.event_type,'INJURY')
        self.assertEqual(b.verified_facts[0].values,('Алекса Примера','Восток','STRETCHER_AFTER_HEAD_COLLISION_NO_DIAGNOSIS'))
        self.assertNotIn('сотрясение',b.render())
    def test_named_club_field_exit_literal(self):
        b=compiled('Потеря для «Запад» — Алекс Пример покинул поле из-за травмы.')
        self.assertIsNotNone(b);self.assertEqual(b.participants,('Алекс Пример','Запад'))
        self.assertIn('Игрок клуба «Запад»',b.render())
    def test_future_or_speculative_not_supported(self):
        self.assertIsNone(compiled('Потеря для «Запад» — Алекс Пример может покинуть поле из-за травмы.'))
    def test_no_identity_cannot_be_reconstructed(self):
        self.assertIsNone(compiled('Игрока уносят с поля на носилках после столкновения головами с игроком «Восток».'))
