"""Generic SOURCE relations and independent source/event-date regression cases."""
from dataclasses import replace
from datetime import datetime,date,timedelta
import unittest
from premwire.daily_text_digest import MOSCOW,filter_items
from premwire.daily_fact_e2e import prepare_immutable_edition
from premwire.daily_fact_digest import editorial_freshness,final_digest_gate,bundle_issues,CARRY_SECTION,merge_editorial_context,compile_bundle
from premwire.daily_fact_shadow import FactBundle,Fact
from premwire.models import IncomingItem
from tests.test_daily_edition_gate import compiled

DAY=date(2026,10,14)
START=datetime(2026,10,13,tzinfo=MOSCOW)
END=datetime(2026,10,14,20,56,tzinfo=MOSCOW)
def item(text,n=1,at=None):
    return IncomingItem('telegram_mtransfers',str(n),text,f'https://example.test/{n}',at or END-timedelta(hours=1))


class EditorialFreshnessTests(unittest.TestCase):
    def test_consider_offer_not_watch_ad_and_real_watch_still_blocked(self):
        news=item('«Челси» готов предложить 30 млн евро. «Барселона» готова рассмотреть предложения о продаже игрока.')
        accepted,rejected=filter_items([news],START,END)
        self.assertNotIn('DAILY_PROMOTION_OR_FAN_CAMPAIGN',[r['reason'] for r in rejected])
        ad=item('«Челси» сыграет матч. Смотреть трансляцию бесплатно по ссылке!',2)
        self.assertEqual(filter_items([ad],START,END)[0],[])

    def test_old_interest_excluded_not_classified_duplicate(self):
        old=item('«Ливерпуль» заинтересован в подписании Примера 💰 Сделка оценивается в 40 млн евро. 📰 Example News',at=START+timedelta(hours=22))
        result=prepare_immutable_edition([old],START,END,None,edition_day=DAY)
        self.assertEqual(result['body'],'')
        self.assertEqual(result['audit']['editorial_freshness'][0]['reason'],'EDITORIAL_OLD_RUMOUR_NO_CURRENT_DEVELOPMENT')

    def test_today_repeated_rumour_not_new_due_to_new_url_or_timestamp(self):
        text='«Ливерпуль» заинтересован в подписании Примера 💰 Сделка оценивается в 40 млн евро. 📰 Example News'
        previous=prepare_immutable_edition([item(text)],START,END,None,edition_day=DAY)
        repeated=prepare_immutable_edition([item(text,2)],START,END,None,history=previous['selected'],edition_day=DAY)
        self.assertEqual(repeated['selected'],[])

    def test_contact_does_not_become_bid_or_full_name(self):
        result=prepare_immutable_edition([item('«Барселона» запросила информацию о Примере. Клуб продолжит следить за полузащитником.')],START,END,None,edition_day=DAY)
        self.assertEqual(result['selected'][0].event_type,'TRANSFER_CONTACT')
        self.assertIn('запросила информацию о Примере',result['body'])
        self.assertNotIn('Алекс',result['body'])

    def test_future_appointment_not_official_completed(self):
        b=compiled('Фабрицио Романо: Алекс Пример будет назначен на пост нового главного тренера «Восток».')
        self.assertEqual(b.event_type,'APPOINTMENT_EXPECTED')
        self.assertEqual(b.participants,('Алекс Пример','Восток'))
        self.assertNotIn('стал главным тренером',b.render())

    def test_official_suspension_count_fixture_and_category(self):
        b=compiled('ОФИЦИАЛЬНО: Пример дисквалифицирован на 4 матча и пропустит дерби.')
        self.assertEqual(bundle_issues(b),[])
        self.assertEqual(b.verified_facts[0].values,('Пример','4','дерби','OFFICIAL'))

    def test_yesterday_red_and_reported_absence_not_invented_official_or_today(self):
        result=prepare_immutable_edition([item('Тем временем Пример получил красную карточку — он пропустит матч против «Запад».',at=START+timedelta(hours=23))],START,END,None,edition_day=DAY)
        b=result['selected'][0]
        self.assertEqual(b.event_type,'DISCIPLINE');self.assertEqual(b.event_date,'')
        self.assertIn('источник сообщил 13 октября',b.render())
        self.assertEqual(b.participants[0],'Пример');self.assertNotIn('Тем временем',b.render())
        self.assertNotIn('официально',b.render());self.assertNotIn('сегодня',b.render())

    def test_proposed_fee_bonus_and_winter_not_submitted_bid(self):
        text='ПРИМЕР – В «ЧЕЛСИ»?! Клуб готов предложить «Барселоне» 30 млн евро + 10 млн бонусами за игрока этой зимой. 📰 Example News'
        b=compiled(text)
        self.assertEqual(b.event_type,'TRANSFER_PLANS')
        self.assertIn('готов предложить',b.render())
        self.assertNotIn('предложил',b.render())
        self.assertIn('30 млн евро',b.render());self.assertIn('10 млн бонусами',b.render())

    def test_halftime_snapshot_never_winner_or_final_score(self):
        text='Алекс Пример сделал дубль в матче Лиги Чемпионов с «Западом». К концу первого тайма «Восток» ведёт 3:0.'
        b=compiled(text)
        self.assertEqual(b.event_type,'MATCH_HALFTIME')
        self.assertIn('К перерыву',b.render());self.assertNotIn('обыграл',b.render())
        self.assertEqual(b.verified_facts[0].values[-1],'HALFTIME_NOT_FINAL')

    def test_shots_record_is_typed_shots_not_goals_or_touches(self):
        b=compiled('«Восток» нанёс 19 ударов в первом тайме выездного матча Лиги чемпионов — это рекорд турнира среди гостевых команд за всю историю сбора статистики от OPTA.')
        self.assertEqual(bundle_issues(b),[])
        observation=b.verified_facts[0].statistics[0]
        self.assertEqual((observation.metric,observation.value,observation.unit),('SHOTS','19','SHOTS'))

    def test_source_midnight_does_not_change_verified_previous_match_date(self):
        inputs=[item('Arsenal beat Lille 3-0.',1,START+timedelta(hours=23)),
                item('The results from Premier League sides in the Champions League this evening\n@Arsenal 3-0 Lille',2,START+timedelta(hours=23,minutes=30)),
                item('Three goals and three different scorers for @Arsenal in the Champions League against Lille 🤩\n⚽️ Alex Example\n⚽️ Ben Example\n⚽️ Dan Example',3,START+timedelta(days=1,minutes=10))]
        result=prepare_immutable_edition(inputs,START,END,None,edition_day=DAY)
        b=result['selected'][0]
        self.assertEqual(b.event_date,'2026-10-13');self.assertEqual(b.section,CARRY_SECTION)
        self.assertRegex(result['body'],r'Лиг[ае] чемпионов');self.assertIn('13 октября',result['body'])
        self.assertIn('Alex Example',result['body']);self.assertEqual(result['essential_facts_lost'],0)
        self.assertEqual(datetime.fromisoformat(b.source_published_at).astimezone(MOSCOW).date(),DAY)

    def test_unknown_match_date_not_invented_from_publication_clock(self):
        inputs=[item('Arsenal beat Lille 3-0.',at=START+timedelta(hours=23))]
        b=prepare_immutable_edition(inputs,START,END,None,edition_day=DAY)['selected'][0]
        self.assertEqual(b.event_date,'')
        self.assertIn('дата матча не уточнена',b.render())

    def test_new_conditional_sale_keeps_changed_threshold_and_condition(self):
        b=compiled('Алекс Пример может покинуть «Восток» уже в январе, если клуб получит предложение на сумму более 100 млн фунтов. После встречи с агентами позиция клуба изменилась.')
        self.assertIn('если',b.render());self.assertIn('более 100 млн фунтов',b.render())
        self.assertIn('CHANGED_AFTER_AGENT_MEETING',[v for f in b.verified_facts for v in f.values])

    def test_uefa_award_is_source_relation_not_photo_or_match_result(self):
        b=compiled('Alex Example, UEFA Man of the Match for Arsenal vs Chelsea.')
        self.assertEqual(b.event_type,'MATCH_AWARD');self.assertEqual(b.verified_facts[0].values[-1],'UEFA_PLAYER_OF_MATCH')
        self.assertNotIn('счётом',b.render())

    def test_career_total_is_bound_to_unique_explicit_full_name_row(self):
        b=compiled('Пример догнал легенду! Алекс забил «Западу» в победном матче Лиги чемпионов (3:0). Теперь у него 14 голов в турнире. Игроку потребовался 31 матч. Алекс Пример — 14 голов.')
        self.assertEqual(b.participants,('Алекс Пример',))
        self.assertEqual(bundle_issues(b),[])
        self.assertEqual([(s.metric,s.value,s.unit) for s in b.verified_facts[0].statistics],[('GOALS','14','GOALS'),('MATCH_COUNT','31','MATCHES')])

    def test_transfer_merge_preserves_both_destinations_and_prevents_subset_repeat(self):
        sources=[item('«Реал» хочет подписать Алекса Примера. 📰 Example News',1),
                 item('Tottenham right-back Алекса Примера is thought to be on his way out of the club, with Barcelona keen. (Sport)',2)]
        result=prepare_immutable_edition(sources,START,END,None,edition_day=DAY)
        self.assertEqual(len(result['selected']),1)
        b=result['selected'][0]
        self.assertEqual(set(b.source_refs),{i.canonical_url for i in sources})
        self.assertLess(b.render().index('По данным Sport'),b.render().index('Кроме того'))
        self.assertIn('«Реал»',b.render());self.assertIn('«Барселона»',b.render())
        unchanged=prepare_immutable_edition(sources[:1],START,END,None,history=[b],edition_day=DAY)
        self.assertEqual(unchanged['selected'],[])

    def test_halftime_context_preserves_every_fact_without_repeating_fixture(self):
        inputs=[item('Алекс Пример сделал дубль в матче Лиги Чемпионов с «Западом». К концу первого тайма «Восток» ведёт 3:0.',1),
                item('Пример и Другой забивают свои первые голы за «Восток» в матче ЛЧ против Западом.',2),
                item('«Восток» нанёс 19 ударов в первом тайме выездного матча Лиги чемпионов — это рекорд турнира среди гостевых команд за всю историю сбора статистики от OPTA.',3)]
        result=prepare_immutable_edition(inputs,START,END,None,edition_day=DAY)
        self.assertEqual(len(result['selected']),1)
        self.assertEqual(result['essential_facts_lost'],0)
        self.assertEqual(len(result['selected'][0].verified_facts),4)
        self.assertIn('19 ударов',result['body']);self.assertIn('впервые отличились',result['body'])

    def test_award_does_not_bind_to_ambiguous_two_results(self):
        stamp=END.isoformat()
        award=replace(compiled('Alex Example, UEFA Man of the Match for Arsenal vs Chelsea.'),source_published_at=stamp)
        first=replace(compiled('Arsenal beat Chelsea 3-0.'),source_published_at=stamp)
        second=replace(first,event_id='second-fixture')
        merged=merge_editorial_context([first,second,award])
        self.assertEqual(len(merged),3)
        self.assertFalse(any(f.key=='match_award' for b in merged if b.event_type=='MATCH_RESULT' for f in b.verified_facts))

    def test_recent_commentary_does_not_hide_richer_original_match_member(self):
        full=item('«Арсенал» разгромил «Челси» на своём поле 4:1. Голами отметились Алекс Пример, Бен Другой, Иван Третий и Пётр Четвёртый.',1)
        recent=item('Зрители обсуждают победу клуба и эмоции болельщиков.',2)
        c=FactBundle('match-group',(),recent,(Fact(recent.text,recent.canonical_url,'detail'),),(recent.canonical_url,full.canonical_url))
        b=compile_bundle(c,DAY,{i.canonical_url:i for i in (full,recent)})
        self.assertEqual(b.event_type,'MATCH_RESULT')
        self.assertEqual(next(f.values for f in b.verified_facts if f.key=='result'),('Арсенал','Челси','4:1'))
        self.assertIn('Пётр Четвёртый',b.render());self.assertEqual(b.source_refs,(full.canonical_url,))

    def test_incompatible_original_results_are_not_recovered(self):
        a=item('«Арсенал» разгромил «Челси» на своём поле 4:1.',1)
        b=item('«Арсенал» разгромил «Челси» на своём поле 5:1.',2)
        recent=item('Реакция болельщиков после матча.',3)
        c=FactBundle('match-group',(),recent,(Fact(recent.text,recent.canonical_url,'detail'),),(a.canonical_url,b.canonical_url,recent.canonical_url))
        self.assertIsNone(compile_bundle(c,DAY,{i.canonical_url:i for i in (a,b,recent)}))


if __name__=='__main__':unittest.main()
