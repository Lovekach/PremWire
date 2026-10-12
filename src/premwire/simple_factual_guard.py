from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .russian_editorial import club_entries
from .editorial_safety import relation_violations


@dataclass(frozen=True, slots=True)
class SimpleFactViolation:
    code: str
    detail: str


_SCALAR_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)?(?!\d)")
_PAIR_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[:\-–—]\s*(\d{1,4})(?!\d)")
_STAT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*\+\s*(\d{1,3})(?!\d)")
_GOAL_AUTHOR_RE = re.compile(r"\b(?:автор\w*\s+гол\w*|голеадор\w*)\b", re.I)
_LAST_16_RE = re.compile(
    r"(?<!\w)(?:last\s*[- ]?16|round\s+of\s+16|1\s*/\s*8(?:\s+финал\w*)?)(?!\w)",
    re.I,
)
_NUMBER_WORDS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), value)
    for pattern, value in (
        (r"\b(?:zero|ноль)\b", "0"),
        (r"\b(?:one|один|одна|одно|одну)\b", "1"),
        (r"\b(?:two|два|две)\b", "2"),
        (r"\b(?:three|три)\b", "3"),
        (r"\b(?:four|четыре)\b", "4"),
        (r"\b(?:five|пять)\b", "5"),
        (r"\b(?:six|шесть)\b", "6"),
        (r"\b(?:seven|семь)\b", "7"),
        (r"\b(?:eight|восемь)\b", "8"),
        (r"\b(?:nine|девять)\b", "9"),
        (r"\b(?:ten|десять)\b", "10"),
        (r"\b(?:eleven|одиннадцать)\b", "11"),
        (r"\b(?:twelve|двенадцать)\b", "12"),
    )
)
_CURRENCY = {
    "€": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "евро": "EUR",
    "£": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "фунт": "GBP",
    "фунта": "GBP",
    "фунтов": "GBP",
    "$": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "доллар": "USD",
    "доллара": "USD",
    "долларов": "USD",
}
_MAGNITUDE = {
    "k": "K",
    "thousand": "K",
    "тыс": "K",
    "тысяч": "K",
    "m": "M",
    "million": "M",
    "millions": "M",
    "млн": "M",
    "bn": "B",
    "billion": "B",
    "billions": "B",
    "млрд": "B",
}
_TRANSFER_CONTEXT_RE = re.compile(
    r"\b(?:transfer|sign(?:ing)?|join|move|loan|deal|bid|offer|personal\s+terms|"
    r"трансфер|переход|перейд|переш|подпис|аренд|сделк|предлож|личн\w*\s+услов)\w*\b",
    re.I,
)
_SOURCE_STATUS: tuple[tuple[int, str, re.Pattern[str]], ...] = (
    (6, "OFFICIAL", re.compile(r"\b(?:official(?:ly)?|signs|joins|have\s+signed|has\s+signed|has\s+joined|have\s+joined|completed\s+(?:the\s+)?(?:move|transfer)|официальн\w*|подписал[аи]?|переш[её]л|стал[аи]?\s+игрок|завершил[аи]?\s+трансфер)\b", re.I)),
    (4, "AGREEMENT", re.compile(r"\b(?:agreed|agreement|deal\s+in\s+place|here\s+we\s+go|договорил\w*|согласова\w*|соглашени\w*)\b", re.I)),
    (3, "OFFER", re.compile(r"\b(?:bid|offer|proposal|предложени\w*|заявк\w*)\b", re.I)),
    (2, "TALKS", re.compile(r"\b(?:talks?|negotiat\w*|discussions?|contact|переговор\w*|обсуждени\w*)\b", re.I)),
    (1, "EXPECTED", re.compile(r"\b(?:expected|set\s+to|close\s+to|ready\s+to|likely|близ\w*|ожида\w*|должен|готов\w*)\b", re.I)),
    (0, "INTEREST", re.compile(r"\b(?:interest\w*|consider\w*|monitor\w*|keen|target\w*|интерес\w*|рассматрива\w*|следит|нацелен\w*)\b", re.I)),
)
_OUTPUT_STATUS: tuple[tuple[int, str, re.Pattern[str]], ...] = (
    (6, "OFFICIAL", re.compile(r"\b(?:официальн\w*|подписал[аи]?|переш[её]л|присоединил\w*|стал[аи]?\s+игрок|завершил[аи]?\s+(?:переход|трансфер)|оформил[аи]?\s+(?:переход|трансфер))\b", re.I)),
    (4, "AGREEMENT", re.compile(r"\b(?:договорил\w*|согласова\w*|достиг\w*\s+соглашени\w*|сделк\w*\s+(?:согласова\w*|заключен\w*))\b", re.I)),
    (3, "OFFER", re.compile(r"\b(?:предложени\w*|заявк\w*)\b", re.I)),
    (2, "TALKS", re.compile(r"\b(?:переговор\w*|обсуждени\w*|контакт\w*)\b", re.I)),
    (1, "EXPECTED", re.compile(r"\b(?:близ\w*|ожида\w*|должен|готов\w*|предстоит|подпишет|перейд[её]т)\b", re.I)),
    (0, "INTEREST", re.compile(r"\b(?:интерес\w*|рассматрива\w*|следит|нацелен\w*)\b", re.I)),
)
_BROKEN_TRANSFER_RELATION_RE = re.compile(
    r"\bклуб\s+предложил[\wа-яё]*\s+(?:игрока|футболиста)\s*[.!?]*\s*$",
    re.I,
)
_SOURCE_LIVE_RE = re.compile(
    r"\b(?:live|in[- ]game|currently|at\s+the\s+break|first\s+half|second\s+half|"
    r"прямом\s+эфире|по\s+ходу\s+матча|первые?\s+\d+\s+минут|"
    r"концовк\w*\s+матча|удержат\s+победу|отыграются|камбэк)\b",
    re.I,
)
_SOURCE_IN_GAME_EVENT_RE = re.compile(
    r"\b(?:забил\w*|пропустил\w*|открыл\w*\s+сч[её]т|"
    r"сравнял\w*\s+сч[её]т|вышел\w*\s+впер[её]д|"
    r"scored?|conceded|opened\s+the\s+scoring|equalised?|took\s+the\s+lead)\b",
    re.I,
)
_SOURCE_PREMATCH_RE = re.compile(
    r"\b(?:starts?\s+(?:for|against)|team\s+news|kick-?off|before\s+the\s+match|"
    r"сегодня\s+(?:сыгра|встрет)|перед\s+матчем|посмотрим,?\s+как|"
    r"матч\s+начн[её]тся|выйдет\s+в\s+старте)\b",
    re.I,
)
_SOURCE_FINAL_RE = re.compile(
    r"\b(?:full[- ]?time|final\s+score|match\s+ended|beat|defeated|won|lost|drew|"
    r"финальн\w*\s+сч[её]т|матч\s+заверш\w*|обыграл\w*|победил\w*|"
    r"проиграл\w*|сыграл\w*\s+вничью|вырыва\w*\s+ничь\w*|одержал\w*\s+побед)\b",
    re.I,
)
_OUTPUT_FINAL_RE = re.compile(
    r"\b(?:обыграл\w*|победил\w*|проиграл\w*|уступил\w*|"
    r"одержал\w*(?:\s+\w+){0,2}\s+побед\w*|сыграл\w*\s+вничью|матч\s+завершил\w*)\b",
    re.I,
)
_OUTPUT_PLAYED_RE = re.compile(
    r"\b(?:вышел\w*\s+на\s+поле|сыграл\w*|пров[её]л\w*\s+матч|принял\w*\s+участие)\b",
    re.I,
)
_UNSUPPORTED_STREAK_RE = re.compile(r"\b(?:подряд|consecutive(?:ly)?)\b", re.I)
_NATIONALITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (
        ("ENGLISH", r"\b(?:английск\w*|англичан\w*|England|English)\b"),
        ("ARGENTINE", r"\b(?:аргентин\w*|Argentina\w*)\b"),
        ("BELGIAN", r"\b(?:бельги\w*|Belgian|Belgium)\b"),
        ("BRAZILIAN", r"\b(?:бразил\w*|Brazil\w*)\b"),
        ("DUTCH", r"\b(?:голланд\w*|нидерланд\w*|Dutch|Netherlands)\b"),
        ("SPANISH", r"\b(?:испан\w*|Spain|Spanish)\b"),
        ("ITALIAN", r"\b(?:италь\w*|Italy|Italian)\b"),
        ("GERMAN", r"\b(?:немец\w*|герман\w*|Germany|German)\b"),
        ("PARAGUAYAN", r"\b(?:парагва\w*|Paraguay\w*)\b"),
        ("PORTUGUESE", r"\b(?:португал\w*|Portugal|Portuguese)\b"),
        ("FRENCH", r"\b(?:француз\w*|France|French)\b"),
        ("CROATIAN", r"\b(?:хорват\w*|Croatia\w*)\b"),
        ("SERBIAN", r"\b(?:серб\w*|Serbia\w*)\b"),
        ("URUGUAYAN", r"\b(?:уругва\w*|Uruguay\w*)\b"),
        ("TURKISH", r"\b(?:турец\w*|турок\w*|Турци\w*|Turkey|Turkish)\b"),
        ("SWEDISH", r"\b(?:швед\w*|Sweden|Swedish)\b"),
        ("NORWEGIAN", r"\b(?:норвеж\w*|Norway|Norwegian)\b"),
        ("DANISH", r"\b(?:датск\w*|датчан\w*|Denmark|Danish)\b"),
    )
)
_SOURCE_ATTRIBUTION_PATTERNS = (
    re.compile(
        r"(?P<name>[А-ЯЁA-Z][А-Яа-яЁёA-Za-z'’.\-]{2,}"
        r"(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-z'’.\-]{2,}){1,3})\s*:\s*",
    ),
    re.compile(
        r"(?P<name>[А-ЯЁA-Z][А-Яа-яЁёA-Za-z'’.\-]{2,}"
        r"(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-z'’.\-]{2,}){0,3})\s+"
        r"(?:сообща\w*|заявил\w*|reports?|reported|says?|said)\b",
        re.I,
    ),
)
_GENERIC_ATTRIBUTION_NAMES = frozenset(
    {"официально", "источник", "sources", "source", "breaking", "exclusive"}
)
_OUTPUT_CLUB_ATTRIBUTION_RE = re.compile(
    r"\b(?:в\s+клубе\s+(?:уточнил|сообщил|подтвердил)\w*|"
    r"клуб\s+(?:уточнил|сообщил|подтвердил)\w*)\b",
    re.I,
)
_SOURCE_CLUB_ATTRIBUTION_RE = re.compile(
    r"\b(?:в\s+клубе|клуб\s+(?:уточнил|сообщил|подтвердил)\w*)\b",
    re.I,
)
_CAUSAL_CLAUSE_RE = re.compile(
    r"\b(?:поняв,?\s+что|поэтому)\s+(?P<clause>[^.!?]{8,180})",
    re.I,
)
_CAUSAL_STOPWORDS = frozenset(
    {"была", "были", "было", "будет", "быть", "его", "ему", "что", "этого", "этой", "этот", "может", "могла", "могли", "могло", "свой", "свою", "свои", "после", "перед", "время"}
)
_TITLECASE_PHRASE_RE = re.compile(
    r"(?<![\w])(?P<name>[А-ЯЁ][а-яё'’]{2,}(?:-[А-ЯЁ][а-яё'’]{2,})?"
    r"(?:\s+[А-ЯЁ][а-яё'’]{2,}(?:-[А-ЯЁ][а-яё'’]{2,})?){1,2})(?![\w])"
)
_TRANSFER_BONUS_AMOUNT_RE = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?:млн|тыс|m|k)?\s*(?:евро|фунт\w*|€|£)?\s*бонус\w*",
    re.I,
)
_FUTURE_SALE_PERCENT_RE = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*%[^.!?]{0,50}"
    r"(?:будущ\w*\s+(?:пере)?продаж|перепродаж|sell-on)",
    re.I,
)
_OUTPUT_PERCENT_AS_BONUS_RE = re.compile(
    r"(?:\bбонус\w*\b[^.!?]{0,80}?(?P<after>\d+(?:[.,]\d+)?)\s*%|"
    r"(?P<before>\d+(?:[.,]\d+)?)\s*%[^.!?]{0,80}?\bбонус\w*\b)",
    re.I,
)
_BRAND_PITCH_RE = re.compile(
    r"(?:\bконтекст\s+вместо\s+шума\b|"
    r"\bобъясняем\s+причины,?\s+последствия\s+и\s+смысл\s+событий\b|"
    r"\b(?:новости|контекст)\s+без\s+шума\b)",
    re.I,
)
_SPEAKER_NAME_RE = re.compile(
    r"(?<![\w])(?P<name>[А-ЯЁ][а-яё'’\-]{1,}"
    r"(?:\s+[А-ЯЁ][а-яё'’\-]{1,}){0,2})\s+"
    r"(?:наш[её]л\w*|объяснил\w*|заявил\w*|сказал\w*|отметил\w*|"
    r"считает\w*|уверен\w*|сообщил\w*|признал\w*|рассказал\w*)\b"
)
_AGE_QUESTION_RE = re.compile(r"[^?\n]*\b(?P<age>\d{1,2})\s+лет\b[^?\n]*\?", re.I)
_AGE_ASSERTION_RE = re.compile(
    r"\b(?:исполнил\w*|уже\s+исполнил\w*|ему|игроку)\b[^.!?]{0,50}?"
    r"(?P<age>\d{1,2})\s+лет\b|\b(?P<age_first>\d{1,2})-летн\w*\b",
    re.I,
)
_HYPOTHETICAL_PLAN_RE = re.compile(
    r"\b(?:может\s+(?:купить|подписать|приобрести|включить)|"
    r"план[^.!?]{0,40}(?:таков|состоит|предусматривает)|если\s+сделк\w*|"
    r"could\s+(?:buy|sign|include)|the\s+plan\s+is)\b",
    re.I,
)
_COMPLETED_PLAN_RE = re.compile(
    r"\b(?:купил\w*|приобр[её]л\w*|включил\w*|активировал\w*|"
    r"оформил\w*|подписал\w*|bought|included|activated|signed)\b",
    re.I,
)
_SOURCE_COMPLETED_PLAN_RE = re.compile(
    r"\b(?:уже\s+)?(?:купил\w*|приобр[её]л\w*|включил\w*|активировал\w*|"
    r"оформил\w*|подписал\w*|bought|included|activated|signed)\b",
    re.I,
)
_FEATURED_PERSON_RE = re.compile(
    r"\b(?:магия|гол|дриблинг|сейв|передача)\s+"
    r"(?P<name>[А-ЯЁ][а-яё'’\-]{2,}(?:\s+[А-ЯЁ][а-яё'’\-]{2,}){1,2})",
    re.I,
)
_POSITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (
        ("GOALKEEPER", r"\b(?:goalkeeper|keeper|вратар\w*|голкипер\w*)\b"),
        ("DEFENDER", r"\b(?:defender|centre-back|center-back|full-back|защитник\w*)\b"),
        ("MIDFIELDER", r"\b(?:midfielder|полузащитник\w*)\b"),
        ("WINGER", r"\b(?:winger|вингер\w*)\b"),
        ("FORWARD", r"\b(?:striker|forward|нападающ\w*|форвард\w*)\b"),
        ("COACH", r"\b(?:coach|manager|тренер\w*)\b"),
    )
)
_BUY_OPTION_MANDATORY_RE = re.compile(
    r"(?:опци\w*\s+выкуп\w*|option\s+to\s+buy)[^.!?]{0,120}"
    r"(?:может\s+стать\s+обязательн|станет\s+обязательн|become\s+mandatory|obligation)",
    re.I,
)
_OUTPUT_MANDATORY_RE = re.compile(r"\b(?:обязательн\w*|mandatory|obligation)\b", re.I)
_PERSONAL_TERMS_RE = re.compile(r"\b(?:personal\s+terms|личн\w*\s+услови\w*)\b", re.I)
_OUTPUT_MEMBERSHIP_RE = re.compile(
    r"\b(?:будет|продолжит)\s+(?:играть|выступать)\s+за\b", re.I
)
_SOURCE_SIGNATURE_RE = re.compile(r"(?:🦁\s*Albion|📝\s*Main\s+Transfers)\b", re.I)
_OUTPUT_SIGNATURE_ENTITY_RE = re.compile(r"\b(?:Альбион|Main\s+Transfers)\b", re.I)
_RULE_MINUTE_AND_STOPPAGE_RE = re.compile(
    r"(?:как\s+минимум|не\s+менее)\s+1\s+минут\w*[^.!?]{0,80}"
    r"следующ\w*\s+остановк\w*",
    re.I,
)
_OUTPUT_NEXT_STOPPAGE_RE = re.compile(r"следующ\w*\s+остановк\w*", re.I)
_OUTPUT_ONE_MINUTE_RE = re.compile(r"(?:как\s+минимум|не\s+менее)?\s*1\s+минут\w*", re.I)
_RAW_SOURCE_MARKER_RE = re.compile(
    r"(?:https?://|www\.|(?<!\w)@[A-Za-z0-9_]+|(?<!\w)#[A-Za-zА-Яа-яЁё0-9_]+|"
    r"🦁\s*Albion|📝\s*Main\s+Transfers|"
    r"\b(?:подпишись|подписывайся|смотри\s+все\s+матчи)\b)",
    re.I,
)


