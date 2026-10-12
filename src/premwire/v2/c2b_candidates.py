from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from premwire.emblem_registry import EmblemRegistry, normalize_entity_name

from .c2b_models import Candidate, CandidateBundle, CandidateType
from .c2b_source import NormalizedText


CANDIDATE_DETECTOR_VERSION = "1.0"
_CURRENCY = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "евро": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "фунт": "GBP",
    "фунта": "GBP",
    "фунтов": "GBP",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "доллар": "USD",
    "доллара": "USD",
    "долларов": "USD",
}
_MULTIPLIER = {
    "m": Decimal("1000000"),
    "mn": Decimal("1000000"),
    "million": Decimal("1000000"),
    "millions": Decimal("1000000"),
    "млн": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "bn": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "billions": Decimal("1000000000"),
    "млрд": Decimal("1000000000"),
}
_MONEY_PATTERNS = (
    re.compile(
        r"(?<![\w%])(?P<currency>€|£|\$)\s*"
        r"(?P<number>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>m|mn|b|bn|million|millions|billion|billions|млн|млрд)?"
        r"(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?P<currency>EUR|GBP|USD)\s*"
        r"(?P<number>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>m|mn|b|bn|million|millions|billion|billions|млн|млрд)?"
        r"(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w%])(?P<number>\d+(?:[.,]\d+)?)\s*"
        r"(?P<unit>m|mn|b|bn|million|millions|billion|billions|млн|млрд)\s*"
        r"(?P<currency>евро|фунт(?:а|ов)?|доллар(?:а|ов)?|EUR|GBP|USD)"
        r"(?!\w)",
        re.IGNORECASE,
    ),
)
_COMBINED_MONEY_RE = re.compile(
    r"(?P<symbol>€|£|\$)\s*(?P<fixed>\d+(?:[.,]\d+)?)\s*"
    r"(?P<fixed_unit>m|mn|million|млн)\s*"
    r"(?:\+|plus)\s*(?P=symbol)?\s*"
    r"(?P<addons>\d+(?:[.,]\d+)?)\s*"
    r"(?P<addons_unit>m|mn|million|млн)"
    r"(?:\s*(?:in\s+)?add-ons?)?",
    re.IGNORECASE,
)
_RANGE_MONEY_RE = re.compile(
    r"(?P<symbol>€|£|\$)\s*(?P<minimum>\d+(?:[.,]\d+)?)\s*"
    r"(?P<min_unit>m|mn|million|млн)\s*(?:-|–|—|to)\s*"
    r"(?P=symbol)?\s*(?P<maximum>\d+(?:[.,]\d+)?)\s*"
    r"(?P<max_unit>m|mn|million|млн)",
    re.IGNORECASE,
)
_CONTRACT_PATTERNS = (
    re.compile(
        r"\b(?:contract|deal|agreement|контракт|соглашение)\s+"
        r"(?:valid\s+)?(?:until|through|до)\s+"
        r"(?P<until>(?:summer|June|July|лет[ао])?\s*20\d{2}(?:\s+года)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<duration>one|two|three|four|five|six|\d+)[ -]year "
        r"(?:contract|deal|agreement)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bконтракт\s+на\s+(?P<duration>один|два|три|четыре|пять|шесть|\d+)"
        r"\s+(?:год|года|лет)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:option for (?:a|one) further year|"
        r"опци(?:я|ей) ещё на один сезон)\b",
        re.IGNORECASE,
    ),
)
_NEGATION_RE = re.compile(
    r"\b(not proceeding|will not advance|no intention to sell|"
    r"does not intend to sell|won['’]t sell|not for sale|"
    r"не рассматривает|не планирует|не намерен|не прода[её]т|"
    r"не будет продвигаться|не продолжит)\b",
    re.IGNORECASE,
)
_TENTATIVE_RE = re.compile(
    r"\b(interested(?: in)?|interest from|could happen|considering|"
    r"willing to sell|in talks|talks|negotiations|possibility|pursuit|"
    r"может|рассматривает|заинтересован(?:а|ы)?|вед[её]т переговоры|"
    r"переговоры|возможност[ьи]|интересуется|готов продать)\b",
    re.IGNORECASE,
)
_COMPLETED_RE = re.compile(
    r"\b(here we go|has joined|have signed|has signed|signed for|"
    r"completed (?:his |her )?(?:move|medical)|deal done|agreement reached|"
    r"переш[её]л|перешла|подписал[аи]?|сделка завершена|"
    r"договорились о переходе|завершил медосмотр)\b",
    re.IGNORECASE,
)
_OFFICIAL_RE = re.compile(
    r"\b(official|officially|официально|announce[ds]?|"
    r"подтвердил[аи]?|объявил[аи]?)\b",
    re.IGNORECASE,
)
_BREAKING_RE = re.compile(r"\b(breaking|срочно)\b", re.IGNORECASE)
_JOURNALIST_RE = re.compile(
    r"\b(here we go|confirmed by|as reported by|"
    r"fabrizio romano|david ornstein|ben jacobs)\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[«“\"](?P<name>[^»”\"\n]{3,60})[»”\"]")
