from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .content_filter import (
    routine_matchday_without_news,
    source_is_award_voting_promotion,
    source_has_live_broadcast_promotion,
    source_is_audience_commentary,
)
from .russian_editorial import club_entries, normalize_editorial_text
from .source_language import normalize_source_language
from .editorial_safety import source_problem, REACTION_TAIL


SKIP_NON_FOOTBALL = "SKIP_NON_FOOTBALL"
SKIP_PROMOTIONAL = "SKIP_PROMOTIONAL"
SKIP_MULTI_STORY = "SKIP_MULTI_STORY"
SKIP_UNNAMED_PLAYER = "SKIP_UNNAMED_PLAYER"
SKIP_LOW_INFORMATION = "SKIP_LOW_INFORMATION"
SKIP_AUDIENCE_COMMENTARY = "SKIP_AUDIENCE_COMMENTARY"


@dataclass(frozen=True, slots=True)
class SimpleSourceDecision:
    allowed: bool
    clean_text: str
    reason: str | None = None


_URL_RE = re.compile(
    r"(?:https?://|www\.|(?:t\.me|telegram\.me|x\.com|twitter\.com)/)\S+",
    re.I,
)
_BARE_URL_RE = re.compile(
    r"(?<![\w@])(?:[A-Za-z0-9-]+\.)+(?:com|net|org|ru|ge|co|io)/\S+",
    re.I,
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*(?:https?://|www\.)[^)]+\)", re.I)
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-zА-Яа-яЁё0-9_]+")
_HASHTAG_RE = re.compile(r"(?<!\w)#[A-Za-zА-Яа-яЁё0-9_]+")
_CTA_RE = re.compile(
    r"\b(?:подпишись|подпишитесь|подписывайся|подписывайтесь|смотри(?:те)?\s+все\s+матчи|"
    r"смотр(?:им|и|ите)\s+(?:этот\s+)?матч\s+(?:в\s+прямом\s+эфире\s+)?"
    r"(?:в|на)\s+(?:наш(?:ей|ем)\s+)?(?:группе|канале)|"
    r"прям(?:ой\s+эфир|ого\s+эфира)\s+(?:в|на)\s+(?:наш(?:ей|ем)\s+)?(?:группе|канале)|"
    r"смотр(?:и|ите)\s+(?:прямую\s+)?трансляци\w*|"
    r"подключа(?:йся|йтесь)\s+(?:к|на)\s+(?:прям\w+\s+)?"
    r"(?:трансляци\w*|эфир\w*)|"
    r"смотрите?\s+(?:здесь|сейчас)|переходи(?:те)?\s+(?:по\s+ссылке\s+)?"
    r"смотр(?:еть|и|ите)\s+(?:прямую\s+)?трансляци\w*|"
    r"find\s+out\s+(?:the\s+)?(?:team\s+)?news\b|"
    r"узнай(?:те)?\s+[^.!?]{0,100}\bпо\s+ссылке\b|watch\s+now|"
    r"click\s+here|join\s+the\s+stream|read\s+more|promo\s*code|промокод)\b",
    re.I,
)
_SOURCE_ENGAGEMENT_RE = re.compile(
    r"^\s*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?\s*)*"
    r"(?:жд[её]м\s+камбэк\b|как\s+вам\b|что\s+думаете\b|"
    r"ваш[еи]?\s+(?:мнения|прогнозы)\b)",
    re.I,
)
_REACTION_POLL_RE = re.compile(
    r"(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?\s*)+"
    r"[—–-]\s*\S+.*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?\s*)+"
    r"[—–-]\s*\S+",
    re.I,
)
_HARD_PROMOTION_RE = re.compile(
    r"\b(?:букмекер|казино|ставка\s+дня|коэффициент|affiliate|реферальн|"
    r"pre-?order|предзаказ|купи(?:ть)?\s+сейчас|buy\s+now)\b",
    re.I,
)
_BETTING_PROMO_RE = re.compile(
    r"\b(?:делай(?:те)?\s+ставк\w*|твоя\s+ставка|ваша\s+ставка|"
    r"выигрыш\w*\s+(?:рассчит|выплат)|досрочно\s+рассчит|"
    r"ограничени\w*\s+по\s+(?:размеру\s+)?ставк|bet\s+now|place\s+(?:a\s+)?bet)\b",
    re.I,
)
_CONTENT_PROMO_RE = re.compile(
    r"\b(?:абсолютно\s+)?все\s+матчи\b[^.!?]{0,120}"
    r"\b(?:смотрим|смотрите|доступн\w*)\b[^.!?]{0,80}\b(?:групп|канал)\w*\b",
    re.I,
)
_BRAND_PITCH_RE = re.compile(
    r"(?:\bконтекст\s+вместо\s+шума\b|"
    r"\bобъясняем\s+причины,?\s+последствия\s+и\s+смысл\s+событий\b|"
    r"\b(?:новости|контекст)\s+без\s+шума\b)",
    re.I,
)
_SIGNATURE_LINE_RE = re.compile(
    r"^(?:main\s+transfers|albion|английский\s+акцент|link|ссылка|video|фото)$",
    re.I,
)
_STORY_BOUNDARY_SIGNATURE_RE = re.compile(
    r"(?:🦁\s*Albion|📝\s*Main\s+Transfers)\b",
    re.I,
)
_FOOTBALL_SIGNAL_RE = re.compile(
    r"\b(?:here\s+we\s+go|football|футбол|player|игрок|club|клуб|team|команд|coach|тренер|manager|"
    r"match|матч|goal|гол|score|сч[её]т|league|лиг|cup|кубок|transfer|трансфер|"
    r"beat|defeat|won|win|victory|scored?|assist|possession|xg|побед|обыграл|"
    r"выиграл|забил|ассист|владени|"
    r"sign(?:ed|ing)?|подпис|join(?:ed|ing)?|переш|переход|loan|аренд|bid|offer|"
    r"предлож|talks?|переговор|contract|контракт|injur|травм|squad|состав|"
    r"goalkeeper|вратар|defender|защитник|midfielder|полузащитник|winger|вингер|"
    r"striker|нападающ|medical|медосмотр|season|сезон|champion|чемпион)\w*\b",
    re.I,
)
_SINGULAR_PLAYER_ROLE = (
    r"(?:player|footballer|goalkeeper|keeper|defender|centre-back|center-back|"
    r"full-back|midfielder|winger|striker|forward|"
    r"игрок(?:а|у|ом|е)?|футболист(?:а|у|ом|е)?|"
    r"вратар(?:ь|я|ю|ём|е)|голкипер(?:а|у|ом|е)?|"
    r"защитник(?:а|у|ом|е)?|полузащитник(?:а|у|ом|е)?|"
    r"вингер(?:а|у|ом|е)?|форвард(?:а|у|ом|е)?|"
    r"нападающ(?:ий|его|ему|им|ем)|капитан(?:а|у|ом|е)?)"
)
_SINGULAR_PLAYER_ROLE_RE = re.compile(rf"\b{_SINGULAR_PLAYER_ROLE}\b", re.I)
_PLAYER_EVENT = (
    r"(?:transfer\w*|sign\w*|join\w*|move\w*|loan\w*|bid\w*|offer\w*|want\w*|"
    r"interest\w*|talk\w*|negotiat\w*|contract\w*|injur\w*|miss\w*|"
    r"suspend\w*|score\w*|medical\w*|said|says|prefer\w*|"
    r"трансфер\w*|переход\w*|перей\w*|подпис\w*|аренд\w*|сделк\w*|"
    r"предлож\w*|интерес\w*|переговор\w*|контракт\w*|травм\w*|"
    r"пропуст\w*|дисквалифиц\w*|забил\w*|медосмотр\w*|куп\w*|прод\w*|"
    r"хоч\w*|сказал\w*|заявил\w*|предпоч\w*)"
)
_PLAYER_EVENT_RE = re.compile(rf"\b{_PLAYER_EVENT}\b", re.I)
_PERSON_WORD = (
    r"(?:[A-ZÀ-ÖØ-ÞА-ЯЁ][A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’.-]{2,}|"
    r"[A-ZА-ЯЁ]{3,})"
)
_PERSON_SPAN = rf"{_PERSON_WORD}(?:\s+{_PERSON_WORD}){{0,3}}"
_TITLECASE_PERSON_WORD = r"[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё'’.-]{2,}"
_TITLECASE_PERSON_RE = re.compile(
    rf"(?<![\w])(?P<person>{_TITLECASE_PERSON_WORD}"
    rf"(?:\s+{_TITLECASE_PERSON_WORD}){{0,3}})(?![\w])"
)
_ROLE_THEN_PERSON_RE = re.compile(
    rf"\b(?i:{_SINGULAR_PLAYER_ROLE})\b\s+"
    rf"(?:(?i:из|from)\s+)?(?P<person>{_PERSON_SPAN})",
)
_PERSON_THEN_EVENT_RE = re.compile(
    rf"(?<![\w])(?P<person>{_PERSON_SPAN})(?![\w])"
    rf"(?:\s+\w+){{0,4}}\s+\b(?i:{_PLAYER_EVENT})\b",
)
_EVENT_THEN_PERSON_RE = re.compile(
    rf"\b(?i:{_PLAYER_EVENT})\b(?:\s+\w+){{0,5}}\s+"
    rf"(?i:for|по|за|на|в\s+отношении)\s+(?P<person>{_PERSON_SPAN})(?![\w])",
)
_TARGET_PERSON_RE = re.compile(
    rf"\b(?i:(?:target|цель|кандидат)\w*)\b(?:\s+\w+){{0,3}}\s+"
    rf"(?P<person>{_PERSON_SPAN})(?![\w])",
)
_QUOTED_SPAN_RE = re.compile(r"[«\"]([^»\"\n]{2,80})[»\"]")
_SOURCE_SIGNATURE_ANYWHERE_RE = re.compile(
    r"(?:🦁\s*)?Albion\b|(?:📝\s*)?Main\s+Transfers\b",
    re.I,
)
_REPORTER_PREFIX_RE = re.compile(
    rf"^\s*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?\s*)*"
    rf"{_PERSON_SPAN}\s*:\s*",
)
_REPORTER_ATTRIBUTION_RE = re.compile(
    rf"\b(?i:according\s+to|по\s+данным)\s+{_PERSON_SPAN}\s*,?\s*|"
    rf"(?<![\w]){_PERSON_SPAN}\s+(?i:reports?|сообща\w*)\s*[:,]?\s*"
)
_NON_PERSON_CAPITALS = frozenset(
    {
        "breaking", "official", "source", "story", "deal", "fee", "personal",
        "premier league", "champions league", "europa league", "world cup",
        "сумма", "трансфер", "сделка", "источник", "официально",
        "тот", "этот", "новый", "новая", "новое", "свободный", "аренда",
        "лига", "лиги", "апл", "чм",
    }
)
_NON_FOOTBALL_RE = re.compile(
    r"\b(?:гардероб|личн(?:ый|ая|ое)\s+стилист|celebrity\s+gossip|"
    r"политическ(?:ий|ая|ое)\s+митинг|криптовалют|косметик)\w*\b",
    re.I,
)
_META_PREFIX_RE = re.compile(
    r"^(?:вот\s+(?:исправленный|готовый)\s+(?:вариант|текст)|готовый\s+текст|"
    r"конечно|перевод)\s*:\s*",
    re.I,
)
_SOURCE_VISUAL_SEMANTICS_RE = re.compile(
    r"\b(?:photos?|pictured|pictures?|images?|фото|сним(?:ок|ке|ка|ки|ков)|"
    r"изображени(?:е|я|и|й))\b",
    re.I,
)
_UNSUPPORTED_VISUAL_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+|\n+)(?:на\s+(?:фото|снимке|изображении))\b"
    r"[^.!?]*(?:[.!?]+|$)",
    re.I,
)
_SOURCE_ATTRIBUTION_RE = re.compile(
    r"\b(?:sources?|according\s+to|reports?|reported|сообща(?:ет|ют|ется)|"
    r"по\s+данным|источник(?:и|ов)?)\b",
    re.I,
)
_GENERIC_ATTRIBUTION_WRAPPER_RE = re.compile(
    r"(?P<prefix>^|(?<=[.!?])\s+|\n+)"
    r"(?:источники\s+сообщают(?:\s+о\s+том)?|по\s+данным\s+источников|"
    r"как\s+сообщают\s+источники)\s*,?\s*(?:что\s+)?",
    re.I,
)
_MASCULINE_CLUB_SUBJECTS = (
    "Атлетико Минеиро",
    "Манчестер Сити",
    "Манчестер Юнайтед",
    "Ньюкасл",
    "Интер",
    "Арсенал",
    "Ливерпуль",
    "Тоттенхэм",
    "Челси",
)
_FEMININE_CLUB_SUBJECTS = (
    "Бенфика",
    "Барселона",
    "Бавария",
    "Астон Вилла",
)