def _compact_surface(value: str) -> str:
    return "".join(re.findall(r"[a-zа-яё0-9]+", value.casefold()))


def _speaker_token_sets(text: str) -> tuple[frozenset[str], ...]:
    names: list[frozenset[str]] = []
    for match in _SPEAKER_NAME_RE.finditer(text):
        tokens = frozenset(
            token.casefold()
            for token in re.findall(r"[А-Яа-яЁё]{3,}", match.group("name"))
        )
        if tokens:
            names.append(tokens)
    return tuple(names)


def _player_positions(text: str) -> frozenset[str]:
    return frozenset(
        name for name, pattern in _POSITION_PATTERNS if pattern.search(text)
    )


def _known_club_ids(text: str) -> frozenset[str]:
    folded = text.casefold()
    handles = tuple(
        _compact_surface(value)
        for value in re.findall(r"(?<!\w)[@#]([A-Za-z0-9_]+)", text)
    )
    found: set[str] = set()
    for club in club_entries():
        surfaces = (club.display, *club.aliases, *club.forms.values())
        for surface in surfaces:
            value = surface.casefold().strip()
            if not value:
                continue
            if re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", folded):
                found.add(club.entity_id)
                break
            compact_alias = _compact_surface(value)
            if len(compact_alias) >= 3 and any(
                handle == compact_alias or handle.startswith(compact_alias + "official")
                for handle in handles
            ):
                found.add(club.entity_id)
                break
            parts = re.findall(r"[a-zа-яё0-9]+", value)
            if len(parts) >= 2 and all(
                len(part) >= 4 and part[:4] in folded for part in parts
            ):
                found.add(club.entity_id)
                break
    shorthand = (
        ("manchester-city", re.compile(r"(?<![\w])(?:(?i:ман\s+сити)|СИТИ)(?![\w])")),
        ("manchester-united", re.compile(r"(?<![\w])(?:мю|ман\s+юнайтед)(?![\w])", re.I)),
        ("barcelona", re.compile(r"(?<![\w])барс[ау](?![\w])", re.I)),
        ("liverpool", re.compile(r"(?<![\w])#?LFC(?![\w])", re.I)),
    )
    for entity_id, pattern in shorthand:
        if pattern.search(text):
            found.add(entity_id)
    return frozenset(found)


