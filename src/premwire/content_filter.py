from __future__ import annotations

import re
from dataclasses import dataclass

from .config import FilterConfig
from .russian_editorial import source_is_promotional_only


_TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/|[A-Za-z0-9_]+)", re.IGNORECASE)
_TELEGRAM_INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:\+|joinchat/)", re.IGNORECASE)
_CALL_TO_ACTION = (
    "подписывайся",
    "подписывайтесь",
    "переходи",
    "переходите",
    "регистрируйся",
    "регистрируйтесь",
    "забирай бонус",
    "получи бонус",
    "участвуй",
    "успей купить",
    "смотреть",
)
_BETTING_PHRASES = (
    "ставка дня",
    "сумма ставки",
    "делай ставки",
    "делайте ставки",
    "твоя ставка",
    "ваша ставка",
    "досрочно рассчитает выигрыш",
    "забрать прогноз",
    "прогноз бесплатно",
    "коэффициент",
    "побед кряду",
)
_BULK_STREAM_PROMO_RE = re.compile(
    r"\b(?:абсолютно\s+)?все\s+матчи\b[^.!?]{0,120}"
    r"\b(?:смотрим|смотрите|доступн\w*)\b[^.!?]{0,80}\b(?:групп|канал)\w*\b",
    re.IGNORECASE,
)
_PUBLIC_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SOCIAL_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]+")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_NEWS_ACTION_RE = re.compile(
    r"\b(?:agree|agreed|announce|announced|appoint|appointed|bid|block|blocked|"
    r"change|changed|confirm|confirmed|contact|deal|extend|extended|eye|face|"
    r"fight|injur|join|leave|loan|lose|miss|offer|pray|reject|rejected|renew|"
    r"retire|return|sack|score|sign|signed|start|train|transfer|undergo|want|win|"
    r"аренд|блокир|выигр|выступ|забил|измен|интерес|назнач|объяв|подпис|"
    r"подтверд|покин|получ|предлож|продл|проигр|пропуст|соглас|сорвал|"
    r"травм|увол|уход|хочет)\w*\b",
    re.IGNORECASE,
)
_LOW_INFORMATION_PREFIXES = (
    "and originally",
    "follows @",
    "via @",
    "credit @",
)
_VIDEO_TEASER_RE = re.compile(
    r"(?:\b(?:in|this)\s+(?:video|clip)\b|"
    r"\b(?:watch|смотрите|смотри)\s+(?:the\s+)?(?:video|clip|ролик|видео)\b|"
    r"\bв\s+(?:этом\s+)?(?:видео|ролике)\b|"
    r"\b(?:видео|ролик)\s+(?:о|про|рассказывает|показывает)\b)",
    re.IGNORECASE,
)
_CHANNEL_MATCH_PROMO_RE = re.compile(
    r"(?:\bбез\s+трансляций\b[\s\S]{0,700}\bподпишись\s+на\s+вышли\b"
    r"[\s\S]{0,160}\bсмотри\s+все\s+матчи\b|"
    r"\bнаш\s+комментатор\b[\s\S]{0,700}\bпрямо\s+сейчас\s+комментирует\b"
    r"[\s\S]{0,120}\bподключайтесь\b)",
    re.IGNORECASE,
)
_LIVE_BROADCAST_CTA_RE = re.compile(
    r"(?:\bподключа(?:йся|йтесь)\s+(?:к|на)\s+(?:прям\w+\s+)?трансляци\w*|"
    r"\bсмотр(?:и|ите|еть)\s+(?:матч\w*\s+)?(?:в\s+)?прям\w+\s+эфир\w*|"
    r"\bсмотр(?:и|ите|еть)\s+(?:прям\w+\s+)?трансляци\w*|"
    r"\bjoin\s+(?:the\s+)?(?:live\s+)?stream\b|"
    r"\bwatch\s+(?:the\s+)?(?:match\s+)?live\b)",
    re.IGNORECASE,
)
_AUDIENCE_COMMENTARY_RE = re.compile(
    r"(?:\b(?:зрител|болельщик|подписчик|читател)\w*\b[\s\S]{0,240}"
    r"\b(?:в\s+)?(?:комментари|чат)\w*\b|"
    r"\b(?:комментари|чат)\w*\b[\s\S]{0,240}"
    r"\b(?:зрител|болельщик|подписчик|читател)\w*\b|"
    r"\b(?:viewers?|fans?|followers?|subscribers?)\b[\s\S]{0,240}"
    r"\b(?:comments?|live\s+chat)\b|"
    r"\b(?:comments?|live\s+chat)\b[\s\S]{0,240}"
    r"\b(?:viewers?|fans?|followers?|subscribers?)\b)",
    re.IGNORECASE,
)
_AWARD_VOTING_CTA_RE = re.compile(
    r"(?=[\s\S]*(?:\btime\s+to\s+(?:get\s+)?vot(?:e|ing)\b|"
    r"\bget\s+voting\b|\bvote\s+(?:now|for)\b|"
    r"\bcast\s+(?:a|your)\s+vote\b|"
    r"\bvoting\s+is\s+(?:now\s+)?open\b|"
    r"\bwho\s+(?:gets|has)\s+your\s+vote\b|"
    r"\b(?:голосуйте|голосуй|проголосуйте|проголосуй)\b|"
    r"\b(?:пора|время)\s+голосовать\b|"
    r"\bпризыва\w*\s+голосовать\b|\bголосовать\s+за\b))"
    r"(?=[\s\S]*(?:\b(?:manager|coach|player|goal|save)\b[\s\S]{0,80}"
    r"\b(?:of\s+the\s+month|monthly\s+award)\b|"
    r"\b(?:тренер|игрок|гол|сейв)\w*\b[\s\S]{0,80}\bмесяц\w*\b))",
    re.IGNORECASE,
)
_BRAND_PITCH_RE = re.compile(
    r"(?:\bконтекст\s+вместо\s+шума\b|"
    r"\bобъясняем\s+причины,?\s+последствия\s+и\s+смысл\s+событий\b|"
    r"\b(?:новости|контекст)\s+без\s+шума\b)",
    re.IGNORECASE,
)
_ROUTINE_MATCHDAY_RE = re.compile(
    r"(?:\bготов\w*\s+(?:к|перед)\s+[^.!?\n]{0,80}\b(?:матч|игр)\w*\b|"
    r"\b(?:пров(?:ёл|ел|ела|ели|одит|одят)|начал\w*)\s+"
    r"(?:[^.!?\n]{0,40}\s)?(?:тренировк|разминк)\w*\b|"
    r"\bприбыл\w*\s+(?:на|к)\s+(?:стадион|матч|игр)\w*\b|"
    r"\bвыш\w*\s+на\s+разминк\w*\b|"
    r"\bprepar\w*\s+for\s+[^.!?\n]{0,80}\b(?:match|game)\b|"
    r"\btrain\w*\s+(?:ahead\s+of|before)\s+[^.!?\n]{0,80}\b(?:match|game)\b|"
    r"\barriv\w*\s+at\s+(?:the\s+)?stadium\b|"
    r"\bwarm\w*\s+up\s+(?:ahead\s+of|before)\b)",
    re.IGNORECASE,
)
_MATERIAL_NEWS_RE = re.compile(
    r"\b(?:трансфер|переход|переговор|контракт|подпис|аренд|официальн|"
    r"травм|восстанов|вернул|возвращ|дебют|впервые|состав|заявк|стартов|"
    r"основн\w+\s+состав|скамейк|пропуст|не\s+сыгра|дисквалиф|назнач|увол|"
    r"забил|гол|побед|проигр|сч[её]т|рекорд|сказал|заявил|рассказал|"
    r"transfer|contract|negotiat|sign|loan|official|injur|recover|return|"
    r"debut|first\s+time|line-?up|squad|starting|bench|miss|suspend|appoint|"
    r"sack|scor|goal|win|defeat|record|said|says)\w*\b",
    re.IGNORECASE,
)