def _strip_decorative_edges(value: str) -> str:
    chars = list(value.strip())
    while chars and (
        unicodedata.category(chars[0]) in {"So", "Sk", "Mn"}
        or chars[0] in "|•·"
    ):
        chars.pop(0)
    while chars and (
        unicodedata.category(chars[-1]) in {"So", "Sk", "Mn"}
        or chars[-1] in "|•·"
    ):
        chars.pop()
    return "".join(chars).strip(" -—–|\t")


def clean_simple_source_text(text: str) -> str:
    """Remove channel garbage without rewriting the football news itself."""

    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = _URL_RE.sub(" ", value)
    value = _BARE_URL_RE.sub(" ", value)
    # This source handle is a club in the sentence, not a channel signature.
    # Resolve it before generic mention removal so Qwen retains the subject.
    value = re.sub(r"(?<!\w)@CPFC\b", "Crystal Palace", value, flags=re.I)
    for handle, name in (("ChelseaFC", "Chelsea"), ("BHAFC", "Brighton"),
                         ("CoventryCityFC", "Coventry City")):
        value = re.sub(rf"(?<!\w)@{handle}\b", name, value, flags=re.I)
    value = re.sub(r"#ThreeLions\b", "England national team", value, flags=re.I)
    value = _HANDLE_RE.sub(" ", value)
    value = _HASHTAG_RE.sub(" ", value)
    # Aggregator sign-offs are metadata, never a club/entity fact.  They are
    # frequently appended to the final news sentence rather than put on a
    # dedicated line.
    value = _STORY_BOUNDARY_SIGNATURE_RE.sub(" ", value)
    kept: list[str] = []
    for raw_line in value.splitlines():
        line = _strip_decorative_edges(raw_line)
        if not line or _SIGNATURE_LINE_RE.fullmatch(line):
            continue
        if (
            _CTA_RE.search(line)
            or _HARD_PROMOTION_RE.search(line)
            or _SOURCE_ENGAGEMENT_RE.search(line)
            or _REACTION_POLL_RE.search(line)
        ):
            # Keep a football sentence before an appended CTA on the same line.
            fragments = re.split(r"(?<=[.!?])\s+", line)
            fragments = [
                part for part in fragments
                if not _CTA_RE.search(part)
                and not _HARD_PROMOTION_RE.search(part)
                and not _SOURCE_ENGAGEMENT_RE.search(part)
                and not _REACTION_POLL_RE.search(part)
            ]
            line = " ".join(fragments).strip()
        if line:
            kept.append(line)
    value = "\n".join(kept)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return normalize_source_language(value).clean_text