def _nationalities(text: str) -> frozenset[str]:
    return frozenset(name for name, pattern in _NATIONALITY_PATTERNS if pattern.search(text))


def _source_attribution_names(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for pattern in _SOURCE_ATTRIBUTION_PATTERNS:
        for match in pattern.finditer(text):
            name = re.sub(r"\s+", " ", match.group("name")).strip(" .,:;")
            if name.casefold() not in _GENERIC_ATTRIBUTION_NAMES:
                names.append(name)
    return tuple(dict.fromkeys(names))


def _same_script_causal_support(source_text: str, output_text: str) -> bool:
    if not re.search(r"[А-Яа-яЁё]", source_text):
        return True
    source_stems = {
        token.casefold()[:5]
        for token in re.findall(r"[А-Яа-яЁё]{4,}", source_text)
        if token.casefold() not in _CAUSAL_STOPWORDS
    }
    for match in _CAUSAL_CLAUSE_RE.finditer(output_text):
        tokens = [
            token.casefold()
            for token in re.findall(r"[А-Яа-яЁё]{4,}", match.group("clause"))
            if token.casefold() not in _CAUSAL_STOPWORDS
        ]
        if len(tokens) < 2:
            continue
        supported = sum(token[:5] in source_stems for token in tokens)
        if supported / len(tokens) < 0.5:
            return False
    return True


def _unsupported_expanded_person(source_text: str, output_text: str) -> str | None:
    source_words = {word.casefold() for word in re.findall(r"[А-Яа-яЁё]{3,}", source_text)}
    for match in _TITLECASE_PHRASE_RE.finditer(output_text):
        phrase = match.group("name")
        if _known_club_ids(phrase):
            continue
        words = [word.casefold() for word in re.findall(r"[А-Яа-яЁё]{3,}", phrase)]
        present = [word in source_words for word in words]
        if any(present) and not all(present):
            return phrase
    return None


def _decimal(value: str) -> str:
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation:
        return value
    return format(number.normalize(), "f")


def _nearby_claim(text: str, match: re.Match[str]) -> str:
    # Units belong immediately around this number. Do not borrow `k`/`m` or a
    # currency from the next/previous amount in strings such as
    # "€2m plus €500k" or "2 млн евро плюс 500 тыс. евро".
    before = text[max(0, match.start() - 5) : match.start()].casefold()
    after = text[match.end() : min(len(text), match.end() + 24)].casefold()
    after = re.split(r"\d", after, maxsplit=1)[0]
    context = before + " " + after
    currency = ""
    for token, normalized in _CURRENCY.items():
        if token in context:
            currency = normalized
            break
    magnitude = ""
    for token, normalized in _MAGNITUDE.items():
        if re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", after, re.I):
            magnitude = normalized
            break
    percent = "PCT" if "%" in context or re.search(r"\b(?:per\s+cent|percent|процент)\w*\b", context, re.I) else ""
    return "|".join((_decimal(match.group()), currency, magnitude, percent))


def normalized_numeric_claims(text: str) -> frozenset[str]:
    claims: set[str] = set()
    masked = text
    if _LAST_16_RE.search(masked):
        claims.add("STAGE|LAST_16")
        masked = _LAST_16_RE.sub(" ", masked)
    for match in _PAIR_RE.finditer(masked):
        claims.add(f"PAIR|{_decimal(match.group(1))}:{_decimal(match.group(2))}")
    for match in _STAT_RE.finditer(masked):
        claims.add(f"STAT|{_decimal(match.group(1))}+{_decimal(match.group(2))}")
    for match in _SCALAR_RE.finditer(masked):
        claims.add(f"NUM|{_nearby_claim(masked, match)}")
    for pattern, value in _NUMBER_WORDS:
        if pattern.search(text):
            claims.add(f"NUM|{value}|||")
    return frozenset(claims)


def _compatible_numeric_claim(output_claim: str, source_claims: frozenset[str]) -> bool:
    if output_claim in source_claims:
        return True
    if not output_claim.startswith("NUM|"):
        return False
    value, currency, magnitude, percent = output_claim[4:].split("|")
    for source in source_claims:
        if not source.startswith("NUM|"):
            continue
        source_value, source_currency, source_magnitude, source_percent = source[4:].split("|")
        if source_value != value:
            continue
        # Missing unit information is tolerated, but an explicit conflicting
        # currency, scale or percent marker is not.
        if currency and source_currency and currency != source_currency:
            continue
        if magnitude and source_magnitude and magnitude != source_magnitude:
            continue
        if percent and source_percent and percent != source_percent:
            continue
        if currency and not source_currency:
            continue
        if percent and not source_percent:
            continue
        return True
    return False


def _status(text: str, patterns: tuple[tuple[int, str, re.Pattern[str]], ...]) -> tuple[int, str] | None:
    found = [(level, name) for level, name, pattern in patterns if pattern.search(text)]
    return max(found) if found else None


def check_simple_factual_safety(source_text: str, output_text: str) -> tuple[SimpleFactViolation, ...]:
    violations: list[SimpleFactViolation] = []
    source_normalized = " ".join(re.findall(r"[a-zа-яё0-9]+", source_text.casefold()))
    output_normalized = " ".join(re.findall(r"[a-zа-яё0-9]+", output_text.casefold()))
    if (
        source_normalized
        and source_normalized == output_normalized
        and _RAW_SOURCE_MARKER_RE.search(source_text)
    ):
        violations.append(
            SimpleFactViolation(
                "SOURCE_COPY_LEAK",
                "OUTPUT совпадает с сырым SOURCE и не прошёл редакционную переработку.",
            )
        )

    if _BRAND_PITCH_RE.search(source_text):
        violations.append(
            SimpleFactViolation(
                "NON_STORY_INVENTED",
                "SOURCE является описанием/промо канала, а не отдельной новостью.",
            )
        )

    source_speakers = _speaker_token_sets(source_text)
    output_speakers = _speaker_token_sets(output_text)
    if source_speakers and output_speakers and any(
        not any(output_name & source_name for source_name in source_speakers)
        for output_name in output_speakers
    ):
        violations.append(
            SimpleFactViolation(
                "PERSON_ENTITY_REPLACED",
                "OUTPUT заменяет названного в SOURCE говорящего другим человеком.",
            )
        )

    featured = _FEATURED_PERSON_RE.search(source_text)
    if featured:
        source_name_stems = {
            token.casefold()[:5]
            for token in re.findall(r"[А-Яа-яЁё]{4,}", featured.group("name"))
        }
        output_stems = {
            token.casefold()[:5]
            for token in re.findall(r"[А-Яа-яЁё]{4,}", output_text)
        }
        if source_name_stems and not (source_name_stems & output_stems):
            violations.append(
                SimpleFactViolation(
                    "SOURCE_SUBJECT_DROPPED",
                    "OUTPUT удаляет названного в SOURCE героя эпизода.",
                )
            )

    source_positions = _player_positions(source_text)
    output_positions = _player_positions(output_text)
    if output_positions - source_positions:
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_PLAYER_POSITION",
                "OUTPUT добавляет позицию игрока, которой нет в SOURCE.",
            )
        )

    source_claims = normalized_numeric_claims(source_text)
    output_claims = normalized_numeric_claims(output_text)
    unsupported = sorted(
        claim for claim in output_claims if not _compatible_numeric_claim(claim, source_claims)
    )
    if unsupported:
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_NUMERIC_CLAIM",
                "OUTPUT содержит числа/счёт, которых нет в SOURCE: " + ", ".join(unsupported),
            )
        )

    source_stats = {claim for claim in source_claims if claim.startswith("STAT|")}
    output_stats = {claim for claim in output_claims if claim.startswith("STAT|")}
    if source_stats and not output_stats.issuperset(source_stats.intersection(output_stats)):
        # Kept for clarity; the subset relation itself is covered above.
        pass
    for stat in source_stats:
        raw = stat.removeprefix("STAT|")
        left, right = raw.split("+")
        if stat not in output_stats and re.search(
            rf"(?<!\d)(?:{re.escape(left)}|{re.escape(right)})(?!\d)", output_text
        ):
            violations.append(
                SimpleFactViolation(
                    "AMBIGUOUS_STAT_REWRITE",
                    f"SOURCE-статистика {raw} должна остаться в формате {raw} или быть опущена, а не интерпретирована.",
                )
            )
            break

    if (
        source_stats
        and _GOAL_AUTHOR_RE.search(output_text)
        and not _GOAL_AUTHOR_RE.search(source_text)
    ):
        violations.append(
            SimpleFactViolation(
                "AMBIGUOUS_STAT_ROLE",
                "Статистика вида 2+2 не доказывает роль автора голов; сохрани "
                "формат показателя без интерпретации или опусти характеристику.",
            )
        )

    if _TRANSFER_CONTEXT_RE.search(source_text):
        if _BROKEN_TRANSFER_RELATION_RE.search(output_text):
            violations.append(
                SimpleFactViolation(
                    "BROKEN_TRANSFER_RELATION",
                    "OUTPUT обрывает связь между клубом, игроком и адресатом предложения.",
                )
            )
        source_status = _status(source_text, _SOURCE_STATUS)
        output_status = _status(output_text, _OUTPUT_STATUS)
        if source_status and output_status and output_status[0] > source_status[0]:
            violations.append(
                SimpleFactViolation(
                    "TRANSFER_CERTAINTY_UPGRADED",
                    f"SOURCE={source_status[1]}, OUTPUT={output_status[1]}; используй не более уверенную формулировку.",
                )
            )

        if _PERSONAL_TERMS_RE.search(source_text) and _OUTPUT_MEMBERSHIP_RE.search(output_text):
            violations.append(
                SimpleFactViolation(
                    "UNSUPPORTED_CLUB_MEMBERSHIP",
                    "Согласование личных условий не подтверждает, что игрок уже будет или продолжит выступать за клуб.",
                )
            )

        source_bonus_amounts = {
            match.group("amount").replace(",", ".")
            for match in _TRANSFER_BONUS_AMOUNT_RE.finditer(source_text)
        }
        if source_bonus_amounts and re.search(r"\bбонус\w*\b", output_text, re.I):
            output_numbers = {
                match.group(0).replace(",", ".")
                for match in re.finditer(r"\d+(?:[.,]\d+)?", output_text)
            }
            missing = sorted(source_bonus_amounts - output_numbers)
            if missing:
                violations.append(
                    SimpleFactViolation(
                        "TRANSFER_QUALIFIER_DROPPED",
                        "OUTPUT упоминает бонусы, но теряет их точную сумму из SOURCE: "
                        + ", ".join(missing),
                    )
                )

        source_sale_percent = {
            match.group("amount").replace(",", ".")
            for match in _FUTURE_SALE_PERCENT_RE.finditer(source_text)
        }
        if source_sale_percent and re.search(r"будущ\w*\s+продаж", output_text, re.I):
            output_percent = {
                match.group(1).replace(",", ".")
                for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", output_text)
            }
            missing = sorted(source_sale_percent - output_percent)
            if missing:
                violations.append(
                    SimpleFactViolation(
                        "TRANSFER_QUALIFIER_DROPPED",
                        "OUTPUT упоминает будущую продажу, но теряет процент из SOURCE: "
                        + ", ".join(missing),
                    )
                )

        output_bonus_percent = {
            (match.group("after") or match.group("before") or "").replace(",", ".")
            for match in _OUTPUT_PERCENT_AS_BONUS_RE.finditer(output_text)
        }
        if source_sale_percent & output_bonus_percent:
            violations.append(
                SimpleFactViolation(
                    "SELL_ON_RECAST_AS_BONUS",
                    "Процент от будущей перепродажи нельзя называть бонусом.",
                )
            )

        if _BUY_OPTION_MANDATORY_RE.search(source_text) and not _OUTPUT_MANDATORY_RE.search(output_text):
            if re.search(r"\bопци\w*\s+выкуп\w*\b", output_text, re.I):
                violations.append(
                    SimpleFactViolation(
                        "TRANSFER_CONDITION_DROPPED",
                        "OUTPUT сохраняет опцию выкупа, но теряет условие, при котором она может стать обязательной.",
                    )
                )

        if re.search(r"половин\w*\s+из\s+котор\w*", source_text, re.I) and re.search(
            r"половин\w*\s+из\s+(?:этой|общей)\s+сумм\w*", output_text, re.I
        ):
            violations.append(
                SimpleFactViolation(
                    "TRANSFER_QUALIFIER_RELATION_CHANGED",
                    "OUTPUT переносит уточнение с бонусов на всю сумму сделки.",
                )
            )

    source_clubs = _known_club_ids(source_text)
    output_clubs = _known_club_ids(output_text)
    unsupported_clubs = sorted(output_clubs - source_clubs)
    if unsupported_clubs:
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_CLUB_ENTITY",
                "OUTPUT добавляет клуб, которого нет в SOURCE: " + ", ".join(unsupported_clubs),
            )
        )

    if source_clubs and not output_clubs:
        unsupported_quoted = [
            value
            for value in re.findall(r"[«\"]([^»\"\n]{2,80})[»\"]", output_text)
            if not _known_club_ids(value)
            and value.casefold() not in source_text.casefold()
        ]
        if unsupported_quoted:
            violations.append(
                SimpleFactViolation(
                    "UNSUPPORTED_CLUB_ENTITY",
                    "OUTPUT добавляет неподтверждённую именованную команду: "
                    + ", ".join(unsupported_quoted),
                )
            )

    expanded_person = _unsupported_expanded_person(source_text, output_text)
    if expanded_person:
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_PERSON_NAME_DETAIL",
                f'OUTPUT расширяет source-local имя неподтверждённой частью: "{expanded_person}".',
            )
        )

    source_nationalities = _nationalities(source_text)
    output_nationalities = _nationalities(output_text)
    if output_nationalities - source_nationalities:
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_NATIONALITY",
                "OUTPUT добавляет национальность, которой нет в SOURCE.",
            )
        )

    source_is_unfinished = (
        bool(
            _SOURCE_LIVE_RE.search(source_text)
            or _SOURCE_PREMATCH_RE.search(source_text)
            or _SOURCE_IN_GAME_EVENT_RE.search(source_text)
        )
        and not _SOURCE_FINAL_RE.search(source_text)
    )
    if source_is_unfinished and _OUTPUT_FINAL_RE.search(output_text):
        violations.append(
            SimpleFactViolation(
                "MATCH_STATUS_UPGRADED",
                "SOURCE описывает матч до финального свистка, а OUTPUT объявляет итог.",
            )
        )
    if _SOURCE_PREMATCH_RE.search(source_text) and _OUTPUT_PLAYED_RE.search(output_text):
        violations.append(
            SimpleFactViolation(
                "LINEUP_STATUS_UPGRADED",
                "SOURCE сообщает о старте/составе, а OUTPUT утверждает, что игрок уже вышел или сыграл.",
            )
        )

    source_age_questions = {
        match.group("age") for match in _AGE_QUESTION_RE.finditer(source_text)
    }
    output_asserted_ages = {
        match.group("age") or match.group("age_first")
        for match in _AGE_ASSERTION_RE.finditer(output_text)
    }
    if source_age_questions & output_asserted_ages and "?" not in output_text:
        violations.append(
            SimpleFactViolation(
                "QUESTION_TURNED_INTO_FACT",
                "SOURCE задаёт вопрос о возрасте, а OUTPUT превращает его в утверждение.",
            )
        )

    if (
        _HYPOTHETICAL_PLAN_RE.search(source_text)
        and _COMPLETED_PLAN_RE.search(output_text)
        and not _SOURCE_COMPLETED_PLAN_RE.search(source_text)
    ):
        violations.append(
            SimpleFactViolation(
                "HYPOTHETICAL_PLAN_UPGRADED",
                "Гипотетический план в SOURCE нельзя описывать как уже выполненное действие.",
            )
        )

    if _UNSUPPORTED_STREAK_RE.search(output_text) and not _UNSUPPORTED_STREAK_RE.search(source_text):
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_STREAK",
                "OUTPUT добавляет серию «подряд», которой нет в SOURCE.",
            )
        )

    if _SOURCE_SIGNATURE_RE.search(source_text) and _OUTPUT_SIGNATURE_ENTITY_RE.search(output_text):
        violations.append(
            SimpleFactViolation(
                "SOURCE_SIGNATURE_AS_ENTITY",
                "OUTPUT превратил подпись канала-источника в клуб или участника новости.",
            )
        )

    if (
        _RULE_MINUTE_AND_STOPPAGE_RE.search(source_text)
        and _OUTPUT_NEXT_STOPPAGE_RE.search(output_text)
        and not _OUTPUT_ONE_MINUTE_RE.search(output_text)
    ):
        violations.append(
            SimpleFactViolation(
                "RULE_CONDITION_DROPPED",
                "OUTPUT сохранил остановку игры, но потерял обязательное ожидание минимум одну минуту.",
            )
        )

    attribution_names = _source_attribution_names(source_text)
    if attribution_names and re.search(r"\b(?:сообща\w*|по\s+данным|reports?|reported|:)\b", source_text, re.I):
        missing_attribution = [
            name for name in attribution_names
            if not (name.casefold().split()[-1] == "referee" and re.search(r"\b(?:арбитр|судь)\w*", output_text, re.I))
            and not any(
                token.casefold() in output_text.casefold()
                for token in name.split()
                if len(token) >= 5
            )
        ]
        if missing_attribution:
            violations.append(
                SimpleFactViolation(
                    "ATTRIBUTION_DROPPED",
                    "OUTPUT превращает атрибутированное сообщение в прямой факт: "
                    + ", ".join(missing_attribution),
                )
            )

    if _OUTPUT_CLUB_ATTRIBUTION_RE.search(output_text) and not _SOURCE_CLUB_ATTRIBUTION_RE.search(source_text):
        violations.append(
            SimpleFactViolation(
                "ATTRIBUTION_ACTOR_CHANGED",
                "OUTPUT приписывает сообщение клубу, хотя SOURCE этого не утверждает.",
            )
        )

    if not _same_script_causal_support(source_text, output_text):
        violations.append(
            SimpleFactViolation(
                "UNSUPPORTED_CAUSAL_EXPLANATION",
                "OUTPUT добавляет причинное объяснение, не поддержанное русским SOURCE.",
            )
        )
    violations.extend(SimpleFactViolation(code, detail) for code, detail in relation_violations(source_text, output_text))
    return tuple(violations)


__all__ = [
    "SimpleFactViolation",
    "check_simple_factual_safety",
    "normalized_numeric_claims",
]
