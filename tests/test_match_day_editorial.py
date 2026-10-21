"""Synthetic cutoff/stage regressions; no real SOURCE, network or send."""
from datetime import timedelta
from dataclasses import replace
import json,unittest
from premwire.football_data import compile_sources,source_item
from premwire.match_day_editorial import nonfinal,validate,parse_relation,edition_gate
from premwire.editorial_v2 import select
from tests.test_football_data import raw,END,OBS

def scheduled():
    row=raw(status='TIMED',utcDate=(END+timedelta(hours=1)).isoformat())
    row['homeTeam']=dict(id=64,name='Liverpool FC',shortName='Liverpool')
    row['awayTeam']=dict(id=94,name='Villarreal CF',shortName='Villarreal')
    row['competition']=dict(id=2001,code='CL',name='UEFA Champions League')
    row['score'].update(winner=None,fullTime=dict(home=None,away=None))
    return row

class MatchDayTests(unittest.TestCase):
    def test_scheduled_never_result_or_scorer(self):
        b=nonfinal(source_item(scheduled(),OBS),END)
        self.assertEqual(b.event_type,'MATCH_SCHEDULED')
        self.assertIn('матч ещё не начался',b.render())
        self.assertNotIn('обыграл',b.render());self.assertNotIn('гол',b.render())
        self.assertFalse(validate(b,b.render()))
    def test_live_snapshot_explicit_not_final(self):
        row=scheduled();row.update(status='IN_PLAY',utcDate=(END-timedelta(hours=1)).isoformat())
        row['score'].update(winner='HOME_TEAM',fullTime=dict(home=2,away=1))
        b=nonfinal(source_item(row,OBS),END)
        self.assertIn('2:1 — не окончательный',b.render());self.assertEqual(b.event_type,'MATCH_LIVE')
        self.assertFalse(validate(b,b.render()))
        self.assertTrue(validate(b,b.render().replace('не окончательный','итоговый')))
    def test_scheduled_score_rejected(self):
        row=scheduled();row['score']['fullTime']['home']=1
        self.assertFalse(compile_sources([source_item(row,OBS)],END)[0])
    def test_after_cutoff_observation_rejected(self):
        self.assertFalse(compile_sources([source_item(scheduled(),END.isoformat())],END)[0])
    def test_unconfirmed_kickoff_cannot_be_invented(self):
        row=scheduled();row['utcDate']='2026-10-18'
        self.assertFalse(compile_sources([source_item(row,OBS)],END)[0])
    def test_not_all_calendar_matches_selected(self):
        row=scheduled();row['homeTeam']=dict(id=9001,name='East FC',shortName='East');row['awayTeam']=dict(id=9002,name='West FC',shortName='West')
        b=replace(nonfinal(source_item(row,OBS),END),freshness_category='FRESH_TODAY')
        self.assertFalse(select([b],END.date())[0])
    def test_official_absence_preserves_approximate_period(self):
        p=parse_relation('ОФИЦИАЛЬНО: Алекс Пример выбыл до середины декабря из-за травмы.')
        self.assertEqual(p[0],'INJURY');self.assertIn('до середины декабря',p[3]);self.assertNotIn('15 декабря',p[3])
    def test_negated_or_speculative_absence_not_promoted(self):
        self.assertIsNone(parse_relation('Возможно, Алекс Пример выбыл до середины декабря из-за травмы.'))
    def test_executive_talks_never_appointment(self):
        p=parse_relation('Фабрицио Романо: «Восток» ведёт переговоры о назначении Алекса Примера на должность спортивного директора.')
        self.assertEqual(p[0],'APPOINTMENT_TALKS');self.assertIn('ведёт переговоры',p[3]);self.assertEqual(p[2][-1],'TALKS_NOT_APPOINTED')
    def test_lineup_requires_exactly_eleven_distinct_confirmed_names(self):
        names=['Алекс '+x for x in ('Пример','Тестов','Востоков','Западов','Северин','Южный','Первый','Второй','Третий','Четвёртый','Пятый')]
        text='Официальный стартовый состав «Восток» на матч с «Запад»: '+', '.join(names)+'.'
        self.assertEqual(parse_relation(text)[0],'LINEUP')
        self.assertIsNone(parse_relation(text.replace(names[-1],names[0])))
        self.assertIsNone(parse_relation(text.replace('Официальный ','Предполагаемый ')))
    def test_lone_minor_yesterday_edition_blocked(self):
        from tests.test_daily_fact_digest import match_bundle
        b=replace(match_bundle(),event_type='APPOINTMENT',participants=('Алекс Пример','Восток'),event_date='',
                  source_published_at='2026-10-17T10:00:00+03:00',freshness_category='DATE_UNKNOWN')
        self.assertEqual(edition_gate([b],[],{},END)['status'],'BLOCKED')

    def test_original_source_clock_cannot_be_shifted(self):
        from premwire.match_day_editorial import validate_source_clocks
        item=source_item(scheduled(),OBS);b=nonfinal(item,END)
        self.assertTrue(validate_source_clocks(b,[item]))
        values=list(b.verified_facts[0].values);values[5]=(END-timedelta(minutes=1)).isoformat()
        changed=replace(b,verified_facts=(replace(b.verified_facts[0],values=tuple(values)),)+b.verified_facts[1:])
        self.assertFalse(validate_source_clocks(changed,[item]))

    def test_exact_lineup_merges_into_fixture(self):
        from premwire.match_day_editorial import recover
        from premwire.models import IncomingItem
        b=nonfinal(source_item(scheduled(),OBS),END)
        names=['Алекс '+x for x in ('Пример','Тестов','Востоков','Западов','Северин','Южный','Первый','Второй','Третий','Четвёртый','Пятый')]
        text='Официальный стартовый состав «Ливерпуль» на матч с «Вильярреал»: '+', '.join(names)+'.'
        item=IncomingItem('trusted','lineup',text,'https://example.test/lineup',END-timedelta(minutes=10))
        audit={'unsupported':[dict(sources=[item.canonical_url],text=text)]}
        result=recover([b],[item],audit)
        self.assertEqual(len(result),1);self.assertIn('official_lineup',result[0].essential_facts)
        self.assertFalse(validate(result[0],result[0].render()))

    def test_final_result_is_new_stage_after_prematch(self):
        from premwire.daily_editorial_gates import freshness_gate
        row=scheduled();prior=nonfinal(source_item(row,OBS),END)
        later=END+timedelta(hours=4)
        row.update(status='FINISHED',lastUpdated=(later-timedelta(minutes=2)).isoformat())
        row['score'].update(winner='HOME_TEAM',fullTime=dict(home=2,away=0))
        item=source_item(row,(later-timedelta(minutes=1)).isoformat())
        final=compile_sources([item],later)[0][0]
        kept,decisions=freshness_gate([final],[item],later,history=[prior])
        self.assertEqual(len(kept),1);self.assertEqual(decisions[0]['decision'],'KEEP')

if __name__=='__main__':unittest.main()
