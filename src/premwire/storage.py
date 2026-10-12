from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Sequence

from .news_priority import is_immediate_news
from .source_language import normalize_source_language


_EDITORIAL_PRIORITY_SQL = """
CASE WHEN editorial_priority IS NULL THEN 1 ELSE 0 END,
COALESCE(editorial_priority, 0) DESC
"""

_SOURCE_PRIORITY_SQL = """
CASE WHEN source_id IS NULL THEN 1 ELSE 0 END
"""

from .models import IncomingItem, IngestOutcome, IngestResult, utc_now


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_DEDUPE_WINDOW = timedelta(hours=48)
_STORY_WINDOW = timedelta(days=7)
_EVENT_COOLDOWN = timedelta(hours=24)
_NON_CANONICAL_DEDUPE_STATES = (
    "baseline",
    "rejected_advertising",
    "rejected_low_information",
    "rejected_low_priority",
    "rejected_quality",
    "rejected_manual",
    "cancelled_source_deleted",
    "deleted_source",
)
_DEDUPE_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "with",
    "albion",
    "main",
    "mtransfers",
    "football",
    "news",
    "а",
    "без",
    "был",
    "была",
    "были",
    "в",
    "во",
    "для",
    "до",
    "за",
    "и",
    "из",
    "к",
    "как",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "под",
    "после",
    "при",
    "про",
    "с",
    "со",
    "у",
    "футбол",
    "это",
}

_TRANSFER_STORY_MARKERS = (
    "signed", "signing", "contract", "deal", "agreement", "completed",
    "medical", "move from", "move to", "officially", "joins", "joined",
    "перешел", "перешёл", "подписал", "подписание", "контракт", "завершен",
    "завершён", "сделк", "соглашение", "медосмотр", "официально",
)
_RUMOUR_STORY_MARKERS = (
    "interest", "interested", "exploring", "approached", "radar", "target",
    "could make a move", "would consider leaving", "would consider departure",
    "ready to let leave", "ready to sell",
    "интерес", "следит", "цель", "может подписать", "может приобрести",
    "рассматривает", "рассматривали уход", "вариант ухода", "готов отпустить",
)
_TRANSFER_STAGE_MARKERS = (
    (4, ("official", "officially", "has joined", "have signed", "has signed",
         "confirm ", "confirmed ", "официально", "объявил о подписании",
         "подтвердил", "перешел", "перешёл")),
    (3, ("here we go", "deal agreed", "agree deal", "agreement done",
         "verbal agreement", "согласовал", "договорил", "соглашение",
         "купил", "купила", "купили", "приобрел", "приобрёл", "приобрела")),
    (2, ("talks", "negotiations", "in contact", "переговор", "контактирует")),
    (1, _RUMOUR_STORY_MARKERS),
)
_TRANSFER_REJECTION_MARKERS = (
    "rejected",
    "turned down",
    "no breakthrough",
    "no agreement",
    "failed to reach agreement",
    "deal fell through",
    "move fell through",
    "no longer considering",
    "no longer consider",
    "not viable",
    "отклонено",
    "отклонён",
    "отклонен",
    "не смогли добиться прогресса",
    "не завершились соглашением",
    "не достигли соглашения",
    "сделка сорвалась",
    "переход сорвался",
    "больше не рассматривает",
    "больше не рассматривают",
    "приостановил переговоры",
    "приостановили переговоры",
)

# This is deliberately a small source-language normalizer, not an entity registry.
# It only covers safe aliases needed to compare story participants locally.
_CLUB_ALIASES = {
    "chelsea": ("chelsea", "челси"),
    "napoli": ("napoli", "наполи"),
    "arsenal": ("arsenal", "арсенал"),
    "manchester_city": (
        "manchester city", "man city", "манчестер сити", "ман сити",
    ),
    "newcastle_united": (
        "newcastle united", "newcastle", "nufc", "ньюкасл юнайтед", "ньюкасл",
    ),
    "atletico_madrid": (
        "atletico madrid", "atletico", "атлетико мадрид", "атлетико",
    ),
    "barcelona": (
        "barcelona", "барселона", "барсы", "барсу", "барсе", "барсой",
    ),
    "aston_villa": (
        "aston villa", "астон вилла", "астон виллы", "астон виллу",
        "астон вилле", "астон виллой", "астон виллe",
    ),
    "olympiacos": ("olympiacos", "олимпиакос"),
    "chivas": ("chivas", "guadalajara", "чивас", "гвадалахар"),
    "leeds_united": ("leeds united", "leeds", "lufc", "лидс юнайтед", "лидс"),
    "nottingham_forest": (
        "nottingham forest", "nott m forest", "nottm forest", "ноттингем форест",
    ),
    "notts_county": ("notts county", "ноттс каунти"),
    "trabzonspor": ("trabzonspor", "трабзонспор"),
    "al_ittihad": ("al ittihad", "al-ittihad", "аль иттихад", "аль-иттихад"),
    "palmeiras": ("palmeiras", "палмейрас"),
    "liverpool": ("liverpool", "ливерпуль"),
    "manchester_united": (
        "manchester united", "man united", "man utd", "mufc",
        "манчестер юнайтед", "ман юнайтед", "мю",
    ),
    "real_madrid": ("real madrid", "реал мадрид"),
    "bayern_munich": ("bayern munich", "bayern", "бавария"),
    "paris_saint_germain": (
        "paris saint germain", "paris saint-germain", "psg", "псж",
    ),
    "inter_milan": ("inter milan", "internazionale", "интер"),
    "tottenham_hotspur": (
        "tottenham hotspur", "tottenham", "spurs", "тоттенхэм",
    ),
    "fulham": ("fulham", "фулхэм"),
    "west_ham": ("west ham united", "west ham", "вест хэм"),
    "crystal_palace": ("crystal palace", "кристал пэлас", "cpfc"),
    "everton": ("everton", "эвертон"),
    "brighton": (
        "brighton and hove albion", "brighton & hove albion", "brighton",
        "брайтон энд хоув альбион", "брайтон",
    ),
    "wolverhampton": (
        "wolverhampton wanderers", "wolverhampton", "wolves",
        "вулверхэмптон", "вулвз",
    ),
    "bournemouth": ("afc bournemouth", "bournemouth", "борнмут"),
    "brentford": ("brentford", "брентфорд"),
    "ipswich_town": (
        "ipswich town", "ipswich", "ипсвич таун", "ипсвич",
    ),
    "sunderland": ("sunderland", "сандерленд"),
    "juventus": ("juventus", "ювентус"),
    "ac_milan": ("ac milan", "milan", "милан"),
    "roma": ("as roma", "roma", "рома"),
    "borussia_dortmund": (
        "borussia dortmund", "dortmund", "боруссия дортмунд", "дортмунд",
    ),
    "bayer_leverkusen": (
        "bayer leverkusen", "leverkusen", "байер леверкузен", "байер",
    ),
    "marseille": ("olympique marseille", "marseille", "марсель"),
    "monaco": ("as monaco", "monaco", "монако"),
    "club_brugge": ("club brugge", "brugge", "брюгге"),
    "lille": ("losc lille", "losc", "lille", "лилль"),
    "rb_leipzig": ("rb leipzig", "leipzig", "рб лейпциг", "лейпциг"),
    "al_hilal": ("al hilal", "al-hilal", "аль хилаль", "аль-хиляль"),
    "villarreal": ("villarreal", "вильярреал"),
    "burnley": ("burnley", "бернли"),
    "hamburg": ("hamburger sv", "hamburg", "гамбург"),
    "rennes": ("stade rennais", "rennes", "ренн"),
    "atlanta_united": ("atlanta united", "атланта юнайтед"),
    "atalanta": ("atalanta", "аталанта"),
    "lyon": ("olympique lyonnais", "lyon", "лион"),
    "benfica": ("benfica", "бенфика"),
    "sporting": ("sporting cp", "sporting lisbon", "спортинг"),
    "porto": ("fc porto", "porto", "порту"),
    "ajax": ("ajax", "аякс"),
    "galatasaray": ("galatasaray", "галатасарай"),
    "fenerbahce": ("fenerbahce", "fenerbahçe", "фенербахче"),
    "sevilla": ("sevilla", "севилья"),
    "valencia": ("valencia", "валенсия"),
    "lazio": ("lazio", "лацио"),
    "toulouse": ("toulouse", "туулуза", "тулуза"),
    "coventry_city": ("coventry city", "coventry", "ковентри сити", "ковентри"),
    "athletic_bilbao": (
        "athletic bilbao", "athletic club", "атлетик бильбао", "атлетик",
    ),
    "al_sadd": ("al sadd", "al-sadd", "аль садд", "аль-садд"),
    "rangers": ("rangers", "рейнджерс"),
    "portland_timbers": (
        "portland timbers", "portland", "портленд тимберс", "портленд",
    ),
    "dynamo_tbilisi": (
        "dynamo tbilisi", "dinamo tbilisi", "динамо тбилиси",
    ),
    "torino": ("torino", "торино"),
    "real_betis": ("real betis", "betis", "реал бетис", "бетис"),
    "slavia_prague": (
        "slavia prague", "slavia praha", "славия прага", "славия",
    ),
    "colorado_rapids": (
        "colorado rapids", "colorado", "колорадо рэпидз", "колорадо",
    ),
    "middlesbrough": ("middlesbrough", "мидлсбро"),
    "genclerbirligi": ("genclerbirligi", "gençlerbirliği", "генчлербирлиги"),
}
_NON_PLAYER_NAMES = {
    "aston villa", "manchester city", "manchester united", "real madrid",
    "atletico madrid", "athletic club", "crystal palace", "west ham",
    "newcastle united", "nottingham forest", "bayern munich", "borussia dortmund",
    "premier league", "champions league", "europa league", "club world cup",
    "the athletic", "sky sports", "main transfers", "red bull", "world cup",
    "fabrizio romano", "david ornstein", "peppa pig", "the gunners",
    "here we go",
    "фабрицио романо", "дэвид орнштейн", "премьер лига", "лига чемпионов",
}
_NON_PLAYER_NAMES.update(
    alias
    for aliases in _CLUB_ALIASES.values()
    for alias in aliases
)
_NON_PLAYER_NAME_TOKENS = {
    "arsenal", "barcelona", "bayern", "chelsea", "city", "club", "cup",
    "fc", "football", "inter", "league", "link", "liverpool", "madrid",
    "main", "manchester", "milan", "paris", "psg", "real", "saint",
    "friday", "london", "monday", "moscow", "round", "saturday", "sports",
    "stats", "sunday", "the", "thursday", "transfers", "tuesday", "united",
    "villa", "wednesday", "world",
    "арсенал", "барселона", "бавария", "манчестер", "милан", "псж", "реал",
    "сити", "трансферы", "челси",
}
_NAME_LEADING_DESCRIPTORS = {
    "effect", "effekt", "komanda", "london", "novichok", "pokupka", "video",
}
_NON_PERSON_SINGLE_NAMES = {
    "breaking", "confirmed", "exclusive", "official", "source", "transfer",
    "update", "championship", "friday", "match", "matches", "monday",
    "london", "moscow", "round", "saturday", "stats", "sunday", "thursday", "tuesday",
    "watch", "wednesday", "subscribe",
    "источник", "официально", "переход", "срочно", "трансфер", "англичанин",
    "бразилец", "будет", "видео", "вышли", "главный", "команда", "клуб",
    "матч", "матчи", "новичок", "подпишись", "победа", "проблемы", "сегодня",
    "смотри", "смотрим", "теперь", "тренер", "футболист", "чемпионат",
}
_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sh", "ы": "y", "э": "e", "ю": "yu",
        "я": "ya", "ь": "", "ъ": "",
    }
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@lru_cache(maxsize=2048)
def _normalise_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = _URL_RE.sub(" ", value)
    value = "".join(char if char.isalnum() else " " for char in value)
    return _SPACE_RE.sub(" ", value).strip()


def _content_hash(item: IncomingItem) -> str:
    payload = "\n".join([item.text.strip(), item.canonical_url.strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dedupe_hash(text: str) -> str:
    return hashlib.sha256(_normalise_text(text).encode("utf-8")).hexdigest()


@lru_cache(maxsize=2048)
def _dedupe_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in _normalise_text(text).split()
        if token not in _DEDUPE_STOPWORDS and (len(token) > 1 or token.isdigit())
    )


def _is_near_duplicate(first: str, second: str) -> bool:
    first_tokens = _dedupe_tokens(first)
    second_tokens = _dedupe_tokens(second)
    if len(first_tokens) < 4 or len(second_tokens) < 4:
        return False
    first_set = set(first_tokens)
    second_set = set(second_tokens)
    shared = len(first_set & second_set)
    if shared < 4:
        return False
    containment = shared / min(len(first_set), len(second_set))
    jaccard = shared / len(first_set | second_set)
    ordered_similarity = SequenceMatcher(
        None,
        " ".join(first_tokens),
        " ".join(second_tokens),
    ).ratio()
    return (
        ordered_similarity >= 0.86
        or jaccard >= 0.72
        or (containment >= 0.82 and jaccard >= 0.56)
    )


def _story_kind(text: str) -> str | None:
    folded = _normalise_text(text)
    if any(marker in folded for marker in _TRANSFER_STORY_MARKERS):
        return "transfer"
    return None


@lru_cache(maxsize=4096)
def _latin_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = folded.translate(_CYRILLIC_TO_LATIN)
    return _SPACE_RE.sub(" ", "".join(char if char.isalnum() else " " for char in folded)).strip()


@lru_cache(maxsize=2048)
def _player_name_candidates(text: str) -> tuple[str, ...]:
    # Two or three title-cased words are a dependable signal for footballer names
    # in both English source posts and Russian Telegram posts. Club, competition and
    # reporter names are explicitly excluded so two unrelated Chelsea stories do
    # not collapse into one.
    matches = re.findall(
        r"(?<![@#])\b(?:[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё]+)"
        r"(?:\s+[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё]+){1,2}\b",
        text,
    )
    result: list[str] = []
    for match in matches:
        value = _latin_name(match)
        parts = value.split()
        while len(parts) > 2 and parts[0] in _NAME_LEADING_DESCRIPTORS:
            parts.pop(0)
        value = " ".join(parts)
        if value in {_latin_name(name) for name in _NON_PLAYER_NAMES}:
            continue
        if any(part in _NON_PLAYER_NAME_TOKENS for part in value.split()):
            continue
        result.append(value)
    return tuple(dict.fromkeys(result))


@lru_cache(maxsize=2048)
def _single_person_name_candidates(text: str) -> tuple[str, ...]:
    """Find source-local mononyms without introducing an entity registry.

    Some footballers are consistently reported by one name (Fabinho, Neymar,
    Alisson).  The normal full-name extractor intentionally misses them.  A
    token is eligible only when it is title-cased, is not part of a detected
    multi-word name, and is not a known club/editorial/source token.  The
    caller still requires the same transfer participants and factual stage.
    """

    full_spans = tuple(
        match.span()
        for match in re.finditer(
            r"(?<![@#])\b(?:[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё]+)"
            r"(?:\s+[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё]+){1,2}\b",
            text,
        )
    )
    club_tokens = {
        token
        for aliases in _CLUB_ALIASES.values()
        for alias in aliases
        for token in _latin_name(alias).split()
    }
    blocked = (
        _NON_PLAYER_NAME_TOKENS
        | club_tokens
        | {_latin_name(value) for value in _NON_PERSON_SINGLE_NAMES}
    )
    result: list[str] = []
    for match in re.finditer(
        r"(?<![@#\w])((?:[A-ZÀ-ÖØ-ÞА-ЯЁ][a-zà-öø-ÿа-яё]{4,})|"
        r"(?:[A-ZÀ-ÖØ-ÞА-ЯЁ]{5,}))(?![\w])",
        text,
    ):
        if any(start <= match.start(1) < end for start, end in full_spans):
            continue
        value = _latin_name(match.group(1))
        if value in blocked or len(value) < 5:
            continue
        if any(
            SequenceMatcher(
                None,
                _surname_key(value),
                _surname_key(club_token),
            ).ratio() >= 0.94
            for club_token in club_tokens
        ):
            continue
        result.append(value)
    return tuple(dict.fromkeys(result))


def _same_player_name(first: str, second: str) -> bool:
    first_parts = first.split()
    second_parts = second.split()
    if len(first_parts) < 2 or len(second_parts) < 2:
        return False
    surname_similarity = SequenceMatcher(None, first_parts[-1], second_parts[-1]).ratio()
    first_similarity = SequenceMatcher(None, first_parts[0], second_parts[0]).ratio()
    # Russian transliteration can move every vowel (Bradley Barcola ->
    # Брэдли Барколя) while preserving the consonant skeleton.  This
    # remains deliberately strict: both parts must have the same sufficiently
    # long skeleton, and callers still require matching event participants.
    def phonetic_key(value: str) -> str:
        folded = value.casefold().replace("c", "k").replace("q", "k")
        return "".join(char for char in folded if char not in "aeiouy")

    first_phonetic = phonetic_key(first_parts[0])
    second_phonetic = phonetic_key(second_parts[0])
    surname_phonetic = phonetic_key(first_parts[-1])
    other_surname_phonetic = phonetic_key(second_parts[-1])
    return (
        first_similarity >= 0.72 and surname_similarity >= 0.72
    ) or (
        surname_similarity >= 0.84
        and min(len(first_parts[0]), len(second_parts[0])) >= 3
        and (
            first_parts[0].startswith(second_parts[0])
            or second_parts[0].startswith(first_parts[0])
        )
    ) or (
        surname_similarity >= 0.94
        and first_parts[0][:1] == second_parts[0][:1]
        and len(first_parts[-1]) >= 5
    ) or (
        len(first_phonetic) >= 4
        and len(surname_phonetic) >= 4
        and first_phonetic == second_phonetic
        and surname_phonetic == other_surname_phonetic
    )


def _surname_key(value: str) -> str:
    """Fold a safe subset of RU/UA surname inflection for local comparison."""

    folded = value.casefold().replace("y", "i")
    for suffix in ("ami", "yami", "ogo", "emu", "omu", "om", "ym", "im", "a", "u", "e"):
        if folded.endswith(suffix) and len(folded) - len(suffix) >= 5:
            return folded[: -len(suffix)]
    return folded


@lru_cache(maxsize=2048)
def _transfer_stage(text: str) -> int | None:
    folded = _normalise_text(text)
    if any(
        marker in folded
        for marker in (
            "no agreement", "agreement not reached", "agreement has not been reached",
            "соглашения пока нет", "соглашение пока не достигнуто",
            "соглашения не достигнуто",
        )
    ) and any(
        marker in folded
        for marker in ("talks", "negotiations", "переговор", "контакт")
    ):
        return 3
    if any(
        marker in folded
        for marker in (
            "official", "officially", "has joined", "have signed", "has signed",
            "has moved", "have moved", "signed contract", "completed the move",
            "completed the transfer",
            "confirm ", "confirmed ", "официально", "объявил о подписании",
            "подтвердил", "подписал контракт", "перешел", "перешёл",
            "стал игроком", "стал новым игроком", "стала игроком",
        )
    ):
        return 7
    # Source feeds often use the headline shorthand "Name — new player of
    # Club" for an agreed/confirmed move without an explicit club announcement.
    # Keep it at AGREEMENT; only explicit official/contract language advances
    # to OFFICIAL above.
    if (
        "new player" in folded
        or re.search(r"\bнов(?:ый|ым)\s+игрок(?:ом)?\b", folded)
        or re.search(
            r"(?m)^\s*[A-ZА-ЯЁ][^\n.!?]{1,80}\s+[\-—]\s*(?:новый\s+)?игрок\b",
            text,
        )
    ):
        return 5
    if any(
        marker in folded
        for marker in (
            "medical", "medical tests", "undergoing medical", "медосмотр",
            "медицинское обследование", "проходит медосмотр",
        )
    ):
        return 6
    if any(
        marker in folded
        for marker in (
            "set to sign", "will sign", "closing in on", "close to signing",
            "will join", "en route to", "formalise his move", "formalize his move",
            "reach agreement", "reached agreement",
            "завершает подписание", "завершают подписание", "подпишет",
            "близок к подписанию", "близка к подписанию", "переходит", "едет в",
            "оформления перехода", "оформить переход",
        )
    ):
        return 5
    if any(
        marker in folded
        for marker in (
            "here we go", "deal agreed", "agree deal", "agreement done",
            "verbal agreement", "principle agreement", "agreement in principle",
            "reach agreement", "reached agreement", "согласовал", "договорил",
            "устной договоренности", "устной договорённости", "принципиального соглашения",
            "соглашение", "купил", "купила", "купили", "приобрел", "приобрёл",
        )
    ):
        return 5
    if any(
        marker in folded
        for marker in (
            "formal bid", "official bid", "submitted a bid", "made a bid",
            "bid submitted", "formal offer", "официальное предложение",
            "формальное предложение", "направил предложение", "сделал предложение",
        )
    ):
        return 4
    if any(
        marker in folded
        for marker in ("talks", "negotiations", "переговор", "обсуждени")
    ):
        return 3
    if any(
        marker in folded
        for marker in (
            "in contact", "contacted", "approached", "контактирует", "связался",
            "связались", "вышел на связь",
        )
    ):
        return 2
    if any(_normalise_text(marker) in folded for marker in _RUMOUR_STORY_MARKERS):
        return 1
    if any(marker in folded for marker in _TRANSFER_STORY_MARKERS):
        return 0
    return None


@lru_cache(maxsize=2048)
def _club_ids(text: str) -> frozenset[str]:
    folded = _normalise_text(text)
    return frozenset(
        club_id
        for club_id, aliases in _CLUB_ALIASES.items()
        if any(
            re.search(
                rf"(?<!\w){re.escape(_normalise_text(alias))}"
                + (
                    r"(?:а|у|ом|ем|е|ы|и)?"
                    if re.search(r"[а-яё]", _normalise_text(alias))
                    else ""
                )
                + r"(?!\w)",
                folded,
            )
            for alias in aliases
        )
    )


@lru_cache(maxsize=2048)
def _transfer_route(text: str) -> tuple[str, str] | None:
    """Infer (from_club, to_club) only when a source-local preposition is clear."""

    folded = _normalise_text(text)
    clubs = _club_ids(text)
    if len(clubs) != 2:
        return None

    def club_after(markers: tuple[str, ...]) -> str | None:
        for marker in markers:
            marker_at = folded.find(marker)
            if marker_at < 0:
                continue
            tail = folded[marker_at + len(marker):]
            matches: list[tuple[int, str]] = []
            for club_id, aliases in _CLUB_ALIASES.items():
                for alias in aliases:
                    clean_alias = _normalise_text(alias)
                    suffix = (
                        r"(?:а|у|ом|ем|е|ы|и)?"
                        if re.search(r"[а-яё]", clean_alias)
                        else ""
                    )
                    alias_match = re.search(
                        rf"(?<!\w){re.escape(clean_alias)}{suffix}(?!\w)",
                        tail,
                    )
                    if alias_match is not None:
                        matches.append((alias_match.start(), club_id))
            if matches:
                return min(matches)[1]
        return None

    from_club = club_after((" from ", " из ", " у "))
    to_club = club_after((" to ", " в "))
    if from_club and not to_club:
        to_club = next(iter(clubs - {from_club}), None)
    if to_club and not from_club:
        from_club = next(iter(clubs - {to_club}), None)
    if from_club and to_club and from_club != to_club:
        return from_club, to_club
    return None


@lru_cache(maxsize=2048)
def _transfer_destination_ids(text: str) -> frozenset[str]:
    """Extract only explicit source-local destination roles for one-club copy."""

    folded = _normalise_text(text)
    destinations: set[str] = set()
    for club_id, aliases in _CLUB_ALIASES.items():
        for alias in aliases:
            club = re.escape(_normalise_text(alias))
            if re.search(
                rf"(?:\bto|\binto|\bjoin(?:s|ed)?|\bв|\bигрок(?:ом)?|"
                rf"\bфутболист(?:ом)?)\s+(?:fc\s+|club\s+)?{club}",
                folded,
            ) or re.search(
                rf"{club}\w*\s+(?:has\s+signed|have\s+signed|signed|signs|"
                rf"подписал[аи]?|оформил[аи]?|купил[аи]?|приобр[её]л[аи]?|"
                rf"договорил(?:ся|ись)\s+о\s+переходе|"
                rf"заверша(?:ет|ют)\s+подписание)\b",
                folded,
            ):
                destinations.add(club_id)
                break
    return frozenset(destinations)


@lru_cache(maxsize=4096)
def _shared_player(first: str, second: str) -> bool:
    first_names = _player_name_candidates(first)
    second_names = _player_name_candidates(second)
    if any(
        _same_player_name(first_name, second_name)
        for first_name in first_names
        for second_name in second_names
    ):
        return True

    first_single = _single_person_name_candidates(first)
    second_single = _single_person_name_candidates(second)
    if any(
        SequenceMatcher(
            None,
            _surname_key(first_name),
            _surname_key(second_name),
        ).ratio() >= 0.90
        for first_name in first_single
        for second_name in second_single
    ):
        return True

    # Some feeds put a full name only in an all-caps headline and then use just
    # the inflected surname in the body. Match that surname against a full name
    # from the other source, but never compare two surname-only stories. Club
    # overlap and transfer stage are still required by the caller.
    def surname_visible(
        names: tuple[str, ...],
        other_names: tuple[str, ...],
    ) -> bool:
        return any(
            SequenceMatcher(
                None,
                _surname_key(name.split()[-1]),
                _surname_key(other_name),
            ).ratio() >= 0.92
            for name in names
            for other_name in other_names
        )

    return surname_visible(first_names, second_single) or surname_visible(
        second_names, first_single
    )


def _transfer_rejected(text: str) -> bool:
    folded = _normalise_text(text)
    return any(_normalise_text(marker) in folded for marker in _TRANSFER_REJECTION_MARKERS)


def _transfer_story_duplicate(first: str, second: str) -> bool:
    """Return whether incoming *first* repeats stored *second* at the same stage."""

    if not _shared_player(first, second):
        return False
    first_clubs = _club_ids(first)
    second_clubs = _club_ids(second)
    # A third club may be contextual (for example, a ruled-out alternative).
    # Two shared route participants are enough; a genuinely different
    # destination normally shares only the selling club and remains allowed.
    shared_clubs = first_clubs & second_clubs
    if len(shared_clubs) < 2:
        # One source can name only the destination while another also gives
        # the selling club.  Allow that source-local omission, but never merge
        # two complete routes which merely share the selling club.
        if len(shared_clubs) != 1 or (
            len(first_clubs) >= 2 and len(second_clubs) >= 2
        ):
            return False
        first_destinations = _transfer_destination_ids(first)
        second_destinations = _transfer_destination_ids(second)
        if not (shared_clubs & first_destinations & second_destinations):
            return False
    first_route = _transfer_route(first)
    second_route = _transfer_route(second)
    if first_route and second_route and first_route != second_route:
        return False
    first_rejected = _transfer_rejected(first)
    second_rejected = _transfer_rejected(second)
    if first_rejected or second_rejected:
        # Rejection/failure is a separate route outcome. Repeated rejection is
        # one story, while talks -> rejection (or a genuine revival) remains a
        # publishable development.
        return first_rejected and second_rejected
    first_stage = _transfer_stage(first)
    second_stage = _transfer_stage(second)
    if first_stage is None or second_stage is None:
        return False
    # A later factual stage is a real development. Equal or regressive reports
    # repeat what the feed has already published.
    return first_stage <= second_stage


def _match_stage(text: str) -> int | None:
    folded = _normalise_text(text)
    if re.search(r"\b\d+\s+\d+\b", folded) and any(
        marker in folded
        for marker in (
            " win ", " won ", " beat ", " beaten ", " defeated ",
            " victory ", " full time ", "побед", "одержал",
            "финальный свисток",
        )
    ):
        return 2
    if any(marker in folded for marker in ("minute", "lead", "scores", "scored", "минут", "забил", "вывел")):
        return 1
    return None


def _match_story_duplicate(first: str, second: str) -> bool:
    """Compare a match result with a later retelling of its key event."""

    first_stage = _match_stage(first)
    second_stage = _match_stage(second)
    if first_stage is None or second_stage is None:
        return False
    first_clubs = _club_ids(first)
    second_clubs = _club_ids(second)
    if len(first_clubs) < 2 or first_clubs != second_clubs:
        return False
    # Scores must be extracted before punctuation-stripping normalization.
    # A changed score is a development/correction, not a semantic duplicate.
    first_scores = frozenset(_SCORE_VALUE_RE.findall(first))
    second_scores = frozenset(_SCORE_VALUE_RE.findall(second))
    if first_scores and second_scores and first_scores != second_scores:
        return False
    if _GOAL_EVENT_RE.search(first) and _GOAL_EVENT_RE.search(second):
        if bool(_MULTI_GOAL_DEVELOPMENT_RE.search(first)) != bool(
            _MULTI_GOAL_DEVELOPMENT_RE.search(second)
        ):
            return False
        first_minutes = frozenset(_MINUTE_VALUE_RE.findall(_normalise_text(first)))
        second_minutes = frozenset(_MINUTE_VALUE_RE.findall(_normalise_text(second)))
        if first_minutes and second_minutes and first_minutes != second_minutes:
            return False
    return first_stage <= second_stage


_GOAL_EVENT_RE = re.compile(
    r"\b(?:goal|goals|scor(?:e|ed|es)|забил(?:а|и)?|гол(?:а|ом|ы)?)\b",
    re.IGNORECASE,
)
_MULTI_GOAL_DEVELOPMENT_RE = re.compile(
    r"\b(?:second\s+goal|brace|hat[ -]?trick|дубл[ья]|хет[ -]?трик|"
    r"втор(?:ой|ым)\s+гол)\b",
    re.IGNORECASE,
)
_PREDICTION_RE = re.compile(
    r"\b(?:predict(?:s|ed|ion)?|tip(?:s|ped)?|champion|title|"
    r"прогноз\w*|уверен\w*|чемпион\w*|титул\w*)\b",
    re.IGNORECASE,
)
_MINUTE_VALUE_RE = re.compile(
    r"(?<!\d)(\d{1,3})(?:st|nd|rd|th)?\s*(?:minute|min|минут)",
    re.IGNORECASE,
)
_SCORE_VALUE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:\-–—]\s*(\d{1,2})(?!\d)")