def _without_non_player_names(text: str) -> str:
    """Remove source labels and club surfaces before looking for a player name."""

    value = _REPORTER_PREFIX_RE.sub(" ", text)
    value = _REPORTER_ATTRIBUTION_RE.sub(" ", value)
    value = _SOURCE_SIGNATURE_ANYWHERE_RE.sub(" ", value)
    value = _QUOTED_SPAN_RE.sub(" ", value)
    for club in club_entries():
        surfaces = (*club.aliases, *club.forms.values())
        for surface in sorted(set(surfaces), key=len, reverse=True):
            value = re.sub(
                rf"(?<![\w]){re.escape(surface)}(?![\w])",
                " ",
                value,
                flags=re.I,
            )
    return re.sub(r"\s+", " ", value).strip()


def _named_person_match(match: re.Match[str]) -> bool:
    value = re.sub(r"\s+", " ", match.group("person")).strip(" .,:;()")
    if not value or value.casefold() in _NON_PERSON_CAPITALS:
        return False
    tokens = value.split()
    return not all(
        _PLAYER_EVENT_RE.fullmatch(token)
        or _SINGULAR_PLAYER_ROLE_RE.fullmatch(token)
        or token.casefold() in _NON_PERSON_CAPITALS
        for token in tokens
    )


def source_requires_named_player(text: str) -> bool:
    """True when a player-specific story relies on an unnamed person in an image."""

    if not _SINGULAR_PLAYER_ROLE_RE.search(text) or not _PLAYER_EVENT_RE.search(text):
        return False
    searchable = _without_non_player_names(text)
    if any(_named_person_match(match) for match in _TITLECASE_PERSON_RE.finditer(searchable)):
        return False
    return not any(
        _named_person_match(match)
        for pattern in (
            _ROLE_THEN_PERSON_RE,
            _PERSON_THEN_EVENT_RE,
            _EVENT_THEN_PERSON_RE,
            _TARGET_PERSON_RE,
        )
        for match in pattern.finditer(searchable)
    )


