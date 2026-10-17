"""October incident relations reproduced with synthetic, public-safe sources."""
from datetime import date, datetime, timezone
import json
import unittest

from premwire.daily_quality import semantic_source_issues, semantic_relation_issues
from premwire.daily_fact_digest import compile_bundle, apply_choices, render, validate_edit
from premwire.daily_fact_shadow import normalize_handles
from tests.test_daily_fact_digest import candidate, match_bundle


class SemanticSafetyTests(unittest.TestCase):
    def test_parker_incident_other_sport_despite_premier_league(self):
        self.assertIn('SPORT_IDENTITY_OTHER_SPORT', semantic_source_issues(
            'Two touchdowns. Two Premier League celebrations. A visiting NFL player.'))

    def test_plain_football_is_not_rejected(self):
        self.assertEqual(semantic_source_issues('ОФИЦИАЛЬНО: Алекс Пример перешёл в «Восток».'), [])

    def test_belloumi_incident_contradictory_goal_type(self):
        self.assertIn('MATCH_RESULT_CONTRADICTORY_GOAL_TYPE', semantic_source_issues(
            'Игрок забил победный гол, чтобы сравнять счёт.'))

    def test_equaliser_cannot_be_winner(self):
        issues=semantic_relation_issues('Example scores Harbor FC\'s equaliser against Northbridge.',
                                       'Игрок забил победный гол.')
        self.assertIn('MATCH_RESULT_GOAL_TYPE_CHANGED',issues)

    def test_arbeloa_incident_risk_not_resignation(self):
        self.assertIn('ACTOR_RELATIONS_DISMISSAL_RISK_TO_RESIGNATION',semantic_relation_issues(
            'Тренер ответил на вопрос о возможном увольнении.', 'Тренер решил уйти.'))

    def test_delap_incident_ironic_violence_not_fact(self):
        self.assertIn('QUOTE_AND_INTENT_UNVERIFIED_VIOLENCE',semantic_source_issues(
            'У него был миллион вариантов, но он выбрал вырубить вратаря.'))

    def test_carvajal_incident_bare_headline_has_unknown_stage(self):
        self.assertIn('TRANSFER_STATUS_UNSPECIFIED',semantic_source_issues('Алекс Пример переходит в «Восток».'))

    def test_interest_not_official(self):
        self.assertIn('TRANSFER_STATUS_OR_PLAN_CERTAINTY_UPGRADED',semantic_relation_issues(
            'Клуб проявляет интерес к игроку.', 'Игрок официально перешёл.'))

    def test_youth_identity_not_guessed_from_coach(self):
        self.assertIn('TEAM_LEVEL_AGE_UNRESOLVED',semantic_source_issues('#YoungLions play next month.'))
        self.assertNotIn('TEAM_LEVEL_AGE_UNRESOLVED',semantic_source_issues('England U21 play next month.'))

    def test_team_level_preserved(self):
        self.assertIn('TEAM_LEVEL_CHANGED_OR_DROPPED',semantic_relation_issues(
            'England U21 face Portugal U21.', 'Сборная Англии сыграет с Португалией.'))

    def test_wrong_age_category(self):
        self.assertIn('TEAM_LEVEL_CHANGED_OR_DROPPED',semantic_relation_issues('England U19.', 'Англия U21.'))

    def test_gabriel_incident_brand_digits_are_not_discarded(self):
        self.assertIn('FACT_PRESERVATION_ATTRIBUTION_CHANGED',semantic_relation_issues(
            'Игрок близок к подписанию. 📰 News365', 'Игрок близок к подписанию. Источник: News.'))

    def test_explicit_club_handle_survives(self):
        self.assertIn('Hull City',normalize_handles("Player scores @HullCity's equaliser."))

    def test_unknown_club_handle_is_not_inferred(self):
        self.assertIn('@UnknownClub',normalize_handles("Player scores @UnknownClub's equaliser."))

    def test_unknown_actor_club_cannot_be_inserted(self):
        self.assertIn('ACTOR_RELATIONS_UNSUPPORTED_GOAL_CLUB',semantic_relation_issues(
            'Hull City scores equaliser against Everton.', 'Брайтон забил гол против Эвертона.'))

    def test_advertorial_rejected_but_separate_football_not(self):
        self.assertTrue(semantic_source_issues('Матч покажут в фудмолле. Еда и напитки.'))
        self.assertEqual(semantic_source_issues('Матч состоится 14 октября.'),[])

    def test_return_plan_not_outcome(self):
        self.assertIn('TRANSFER_STATUS_OR_PLAN_CERTAINTY_UPGRADED',semantic_relation_issues(
            'The Federation wants him back and the plan will follow.', 'План будет реализован.'))

    def test_general_equaliser_template_keeps_all_relations(self):
        b=compile_bundle(candidate(["Alex Example scores Harbor FC's equaliser against Northbridge to continue his season."]),date(2026,10,12))
        self.assertEqual(b.event_type,'MATCH_GOAL')
        self.assertEqual(b.verified_facts[0].values,('Alex Example','Harbor FC','Northbridge','EQUALISER_NOT_WINNER'))
        self.assertNotIn('победный',b.render());self.assertNotIn('2:2',b.render())

    def test_contract_agreement_not_signed(self):
        b=compile_bundle(candidate(['Harbor FC have reached an agreement in principle with Alex Example over new contract.']),date(2026,10,12))
        self.assertIn('AGREEMENT_IN_PRINCIPLE_NOT_SIGNED',b.verified_facts[0].values)
        self.assertNotIn('официально',b.render())

    def test_general_injury_template_no_diagnosis_invention(self):
        b=compile_bundle(candidate(['Coach Example confirms Harbor FC lost Alex Example and Ben Example due to injury.']),date(2026,10,12))
        self.assertEqual(b.event_type,'INJURY');self.assertNotIn('разрыв',b.render())

    def test_model_cannot_inject_intent_or_score(self):
        b=match_bundle()
        self.assertTrue(validate_edit(b,b.render()+' Игрок решил ударить соперника.'))
        choices,issues=apply_choices([b],json.dumps([{'event_id':b.event_id,'variant':0,'text':'Счёт 5:0.'}]))
        self.assertTrue(issues);self.assertEqual(choices[b.event_id],0)

    def test_essential_match_details_survive(self):
        b=match_bundle()
        for variant in (0,1):
            for token in ('3:0','Холанд','Доку','Черки','Гравенберх','62'):
                self.assertIn(token,b.render(variant))

    def test_only_bad_member_excluded_from_model_prose(self):
        b=match_bundle();body,selected=render([b,b],date(2026,10,12))
        self.assertEqual(len(selected),1);self.assertIn('3:0',body)


if __name__=='__main__':unittest.main()
