"""Synthetic editorial contracts, no private SOURCE or network."""
from dataclasses import replace
from datetime import timedelta
import json,unittest
from premwire.editorial_v2 import team_name,score,select,validate,parts,fingerprint
from premwire.football_data import compile_sources,source_item
from tests.test_football_data import raw,END,OBS
from premwire.models import IncomingItem
from premwire.editorial_source_context import enrich

def bundle(home=5,away=0,previous=False):
    r=raw();r['homeTeam']=dict(id=397,name='Brighton & Hove Albion FC',shortName='Brighton Hove')
    r['awayTeam']=dict(id=354,name='Crystal Palace FC',shortName='Crystal Palace')
    r['score']['fullTime']=dict(home=home,away=away)
    r['score']['winner']='DRAW' if home==away else 'HOME_TEAM' if home>away else 'AWAY_TEAM'
    if previous:r['utcDate']=(END-timedelta(days=1,hours=4)).isoformat()
    b=compile_sources([source_item(r,OBS)],END)[0][0]
    return replace(b,freshness_category='RECENT_UNPUBLISHED' if previous else 'FRESH_TODAY')

class EditorialTests(unittest.TestCase):
    def test_provider_id_and_name_both_required(self):
        self.assertEqual(team_name(dict(id=397,name='Brighton & Hove Albion FC')),'Брайтон')
        self.assertEqual(team_name(dict(id=397,name='Other Brighton')),'Other Brighton')
        self.assertEqual(team_name(dict(id=999,name='Brighton & Hove Albion FC')),'Brighton & Hove Albion FC')
    def test_blowout_beats_routine(self):self.assertGreater(score(bundle(),END.date())[0],score(bundle(1),END.date())[0])
    def test_no_unproven_surprise(self):self.assertEqual(score(bundle(),END.date())[1]['surprise'],0)
    def test_ordinary_not_filler(self):self.assertFalse(select([bundle(1)],END.date())[0])
    def test_yesterday_requires_more(self):self.assertFalse(select([bundle(3,previous=True)],END.date())[0])
    def test_facts_immutable(self):
        b=bundle();chosen,_=select([b],END.date());self.assertEqual(fingerprint(b),fingerprint(chosen[0]))
    def test_exact_consolidation(self):self.assertEqual(len(select([bundle(),bundle()],END.date())[0]),1)
    def test_natural_russian(self):
        b=replace(bundle(),editorial_mode='v2');text=b.render()
        self.assertIn('«Брайтон» разгромил «Кристал Пэлас» — 5:0',text)
        self.assertNotIn('Турнир:',text);self.assertNotIn('Клуб ',text)
        self.assertFalse(validate(b,text))
    def test_away_winner_orientation(self):
        b=replace(bundle(0,5),editorial_mode='v2');self.assertFalse(validate(b,b.render()))
        self.assertIn('«Кристал Пэлас» разгромил «Брайтон» — 5:0',b.render())
    def test_draw_preserved(self):
        b=replace(bundle(3,3),editorial_mode='v2');self.assertIn('3:3',b.render());self.assertFalse(validate(b,b.render()))
    def test_new_cause_rejected(self):
        b=replace(bundle(),editorial_mode='v2');self.assertTrue(validate(b,b.render()+' Тренер потерял раздевалку.'))
    def test_score_mutation_rejected(self):
        b=replace(bundle(),editorial_mode='v2');self.assertTrue(validate(b,b.render().replace('5:0','6:0')))
    def test_slot_score_corruption_rejected(self):
        b=bundle();facts=tuple(replace(f,values=(f.values[0],f.values[1],'6:0')) if f.key=='result' else f for f in b.verified_facts)
        b=replace(b,verified_facts=facts,editorial_mode='v2');self.assertIn('RESULT_PARTICIPANTS_OR_SCORE',validate(b,b.render()))
    def test_slot_date_corruption_rejected(self):
        b=replace(bundle(),event_date='2026-10-17',editorial_mode='v2');self.assertIn('EVENT_DATE',validate(b,b.render()))
    def test_only_closed_variants(self):
        b=replace(bundle(),editorial_mode='v2')
        self.assertFalse(validate(b,b.render(1),1))
        with self.assertRaises(ValueError):b.render(2)
    def test_every_essential_slot_visible(self):
        b=replace(bundle(),editorial_mode='v2')
        for key in b.essential_facts:self.assertIn(parts(b,0)[key],b.render())
    def test_timeout_has_one_call_and_exact_fallback(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        r=raw();r['score']['fullTime']['home']=5
        i=source_item(r,OBS);start=END.replace(hour=0,minute=0)
        plain=prepare_immutable_edition([i],start,END,None)
        calls=[]
        def timeout(prompt):calls.append(prompt);raise TimeoutError()
        edited=prepare_immutable_edition([i],start,END,None,style_chat=timeout)
        self.assertEqual(plain['body'],edited['body']);self.assertEqual(len(calls),1)
    def test_unavailable_model_no_retry(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        r=raw();r['score']['fullTime']['home']=5
        def unavailable(prompt):raise ConnectionError()
        v=prepare_immutable_edition([source_item(r,OBS)],END.replace(hour=0,minute=0),END,None,style_chat=unavailable)
        self.assertEqual(sum(t['calls'] for t in v['trace']),1);self.assertEqual(v['distorted_events'],0)
    def test_free_prose_from_model_rejected(self):
        from premwire.daily_fact_digest import apply_choices
        b=replace(bundle(),editorial_mode='v2')
        choices,issues=apply_choices([b],json.dumps([dict(event_id=b.event_id,text='Победу обеспечил выдуманный игрок')]))
        self.assertTrue(issues);self.assertEqual(choices[b.event_id],0)
    def test_recover_official_injury_only_from_admitted_source(self):
        i=IncomingItem('trusted','x','ОФИЦИАЛЬНО: Алекс Пример выбыл на 3-4 недели из-за травмы.','https://example.test/injury',END-timedelta(minutes=4))
        self.assertFalse(enrich([], [i], {'unsupported':[]}))
        b=enrich([], [i], {'unsupported':[dict(sources=[i.canonical_url],text=i.text)]})[0]
        self.assertEqual(b.verified_facts[0].values,('Алекс Пример','3','4','недели','SOURCE_OFFICIAL'))
        self.assertNotIn('сегодня',b.render())
        self.assertFalse(validate(replace(b,editorial_mode='v2'),b.render()))
    def test_scorer_binds_both_teams_and_single_goal(self):
        b=bundle(1);i=IncomingItem('trusted','x','«Брайтон» обыграл «Кристал Пэлас». Единственный мяч забил Алекс Пример.','https://example.test/scorer',END-timedelta(minutes=4))
        row=dict(sources=[i.canonical_url],text=i.text)
        v=enrich([b],[i],{'unsupported':[row]})[0]
        self.assertIn('decisive_scorer',[f.key for f in v.verified_facts])
        self.assertFalse(validate(replace(v,editorial_mode='v2'),replace(v,editorial_mode='v2').render()))
        other=replace(i,text=i.text.replace('Кристал Пэлас','Другой клуб'))
        self.assertNotIn('decisive_scorer',[f.key for f in enrich([b],[other],{'unsupported':[dict(row,text=other.text)]})[0].verified_facts])
        self.assertNotIn('decisive_scorer',[f.key for f in enrich([bundle(5)],[i],{'unsupported':[row]})[0].verified_facts])
    def test_old_caption_cannot_enrich_today(self):
        i=IncomingItem('trusted','x','«Брайтон» обыграл «Кристал Пэлас». Единственный мяч забил Алекс Пример.','https://example.test/old',END-timedelta(days=1))
        self.assertNotIn('decisive_scorer',[f.key for f in enrich([bundle(1)],[i],{'unsupported':[dict(sources=[i.canonical_url],text=i.text)]})[0].verified_facts])
    def test_conflicting_scorers_both_withheld(self):
        a=IncomingItem('trusted','a','«Брайтон» обыграл «Кристал Пэлас». Единственный мяч забил Алекс Пример.','https://example.test/a',END-timedelta(minutes=4))
        b=replace(a,external_id='b',canonical_url='https://example.test/b',text=a.text.replace('Алекс Пример','Борис Пример'))
        v=enrich([bundle(1)],[a,b],{'unsupported':[dict(sources=[i.canonical_url],text=i.text) for i in (a,b)]})[0]
        self.assertNotIn('decisive_scorer',[f.key for f in v.verified_facts])
    def test_draw_goal_context_no_invented_second_minute(self):
        i=IncomingItem('trusted','x','«Брайтон» не обыграл «Кристал Пэлас». На гол Примера на 35-й минуте хозяева ответили точным ударом Тестова-Примера.','https://example.test/draw',END-timedelta(minutes=4))
        v=enrich([bundle(1,1)],[i],{'unsupported':[dict(sources=[i.canonical_url],text=i.text)]})[0]
        f=next(f for f in v.verified_facts if f.key=='equalising_goals')
        self.assertEqual(f.values,('Примера','35','Тестова-Примера','HOME_REPLY_TO_AWAY'))
        self.assertNotIn('51',f.formulations[0])
    def test_cleaned_candidate_cannot_override_original_evidence(self):
        i=IncomingItem('trusted','x','Обычный рекламный текст.','https://example.test/injected',END-timedelta(minutes=4))
        audit={'unsupported':[dict(sources=[i.canonical_url],text='ОФИЦИАЛЬНО: Алекс Пример выбыл на 3-4 недели из-за травмы.')]}
        self.assertFalse(enrich([], [i],audit))
        self.assertEqual(audit['source_context_recovery'][0]['reason'],'ORIGINAL_SOURCE_REPARSE_FAILED')
    def test_conditional_fee_not_a_deal_or_bid(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        i=IncomingItem('trusted','x','⚽️ «Барселона» хочет подписать Алекса Примера\n💰 Сумма трансфера может составить около €45 млн.\n📰 Example News','https://example.test/fee',END-timedelta(minutes=4))
        v=prepare_immutable_edition([i],END.replace(hour=0,minute=0),END,None)
        self.assertIn('Возможная сумма трансфера — около 45 млн евро.',v['body'])
        self.assertEqual(v['selected'][0].event_type,'TRANSFER_INTEREST')
        self.assertNotIn('договорились',v['body']);self.assertEqual(v['essential_facts_lost'],0)
    def test_id_bound_russian_caption_does_not_split_fixture(self):
        from premwire.football_data import merge_results
        b=bundle();facts=tuple(replace(f,values=('Брайтон','Кристал Пэлас','5:0')) if f.key=='result' else f for f in b.verified_facts if f.key!='fixture_identity')
        caption=replace(b,event_id='caption',participants=('Брайтон','Кристал Пэлас'),verified_facts=facts)
        self.assertEqual(len(merge_results([b,caption])[0]),1)
        canonical=replace(b,verified_facts=tuple(replace(f,values=('Брайтон','Кристал Пэлас','5:0')) if f.key=='result' else f for f in b.verified_facts),editorial_mode='v2')
        self.assertFalse(validate(canonical,canonical.render()))

if __name__=='__main__':unittest.main()
