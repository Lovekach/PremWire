from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from premwire.daily_strategy import (DailyState, accumulated_items, cached_rewrite,
    merge_source_context, source_key, publish_secondary, x_digest, adapt_telegram)
from premwire.daily_text_digest import (MOSCOW, DigestStory, DigestLedger, SECTIONS,
    day_window, publish_prepared, render_digest, collapse, telegram_length)
from premwire.models import IncomingItem
from premwire.config import load_config
from premwire.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 21, tzinfo=MOSCOW)

def item(text='Ливерпуль выиграл матч со счётом 2:0.', number=1, at=NOW):
    return IncomingItem('test', str(number), text, f'https://example.test/{number}', at)

def story(source=None, text=None, priority=90):
    source = source or item()
    return DigestStory(source, text or source.text, SECTIONS[0], 90, priority)

class DailyStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = DailyState(self.root/'daily.sqlite3')
    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_journal_retains_cross_source_duplicates_before_collapse(self):
        a, b = item(), item(number=2)
        self.state.accumulate([a, b, a])
        self.assertEqual(len(self.state.items()), 2)

    def test_source_edit_invalidates_cached_rewrite(self):
        a = item()
        self.state.save_story(story(a))
        self.assertIsNotNone(self.state.cached(a))
        edit = replace(a, text='Ливерпуль выиграл матч со счётом 3:0.')
        self.assertNotEqual(source_key(a), source_key(edit))
        self.assertIsNone(self.state.cached(edit))

    def test_cached_factual_error_is_rejected(self):
        a = item()
        self.state.save_story(story(a, 'Ливерпуль выиграл матч со счётом 8:0.'))
        self.assertIsNone(self.state.cached(a))

    def test_cached_stories_do_not_call_llm(self):
        a = item()
        self.state.save_story(story(a))
        rewrite = Mock(side_effect=AssertionError('LLM should be asleep'))
        result, rejected = cached_rewrite(self.state, None, [a], 0, rewrite)
        self.assertEqual(len(result), 1)
        self.assertFalse(rejected)
        rewrite.assert_not_called()

    def test_each_generated_story_has_a_trace_record(self):
        import time
        editor=Mock()
        a,b=item(),item(number=2)
        def rewrite(ed,items,deadline):
            ed.daily_trace=[{'response':items[0].text,'issues':[]}]
            return [story(items[0])],[]
        output,rejected=cached_rewrite(self.state,editor,[a,b],time.monotonic()+60,rewrite)
        self.assertEqual(len(output),2)
        self.assertEqual([entry['url'] for entry in editor.daily_trace],[a.canonical_url,b.canonical_url])

    def test_day_window_uses_moscow_not_utc_day(self):
        start, end = day_window(datetime(2026,10,10,22,tzinfo=timezone.utc))
        self.assertEqual(start.date().isoformat(), '2026-10-11')

    def test_accumulation_excludes_other_day_and_future(self):
        config = load_config(ROOT)
        db = self.root/'existing.sqlite3'
        Storage(db).initialise()
        config = replace(config, storage=replace(config.storage, database=db))
        self.state.accumulate([item(number=1), item(number=2,at=NOW-timedelta(days=1)),
                               item(number=3,at=NOW+timedelta(hours=4))])
        start,end=day_window(NOW)
        found=accumulated_items(self.state,config,start,end)
        self.assertEqual([i.external_id for i in found], ['1'])

    def test_included_sources_are_not_candidates_again(self):
        a,b=item(),item(number=2)
        self.state.accumulate([a,b])
        self.state.mark_included(NOW.date(),[story(a)],[a,b])
        self.assertEqual(self.state.items(), [])

    def test_manual_rejection_cannot_be_bypassed_by_source_journal(self):
        config=load_config(ROOT)
        db=self.root/'existing.sqlite3'
        storage=Storage(db)
        storage.initialise()
        a=item()
        outcome=storage.ingest(a)
        with storage.connect() as connection:
            connection.execute("UPDATE items SET state='rejected_manual' WHERE id=?",(outcome.item_id,))
        config=replace(config,storage=replace(config.storage,database=db))
        self.state.accumulate([a])
        start,end=day_window(NOW)
        self.assertEqual(accumulated_items(self.state,config,start,end),[])

    def test_frozen_daily_edition_cannot_be_replaced(self):
        a=item()
        self.state.freeze(NOW.date(),'first',self.root,[story(a)],[a])
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.freeze(NOW.date(),'second',self.root,[story(a)],[a])
        self.assertEqual(self.state.db.execute('SELECT full_body FROM daily_editions').fetchone()[0],'first')

    def test_each_platform_has_one_durable_delivery_claim(self):
        self.assertTrue(self.state.delivery_claim(NOW.date(),'X'))
        self.assertFalse(self.state.delivery_claim(NOW.date(),'X'))
        self.assertTrue(self.state.delivery_claim(NOW.date(),'Dzen'))
        self.state.delivery_result(NOW.date(),'X','FAILED_OR_AMBIGUOUS')
        self.assertFalse(self.state.delivery_claim(NOW.date(),'X'))

    def test_telegram_ambiguous_send_is_never_repeated(self):
        ledger=DigestLedger(self.root/'daily.sqlite3')
        try:
            self.assertTrue(ledger.claim(NOW.date().isoformat()))
            api=Mock()
            api.send_text.side_effect=TimeoutError
            with self.assertRaises(TimeoutError):
                publish_prepared(ledger,NOW.date(),'body',api,'test',now=NOW)
            self.assertFalse(ledger.claim(NOW.date().isoformat()))
            self.assertEqual(api.send_text.call_count,1)
        finally:
            ledger.db.close()

    def test_full_version_is_not_cut_to_telegram_limit(self):
        sources=[story(item('Клуб '+str(i)+' подписал контракт до 2030 года.',i), 'Детали события '+str(i)+': '+('Подробный факт. '*30)) for i in range(25)]
        with patch('premwire.daily_text_digest.same_event',return_value=False):
            full,selected=render_digest(sources,NOW.date(),limit=None)
            short,small=render_digest(sources,NOW.date())
        self.assertEqual(len(selected),25)
        self.assertGreater(telegram_length(full),4096)
        self.assertLessEqual(telegram_length(short),3900)
        self.assertLess(len(small),len(selected))

    def test_empty_day_has_no_digest_or_x_post(self):
        self.assertEqual(render_digest([],NOW.date()),('',[]))
        self.assertEqual(x_digest([],NOW.date(),275),'')

    def test_merge_keeps_supplemental_fee_without_old_status(self):
        old=item('Liverpool opened talks. Fee: 50 million euros.',1,NOW-timedelta(hours=5))
        new=item('Liverpool officially signed the player.',2)
        merged=merge_source_context(new,[old,new])
        self.assertIn('50 million',merged.text)
        self.assertNotIn('opened talks',merged.text)

    def test_new_fee_overrides_old_fee(self):
        old=item('Fee: 50 million euros.',1,NOW-timedelta(hours=5))
        new=item('Official signing. Fee: 60 million euros.',2)
        self.assertEqual(merge_source_context(new,[old,new]),new)

    def test_x_adapter_retains_complete_fact_and_length(self):
        result=x_digest([story()],NOW.date(),275)
        self.assertIn('2:0.',result)
        self.assertLessEqual(telegram_length(result),275)

    def test_daily_editorial_worker_cannot_run_old_pipeline(self):
        from premwire.text_production import editorial_publish_once
        config=load_config(ROOT)
        storage=Mock()
        with self.assertRaisesRegex(RuntimeError,'CONTINUOUS_PRODUCTION_DISABLED'):
            editorial_publish_once(config,{},storage)
        storage.suppress_stale_production_items.assert_not_called()

    def test_preparation_handles_coverage_datetimes_without_send_or_llm(self):
        import contextlib,io
        from premwire.daily_text_digest import run_daily
        config=load_config(ROOT)
        db=self.root/'source.sqlite3'
        Storage(db).initialise()
        config=replace(config,storage=replace(config.storage,database=db))
        def collect(*args,coverage,**kwargs):
            coverage.append({'source_id':'test','earliest_retained':NOW})
            return [],[]
        with patch('premwire.daily_text_digest.load_config',return_value=config), \
             patch('premwire.daily_text_digest.load_env',return_value={}), \
             patch('premwire.daily_text_digest.collect_day',side_effect=collect), \
             patch('premwire.football_data.collect_optional',side_effect=lambda cache,start,end:([],None,end)), \
             patch('premwire.daily_text_digest.TelegramBotApi',side_effect=AssertionError('No send')), \
             patch('premwire.daily_text_digest.BatchOllama',side_effect=AssertionError('No LLM')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily(self.root,prepare=True),0)
        self.assertEqual(len(list((self.root/'artifacts/daily-text-digest').glob('*/report.json'))),1)

    def test_secondary_adapters_publish_once_and_dzen_gets_full_body(self):
        config=load_config(ROOT)
        config=replace(config, x_publishing=replace(config.x_publishing,enabled=True),
                       dzen_digest=replace(config.dzen_digest,enabled=True))
        browser=Mock()
        browser.post.return_value='https://x.com/PremWire/status/123'
        xcontext=Mock()
        xcontext.__enter__=Mock(return_value=browser)
        xcontext.__exit__=Mock(return_value=False)
        dcontext=Mock()
        dcontext.__enter__=Mock(return_value=Mock())
        dcontext.__exit__=Mock(return_value=False)
        with patch('premwire.daily_text_digest.publication_window',return_value=True), \
             patch('premwire.daily_text_digest.owner_paused',return_value=False), \
             patch('premwire.x_browser.XBrowserSession',return_value=xcontext), \
             patch('premwire.dzen_digest_service._default_browser_factory',return_value=lambda:dcontext), \
             patch('premwire.dzen_publisher.publish_or_reconcile_dzen_article',return_value=Mock(article_url='https://dzen.ru/a/123')) as dsend, \
             patch('premwire.daily_strategy.ProcessLock') as lock:
            lock.return_value.acquire.return_value=True
            first=publish_secondary(self.state,config,NOW.date(),'FULL TEXT',[story()])
            second=publish_secondary(self.state,config,NOW.date(),'FULL TEXT',[story()])
        self.assertEqual(first,{'X':'PUBLISHED','Dzen':'PUBLISHED'})
        self.assertEqual(second['X'],'ALREADY_HANDLED_OR_AMBIGUOUS')
        browser.post.assert_called_once()
        self.assertEqual(dsend.call_args.args[1].body,'FULL TEXT')
        self.assertEqual(dsend.call_count,1)

    def test_missing_x_login_does_not_prevent_dzen(self):
        config=load_config(ROOT)
        config=replace(config, x_publishing=replace(config.x_publishing,enabled=True),
                       dzen_digest=replace(config.dzen_digest,enabled=True))
        dcontext=Mock()
        dcontext.__enter__=Mock(return_value=Mock())
        dcontext.__exit__=Mock(return_value=False)
        with patch('premwire.daily_text_digest.publication_window',return_value=True), \
             patch('premwire.daily_text_digest.owner_paused',return_value=False), \
             patch('premwire.x_browser.XBrowserSession',side_effect=RuntimeError('auth')), \
             patch('premwire.dzen_digest_service._default_browser_factory',return_value=lambda:dcontext), \
             patch('premwire.dzen_publisher.publish_or_reconcile_dzen_article',return_value=Mock(article_url='https://dzen.ru/a/123')), \
             patch('premwire.daily_strategy.ProcessLock') as lock:
            lock.return_value.acquire.return_value=True
            result=publish_secondary(self.state,config,NOW.date(),'FULL TEXT',[story()])
        self.assertEqual(result['X'],'RuntimeError')
        self.assertEqual(result['Dzen'],'PUBLISHED')

    def test_collector_never_calls_editor_or_publisher(self):
        from premwire.daily_strategy import collect_light
        config=load_config(ROOT)
        config=replace(config, production=replace(config.production,enabled=True))
        config=replace(config, storage=replace(config.storage,database=self.root/'storage.sqlite3'))
        output=Mock(collected=3,new=2,updated=1,warnings=())
        with patch('premwire.daily_text_digest.daily_mode',return_value=True), \
             patch('premwire.daily_strategy.load_config',return_value=config), \
             patch('premwire.daily_strategy.load_env',return_value={}), \
             patch('premwire.text_production.ingest_poll',return_value=output) as poll, \
             patch('premwire.daily_strategy.time.sleep'), \
             patch('premwire.daily_text_digest.OllamaTextEditor',side_effect=AssertionError('LLM')), \
             patch('premwire.daily_text_digest.TelegramBotApi',side_effect=AssertionError('send')):
            self.assertEqual(collect_light(self.root),0)
        self.assertEqual(poll.call_count,2)
        self.assertIn('observer',poll.call_args.kwargs)

if __name__=='__main__':
    unittest.main()
