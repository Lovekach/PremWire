"""Isolated candidate regressions; no sender, model or production state."""
from dataclasses import replace
from datetime import date
import unittest
from premwire.daily_fact_digest import editorial_value_gate,russian,merge_editorial_context
from premwire.daily_fact_patterns import paired_results
from tests.test_daily_edition_gate import compiled
from tests.test_daily_editorial_freshness import item

class CandidateTests(unittest.TestCase):
    def test_explicit_separator_preserves_hyphenated_club(self):
        b=compiled('Alex Example, UEFA Man of the Match for Paris Saint-Germain against Manchester City.')
        f=b.verified_facts[0]
        self.assertEqual(f.values[1],russian('Paris Saint-Germain'))
        self.assertEqual(f.values[2],russian('Manchester City'))

    def test_award_does_not_swallow_following_comment(self):
        b=compiled('Alex Example, UEFA Man of the Match for Real Madrid vs AS Roma. …after playing only 4 minutes until tonight for Real Madrid, entered the pitch and delivered a crucial goal.')
        self.assertEqual(b.verified_facts[0].values[2],russian('AS Roma'))
        self.assertNotIn('4 minutes',b.render())

    def test_multiple_hyphens_without_explicit_separator_are_ambiguous(self):
        self.assertIsNone(compiled('Alex Example, UEFA Man of the Match for Paris Saint-Germain-Saint-Etienne.'))

    def test_minor_award_is_context_not_standalone_filler(self):
        b=compiled('Alex Example, UEFA Man of the Match for Arsenal vs Lille.')
        kept,trace=editorial_value_gate([b],[])
        self.assertEqual(kept,[]);self.assertIn('WITHOUT_RESULT_CONTEXT',trace[0]['reason'])

    def test_unattributed_bare_close_appointment_not_future_appointment(self):
        bare=compiled('Пример близок к назначению в «Восток».')
        future=compiled('Фабрицио Романо: Алекс Пример будет назначен на пост нового главного тренера «Восток».')
        kept,_=editorial_value_gate([bare,future],[])
        self.assertEqual(kept,[future])

    def test_attributed_close_appointment_not_globally_banned(self):
        source=item('Пример близок к назначению в «Восток». 📰 Example News')
        b=compiled(source.text);b=replace(b,source_refs=(source.canonical_url,))
        self.assertEqual(editorial_value_gate([b],[source])[0],[b])

    def test_new_two_fixture_recap_is_generic_and_keeps_score_direction(self):
        raw='«Восток» проехалась катком по «Запад», а «Север» страдает на стадионе. Парни тренера разгромили соперника — 6:0. А вот приезд тренера не помог. Команда наставника терпит первое поражение в сезоне от «Юг» — 2:4.'
        self.assertEqual(paired_results(raw,russian),(('Восток','Запад','6:0'),('Юг','Север','4:2')))
        self.assertEqual(paired_results(raw.replace('2:4','4:2'),russian),())
        self.assertEqual(paired_results(raw+' Update: к перерыву счёт изменился.',russian),())

    def test_split_threshold_is_same_fact_not_another_stage(self):
        from premwire.daily_fact_digest import final_digest_gate
        old=compiled('Алекс Пример может покинуть «Восток» уже в январе, если клуб получит предложение на сумму более 100 млн фунтов. После встречи с агентами позиция клуба изменилась.')
        new=compiled('Фабрицио Романо: Алекс Пример может покинуть «Восток» в январе, если клуб получит крупное предложение. После встречи с агентами позиция изменилась: летом клуб отклонил предложения. «Восток» хочет получить за игрока более 100 млн фунтов.')
        self.assertEqual(new.participants,old.participants)
        self.assertEqual([f.values for f in new.verified_facts],[f.values for f in old.verified_facts])
        self.assertEqual(final_digest_gate([new],[old])[0],[])

    def test_split_threshold_for_other_club_cannot_fill_condition(self):
        self.assertIsNone(compiled('Алекс Пример может покинуть «Восток» в январе, если клуб получит крупное предложение. «Запад» хочет получить за игрока более 100 млн фунтов.'))

    def test_unbeaten_run_is_match_count_not_wins_or_icon_scores(self):
        b=compiled('«Восток» не проигрывает клубам АПЛ уже 12 матчей подряд в турнирах под эгидой УЕФА. 2–4 2–1 1–1')
        s=b.verified_facts[0].statistics[0]
        self.assertEqual((s.metric,s.value,s.unit),('MATCH_COUNT','12','MATCHES'))
        self.assertNotIn('побед',b.render());self.assertNotIn('2:4',b.render())