def _short_window_story_duplicate(first: str, second: str) -> bool:
    """Catch an immediate retelling when one source omits match context.

    The caller limits this rule to twelve hours. It is intentionally not a
    seven-day one-player lock and does not introduce an entity registry.
    """

    if not _shared_player(first, second):
        return False
    first_clubs = _club_ids(first)
    second_clubs = _club_ids(second)
    if first_clubs and second_clubs and first_clubs.isdisjoint(second_clubs):
        return False

    if _GOAL_EVENT_RE.search(first) and _GOAL_EVENT_RE.search(second):
        if bool(_MULTI_GOAL_DEVELOPMENT_RE.search(first)) != bool(
            _MULTI_GOAL_DEVELOPMENT_RE.search(second)
        ):
            return False
        first_minutes = frozenset(_MINUTE_VALUE_RE.findall(_normalise_text(first)))
        second_minutes = frozenset(_MINUTE_VALUE_RE.findall(_normalise_text(second)))
        if first_minutes and second_minutes and first_minutes != second_minutes:
            return False
        first_scores = frozenset(_SCORE_VALUE_RE.findall(first))
        second_scores = frozenset(_SCORE_VALUE_RE.findall(second))
        if first_scores and second_scores and first_scores != second_scores:
            return False
        return True

    return bool(_PREDICTION_RE.search(first) and _PREDICTION_RE.search(second))


def _is_meaningful_story_development(first: str, second: str) -> bool:
    """Protect a real stage/route development from broad near-text dedupe."""

    first_scores = frozenset(_SCORE_VALUE_RE.findall(first))
    second_scores = frozenset(_SCORE_VALUE_RE.findall(second))
    if (
        first_scores and second_scores and first_scores != second_scores
        and _match_stage(first) is not None and _match_stage(second) is not None
        and len(_club_ids(first)) == 2 and _club_ids(first) == _club_ids(second)
    ):
        return True
    shared_player = _shared_player(first, second)
    first_rejected = _transfer_rejected(first)
    second_rejected = _transfer_rejected(second)
    if shared_player and first_rejected != second_rejected:
        return True
    first_transfer = _transfer_stage(first)
    second_transfer = _transfer_stage(second)
    if first_transfer is not None and second_transfer is not None and shared_player:
        first_clubs = _club_ids(first)
        second_clubs = _club_ids(second)
        first_route = _transfer_route(first)
        second_route = _transfer_route(second)
        first_destinations = _transfer_destination_ids(first)
        second_destinations = _transfer_destination_ids(second)
        return (
            (len(first_clubs) >= 2 and len(second_clubs) >= 2 and first_clubs != second_clubs)
            or (first_route is not None and second_route is not None and first_route != second_route)
            or (
                bool(first_destinations)
                and bool(second_destinations)
                and first_destinations.isdisjoint(second_destinations)
            )
            or first_transfer > second_transfer
        )
    first_match = _match_stage(first)
    second_match = _match_stage(second)
    return bool(
        first_match is not None
        and second_match is not None
        and _shared_player(first, second)
        and first_match > second_match
    )


def _is_same_story(first: str, second: str) -> bool:
    return _transfer_story_duplicate(first, second) or _match_story_duplicate(first, second)


class EventGateAction(StrEnum):
    ALLOW = "ALLOW"
    SUPPRESS = "SUPPRESS"
    DEFER = "DEFER"
    SKIP_PROMO = "SKIP_PROMO"


@dataclass(frozen=True, slots=True)
class EventIdentity:
    event_type: str
    player: str | None
    from_club: str | None
    to_club: str | None
    stage: str
    clubs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventGateDecision:
    action: EventGateAction
    reason: str
    identity: EventIdentity | None = None
    related_item_id: int | None = None


_TRANSFER_STAGE_NAMES = {
    0: "TRANSFER_UPDATE",
    1: "INTEREST",
    2: "CONTACT",
    3: "TALKS",
    4: "BID",
    5: "AGREEMENT",
    6: "MEDICAL",
    7: "OFFICIAL",
}
_PROMO_EVENT_RE = re.compile(
    r"(?:\b(?:in|this)\s+(?:video|clip)\b|"
    r"\bв\s+(?:этом\s+)?(?:видео|ролике)\b|"
    r"\b(?:видео|ролик)\s+(?:о|про|рассказывает|рассказывается|показывает)\b|"
    r"\bбез\s+трансляций\b[\s\S]{0,700}\bподпишись\s+на\s+вышли\b"
    r"[\s\S]{0,160}\bсмотри\s+все\s+матчи\b|"
    r"\bнаш\s+комментатор\b[\s\S]{0,700}\bпрямо\s+сейчас\s+комментирует\b"
    r"[\s\S]{0,120}\bподключайтесь\b)",
    re.IGNORECASE,
)
_LINEUP_EVENT_RE = re.compile(
    r"\b(?:starts?\s+for|named\s+in\s+the\s+starting\s+(?:lineup|xi)|"
    r"выш(?:ел|ла|ли)\s+в\s+стартов(?:ом|ый|ую)\s+состав(?:е)?|"
    r"выходит\s+в\s+старте|выйдет\s+в\s+старте|стартов(?:ый|ом)\s+состав)\b",
    re.IGNORECASE,
)


def build_event_identity(text: str) -> EventIdentity | None:
    """Build the compact source-local identity used by the live dedupe gate."""

    transfer_stage = _transfer_stage(text)
    clubs = tuple(sorted(_club_ids(text)))
    names = _player_name_candidates(text) or _single_person_name_candidates(text)
    player = names[0] if names else None
    if transfer_stage is not None and player is not None:
        route = _transfer_route(text)
        destinations = _transfer_destination_ids(text)
        from_club = route[0] if route else None
        to_club = route[1] if route else (next(iter(destinations), None))
        return EventIdentity(
            event_type="TRANSFER",
            player=player,
            from_club=from_club,
            to_club=to_club,
            stage=_TRANSFER_STAGE_NAMES[transfer_stage],
            clubs=clubs,
        )
    match_stage = _match_stage(text)
    if match_stage is not None and len(clubs) >= 2:
        return EventIdentity(
            event_type="MATCH",
            player=player,
            from_club=None,
            to_club=None,
            stage="FINAL" if match_stage >= 2 else "LIVE_EVENT",
            clubs=clubs,
        )
    if _LINEUP_EVENT_RE.search(text) and player is not None and clubs:
        return EventIdentity(
            event_type="LINEUP",
            player=player,
            from_club=None,
            to_club=None,
            stage="STARTING",
            clubs=clubs,
        )
    return None