def routine_matchday_without_news(text: str) -> bool:
    """Identify match-day atmosphere that contains no publishable development."""

    return bool(_ROUTINE_MATCHDAY_RE.search(text)) and not bool(
        _MATERIAL_NEWS_RE.search(text)
    )


def source_has_live_broadcast_promotion(text: str) -> bool:
    """Return true for a direct invitation to watch or join a live broadcast."""

    return bool(_LIVE_BROADCAST_CTA_RE.search(text))


def source_is_audience_commentary(text: str) -> bool:
    """Identify another outlet's audience/chat discussion rather than a news event."""

    return bool(_AUDIENCE_COMMENTARY_RE.search(text))


def source_is_award_voting_promotion(text: str) -> bool:
    """Identify calls to vote in monthly football awards rather than news."""

    return bool(_AWARD_VOTING_CTA_RE.search(text))


@dataclass(frozen=True, slots=True)
class FilterDecision:
    blocked: bool
    reason: str | None = None
    evidence: tuple[str, ...] = ()


class AdvertisementFilter:
    def __init__(self, config: FilterConfig) -> None:
        self.config = config

    def check(self, text: str) -> FilterDecision:
        if not self.config.block_advertising:
            return FilterDecision(False)

        folded = text.casefold()
        markers = tuple(marker for marker in self.config.advertising_markers if marker in folded)
        calls = tuple(marker for marker in _CALL_TO_ACTION if marker in folded)
        telegram_links = tuple(_TELEGRAM_LINK_RE.findall(text))
        invite_links = tuple(_TELEGRAM_INVITE_RE.findall(text))
        betting = tuple(marker for marker in _BETTING_PHRASES if marker in folded)

        hard_markers = tuple(
            marker
            for marker in markers
            if marker.startswith(("#реклам", "erid:"))
            or "реклам" in marker
            or "промокод" in marker
            or "букмекер" in marker
            or "казино" in marker
        )
        evidence = tuple(dict.fromkeys((*markers, *calls, *betting)))
        if hard_markers:
            return FilterDecision(True, "advertising_marker", evidence)
        if "ставка дня" in betting or "сумма ставки" in betting or len(betting) >= 2:
            return FilterDecision(True, "betting_promotion", evidence)
        if invite_links and calls:
            return FilterDecision(True, "channel_promotion", evidence)
        if len(telegram_links) >= 2 and calls:
            return FilterDecision(True, "repeated_channel_promotion", evidence)
        if len(evidence) >= 2:
            return FilterDecision(True, "advertising_pattern", evidence)
        return FilterDecision(False)


