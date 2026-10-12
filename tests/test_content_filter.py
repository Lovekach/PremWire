from __future__ import annotations

import unittest
from pathlib import Path

from premwire.config import load_config
from premwire.content_filter import AdvertisementFilter, SourceQualityFilter


ROOT = Path(__file__).resolve().parents[1]


class AdvertisementFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = AdvertisementFilter(load_config(ROOT).filters)

    def test_explicit_advertisement_is_blocked(self) -> None:
        decision = self.filter.check("Реклама. Используйте промокод GOAL")
        self.assertTrue(decision.blocked)

    def test_channel_promotion_is_blocked(self) -> None:
        decision = self.filter.check(
            "Подписывайтесь на канал наших друзей https://t.me/example"
        )
        self.assertTrue(decision.blocked)

    def test_normal_transfer_news_is_allowed(self) -> None:
        decision = self.filter.check(
            "Арсенал начал переговоры по трансферу полузащитника."
        )
        self.assertFalse(decision.blocked)

    def test_streaming_invite_spam_is_blocked(self) -> None:
        decision = self.filter.check(
            "ФИНАЛ НАЧИНАЕТСЯ! СМОТРЕТЬ: https://t.me/+abc https://t.me/+abc"
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "channel_promotion")

    def test_betting_streak_promotion_is_blocked(self) -> None:
        decision = self.filter.check(
            "Ставка дня, коэффициент 2.85. Забрать прогноз бесплатно"
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "betting_promotion")


class SourceQualityFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = SourceQualityFilter()

    def test_cross_post_fragment_is_blocked(self) -> None:
        self.assertTrue(self.filter.check("And originally @Reporter").blocked)

    def test_reaction_caption_without_news_is_blocked(self) -> None:
        self.assertTrue(self.filter.check("Rogers 🤝 Palmer Cold x2 Video").blocked)

    def test_short_transfer_fact_is_allowed(self) -> None:
        decision = self.filter.check(
            "Real Madrid want to sign Rodri during the summer transfer window."
        )
        self.assertFalse(decision.blocked)

    def test_short_russian_transfer_fact_is_allowed(self) -> None:
        decision = self.filter.check(
            "Арсенал предложил новый контракт своему капитану команды."
        )
        self.assertFalse(decision.blocked)

    def test_watch_now_promotion_without_news_is_blocked(self) -> None:
        decision = self.filter.check(
            "We launch part two of our highlights stream. Watch now! https://example.test"
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "non_news_promotional_content")

    def test_russian_video_teaser_is_blocked_even_with_transfer_keywords(self) -> None:
        decision = self.filter.check(
            "До закрытия трансферного окна остается неделя, поэтому многие "
            "игроки и клубы спешат завершить переходы. В этом видео "
            "рассказывается о сделках, которые прямо сейчас выглядят "
            "наиболее вероятными."
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "video_teaser_without_standalone_news")

    def test_fixture_roundup_with_channel_watch_slogan_is_blocked(self) -> None:
        decision = self.filter.check(
            "Какое воскресенье без трансляций? Челси сыграет против Брайтона, "
            "а Манчестер Юнайтед — с Ипсвичем. "
            "ПОДПИШИСЬ НА ВЫШЛИ! СМОТРИ ВСЕ МАТЧИ АПЛ"
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "channel_match_promotion")

    def test_commentator_invitation_is_blocked_as_promo(self) -> None:
        decision = self.filter.check(
            "Наш комментатор оценил матч Челси и Брайтона. "
            "Он прямо сейчас комментирует этот матч. Подключайтесь."
        )
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "channel_match_promotion")

    def test_news_fact_with_appended_cta_is_not_wholly_blocked(self) -> None:
        decision = self.filter.check("Chelsea signed the player. Watch now!")
        self.assertFalse(decision.blocked)

    def test_brand_pitch_without_story_is_blocked(self) -> None:
        decision = self.filter.check(
            "GOAL24 PRO — Контекст вместо шума. Объясняем причины, "
            "последствия и смысл событий. Подпишись на канал."
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "brand_promotion_without_story")

    def test_routine_match_preparation_is_blocked(self) -> None:
        decision = self.filter.check(
            "🎙️ Михаил Сазанов уже готовится к вечернему матчу! "
            "Возможно всем стоит внимательно прислушаться к его словам, "
            "ведь интриг сегодня очень много 💯 ⚽️ ПОДПИШИСЬ НА ВЫШЛИ! — "
            "СМОТРИ ВСЕ МАТЧИ АПЛ"
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "routine_matchday_without_news")

    def test_material_return_from_injury_is_not_routine_matchday(self) -> None:
        decision = self.filter.check(
            "Михаил Сазанов восстановился после травмы и готовится к матчу."
        )

        self.assertFalse(decision.blocked)

    def test_live_broadcast_invitation_blocks_entire_stat_post(self) -> None:
        decision = self.filter.check(
            "Эмери посмеет испортить особый день для Артеты?! "
            "Сегодняшняя игра станет для Артеты 250-й в качестве тренера. "
            "На этом отрезке у него 60% побед. "
            "Подключайтесь к трансляции матча в 22:00."
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "live_broadcast_promotion")

    def test_plain_manager_milestone_without_broadcast_cta_is_allowed(self) -> None:
        decision = self.filter.check(
            "Сегодняшняя игра станет для Артеты 250-й в качестве тренера. "
            "На этом отрезке у него 60% побед."
        )

        self.assertFalse(decision.blocked)

    def test_audience_commentary_repost_is_blocked(self) -> None:
        decision = self.filter.check(
            "Скаутский отдел Манчестер Юнайтед переехал в комментарии к "
            "трансляциям матча против Ипсвича. Зрители предлагали варианты "
            "трансферных усилений для защиты команды. Особое внимание уделяется "
            "левому флангу, где Люк Шоу регулярно получает травмы."
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "audience_commentary_not_news")

    def test_actual_club_transfer_news_is_not_audience_commentary(self) -> None:
        decision = self.filter.check(
            "Манчестер Юнайтед рассматривает усиление левого фланга защиты из-за "
            "регулярных травм Люка Шоу."
        )

        self.assertFalse(decision.blocked)

    def test_manager_of_the_month_voting_cta_is_blocked(self) -> None:
        decision = self.filter.check(
            "Guiding their teams to maximum points at the start of the season. "
            "Time to get voting for August's Manager of the Month."
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "award_voting_promotion")

    def test_russian_award_voting_cta_is_blocked(self) -> None:
        decision = self.filter.check(
            "Ведя свои команды к максимальному количеству очков в начале сезона, "
            "руководство призывает голосовать за лучшего тренера месяца августа."
        )

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "award_voting_promotion")

    def test_manager_of_the_month_result_remains_publishable(self) -> None:
        decision = self.filter.check(
            "The Premier League has announced Mikel Arteta as Manager of the "
            "Month after Arsenal won all four league matches in August."
        )

        self.assertFalse(decision.blocked)

    def test_award_result_after_public_vote_is_not_misread_as_cta(self) -> None:
        decision = self.filter.check(
            "The Premier League announced Mikel Arteta as Manager of the Month "
            "following a public vote and four consecutive Arsenal wins."
        )

        self.assertFalse(decision.blocked)

    def test_closed_award_voting_is_not_misread_as_cta(self) -> None:
        decision = self.filter.check(
            "Voting has closed and the Premier League announced Mikel Arteta as "
            "Manager of the Month after four consecutive Arsenal wins."
        )

        self.assertFalse(decision.blocked)


if __name__ == "__main__":
    unittest.main()