def assess_simple_source(text: str) -> SimpleSourceDecision:
    problem = source_problem(text)
    if problem:
        return SimpleSourceDecision(False, text.strip(), problem)
    signature_boundaries = tuple(_STORY_BOUNDARY_SIGNATURE_RE.finditer(text))
    cleaned = clean_simple_source_text(text)
    if not cleaned:
        return SimpleSourceDecision(False, "", SKIP_PROMOTIONAL)
    if source_has_live_broadcast_promotion(text):
        return SimpleSourceDecision(False, cleaned, SKIP_PROMOTIONAL)
    if source_is_award_voting_promotion(text):
        return SimpleSourceDecision(False, cleaned, SKIP_PROMOTIONAL)
    if source_is_audience_commentary(text):
        return SimpleSourceDecision(False, cleaned, SKIP_AUDIENCE_COMMENTARY)
    if len(signature_boundaries) > 1:
        return SimpleSourceDecision(False, cleaned, SKIP_MULTI_STORY)
    if _BRAND_PITCH_RE.search(text):
        return SimpleSourceDecision(False, cleaned, SKIP_PROMOTIONAL)
    if _BETTING_PROMO_RE.search(text) or _CONTENT_PROMO_RE.search(text):
        return SimpleSourceDecision(False, cleaned, SKIP_PROMOTIONAL)
    if _HARD_PROMOTION_RE.search(text) and not _FOOTBALL_SIGNAL_RE.search(cleaned):
        return SimpleSourceDecision(False, cleaned, SKIP_PROMOTIONAL)
    if _NON_FOOTBALL_RE.search(cleaned) or not _FOOTBALL_SIGNAL_RE.search(cleaned):
        return SimpleSourceDecision(False, cleaned, SKIP_NON_FOOTBALL)
    if routine_matchday_without_news(cleaned):
        return SimpleSourceDecision(False, cleaned, SKIP_LOW_INFORMATION)
    if source_requires_named_player(cleaned):
        return SimpleSourceDecision(False, cleaned, SKIP_UNNAMED_PLAYER)
    return SimpleSourceDecision(True, cleaned)