class SourceQualityFilter:
    """Reject captions and cross-post fragments that do not contain a news fact."""

    def check(self, text: str) -> FilterDecision:
        raw = text.strip()
        folded = raw.casefold()
        if source_has_live_broadcast_promotion(raw):
            return FilterDecision(True, "live_broadcast_promotion", ())
        if source_is_audience_commentary(raw):
            return FilterDecision(True, "audience_commentary_not_news", ())
        if source_is_award_voting_promotion(raw):
            return FilterDecision(True, "award_voting_promotion", ())
        if routine_matchday_without_news(raw):
            return FilterDecision(True, "routine_matchday_without_news", ())
        if _BRAND_PITCH_RE.search(raw) and not _NEWS_ACTION_RE.search(raw):
            return FilterDecision(True, "brand_promotion_without_story", ())
        if source_is_promotional_only(raw):
            return FilterDecision(True, "non_news_promotional_content", ())
        if _BULK_STREAM_PROMO_RE.search(raw):
            return FilterDecision(True, "bulk_stream_promotion", ())
        # A video teaser is not a standalone editorial fact.  PremWire does
        # not publish videos, so text that merely introduces a clip must not
        # become a news post even when it contains football keywords.
        if _VIDEO_TEASER_RE.search(raw):
            return FilterDecision(True, "video_teaser_without_standalone_news", ())
        if _CHANNEL_MATCH_PROMO_RE.search(raw):
            return FilterDecision(True, "channel_match_promotion", ())
        if any(folded.startswith(prefix) for prefix in _LOW_INFORMATION_PREFIXES):
            return FilterDecision(True, "source_fragment", ())
        cleaned = _PUBLIC_URL_RE.sub(" ", raw)
        cleaned = _SOCIAL_HANDLE_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\b(?:link|video|photo|read more)\b", " ", cleaned, flags=re.IGNORECASE)
        words = _WORD_RE.findall(cleaned)
        meaningful = " ".join(words).strip()
        if len(meaningful) < 24 or len(words) < 4:
            return FilterDecision(True, "insufficient_source_text", ())
        if len(meaningful) < 90 and not _NEWS_ACTION_RE.search(meaningful):
            return FilterDecision(True, "caption_without_news_action", ())
        return FilterDecision(False)