class ProductionSourceTests(unittest.TestCase):
    def test_current_report_precedes_unprinted_previous_day_event(self):
        from premwire.daily_fact_digest import editorial_freshness
        from premwire.models import IncomingItem
        from tests.test_daily_editorial_freshness import START,DAY
        from datetime import timedelta
        old=compiled('ОФИЦИАЛЬНО: Алекс Пример — новый главный тренер «Восток».')
        fresh=compiled('Борис Другой перенёс операцию на колене после травмы, полученной на тренировке.')
        midnight=START+timedelta(days=1)
        sources=[IncomingItem('test','1','ОФИЦИАЛЬНО: Алекс Пример — новый главный тренер «Восток».',old.source_refs[0],midnight-timedelta(hours=1)),
                 IncomingItem('test','2','Борис Другой перенёс операцию на колене после травмы, полученной на тренировке.','https://example.test/new',midnight+timedelta(hours=1))]
        fresh=replace(fresh,source_refs=(sources[1].canonical_url,))
        result,_=editorial_freshness([old,fresh],sources,DAY)
        self.assertEqual(result[0].event_id,fresh.event_id)
        self.assertLess(result[1].importance,result[0].importance)

    def test_fresh_russian_knee_surgery_no_invented_club_or_return_date(self):
        b=compiled('🤕🚨 Алекс Пример перенёс операцию на колене после травмы, полученной на тренировке.')
        self.assertEqual(b.event_type,'INJURY');self.assertEqual(b.participants,('Алекс Пример',))
        self.assertNotIn('2026',b.render());self.assertNotIn('месяц',b.render())

    def test_russian_result_binds_headline_club_and_competition_not_coach_identity(self):
        b=compiled('«Восток» вписал себя в историю! Команда тренера одержала победу над «Запад» в Лиге Европы со счётом 3:0. Далее обсуждают АПЛ.')
        self.assertEqual(b.event_type,'MATCH_RESULT')
        self.assertEqual(b.verified_facts[0].values,('Восток','Запад','3:0'))
        self.assertEqual(b.verified_facts[1].values,('EUROPA_LEAGUE',))
        self.assertIn('В Лиге Европы',b.render())
        self.assertIsNone(compiled('Команда тренера одержала победу над «Запад» в Лиге Европы со счётом 3:0.'))
        self.assertIsNone(compiled('«Восток» вписал себя в историю! Команда тренера одержала победу над «Запад» в Лиге Европы со счётом 3:0. Update: к перерыву счёт изменился.'))

    def test_player_contact_does_not_upgrade_to_free_transfer_or_signing(self):
        b=compiled('🚨 Фабрицио Романо: «Восток» связался с Алексом Примером, чтобы попытаться подписать футболиста в статусе свободного агента.')
        self.assertEqual(b.event_type,'TRANSFER_CONTACT')
        self.assertIn('CONTACT_NOT_AGREEMENT',b.verified_facts[0].values)
        self.assertNotIn('официально',b.render());self.assertNotIn('перешёл',b.render())

    def test_russian_verbal_contract_without_expiry_not_routine_quote(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        from tests.test_daily_editorial_freshness import START,END,DAY
        source=item('🚨‼️ Фабрицио Романо: «Восток» и Алекс Пример достигли устной договорённости о продлении контракта.')
        result=prepare_immutable_edition([source],START,END,None,edition_day=DAY)
        self.assertEqual(len(result['selected']),1)
        self.assertIn('VERBAL_AGREEMENT_NOT_SIGNED',result['selected'][0].verified_facts[0].values)
        self.assertNotIn('официально',result['body']);self.assertNotIn('2029',result['body'])

    def test_short_official_appointment_survives_real_filter(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        from tests.test_daily_editorial_freshness import START,END,DAY
        source=item('⚡️ОФИЦИАЛЬНО: Алекс Пример — новый главный тренер «Восток».')
        result=prepare_immutable_edition([source],START,END,None,edition_day=DAY)
        self.assertEqual(len(result['selected']),1)
        self.assertEqual(result['selected'][0].event_type,'APPOINTMENT')

    def test_russian_contract_agreement_not_quote_or_signed_contract(self):
        from premwire.daily_fact_e2e import prepare_immutable_edition
        from tests.test_daily_editorial_freshness import START,END,DAY
        source=item('🚨‼️ Фабрицио Романо: «Восток» и Алекс Пример достигли договорённости о продлении контракта до лета 2029 года.')
        result=prepare_immutable_edition([source],START,END,None,edition_day=DAY)
        self.assertEqual(len(result['selected']),1)
        self.assertEqual(result['selected'][0].event_type,'CONTRACT_AGREEMENT')
        self.assertIn('AGREEMENT_NOT_SIGNED',result['selected'][0].verified_facts[0].values)
        self.assertNotIn('официально',result['body'])

    def test_interest_asking_price_not_completed_transfer(self):
        b=compiled('«Восток» нацелился на Алекса Примера 💰 «Запад» готов продать полузащитника за 100 миллионов евро. 📰 Example News')
        self.assertEqual(b.event_type,'TRANSFER_INTEREST')
        self.assertEqual(b.verified_facts[-1].values[-1],'ASKING_NOT_AGREED')
        self.assertNotIn('перешёл',b.render())

    def test_financial_report_has_typed_scope_not_goal_numbers(self):
        from premwire.daily_fact_digest import bundle_issues
        b=compiled('Отчёт Независимого футбольного регулятора Англии (IFR): 94% клубов пяти высших дивизионов Англии несут убытки — 109 из 116 команд. 73 клуба не смогут продержаться более 90 ДНЕЙ без финансовой поддержки своих владельцев.')
        self.assertEqual(bundle_issues(b),[])
        self.assertEqual({s.metric for f in b.verified_facts for s in f.statistics},{'PERCENTAGE','CLUB_COUNT','DURATION'})
        self.assertNotIn('гол',b.render())

    def test_inconsistent_finance_count_rejected(self):
        self.assertIsNone(compiled('Отчёт Независимого футбольного регулятора Англии (IFR): 94% клубов пяти высших дивизионов Англии несут убытки — 9 из 116 команд.'))

    def test_club_career_record_not_all_time_competition_total(self):
        b=compiled('+1 рекорд Примера! Благодаря дублю в ворота «Запад» Алекс стал лучшим бомбардиром «Восток» в ЛЧ. Теперь у игрока 37 голов в 41-м матче.')
        self.assertEqual(b.event_type,'PLAYER_STATS')
        self.assertEqual({s.period for s in b.verified_facts[0].statistics},{'CLUB_CHAMPIONS_LEAGUE_CAREER'})
        self.assertIn('Восток',b.render())

    def test_empty_diagnostics_differentiate_network_parser_and_dedupe(self):
        from premwire.daily_text_digest import empty_edition_reason
        self.assertEqual(empty_edition_reason([],[],{}),'NO_SOURCE_NEWS')
        self.assertEqual(empty_edition_reason([],['timeout'],{}),'SOURCE_COVERAGE_INCOMPLETE')
        self.assertEqual(empty_edition_reason([1],[],{'unsupported':[{'reason':'UNSUPPORTED_SOURCE_RELATION_SCHEMA'}]}),'SOURCE_PARSER_COVERAGE_FAILURE')
        self.assertEqual(empty_edition_reason([1],[],{'unsupported':[{'reason':'EDITORIAL_PRIVATE_LIFESTYLE'}]}),'NO_EDITORIALLY_SAFE_EVENTS')
        self.assertEqual(empty_edition_reason([1],[],{'unsupported':[{'reason':'MALFORMED_SOURCE_RELATION_ValueError'}]}),'SOURCE_PROCESSING_FAILURE')
        self.assertEqual(empty_edition_reason([1],[],{'rejected':[{'reason':'MALFORMED_SOURCE_RELATION_ValueError'}]}),'SOURCE_PROCESSING_FAILURE')
        self.assertEqual(empty_edition_reason([1],[],{'final_digest_gate':[{'decision':'REJECT','reason':'EVENT_DEDUPE_CROSS_DAY_UNCHANGED_FACTS'}]}),'NO_NEW_EVENTS_AFTER_DEDUPE')

if __name__=='__main__':unittest.main()
