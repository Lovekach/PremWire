"""Production orchestration acceptance with isolated SQLite and fake senders."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from premwire.config import load_config
from premwire.daily_text_digest import MOSCOW, run_daily, finalize_interrupted_health
from premwire.daily_fact_e2e import PublishedFactStore
from premwire.models import IncomingItem
from premwire.storage import Storage
from premwire.telegram_api import TelegramApiError

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT/'src/premwire').is_dir():
    ROOT = ROOT.parent  # private isolated acceptance copy, never runtime code
NOW = datetime(2026, 10, 14, 20, 56, tzinfo=MOSCOW)


class FixedDate(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ImmutableProductionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/settings.json').write_text('{"production":{"mode":"daily_digest"}}')
        database = self.root / 'data/source.sqlite3'
        Storage(database).initialise()
        base = load_config(ROOT)
        self.config = replace(base, storage=replace(base.storage, database=database),
                              publishing=replace(base.publishing, channel='@shadow', dry_run=False),
                              production=replace(base.production, enabled=True))
        self.items = [IncomingItem('telegram_mtransfers', '1',
                      'Liverpool beat Manchester City 3-0.', 'https://t.me/test/1', NOW-timedelta(hours=1))]
        self.api = Mock()
        self.api.send_text.return_value = 101

    def collect(self, *args, coverage, **kwargs):
        self.assertTrue(kwargs['bounded'])
        self.assertEqual(kwargs['budget_seconds'], 90)
        coverage.append(dict(source_id='telegram_mtransfers', status='MIDNIGHT_BOUNDARY_VERIFIED', count=len(self.items)))
        return self.items, []

    def run_batch(self, **kwargs):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('premwire.daily_text_digest.datetime', FixedDate))
            stack.enter_context(patch('premwire.daily_text_digest.load_config', return_value=self.config))
            stack.enter_context(patch('premwire.daily_text_digest.load_env', return_value={'TELEGRAM_BOT_TOKEN': 'synthetic'}))
            stack.enter_context(patch('premwire.daily_text_digest.owner_paused', return_value=False))
            stack.enter_context(patch('premwire.daily_text_digest.suppression_texts', return_value=[]))
            stack.enter_context(patch('premwire.daily_text_digest.collect_day', side_effect=self.collect))
            stack.enter_context(patch('premwire.daily_text_digest.TelegramBotApi', return_value=self.api))
            stack.enter_context(patch('premwire.daily_text_digest.BatchOllama', side_effect=AssertionError('No model')))
            stack.enter_context(patch('premwire.daily_strategy.cached_rewrite', side_effect=AssertionError('No legacy rewrite')))
            stack.enter_context(patch('premwire.daily_strategy.publish_secondary', return_value={'X': 'DISABLED', 'Dzen': 'DISABLED'}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            return run_daily(self.root, **kwargs)

    def health(self):
        return json.loads((self.root/'data/daily-health.json').read_text(encoding='utf-8'))

    def receipt(self):
        with contextlib.closing(sqlite3.connect(self.root/'data/daily-text-digest.sqlite3')) as db:
            return db.execute('SELECT state,body,message_id FROM daily_digest WHERE day=?', (NOW.date().isoformat(),)).fetchone()

    def test_single_live_route_no_model_and_second_launch_cannot_send(self):
        self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.receipt()[0], 'PUBLISHED')
        self.assertIn('3:0', self.receipt()[1])
        health = self.health()
        self.assertEqual(health['llm_calls'], 0)
        self.assertEqual(health['published_events'], 1)
        self.assertEqual(health['status'], 'PARTIAL_SUCCESS')
        self.assertEqual(health['match_coverage']['status'], 'COVERAGE_UNKNOWN')
        self.assertFalse(health['running'])
        self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.api.send_text.call_count, 1)

    def provider_item(self):
        from tests.test_football_data import raw
        from premwire.football_data import source_item
        row=raw()
        row.update(utcDate=(NOW-timedelta(hours=4)).isoformat(),lastUpdated=(NOW-timedelta(minutes=8)).isoformat())
        row['score']['fullTime']=dict(home=5,away=0);row['score']['winner']='HOME_TEAM'
        return source_item(row,(NOW-timedelta(minutes=5)).isoformat())

    def test_attribution_read_failure_retains_inline_credit(self):
        from premwire.daily_footer import CREDIT
        self.items=[self.provider_item()]
        self.api.call.side_effect=TelegramApiError('Synthetic read unavailable')
        self.assertEqual(self.run_batch(confirm=True),0)
        self.assertEqual(self.receipt()[1].count(CREDIT),1)
        self.assertEqual(self.health()['attribution']['placement'],'INLINE')
        self.assertEqual(self.health()['attribution_check_failure'],'TelegramApiError')
        self.assertEqual(self.api.send_text.call_count,1)

    def test_verified_about_credit_removes_only_inline_footer(self):
        from premwire.daily_footer import CREDIT
        self.items=[self.provider_item()]
        self.api.call.return_value=dict(type='channel',username='shadow',description='Original\n\n'+CREDIT)
        self.assertEqual(self.run_batch(confirm=True),0)
        body=self.receipt()[1]
        self.assertNotIn(CREDIT,body);self.assertTrue(body.endswith('\n\n@PremWire'))
        self.assertIn('5:0',body)
        self.assertEqual(self.health()['attribution']['placement'],'CHANNEL_DESCRIPTION')
        self.assertEqual(self.health()['essential_facts_lost'],0)
        self.assertEqual(self.run_batch(confirm=True),0)
        self.assertEqual(self.api.send_text.call_count,1)

    def test_dry_run_same_route_never_sends_or_creates_production_ledger(self):
        self.assertEqual(self.run_batch(), 0)
        self.api.send_text.assert_not_called()
        self.assertFalse((self.root/'data/daily-text-digest.sqlite3').exists())
        self.assertFalse((self.root/'data/daily-text-digest.lock').exists())
        report = next((self.root/'artifacts/daily-text-digest').glob('*/report.json'))
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['essential_facts_lost'], 0)

    def test_network_outage_not_terminal_no_news(self):
        self.items = []
        self.collect = lambda *args, **kwargs: ([], ['SOURCE_UNAVAILABLE:TimeoutError'])
        self.assertEqual(self.run_batch(confirm=True), 1)
        self.assertEqual(self.receipt()[0], 'FAILED_PRE_SEND')
        self.assertEqual(self.health()['status'], 'FAILED')
        self.api.send_text.assert_not_called()

    def test_ambiguous_send_keeps_sending_forever_without_second_post(self):
        self.api.send_text.side_effect = TelegramApiError('Synthetic network ambiguity')
        self.assertEqual(self.run_batch(confirm=True), 1)
        self.assertEqual(self.receipt()[0], 'SENDING')
        self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.api.send_text.call_count, 1)

    def test_explicit_flood_rejection_allows_one_bounded_retry(self):
        self.api.send_text.side_effect = [TelegramApiError('Rejected', rejected=True, retry_after=0), 101]
        self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.api.send_text.call_count, 2)
        self.assertEqual(self.receipt()[0], 'PUBLISHED')

    def test_rejection_retry_does_not_wait_unbounded(self):
        self.api.send_text.side_effect = TelegramApiError('Rejected', rejected=True, retry_after=60)
        self.assertEqual(self.run_batch(confirm=True), 1)
        self.assertEqual(self.api.send_text.call_count, 1)
        self.assertEqual(self.receipt()[0], 'FAILED_PRE_SEND')

    def test_crash_after_receipt_reconciles_sources_without_resend(self):
        with patch.object(PublishedFactStore, 'record', side_effect=RuntimeError('Crash after receipt')):
            self.assertEqual(self.run_batch(confirm=True), 1)
        self.assertEqual(self.receipt()[0], 'PUBLISHED')
        self.assertEqual(self.health()['status'], 'PARTIAL_SUCCESS')
        self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.api.send_text.call_count, 1)
        with contextlib.closing(sqlite3.connect(self.root/'data/daily-text-digest.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT included_day FROM daily_sources').fetchone()[0], NOW.date().isoformat())

    def test_broken_bundle_isolated_while_independent_event_survives(self):
        from premwire.daily_fact_digest import compile_bundle
        self.items.append(replace(self.items[0], external_id='2', canonical_url='https://t.me/test/2',
                                  text='Chelsea beat Arsenal 2-0.'))
        def compile_one(candidate, *args, **kwargs):
            if candidate.source.external_id == '1':
                raise ValueError('Malformed SOURCE')
            return compile_bundle(candidate, *args, **kwargs)
        with patch('premwire.daily_fact_digest.compile_bundle', side_effect=compile_one):
            self.assertEqual(self.run_batch(confirm=True), 0)
        self.assertEqual(self.health()['accepted_count'], 1)
        self.assertIn('MALFORMED_SOURCE_RELATION_ValueError', self.health()['exclusion_reasons'])
        self.assertNotIn('3:0', self.receipt()[1])

    def test_supervisor_post_check_does_not_reset_confirmed_receipt(self):
        self.assertEqual(self.run_batch(confirm=True), 0)
        health = self.health()
        health['running'] = True
        (self.root/'data/daily-health.json').write_text(json.dumps(health), encoding='utf-8')
        finalize_interrupted_health(self.root, NOW-timedelta(seconds=1), 1)
        self.assertEqual(self.health()['status'], 'PARTIAL_SUCCESS')
        self.assertEqual(self.health()['telegram_status'], 'PUBLISHED')
        self.assertEqual(self.receipt()[0], 'PUBLISHED')
        self.assertEqual(self.api.send_text.call_count, 1)

    def test_failed_factual_gate_never_falls_back_to_legacy(self):
        with patch('premwire.daily_fact_e2e.prepare_immutable_edition', side_effect=ValueError('Unsafe facts')):
            self.assertEqual(self.run_batch(confirm=True), 1)
        self.assertEqual(self.health()['status'], 'SKIPPED_UNSAFE')
        self.assertEqual(self.receipt()[0], 'FAILED_PRE_SEND')
        self.api.send_text.assert_not_called()

    def test_supervisor_failure_before_send_is_recorded_without_ledger_reset(self):
        self.items = []
        self.collect = lambda *args, **kwargs: ([], ['UNAVAILABLE'])
        self.assertEqual(self.run_batch(confirm=True), 1)
        health = self.health(); health['running'] = True
        (self.root/'data/daily-health.json').write_text(json.dumps(health), encoding='utf-8')
        finalize_interrupted_health(self.root, NOW-timedelta(seconds=1), 1)
        self.assertEqual(self.health()['status'], 'FAILED')
        self.assertEqual(self.receipt()[0], 'FAILED_PRE_SEND')
        self.api.send_text.assert_not_called()

    def test_real_transport_metadata_requires_explicit_telegram_rejection(self):
        from premwire.telegram_api import TelegramBotApi
        from urllib.error import HTTPError
        payload = io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":1}}')
        with patch('premwire.telegram_api.urlopen', side_effect=HTTPError('https://example.test',429,'rejected',{},payload)):
            with self.assertRaises(TelegramApiError) as error:
                TelegramBotApi('synthetic').send_text('@shadow','Checked text')
        self.assertTrue(error.exception.rejected)
        self.assertEqual(error.exception.retry_after, 1)
        payload = io.BytesIO(b'{"ok":false,"error_code":500}')
        with patch('premwire.telegram_api.urlopen', side_effect=HTTPError('https://example.test',500,'ambiguous',{},payload)):
            with self.assertRaises(TelegramApiError) as error:
                TelegramBotApi('synthetic').send_text('@shadow','Checked text')
        self.assertFalse(error.exception.rejected)


if __name__ == '__main__':
    unittest.main()