def tiny_source_aware_cleanup(source_text: str, generated_text: str) -> str:
    """Remove three narrow 9B artifacts without interpreting football facts."""

    value = generated_text.strip()
    if not _SOURCE_VISUAL_SEMANTICS_RE.search(source_text):
        value = _UNSUPPORTED_VISUAL_SENTENCE_RE.sub(" ", value)
    if not _SOURCE_ATTRIBUTION_RE.search(source_text):
        value = _GENERIC_ATTRIBUTION_WRAPPER_RE.sub(
            lambda match: match.group("prefix"), value
        )
    for club in _MASCULINE_CLUB_SUBJECTS:
        value = re.sub(
            rf"(?<![\w])(?:[«\"])?({re.escape(club)})(?:[»\"])?\s+согласовали\b",
            rf"\1 согласовал",
            value,
            flags=re.I,
        )
    for club in _FEMININE_CLUB_SUBJECTS:
        value = re.sub(
            rf"(?<![\w])(?:[«\"])?({re.escape(club)})(?:[»\"])?\s+согласовали\b",
            rf"\1 согласовала",
            value,
            flags=re.I,
        )
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def sanitize_simple_publication_text(
    text: str,
    *,
    source_name: str = "",
    max_chars: int = 900,
) -> str:
    """Light deterministic cleanup; deliberately performs no semantic parsing."""

    value = text.strip().strip("`").strip()
    if value.casefold().startswith("markdown\n"):
        value = value[9:].lstrip()
    value = _META_PREFIX_RE.sub("", value)
    value = REACTION_TAIL.sub("", value)
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = _URL_RE.sub("", value)
    value = _HANDLE_RE.sub("", value)
    value = _HASHTAG_RE.sub("", value)
    paragraphs = re.split(r"\n\s*\n", value)
    cleaned_paragraphs = []
    for paragraph in paragraphs:
        sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", paragraph)
        if part.strip()
        and not _CTA_RE.search(part)
        and not _HARD_PROMOTION_RE.search(part)
        and not _SOURCE_ENGAGEMENT_RE.search(part)
        and not _REACTION_POLL_RE.search(part)
        ]
        if sentences:
            cleaned_paragraphs.append(" ".join(sentences))
    value = "\n\n".join(cleaned_paragraphs)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    value = re.sub(r"[ \t]{2,}", " ", value).strip(" -—–|,:;")
    value = normalize_editorial_text(value, source_name)
    value = re.sub(r"\s*[—–]\s*", " - ", value)
    # Preserve the canonical blank line before the deterministic source block.
    # ``\s`` also matches newlines, so using it here used to collapse
    # ``\n\nИсточник: ...`` back into the final body paragraph.
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        raise ValueError("SIMPLE_EDITOR_EMPTY_OUTPUT")
    if len(value) > max_chars:
        raise ValueError("SIMPLE_EDITOR_OUTPUT_TOO_LONG")
    if len(re.findall(r"[А-Яа-яЁё]", value)) < 5:
        raise ValueError("SIMPLE_EDITOR_NOT_RUSSIAN")
    return value