def extract_hashtag_entities(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return source-local people and clubs in mention order for X tags.

    This is presentation metadata only. It reuses the same conservative local
    extractors as event identity and never gates Telegram publication.
    """

    folded_latin = _latin_name(text)
    people = tuple(
        sorted(
            dict.fromkeys(
                (*_player_name_candidates(text), *_single_person_name_candidates(text))
            ),
            key=lambda value: (
                folded_latin.find(value) if folded_latin.find(value) >= 0 else 10**9,
                value,
            ),
        )
    )
    folded = _normalise_text(text)
    eligible_clubs = _club_ids(text)
    ordered_clubs: list[tuple[int, str]] = []
    for club_id, aliases in _CLUB_ALIASES.items():
        if club_id not in eligible_clubs:
            continue
        positions = [
            folded.find(_normalise_text(alias))
            for alias in aliases
            if folded.find(_normalise_text(alias)) >= 0
        ]
        if positions:
            ordered_clubs.append((min(positions), club_id))
    ordered_clubs.sort(key=lambda value: (value[0], value[1]))
    return people, tuple(club_id for _, club_id in ordered_clubs)


def compare_event_identity(current: str, previous: str) -> EventGateDecision:
    """Compare factual events rather than wording, source, currency or amount."""

    current_identity = build_event_identity(current)
    if _PROMO_EVENT_RE.search(current):
        return EventGateDecision(
            EventGateAction.SKIP_PROMO,
            "PROMO_OR_VIDEO_TEASER",
            current_identity,
        )
    previous_identity = build_event_identity(previous)
    if current_identity is None or previous_identity is None:
        return EventGateDecision(EventGateAction.ALLOW, "NO_SHARED_EVENT_IDENTITY", current_identity)
    if current_identity.event_type != previous_identity.event_type:
        return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_EVENT_TYPE", current_identity)

    if current_identity.event_type == "TRANSFER":
        if not _shared_player(current, previous):
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_PLAYER", current_identity)
        current_route = _transfer_route(current)
        previous_route = _transfer_route(previous)
        if (
            current_route is not None
            and previous_route is not None
            and set(current_route) == set(previous_route)
            and current_route != previous_route
        ):
            return EventGateDecision(
                EventGateAction.DEFER,
                "CONFLICTING_TRANSFER_ROUTE",
                current_identity,
            )
        current_clubs = _club_ids(current)
        previous_clubs = _club_ids(previous)
        shared_clubs = current_clubs & previous_clubs
        if not shared_clubs:
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_TRANSFER_ROUTE", current_identity)
        if len(current_clubs) >= 2 and len(previous_clubs) >= 2 and current_clubs != previous_clubs:
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_TRANSFER_ROUTE", current_identity)
        current_destinations = _transfer_destination_ids(current)
        previous_destinations = _transfer_destination_ids(previous)
        if (
            current_destinations
            and previous_destinations
            and current_destinations.isdisjoint(previous_destinations)
        ):
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_DESTINATION", current_identity)
        current_rejected = _transfer_rejected(current)
        previous_rejected = _transfer_rejected(previous)
        if current_rejected != previous_rejected:
            return EventGateDecision(EventGateAction.ALLOW, "TRANSFER_OUTCOME_CHANGED", current_identity)
        current_stage = _transfer_stage(current)
        previous_stage = _transfer_stage(previous)
        if current_stage is not None and previous_stage is not None:
            if current_stage <= previous_stage:
                return EventGateDecision(
                    EventGateAction.SUPPRESS,
                    "SAME_EVENT_SAME_OR_REGRESSIVE_STAGE",
                    current_identity,
                )
            return EventGateDecision(EventGateAction.ALLOW, "FACTUAL_STAGE_ADVANCED", current_identity)

    if current_identity.event_type == "MATCH" and current_identity.clubs == previous_identity.clubs:
        if _match_story_duplicate(current, previous):
            return EventGateDecision(
                EventGateAction.SUPPRESS,
                "SAME_MATCH_SAME_OR_REGRESSIVE_STAGE",
                current_identity,
            )
        return EventGateDecision(EventGateAction.ALLOW, "MATCH_STAGE_ADVANCED", current_identity)

    if current_identity.event_type == "LINEUP":
        if not _shared_player(current, previous):
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_PLAYER", current_identity)
        if set(current_identity.clubs).isdisjoint(previous_identity.clubs):
            return EventGateDecision(EventGateAction.ALLOW, "DIFFERENT_MATCH_OR_CLUB", current_identity)
        return EventGateDecision(
            EventGateAction.SUPPRESS,
            "SAME_LINEUP_ANNOUNCEMENT",
            current_identity,
        )

    return EventGateDecision(EventGateAction.ALLOW, "DISTINCT_EVENT", current_identity)


def same_story_text(first: str, second: str) -> bool:
    """Expose existing dedupe semantics as a soft editorial repetition signal."""

    return _is_same_story(first, second) or _is_near_duplicate(first, second)


def same_transfer_subject(first: str, second: str) -> bool:
    """Compare transfer subjects without changing hard ingest-dedupe policy."""

    return _shared_player(first, second)


def _row_story_text(row: sqlite3.Row) -> str:
    """Use both trusted source and final Russian draft for last-line dedupe."""

    parts = (
        str(row["source_text"] or "").strip(),
        str(row["clean_source_text"] or "").strip(),
        str(row["draft_text"] or "").strip(),
    )
    return "\n".join(dict.fromkeys(part for part in parts if part))


@dataclass(frozen=True, slots=True)
class CanaryDecision:
    active: bool
    allowed: bool
    reason: str
    run_id: str | None = None
    slot: int | None = None


@dataclass(slots=True)
class CanaryTelegramSendGuard:
    """Mutable result carried through the revision-locked Telegram send."""

    decision: CanaryDecision
    message_id: int | None = None

    def record_success(self, message_id: int) -> None:
        self.message_id = int(message_id)


class Storage:
    CEO_PUBLICATION_PAUSE_KEY = "ceo_publication_pause_v1"
    PUBLICATION_CANARY_KEY = "publication_canary_v1"
    X_SOURCE_QUOTE_CANARY_KEY = "x_source_quote_canary_v1"
    VK_AUTH_BLOCK_KEY = "vk_auth_blocked_v1"
    CANARY_VALIDATION_VERSION = "publication-eligibility-v1"

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialise(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    source_published_at TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    clean_source_text TEXT,
                    media_url TEXT,
                    content_hash TEXT NOT NULL,
                    dedupe_hash TEXT NOT NULL,
                    duplicate_of INTEGER REFERENCES items(id),
                    source_deleted INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued',
                    draft_text TEXT,
                    published_text TEXT,
                    published_source_text TEXT,
                    published_source_content_hash TEXT,
                    image_path TEXT,
                    visual_manifest_path TEXT,
                    card_category TEXT,
                    card_headline TEXT,
                    card_secondary TEXT,
                    visual_style TEXT,
                    previous_visual_style TEXT,
                    redesign_count INTEGER NOT NULL DEFAULT 0,
                    scheduled_for TEXT,
                    telegram_message_id INTEGER,
                    x_post_url TEXT,
                    x_text TEXT,
                    x_status TEXT,
                    x_attempts INTEGER NOT NULL DEFAULT 0,
                    x_next_attempt_at TEXT,
                    x_published_at TEXT,
                    x_like_status TEXT,
                    x_like_attempts INTEGER NOT NULL DEFAULT 0,
                    x_like_next_attempt_at TEXT,
                    vk_post_id INTEGER,
                    vk_post_url TEXT,
                    vk_text TEXT,
                    vk_status TEXT,
                    vk_attempts INTEGER NOT NULL DEFAULT 0,
                    vk_next_attempt_at TEXT,
                    vk_published_at TEXT,
                    vk_last_error TEXT,
                    dzen_article_url TEXT,
                    dzen_article_id TEXT,
                    dzen_mapping_status TEXT,
                    dzen_delete_status TEXT,
                    dzen_attempts INTEGER NOT NULL DEFAULT 0,
                    dzen_like_status TEXT,
                    dzen_like_attempts INTEGER NOT NULL DEFAULT 0,
                    dzen_like_next_attempt_at TEXT,
                    telegram_published_at TEXT,
                    dzen_next_attempt_at TEXT,
                    dzen_last_error TEXT,
                    reviewed_at TEXT,
                    last_error TEXT,
                    editorial_category TEXT,
                    editorial_importance INTEGER,
                    editorial_interest INTEGER,
                    editorial_novelty INTEGER,
                    editorial_priority REAL,
                    editorial_mode TEXT,
                    editorial_diversity_adjustment REAL,
                    editorial_reason TEXT,
                    UNIQUE(source_id, external_id)
                );

                CREATE INDEX IF NOT EXISTS idx_items_state
                    ON items(state, scheduled_for);
                CREATE INDEX IF NOT EXISTS idx_items_dedupe
                    ON items(dedupe_hash, source_published_at);
                CREATE INDEX IF NOT EXISTS idx_items_recent_publication
                    ON items(source_published_at, telegram_published_at)
                    WHERE telegram_message_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_type_created
                    ON events(event_type, created_at);

                CREATE TABLE IF NOT EXISTS source_state (
                    source_id TEXT PRIMARY KEY,
                    baseline_completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dzen_daily_digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest_date TEXT NOT NULL UNIQUE,
                    timezone_name TEXT NOT NULL,
                    window_start_utc TEXT NOT NULL,
                    window_end_utc TEXT NOT NULL,
                    source_set_hash TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    story_count INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready',
                    dzen_article_url TEXT UNIQUE,
                    dzen_article_id TEXT UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    published_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dzen_daily_digest_state
                    ON dzen_daily_digests(state, next_attempt_at, digest_date);

                CREATE TABLE IF NOT EXISTS dzen_daily_digest_items (
                    digest_id INTEGER NOT NULL REFERENCES dzen_daily_digests(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    telegram_message_id INTEGER NOT NULL,
                    text_snapshot TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(digest_id, item_id),
                    UNIQUE(digest_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS publication_canary_slots (
                    canary_run_id TEXT NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    slot INTEGER NOT NULL,
                    admitted_at TEXT NOT NULL,
                    telegram_status TEXT NOT NULL DEFAULT 'reserved',
                    telegram_message_id INTEGER,
                    x_status TEXT NOT NULL DEFAULT 'pending',
                    x_post_url TEXT,
                    last_error TEXT,
                    reservation_status TEXT NOT NULL DEFAULT 'RESERVED',
                    approval_revision INTEGER,
                    source_content_hash TEXT,
                    editorial_final_hash TEXT,
                    approval_fingerprint TEXT,
                    validation_version TEXT,
                    invalidated_at TEXT,
                    invalidation_reason TEXT,
                    invalidation_count INTEGER NOT NULL DEFAULT 0,
                    reservation_count INTEGER NOT NULL DEFAULT 1,
                    successful_slot INTEGER,
                    PRIMARY KEY(canary_run_id, item_id),
                    UNIQUE(canary_run_id, slot)
                );

                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(items)").fetchall()
            }
            migrations = {
                "clean_source_text": "TEXT",
                "published_text": "TEXT",
                "published_source_text": "TEXT",
                "published_source_content_hash": "TEXT",
                "card_category": "TEXT",
                "card_headline": "TEXT",
                "card_secondary": "TEXT",
                "reviewed_at": "TEXT",
                "last_error": "TEXT",
                "visual_style": "TEXT",
                "visual_manifest_path": "TEXT",
                "previous_visual_style": "TEXT",
                "redesign_count": "INTEGER NOT NULL DEFAULT 0",
                "x_post_url": "TEXT",
                "x_text": "TEXT",
                "x_status": "TEXT",
                "x_attempts": "INTEGER NOT NULL DEFAULT 0",
                "x_next_attempt_at": "TEXT",
                "x_published_at": "TEXT",
                "x_like_status": "TEXT",
                "x_like_attempts": "INTEGER NOT NULL DEFAULT 0",
                "x_like_next_attempt_at": "TEXT",
                "vk_post_id": "INTEGER",
                "vk_post_url": "TEXT",
                "vk_text": "TEXT",
                "vk_status": "TEXT",
                "vk_attempts": "INTEGER NOT NULL DEFAULT 0",
                "vk_next_attempt_at": "TEXT",
                "vk_published_at": "TEXT",
                "vk_last_error": "TEXT",
                "dzen_article_url": "TEXT",
                "dzen_article_id": "TEXT",
                "dzen_mapping_status": "TEXT",
                "dzen_delete_status": "TEXT",
                "dzen_attempts": "INTEGER NOT NULL DEFAULT 0",
                "dzen_like_status": "TEXT",
                "dzen_like_attempts": "INTEGER NOT NULL DEFAULT 0",
                "dzen_like_next_attempt_at": "TEXT",
                "telegram_published_at": "TEXT",
                "dzen_next_attempt_at": "TEXT",
                "dzen_last_error": "TEXT",
                "editorial_category": "TEXT",
                "editorial_importance": "INTEGER",
                "editorial_interest": "INTEGER",
                "editorial_novelty": "INTEGER",
                "editorial_priority": "REAL",
                "editorial_mode": "TEXT",
                "editorial_diversity_adjustment": "REAL",
                "editorial_reason": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
            # Older runtimes retried permanent VK authentication/application
            # rejections as if they were transient API failures. Park those
            # rows and open a global circuit breaker until an operator enters
            # a replacement token explicitly.
            vk_auth_error = db.execute(
                "SELECT vk_last_error FROM items WHERE vk_status IN "
                "('publish_failed','replace_failed','delete_failed') AND "
                "(vk_last_error LIKE 'VK: VK_API_ERROR_5:%' OR "
                "vk_last_error LIKE 'VK: VK_API_ERROR_15:%' OR "
                "vk_last_error LIKE 'VK: VK_API_ERROR_27:%' OR "
                "vk_last_error LIKE 'VK: VK_API_ERROR_28:%') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if vk_auth_error is not None:
                db.execute(
                    "UPDATE items SET vk_status = CASE vk_status "
                    "WHEN 'publish_failed' THEN 'publish_auth_failed' "
                    "WHEN 'replace_failed' THEN 'replace_auth_failed' "
                    "WHEN 'delete_failed' THEN 'delete_auth_failed' END, "
                    "vk_next_attempt_at = NULL WHERE vk_status IN "
                    "('publish_failed','replace_failed','delete_failed') AND "
                    "(vk_last_error LIKE 'VK: VK_API_ERROR_5:%' OR "
                    "vk_last_error LIKE 'VK: VK_API_ERROR_15:%' OR "
                    "vk_last_error LIKE 'VK: VK_API_ERROR_27:%' OR "
                    "vk_last_error LIKE 'VK: VK_API_ERROR_28:%')"
                )
                db.execute(
                    "INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (
                        self.VK_AUTH_BLOCK_KEY,
                        str(vk_auth_error["vk_last_error"] or "VK authentication failed")[:1000],
                        _iso(utc_now()),
                    ),
                )
            # Automatic reactions on X are intentionally disabled. Retire any
            # queue left by an older runtime so a restart cannot perform a
            # delayed like on an already published post.
            db.execute(
                "UPDATE items SET x_like_status = 'not_applicable', "
                "x_like_attempts = 0, x_like_next_attempt_at = NULL "
                "WHERE x_like_status IN ('pending', 'like_failed')"
            )
            rows_without_clean_source = db.execute(
                "SELECT id, source_text FROM items WHERE clean_source_text IS NULL"
            ).fetchall()
            db.executemany(
                "UPDATE items SET clean_source_text = ? WHERE id = ?",
                (
                    (normalize_source_language(str(row["source_text"])).clean_text, int(row["id"]))
                    for row in rows_without_clean_source
                ),
            )
            canary_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(publication_canary_slots)").fetchall()
            }
            canary_migrations = {
                "reservation_status": "TEXT NOT NULL DEFAULT 'RESERVED'",
                "approval_revision": "INTEGER",
                "source_content_hash": "TEXT",
                "editorial_final_hash": "TEXT",
                "approval_fingerprint": "TEXT",
                "validation_version": "TEXT",
                "invalidated_at": "TEXT",
                "invalidation_reason": "TEXT",
                "invalidation_count": "INTEGER NOT NULL DEFAULT 0",
                "reservation_count": "INTEGER NOT NULL DEFAULT 1",
                "successful_slot": "INTEGER",
            }
            for name, definition in canary_migrations.items():
                if name not in canary_columns:
                    db.execute(
                        f"ALTER TABLE publication_canary_slots ADD COLUMN {name} {definition}"
                    )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_canary_successful_slot "
                "ON publication_canary_slots(canary_run_id, successful_slot) "
                "WHERE successful_slot IS NOT NULL"
            )
            db.execute(
                "UPDATE publication_canary_slots SET reservation_status = CASE "
                "WHEN telegram_status='published' THEN 'TELEGRAM_SENT' "
                "WHEN telegram_status='failed' THEN 'FAILED' "
                "WHEN telegram_status='invalidated' THEN 'INVALIDATED' "
                "ELSE COALESCE(NULLIF(reservation_status,''),'RESERVED') END "
                "WHERE approval_fingerprint IS NULL"
            )
            # Older versions interpreted a manual channel deletion as a request
            # for redesign. A deletion is now terminal, so legacy requests must
            # never return to the publishing queue after a restart.
            db.execute(
                "UPDATE items SET state = 'cancelled_editor_deleted', "
                "scheduled_for = NULL, "
                "last_error = 'Пост удалён владельцем: повторная публикация запрещена' "
                "WHERE state = 'needs_redesign'"
            )
            # A historical source-update path could leave a row marked with
            # duplicate_of while changing its state back to queued. Such rows
            # were publication-ineligible but polluted queue/status counts.
            db.execute(
                "UPDATE items SET state = 'duplicate', scheduled_for = NULL, "
                "last_error = COALESCE(last_error, 'Повтор уже известной истории') "
                "WHERE duplicate_of IS NOT NULL AND telegram_message_id IS NULL "
                "AND state IN ('queued','scheduled','ready_for_review','approved',"
                "'visual_pending','visual_processing')"
            )
            # A crash after opening Dzen's submit boundary has an unknown
            # delivery outcome.  Such a digest may only be reconciled; it must
            # never return to the create queue after a restart.
            db.execute(
                "UPDATE dzen_daily_digests SET state = 'publish_unknown', "
                "next_attempt_at = NULL, "
                "last_error = COALESCE(last_error, 'DZEN_PUBLISH_INTERRUPTED'), "
                "updated_at = ? WHERE state = 'publishing'",
                (_iso(utc_now()),),
            )

    def get_runtime_state(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_runtime_state(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO runtime_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _iso(utc_now())),
            )

    def clear_runtime_state(self, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM runtime_state WHERE key = ?", (key,))

    def publication_is_paused(self) -> bool:
        return self.get_runtime_state(self.CEO_PUBLICATION_PAUSE_KEY) == "1"

    def set_publication_paused(self, paused: bool) -> None:
        if paused:
            self.set_runtime_state(self.CEO_PUBLICATION_PAUSE_KEY, "1")
        else:
            self.clear_runtime_state(self.CEO_PUBLICATION_PAUSE_KEY)

    @staticmethod
    def _publication_canary_state(db: sqlite3.Connection) -> dict[str, object] | None:
        row = db.execute(
            "SELECT value FROM runtime_state WHERE key = ?",
            (Storage.PUBLICATION_CANARY_KEY,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["value"]))
            run_id = str(payload["run_id"]).strip()
            maximum = int(payload["max_stories"])
            activated_at = datetime.fromisoformat(str(payload["activated_at"]))
            if not run_id or maximum < 1 or activated_at.tzinfo is None:
                raise ValueError("invalid canary state")
            return {
                "active": bool(payload.get("active", True)),
                "run_id": run_id,
                "max_stories": maximum,
                "activated_at": activated_at.astimezone(timezone.utc),
                "finished_at": payload.get("finished_at"),
                "terminal_reason": payload.get("terminal_reason"),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {"active": True, "invalid": True}

    def arm_publication_canary(
        self,
        run_id: str,
        *,
        max_stories: int = 5,
        activated_at: datetime | None = None,
    ) -> dict[str, object]:
        clean_run_id = run_id.strip()
        if not clean_run_id or max_stories < 1:
            raise ValueError("Invalid canary run configuration")
        activation = (activated_at or utc_now()).astimezone(timezone.utc)
        payload = {
            "active": True,
            "run_id": clean_run_id,
            "max_stories": int(max_stories),
            "activated_at": _iso(activation),
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pause = db.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (self.CEO_PUBLICATION_PAUSE_KEY,),
            ).fetchone()
            if pause is None or str(pause["value"]) != "1":
                raise ValueError("CANARY_ARM_REQUIRES_CEO_PUBLICATION_PAUSE")
            existing = self._publication_canary_state(db)
            if existing and bool(existing.get("active")):
                raise ValueError("ACTIVE_PUBLICATION_CANARY_ALREADY_EXISTS")
            db.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (self.PUBLICATION_CANARY_KEY, json.dumps(payload), _iso(utc_now())),
            )
        return payload

    def publication_canary_status(self) -> dict[str, object] | None:
        with self.connect() as db:
            state = self._publication_canary_state(db)
            if state is None:
                return None
            run_id = str(state.get("run_id") or "")
            slots = [
                dict(row)
                for row in db.execute(
                    "SELECT item_id,slot,admitted_at,telegram_status,telegram_message_id,"
                    "x_status,x_post_url,last_error,reservation_status,approval_revision,"
                    "source_content_hash,editorial_final_hash,approval_fingerprint,"
                    "validation_version,invalidated_at,invalidation_reason,"
                    "invalidation_count,reservation_count,successful_slot "
                    "FROM publication_canary_slots "
                    "WHERE canary_run_id=? ORDER BY slot",
                    (run_id,),
                ).fetchall()
            ] if run_id else []
        result = dict(state)
        if isinstance(result.get("activated_at"), datetime):
            result["activated_at"] = _iso(result["activated_at"])
        result["slots"] = slots
        result["reservations"] = sum(int(row["reservation_count"] or 0) for row in slots)
        result["invalidations"] = sum(int(row["invalidation_count"] or 0) for row in slots)
        result["successful_publications"] = sum(
            str(row["telegram_status"]) == "published" for row in slots
        )
        return result

    @staticmethod
    def _canary_candidate_fingerprint(
        db: sqlite3.Connection,
        item_id: int,
        editorial_final_text: str | None,
        validation_version: str,
    ) -> tuple[sqlite3.Row | None, int | None, str, str]:
        item = db.execute(
            "SELECT id,source_id,external_id,source_published_at,discovered_at,source_text,content_hash,"
            "dedupe_hash,duplicate_of,source_deleted,state,draft_text,image_path,"
            "card_category,reviewed_at,telegram_message_id FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        if item is None:
            return None, None, "", ""
        approval = db.execute(
            "SELECT MAX(id) AS value FROM events WHERE item_id=? AND event_type IN "
            "('approved','f1_auto_approved')",
            (item_id,),
        ).fetchone()
        approval_revision = (
            int(approval["value"])
            if approval is not None and approval["value"] is not None
            else None
        )
        final_text = (
            editorial_final_text
            if editorial_final_text is not None
            else str(item["draft_text"] or "")
        ).strip()
        final_hash = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        payload = {
            "item_id": int(item["id"]),
            "source_id": str(item["source_id"]),
            "external_id": str(item["external_id"]),
            "source_published_at": str(item["source_published_at"]),
            "source_text_hash": hashlib.sha256(
                str(item["source_text"] or "").encode("utf-8")
            ).hexdigest(),
            "source_content_hash": str(item["content_hash"] or ""),
            "dedupe_hash": str(item["dedupe_hash"] or ""),
            "duplicate_of": item["duplicate_of"],
            "source_deleted": int(item["source_deleted"] or 0),
            "state": str(item["state"]),
            "stored_draft_hash": hashlib.sha256(
                str(item["draft_text"] or "").strip().encode("utf-8")
            ).hexdigest(),
            "editorial_final_hash": final_hash,
            "image_path": str(item["image_path"] or ""),
            "card_category": str(item["card_category"] or ""),
            "reviewed_at": str(item["reviewed_at"] or ""),
            "telegram_message_id": item["telegram_message_id"],
            "approval_revision": approval_revision,
            "validation_version": validation_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        return item, approval_revision, final_hash, fingerprint

    @staticmethod
    def _canary_capacity_used(db: sqlite3.Connection, run_id: str) -> int:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM publication_canary_slots WHERE canary_run_id=? "
                "AND (telegram_status='published' OR reservation_status IN "
                "('RESERVED','READY_TO_SEND'))",
                (run_id,),
            ).fetchone()[0]
        )

    def _invalidate_canary_reservation(
        self,
        db: sqlite3.Connection,
        run_id: str,
        item_id: int,
        reason: str,
    ) -> None:
        now = _iso(utc_now())
        cursor = db.execute(
            "UPDATE publication_canary_slots SET reservation_status='INVALIDATED',"
            "telegram_status='invalidated',x_status='blocked',invalidated_at=?,"
            "invalidation_reason=?,invalidation_count=invalidation_count+1,last_error=? "
            "WHERE canary_run_id=? AND item_id=? AND reservation_status IN "
            "('RESERVED','READY_TO_SEND') AND telegram_status='reserved'",
            (now, reason, reason, run_id, item_id),
        )
        if cursor.rowcount:
            self._event(
                db,
                item_id,
                "canary_reservation_invalidated",
                {"run_id": run_id, "reason": reason},
            )

    def close_publication_canary(self, reason: str) -> dict[str, object]:
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("CANARY_CLOSE_REASON_REQUIRED")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pause = db.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                (self.CEO_PUBLICATION_PAUSE_KEY,),
            ).fetchone()
            if pause is None or str(pause["value"]) != "1":
                raise ValueError("CANARY_CLOSE_REQUIRES_CEO_PUBLICATION_PAUSE")
            state = self._publication_canary_state(db)
            if state is None or bool(state.get("invalid")):
                raise ValueError("ACTIVE_PUBLICATION_CANARY_NOT_FOUND")
            if bool(state.get("active")):
                payload = {
                    "active": False,
                    "run_id": str(state["run_id"]),
                    "max_stories": int(state["max_stories"]),
                    "activated_at": _iso(state["activated_at"]),
                    "finished_at": _iso(utc_now()),
                    "terminal_reason": clean_reason,
                }
                db.execute(
                    "UPDATE runtime_state SET value=?,updated_at=? WHERE key=?",
                    (json.dumps(payload), _iso(utc_now()), self.PUBLICATION_CANARY_KEY),
                )
        return self.publication_canary_status() or {}

    def publication_canary_candidate_precheck(self, item_id: int) -> CanaryDecision:
        """Read-only queue filter; the real slot reservation still happens pre-send."""
        with self.connect() as db:
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")):
                return CanaryDecision(False, True, "CANARY_MODE_DISABLED")
            if bool(state.get("invalid")):
                return CanaryDecision(True, False, "CANARY_STATE_INVALID")
            run_id = str(state["run_id"])
            item = db.execute(
                "SELECT state,duplicate_of,source_deleted,telegram_message_id,"
                "source_published_at,discovered_at "
                "FROM items WHERE id=?",
                (item_id,),
            ).fetchone()
            if item is None or (
                str(item["state"]) != "approved"
                or item["duplicate_of"] is not None
                or int(item["source_deleted"] or 0) != 0
                or item["telegram_message_id"] is not None
            ):
                return CanaryDecision(True, False, "CANARY_ITEM_NOT_ELIGIBLE", run_id)
            existing = db.execute(
                "SELECT slot,reservation_status,telegram_status FROM publication_canary_slots "
                "WHERE canary_run_id=? AND item_id=?",
                (run_id, item_id),
            ).fetchone()
            if existing is not None and str(existing["reservation_status"]) != "INVALIDATED":
                return CanaryDecision(
                    True,
                    False,
                    "CANARY_STORY_ALREADY_FINALIZED"
                    if str(existing["telegram_status"]) == "published"
                    else "CANARY_SLOT_ALREADY_RESERVED",
                    run_id,
                    int(existing["slot"]),
                )
            try:
                source_time = datetime.fromisoformat(
                    str(item["source_published_at"] or item["discovered_at"])
                ).astimezone(timezone.utc)
            except ValueError:
                return CanaryDecision(True, False, "CANARY_SOURCE_TIME_INVALID", run_id)
            if source_time < state["activated_at"]:
                return CanaryDecision(True, False, "CANARY_ITEM_PREDATES_ACTIVATION", run_id)
            used = self._canary_capacity_used(db, run_id)
            if used >= int(state["max_stories"]):
                return CanaryDecision(True, False, "CANARY_LIMIT_REACHED", run_id)
            return CanaryDecision(True, True, "CANARY_CANDIDATE_ELIGIBLE", run_id)

    def reserve_publication_canary_slot(
        self,
        item_id: int,
        *,
        editorial_final_text: str | None = None,
        validation_version: str = CANARY_VALIDATION_VERSION,
    ) -> CanaryDecision:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")):
                return CanaryDecision(False, True, "CANARY_MODE_DISABLED")
            if bool(state.get("invalid")):
                return CanaryDecision(True, False, "CANARY_STATE_INVALID")
            run_id = str(state["run_id"])
            existing = db.execute(
                "SELECT slot,telegram_status,reservation_status,approval_fingerprint "
                "FROM publication_canary_slots "
                "WHERE canary_run_id=? AND item_id=?",
                (run_id, item_id),
            ).fetchone()
            if existing is not None and str(existing["reservation_status"]) != "INVALIDATED":
                return CanaryDecision(
                    True,
                    False,
                    "CANARY_SLOT_ALREADY_RESERVED"
                    if str(existing["telegram_status"]) == "reserved"
                    else "CANARY_STORY_ALREADY_FINALIZED",
                    run_id,
                    int(existing["slot"]),
                )
            item, approval_revision, final_hash, fingerprint = (
                self._canary_candidate_fingerprint(
                    db,
                    item_id,
                    editorial_final_text,
                    validation_version,
                )
            )
            if item is None:
                return CanaryDecision(True, False, "CANARY_ITEM_NOT_FOUND", run_id)
            if (
                editorial_final_text is not None
                and str(item["draft_text"] or "").strip()
                != editorial_final_text.strip()
            ):
                return CanaryDecision(
                    True, False, "CANARY_FINAL_TEXT_NOT_CURRENT", run_id
                )
            if (
                str(item["state"]) != "approved"
                or item["duplicate_of"] is not None
                or int(item["source_deleted"] or 0) != 0
                or item["telegram_message_id"] is not None
            ):
                return CanaryDecision(True, False, "CANARY_ITEM_NOT_ELIGIBLE", run_id)
            try:
                source_time = datetime.fromisoformat(
                    str(item["source_published_at"] or item["discovered_at"])
                ).astimezone(timezone.utc)
            except ValueError:
                return CanaryDecision(True, False, "CANARY_SOURCE_TIME_INVALID", run_id)
            if source_time < state["activated_at"]:
                return CanaryDecision(True, False, "CANARY_ITEM_PREDATES_ACTIVATION", run_id)
            used = self._canary_capacity_used(db, run_id)
            maximum = int(state["max_stories"])
            if used >= maximum:
                return CanaryDecision(True, False, "CANARY_LIMIT_REACHED", run_id)
            slot = int(
                db.execute(
                    "SELECT COALESCE(MAX(slot),0)+1 FROM publication_canary_slots "
                    "WHERE canary_run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            admitted_at = _iso(utc_now())
            if existing is None:
                db.execute(
                    "INSERT INTO publication_canary_slots("
                    "canary_run_id,item_id,slot,admitted_at,reservation_status,"
                    "approval_revision,source_content_hash,editorial_final_hash,"
                    "approval_fingerprint,validation_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        item_id,
                        slot,
                        admitted_at,
                        "RESERVED",
                        approval_revision,
                        str(item["content_hash"] or ""),
                        final_hash,
                        fingerprint,
                        validation_version,
                    ),
                )
            else:
                db.execute(
                    "UPDATE publication_canary_slots SET slot=?,admitted_at=?,"
                    "telegram_status='reserved',telegram_message_id=NULL,x_status='pending',"
                    "x_post_url=NULL,last_error=NULL,reservation_status='RESERVED',"
                    "approval_revision=?,source_content_hash=?,editorial_final_hash=?,"
                    "approval_fingerprint=?,validation_version=?,invalidated_at=NULL,"
                    "invalidation_reason=NULL,reservation_count=reservation_count+1,"
                    "successful_slot=NULL WHERE canary_run_id=? AND item_id=? "
                    "AND reservation_status='INVALIDATED'",
                    (
                        slot,
                        admitted_at,
                        approval_revision,
                        str(item["content_hash"] or ""),
                        final_hash,
                        fingerprint,
                        validation_version,
                        run_id,
                        item_id,
                    ),
                )
            self._event(db, item_id, "canary_slot_reserved", {"run_id": run_id, "slot": slot})
            return CanaryDecision(True, True, "CANARY_SLOT_RESERVED", run_id, slot)

    def _revalidate_canary_reservation_in_db(
        self,
        db: sqlite3.Connection,
        item_id: int,
        editorial_final_text: str,
        validation_version: str,
        fresh_after: datetime | None,
    ) -> CanaryDecision:
        state = self._publication_canary_state(db)
        if state is None or not bool(state.get("active")):
            return CanaryDecision(False, True, "CANARY_MODE_DISABLED")
        if bool(state.get("invalid")):
            return CanaryDecision(True, False, "CANARY_STATE_INVALID")
        run_id = str(state["run_id"])
        pause = db.execute(
            "SELECT value FROM runtime_state WHERE key=?",
            (self.CEO_PUBLICATION_PAUSE_KEY,),
        ).fetchone()
        if pause is not None and str(pause["value"]) == "1":
            return CanaryDecision(True, False, "CANARY_CEO_PAUSE_ACTIVE", run_id)
        reservation = db.execute(
            "SELECT slot,approval_fingerprint FROM publication_canary_slots "
            "WHERE canary_run_id=? AND item_id=? AND telegram_status='reserved' "
            "AND reservation_status IN ('RESERVED','READY_TO_SEND')",
            (run_id, item_id),
        ).fetchone()
        if reservation is None:
            return CanaryDecision(True, False, "CANARY_RESERVATION_NOT_ACTIVE", run_id)
        slot = int(reservation["slot"])
        item, _revision, _final_hash, fingerprint = self._canary_candidate_fingerprint(
            db,
            item_id,
            editorial_final_text,
            validation_version,
        )
        reason = ""
        if item is None:
            reason = "INVALIDATED_BY_SOURCE_UPDATE"
        elif str(item["state"]) == "cancelled_editor_deleted":
            reason = "INVALIDATED_BY_CEO_DELETE"
        elif item["duplicate_of"] is not None:
            reason = "INVALIDATED_BY_DEDUPE"
        elif (
            str(item["state"]) != "approved"
            or int(item["source_deleted"] or 0) != 0
            or item["telegram_message_id"] is not None
            or str(reservation["approval_fingerprint"] or "") != fingerprint
        ):
            reason = "INVALIDATED_BY_SOURCE_UPDATE"
        elif fresh_after is not None:
            try:
                source_time = datetime.fromisoformat(str(item["source_published_at"])).astimezone(
                    timezone.utc
                )
            except ValueError:
                reason = "INVALIDATED_BY_FRESHNESS"
            else:
                if source_time < fresh_after.astimezone(timezone.utc):
                    reason = "INVALIDATED_BY_FRESHNESS"
        if reason:
            self._invalidate_canary_reservation(db, run_id, item_id, reason)
            return CanaryDecision(True, False, reason, run_id, slot)
        db.execute(
            "UPDATE publication_canary_slots SET reservation_status='READY_TO_SEND' "
            "WHERE canary_run_id=? AND item_id=? AND telegram_status='reserved'",
            (run_id, item_id),
        )
        return CanaryDecision(True, True, "CANARY_REVISION_CURRENT", run_id, slot)

    @contextmanager
    def canary_telegram_send_guard(
        self,
        item_id: int,
        *,
        editorial_final_text: str,
        validation_version: str = CANARY_VALIDATION_VERSION,
        fresh_after: datetime | None = None,
    ) -> Iterator[CanaryTelegramSendGuard]:
        """Hold the SQLite writer boundary from revision check through send commit.

        Concurrent ingestion either commits first and invalidates this reservation,
        or waits until this exact approved revision has completed its Telegram send.
        """

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            decision = self._revalidate_canary_reservation_in_db(
                db,
                item_id,
                editorial_final_text,
                validation_version,
                fresh_after,
            )
            guard = CanaryTelegramSendGuard(decision)
            yield guard
            if not decision.active or not decision.allowed:
                return
            if guard.message_id is None:
                raise ValueError("CANARY_TELEGRAM_SEND_NOT_RECORDED")
            self._mark_published_in_db(db, item_id, guard.message_id)
            self._mark_canary_telegram_success_in_db(
                db,
                str(decision.run_id),
                item_id,
                guard.message_id,
            )

    def mark_canary_telegram_success(self, item_id: int, message_id: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")) or bool(state.get("invalid")):
                return
            self._mark_canary_telegram_success_in_db(
                db, str(state["run_id"]), item_id, message_id
            )

    def _mark_canary_telegram_success_in_db(
        self,
        db: sqlite3.Connection,
        run_id: str,
        item_id: int,
        message_id: int,
    ) -> None:
        state = self._publication_canary_state(db)
        if (
            state is None
            or not bool(state.get("active"))
            or str(state.get("run_id")) != run_id
        ):
            raise ValueError("CANARY_TELEGRAM_SUCCESS_WITHOUT_ACTIVE_RUN")
        successful_before = int(
            db.execute(
                "SELECT COUNT(*) FROM publication_canary_slots WHERE canary_run_id=? "
                "AND telegram_status='published'",
                (run_id,),
            ).fetchone()[0]
        )
        if successful_before >= int(state["max_stories"]):
            raise ValueError("CANARY_SUCCESS_LIMIT_REACHED")
        successful_slot = successful_before + 1
        cursor = db.execute(
            "UPDATE publication_canary_slots SET telegram_status='published', "
            "telegram_message_id=?,reservation_status='TELEGRAM_SENT',"
            "successful_slot=?,last_error=NULL WHERE canary_run_id=? AND item_id=? "
            "AND telegram_status='reserved' AND reservation_status IN "
            "('RESERVED','READY_TO_SEND')",
            (message_id, successful_slot, run_id, item_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("CANARY_TELEGRAM_SUCCESS_WITHOUT_RESERVED_SLOT")
        self._event(
            db,
            item_id,
            "canary_telegram_published",
            {"run_id": run_id, "successful_slot": successful_slot},
        )
        if successful_slot >= int(state["max_stories"]):
            db.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (self.CEO_PUBLICATION_PAUSE_KEY, "1", _iso(utc_now())),
            )

    def mark_canary_telegram_failed(self, item_id: int, error: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")) or bool(state.get("invalid")):
                return
            run_id = str(state["run_id"])
            cursor = db.execute(
                "UPDATE publication_canary_slots SET telegram_status='failed',x_status='blocked',"
                "reservation_status='FAILED',last_error=? WHERE canary_run_id=? "
                "AND item_id=? AND telegram_status='reserved'",
                (error[:1000], run_id, item_id),
            )
            if cursor.rowcount:
                db.execute(
                    "INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (self.CEO_PUBLICATION_PAUSE_KEY, "1", _iso(utc_now())),
                )
                self._event(db, item_id, "canary_telegram_failed", {"run_id": run_id})

    def canary_x_pre_send(self, item_id: int) -> CanaryDecision:
        with self.connect() as db:
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")):
                return CanaryDecision(False, True, "CANARY_MODE_DISABLED")
            if bool(state.get("invalid")):
                return CanaryDecision(True, False, "CANARY_STATE_INVALID")
            run_id = str(state["run_id"])
            row = db.execute(
                "SELECT slot,telegram_status FROM publication_canary_slots "
                "WHERE canary_run_id=? AND item_id=?",
                (run_id, item_id),
            ).fetchone()
            item = db.execute(
                "SELECT telegram_message_id FROM items WHERE id=?",
                (item_id,),
            ).fetchone()
            if (
                row is None
                or str(row["telegram_status"]) != "published"
                or item is None
                or item["telegram_message_id"] is None
            ):
                return CanaryDecision(
                    True, False, "BLOCKED_BY_CANARY_INTERLOCK", run_id,
                    int(row["slot"]) if row is not None else None,
                )
            return CanaryDecision(True, True, "CANARY_X_ALLOWED", run_id, int(row["slot"]))

    def mark_canary_x_success(self, item_id: int, url: str) -> None:
        with self.connect() as db:
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")) or bool(state.get("invalid")):
                return
            db.execute(
                "UPDATE publication_canary_slots SET x_status='published',x_post_url=?,"
                "reservation_status='X_SENT',last_error=NULL "
                "WHERE canary_run_id=? AND item_id=? AND telegram_status='published'",
                (url, str(state["run_id"]), item_id),
            )

    def mark_canary_x_failed(self, item_id: int, error: str) -> None:
        with self.connect() as db:
            state = self._publication_canary_state(db)
            if state is None or not bool(state.get("active")) or bool(state.get("invalid")):
                return
            db.execute(
                "UPDATE publication_canary_slots SET x_status='failed',last_error=? "
                "WHERE canary_run_id=? AND item_id=?",
                (error[:1000], str(state["run_id"]), item_id),
            )

    def x_rate_limit_pause_until(self) -> datetime | None:
        """Return the temporary X publishing pause, if it is still valid."""
        raw = self.get_runtime_state("x_rate_limit_pause_until")
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(raw)
            if until.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError:
            self.clear_runtime_state("x_rate_limit_pause_until")
            return None
        if until <= utc_now():
            self.clear_runtime_state("x_rate_limit_pause_until")
            return None
        return until

    def arm_x_source_quote_canary(self, *, max_posts: int = 5) -> dict[str, object]:
        """Enable a bounded X quote-post experiment without reuploading source media."""

        maximum = int(max_posts)
        if maximum < 1:
            raise ValueError("X_SOURCE_QUOTE_CANARY_INVALID_LIMIT")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                (self.X_SOURCE_QUOTE_CANARY_KEY,),
            ).fetchone()
            if row is not None:
                try:
                    existing = json.loads(str(row["value"]))
                except json.JSONDecodeError:
                    existing = {}
                if bool(existing.get("active")):
                    raise ValueError("X_SOURCE_QUOTE_CANARY_ALREADY_ACTIVE")
            payload: dict[str, object] = {
                "active": True,
                "max_posts": maximum,
                "activated_at": _iso(utc_now()),
                "publications": [],
            }
            db.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (
                    self.X_SOURCE_QUOTE_CANARY_KEY,
                    json.dumps(payload, ensure_ascii=False),
                    _iso(utc_now()),
                ),
            )
        return payload

    def x_source_quote_canary_status(self) -> dict[str, object] | None:
        raw = self.get_runtime_state(self.X_SOURCE_QUOTE_CANARY_KEY)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"active": False, "invalid": True, "publications": []}
        publications = payload.get("publications")
        if not isinstance(publications, list):
            return {"active": False, "invalid": True, "publications": []}
        payload["published"] = len(publications)
        payload["remaining"] = max(
            0, int(payload.get("max_posts") or 0) - len(publications)
        )
        return payload

    def mark_x_source_quote_canary_published(
        self,
        item_id: int,
        *,
        source_url: str,
        x_post_url: str,
    ) -> dict[str, object]:
        """Record one verified quote-post and close the canary at its hard limit."""

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM runtime_state WHERE key=?",
                (self.X_SOURCE_QUOTE_CANARY_KEY,),
            ).fetchone()
            if row is None:
                raise ValueError("X_SOURCE_QUOTE_CANARY_NOT_ACTIVE")
            try:
                payload = json.loads(str(row["value"]))
            except json.JSONDecodeError as exc:
                raise ValueError("X_SOURCE_QUOTE_CANARY_INVALID_STATE") from exc
            if not bool(payload.get("active")):
                raise ValueError("X_SOURCE_QUOTE_CANARY_NOT_ACTIVE")
            publications = payload.get("publications")
            if not isinstance(publications, list):
                raise ValueError("X_SOURCE_QUOTE_CANARY_INVALID_STATE")
            if any(int(entry.get("item_id") or 0) == int(item_id) for entry in publications):
                return payload
            maximum = int(payload.get("max_posts") or 0)
            if maximum < 1 or len(publications) >= maximum:
                raise ValueError("X_SOURCE_QUOTE_CANARY_LIMIT_REACHED")
            publications.append(
                {
                    "item_id": int(item_id),
                    "source_url": source_url,
                    "x_post_url": x_post_url,
                    "published_at": _iso(utc_now()),
                }
            )
            if len(publications) >= maximum:
                payload["active"] = False
                payload["completed_at"] = _iso(utc_now())
                payload["terminal_reason"] = "LIMIT_REACHED"
            db.execute(
                "UPDATE runtime_state SET value=?,updated_at=? WHERE key=?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    _iso(utc_now()),
                    self.X_SOURCE_QUOTE_CANARY_KEY,
                ),
            )
        payload["published"] = len(publications)
        payload["remaining"] = max(0, maximum - len(publications))
        return payload

    def pause_x_publishing_until(self, until: datetime) -> None:
        if until.tzinfo is None:
            raise ValueError("X publishing pause must include a timezone")
        self.set_runtime_state("x_rate_limit_pause_until", _iso(until))

    def start_publication_drain(
        self, item_ids: list[int], *, interval_minutes: int = 2
    ) -> None:
        ids = sorted({int(value) for value in item_ids if int(value) > 0})
        if not ids:
            self.clear_runtime_state("publication_drain")
            return
        self.set_runtime_state(
            "publication_drain",
            json.dumps(
                {"item_ids": ids, "interval_minutes": max(1, interval_minutes)},
                ensure_ascii=False,
            ),
        )

    def publication_drain(self) -> tuple[set[int], int]:
        raw = self.get_runtime_state("publication_drain")
        if not raw:
            return set(), 0
        try:
            payload = json.loads(raw)
            ids = {
                int(value)
                for value in payload.get("item_ids", [])
                if int(value) > 0
            }
            interval = max(1, int(payload.get("interval_minutes", 2)))
        except (TypeError, ValueError, json.JSONDecodeError):
            self.clear_runtime_state("publication_drain")
            return set(), 0
        if not ids:
            self.clear_runtime_state("publication_drain")
            return set(), 0
        return ids, interval

    def finish_publication_drain_item(self, item_id: int) -> None:
        ids, interval = self.publication_drain()
        ids.discard(int(item_id))
        if ids:
            self.start_publication_drain(sorted(ids), interval_minutes=interval)
        else:
            self.clear_runtime_state("publication_drain")

    def clear_unpublished_backlog(self, cutoff: datetime) -> tuple[int, ...]:
        """Close every unfinished item while preserving publication history."""
        pending_states = (
            "queued",
            "scheduled",
            "needs_update",
            "needs_photo",
            "needs_headline_review",
            "needs_editor_review",
            "ready_for_review",
            "ready_update_review",
            "approved",
            "approved_update",
        )
        placeholders = ",".join("?" for _ in pending_states)
        cutoff_value = _iso(cutoff)
        reason = f"Очередь очищена пользователем. Новая точка отсечения: {cutoff_value}"
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id FROM items WHERE telegram_message_id IS NULL "
                f"AND state IN ({placeholders})",
                pending_states,
            ).fetchall()
            item_ids = [int(row["id"]) for row in rows]
            if item_ids:
                db.execute(
                    f"""
                    UPDATE items
                    SET state = 'rejected_manual', scheduled_for = NULL,
                        draft_text = NULL, image_path = NULL,
                        card_category = NULL, card_headline = NULL,
                        card_secondary = NULL, reviewed_at = ?, last_error = ?
                    WHERE telegram_message_id IS NULL
                      AND state IN ({placeholders})
                    """,
                    (cutoff_value, reason, *pending_states),
                )
                for item_id in item_ids:
                    self._event(
                        db,
                        item_id,
                        "backlog_cleared",
                        {"cutoff": cutoff_value},
                    )
            db.execute(
                """
                INSERT INTO runtime_state(key, value, updated_at)
                VALUES ('fresh_start_cutoff', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (cutoff_value, _iso(utc_now())),
            )
            return tuple(item_ids)

    def reject_unpublished_before_cutoff(
        self, item_id: int, cutoff: datetime
    ) -> bool:
        """Permanently remove a pre-launch item from publication eligibility."""
        cutoff_value = _iso(cutoff)
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE items
                SET state = 'rejected_manual', scheduled_for = NULL,
                    draft_text = NULL, image_path = NULL,
                    card_category = NULL, card_headline = NULL,
                    card_secondary = NULL, reviewed_at = ?,
                    last_error = 'SKIP_PRODUCTION_CUTOFF'
                WHERE id = ? AND telegram_message_id IS NULL
                  AND COALESCE(NULLIF(source_published_at, ''), discovered_at) <= ?
                  AND state IN (
                      'queued', 'scheduled', 'needs_update', 'needs_photo',
                      'needs_headline_review', 'needs_editor_review',
                      'ready_for_review', 'ready_update_review',
                      'approved', 'approved_update'
                  )
                """,
                (cutoff_value, item_id, cutoff_value),
            )
            if cursor.rowcount:
                self._event(
                    db, item_id, "production_cutoff_skipped",
                    {"cutoff": cutoff_value, "reason": "SKIP_PRODUCTION_CUTOFF"},
                )
                return True
            return False

    def suppress_unpublished_before_cutoff(self, cutoff: datetime) -> int:
        """Atomically quarantine every active pre-cutoff row without deleting it."""
        active_states = (
            "queued", "scheduled", "needs_update", "needs_photo",
            "needs_headline_review", "needs_editor_review", "ready_for_review",
            "ready_update_review", "approved", "approved_update", "visual_pending",
        )
        placeholders = ",".join("?" for _ in active_states)
        cutoff_value = _iso(cutoff)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id FROM items WHERE telegram_message_id IS NULL "
                f"AND state IN ({placeholders}) "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) <= ?",
                (*active_states, cutoff_value),
            ).fetchall()
            ids = tuple(int(row["id"]) for row in rows)
            if ids:
                id_placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE items SET state = 'rejected_manual', scheduled_for = NULL, "
                    "draft_text = NULL, image_path = NULL, card_category = NULL, "
                    "card_headline = NULL, card_secondary = NULL, reviewed_at = ?, "
                    f"last_error = 'SKIP_PRODUCTION_CUTOFF' WHERE id IN ({id_placeholders})",
                    (cutoff_value, *ids),
                )
                for item_id in ids:
                    self._event(
                        db, item_id, "production_cutoff_skipped",
                        {"cutoff": cutoff_value, "reason": "SKIP_PRODUCTION_CUTOFF"},
                    )
            db.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES "
                "('fresh_start_cutoff', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (cutoff_value, _iso(utc_now())),
            )
            return len(ids)

    def suppress_stale_production_items(
        self, cutoff: datetime, expires_before: datetime
    ) -> int:
        """Fail closed when a post-cutoff active item ages out in the queue."""
        active_states = (
            "queued", "scheduled", "needs_update", "needs_photo",
            "needs_headline_review", "needs_editor_review", "ready_for_review",
            "ready_update_review", "approved", "approved_update", "visual_pending",
        )
        placeholders = ",".join("?" for _ in active_states)
        cutoff_value = _iso(cutoff)
        expiry_value = _iso(expires_before)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id FROM items WHERE telegram_message_id IS NULL "
                f"AND state IN ({placeholders}) "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) > ? "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) <= ?",
                (*active_states, cutoff_value, expiry_value),
            ).fetchall()
            ids = tuple(int(row["id"]) for row in rows)
            if ids:
                id_placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE items SET state = 'rejected_low_priority', "
                    "scheduled_for = NULL, last_error = 'SKIP_STALE' "
                    f"WHERE id IN ({id_placeholders})",
                    ids,
                )
                for item_id in ids:
                    self._event(db, item_id, "production_stale_skipped", {"reason": "SKIP_STALE"})
            return len(ids)

    def _event(
        self,
        db: sqlite3.Connection,
        item_id: int,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO events(item_id, event_type, created_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (item_id, event_type, _iso(utc_now()), json.dumps(payload or {})),
        )

    def ingest(self, item: IncomingItem) -> IngestOutcome:
        now = _iso(utc_now())
        content_hash = _content_hash(item)
        dedupe_hash = _dedupe_hash(item.text)
        clean_source_text = normalize_source_language(item.text).clean_text
        with self.connect() as db:
            existing = db.execute(
                "SELECT id, content_hash, state, source_text, duplicate_of, "
                "telegram_message_id FROM items "
                "WHERE source_id = ? AND external_id = ?",
                (item.source_id, item.external_id),
            ).fetchone()

            if existing:
                item_id = int(existing["id"])
                deleted_by_editor = (
                    str(existing["state"]) == "cancelled_editor_deleted"
                )
                if existing["content_hash"] == content_hash:
                    db.execute(
                        "UPDATE items SET last_seen_at = ?, source_deleted = 0, "
                        "clean_source_text = ?, media_url = COALESCE(?, media_url) "
                        "WHERE id = ?",
                        (now, clean_source_text, item.media_url, item_id),
                    )
                    return IngestOutcome(item_id, IngestResult.UNCHANGED)

                if deleted_by_editor:
                    db.execute(
                        """
                        UPDATE items
                        SET canonical_url = ?, source_published_at = ?, last_seen_at = ?,
                            source_text = ?, clean_source_text = ?, content_hash = ?,
                            dedupe_hash = ?, media_url = COALESCE(?, media_url),
                            source_deleted = 0
                        WHERE id = ?
                        """,
                        (
                            item.canonical_url,
                            _iso(item.published_at),
                            now,
                            item.text,
                            clean_source_text,
                            content_hash,
                            dedupe_hash,
                            item.media_url,
                            item_id,
                        ),
                    )
                    self._event(db, item_id, "source_updated_after_editor_deletion")
                    return IngestOutcome(item_id, IngestResult.UPDATED)

                next_state = (
                    "needs_update"
                    if existing["state"] == "published"
                    or existing["telegram_message_id"] is not None
                    else "duplicate"
                    if existing["duplicate_of"] is not None
                    else "queued"
                )
                db.execute(
                    """
                    UPDATE items
                    SET canonical_url = ?, source_published_at = ?, last_seen_at = ?,
                        source_text = ?, clean_source_text = ?, content_hash = ?,
                        dedupe_hash = ?, media_url = COALESCE(?, media_url),
                        source_deleted = 0, state = ?,
                        draft_text = NULL, image_path = NULL,
                        card_category = NULL, card_headline = NULL,
                        card_secondary = NULL, reviewed_at = NULL, last_error = NULL,
                        editorial_category = NULL, editorial_importance = NULL,
                        editorial_interest = NULL, editorial_novelty = NULL,
                        editorial_priority = NULL, editorial_mode = NULL,
                        editorial_diversity_adjustment = NULL,
                        editorial_reason = NULL
                    WHERE id = ?
                    """,
                    (
                        item.canonical_url,
                        _iso(item.published_at),
                        now,
                        item.text,
                        clean_source_text,
                        content_hash,
                        dedupe_hash,
                        item.media_url,
                        next_state,
                        item_id,
                    ),
                )
                self._event(db, item_id, "source_updated")
                return IngestOutcome(item_id, IngestResult.UPDATED)

            noncanonical_placeholders = ",".join(
                "?" for _ in _NON_CANONICAL_DEDUPE_STATES
            )
            duplicate = db.execute(
                "SELECT id FROM items WHERE dedupe_hash = ? AND duplicate_of IS NULL "
                f"AND state NOT IN ({noncanonical_placeholders}) ORDER BY id LIMIT 1",
                (dedupe_hash, *_NON_CANONICAL_DEDUPE_STATES),
            ).fetchone()
            if duplicate is None:
                window_start = _iso(item.published_at - _DEDUPE_WINDOW)
                window_end = _iso(item.published_at + _DEDUPE_WINDOW)
                candidates = db.execute(
                    "SELECT id, source_text FROM items "
                    "WHERE duplicate_of IS NULL "
                    f"AND state NOT IN ({noncanonical_placeholders}) "
                    "AND source_published_at BETWEEN ? AND ? "
                    "ORDER BY id DESC LIMIT 250",
                    (*_NON_CANONICAL_DEDUPE_STATES, window_start, window_end),
                ).fetchall()
                duplicate = next(
                    (
                        candidate
                        for candidate in candidates
                        if _is_near_duplicate(item.text, str(candidate["source_text"]))
                        and not _is_meaningful_story_development(
                            item.text, str(candidate["source_text"])
                        )
                    ),
                    None,
                )
            if duplicate is None:
                story_window_start = _iso(item.published_at - _STORY_WINDOW)
                story_window_end = _iso(item.published_at + _STORY_WINDOW)
                story_candidates = db.execute(
                    "SELECT id, source_text FROM items "
                    "WHERE duplicate_of IS NULL "
                    f"AND state NOT IN ({noncanonical_placeholders}) "
                    "AND source_published_at BETWEEN ? AND ? "
                    "ORDER BY id DESC LIMIT 500",
                    (
                        *_NON_CANONICAL_DEDUPE_STATES,
                        story_window_start,
                        story_window_end,
                    ),
                ).fetchall()
                duplicate = next(
                    (
                        candidate
                        for candidate in story_candidates
                        if _is_same_story(item.text, str(candidate["source_text"]))
                    ),
                    None,
                )
            duplicate_of = int(duplicate["id"]) if duplicate else None
            state = "duplicate" if duplicate_of else "queued"
            cursor = db.execute(
                """
                INSERT INTO items(
                    source_id, external_id, canonical_url, source_published_at,
                    discovered_at, last_seen_at, source_text, clean_source_text,
                    media_url, content_hash, dedupe_hash, duplicate_of, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_id,
                    item.external_id,
                    item.canonical_url,
                    _iso(item.published_at),
                    now,
                    now,
                    item.text,
                    clean_source_text,
                    item.media_url,
                    content_hash,
                    dedupe_hash,
                    duplicate_of,
                    state,
                ),
            )
            item_id = int(cursor.lastrowid)
            self._event(
                db,
                item_id,
                "duplicate_detected" if duplicate_of else "discovered",
                {"duplicate_of": duplicate_of} if duplicate_of else None,
            )
            result = IngestResult.DUPLICATE if duplicate_of else IngestResult.NEW
            return IngestOutcome(item_id, result, duplicate_of)

    @staticmethod
    def _recent_published_duplicate_in_db(
        db: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> int | None:
        """Find the canonical recent Telegram story repeated by *row*.

        Ingestion remains the cheap first gate.  This second gate compares the
        trusted source together with the final Russian draft, which catches
        cross-language and cross-source repeats that become obvious only after
        rewrite.  A genuine later factual stage remains publishable.
        """

        item_id = int(row["id"])
        if row["telegram_message_id"] is not None:
            return None
        published_at = datetime.fromisoformat(str(row["source_published_at"]))
        window_start = _iso(published_at - _STORY_WINDOW)
        window_end = _iso(published_at + _STORY_WINDOW)
        candidate_text = _row_story_text(row)
        if not candidate_text:
            return None
        candidate_source = str(row["clean_source_text"] or row["source_text"] or "").strip()
        published_rows = db.execute(
            "SELECT * FROM items WHERE id <> ? AND telegram_message_id IS NOT NULL "
            "AND source_published_at BETWEEN ? AND ? "
            "ORDER BY telegram_published_at DESC, telegram_message_id DESC LIMIT 250",
            (item_id, window_start, window_end),
        ).fetchall()
        for published in published_rows:
            known_text = _row_story_text(published)
            known_source = str(
                published["clean_source_text"] or published["source_text"] or ""
            ).strip()
            known_at = datetime.fromisoformat(str(published["source_published_at"]))
            short_window_repeat = (
                abs(published_at - known_at) <= timedelta(hours=12)
                and _short_window_story_duplicate(candidate_text, known_text)
            )
            # Determine factual stage from the sources as well as from the
            # source+draft search surface.  A fluent rewrite such as
            # "подписал" must not make a simultaneous Here-We-Go repeat look
            # like a later OFFICIAL development.
            if (
                _is_same_story(candidate_source, known_source)
                or _is_same_story(candidate_text, known_text)
                or short_window_repeat
            ) or (
                _is_near_duplicate(candidate_text, known_text)
                and not _is_meaningful_story_development(candidate_text, known_text)
            ):
                return int(published["id"])
        return None

    def suppress_if_recent_published_duplicate(self, item_id: int) -> int | None:
        """Atomically retire an unpublished repeat before any outbound send."""

        active_states = (
            "queued", "scheduled", "needs_photo", "needs_headline_review",
            "ready_for_review", "approved", "visual_pending", "visual_processing",
        )
        placeholders = ",".join("?" for _ in active_states)
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None or str(row["state"]) not in active_states:
                return None
            duplicate_of = self._recent_published_duplicate_in_db(db, row)
            if duplicate_of is None:
                return None
            cursor = db.execute(
                f"UPDATE items SET state = 'duplicate', duplicate_of = ?, "
                "scheduled_for = NULL, image_path = NULL, visual_manifest_path = NULL, "
                "reviewed_at = ?, last_error = 'Повтор уже опубликованной истории' "
                f"WHERE id = ? AND telegram_message_id IS NULL AND state IN ({placeholders})",
                (duplicate_of, _iso(utc_now()), item_id, *active_states),
            )
            if cursor.rowcount != 1:
                return None
            self._event(
                db,
                item_id,
                "published_story_duplicate_suppressed",
                {"duplicate_of": duplicate_of},
            )
            return duplicate_of

    def apply_event_identity_gate(
        self,
        item_id: int,
        *,
        now: datetime | None = None,
    ) -> EventGateDecision:
        """Apply the compact 24-hour event/stage gate before editorial or send.

        The comparison is source-local and deterministic.  It does not use the
        rewritten draft, so a model cannot manufacture a higher factual stage.
        Conflicting transfer directions are held for review; same-stage repeats
        are retired against the canonical row.
        """

        active_states = (
            "queued", "scheduled", "needs_update", "needs_photo",
            "needs_headline_review", "ready_for_review", "ready_update_review",
            "approved", "approved_update", "visual_pending", "visual_processing",
        )
        placeholders = ",".join("?" for _ in active_states)
        current_time = (now or utc_now()).astimezone(timezone.utc)
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ValueError(f"Item not found: {item_id}")
            if row["telegram_message_id"] is not None or str(row["state"]) not in active_states:
                return EventGateDecision(EventGateAction.ALLOW, "ITEM_NOT_GATE_ELIGIBLE")

            source_text = str(row["clean_source_text"] or row["source_text"] or "").strip()
            promo = compare_event_identity(source_text, "")
            if promo.action is EventGateAction.SKIP_PROMO:
                db.execute(
                    f"UPDATE items SET state = 'rejected_low_information', scheduled_for = NULL, "
                    f"last_error = 'SKIP_PROMO' WHERE id = ? AND state IN ({placeholders})",
                    (item_id, *active_states),
                )
                self._event(db, item_id, "event_identity_skipped", {"reason": promo.reason})
                return promo

            identity = build_event_identity(source_text)
            if identity is None:
                return EventGateDecision(EventGateAction.ALLOW, "NO_EVENT_IDENTITY")

            source_at_raw = str(row["source_published_at"] or row["discovered_at"] or "")
            try:
                source_at = datetime.fromisoformat(source_at_raw).astimezone(timezone.utc)
            except ValueError:
                source_at = current_time
            window_start = _iso(source_at - _EVENT_COOLDOWN)
            window_end = _iso(source_at + _EVENT_COOLDOWN)
            candidates = db.execute(
                "SELECT * FROM items WHERE id <> ? AND duplicate_of IS NULL "
                "AND state NOT IN ('baseline','rejected_advertising','rejected_low_information',"
                "'rejected_low_priority','rejected_quality','rejected_manual',"
                "'cancelled_source_deleted','deleted_source') "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) BETWEEN ? AND ? "
                "ORDER BY CASE WHEN telegram_message_id IS NULL THEN 1 ELSE 0 END, "
                "COALESCE(NULLIF(source_published_at, ''), discovered_at) DESC, id DESC LIMIT 500",
                (item_id, window_start, window_end),
            ).fetchall()

            compared: list[tuple[EventGateDecision, sqlite3.Row]] = []
            for candidate in candidates:
                known_text = str(
                    candidate["clean_source_text"] or candidate["source_text"] or ""
                ).strip()
                decision = compare_event_identity(source_text, known_text)
                if decision.action is not EventGateAction.ALLOW:
                    compared.append((decision, candidate))

            conflict = next(
                (
                    (decision, candidate)
                    for decision, candidate in compared
                    if decision.action is EventGateAction.DEFER
                ),
                None,
            )
            if conflict is not None:
                decision, candidate = conflict
                related_id = int(candidate["id"])
                db.execute(
                    f"UPDATE items SET state = 'needs_editor_review', scheduled_for = NULL, "
                    f"last_error = ? WHERE id = ? AND state IN ({placeholders})",
                    (f"{decision.reason}:{related_id}", item_id, *active_states),
                )
                self._event(
                    db,
                    item_id,
                    "event_identity_deferred",
                    {"reason": decision.reason, "related_item_id": related_id},
                )
                return EventGateDecision(
                    decision.action,
                    decision.reason,
                    decision.identity,
                    related_id,
                )

            repeated = next(
                (
                    (decision, candidate)
                    for decision, candidate in compared
                    if decision.action is EventGateAction.SUPPRESS
                ),
                None,
            )
            if repeated is not None:
                decision, candidate = repeated
                related_id = int(candidate["id"])
                db.execute(
                    f"UPDATE items SET state = 'duplicate', duplicate_of = ?, "
                    f"scheduled_for = NULL, image_path = NULL, visual_manifest_path = NULL, "
                    f"last_error = ? WHERE id = ? AND telegram_message_id IS NULL "
                    f"AND state IN ({placeholders})",
                    (
                        related_id,
                        f"{decision.reason}: 24h semantic event cooldown",
                        item_id,
                        *active_states,
                    ),
                )
                self._event(
                    db,
                    item_id,
                    "event_identity_suppressed",
                    {"reason": decision.reason, "duplicate_of": related_id},
                )
                return EventGateDecision(
                    decision.action,
                    decision.reason,
                    decision.identity,
                    related_id,
                )

            return EventGateDecision(EventGateAction.ALLOW, "EVENT_STAGE_ALLOWED", identity)

    def collapse_story_duplicates(self) -> tuple[tuple[int, int], ...]:
        """Cancel active copies of a story already stored by another source.

        Published and editor-deleted originals remain canonical. This makes a
        manual deletion terminal for the story instead of inviting another feed
        to publish a rewritten copy a few minutes later.
        """
        active_states = (
            "queued", "scheduled", "needs_update", "needs_photo",
            "ready_for_review", "ready_update_review", "approved", "approved_update",
            "visual_pending",
        )
        collapsed: list[tuple[int, int]] = []
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, source_published_at, source_text, state FROM items "
                "WHERE duplicate_of IS NULL ORDER BY source_published_at, id"
            ).fetchall()
            for index, row in enumerate(rows):
                if str(row["state"]) not in active_states:
                    continue
                published_at = datetime.fromisoformat(str(row["source_published_at"]))
                duplicate_of: int | None = None
                for candidate in reversed(rows[:index]):
                    candidate_at = datetime.fromisoformat(str(candidate["source_published_at"]))
                    if published_at - candidate_at > _STORY_WINDOW:
                        break
                    if str(candidate["state"]) == "duplicate" or str(
                        candidate["state"]
                    ) in _NON_CANONICAL_DEDUPE_STATES:
                        continue
                    if _is_same_story(str(row["source_text"]), str(candidate["source_text"])):
                        duplicate_of = int(candidate["id"])
                        break
                if duplicate_of is None:
                    continue
                item_id = int(row["id"])
                db.execute(
                    "UPDATE items SET state = 'duplicate', duplicate_of = ?, "
                    "scheduled_for = NULL, last_error = 'Повтор уже известной истории' "
                    "WHERE id = ?",
                    (duplicate_of, item_id),
                )
                self._event(db, item_id, "story_duplicate_collapsed", {"duplicate_of": duplicate_of})
                collapsed.append((item_id, duplicate_of))
        return tuple(collapsed)

    def mark_source_deleted(
        self,
        source_id: str,
        external_id: str,
        delete_mirror: bool = True,
    ) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, source_deleted, state FROM items "
                "WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ).fetchone()
            if not row or row["source_deleted"]:
                return False
            item_id = int(row["id"])
            state = str(row["state"])
            if state == "cancelled_editor_deleted":
                next_state = state
            elif delete_mirror and state == "published":
                next_state = "needs_delete"
            elif delete_mirror and state not in {
                "published",
                "needs_delete",
                "deleted_source",
            }:
                next_state = "cancelled_source_deleted"
            else:
                next_state = state
            db.execute(
                "UPDATE items SET source_deleted = 1, state = ? WHERE id = ?",
                (next_state, item_id),
            )
            self._event(
                db,
                item_id,
                "source_deleted",
                {"delete_mirror": delete_mirror, "next_state": next_state},
            )
            return True

    def reconcile_source_window(
        self,
        source_id: str,
        seen_external_ids: tuple[str, ...],
        *,
        delete_mirror: bool = True,
    ) -> int:
        numeric_seen = {int(value) for value in seen_external_ids if value.isdigit()}
        if len(numeric_seen) != len(seen_external_ids) or not numeric_seen:
            return 0
        lower = min(numeric_seen)
        upper = max(numeric_seen)
        changed = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, external_id, source_deleted, state FROM items "
                "WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            for row in rows:
                external = str(row["external_id"])
                if not external.isdigit():
                    continue
                number = int(external)
                if number < lower or number > upper or number in numeric_seen:
                    continue
                if row["source_deleted"]:
                    continue
                item_id = int(row["id"])
                state = str(row["state"])
                if state == "cancelled_editor_deleted":
                    next_state = state
                elif delete_mirror and state == "published":
                    next_state = "needs_delete"
                elif delete_mirror and state not in {"needs_delete", "deleted_source"}:
                    next_state = "cancelled_source_deleted"
                else:
                    next_state = state
                db.execute(
                    "UPDATE items SET source_deleted = 1, state = ? WHERE id = ?",
                    (next_state, item_id),
                )
                self._event(
                    db,
                    item_id,
                    "source_deleted_in_window",
                    {"lower": lower, "upper": upper, "next_state": next_state},
                )
                changed += 1
        return changed

    def mark_filtered(
        self,
        item_id: int,
        reason: str,
        evidence: tuple[str, ...] = (),
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET state = 'rejected_advertising', scheduled_for = NULL "
                "WHERE id = ? AND state != 'published'",
                (item_id,),
            )
            self._event(
                db,
                item_id,
                "rejected_advertising",
                {"reason": reason, "evidence": list(evidence)},
            )

    def mark_low_information(self, item_id: int, reason: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'rejected_low_information', "
                "scheduled_for = NULL, last_error = ? WHERE id = ? "
                "AND state NOT IN ('published', 'needs_update')",
                (reason[:1000], item_id),
            )
            if cursor.rowcount:
                self._event(db, item_id, "rejected_low_information", {"reason": reason})

    def mark_low_priority_backlog(self, item_id: int, score: int) -> None:
        """Drop a stale non-hype story before it consumes editor/card capacity."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'rejected_low_priority', scheduled_for = NULL, "
                "last_error = ? WHERE id = ? AND state IN "
                "('queued', 'scheduled', 'needs_photo', 'ready_for_review', 'approved')",
                (f"Устаревший офлайн-бэклог, hype_score={score}", item_id),
            )
            if cursor.rowcount:
                self._event(db, item_id, "rejected_low_priority_backlog", {"score": score})

    def source_is_initialised(self, source_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM source_state WHERE source_id = ?", (source_id,)
            ).fetchone()
            return row is not None

    def complete_source_baseline(self, source_id: str) -> int:
        now = _iso(utc_now())
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'baseline' "
                "WHERE source_id = ? AND state = 'queued'",
                (source_id,),
            )
            db.execute(
                "INSERT INTO source_state(source_id, baseline_completed_at) "
                "VALUES (?, ?) ON CONFLICT(source_id) DO NOTHING",
                (source_id, now),
            )
            return int(cursor.rowcount)

    def queued_items(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            query = (
                "SELECT * FROM items "
                "WHERE state = 'queued' AND duplicate_of IS NULL ORDER BY "
                + _EDITORIAL_PRIORITY_SQL
                + ", " + _SOURCE_PRIORITY_SQL
                + ", source_published_at, id"
            )
            return list(
                db.execute(query).fetchall()
            )

    def production_editorial_candidates(
        self,
        cutoff: datetime,
        expires_after: datetime,
    ) -> list[sqlite3.Row]:
        """Return fresh text candidates by urgency, freshness, then stable ID."""
        with self.connect() as db:
            query = (
                "SELECT * FROM items WHERE state IN ('queued', 'scheduled') "
                "AND draft_text IS NULL AND duplicate_of IS NULL "
                "AND telegram_message_id IS NULL AND source_deleted = 0 "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) > ? "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) > ? "
                "ORDER BY " + _EDITORIAL_PRIORITY_SQL
                + ", COALESCE(NULLIF(source_published_at, ''), discovered_at) DESC, id"
            )
            return list(db.execute(query, (_iso(cutoff), _iso(expires_after))).fetchall())

    def production_queue_depth(
        self,
        cutoff: datetime,
        expires_after: datetime,
    ) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS value FROM items WHERE state IN "
                "('queued','scheduled','ready_for_review','approved','visual_pending',"
                "'visual_processing','visual_delivery_unknown') "
                "AND duplicate_of IS NULL AND telegram_message_id IS NULL "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) > ? "
                "AND COALESCE(NULLIF(source_published_at, ''), discovered_at) > ?",
                (_iso(cutoff), _iso(expires_after)),
            ).fetchone()
            return int(row["value"] if row else 0)

    def save_editorial_decision(
        self,
        item_id: int,
        *,
        category: str,
        importance: int,
        interest: int,
        novelty: int,
        mode: str,
        reason: str,
    ) -> None:
        """Persist ranking metadata without making it part of article facts."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET editorial_category = ?, editorial_importance = ?, "
                "editorial_interest = ?, editorial_novelty = ?, editorial_mode = ?, "
                "editorial_reason = ? WHERE id = ? AND state IN "
                "('queued', 'scheduled', 'approved', 'approved_update')",
                (
                    category,
                    importance,
                    interest,
                    novelty,
                    mode,
                    reason[:160],
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Editorial candidate not found: {item_id}")
            self._event(
                db,
                item_id,
                "editorial_ranked",
                {
                    "category": category,
                    "importance": importance,
                    "interest": interest,
                    "novelty": novelty,
                    "mode": mode,
                    "reason": reason[:160],
                },
            )

    def record_editorial_selection(
        self,
        item_id: int,
        *,
        priority: float,
        diversity_adjustment: float,
        selected: bool,
    ) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET editorial_priority = ?, "
                "editorial_diversity_adjustment = ? WHERE id = ?",
                (priority, diversity_adjustment, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Editorial candidate not found: {item_id}")
            if selected:
                self._event(
                    db,
                    item_id,
                    "editorial_selected",
                    {
                        "priority": priority,
                        "diversity_adjustment": diversity_adjustment,
                    },
                )

    def recent_published_editorial_rows(self, *, limit: int = 5) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE telegram_message_id IS NOT NULL "
                    "AND state NOT IN ('duplicate', 'cancelled_editor_deleted') "
                    "ORDER BY telegram_published_at DESC, telegram_message_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )

    def production_candidates(
        self,
        source_ids: tuple[str, ...],
        activation_watermark: datetime,
    ) -> list[sqlite3.Row]:
        """Return recent F.1 candidates in stable first-seen order.

        Both discovery and source timestamps are bounded. This prevents an old
        stored record or a newly rediscovered historical feed entry from
        entering continuous production after activation.
        """
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        watermark = _iso(activation_watermark)
        with self.connect() as db:
            return list(
                db.execute(
                    f"SELECT * FROM items WHERE source_id IN ({placeholders}) "
                    "AND state = 'queued' AND duplicate_of IS NULL "
                    "AND telegram_message_id IS NULL AND source_deleted = 0 "
                    "AND discovered_at >= ? AND source_published_at >= ? "
                    "ORDER BY discovered_at, source_published_at, id",
                    (*source_ids, watermark, watermark),
                ).fetchall()
            )

    def mark_production_skipped(self, item_id: int, reason: str) -> None:
        value = reason.strip()[:1000] or "F1_SAFE_SKIP"
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'rejected_quality', scheduled_for = NULL, "
                "last_error = ? WHERE id = ? AND state = 'queued' "
                "AND telegram_message_id IS NULL",
                (value, item_id),
            )
            if cursor.rowcount:
                self._event(db, item_id, "f1_safe_skip", {"reason": value})

    def stale_backlog_candidates(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE duplicate_of IS NULL "
                    "AND COALESCE(redesign_count, 0) = 0 AND state IN "
                    "('queued', 'scheduled', 'needs_photo', 'ready_for_review', 'approved')"
                ).fetchall()
            )

    def set_schedule(self, item_id: int, scheduled_for: datetime) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET scheduled_for = ?, state = 'scheduled' WHERE id = ?",
                (_iso(scheduled_for), item_id),
            )
            self._event(
                db, item_id, "scheduled", {"scheduled_for": _iso(scheduled_for)}
            )

    def last_scheduled_at(self) -> datetime | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(scheduled_for) AS value FROM items "
                "WHERE scheduled_for IS NOT NULL"
            ).fetchone()
            if not row or not row["value"]:
                return None
            return datetime.fromisoformat(row["value"])

    def counts_by_state(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT state, COUNT(*) AS count FROM items GROUP BY state"
            ).fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}

    def items_with_state(self, state: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE state = ? ORDER BY id", (state,)
                ).fetchall()
            )

    def scheduled_without_draft(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items "
                    "WHERE state IN ('scheduled', 'needs_update') "
                    "AND draft_text IS NULL ORDER BY "
                    + _EDITORIAL_PRIORITY_SQL
                    + ", " + _SOURCE_PRIORITY_SQL
                    + ", scheduled_for, id"
                ).fetchall()
            )

    def save_draft(self, item_id: int, draft_text: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET draft_text = ? "
                "WHERE id = ? AND state IN ('scheduled', 'needs_update')",
                (draft_text, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Editable item not found: {item_id}")
            self._event(db, item_id, "draft_generated")

    def mark_editor_failed(self, item_id: int, reason: str) -> None:
        """Retire a repeatedly invalid LLM result without inflating the live queue."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'rejected_quality' ELSE 'published' END, scheduled_for = NULL, "
                "draft_text = NULL, image_path = NULL, last_error = ? "
                "WHERE id = ? AND state IN "
                "('scheduled', 'needs_update', 'visual_pending')",
                (reason[:1000], item_id),
            )
            if cursor.rowcount:
                self._event(db, item_id, "rejected_quality", {"stage": "editor", "reason": reason[:1000]})

    def mark_quality_rejected(self, item_id: int, reason: str, *, stage: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'rejected_quality' ELSE 'published' END, scheduled_for = NULL, "
                "image_path = NULL, visual_style = NULL, last_error = ? "
                "WHERE id = ? AND state IN ('scheduled', 'needs_update', "
                "'ready_for_review', 'ready_update_review', 'needs_photo', "
                "'needs_headline_review')",
                (reason[:1000], item_id),
            )
            if cursor.rowcount:
                self._event(
                    db,
                    item_id,
                    "rejected_quality",
                    {"stage": stage, "reason": reason[:1000]},
                )

    def record_quality_soft_pass(self, item_id: int, reason: str, *, stage: str) -> None:
        """Persist every hybrid override so relaxed checks remain auditable."""
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Item not found: {item_id}")
            self._event(
                db,
                item_id,
                "quality_soft_pass",
                {"stage": stage, "reason": reason[:1000]},
            )

    def archive_quality_holds(self) -> int:
        """Move historical manual holds out of the active queue without deleting data."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'rejected_quality' ELSE 'published' END, scheduled_for = NULL "
                "WHERE state IN ('needs_editor_review', 'needs_headline_review', 'needs_photo')"
            )
            return int(cursor.rowcount)

    def get_item(self, item_id: int) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ValueError(f"Item not found: {item_id}")
            return row

    def text_only_candidates(self) -> list[sqlite3.Row]:
        """Drafts ready for the text publication path."""
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items "
                    "WHERE state IN ('scheduled', 'needs_update', 'needs_photo', "
                    "'needs_headline_review') AND draft_text IS NOT NULL "
                    "AND duplicate_of IS NULL ORDER BY "
                    + _EDITORIAL_PRIORITY_SQL
                    + ", " + _SOURCE_PRIORITY_SQL
                    + ", scheduled_for, id"
                ).fetchall()
            )

    def prepare_text_only(self, item_id: int) -> None:
        """Move a text draft directly to review."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET image_path = NULL, card_category = NULL, "
                "card_headline = NULL, card_secondary = NULL, visual_style = NULL, "
                "state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'ready_for_review' ELSE 'ready_update_review' END, "
                "last_error = NULL WHERE id = ? AND draft_text IS NOT NULL "
                "AND state IN ('scheduled', 'needs_update', 'needs_photo', "
                "'needs_headline_review', 'ready_for_review', 'ready_update_review')",
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot be prepared as text-only: {item_id}")
            self._event(db, item_id, "text_only_prepared")

    def visual_work_active(self) -> bool:
        """Return whether one story already owns the ImageGen handoff slot."""

        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM items WHERE state IN "
                "('visual_pending', 'visual_processing', 'visual_delivery_unknown') "
                "LIMIT 1"
            ).fetchone()
            return row is not None

    def active_visual_item(self) -> sqlite3.Row | None:
        """Return the single active visual handoff without changing its state."""

        with self.connect() as db:
            return db.execute(
                "SELECT * FROM items WHERE state IN "
                "('visual_pending', 'visual_processing', 'visual_delivery_unknown') "
                "ORDER BY reviewed_at, id LIMIT 1"
            ).fetchone()

    def prepare_visual_pending(self, item_id: int) -> None:
        """Reserve a rewritten story for the external Codex ImageGen worker."""

        now = _iso(utc_now())
        with self.connect() as db:
            active = db.execute(
                "SELECT id FROM items WHERE state IN "
                "('visual_pending', 'visual_processing', 'visual_delivery_unknown') "
                "AND id <> ? LIMIT 1",
                (item_id,),
            ).fetchone()
            if active is not None:
                raise ValueError(f"Visual handoff already occupied by item {int(active['id'])}")
            cursor = db.execute(
                "UPDATE items SET state = 'visual_pending', image_path = NULL, "
                "visual_manifest_path = NULL, reviewed_at = ?, last_error = NULL "
                "WHERE id = ? AND telegram_message_id IS NULL "
                "AND draft_text IS NOT NULL AND state = 'scheduled'",
                (now, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot enter visual handoff: {item_id}")
            self._event(db, item_id, "visual_pending")

    def promote_waiting_visual_draft(self) -> int | None:
        """Move the best already-rewritten draft into the single visual slot.

        A scheduled row with ``draft_text`` is the lightweight visual waiting
        queue.  This keeps Qwen/editorial work independent from slow ImageGen
        delivery without allowing more than one active visual handoff.
        """

        now = utc_now()
        now_value = _iso(now)
        with self.connect() as db:
            active = db.execute(
                "SELECT 1 FROM items WHERE state IN "
                "('visual_pending', 'visual_processing', 'visual_delivery_unknown') "
                "LIMIT 1"
            ).fetchone()
            if active is not None:
                return None
            rows = db.execute(
                "SELECT * FROM items WHERE state = 'scheduled' "
                "AND draft_text IS NOT NULL "
                "AND duplicate_of IS NULL AND source_deleted = 0 ORDER BY "
                + _EDITORIAL_PRIORITY_SQL
                + ", COALESCE(scheduled_for, source_published_at), id"
            ).fetchall()
            for row in rows:
                item_id = int(row["id"])
                duplicate_of = self._recent_published_duplicate_in_db(db, row)
                if duplicate_of is not None:
                    db.execute(
                        "UPDATE items SET state = 'duplicate', duplicate_of = ?, "
                        "scheduled_for = NULL, image_path = NULL, "
                        "visual_manifest_path = NULL, reviewed_at = ?, "
                        "last_error = 'Повтор уже опубликованной истории' "
                        "WHERE id = ? AND state = 'scheduled' "
                        "AND telegram_message_id IS NULL",
                        (duplicate_of, now_value, item_id),
                    )
                    self._event(
                        db,
                        item_id,
                        "published_story_duplicate_suppressed",
                        {"duplicate_of": duplicate_of, "stage": "visual_promotion"},
                    )
                    continue
                cursor = db.execute(
                    "UPDATE items SET state = 'visual_pending', image_path = NULL, "
                    "visual_manifest_path = NULL, reviewed_at = ?, last_error = NULL "
                    "WHERE id = ? AND state = 'scheduled' AND draft_text IS NOT NULL "
                    "AND duplicate_of IS NULL",
                    (now_value, item_id),
                )
                if cursor.rowcount == 1:
                    self._event(db, item_id, "visual_pending")
                    self._event(db, item_id, "visual_waiting_promoted")
                    return item_id
            return None

    def release_expired_visual_work(self, older_than: datetime) -> int:
        """Fail open to the established text publisher before visual can stall news."""

        cutoff = _iso(older_than)
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, state FROM items WHERE state IN "
                "('visual_pending', 'visual_processing') AND reviewed_at <= ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                item_id = int(row["id"])
                previous = str(row["state"])
                db.execute(
                    "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                    "THEN 'approved' ELSE 'published' END, image_path = NULL, "
                    "visual_manifest_path = NULL, reviewed_at = ?, "
                    "last_error = 'IMAGEGEN_TIMEOUT_TEXT_FALLBACK' WHERE id = ?",
                    (_iso(utc_now()), item_id),
                )
                self._event(
                    db,
                    item_id,
                    "visual_text_fallback",
                    {"reason": "IMAGEGEN_TIMEOUT", "previous_state": previous},
                )
            return len(rows)

    def retire_visual_handoffs_for_text_only(self) -> int:
        """Return unambiguous legacy visual work to the text-only pipeline.

        A row with a Telegram mapping is already delivered and becomes
        ``published``. An undelivered row becomes ``approved`` so the ordinary
        text publisher can handle it. Delivery-unknown rows are intentionally
        excluded because their Telegram outcome requires reconciliation.
        """

        now = _iso(utc_now())
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, state, telegram_message_id FROM items WHERE state IN "
                "('visual_pending', 'visual_processing')"
            ).fetchall()
            for row in rows:
                item_id = int(row["id"])
                previous = str(row["state"])
                delivered = row["telegram_message_id"] is not None
                db.execute(
                    "UPDATE items SET state = ?, image_path = NULL, "
                    "visual_manifest_path = NULL, reviewed_at = ?, "
                    "last_error = 'TEXT_ONLY_VISUAL_HANDOFF_RETIRED' WHERE id = ?",
                    ("published" if delivered else "approved", now, item_id),
                )
                self._event(
                    db,
                    item_id,
                    "visual_handoff_retired_text_only",
                    {"previous_state": previous, "already_delivered": delivered},
                )
            return len(rows)

    def claim_due_visual_item(
        self,
        now: datetime,
        interval_minutes: int,
    ) -> sqlite3.Row | None:
        """Atomically claim one due visual without bypassing publication cadence."""

        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM items WHERE state = 'visual_pending' "
                "AND duplicate_of IS NULL "
                "ORDER BY COALESCE(scheduled_for, source_published_at), id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            # An early Telegram text is an existing publication: adding its
            # media is an edit and must not wait for another publication slot.
            if row["telegram_message_id"] is None:
                last = db.execute(
                    "SELECT MAX(created_at) AS value FROM events "
                    "WHERE event_type = 'published'"
                ).fetchone()
                if last and last["value"]:
                    last_at = datetime.fromisoformat(str(last["value"]))
                    if now - last_at < timedelta(
                        minutes=max(1, int(interval_minutes))
                    ):
                        return None
            item_id = int(row["id"])
            duplicate_of = self._recent_published_duplicate_in_db(db, row)
            if duplicate_of is not None:
                db.execute(
                    "UPDATE items SET state = 'duplicate', duplicate_of = ?, "
                    "scheduled_for = NULL, image_path = NULL, visual_manifest_path = NULL, "
                    "reviewed_at = ?, last_error = 'Повтор уже опубликованной истории' "
                    "WHERE id = ? AND state = 'visual_pending' "
                    "AND telegram_message_id IS NULL",
                    (duplicate_of, _iso(now), item_id),
                )
                self._event(
                    db,
                    item_id,
                    "published_story_duplicate_suppressed",
                    {"duplicate_of": duplicate_of, "stage": "visual_claim"},
                )
                return None
            cursor = db.execute(
                "UPDATE items SET state = 'visual_processing', reviewed_at = ?, "
                "last_error = NULL WHERE id = ? AND state = 'visual_pending'",
                (_iso(now), item_id),
            )
            if cursor.rowcount != 1:
                return None
            self._event(db, item_id, "visual_processing")
            return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def release_visual_item(self, item_id: int, reason: str) -> None:
        """Return a pre-delivery visual failure to the normal text-only path."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'approved' ELSE 'published' END, image_path = NULL, "
                "visual_manifest_path = NULL, reviewed_at = ?, last_error = ? "
                "WHERE id = ? AND state IN ('visual_pending', 'visual_processing')",
                (_iso(utc_now()), reason[:1000], item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Visual item cannot fall back to text: {item_id}")
            self._event(
                db,
                item_id,
                "visual_text_fallback",
                {"reason": reason[:1000]},
            )

    def refresh_visual_claim(self, item_id: int) -> None:
        """Extend one active pre-delivery claim after asset selection succeeds."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET reviewed_at = ? WHERE id = ? "
                "AND state = 'visual_processing'",
                (_iso(utc_now()), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Visual claim cannot be refreshed: {item_id}")
            self._event(db, item_id, "visual_claim_refreshed")

    def mark_visual_delivery_unknown(self, item_id: int, reason: str) -> None:
        """Quarantine an ambiguous Telegram send instead of risking a duplicate."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'visual_delivery_unknown', last_error = ? "
                "WHERE id = ? AND state = 'visual_processing'",
                (reason[:1000], item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Visual delivery state cannot be quarantined: {item_id}")
            self._event(
                db,
                item_id,
                "visual_delivery_unknown",
                {"reason": reason[:1000]},
            )

    def confirm_visual_not_delivered(self, item_id: int) -> None:
        """Release a quarantined visual only after a manual non-delivery check."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'visual_processing', reviewed_at = ?, "
                "last_error = 'VISUAL_NOT_DELIVERED_MANUALLY_CONFIRMED' "
                "WHERE id = ? AND state = 'visual_delivery_unknown'",
                (_iso(utc_now()), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Visual item is not awaiting delivery reconciliation: {item_id}"
                )
            self._event(
                db,
                item_id,
                "visual_not_delivered_confirmed",
                {"manual_reconciliation": True},
            )

    def mark_visual_published(
        self,
        item_id: int,
        message_id: int,
        *,
        image_path: Path,
        manifest_path: Path,
        public_text: str,
    ) -> None:
        """Complete the claimed visual delivery and retain its internal provenance."""

        published_at = utc_now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT telegram_message_id, telegram_published_at FROM items "
                "WHERE id = ? AND state = 'visual_processing'",
                (item_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"Visual item is not claimed: {item_id}")
            existing_message_id = existing["telegram_message_id"]
            if existing_message_id is not None and int(existing_message_id) != int(message_id):
                raise ValueError("Visual Telegram mapping changed during media upgrade")
            cursor = db.execute(
                "UPDATE items SET state = 'published', "
                "telegram_message_id = COALESCE(telegram_message_id, ?), "
                "draft_text = ?, published_text = ?, "
                "published_source_text = COALESCE(clean_source_text, source_text), "
                "published_source_content_hash = content_hash, "
                "image_path = ?, visual_manifest_path = ?, "
                "dzen_mapping_status = 'pending', dzen_delete_status = NULL, "
                "dzen_attempts = 0, telegram_published_at = "
                "COALESCE(telegram_published_at, ?), "
                "dzen_next_attempt_at = ?, dzen_last_error = NULL, "
                "last_error = NULL WHERE id = ? AND state = 'visual_processing'",
                (
                    message_id,
                    public_text,
                    public_text,
                    str(image_path.resolve()),
                    str(manifest_path.resolve()),
                    _iso(published_at),
                    _iso(published_at + timedelta(seconds=60)),
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Visual item is not claimed: {item_id}")
            event_type = "visual_upgraded" if existing_message_id is not None else "published"
            self._event(
                db,
                item_id,
                event_type,
                {"message_id": message_id, "visual": True},
            )

    def visual_text_preview_due(
        self,
        now: datetime,
        interval_minutes: int,
        max_posts_per_hour: int,
    ) -> sqlite3.Row | None:
        """Return one visual-bound draft eligible for immediate text delivery."""

        with self.connect() as db:
            last = db.execute(
                "SELECT MAX(created_at) AS value FROM events "
                "WHERE event_type = 'published'"
            ).fetchone()
            if last and last["value"]:
                last_at = datetime.fromisoformat(str(last["value"]))
                if now - last_at < timedelta(minutes=max(1, int(interval_minutes))):
                    return None
            recent = db.execute(
                "SELECT COUNT(*) AS value FROM events WHERE event_type = 'published' "
                "AND created_at >= ?",
                (_iso(now - timedelta(hours=1)),),
            ).fetchone()
            if recent and int(recent["value"] or 0) >= int(max_posts_per_hour):
                return None
            return db.execute(
                "SELECT * FROM items WHERE state IN ('visual_pending', 'scheduled') "
                "AND draft_text IS NOT NULL AND telegram_message_id IS NULL "
                "AND duplicate_of IS NULL AND source_deleted = 0 ORDER BY "
                + _EDITORIAL_PRIORITY_SQL
                + ", COALESCE(scheduled_for, source_published_at), id LIMIT 1"
            ).fetchone()

    def mark_visual_text_published(
        self,
        item_id: int,
        message_id: int,
        public_text: str,
    ) -> None:
        """Persist an early text post while retaining its pending visual state."""

        published_at = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET telegram_message_id = ?, draft_text = ?, "
                "published_text = ?, "
                "published_source_text = COALESCE(clean_source_text, source_text), "
                "published_source_content_hash = content_hash, "
                "telegram_published_at = ?, last_error = NULL WHERE id = ? "
                "AND state IN ('visual_pending', 'scheduled') "
                "AND draft_text IS NOT NULL AND telegram_message_id IS NULL",
                (message_id, public_text, public_text, _iso(published_at), item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Visual text cannot be marked published: {item_id}")
            self._event(
                db,
                item_id,
                "published",
                {"message_id": message_id, "visual_pending": True},
            )

    def mark_needs_headline_review(self, item_id: int, reason: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'needs_headline_review', image_path = NULL, "
                "visual_style = NULL, last_error = ? WHERE id = ? AND state IN "
                "('scheduled', 'needs_update', 'ready_for_review', "
                "'ready_update_review', 'rejected_manual')",
                (reason[:1000], item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot be held for headline review: {item_id}")
            self._event(db, item_id, "needs_headline_review", {"reason": reason[:1000]})

    def approve_item(self, item_id: int) -> None:
        now = _iso(utc_now())
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'approved' ELSE 'approved_update' END, "
                "reviewed_at = ?, last_error = NULL "
                "WHERE id = ? AND state IN ('ready_for_review', 'ready_update_review') "
                "AND draft_text IS NOT NULL",
                (now, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item is not ready for approval: {item_id}")
            self._event(db, item_id, "approved")

    def replace_review_draft(self, item_id: int, draft_text: str) -> None:
        value = draft_text.strip()
        if not value:
            raise ValueError("Draft text cannot be empty")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET draft_text = ?, last_error = NULL "
                "WHERE id = ? AND state IN ('ready_for_review', 'ready_update_review')",
                (value, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item is not waiting for review: {item_id}")
            self._event(db, item_id, "review_draft_replaced")

    def apply_publication_text_policy(self, item_id: int, draft_text: str) -> None:
        """Persist the final policy-clean caption used by the publisher."""
        value = draft_text.strip()
        if not value:
            raise ValueError("Publication text cannot be empty")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET draft_text = ? WHERE id = ? "
                "AND state IN ('approved', 'approved_update')",
                (value, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Approved item not found: {item_id}")
            self._event(db, item_id, "publication_text_sanitized")

    def requeue_editorial_policy(self, item_id: int, reason: str) -> None:
        """Return an approved legacy draft to the editor after a policy change."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'scheduled' ELSE 'needs_update' END, draft_text = NULL, "
                "image_path = NULL, card_category = NULL, card_headline = NULL, "
                "card_secondary = NULL, visual_style = NULL, reviewed_at = NULL, "
                "last_error = ? WHERE id = ? AND state IN ('approved', 'approved_update')",
                (reason[:1000], item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Approved item not found: {item_id}")
            self._event(
                db,
                item_id,
                "editorial_policy_requeued",
                {"reason": reason[:1000]},
            )

    def publication_policy_was_requeued(self, item_id: int) -> bool:
        """Return whether the terminal gate already granted this item one retry."""

        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM events WHERE item_id = ? "
                    "AND event_type = 'editorial_policy_requeued' LIMIT 1",
                    (item_id,),
                ).fetchone()
                is not None
            )

    def reject_publication_policy(self, item_id: int, reason: str) -> None:
        """Retire an approved draft that cannot be made safe by another rewrite."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = CASE WHEN telegram_message_id IS NULL "
                "THEN 'rejected_quality' ELSE 'published' END, scheduled_for = NULL, "
                "draft_text = NULL, image_path = NULL, reviewed_at = ?, last_error = ? "
                "WHERE id = ? AND state IN ('approved', 'approved_update')",
                (_iso(utc_now()), reason[:1000], item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Approved item not found: {item_id}")
            self._event(
                db,
                item_id,
                "publication_policy_rejected",
                {"reason": reason[:1000]},
            )

    def reject_item(self, item_id: int, reason: str = "") -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'rejected_manual', reviewed_at = ?, "
                "last_error = ? WHERE id = ? AND state IN "
                "('scheduled', 'needs_update', 'needs_photo', 'ready_for_review', "
                "'ready_update_review', 'approved', 'approved_update')",
                (_iso(utc_now()), reason.strip() or None, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot be rejected: {item_id}")
            self._event(db, item_id, "rejected_manual", {"reason": reason.strip()})

    def mark_published(self, item_id: int, message_id: int) -> None:
        with self.connect() as db:
            self._mark_published_in_db(db, item_id, message_id)

    def _mark_published_in_db(
        self,
        db: sqlite3.Connection,
        item_id: int,
        message_id: int,
    ) -> None:
        published_at = utc_now()
        cursor = db.execute(
            "UPDATE items SET state = 'published', telegram_message_id = ?, "
            "published_text = draft_text, "
            "published_source_text = COALESCE(clean_source_text, source_text), "
            "published_source_content_hash = content_hash, "
            "dzen_mapping_status = 'pending', dzen_delete_status = NULL, "
            "dzen_attempts = 0, telegram_published_at = ?, "
            "dzen_next_attempt_at = ?, dzen_last_error = NULL, "
            "last_error = NULL WHERE id = ? AND state = 'approved'",
            (
                message_id,
                _iso(published_at),
                _iso(published_at + timedelta(seconds=60)),
                item_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Item is not approved: {item_id}")
        self._event(db, item_id, "published", {"message_id": message_id})
    def mark_publish_failed(self, item_id: int, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET last_error = ? WHERE id = ?",
                (error[:1000], item_id),
            )
            self._event(db, item_id, "publish_failed", {"error": error[:1000]})

    def mark_updated(self, item_id: int) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'published', "
                "published_text = draft_text, "
                "published_source_text = COALESCE(clean_source_text, source_text), "
                "published_source_content_hash = content_hash, last_error = NULL "
                "WHERE id = ? AND state = 'approved_update' "
                "AND telegram_message_id IS NOT NULL",
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item is not approved for update: {item_id}")
            self._event(db, item_id, "published_message_updated")

    def mark_updated_with_message(self, item_id: int, message_id: int) -> None:
        """Use when a media post is replaced with a fresh text-only message."""
        published_at = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'published', telegram_message_id = ?, "
                "published_text = draft_text, "
                "published_source_text = COALESCE(clean_source_text, source_text), "
                "published_source_content_hash = content_hash, "
                "dzen_article_url = NULL, dzen_article_id = NULL, "
                "dzen_mapping_status = 'pending', dzen_delete_status = NULL, "
                "dzen_attempts = 0, telegram_published_at = ?, "
                "dzen_next_attempt_at = ?, dzen_last_error = NULL, "
                "last_error = NULL WHERE id = ? AND state = 'approved_update'",
                (
                    message_id,
                    _iso(published_at),
                    _iso(published_at + timedelta(seconds=60)),
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item is not approved for update: {item_id}")
            self._event(db, item_id, "published_message_replaced", {"message_id": message_id})

    def queue_x_mirror(self, item_id: int, text: str) -> None:
        clean = text.strip()
        if not clean:
            raise ValueError("X mirror text cannot be empty")
        paused_until = self.x_rate_limit_pause_until()
        with self.connect() as db:
            row = db.execute(
                "SELECT x_post_url, source_text, draft_text, card_category FROM items "
                "WHERE id = ? AND state = 'published' "
                "AND telegram_message_id IS NOT NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Published item not found: {item_id}")
            high_priority = is_immediate_news(
                clean,
                str(row["draft_text"] or ""),
                str(row["source_text"] or ""),
                category=str(row["card_category"] or ""),
            )
            if paused_until is not None and not high_priority:
                db.execute(
                    "UPDATE items SET x_text = ?, x_status = 'skipped', x_attempts = 0, "
                    "last_error = ? WHERE id = ?",
                    (
                        clean,
                        "X skipped: publishing temporarily unavailable due to daily limit",
                        item_id,
                    ),
                )
                self._event(
                    db,
                    item_id,
                    "x_skipped",
                    {"reason": "daily X publishing limit is active"},
                )
                return
            status = "needs_replace" if row["x_post_url"] else "pending"
            db.execute(
                "UPDATE items SET x_text = ?, x_status = ?, x_attempts = 0, "
                "x_next_attempt_at = NULL, last_error = NULL WHERE id = ?",
                (clean, status, item_id),
            )
            self._event(db, item_id, "x_mirror_queued", {"status": status})

    def x_mirror_queue(
        self, limit: int = 6, now: datetime | None = None
    ) -> list[sqlite3.Row]:
        current = now or utc_now()
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE (state = 'published' "
                    "AND telegram_message_id IS NOT NULL AND x_status IN "
                    "('pending', 'publish_failed', 'needs_replace', 'replace_failed') "
                    "AND (x_next_attempt_at IS NULL OR x_next_attempt_at <= ?)) "
                    "OR (x_status IN ('needs_delete', 'delete_failed') "
                    "AND (x_next_attempt_at IS NULL OR x_next_attempt_at <= ?)) "
                    "ORDER BY CASE WHEN x_status IN ('needs_delete', 'delete_failed') "
                    "THEN 0 ELSE 1 END, id LIMIT ?",
                    (_iso(current), _iso(current), limit),
                ).fetchall()
            )

    def mark_x_published(self, item_id: int, url: str, text: str) -> None:
        if not re.fullmatch(
            r"https://x\.com/[A-Za-z0-9_]+/status/[0-9]+(?:\?.*)?", url
        ):
            raise ValueError("X_DELIVERY_UNVERIFIED: exact X status URL required")
        with self.connect() as db:
            db.execute(
                "UPDATE items SET x_post_url = ?, x_text = ?, x_status = 'published', "
                "x_attempts = 0, x_next_attempt_at = NULL, x_published_at = ?, "
                "x_like_status = 'not_applicable', "
                "x_like_attempts = 0, x_like_next_attempt_at = NULL, "
                "last_error = NULL WHERE id = ?",
                (url, text, _iso(utc_now()), item_id),
            )
            self._event(db, item_id, "x_published", {"url": url})

    def mark_x_ready_to_republish(self, item_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET x_post_url = NULL, x_status = 'pending', "
                "x_attempts = 0, x_next_attempt_at = NULL WHERE id = ?",
                (item_id,),
            )

    def mark_x_deleted(self, item_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET x_status = 'deleted', x_like_status = 'not_applicable', "
                "x_attempts = 0, x_next_attempt_at = NULL, last_error = NULL WHERE id = ?",
                (item_id,),
            )
            self._event(db, item_id, "x_deleted")

    def mark_x_skipped(self, item_id: int, reason: str) -> None:
        """Retire an unpublishable X mirror item without blocking later posts."""
        with self.connect() as db:
            db.execute(
                "UPDATE items SET x_status = 'skipped', x_attempts = 0, "
                "x_next_attempt_at = NULL, last_error = ? WHERE id = ?",
                (f"X skipped: {reason}"[:1000], item_id),
            )
            self._event(
                db,
                item_id,
                "x_skipped",
                {"reason": reason[:1000]},
            )

    def skip_pending_x_publications(self, reason: str) -> tuple[int, int]:
        """Discard stale normal mirrors while retaining immediate news.

        Deletions are deliberately excluded: they are housekeeping actions,
        while breaking and official posts remain valid enough to publish when
        the temporary X limit is lifted.
        """
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT id, x_text, draft_text, source_text, card_category "
                    "FROM items WHERE state = 'published' "
                    "AND telegram_message_id IS NOT NULL AND x_status IN "
                    "('pending', 'publish_failed', 'needs_replace', 'replace_failed')"
                ).fetchall()
            )
            skipped = preserved = 0
            for row in rows:
                item_id = int(row["id"])
                if is_immediate_news(
                    str(row["x_text"] or ""),
                    str(row["draft_text"] or ""),
                    str(row["source_text"] or ""),
                    category=str(row["card_category"] or ""),
                ):
                    preserved += 1
                    continue
                db.execute(
                    "UPDATE items SET x_status = 'skipped', x_attempts = 0, "
                    "x_next_attempt_at = NULL, last_error = ? WHERE id = ?",
                    (f"X skipped: {reason}"[:1000], item_id),
                )
                self._event(db, item_id, "x_skipped", {"reason": reason[:1000]})
                skipped += 1
        return skipped, preserved

    def restore_priority_x_mirrors_skipped_for_rate_limit(self) -> int:
        """Restore breaking/official mirrors skipped by the former broad pause."""
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT id, x_post_url, x_text, draft_text, source_text, card_category "
                    "FROM items WHERE state = 'published' AND telegram_message_id IS NOT NULL "
                    "AND x_status = 'skipped' "
                    "AND last_error LIKE 'X skipped: daily X publishing limit%'"
                ).fetchall()
            )
            restored = 0
            for row in rows:
                if not is_immediate_news(
                    str(row["x_text"] or ""),
                    str(row["draft_text"] or ""),
                    str(row["source_text"] or ""),
                    category=str(row["card_category"] or ""),
                ):
                    continue
                item_id = int(row["id"])
                status = "needs_replace" if row["x_post_url"] else "pending"
                db.execute(
                    "UPDATE items SET x_status = ?, x_attempts = 0, "
                    "x_next_attempt_at = NULL, last_error = NULL "
                    "WHERE id = ?",
                    (status, item_id),
                )
                self._event(db, item_id, "x_priority_restored", {"status": status})
                restored += 1
        return restored

    def skip_pending_x_backlog(self, reason: str) -> int:
        """Retire old X mirrors so they can never be published out of sync."""
        pending = ("pending", "publish_failed", "needs_replace", "replace_failed")
        placeholders = ",".join("?" for _ in pending)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id FROM items WHERE x_status IN ({placeholders})", pending
            ).fetchall()
            ids = tuple(int(row["id"]) for row in rows)
            if not ids:
                return 0
            db.execute(
                f"UPDATE items SET x_status = 'skipped', x_attempts = 0, "
                f"x_next_attempt_at = NULL, last_error = ? "
                f"WHERE x_status IN ({placeholders})",
                (f"X skipped: {reason}"[:1000], *pending),
            )
            for item_id in ids:
                self._event(db, item_id, "x_skipped", {"reason": reason[:1000]})
            return len(ids)

    def mark_x_failed(self, item_id: int, action: str, error: str) -> None:
        states = {
            "publish": "publish_failed",
            "replace": "replace_failed",
            "delete": "delete_failed",
        }
        if action not in states:
            raise ValueError(f"Unknown X mirror action: {action}")
        row = self.get_item(item_id)
        attempts = int(row["x_attempts"] or 0)
        delay = min(900, 60 * (2 ** min(4, attempts)))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET x_status = ?, x_attempts = x_attempts + 1, "
                "x_next_attempt_at = ?, last_error = ? WHERE id = ?",
                (
                    states[action],
                    _iso(utc_now() + timedelta(seconds=delay)),
                    f"X: {error}"[:1000],
                    item_id,
                ),
            )
            self._event(
                db,
                item_id,
                "x_publish_failed",
                {"action": action, "error": error[:1000]},
            )

    def queue_vk_mirror(self, item_id: int, text: str) -> None:
        clean = text.strip()
        if not clean:
            raise ValueError("VK mirror text cannot be empty")
        with self.connect() as db:
            row = db.execute(
                "SELECT vk_post_id FROM items WHERE id = ? AND state IN "
                "('scheduled', 'visual_pending', 'visual_processing', 'published') "
                "AND telegram_message_id IS NOT NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Published item not found: {item_id}")
            status = "needs_replace" if row["vk_post_id"] else "pending"
            db.execute(
                "UPDATE items SET vk_text = ?, vk_status = ?, vk_attempts = 0, "
                "vk_next_attempt_at = ?, vk_last_error = NULL WHERE id = ?",
                (clean, status, _iso(utc_now()), item_id),
            )
            self._event(db, item_id, "vk_mirror_queued", {"status": status})

    def vk_mirror_queue(
        self, now: datetime | None = None, *, limit: int = 6
    ) -> list[sqlite3.Row]:
        current = now or utc_now()
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE (((state IN "
                    "('scheduled', 'visual_pending', 'visual_processing', 'published') "
                    "AND telegram_message_id IS NOT NULL AND vk_status IN "
                    "('pending', 'publish_failed', 'needs_replace', 'replace_failed')) "
                    "OR vk_status IN ('needs_delete', 'delete_failed')) "
                    "AND vk_attempts < 5 AND (vk_next_attempt_at IS NULL "
                    "OR vk_next_attempt_at <= ?)) ORDER BY CASE WHEN vk_status IN "
                    "('needs_delete', 'delete_failed') THEN 0 ELSE 1 END, id LIMIT ?",
                    (_iso(current), limit),
                ).fetchall()
            )

    def mark_vk_published(
        self, item_id: int, post_id: int, url: str, text: str
    ) -> None:
        if post_id <= 0 or not re.fullmatch(r"https://vk\.ru/wall-\d+_\d+", url):
            raise ValueError("VK_DELIVERY_UNVERIFIED: exact VK wall mapping required")
        with self.connect() as db:
            db.execute(
                "UPDATE items SET vk_post_id = ?, vk_post_url = ?, vk_text = ?, "
                "vk_status = 'published', vk_attempts = 0, vk_next_attempt_at = NULL, "
                "vk_published_at = ?, vk_last_error = NULL WHERE id = ?",
                (post_id, url, text, _iso(utc_now()), item_id),
            )
            self._event(db, item_id, "vk_published", {"post_id": post_id, "url": url})

    def mark_vk_replaced(self, item_id: int, text: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET vk_text = ?, vk_status = 'published', "
                "vk_attempts = 0, vk_next_attempt_at = NULL, vk_last_error = NULL "
                "WHERE id = ? AND vk_post_id IS NOT NULL",
                (text, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("VK_REPLACE_MAPPING_MISSING")
            self._event(db, item_id, "vk_replaced")

    def mark_vk_deleted(self, item_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET vk_status = 'deleted', vk_attempts = 0, "
                "vk_next_attempt_at = NULL, vk_last_error = NULL WHERE id = ?",
                (item_id,),
            )
            self._event(db, item_id, "vk_deleted")

    def mark_vk_delivery_unknown(self, item_id: int, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE items SET vk_status = 'delivery_unknown', "
                "vk_attempts = vk_attempts + 1, vk_next_attempt_at = NULL, "
                "vk_last_error = ? WHERE id = ?",
                (f"VK: {error}"[:1000], item_id),
            )
            self._event(db, item_id, "vk_delivery_unknown", {"error": error[:1000]})

    def mark_vk_auth_failed(self, item_id: int, action: str, error: str) -> None:
        """Park a VK action until credentials are explicitly replaced.

        Authentication and application-type rejections are permanent for the
        current token. Retrying them on every worker cycle only burns API calls
        and obscures the actionable credential problem.
        """

        states = {
            "publish": "publish_auth_failed",
            "replace": "replace_auth_failed",
            "delete": "delete_auth_failed",
        }
        if action not in states:
            raise ValueError(f"Unknown VK mirror action: {action}")
        with self.connect() as db:
            db.execute(
                "UPDATE items SET vk_status = ?, vk_attempts = vk_attempts + 1, "
                "vk_next_attempt_at = NULL, vk_last_error = ? WHERE id = ?",
                (states[action], f"VK: {error}"[:1000], item_id),
            )
            self._event(
                db,
                item_id,
                "vk_auth_failed",
                {"action": action, "error": error[:1000]},
            )

    def requeue_vk_auth_failures(self) -> int:
        """Requeue parked VK actions after an operator installs a new token."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET vk_status = CASE vk_status "
                "WHEN 'publish_auth_failed' THEN 'pending' "
                "WHEN 'replace_auth_failed' THEN 'needs_replace' "
                "WHEN 'delete_auth_failed' THEN 'needs_delete' END, "
                "vk_attempts = 0, vk_next_attempt_at = ?, vk_last_error = NULL "
                "WHERE vk_status IN ('publish_auth_failed', "
                "'replace_auth_failed', 'delete_auth_failed')",
                (_iso(utc_now()),),
            )
            db.execute(
                "DELETE FROM runtime_state WHERE key = ?",
                (self.VK_AUTH_BLOCK_KEY,),
            )
            return int(cursor.rowcount)

    def mark_vk_failed(self, item_id: int, action: str, error: str) -> None:
        states = {
            "publish": "publish_failed",
            "replace": "replace_failed",
            "delete": "delete_failed",
        }
        if action not in states:
            raise ValueError(f"Unknown VK mirror action: {action}")
        row = self.get_item(item_id)
        attempts = int(row["vk_attempts"] or 0)
        delay = min(900, 30 * (2 ** min(5, attempts)))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET vk_status = ?, vk_attempts = vk_attempts + 1, "
                "vk_next_attempt_at = ?, vk_last_error = ? WHERE id = ?",
                (
                    states[action],
                    _iso(utc_now() + timedelta(seconds=delay)),
                    f"VK: {error}"[:1000],
                    item_id,
                ),
            )
            self._event(
                db,
                item_id,
                "vk_action_failed",
                {"action": action, "error": error[:1000]},
            )

    def approve_published_revision(
        self,
        item_id: int,
        draft_text: str,
    ) -> None:
        """Queue an explicitly approved replacement for an existing Telegram post."""

        text = draft_text.strip()
        if not text:
            raise ValueError("Draft text cannot be empty")
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE items
                SET draft_text = ?, image_path = NULL, card_category = NULL,
                    card_headline = NULL, card_secondary = NULL, state = 'approved_update',
                    reviewed_at = ?, last_error = NULL
                WHERE id = ? AND telegram_message_id IS NOT NULL
                    AND state IN ('published', 'ready_update_review', 'approved_update')
                """,
                (
                    text,
                    _iso(utc_now()),
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Published item cannot be revised: {item_id}")
            self._event(
                db,
                item_id,
                "published_revision_approved",
                {},
            )

    def mark_source_delete_done(self, item_id: int) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT last_error FROM items WHERE id = ? AND state = 'needs_delete'",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Item is not awaiting source deletion: {item_id}")
            quality_delete = str(row["last_error"] or "").startswith("quality_delete:")
            next_state = "cancelled_editor_deleted" if quality_delete else "deleted_source"
            cursor = db.execute(
                "UPDATE items SET state = ?, last_error = NULL "
                "WHERE id = ? AND state = 'needs_delete'",
                (next_state, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item is not awaiting source deletion: {item_id}")
            self._event(
                db,
                item_id,
                "published_message_deleted",
                {"reason": "quality" if quality_delete else "source_deleted"},
            )
            db.execute(
                "UPDATE items SET x_status = CASE WHEN x_post_url IS NULL "
                "THEN 'deleted' ELSE 'needs_delete' END, x_attempts = 0, "
                "x_next_attempt_at = ? WHERE id = ?",
                (_iso(utc_now()), item_id),
            )
            db.execute(
                "UPDATE items SET vk_status = CASE WHEN vk_post_id IS NULL "
                "THEN 'deleted' ELSE 'needs_delete' END, vk_attempts = 0, "
                "vk_next_attempt_at = ? WHERE id = ?",
                (_iso(utc_now()), item_id),
            )

    def mark_quality_delete(self, item_id: int, reason: str) -> None:
        """Remove an already published post that violates visual quality rules."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET state = 'needs_delete', source_deleted = 1, "
                "scheduled_for = NULL, last_error = ? "
                "WHERE id = ? AND state = 'published' AND telegram_message_id IS NOT NULL",
                (f"quality_delete: {reason[:850]}", item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Published item cannot be removed for quality: {item_id}")
            self._event(db, item_id, "quality_delete_requested", {"reason": reason[:850]})

    def approved_due(
        self,
        now: datetime,
        *,
        cutoff: datetime | None = None,
        freshness_minutes: int | None = None,
    ) -> list[sqlite3.Row]:
        with self.connect() as db:
            conditions = ["state IN ('approved', 'approved_update')"]
            parameters: list[str] = []
            if cutoff is not None:
                conditions.append(
                    "COALESCE(NULLIF(source_published_at, ''), discovered_at) > ?"
                )
                parameters.append(_iso(cutoff))
            if freshness_minutes is not None:
                conditions.append(
                    "COALESCE(NULLIF(source_published_at, ''), discovered_at) > ?"
                )
                parameters.append(_iso(now - timedelta(minutes=freshness_minutes)))
            return list(
                db.execute(
                    "SELECT * FROM items WHERE " + " AND ".join(conditions)
                    + " ORDER BY " + _EDITORIAL_PRIORITY_SQL
                    + ", COALESCE(NULLIF(source_published_at, ''), discovered_at) DESC, id",
                    parameters,
                ).fetchall()
            )

    def published_count_since(self, since: datetime) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS value FROM events "
                "WHERE event_type = 'published' AND created_at >= ?",
                (_iso(since),),
            ).fetchone()
            return int(row["value"] if row else 0)

    def published_items(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE state = 'published' "
                    "AND telegram_message_id IS NOT NULL ORDER BY telegram_message_id"
                ).fetchall()
            )

    def mark_deleted_by_editor(self, item_id: int) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT telegram_message_id FROM items "
                "WHERE id = ? AND state = 'published'",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Published item not found: {item_id}")
            message_id = int(row["telegram_message_id"])
            db.execute(
                "UPDATE items SET state = 'cancelled_editor_deleted', "
                "scheduled_for = NULL, "
                "x_status = CASE WHEN x_post_url IS NULL THEN 'deleted' "
                "ELSE 'needs_delete' END, "
                "vk_status = CASE WHEN vk_post_id IS NULL THEN 'deleted' "
                "ELSE 'needs_delete' END, vk_attempts = 0, vk_next_attempt_at = ?, "
                "dzen_delete_status = CASE "
                "WHEN dzen_mapping_status = 'verified' AND dzen_article_url IS NOT NULL "
                "THEN 'needs_delete' ELSE 'mapping_required' END, "
                "last_error = 'Пост удалён владельцем: повторная публикация запрещена' "
                "WHERE id = ?",
                (_iso(utc_now()), item_id),
            )
            self._event(
                db,
                item_id,
                "cancelled_by_editor_deletion",
                {"message_id": message_id, "terminal": True},
            )

    def dzen_digest_source_rows(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> list[sqlite3.Row]:
        """Return immutable Telegram publication snapshots for one closed day."""

        if window_start_utc.tzinfo is None or window_end_utc.tzinfo is None:
            raise ValueError("DZEN_DIGEST_WINDOW_MUST_BE_TIMEZONE_AWARE")
        if window_end_utc <= window_start_utc:
            raise ValueError("DZEN_DIGEST_WINDOW_INVALID")
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT id AS item_id, telegram_message_id, published_text, "
                    "editorial_category, editorial_importance, "
                    "telegram_published_at AS published_at "
                    "FROM items WHERE state = 'published' AND source_deleted = 0 "
                    "AND telegram_message_id IS NOT NULL "
                    "AND published_text IS NOT NULL AND TRIM(published_text) <> '' "
                    "AND telegram_published_at >= ? AND telegram_published_at < ? "
                    "ORDER BY telegram_published_at, id",
                    (_iso(window_start_utc), _iso(window_end_utc)),
                ).fetchall()
            )

    def get_dzen_daily_digest(self, digest_date: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM dzen_daily_digests WHERE digest_date = ?",
                (digest_date,),
            ).fetchone()

    def dzen_daily_digest_items(self, digest_id: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM dzen_daily_digest_items WHERE digest_id = ? "
                    "ORDER BY ordinal",
                    (digest_id,),
                ).fetchall()
            )

    def create_dzen_daily_digest(
        self,
        *,
        digest_date: str,
        timezone_name: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
        source_set_hash: str,
        story_count: int,
        title: str,
        body: str,
        body_hash: str,
        source_items: Sequence[tuple[int, int, str, str]],
    ) -> sqlite3.Row:
        """Freeze one digest and all source texts in a single transaction.

        Existing dates are immutable.  A changed source set after the freeze is
        surfaced as a conflict instead of silently changing an article that may
        already have crossed Dzen's submit boundary.
        """

        if not source_items:
            raise ValueError("DZEN_DIGEST_SOURCE_ITEMS_EMPTY")
        if len({item[0] for item in source_items}) != len(source_items):
            raise ValueError("DZEN_DIGEST_SOURCE_ITEMS_DUPLICATE")
        now = _iso(utc_now())
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM dzen_daily_digests WHERE digest_date = ?",
                (digest_date,),
            ).fetchone()
            if existing is not None:
                immutable = (
                    str(existing["source_set_hash"]) == source_set_hash
                    and str(existing["title"]) == title
                    and str(existing["body_hash"]) == body_hash
                )
                if not immutable:
                    raise ValueError("DZEN_DIGEST_IMMUTABLE_CONFLICT")
                return existing
            cursor = db.execute(
                "INSERT INTO dzen_daily_digests("
                "digest_date,timezone_name,window_start_utc,window_end_utc,"
                "source_set_hash,source_count,story_count,title,body,body_hash,"
                "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,"
                "'ready',?,?)",
                (
                    digest_date,
                    timezone_name,
                    _iso(window_start_utc),
                    _iso(window_end_utc),
                    source_set_hash,
                    len(source_items),
                    int(story_count),
                    title,
                    body,
                    body_hash,
                    now,
                    now,
                ),
            )
            digest_id = int(cursor.lastrowid)
            db.executemany(
                "INSERT INTO dzen_daily_digest_items("
                "digest_id,item_id,telegram_message_id,text_snapshot,text_hash,ordinal"
                ") VALUES (?,?,?,?,?,?)",
                (
                    (digest_id, item_id, message_id, text, text_hash, ordinal)
                    for ordinal, (item_id, message_id, text, text_hash) in enumerate(
                        source_items, start=1
                    )
                ),
            )
            row = db.execute(
                "SELECT * FROM dzen_daily_digests WHERE id = ?", (digest_id,)
            ).fetchone()
            assert row is not None
            return row

    def claim_dzen_daily_digest(
        self, digest_id: int, *, now: datetime | None = None
    ) -> bool:
        current = now or utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE dzen_daily_digests SET state = 'publishing', "
                "attempts = attempts + 1, next_attempt_at = NULL, "
                "last_error = NULL, updated_at = ? WHERE id = ? "
                "AND state IN ('ready','failed_retryable') "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
                (_iso(current), digest_id, _iso(current)),
            )
            return cursor.rowcount == 1

    def mark_dzen_daily_digest_published(
        self,
        digest_id: int,
        article_url: str,
        article_id: str,
        *,
        published_at: datetime | None = None,
    ) -> None:
        current = published_at or utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE dzen_daily_digests SET state = 'published', "
                "dzen_article_url = ?, dzen_article_id = ?, published_at = ?, "
                "next_attempt_at = NULL, last_error = NULL, updated_at = ? "
                "WHERE id = ? AND state IN ('publishing','publish_unknown')",
                (
                    article_url,
                    article_id,
                    _iso(current),
                    _iso(current),
                    digest_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("DZEN_DIGEST_PUBLISH_STATE_INVALID")

    def mark_dzen_daily_digest_unknown(self, digest_id: int, error: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE dzen_daily_digests SET state = 'publish_unknown', "
                "next_attempt_at = NULL, last_error = ?, updated_at = ? "
                "WHERE id = ? AND state = 'publishing'",
                (error[:1000], _iso(utc_now()), digest_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("DZEN_DIGEST_UNKNOWN_STATE_INVALID")

    def mark_dzen_daily_digest_retryable(
        self,
        digest_id: int,
        error: str,
        *,
        retry_at: datetime,
    ) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE dzen_daily_digests SET state = 'failed_retryable', "
                "next_attempt_at = ?, last_error = ?, updated_at = ? "
                "WHERE id = ? AND state = 'publishing'",
                (_iso(retry_at), error[:1000], _iso(utc_now()), digest_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("DZEN_DIGEST_RETRY_STATE_INVALID")

    def dzen_reconciliation_queue(
        self, now: datetime | None = None, *, limit: int = 3
    ) -> list[sqlite3.Row]:
        current = now or utc_now()
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE telegram_message_id IS NOT NULL "
                    "AND dzen_mapping_status IN ('pending', 'not_found') "
                    "AND dzen_attempts < 5 "
                    "AND (dzen_next_attempt_at IS NULL OR dzen_next_attempt_at <= ?) "
                    "ORDER BY telegram_message_id LIMIT ?",
                    (_iso(current), limit),
                ).fetchall()
            )

    def dzen_delete_queue(
        self, now: datetime | None = None, *, limit: int = 3
    ) -> list[sqlite3.Row]:
        current = now or utc_now()
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE state = 'cancelled_editor_deleted' "
                    "AND dzen_mapping_status = 'verified' "
                    "AND dzen_article_url IS NOT NULL AND dzen_article_id IS NOT NULL "
                    "AND dzen_delete_status IN ('needs_delete', 'delete_failed') "
                    "AND dzen_attempts < 5 "
                    "AND (dzen_next_attempt_at IS NULL OR dzen_next_attempt_at <= ?) "
                    "ORDER BY telegram_message_id LIMIT ?",
                    (_iso(current), limit),
                ).fetchall()
            )

    def mark_dzen_mapped(self, item_id: int, url: str, article_id: str) -> None:
        clean_url = url.split("?", 1)[0]
        match = re.fullmatch(r"https://dzen\.ru/a/([A-Za-z0-9_-]+)", clean_url)
        if not match or match.group(1) != article_id:
            raise ValueError("DZEN_ARTICLE_MAPPING_INVALID")
        with self.connect() as db:
            row = db.execute("SELECT state FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ValueError(f"Item not found: {item_id}")
            delete_status = (
                "needs_delete"
                if str(row["state"]) == "cancelled_editor_deleted"
                else None
            )
            like_status = (
                "not_applicable"
                if str(row["state"]) == "cancelled_editor_deleted"
                else "pending"
            )
            db.execute(
                "UPDATE items SET dzen_article_url = ?, dzen_article_id = ?, "
                "dzen_mapping_status = 'verified', dzen_delete_status = ?, "
                "dzen_attempts = 0, dzen_next_attempt_at = NULL, dzen_last_error = NULL, "
                "dzen_like_status = ?, dzen_like_attempts = 0, "
                "dzen_like_next_attempt_at = ? "
                "WHERE id = ?",
                (
                    clean_url,
                    article_id,
                    delete_status,
                    like_status,
                    _iso(utc_now()) if like_status == "pending" else None,
                    item_id,
                ),
            )
            self._event(db, item_id, "dzen_mapping_verified", {"url": clean_url})

    def dzen_like_queue(
        self, now: datetime | None = None, *, limit: int = 3
    ) -> list[sqlite3.Row]:
        current = now or utc_now()
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE state = 'published' "
                    "AND dzen_mapping_status = 'verified' "
                    "AND dzen_article_url IS NOT NULL AND dzen_article_id IS NOT NULL "
                    "AND dzen_like_status IN ('pending', 'like_failed') "
                    "AND dzen_like_attempts < 5 "
                    "AND (dzen_like_next_attempt_at IS NULL OR dzen_like_next_attempt_at <= ?) "
                    "ORDER BY telegram_message_id LIMIT ?",
                    (_iso(current), limit),
                ).fetchall()
            )

    def mark_dzen_liked(self, item_id: int) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET dzen_like_status = 'liked', "
                "dzen_like_attempts = 0, dzen_like_next_attempt_at = NULL "
                "WHERE id = ? AND state = 'published' "
                "AND dzen_mapping_status = 'verified' "
                "AND dzen_article_url IS NOT NULL",
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("DZEN_LIKE_NOT_ELIGIBLE")
            self._event(db, item_id, "dzen_liked")

    def mark_dzen_like_failed(self, item_id: int, reason: str) -> None:
        row = self.get_item(item_id)
        attempts = int(row["dzen_like_attempts"] or 0)
        delay = min(900, 30 * (2 ** min(5, attempts)))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET dzen_like_status = 'like_failed', "
                "dzen_like_attempts = dzen_like_attempts + 1, "
                "dzen_like_next_attempt_at = ? WHERE id = ?",
                (_iso(utc_now() + timedelta(seconds=delay)), item_id),
            )
            self._event(
                db,
                item_id,
                "dzen_like_failed",
                {"reason": reason[:1000]},
            )

    def mark_dzen_mapping_result(self, item_id: int, status: str, reason: str) -> None:
        if status not in {"not_found", "ambiguous", "auth_required"}:
            raise ValueError(f"Unknown Dzen mapping status: {status}")
        delay = min(900, 60 * (2 ** min(4, self.get_item(item_id)["dzen_attempts"])))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET dzen_mapping_status = ?, "
                "dzen_attempts = dzen_attempts + 1, dzen_next_attempt_at = ?, "
                "dzen_last_error = ? WHERE id = ?",
                (status, _iso(utc_now() + timedelta(seconds=delay)), reason[:1000], item_id),
            )
            self._event(db, item_id, "dzen_mapping_failed", {"status": status})

    def mark_dzen_delete_auth_required(self, item_id: int) -> None:
        row = self.get_item(item_id)
        delay = min(900, 60 * (2 ** min(4, int(row["dzen_attempts"] or 0))))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET dzen_delete_status = 'auth_required', "
                "dzen_attempts = dzen_attempts + 1, dzen_next_attempt_at = ?, "
                "dzen_last_error = 'DZEN_AUTH_REQUIRED' WHERE id = ?",
                (_iso(utc_now() + timedelta(seconds=delay)), item_id),
            )
            self._event(db, item_id, "dzen_delete_auth_required")

    def mark_dzen_deleted(self, item_id: int, *, already_absent: bool = False) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE items SET dzen_delete_status = 'deleted', dzen_attempts = 0, "
                "dzen_next_attempt_at = NULL, dzen_last_error = NULL WHERE id = ? "
                "AND state = 'cancelled_editor_deleted' "
                "AND dzen_mapping_status = 'verified' AND dzen_article_url IS NOT NULL",
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("DZEN_DELETE_NOT_ELIGIBLE")
            self._event(
                db, item_id, "dzen_deleted",
                {"already_absent": bool(already_absent)},
            )

    def mark_dzen_delete_failed(self, item_id: int, reason: str) -> None:
        row = self.get_item(item_id)
        delay = min(900, 60 * (2 ** min(4, int(row["dzen_attempts"] or 0))))
        with self.connect() as db:
            db.execute(
                "UPDATE items SET dzen_delete_status = 'delete_failed', "
                "dzen_attempts = dzen_attempts + 1, dzen_next_attempt_at = ?, "
                "dzen_last_error = ? WHERE id = ?",
                (_iso(utc_now() + timedelta(seconds=delay)), reason[:1000], item_id),
            )
            self._event(db, item_id, "dzen_delete_failed", {"reason": reason[:1000]})

    def record_dzen_manually_deleted(
        self, item_id: int, url: str, article_id: str
    ) -> None:
        self.mark_dzen_mapped(item_id, url, article_id)
        with self.connect() as db:
            db.execute(
                "UPDATE items SET dzen_delete_status = 'manually_deleted_by_ceo', "
                "dzen_attempts = 0, dzen_last_error = NULL WHERE id = ? "
                "AND state = 'cancelled_editor_deleted'",
                (item_id,),
            )
            self._event(db, item_id, "dzen_manually_deleted_by_ceo", {"url": url})

    def reset_dzen_auth_required(self) -> int:
        with self.connect() as db:
            mapping = db.execute(
                "UPDATE items SET dzen_mapping_status = 'pending', dzen_attempts = 0, "
                "dzen_next_attempt_at = ?, dzen_last_error = NULL "
                "WHERE dzen_mapping_status = 'auth_required'",
                (_iso(utc_now()),),
            )
            deletion = db.execute(
                "UPDATE items SET dzen_delete_status = 'needs_delete', dzen_attempts = 0, "
                "dzen_next_attempt_at = ?, dzen_last_error = NULL "
                "WHERE dzen_delete_status = 'auth_required'",
                (_iso(utc_now()),),
            )
            return int(mapping.rowcount) + int(deletion.rowcount)

    def last_published_at(self) -> datetime | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(created_at) AS value FROM events "
                "WHERE event_type = 'published'"
            ).fetchone()
            if not row or not row["value"]:
                return None
            return datetime.fromisoformat(str(row["value"]))

    def review_queue(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM items WHERE state IN "
                    "('scheduled', 'needs_update', 'ready_for_review', "
                    "'ready_update_review', 'approved', 'approved_update') "
                    "ORDER BY COALESCE(scheduled_for, source_published_at), id"
                ).fetchall()
            )

    def reset_for_editing(self, item_id: int) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE items
                SET state = 'scheduled', draft_text = NULL, image_path = NULL,
                    card_category = NULL, card_headline = NULL,
                    card_secondary = NULL, reviewed_at = NULL, last_error = NULL
                WHERE id = ? AND state IN
                    ('scheduled', 'needs_update', 'ready_for_review',
                     'ready_update_review', 'approved', 'approved_update', 'rejected_manual',
                     'needs_editor_review', 'needs_headline_review', 'rejected_quality')
                """,
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot be reset for editing: {item_id}")
            self._event(db, item_id, "reset_for_editing")

    def replace_draft(self, item_id: int, draft_text: str) -> None:
        value = draft_text.strip()
        if not value:
            raise ValueError("Draft text cannot be empty")
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE items
                SET state = 'scheduled', draft_text = ?, image_path = NULL,
                    card_category = NULL, card_headline = NULL,
                    card_secondary = NULL, reviewed_at = NULL, last_error = NULL
                WHERE id = ? AND state IN
                    ('scheduled', 'needs_update', 'ready_for_review',
                     'ready_update_review', 'approved', 'approved_update', 'rejected_manual',
                      'needs_editor_review', 'needs_headline_review', 'rejected_quality')
                """,
                (value, item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Item cannot be edited: {item_id}")
            self._event(db, item_id, "draft_replaced")
