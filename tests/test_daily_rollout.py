"""Synthetic offline acceptance of the single daily receipt contract."""
from dataclasses import replace
from datetime import date,datetime
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from premwire.daily_text_digest import DigestLedger,publish_prepared,MOSCOW
from premwire.daily_fact_e2e import PublishedFactStore,prepare_immutable_edition,BoundedStyleChat
from premwire.config import load_config
from premwire.daily_fact_digest import bundle_issues
from tests.test_daily_fact_digest import match_bundle,candidate
from tests.test_daily_edition_gate import compiled


class RolloutTests(unittest.TestCase):
    def test_optional_model_has_local_budget_and_no_retry(self):
        config=load_config(Path(__file__).resolve().parents[1])
        chat=BoundedStyleChat(config.editor)
        self.assertEqual(chat.editor.config.timeout_seconds,12)
        self.assertEqual(chat.editor.config.context_length,2048)
        self.assertEqual(chat.editor.config.simple_num_predict,128)
        with patch.object(chat.editor,'_request',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):chat('[]')
        self.assertEqual(chat.calls,1)
        with self.assertRaises(ValueError):chat('[]')
        self.assertEqual(chat.metrics['error'],'TimeoutError')

    def test_optional_model_never_sends_source_to_remote(self):
        config=load_config(Path(__file__).resolve().parents[1])
        with self.assertRaises(ValueError):BoundedStyleChat(replace(config.editor,base_url='https://example.test'))

    def test_full_preparation_without_qwen_and_timeout_fallback(self):
        b=match_bundle();source=candidate(['Source']).source
        inputs=[replace(source,canonical_url=url) for url in b.source_refs]
        from datetime import timedelta
        start=datetime(2026,10,11,tzinfo=MOSCOW);end=start+timedelta(days=1)
        checked=dict(essential_facts_lost=0,distorted_events=0,provenance_issues=[],essential_facts_checked=3)
        with patch('premwire.daily_fact_e2e.build_bundles',return_value=([b],{})),patch('premwire.daily_fact_e2e.audit_digest',return_value=checked):
            a=prepare_immutable_edition(inputs,start,end,None)
            calls=[]
            def failed(prompt):calls.append(prompt);raise TimeoutError()
            c=prepare_immutable_edition(inputs,start,end,None,style_chat=failed)
        self.assertEqual(a['body'],c['body']);self.assertEqual(len(calls),1)
        self.assertEqual(c['trace'][0]['issues'],['TimeoutError'])

    def test_receipt_required_and_round_trip_not_preview_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=DigestLedger(Path(tmp)/'shadow.sqlite3');store=PublishedFactStore(ledger.db)
            try:
                b=match_bundle();day='2026-10-11';body=b.render()
                ledger.claim(day)
                for state in ('STARTED','SENDING'):
                    ledger.state(day,state,body)
                    with self.assertRaises(ValueError):store.record(day,body,[b],'revision')
                self.assertEqual(store.history('2026-10-12'),[])
                ledger.state(day,'PUBLISHED',body,123)
                store.record(day,body,[b],'revision');store.record(day,body,[b],'revision')
                self.assertEqual(store.history('2026-10-12'),[b])
                self.assertEqual(store.history(day),[])
                self.assertFalse(ledger.claim(day))
                with self.assertRaises(ValueError):store.record(day,body,[b],'different revision')
            finally:ledger.db.close()

    def test_ambiguous_send_not_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=DigestLedger(Path(tmp)/'shadow.sqlite3')
            class Failed:
                calls=0
                def send_text(self,*_):self.calls+=1;raise TimeoutError()
            try:
                day=date(2026,10,11);ledger.claim(str(day));api=Failed()
                now=datetime(2026,10,11,20,57,tzinfo=MOSCOW)
                with self.assertRaises(TimeoutError):publish_prepared(ledger,day,'Проверенный факт',api,'@shadow',now=now)
                self.assertEqual(ledger.get_state(str(day)),'SENDING');self.assertFalse(ledger.claim(str(day)))
                with self.assertRaises(RuntimeError):publish_prepared(ledger,day,'Проверенный факт',api,'@shadow',now=now)
                self.assertEqual(api.calls,1)
            finally:ledger.db.close()

    def test_crash_after_receipt_retains_only_confirmed_staged_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=DigestLedger(Path(tmp)/'shadow.sqlite3');store=PublishedFactStore(ledger.db)
            try:
                b=match_bundle();day='2026-10-11';ledger.claim(day)
                store.stage(day,b.render(),[b],'revision')
                ledger.state(day,'SENDING',b.render())
                self.assertEqual(store.history('2026-10-12'),[])
                ledger.state(day,'PUBLISHED',b.render(),123)
                # Simulate termination before store.record: no send/recovery IO.
                self.assertEqual(store.history('2026-10-12'),[b])
                ledger.state(day,'PUBLISHED','Changed receipt',123)
                with self.assertRaises(ValueError):store.history('2026-10-12')
            finally:ledger.db.close()

    def test_semantic_wording_mismatch_not_digit_match(self):
        b=compiled('Alex Example reaches 7 Premier League goals in 8 games.')
        f=replace(b.verified_facts[0],formulations=('Alex Example: 7 касаний в 8 матчах.',)*2)
        self.assertIn('STAT_METRIC_AND_UNIT',bundle_issues(replace(b,verified_facts=(f,))))

    def test_russian_season_stat_retains_typed_goals_and_assists(self):
        b=compiled('В текущем сезоне у Примера уже 4 гола и 2 ассиста в АПЛ.')
        self.assertEqual(bundle_issues(b),[])
        self.assertEqual([s.metric for s in b.verified_facts[0].statistics],['GOALS','ASSISTS'])

    def test_generic_price_is_not_payment_or_personal_salary(self):
        b=compiled('Harbor FC will need to pay at least £50m to tempt Northbridge into selling midfielder Alex Example. (Example News)')
        self.assertEqual(b.verified_facts[0].values[-1],'REPORTED_ASKING_PRICE_NOT_DEAL')
        self.assertIn('не менее 50 млн фунтов',b.render());self.assertNotIn('перешёл',b.render())

    def test_generic_january_target_preserves_seller_and_player(self):
        b=compiled('Harbor FC have identified Northbridge midfielder Alex Example as a leading January target. (Example News)')
        self.assertEqual(b.participants,('Alex Example','Northbridge','Harbor FC'))
        self.assertEqual(b.verified_facts[0].values[-1],'REPORTED_JANUARY_TARGET_NOT_DEAL')


if __name__=='__main__':unittest.main()
