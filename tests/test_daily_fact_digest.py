from dataclasses import FrozenInstanceError, replace
from datetime import datetime,timezone,date
import json
from types import SimpleNamespace
import unittest

from premwire.daily_fact_shadow import Fact
from premwire.models import IncomingItem
from premwire.daily_fact_digest import (compile_bundle,apply_choices,validate_edit,
    edit_batches,render,batches,batch_prompt,attach_match_context,audit_digest)


def candidate(texts):
    facts=tuple(Fact(t,f'https://x.com/test/status/{n}','detail') for n,t in enumerate(texts))
    source=IncomingItem('x_premier_league','1','\n'.join(texts),'https://x.com/test/status/1',datetime(2026,10,11,tzinfo=timezone.utc))
    return SimpleNamespace(facts=facts,source=source,participants=())


def match_bundle():
    return compile_bundle(candidate([
        'Manchester City make it six wins out of six after a 3-0 victory over ten-man Liverpool at Anfield.',
        'Голы на счету Холанда, Доку и Шерки.',
        'А Гравенберх подставил «Ливерпуль» и удалился на 62-й минуте.',
        '«Ман Сити» закрепляется на первой строчке и отрывается от «Арсенала» на 3 очка.'
    ]),date(2026,10,11))


class ImmutableFactTests(unittest.TestCase):
    def test_typed_essential_facts(self):
        b=match_bundle()
        self.assertEqual(b.event_type,'MATCH_RESULT')
        self.assertEqual(b.essential_facts,('result','scorers','red_card'))
        self.assertEqual(next(f.values for f in b.verified_facts if f.key=='result'),('Манчестер Сити','Ливерпуль','3:0'))
        self.assertEqual(next(f.values for f in b.verified_facts if f.key=='scorers'),('Холанд','Доку','Черки'))
        self.assertEqual(next(f.values for f in b.verified_facts if f.key=='red_card'),('Гравенберх','62'))

    def test_deep_immutable(self):
        b=match_bundle()
        with self.assertRaises(FrozenInstanceError):b.importance=0
        with self.assertRaises(FrozenInstanceError):b.verified_facts[0].values=('2:0',)
        self.assertIsInstance(b.verified_facts,tuple)
        self.assertTrue(all(isinstance(f.values,tuple) for f in b.verified_facts))

    def test_each_fact_has_source_and_evidence(self):
        self.assertTrue(all(f.source_refs and f.evidence for f in match_bundle().verified_facts))

    def test_no_fact_loss_in_both_style_variants(self):
        b=match_bundle()
        for variant in (0,1):
            text=b.render(variant)
            for required in ('3:0','Манчестер Сити','Ливерпуль','Холанд','Доку','Черки','Гравенберх','62'):
                self.assertIn(required,text)
            self.assertEqual(validate_edit(b,text),[])
        self.assertNotEqual(b.render(0),b.render(1))

    def test_changed_score_rejected(self):
        b=match_bundle();self.assertTrue(validate_edit(b,b.render().replace('3:0','2:0')))

    def test_missing_scorer_rejected(self):
        b=match_bundle();self.assertTrue(validate_edit(b,b.render().replace('Черки','')))

    def test_wrong_role_rejected(self):
        b=match_bundle();self.assertTrue(validate_edit(b,b.render()+' Хозяева победили.'))

    def test_nonnumeric_hallucination_rejected(self):
        b=match_bundle();self.assertTrue(validate_edit(b,b.render()+' Победа гарантирует титул.'))

    def test_unknown_player_rejected(self):
        b=match_bundle();self.assertTrue(validate_edit(b,b.render().replace('Черки','Салах')))

    def test_transfer_direction_status_date_fee_preserved(self):
        b=compile_bundle(candidate(['ОФИЦИАЛЬНО: Абдулай Дукуре перешёл в «Шарлотт» (США) из «Неома» (Саудовская Аравия).',
            'Свободный агент.','Контракт до лета 2027 года.']),date(2026,10,11))
        self.assertEqual(next(f.values for f in b.verified_facts if f.key=='transfer'),('Абдулай Дукуре','Неома','Шарлотт','OFFICIAL'))
        for change in (b.render().replace('2027','2028'), b.render().replace('из Неома в Шарлотт','из Шарлотт в Неома'),b.render().replace('официально перешёл','обсуждает переход')):
            self.assertTrue(validate_edit(b,change))

    def test_complete_valid_batch(self):
        b=match_bundle();choices,issues=apply_choices([b],json.dumps([{'event_id':b.event_id,'variant':1}]))
        self.assertEqual(issues,[]);self.assertEqual(choices[b.event_id],1)

    def test_bad_payload_always_falls_back(self):
        b=match_bundle()
        for payload in ('not json','[]',json.dumps([{'event_id':b.event_id,'variant':True}]),
            json.dumps([{'event_id':b.event_id,'variant':2}]),json.dumps([{'event_id':'wrong','variant':1}]),
            json.dumps([{'event_id':b.event_id,'variant':1,'text':'Салах забил'}])):
            choices,issues=apply_choices([b],payload)
            self.assertTrue(issues);self.assertEqual(choices[b.event_id],0)

    def test_duplicate_id(self):
        b=match_bundle();choices,issues=apply_choices([b],json.dumps([{'event_id':b.event_id,'variant':1}]*2))
        self.assertTrue(issues);self.assertEqual(choices[b.event_id],0)

    def test_timeout_no_retry_no_fact_loss(self):
        b=match_bundle();calls=[]
        def chat(prompt):calls.append(prompt);raise TimeoutError()
        choices,trace=edit_batches([b],chat,100,lambda:0)
        self.assertEqual(len(calls),1);self.assertEqual(choices[b.event_id],0)
        self.assertEqual(trace[0]['issues'],['TimeoutError'])
        self.assertEqual(validate_edit(b,b.render(choices[b.event_id])),[])

    def test_deadline_no_call(self):
        b=match_bundle();calls=[]
        choices,trace=edit_batches([b],lambda p:calls.append(p),0,lambda:1)
        self.assertEqual(calls,[]);self.assertEqual(choices[b.event_id],0)

    def test_stable_id(self):self.assertEqual(match_bundle().event_id,match_bundle().event_id)

    def test_no_duplicate_whole_events(self):
        b=match_bundle();body,selected=render([b,b],date(2026,10,11))
        self.assertEqual(len(selected),1);self.assertEqual(body.count('со счётом 3:0'),1)

    def test_short_source_requires_only_confirmed_fact(self):
        b=compile_bundle(candidate(['«Арсенал» вырвал победу у «Лидса».']),date(2026,10,11))
        self.assertEqual(b.essential_facts,('event',));self.assertNotIn('3:0',b.render())

    def test_promotional_or_unresolved_shape_no_renderer(self):
        for text in ('Подключайтесь к нашей трансляции!','make it six wins from their opening six games!', 'Who is best? Vote now!'):
            self.assertIsNone(compile_bundle(candidate([text]),date(2026,10,11)))

    def test_large_fact_payload_not_context_expansion(self):
        self.assertEqual(list(batches([match_bundle()],max_chars=10)),[])

    def test_no_llm_call_for_identical_style_variants(self):
        b=compile_bundle(candidate(['«Арсенал» вырвал победу у «Лидса».']),date(2026,10,11))
        calls=[]
        choices,trace=edit_batches([b],lambda p:calls.append(p),100,lambda:0)
        self.assertEqual(calls,[]);self.assertEqual(trace,[])
        self.assertEqual(choices[b.event_id],0)

    def test_partial_invalid_batch_is_atomic_fallback(self):
        b=match_bundle();other=compile_bundle(candidate(['«Арсенал» вырвал победу у «Лидса».']),date(2026,10,11))
        choices,issues=apply_choices([b,other],json.dumps([{'event_id':b.event_id,'variant':1},{'event_id':other.event_id,'variant':True}]))
        self.assertTrue(issues);self.assertTrue(all(v==0 for v in choices.values()))

    def test_final_body_audit_measures_loss_and_distortion(self):
        b=match_bundle();body,selected=render([b],date(2026,10,11))
        self.assertEqual(audit_digest(body,selected,{})['essential_facts_lost'],0)
        broken=body.replace('Голы забили Холанд, Доку и Черки.','')
        audit=audit_digest(broken,selected,{})
        self.assertEqual(audit['essential_facts_lost'],1)
        self.assertEqual(audit['distorted_events'],1)

    def test_wrong_source_evidence_detected(self):
        b=match_bundle();body,selected=render([b],date(2026,10,11))
        audit=audit_digest(body,selected,{},[candidate(['Другой матч.']).source])
        self.assertTrue(audit['provenance_issues'])

    def test_explicit_club_handle_source_provenance(self):
        source=replace(candidate(["Three goals and a first win at Selhurst Park for Pierre Sage's @CPFC 🦅"]).source,
                       canonical_url='https://x.com/test/status/1')
        b=compile_bundle(candidate(["Three goals and a first win at Selhurst Park for Pierre Sage's Crystal Palace 🦅"]),date(2026,10,11))
        body,selected=render([b],date(2026,10,11))
        self.assertEqual(audit_digest(body,selected,{},[source])['provenance_issues'],[])

    def test_related_match_context_composed_without_fact_loss(self):
        from dataclasses import replace
        fixture=replace(compile_bundle(candidate(['«МЮ» не смогли обыграть «Тоттенхэм».']),date(2026,10,11)),participants=('Манчестер Юнайтед','Тоттенхэм'))
        stats=compile_bundle(candidate(['«Манчестер Юнайтед» набрал всего 6 очков в первых 6 турах чемпионата.',
            'Это худший старт клуба в лиге за последние 40 лет.']),date(2026,10,11))
        merged=attach_match_context([fixture,stats],date(2026,10,11))
        self.assertEqual(len(merged),1)
        self.assertEqual(len(merged[0].essential_facts),3)
        self.assertIn('Тоттенхэм',merged[0].render())
        self.assertIn('40 лет',merged[0].render())
        injury=replace(stats,event_type='INJURY')
        self.assertEqual(len(attach_match_context([fixture,injury],date(2026,10,11))),2)
        second=replace(fixture,event_id='different',participants=('Манчестер Юнайтед','Лидс'))
        self.assertEqual(len(attach_match_context([fixture,second,stats],date(2026,10,11))),3)


if __name__=='__main__':unittest.main()
