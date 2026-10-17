"""Public-safe, source-local regressions for typed daily edition safety."""
from dataclasses import replace
from datetime import date,datetime,timezone
import unittest

from premwire.daily_fact_digest import (
    compile_bundle,final_digest_gate,bundle_issues,VerifiedFact,
    supplement_explicit_context,CONTRACTS_SECTION,STATS_SECTION,
    fact_signature,apply_choices,edit_batches,StatisticalObservation,
)
from premwire.daily_fact_shadow import Fact,FactBundle
from premwire.models import IncomingItem
from premwire.daily_strategy import confirmed_published_history
from tests.test_daily_fact_digest import candidate,match_bundle

DAY=date(2026,10,13)


def source(text,url='https://example.test/news/1'):
    return IncomingItem('test','1',text,url,datetime(2026,10,13,10,tzinfo=timezone.utc))


def compiled(text):
    return compile_bundle(candidate([text]),DAY)


class EditionGateTests(unittest.TestCase):
    def test_contract_category_not_transfer(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример продлил контракт с «Востоком» до лета 2029 года.')
        self.assertEqual(b.section,CONTRACTS_SECTION)

    def test_mixed_metric_table_is_excluded_not_numeric_pass(self):
        b=compiled('Команды, совершившие 180 и более касаний мяча в штрафной площади соперника в текущем сезоне в рамках чемпионата: «Восток» (34 гола) «Запад» (18 голов)')
        self.assertIsNone(b)

    def test_wrong_unit_rejected_only_member(self):
        b=compiled('Alex Example reaches 7 Premier League goals in 8 games.')
        f=b.verified_facts[0];s=f.statistics[0]
        bad=replace(b,verified_facts=(replace(f,statistics=(replace(s,unit='TOUCHES'),)),))
        kept,checks=final_digest_gate([bad,match_bundle()])
        self.assertEqual(len(kept),1);self.assertIn('STAT_METRIC_AND_UNIT',checks[0]['reason'])

    def test_wrong_section_rejected(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример продлил контракт с «Востоком» до лета 2029 года.')
        self.assertIn('SECTION_CLASSIFICATION',bundle_issues(replace(b,section='🔄 Трансферы')))

    def test_repeated_event_not_new_source_url(self):
        b=match_bundle();other=replace(b,event_id='different',source_refs=('https://example.test/new',),verified_facts=tuple(replace(f,source_refs=('https://example.test/new',),statistics=tuple(replace(s,source='https://example.test/new') for s in f.statistics)) for f in b.verified_facts))
        kept,checks=final_digest_gate([other],[b]);self.assertEqual(kept,[])
        self.assertEqual(checks[0]['reason'],'EVENT_DEDUPE_CROSS_DAY_UNCHANGED_FACTS')

    def test_real_new_fact_not_suppressed(self):
        b=match_bundle();old=replace(b,verified_facts=b.verified_facts[:-1])
        self.assertEqual(final_digest_gate([b],[old])[0],[b])

    def test_date_annotation_not_event_development(self):
        b=match_bundle();f=VerifiedFact('match_date',('2026-10-11',),b.source_refs,'yesterday',('Матч состоялся 11 октября.',)*2)
        newer=replace(b,verified_facts=b.verified_facts+(f,),essential_facts=b.essential_facts+('match_date',))
        self.assertEqual(final_digest_gate([newer],[b])[0],[])

    def test_source_term_supplement_needs_same_person_and_club(self):
        b=compiled('Alex Example signs for Harbor FC today after the verbal agreement last week, free move now confirmed.')
        start=datetime(2026,10,13,tzinfo=timezone.utc);end=datetime(2026,10,14,tzinfo=timezone.utc)
        wrong=source('ОФИЦИАЛЬНО: Alex Example — игрок «Other FC». Контракт до лета 2027 года.')
        self.assertFalse(any(f.key=='contract' for f in supplement_explicit_context(b,[wrong],start,end).verified_facts))
        right=source('ОФИЦИАЛЬНО: Alex Example — игрок «Harbor FC». Контракт до лета 2027 года.')
        supplemented=supplement_explicit_context(b,[right],start,end)
        self.assertIn('contract',supplemented.essential_facts)
        self.assertEqual(supplemented.verified_facts[-1].source_refs,(right.canonical_url,))

    def test_stale_term_supplement_not_allowed(self):
        b=compiled('Alex Example signs for Harbor FC today after the verbal agreement last week, free move now confirmed.')
        item=replace(source('ОФИЦИАЛЬНО: Alex Example — игрок «Harbor FC». Контракт до лета 2027 года.'),published_at=datetime(2026,10,12,tzinfo=timezone.utc))
        start=datetime(2026,10,13,tzinfo=timezone.utc);end=datetime(2026,10,14,tzinfo=timezone.utc)
        self.assertEqual(supplement_explicit_context(b,[item],start,end).verified_facts,b.verified_facts)

    def test_loan_quote_not_injury(self):
        b=compiled('Coach Example: “Alex Example does NOT need to be loaned out”. He returned from injury.')
        self.assertEqual(b.event_type,'COACH_LOAN_POSITION');self.assertNotIn('Травмы',b.section)

    def test_loan_status_not_transfer_completion(self):
        b=compiled('Coach Example closes all doors to a loan for Alex Example in January: “No”.')
        self.assertEqual(b.verified_facts[0].values[-1],'SPEAKER_OPPOSES_LOAN')

    def test_short_name_preserved_without_guessing_first_name(self):
        b=compiled(' ПРИМЕР ПЕРЕЙДЕТ В «Восток»?! Руководство заинтересовано в зимнем трансфере голландца. «Запад» готова отпустить полузащитника и рассчитывает выручить не менее 60 млн евро 📰 Example News')
        self.assertEqual(b.participants[0],'Пример');self.assertIn('60 млн евро',b.render())

    def test_scouting_not_match_result_or_bid(self):
        b=compiled('Harbor FC will be keeping a close eye on Northbridge forward Alex Example during their Champions League clash this evening. (Example News)')
        self.assertEqual(b.event_type,'TRANSFER_INTEREST');self.assertNotIn('Матчи',b.section)
        self.assertEqual(b.verified_facts[0].values[-1],'SCOUTING_NOT_BID')

    def test_no_personal_fee_instead_of_club_payment(self):
        b=compiled('«Восток» готов выложить 120 МЛН ЕВРО за Алекс Пример этой зимой! Он отклонил предложение о продлении контракта с «Запад». 📰 ExampleNews')
        fee=next(f for f in b.verified_facts if f.key=='reported_willingness_fee')
        self.assertIn('WILLINGNESS_NOT_BID_OR_SALARY',fee.values);self.assertIn('120',fee.values)

    def test_two_reports_same_transfer_composed(self):
        a=compiled('«Восток» готов выложить 120 МЛН ЕВРО за Алекс Пример этой зимой! Он отклонил предложение о продлении контракта с «Запад». 📰 ExampleNews')
        b=replace(a,event_id='1234567890abcdef',verified_facts=(replace(a.verified_facts[0],values=('Алекс Пример','Запад','Другой','INTEREST')),))
        b=replace(b,essential_facts=('transfer_subject',))
        kept,checks=final_digest_gate([a,b]);self.assertEqual(len(kept),1)
        self.assertEqual(checks[-1]['decision'],'MERGE')

    def test_conflicted_actor_cannot_reenter_via_third_report(self):
        a=compiled('«Восток» готов выложить 120 МЛН ЕВРО за Алекс Пример этой зимой! Он отклонил предложение о продлении контракта с «Запад». 📰 ExampleNews')
        b=replace(a,event_id='other',verified_facts=(replace(a.verified_facts[0],values=('Алекс Пример','Другой','Восток','INTEREST')),),essential_facts=('transfer_subject',))
        self.assertEqual(final_digest_gate([a,b,a])[0],[])

    def test_merged_key_does_not_bypass_history(self):
        f=VerifiedFact('fee',('60','EUR'),('https://example.test',),'fee',('fee','fee'))
        self.assertEqual(fact_signature(f),fact_signature(replace(f,key=('a'*16)+'_fee')))

    def test_missing_essential_fact_detected(self):
        b=match_bundle();self.assertIn('SOURCE_FACT_PRESERVATION',bundle_issues(replace(b,essential_facts=b.essential_facts+('absent',))))

    def test_invalid_model_response_fallback_no_retry(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример продлил контракт с «Востоком» до лета 2029 года.')
        calls=[]
        choices,trace=edit_batches([b],lambda p:(calls.append(p) or 'invented text'),100,lambda:0)
        self.assertEqual(len(calls),1);self.assertEqual(choices[b.event_id],0);self.assertTrue(trace[0]['issues'])

    def test_timeout_fallback_no_retry(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример продлил контракт с «Востоком» до лета 2029 года.')
        def timeout(_):raise TimeoutError()
        choices,trace=edit_batches([b],timeout,100,lambda:0)
        self.assertEqual(choices[b.event_id],0);self.assertEqual(trace[0]['issues'],['TimeoutError']);self.assertEqual(len(trace),1)

    def test_ceremony_presentation_is_not_attendance(self):
        b=compiled('Alex Example will not be presenting a trophy to any winners at the Ballon d’Or, as he has not been invited to do so. Also his attendance has not yet been confirmed.')
        self.assertEqual(b.verified_facts[0].values[-1],'ATTENDANCE_UNCONFIRMED')
        self.assertIn('вручать',b.render());self.assertIn('пока не подтверждено',b.render())

    def test_unprinted_source_detail_is_not_published_history(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример продлил контракт с «Востоком» до лета 2029 года.')
        rows=[{'source':{'canonical_url':b.source_refs[0]},'text':'Клуб объявил новость.'}]
        history,evidence=confirmed_published_history([b],rows)
        self.assertEqual(history,[]);self.assertFalse(evidence[0]['published_evidence'])

    def test_approved_wording_is_proven_history(self):
        b=match_bundle();rows=[{'source':{'canonical_url':b.source_refs[0]},'text':b.render()}]
        history,evidence=confirmed_published_history([b],rows)
        expected=tuple(f for f in b.verified_facts if f.formulations[0] in b.render())
        self.assertEqual(history[0].verified_facts,expected)
        self.assertTrue(all(r['published_evidence'] for r in evidence if r['fact']!='streak'))

    def test_emoji_subdivision_flag_does_not_erase_surname(self):
        b=compiled('\U000e0067\U000e0062\U000e007f ПРИМЕР ПЕРЕЙДЕТ В «Восток»?! Руководство заинтересовано в зимнем трансфере голландца. «Запад» готова отпустить полузащитника и рассчитывает выручить не менее 60 млн евро 📰 Example News')
        self.assertEqual(b.participants[0],'Пример')

    def test_metric_formulation_must_not_contradict_typed_value(self):
        for metric,unit in [('GOALS','TOUCHES'),('SHOTS','GOALS'),('XG','ASSISTS'),('BIG_CHANCES','SHOTS')]:
            self.assertTrue(StatisticalObservation(metric,'34',unit,'Example','season','https://example.test').issues())

    def test_stat_missing_period_or_source_is_rejected(self):
        s=StatisticalObservation('GOALS','7','GOALS','Example','season','https://example.test')
        self.assertEqual(s.issues(),[])
        self.assertTrue(replace(s,period='').issues());self.assertTrue(replace(s,source='').issues())

    def test_other_actors_fact_cannot_suppress_new_detail(self):
        b=compiled('ОФИЦИАЛЬНО: Алекс Пример — игрок «Восток». Контракт до лета 2028 года.')
        old=compiled('ОФИЦИАЛЬНО: Алекс Пример — игрок «Восток». Контракт до лета 2027 года.')
        other=compiled('ОФИЦИАЛЬНО: Борис Пример — игрок «Восток». Контракт до лета 2028 года.')
        self.assertEqual(final_digest_gate([b],[old,other])[0],[b])


if __name__=='__main__':unittest.main()
