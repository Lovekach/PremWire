"""Offline regressions for opt-in collection budgets and reusable SOURCE forms."""
from dataclasses import replace
from datetime import datetime,timezone
import unittest
from unittest.mock import patch

from premwire.daily_fact_digest import bundle_issues,GOVERNANCE_SECTION,CONTRACTS_SECTION
from premwire.daily_fact_patterns import unsupported_reason
from premwire.daily_fact_patterns import paired_results
from premwire.daily_fact_digest import russian
from premwire.daily_fact_digest import final_digest_gate,VerifiedFact
from premwire.daily_fact_e2e import publication_adapter
from premwire.daily_fact_e2e import prepare_immutable_edition
from premwire.daily_text_digest import MOSCOW
from tests.test_daily_fact_digest import match_bundle,candidate
from premwire.collectors.x_rss import XFreeRssCollector
from premwire.collectors.base import CollectorError
from premwire.models import Source
from tests.test_daily_edition_gate import compiled


class NewSourceShapes(unittest.TestCase):
    def test_finance_report_separates_revenue_loss_and_owner_dependency(self):
        b=compiled("The Independent Football Regulator has released its 'State of the Game' report. Clubs brought in £9bn in revenue in 2025/26. And yet 94% of clubs still make losses. 32 clubs stated they couldn't survive 30 days without owner funding.")
        self.assertEqual(b.event_type,'FINANCIAL_REPORT');self.assertEqual(b.section,GOVERNANCE_SECTION)
        self.assertEqual(b.verified_facts[0].values[-1],'2025/26')
        self.assertIn('9 млрд фунтов',b.render());self.assertIn('94%',b.render());self.assertIn('30 дней',b.render())
        self.assertNotIn('гол',b.render())

    def test_reported_terms_not_actual_salary_or_transfer(self):
        text='Example News раскрыли ПЯТЬ требований, которые Алекс Пример и его окружение, предположительно, выдвинули «Восток»: • номер 77 • нанесение на джерси – EXAMPLE JR • семизначная годовая зарплата • тренировки только с первой командой • минимум 20 игровых минут в трех конкретных матчах «Восток» отклонил несколько требований и теперь ожидает, что 16-летний футболист покинет клуб'
        b=compiled(text);self.assertEqual(b.section,CONTRACTS_SECTION)
        self.assertIn('предположительно',b.render());self.assertIn('валюта не указана',b.render())
        self.assertNotIn('официально',b.render());self.assertNotIn('перешёл',b.render())

    def test_multi_period_stats_do_not_assign_historical_record_to_player(self):
        b=compiled('Алекс Пример — новый кумир? 5 голов и 3 ассиста в первых шести турах АПЛ. В сезоне 2010/11 Другой Игрок набрал 10 результативных действий (4+6).')
        self.assertEqual(bundle_issues(b),[]);self.assertIn('6 турах',b.render())
        self.assertNotIn('10',b.render());self.assertNotIn('Другой',b.render())
        self.assertEqual({s.period for s in b.verified_facts[0].statistics},{'FIRST_6_PREMIER_LEAGUE_ROUNDS'})

    def test_exclusion_reasons_are_specific(self):
        self.assertEqual(unsupported_reason('Команды с касаниями «Восток» (34 гола)'),'AMBIGUOUS_MIXED_METRIC_TABLE')
        self.assertEqual(unsupported_reason("Tonight's XI"),'UNRESOLVED_LINEUP_OR_YOUTH_MEMBERSHIP')

    def test_two_final_fixtures_do_not_share_score_or_winner(self):
        text='«Восток» продолжает доминацию, «Норт Юнайтед» уступает! Парни тренера разгромили на своём поле «Запад» — 3:0. А вот у «Юнайтед» всё хуже — 0:2 в гостях от «Юг».'
        self.assertEqual(paired_results(text,russian),(('Восток','Запад','3:0'),('Юг','Норт Юнайтед','2:0')))
        self.assertEqual(paired_results(text.replace('у «Юнайтед»','у «Другого»'),russian),())

    def test_announced_signing_does_not_invent_origin_or_official_proof(self):
        b=compiled('СВЕРШИЛОСЬ! Алекс Пример – игрок «Восток» Контракт до лета 2027 года')
        self.assertEqual(b.event_type,'TRANSFER_ANNOUNCEMENT');self.assertNotIn('официально',b.render())
        self.assertIn('2027',b.render())

    def test_changed_explicit_fixture_date_is_an_independent_match(self):
        b=match_bundle()
        def dated(stamp):
            f=VerifiedFact('match_date',(stamp,),b.source_refs,'explicit fixture date',('Матч '+stamp+'.',)*2)
            return replace(b,verified_facts=b.verified_facts+(f,))
        self.assertEqual(final_digest_gate([dated('2026-10-13')],[dated('2026-10-11')])[0],[dated('2026-10-13')])
        self.assertEqual(final_digest_gate([dated('2026-10-11')],[dated('2026-10-11')])[0],[])

    def test_publication_adapter_preserves_destination_without_a_sender(self):
        b=match_bundle();original=candidate(['Original SOURCE']).source
        originals=[replace(original,canonical_url=u,external_id=str(n)) for n,u in enumerate(b.source_refs)]
        result={'selected':[b],'body':b.render(),'choices':{}}
        adapted=publication_adapter(result,originals,'@original_destination')
        self.assertEqual(adapted['payload']['chat_id'],'@original_destination')
        self.assertEqual(adapted['stories'][0].text,b.render())
        self.assertEqual(adapted['represented_originals'],originals)

    def test_multitoken_reporter_name_not_cut_at_first_word(self):
        b=compiled('«Восток» заинтересован в подписании Примера 💰 Сделка оценивается в 40 млн евро. 📰 Example News')
        self.assertIn('Example News',b.render())

    def test_carryover_header_and_today_are_bound_to_their_own_dates(self):
        from datetime import date,timedelta
        c=candidate(['Richard Example: “Pablo Example has a tight hamstring, this is why he’s not available today”.'])
        b=compiled(c.source.text)
        inputs=[replace(c.source,canonical_url=u,published_at=datetime(2026,10,13,23,tzinfo=MOSCOW)) for u in b.source_refs]
        start=datetime(2026,10,13,20,56,tzinfo=MOSCOW);end=start+timedelta(hours=12)
        with patch('premwire.daily_fact_e2e.build_bundles',return_value=([b],{})):
            result=prepare_immutable_edition(inputs,start,end,None,edition_day=date(2026,10,14))
        self.assertIn('14 ОКТЯБРЯ',result['body']);self.assertIn('13 октября',result['body'])
        self.assertNotIn('сегодня',result['body']);self.assertIn('Pablo Example',result['body'])

    def test_expired_rss_budget_makes_no_request(self):
        s=Source('x','x','Example','example','https://x.com/example')
        c=XFreeRssCollector((s,),deadline=0)
        with patch('premwire.collectors.x_rss.get_text') as get:
            with self.assertRaises(CollectorError):c._feed(s)
            get.assert_not_called()

    def test_rss_timeout_is_clipped_to_remaining_budget(self):
        s=Source('x','x','Example','example','https://x.com/example')
        c=XFreeRssCollector((s,),mirrors=('https://example.test/{handle}',),deadline=11)
        with patch('premwire.collectors.x_rss.time.monotonic',return_value=10),patch('premwire.collectors.x_rss.get_text',side_effect=CollectorError('offline')) as get:
            with self.assertRaises(CollectorError):c._feed(s)
            self.assertEqual(get.call_args.kwargs['timeout'],1)

    def test_hung_source_does_not_hold_bounded_collection_shutdown(self):
        from premwire.daily_collection import collect_day
        from unittest.mock import MagicMock
        s=Source('tg','telegram','Example','example','https://t.me/example')
        pool=MagicMock();pool.submit.return_value.result.side_effect=TimeoutError()
        coverage=[]
        with patch('premwire.daily_collection.ThreadPoolExecutor',return_value=pool):
            items,warnings=collect_day((s,),{},datetime(2026,10,13,tzinfo=timezone.utc),datetime(2026,10,14,tzinfo=timezone.utc),budget_seconds=1,coverage=coverage,bounded=True)
        self.assertEqual(items,[]);self.assertIn('TimeoutError',warnings[0])
        pool.shutdown.assert_called_once_with(wait=False,cancel_futures=True)


if __name__=='__main__':unittest.main()