SIMPLE_EDITOR_SYSTEM_PROMPT = """Ты — информационно-точный русскоязычный редактор футбольных новостей PremWire.

1. Используй только SOURCE и никогда не добавляй контекст из памяти.
2. Сохраняй людей, клубы, числа и направление отношений: кто что сделал кому.
3. Никогда не усиливай степень уверенности сообщения.
4. Неуверенную второстепенную деталь опусти, а не интерпретируй.
5. Пиши естественно, содержательно и грамотно по-русски, без воды и повторов.
6. Не добавляй комментарии, объяснения, CTA, ссылки или новые факты.
7. Не превращай разговорный заголовок вроде «игрок и клуб — всё» в юридический факт о разрыве контракта.
8. Срок аренды называй сроком аренды, а не сроком контракта.
9. Не угадывай хозяев/гостей, причину или механику эпизода, если SOURCE не формулирует это однозначно.
10. Перед именем не вставляй пустые «игрок», «футболист», «спортсмен» или «представитель клуба». Эти слова допустимы, когда они нужны для смысла или связности.
11. Клуб, позицию и другие уточнения бери только из SOURCE; если их нет, сделай фразу короче.
12. Перед понятным названием команды обычно опускай пустые «клуб», «команда» и «футбольный клуб»: пиши «связалась с „Брюгге“», «переговоры с „Челси“», «интерес со стороны „Тоттенхэма“», «перешёл из „Челси“», «матч против „Арсенала“». Не удаляй эти слова, когда они самостоятельно нужны для смысла, например: «Клуб подтвердил уход игрока».
13. Служебные части иностранных названий Club, FC, AFC, CF и SC опускай только при безопасном известном русском названии, очевидном из SOURCE: Club Brugge — «Брюгге». Не угадывай и не придумывай другое название клуба.
14. Имена людей из латинского SOURCE передавай кириллицей: Jamie Bynoe-Gittens — «Джейми Байно-Гиттенс». Не оставляй имя или фамилию латиницей и не заменяй человека другим.
15. Используй все важные факты, явно содержащиеся в SOURCE: основной факт, числа, условия, статус, цитату или контекст события. Не своди насыщенный источник к одному заголовку.
16. Если SOURCE содержит несколько поддержанных деталей, напиши 2–4 предложения и раздели их на 2–3 коротких абзаца. Если SOURCE сообщает только один факт, достаточно одного сильного предложения: не растягивай его общими словами и не добавляй знания из памяти.
17. Не превращай предматчевый анонс, стартовый состав или live-эпизод в сыгранный матч либо финальный результат.
18. Не добавляй имя, национальность, клубную принадлежность, серию «подряд» или источник сообщения, которых нет в SOURCE. Явную атрибуцию SOURCE сохраняй.
19. В трансферах сохраняй точные суммы бонусов, процент будущей продажи и условие обязательного выкупа, если соответствующую деталь упоминаешь в OUTPUT.
20. Half-time — перерыв, level — равный текущий счёт, as it stands — предварительное положение. Не называй их итогом. «Забивал против всех клубов» не означает «забивал в каждом матче».
21. Не считывай числа или выводы из упомянутой картинки/графика: ты получаешь только текст. «Показал» не превращай в «заявил». Не достраивай неполные имена.
22. Сохраняй возрастную и женскую категорию сборной и направление пары соперников. Название соревнования не переводи дословно: League One — «Лига 1». As number 9 — позиция нападающего, если речь о роли на поле; initial approaches made — контакты уже состоялись.
23. Для насыщенного источника ориентируйся на 350–650 знаков и 3–4 предложения в двух абзацах: новость, затем конкретные условия, статистика или содержательная цитата. Это ориентир, не минимум. Короткий источник не растягивай; второе предложение не должно повторять первое.
24. Не генерируй блок «Источник»: его добавляет программа. Не переноси вопросы читателям, «наш клуб», «смотрим здесь» и рекламные анонсы. Has been offered to — игрока предложили клубу; first start — первый выход в стартовом составе, не первый матч; year — год, не матч. Не додумывай следующий тур и способ забитого гола.
Пример: не «интерес к подписанию игрока Балогуна», а «интерес к подписанию Балогуна» или «Барселона интересуется Балогуном».

Верни только готовый текст PremWire без Markdown."""


