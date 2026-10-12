from __future__ import annotations

import unittest

from premwire.simple_factual_guard import check_simple_factual_safety


def codes(source: str, output: str) -> set[str]:
    return {item.code for item in check_simple_factual_safety(source, output)}


class SimpleFactualGuardTests(unittest.TestCase):
    def test_combined_stat_cannot_be_recast_as_goal_authorship(self) -> None:
        violations = check_simple_factual_safety(
            "Главными звёздами стали Кевин Шаде (2+2) и Игор Тиаго (2+1).",
            "Главными авторами голов стали Кевин Шаде (2+2) и Игор Тиаго (2+1).",
        )
        self.assertIn("AMBIGUOUS_STAT_ROLE", {item.code for item in violations})

    def test_same_numeric_fact_is_accepted(self) -> None:
        self.assertEqual(codes("Контракт до 2028 года.", "Контракт действует до 2028 года."), set())

    def test_currency_normalization_is_accepted(self) -> None:
        self.assertEqual(codes("Fee is £7m.", "Сумма трансфера — 7 млн фунтов."), set())

    def test_adjacent_amounts_do_not_exchange_units(self) -> None:
        self.assertEqual(
            codes(
                "Fee is €2m plus €500k add-ons.",
                "Сумма — 2 млн евро плюс 500 тыс. евро бонусами.",
            ),
            set(),
        )

    def test_new_amount_is_rejected(self) -> None:
        self.assertIn("UNSUPPORTED_NUMERIC_CLAIM", codes("Fee is €20m.", "Сумма — 25 млн евро."))

    def test_changed_year_is_rejected(self) -> None:
        self.assertIn("UNSUPPORTED_NUMERIC_CLAIM", codes("Контракт до 2028 года.", "Контракт до 2029 года."))

    def test_changed_percentage_is_rejected(self) -> None:
        self.assertIn("UNSUPPORTED_NUMERIC_CLAIM", codes("Sell-on clause is 15%.", "Процент от продажи — 20%."))

    def test_changed_currency_is_rejected(self) -> None:
        self.assertIn("UNSUPPORTED_NUMERIC_CLAIM", codes("Fee is £7m.", "Сумма — 7 млн евро."))

    def test_score_order_is_preserved(self) -> None:
        self.assertIn("UNSUPPORTED_NUMERIC_CLAIM", codes("England won 6-4.", "Англия проиграла 4:6."))

    def test_last_16_and_round_of_16_are_equivalent(self) -> None:
        self.assertEqual(
            codes(
                "Spain won its World Cup last-16 match.",
                "Испания победила в матче 1/8 финала чемпионата мира.",
            ),
            set(),
        )

    def test_dense_stat_must_remain_compact_or_be_omitted(self) -> None:
        self.assertIn("AMBIGUOUS_STAT_REWRITE", codes("Kevin recorded 2+2.", "Кевин забил 2 гола."))
        self.assertEqual(codes("Kevin recorded 2+2.", "Кевин стал лучшим игроком."), set())
        self.assertEqual(codes("Kevin recorded 2+2.", "Кевин набрал 2+2."), set())

    def test_agreement_to_signed_is_rejected(self) -> None:
        self.assertIn("TRANSFER_CERTAINTY_UPGRADED", codes("Edin agreed to sign a new deal.", "Эдин подписал новый контракт."))

    def test_source_says_player_signs_contract(self) -> None:
        self.assertEqual(
            codes(
                "Club agreement reached. Fred signs a 3.5 year deal.",
                "Клубы договорились. Fred подписал контракт на 3,5 года.",
            ),
            set(),
        )

    def test_talks_to_agreement_is_rejected(self) -> None:
        self.assertIn("TRANSFER_CERTAINTY_UPGRADED", codes("Clubs are in talks over a transfer.", "Клубы договорились о трансфере."))

    def test_interest_to_signing_is_rejected(self) -> None:
        self.assertIn("TRANSFER_CERTAINTY_UPGRADED", codes("Arsenal are interested in signing him.", "Арсенал подписал игрока."))

    def test_more_cautious_wording_is_accepted(self) -> None:
        self.assertEqual(codes("Player is set to rejoin on loan.", "Игрок близок к возвращению в аренду."), set())

    def test_broken_transfer_relation_is_rejected(self) -> None:
        self.assertIn(
            "BROKEN_TRANSFER_RELATION",
            codes(
                "Chelsea offered a contract to the player.",
                "Лондонский клуб предложил игрока.",
            ),
        )

    def test_omitted_numbers_are_accepted(self) -> None:
        self.assertEqual(codes("He scored 3 goals in 12 matches.", "Он отличился в прошлом сезоне."), set())

    def test_non_story_brand_pitch_cannot_become_transfer_news(self) -> None:
        self.assertIn(
            "NON_STORY_INVENTED",
            codes(
                "GOAL24 PRO — Контекст вместо шума. Объясняем причины, последствия и смысл событий.",
                "Брюгге объявил о переходе Джейми Байно-Гиттенса.",
            ),
        )

    def test_named_speaker_cannot_be_replaced(self) -> None:
        self.assertIn(
            "PERSON_ENTITY_REPLACED",
            codes(
                "Де Дзерби нашёл объяснение неудачному матчу команды.",
                "Симоне Индзаги объяснил неудачный матч команды.",
            ),
        )

    def test_named_speaker_paraphrase_is_accepted(self) -> None:
        self.assertNotIn(
            "PERSON_ENTITY_REPLACED",
            codes(
                "Роберто Де Дзерби нашёл объяснение неудачному матчу команды.",
                "Де Дзерби объяснил неудачный матч команды.",
            ),
        )

    def test_goal_event_cannot_be_recast_as_final_result(self) -> None:
        self.assertIn(
            "MATCH_STATUS_UPGRADED",
            codes(
                "МЮ пропустил от Ипсвича. Смотрим матч в прямом эфире.",
                "Манчестер Юнайтед уступил Ипсвичу.",
            ),
        )

    def test_goal_event_without_live_cta_still_is_not_a_final(self) -> None:
        self.assertIn(
            "MATCH_STATUS_UPGRADED",
            codes(
                "МЮ пропустил от Ипсвича на 34-й минуте.",
                "Манчестер Юнайтед уступил Ипсвичу.",
            ),
        )

    def test_explicit_final_result_after_goal_event_is_accepted(self) -> None:
        self.assertNotIn(
            "MATCH_STATUS_UPGRADED",
            codes(
                "МЮ пропустил первым, но матч завершился победой 2:1.",
                "Манчестер Юнайтед победил 2:1.",
            ),
        )

    def test_age_question_cannot_become_asserted_age(self) -> None:
        self.assertIn(
            "QUESTION_TURNED_INTO_FACT",
            codes(
                "А точно ли Малику Ялькуйе 20 лет?",
                "Некоторые считают, что игроку уже исполнилось 20 лет.",
            ),
        )

    def test_sell_on_percentage_cannot_be_called_a_bonus(self) -> None:
        self.assertIn(
            "SELL_ON_RECAST_AS_BONUS",
            codes(
                "Рафаэл Леау перейдёт за 45 млн евро и 20% с будущей продажи.",
                "Сделка составит 45 млн евро. Также предусмотрены бонусы в размере 20% от будущей перепродажи.",
            ),
        )

    def test_real_sell_on_wording_cannot_be_called_a_bonus(self) -> None:
        self.assertIn(
            "SELL_ON_RECAST_AS_BONUS",
            codes(
                "Рафаэль Леау - игрок Галатасарая. Сумма трансфера: 45 млн евро + 20% с перепродажи.",
                "Рафаэль Леау перешёл в Галатасарай за 45 млн евро. В сделку включены бонусы в размере 20% от будущей перепродажи.",
            ),
        )

    def test_hypothetical_plan_cannot_become_completed_clause(self) -> None:
        self.assertIn(
            "HYPOTHETICAL_PLAN_UPGRADED",
            codes(
                "Аль-Хиляль может купить Альвареса. План таков: Барселона получит опцию выкупа.",
                "Аль-Хиляль купил Альвареса и включил эксклюзивную опцию для Барселоны.",
            ),
        )

    def test_real_hypothetical_plan_wording_is_not_upgraded(self) -> None:
        self.assertIn(
            "HYPOTHETICAL_PLAN_UPGRADED",
            codes(
                "Аль-Хиляль может купить Альвареса. План саудовского клуба таков: клуб покупает игрока и включает опцию выкупа для Барселоны.",
                "Аль-Хиляль готов приобрести Альвареса и включил эксклюзивную опцию для Барселоны.",
            ),
        )

    def test_featured_player_cannot_disappear_from_output(self) -> None:
        self.assertIn(
            "SOURCE_SUBJECT_DROPPED",
            codes(
                "Та самая магия Райана Шерки. В штрафной разобрался с четырьмя защитниками.",
                "В штрафной разобрался с четырьмя защитниками и пробил в дальний угол.",
            ),
        )

    def test_source_local_player_position_cannot_be_invented(self) -> None:
        self.assertIn(
            "UNSUPPORTED_PLAYER_POSITION",
            codes(
                "Emi Martinez starts for Chelsea!",
                "Эми Мартинес выйдет стартовым вратарём за Челси.",
            ),
        )


if __name__ == "__main__":
    unittest.main()