_LATIN_NAME_RE = re.compile(
    r"\b(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’.-]+)"
    r"(?:\s+(?:[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’.-]+)){1,3}\b"
)
_CYRILLIC_NAME_RE = re.compile(
    r"\b(?:[А-ЯЁ][а-яё'’.-]+)(?:\s+(?:[А-ЯЁ][а-яё'’.-]+)){1,2}\b"
)
_PERSON_STOPWORDS = {
    "real madrid",
    "manchester united",
    "manchester city",
    "nottingham forest",
    "aston villa",
    "premier league",
    "champions league",
    "football club",
    "the fa",
    "world cup",
}


def _word_number(value: str) -> int | None:
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "один": 1,
        "два": 2,
        "три": 3,
        "четыре": 4,
        "пять": 5,
        "шесть": 6,
    }
    if value.isdigit():
        return int(value)
    return words.get(value.casefold())


class CandidateExtractor:
    def __init__(
        self,
        *,
        project_root: Path,
        reviewed_registry_path: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.emblems = EmblemRegistry.load(project_root)
        self.entity_aliases = self._load_entity_aliases(
            project_root / "config" / "entity_registry.json"
        )
        if reviewed_registry_path is not None and reviewed_registry_path.is_file():
            self.entity_aliases.extend(
                self._load_reviewed_aliases(reviewed_registry_path)
            )

    @staticmethod
    def _load_entity_aliases(path: Path) -> list[tuple[str, str, str]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = []
        for entity in payload["entities"]:
            for alias in entity.get("aliases", []):
                values.append(
                    (
                        str(alias["alias"]),
                        str(entity["entity_id"]),
                        str(entity["entity_type"]),
                    )
                )
        return values

    @staticmethod
    def _load_reviewed_aliases(path: Path) -> list[tuple[str, str, str]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            (
                str(item["alias"]),
                str(item["entity_id"]),
                str(item["entity_type"]),
            )
            for item in payload["aliases"]
            if item.get("reviewed") is True
        ]

    def extract(
        self, *, source_event_id: int, text: NormalizedText
    ) -> CandidateBundle:
        original = text.original_text
        raw: list[dict[str, object]] = []
        self._extract_registry_entities(original, raw)
        self._extract_clubs(original, raw)
        self._extract_unknown_names(original, raw)
        self._extract_quoted_entities(original, raw)
        self._extract_money(original, raw)
        self._extract_contracts(original, raw)
        self._extract_markers(original, raw)
        unique: dict[tuple[CandidateType, int, int, str], dict[str, object]] = {}
        for item in raw:
            key = (
                item["candidate_type"],
                int(item["start_offset"]),
                int(item["end_offset"]),
                normalize_entity_name(str(item["surface"])),
            )
            existing = unique.get(key)
            if existing is None:
                unique[key] = item
            else:
                existing["registry_matches"] = sorted(
                    set(existing.get("registry_matches", []))
                    | set(item.get("registry_matches", []))
                )
                existing["type_hints"] = sorted(
                    set(existing.get("type_hints", []))
                    | set(item.get("type_hints", []))
                )
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                int(item["start_offset"]),
                int(item["end_offset"]),
                str(item["candidate_type"]),
            ),
        )
        counters: dict[str, int] = {}
        candidates = []
        for item in ordered:
            prefix = {
                CandidateType.PERSON: "entity",
                CandidateType.CLUB: "club",
                CandidateType.ORGANIZATION: "organization",
                CandidateType.MONEY: "money",
                CandidateType.CONTRACT: "contract",
                CandidateType.DATE: "date",
                CandidateType.NEGATION: "negation",
                CandidateType.TENTATIVE: "tentative",
                CandidateType.COMPLETED_MOVE: "completed",
                CandidateType.OFFICIAL: "official",
                CandidateType.BREAKING: "breaking",
                CandidateType.JOURNALIST_CONFIRMATION: "confirmation",
            }[item["candidate_type"]]
            counters[prefix] = counters.get(prefix, 0) + 1
            candidates.append(
                Candidate(
                    candidate_id=f"{prefix}-{counters[prefix]}",
                    **item,
                )
            )
        return CandidateBundle(
            source_event_id=source_event_id,
            candidates=candidates,
        )

    @staticmethod
    def _find_exact(text: str, surface: str) -> Iterable[re.Match[str]]:
        return re.finditer(
            rf"(?<!\w){re.escape(surface)}(?!\w)",
            text,
            re.IGNORECASE,
        )

    def _extract_registry_entities(
        self, text: str, raw: list[dict[str, object]]
    ) -> None:
        for alias, entity_id, entity_type in self.entity_aliases:
            candidate_type = (
                CandidateType.CLUB
                if entity_type == "club"
                else CandidateType.ORGANIZATION
                if entity_type in {"federation", "league", "organization"}
                else CandidateType.PERSON
            )
            for match in self._find_exact(text, alias):
                raw.append(
                    self._raw(
                        candidate_type,
                        match.group(0),
                        match.start(),
                        match.end(),
                        "registry_alias",
                        registry_matches=[entity_id],
                        type_hints=[entity_type],
                    )
                )

    def _extract_clubs(
        self, text: str, raw: list[dict[str, object]]
    ) -> None:
        for entry in self.emblems.entries:
            if entry.verification_status != "verified":
                continue
            for alias in sorted(entry.aliases, key=len, reverse=True):
                if len(normalize_entity_name(alias)) < 3:
                    continue
                for match in self._find_exact(text, alias):
                    raw.append(
                        self._raw(
                            CandidateType.CLUB,
                            match.group(0),
                            match.start(),
                            match.end(),
                            "emblem_registry_alias",
                            registry_matches=[f"club-{entry.id}"],
                            type_hints=["club"],
                        )
                    )

    @staticmethod
    def _extract_unknown_names(
        text: str, raw: list[dict[str, object]]
    ) -> None:
        for pattern in (_LATIN_NAME_RE, _CYRILLIC_NAME_RE):
            for match in pattern.finditer(text):
                surface = match.group(0)
                if normalize_entity_name(surface) in _PERSON_STOPWORDS:
                    continue
                raw.append(
                    CandidateExtractor._raw(
                        CandidateType.PERSON,
                        surface,
                        match.start(),
                        match.end(),
                        "capitalized_name",
                        type_hints=["person"],
                    )
                )

    @staticmethod
    def _extract_quoted_entities(
        text: str, raw: list[dict[str, object]]
    ) -> None:
        for match in _QUOTED_RE.finditer(text):
            start, end = match.span("name")
            raw.append(
                CandidateExtractor._raw(
                    CandidateType.ORGANIZATION,
                    match.group("name"),
                    start,
                    end,
                    "quoted_name",
                    type_hints=["unknown_organization"],
                )
            )

    @staticmethod
    def _extract_money(text: str, raw: list[dict[str, object]]) -> None:
        seen: set[tuple[int, int]] = set()
        for match in _COMBINED_MONEY_RE.finditer(text):
            fixed = Decimal(match.group("fixed").replace(",", ".")) * _MULTIPLIER[
                match.group("fixed_unit").casefold()
            ]
            add_ons = Decimal(match.group("addons").replace(",", ".")) * _MULTIPLIER[
                match.group("addons_unit").casefold()
            ]
            currency = _CURRENCY[match.group("symbol").casefold()]
            raw.append(
                CandidateExtractor._raw(
                    CandidateType.MONEY,
                    match.group(0),
                    match.start(),
                    match.end(),
                    "money_fixed_addons_regex",
                    type_hints=["money", "fixed_plus_add_ons"],
                    parsed={
                        "currency": currency,
                        "amount_value": int(fixed),
                        "fixed_amount": int(fixed),
                        "add_ons_amount": int(add_ons),
                        "total_possible_amount": int(fixed + add_ons),
                        "amount_type": "fixed",
                        "includes_add_ons": True,
                    },
                )
            )
            seen.add(match.span())
        for match in _RANGE_MONEY_RE.finditer(text):
            minimum = Decimal(match.group("minimum").replace(",", ".")) * _MULTIPLIER[
                match.group("min_unit").casefold()
            ]
            maximum = Decimal(match.group("maximum").replace(",", ".")) * _MULTIPLIER[
                match.group("max_unit").casefold()
            ]
            currency = _CURRENCY[match.group("symbol").casefold()]
            raw.append(
                CandidateExtractor._raw(
                    CandidateType.MONEY,
                    match.group(0),
                    match.start(),
                    match.end(),
                    "money_range_regex",
                    type_hints=["money", "range"],
                    parsed={
                        "currency": currency,
                        "amount_min": int(minimum),
                        "amount_max": int(maximum),
                        "amount_type": "range",
                        "includes_add_ons": False,
                    },
                )
            )
            seen.add(match.span())
        for pattern in _MONEY_PATTERNS:
            for match in pattern.finditer(text):
                if any(
                    match.start() >= start and match.end() <= end
                    for start, end in seen
                ):
                    continue
                seen.add(match.span())
                number = Decimal(match.group("number").replace(",", "."))
                unit = (match.group("unit") or "").casefold()
                multiplier = _MULTIPLIER.get(unit, Decimal(1))
                currency = _CURRENCY[match.group("currency").casefold()]
                value = number * multiplier
                raw.append(
                    CandidateExtractor._raw(
                        CandidateType.MONEY,
                        match.group(0),
                        match.start(),
                        match.end(),
                        "money_regex",
                        type_hints=["money"],
                        parsed={
                            "currency": currency,
                            "amount_value": int(value),
                            "amount_type": "fixed",
                            "includes_add_ons": False,
                        },
                    )
                )

    @staticmethod
    def _extract_contracts(text: str, raw: list[dict[str, object]]) -> None:
        for pattern in _CONTRACT_PATTERNS:
            for match in pattern.finditer(text):
                parsed: dict[str, str | int | bool | Decimal | None] = {}
                if match.groupdict().get("until"):
                    parsed["contract_until"] = match.group("until")
                if match.groupdict().get("duration"):
                    parsed["contract_years"] = _word_number(match.group("duration"))
                if "option" in match.group(0).casefold() or "опци" in match.group(0).casefold():
                    parsed["extension_option_years"] = 1
                raw.append(
                    CandidateExtractor._raw(
                        CandidateType.CONTRACT,
                        match.group(0),
                        match.start(),
                        match.end(),
                        "contract_regex",
                        type_hints=["contract"],
                        parsed=parsed,
                    )
                )

    @staticmethod
    def _extract_markers(text: str, raw: list[dict[str, object]]) -> None:
        for candidate_type, detector, pattern in (
            (CandidateType.NEGATION, "negation_regex", _NEGATION_RE),
            (CandidateType.TENTATIVE, "tentative_regex", _TENTATIVE_RE),
            (CandidateType.COMPLETED_MOVE, "completed_move_regex", _COMPLETED_RE),
            (CandidateType.OFFICIAL, "official_marker_regex", _OFFICIAL_RE),
            (CandidateType.BREAKING, "breaking_marker_regex", _BREAKING_RE),
            (
                CandidateType.JOURNALIST_CONFIRMATION,
                "journalist_confirmation_regex",
                _JOURNALIST_RE,
            ),
        ):
            for match in pattern.finditer(text):
                raw.append(
                    CandidateExtractor._raw(
                        candidate_type,
                        match.group(0),
                        match.start(),
                        match.end(),
                        detector,
                    )
                )

    @staticmethod
    def _raw(
        candidate_type: CandidateType,
        surface: str,
        start: int,
        end: int,
        detector: str,
        *,
        registry_matches: list[str] | None = None,
        type_hints: list[str] | None = None,
        parsed: dict[str, str | int | bool | Decimal | None] | None = None,
    ) -> dict[str, object]:
        return {
            "candidate_type": candidate_type,
            "surface": surface,
            "normalized_surface": normalize_entity_name(surface),
            "start_offset": start,
            "end_offset": end,
            "detector": detector,
            "registry_matches": registry_matches or [],
            "type_hints": type_hints or [],
            "parsed": parsed or {},
        }