SIMPLE_EDITOR_USER_INSTRUCTION = """Перепиши SOURCE в содержательный пост PremWire.
Если SOURCE содержит несколько фактов, используй 2–4 ясных предложения в 2–3 коротких абзацах: сначала главный факт, затем важные детали и поддержанный SOURCE контекст. Сохрани все значимые числа, условия и статус события.
Если в SOURCE только один факт, напиши одно сильное предложение без воды. Ничего не объясняй, не повторяй и не добавляй из памяти."""


SIMPLE_EDITOR_REPAIR_SYSTEM_PROMPT = """Ты исправляешь фактический дефект русского текста.
SOURCE — единственный авторитет. Исправь только перечисленные нарушения, сохрани остальной удачный текст. Не добавляй информацию. Если деталь нельзя исправить точно, опусти её. Верни только исправленный текст."""


__all__ = [
    "SIMPLE_EDITOR_SYSTEM_PROMPT",
    "SIMPLE_EDITOR_REPAIR_SYSTEM_PROMPT",
    "SIMPLE_EDITOR_USER_INSTRUCTION",
    "SKIP_MULTI_STORY",
    "SKIP_NON_FOOTBALL",
    "SKIP_PROMOTIONAL",
    "SKIP_UNNAMED_PLAYER",
    "SimpleSourceDecision",
    "assess_simple_source",
    "clean_simple_source_text",
    "sanitize_simple_publication_text",
    "source_requires_named_player",
    "tiny_source_aware_cleanup",
]
