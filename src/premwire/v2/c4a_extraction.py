from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .c4a_entities import EntityHit, StrategyBEntityRegistry
from .c4a_models import (
    C4AStrictModel,
    ContractEvent,
    ContractTerms,
    EntityRef,
    EvidenceSpan,
    FactualStatus,
    MoneyValue,
    MovementType,
    PlayerLifecycleEvent,
    RouteDecision,
    SourceIdentity,
    SourceRole,
    StrategyBEventType,
    StrategyBParseResult,
    TransferEvent,
    TransferInterestEvent,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class StrategyBSourceEnvelope(C4AStrictModel):
    story_id: str
    source_event_id: str
    source_id: str
    source_role: SourceRole = SourceRole.UNKNOWN
    published_at: str
    language: Literal["ru", "en", "mixed", "unknown"]
    text: str = Field(min_length=1)


AMOUNT_RE = re.compile(
    r"(?:[€£$]\s?\d+(?:[.,]\d+)?\s?(?:m|million|млн)?|"
    r"\d+(?:[.,]\d+)?\s?(?:m|million|млн)\s?(?:euros?|pounds?|евро|фунт\w*)?)",
    re.IGNORECASE,
)
CONTRACT_RE = re.compile(
    r"(?:contract|deal|контракт|соглашени\w*)[^.!?\n]{0,55}(?:until|до|through)?\s*(?:June\s+)?20\d{2}|"
    r"(?:one|two|three|four|five|six|\d+)[ -]year (?:contract|deal|extension)|"
    r"(?:один|два|три|четыре|пять|шесть|\d+)\s*(?:года|год|лет)",
    re.IGNORECASE,
)
TEMPORAL_RE = re.compile(
    r"today|tomorrow|this (?:summer|winter|week|season)|next (?:week|season|summer)|"
    r"in (?:January|June|July|August)|сегодня|завтра|этим летом|этой зимой|"
    r"на этой неделе|в следующем сезоне|в январе|летом",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(?:did|does|do|is|are|was|were|will|would|has|have|had|can|could)\s+not\s+"
    r"(?:join|move|sign|agree|transfer|leave|return|extend|renew|consider|pursue)|"
    r"\bnot\s+(?:interested|considering|joining|moving|signing|agreed|official|true)\b|"
    r"\bnot\s+(?:yet\s+)?in\s+(?:talks|negotiations)\b|"
    r"\bno agreement\b|denied|rejected (?:the |a |an )?(?:move|interest|offer|bid|agreement)|"
    r"ruled out|false report|"
    r"\bне\s+(?:переш|переход|интерес|рассматри|подпис|соглас|вед|продл|вернул|расторг)|"
    r"нет соглашения|опроверг|"
    r"(?:отклонил|отказал)\s+(?:предложение|заявку|переход|интерес)|"
    r"(?:предложени\w*|заявк\w*)\s+отклон",
    re.IGNORECASE,
)
HISTORICAL_RE = re.compile(
    r"last (?:year|season|summer|winter)|(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+) years? ago|previously|former(?:ly)?|"
    r"before (?:he|she|they) (?:joined|moved|signed)|"
    r"в прошлом (?:году|сезоне)|\d+ (?:года|лет) назад|ранее выступал|бывш\w* игрок",
    re.IGNORECASE,
)
ATTRIBUTION_RE = re.compile(
    r"Fabrizio Romano|Фабрицио Романо|David Ornstein|Дэвид Орнштейн|"
    r"Ben Jacobs|Бен Джейкобс|The Athletic|Sky Sports|BBC Sport|L[’']Équipe|"
    r"according to|по данным|сообщает|пишет|reports?",
    re.IGNORECASE,
)
EXTENSION_RE = re.compile(
    r"extended (?:his |her )?(?:contract|deal)|contract extension|renewed (?:his |her )?(?:contract|deal)|"
    r"contract (?:was )?extended|agreed (?:a )?new (?:contract|deal)|"
    r"reached (?:an )?agreement (?:over|on) (?:a )?new (?:contract|deal)|"
    r"set to sign (?:a )?new (?:contract|deal)|"
    r"продлил[аи]? контракт|продление контракта|договорил\w* о новом контракте|"
    r"достиг\w* соглашени\w* о новом контракте|договор продл\w* до",
    re.IGNORECASE,
)
NEW_CONTRACT_RE = re.compile(
    r"signed (?:(?:a|his|her) )?new (?:contract|deal)|signs new (?:contract|deal)|"
    r"signed (?:a )?new deal|подписал[аи]? новый контракт|подписал[аи]? новую сделку|новое соглашение",
    re.IGNORECASE,
)
FREE_AGENT_RE = re.compile(r"free agent|свободн\w* агент", re.IGNORECASE)
RELEASE_RE = re.compile(
    r"(?:was |has been )?released by|contract (?:was )?terminated|terminated (?:his |her )?contract with|"
    r"[A-Z][A-Za-zÀ-ž .&'’-]+ terminated [A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3}'s contract|"
    r"left after (?:his |her )?contract expir|освобожд[её]н|расторг\w* контракт|покинул\w*.*истечени",
    re.IGNORECASE,
)
RETURN_LOAN_RE = re.compile(
    r"returned from (?:his |her )?loan at|recalled .* from loan at|loan at .* ended and .* returned to|"
    r"вернул\w* .* из аренды в|вернул\w* из аренды|вернулся из аренды (?:в|из)",
    re.IGNORECASE,
)
INTEREST_MARKER_RE = re.compile(
    r"interested in|showing (?:strong )?interest in|remain keen to sign|monitoring|considering (?:a move|signing)|"
    r"targeting|keen on|exploring (?:a move|a deal)|pursuit of|race to sign|joined the race (?:to sign|for)|"
    r"talks to sign|talks over (?:a |the )?(?:potential )?move|negotiations? (?:to sign|ongoing)|"
    r"working on (?:a )?deal to sign|close to (?:signing|agreeing a deal)|"
    r"интересу\w*|интерес\s+к|проявля\w* интерес|рассматривает (?:возможность )?подписан|"
    r"рассматривает (?:возможность )?трансфер|нацелен\w* на|"
    r"вступил\w* в борьбу за подписан|вед[её]т переговоры (?:о|по)|переговоры по подписанию|"
    r"готов\w* предложить|работает над возможным трансфером|борьбу за подписание",
    re.IGNORECASE,
)
TALKS_RE = re.compile(r"talks|negotiations|переговор", re.IGNORECASE)
BID_RE = re.compile(
    r"bid (?:accepted|submitted|made|rejected)|(?:formal )?offer (?:submitted|made|rejected)|"
    r"официальн\w* предложени|заявк",
    re.IGNORECASE,
)
AGREEMENT_RE = re.compile(
    r"deal agreed|agreed a deal|agreement reached|agreement in place|"
    r"сделка согласована|договор[её]нность достигнута",
    re.IGNORECASE,
)
QUOTED_SPECULATION_RE = re.compile(
    r"(?:said|claimed|suggested|believes?|сказал|заявил|считает)\s*:\s*[\"«“][^\"»”]{0,300}"
    r"(?:interested|join|sign|move|transfer|интерес|перейд|подпиш)",
    re.IGNORECASE,
)
CORRECTION_RE = re.compile(r"^\s*(?:correction|исправление)\s*:", re.IGNORECASE)
NON_TRANSFER_CONTEXT_RE = re.compile(
    r"\b(?:article|profile|performance|performances|highlights|merchandise|autograph|sponsorship)\b|"
    r"(?:статья|профиль|выступлени\w*|автограф|мерч|спонсорск\w*)",
    re.IGNORECASE,
)


TRANSFER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "en_subject_joined_to_from",
        re.compile(
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?i:(?:has\s+)?(?:joined|moved|transferred))\s+@?(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:from)\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)(?=[.,;!]|\s+(?:on|for|after)\b|$)"
        ),
    ),
    (
        "en_subject_moved_to_from",
        re.compile(
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?i:(?:has\s+)?(?:moved|transferred)\s+to)\s+@?(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:from)\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)(?=[.,;!]|\s+(?:on|for|after)\b|$)"
        ),
    ),
    (
        "en_club_signed_player_from",
        re.compile(
            r"@?(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:(?:have|has)\s+(?:officially\s+)?signed)\s+"
            r"(?i:(?:(?:goalkeeper|defender|midfielder|forward|winger)\s+)?)"
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+(?i:from)\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)(?=[.,;!]|\s+(?:on|for|after)\b|$)"
        ),
    ),
    (
        "en_club_agreed_with_for_signing",
        re.compile(
            r"(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:(?:have|has)\s+(?:agreed\s+a\s+deal|reached\s+(?:an\s+)?agreement)\s+with)\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:for\s+the\s+signing\s+of)\s+"
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})"
            r"(?=[.,;!]|\s+(?:on|for|after)\b|$)"
        ),
    ),
    (
        "en_club_agree_to_sign_from",
        re.compile(
            r"@?(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+"
            r"(?i:(?:have|has|are|is)?\s*(?:agreed?|agree|set)\s+(?:a\s+)?(?:deal\s+)?to\s+sign)\s+"
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+(?i:from)\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)(?=[.,;!]|\s+(?:on|for|after|here)\b|$)"
        ),
    ),
    (
        "en_subject_join_to_from",
        re.compile(
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?i:(?:(?:has\s+)?agree+d?\s+to\s+|will\s+)?(?:sign|joins?))\s+(?i:(?:for\s+)?)@?(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+"
            r"(?i:(?:on\s+(?:a\s+)?(?:season-long\s+)?loan\s+)?from)\s+(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)"
            r"(?=[.,;!]|\s+(?:on|for|after)\b|$)"
        ),
    ),
    (
        "ru_subject_to_from",
        re.compile(
            r"(?P<subject>[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?:официально\s+)?(?:переш[её]л|переходит|перебрался|отправился)\s+"
            r"(?:на правах аренды\s+)?в\s+[«\"](?P<to>[^»\"]+)[»\"](?:\s*\([^)]{2,40}\))?\s+"
            r"из\s+[«\"](?P<from>[^»\"]+)[»\"]"
        ),
    ),
    (
        "ru_subject_from_to",
        re.compile(
            r"(?P<subject>[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?:официально\s+)?(?:переш[её]л|переходит|перебрался)\s+из\s+[«\"](?P<from>[^»\"]+)[»\"]\s+"
            r"в\s+[«\"](?P<to>[^»\"]+)[»\"]"
        ),
    ),
)


# A compact Russian headline can state that a named player is a "new player"
# of a club without spelling out a source club.  It is enough to identify the
# asserted destination deterministically, but not enough for TransferEvent,
# whose closed factual contract requires both endpoints.  The parser therefore
# recognises the construction and fails closed with MISSING_EXPLICIT_FROM_CLUB.
COMPACT_TRANSFER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ru_subject_new_player_to",
        re.compile(
            r"(?P<subject>[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+){1,3})\s*"
            r"[–—-]\s*нов\w*\s+игрок\s+(?:[«\"](?P<to>[^»\"]+)[»\"]|(?P<to_unquoted>[А-ЯЁ][А-Яа-яЁёA-Za-zÀ-ž'’.-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+){0,3}))"
            r"(?=\s*(?:[.!?]|📝|❗|$))",
            re.IGNORECASE,
        ),
    ),
)


BID_TRANSFER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "en_bid_accepted",
        re.compile(
            r"(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:(?:have|has)\s+had\s+(?:a\s+)?bid\s+accepted\s+(?:by|from))\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:for)\s+"
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})(?=[.,;!]|$)"
        ),
    ),
    (
        "en_formal_offer_submitted",
        re.compile(
            r"(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:(?:have|has)\s+(?:submitted|made)\s+(?:a\s+)?formal\s+offer\s+(?:to|for))\s+"
            r"(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:for)\s+"
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})(?=[.,;!]|$)"
        ),
    ),
)


# A departure report is not a completed move.  These patterns only bind the
# two explicitly role-marked clubs and are emitted as transfer-rumour events.
DEPARTURE_TRANSFER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "en_subject_leave_from_for",
        re.compile(
            r"(?P<subject>[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"(?i:(?:could|may|might)\s+leave)\s+(?P<from>[A-Z][A-Za-zÀ-ž .&'’-]+?)\s+(?i:for)\s+"
            r"(?P<to>[A-Z][A-Za-zÀ-ž .&'’-]+?)(?=[.,;!]|$)"
        ),
    ),
    (
        "ru_subject_leave_from_to",
        re.compile(
            r"(?P<subject>[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-zÀ-ž'’.-]+){1,3})\s+"
            r"может\s+покинуть\s+[«\"](?P<from>[^»\"]+)[»\"]\s+и\s+перейти\s+в\s+[«\"](?P<to>[^»\"]+)[»\"]"
        ),
    ),
)


def _evidence(envelope: StrategyBSourceEnvelope, field: str, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(
        field=field,
        source_text=envelope.text[start:end],
        start_offset=start,
        end_offset=end,
        source_event_id=envelope.source_event_id,
    )


def _entity_ref(envelope: StrategyBSourceEnvelope, hit: EntityHit, field: str) -> EntityRef:
    return EntityRef(
        entity_id=hit.entity_id,
        entity_type=hit.entity_type,
        canonical_name=hit.canonical_name,
        source_mention=hit.source_mention,
        evidence=_evidence(envelope, field, hit.start_offset, hit.end_offset),
    )


def _all_matches(regex: re.Pattern[str], text: str) -> tuple[re.Match[str], ...]:
    return tuple(regex.finditer(text))


class StrategyBFactualExtractor:
    """Deterministic, fail-closed Strategy B extractor. No model dependency."""

    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.registry = StrategyBEntityRegistry(project_root)

    def parse(self, envelope: StrategyBSourceEnvelope) -> StrategyBParseResult:
        started = time.perf_counter()
        try:
            return self._parse(envelope, started)
        except (ValueError, TypeError) as exc:
            return self._blocked(envelope, started, f"VALIDATION_ERROR:{type(exc).__name__}")

    def _blocked(
        self, envelope: StrategyBSourceEnvelope, started: float, *reasons: str,
        grammar_family: str | None = None,
    ) -> StrategyBParseResult:
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id,
            route_decision=RouteDecision.BLOCKED,
            blocking_reasons=tuple(dict.fromkeys(reasons or ("UNSUPPORTED_STRATEGY_B_GRAMMAR",))),
            grammar_family=grammar_family,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _source(self, envelope: StrategyBSourceEnvelope) -> SourceIdentity:
        match = ATTRIBUTION_RE.search(envelope.text)
        attribution = _evidence(envelope, "attribution", *match.span()) if match else None
        return SourceIdentity(
            source_id=envelope.source_id,
            source_role=envelope.source_role,
            published_at=envelope.published_at,
            original_language=envelope.language,
            account_is_attribution=match is None,
            attribution_evidence=attribution,
        )

    @staticmethod
    def _status(envelope: StrategyBSourceEnvelope, family: str) -> FactualStatus:
        text = envelope.text
        if NEGATION_RE.search(text):
            return FactualStatus.NEGATED
        if re.search(r"denied|not true|опроверг", text, re.IGNORECASE):
            return FactualStatus.DENIED
        if re.search(r"here we go", text, re.IGNORECASE):
            return FactualStatus.CONFIRMED_REPORT
        if AGREEMENT_RE.search(text):
            return (
                FactualStatus.AGREED
                if envelope.source_role in {SourceRole.JOURNALIST, SourceRole.OFFICIAL_CLUB}
                else FactualStatus.CONFIRMED_REPORT
            )
        if BID_RE.search(text):
            return FactualStatus.BID_REPORTED
        if TALKS_RE.search(text):
            return FactualStatus.TALKS
        if re.search(r"close to (?:agreeing|signing|joining)|близ\w* к (?:соглашению|переходу)", text, re.IGNORECASE):
            return FactualStatus.RUMOUR
        if family == "interest":
            return FactualStatus.INTEREST
        if family == "rumour":
            return FactualStatus.RUMOUR
        if re.search(r"official(?:ly)?|официально", text, re.IGNORECASE) and envelope.source_role == SourceRole.OFFICIAL_CLUB:
            return FactualStatus.OFFICIAL
        return FactualStatus.CONFIRMED_REPORT

    def _parse(self, envelope: StrategyBSourceEnvelope, started: float) -> StrategyBParseResult:
        text = envelope.text
        if CORRECTION_RE.search(text):
            return self._blocked(envelope, started, "CORRECTION_REQUIRES_REVIEW")
        if QUOTED_SPECULATION_RE.search(text):
            return self._blocked(envelope, started, "QUOTED_SPECULATION")
        if NEGATION_RE.search(text):
            return self._blocked(envelope, started, "NEGATED_OR_DENIED")
        if HISTORICAL_RE.search(text):
            return self._blocked(envelope, started, "HISTORICAL_EVENT")
        if INTEREST_MARKER_RE.search(text) and NON_TRANSFER_CONTEXT_RE.search(text):
            return self._blocked(envelope, started, "UNSUPPORTED_STRATEGY_B_GRAMMAR")

        players = self.registry.scan(text, "player")
        subject_hit = self._resolve_subject(text, players)
        if subject_hit is None:
            return self._blocked(envelope, started, "UNKNOWN_OR_MULTIPLE_SUBJECTS")

        if EXTENSION_RE.search(text):
            return self._parse_contract(envelope, started, subject_hit, extension=True)
        if NEW_CONTRACT_RE.search(text):
            return self._parse_contract(envelope, started, subject_hit, extension=False)
        if RETURN_LOAN_RE.search(text) or FREE_AGENT_RE.search(text) or RELEASE_RE.search(text):
            return self._parse_lifecycle(envelope, started, subject_hit)

        for grammar, regex in TRANSFER_PATTERNS:
            match = regex.search(text)
            if match:
                return self._parse_transfer(envelope, started, subject_hit, grammar, match)

        for grammar, regex in COMPACT_TRANSFER_PATTERNS:
            match = regex.search(text)
            if match:
                return self._parse_compact_transfer(envelope, started, subject_hit, grammar, match)

        for grammar, regex in BID_TRANSFER_PATTERNS:
            match = regex.search(text)
            if match:
                return self._parse_bid_transfer(envelope, started, subject_hit, grammar, match)

        for grammar, regex in DEPARTURE_TRANSFER_PATTERNS:
            match = regex.search(text)
            if match:
                return self._parse_departure_transfer(envelope, started, subject_hit, grammar, match)

        if INTEREST_MARKER_RE.search(text) or TALKS_RE.search(text) or BID_RE.search(text):
            return self._parse_interest(envelope, started, subject_hit)
        return self._blocked(envelope, started, "UNSUPPORTED_STRATEGY_B_GRAMMAR")

    def _resolve_subject(self, text: str, players: tuple[EntityHit, ...]) -> EntityHit | None:
        """Resolve a participant only when identity is unique and explicitly bound.

        Normal text keeps the original one-player rule.  When contextual text
        names another player, a transfer grammar capture may disambiguate the
        actual participant, but only if every matched transfer capture resolves
        to the same reviewed canonical player.
        """
        if len(players) == 1:
            return players[0]
        captured: dict[str, EntityHit] = {}
        for _, regex in (*TRANSFER_PATTERNS, *COMPACT_TRANSFER_PATTERNS, *DEPARTURE_TRANSFER_PATTERNS):
            for match in regex.finditer(text):
                hit = self.registry.resolve_capture(text, *match.span("subject"), "player")
                subject_start, _ = match.span("subject")
                coordinated_other = any(
                    other.entity_id != hit.entity_id
                    and other.end_offset <= subject_start
                    and re.fullmatch(r"\s*(?:and|&|и)\s*", text[other.end_offset:subject_start], re.IGNORECASE)
                    for other in players
                ) if hit is not None else False
                if hit is not None and not coordinated_other:
                    captured[hit.entity_id] = hit
        return next(iter(captured.values())) if len(captured) == 1 else None

    def _parse_compact_transfer(
        self, envelope: StrategyBSourceEnvelope, started: float, scanned_subject: EntityHit,
        grammar: str, match: re.Match[str],
    ) -> StrategyBParseResult:
        """Recognise an explicit compact headline without inventing FROM_CLUB."""
        subject = self.registry.resolve_capture(envelope.text, *match.span("subject"), "player")
        to_group = "to" if match.group("to") is not None else "to_unquoted"
        to_club = self.registry.resolve_capture(envelope.text, *match.span(to_group), "club")
        if subject is None or to_club is None:
            return self._blocked(envelope, started, "UNKNOWN_CRITICAL_ENTITY", grammar_family=grammar)
        if subject.entity_id != scanned_subject.entity_id:
            return self._blocked(envelope, started, "AMBIGUOUS_SUBJECT_BINDING", grammar_family=grammar)
        return self._blocked(envelope, started, "MISSING_EXPLICIT_FROM_CLUB", grammar_family=grammar)

    def _parse_bid_transfer(
        self, envelope: StrategyBSourceEnvelope, started: float, scanned_subject: EntityHit,
        grammar: str, match: re.Match[str],
    ) -> StrategyBParseResult:
        """Represent an explicit bid as a rumour, never as a completed transfer."""
        subject = self.registry.resolve_capture(envelope.text, *match.span("subject"), "player")
        from_club = self.registry.resolve_capture(envelope.text, *match.span("from"), "club")
        to_club = self.registry.resolve_capture(envelope.text, *match.span("to"), "club")
        if subject is None or from_club is None or to_club is None:
            return self._blocked(envelope, started, "UNKNOWN_CRITICAL_ENTITY", grammar_family=grammar)
        if subject.entity_id != scanned_subject.entity_id or from_club.entity_id == to_club.entity_id:
            return self._blocked(envelope, started, "AMBIGUOUS_SUBJECT_BINDING", grammar_family=grammar)
        subject_ref = _entity_ref(envelope, subject, "subject")
        from_ref = _entity_ref(envelope, from_club, "current_club")
        to_ref = _entity_ref(envelope, to_club, "interested_club")
        claim = _evidence(envelope, "bid_claim", *match.span())
        event = TransferInterestEvent(
            story_id=envelope.story_id, source=self._source(envelope), status=FactualStatus.BID_REPORTED,
            event_type=StrategyBEventType.TRANSFER_RUMOUR, subject=subject_ref,
            current_club=from_ref, interested_clubs=(to_ref,),
            evidence_spans=(claim, subject_ref.evidence, from_ref.evidence, to_ref.evidence),
        )
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family=grammar,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _parse_transfer(
        self, envelope: StrategyBSourceEnvelope, started: float, scanned_subject: EntityHit,
        grammar: str, match: re.Match[str],
    ) -> StrategyBParseResult:
        subject = self.registry.resolve_capture(envelope.text, *match.span("subject"), "player")
        from_club = self.registry.resolve_capture(envelope.text, *match.span("from"), "club")
        to_club = self.registry.resolve_capture(envelope.text, *match.span("to"), "club")
        if subject is None or from_club is None or to_club is None:
            return self._blocked(envelope, started, "UNKNOWN_CRITICAL_ENTITY", grammar_family=grammar)
        if subject.entity_id != scanned_subject.entity_id:
            return self._blocked(envelope, started, "AMBIGUOUS_SUBJECT_BINDING", grammar_family=grammar)
        if from_club.entity_id == to_club.entity_id:
            return self._blocked(envelope, started, "SAME_FROM_TO", grammar_family=grammar)

        amounts = self._bound_transfer_amounts(envelope.text, match)
        contracts = _all_matches(CONTRACT_RE, envelope.text)
        if len(amounts) > 1:
            return self._blocked(envelope, started, "MULTIPLE_AMOUNTS_REQUIRE_BINDING", grammar_family=grammar)
        if len(contracts) > 1:
            return self._blocked(envelope, started, "MULTIPLE_CONTRACTS_REQUIRE_BINDING", grammar_family=grammar)
        movement = MovementType.LOAN if re.search(r"\bloan\b|аренд", envelope.text, re.IGNORECASE) else MovementType.PERMANENT
        status = self._status(envelope, "transfer")
        if status not in {FactualStatus.OFFICIAL, FactualStatus.AGREED, FactualStatus.CONFIRMED_REPORT}:
            return self._blocked(envelope, started, "SPECULATIVE_STATUS_CANNOT_ROUTE_TRANSFER", grammar_family=grammar)
        if re.search(r"here we go", envelope.text, re.IGNORECASE):
            event_type = StrategyBEventType.CONFIRMED_TRANSFER_REPORT
        elif movement == MovementType.LOAN:
            event_type = StrategyBEventType.COMPLETED_LOAN
        else:
            event_type = StrategyBEventType.COMPLETED_TRANSFER
        subject_ref = _entity_ref(envelope, subject, "subject")
        from_ref = _entity_ref(envelope, from_club, "from_club")
        to_ref = _entity_ref(envelope, to_club, "to_club")
        claim = _evidence(envelope, "event_claim", *match.span())
        money = self._money(envelope, amounts[0]) if amounts else None
        contract = self._contract_terms(envelope, contracts[0]) if contracts else None
        evidence = [claim, subject_ref.evidence, from_ref.evidence, to_ref.evidence]
        if money:
            evidence.append(money.evidence)
        if contract:
            evidence.append(contract.evidence)
        event = TransferEvent(
            story_id=envelope.story_id, source=self._source(envelope), status=status,
            event_type=event_type, movement_type=movement, subject=subject_ref,
            from_club=from_ref, to_club=to_ref, amount=money, contract=contract,
            temporal_qualifier=self._temporal(envelope), evidence_spans=tuple(evidence),
        )
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family=grammar,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _parse_departure_transfer(
        self, envelope: StrategyBSourceEnvelope, started: float, scanned_subject: EntityHit,
        grammar: str, match: re.Match[str],
    ) -> StrategyBParseResult:
        """Bind an explicit possible departure without upgrading it to a transfer."""
        subject = self.registry.resolve_capture(envelope.text, *match.span("subject"), "player")
        from_club = self.registry.resolve_capture(envelope.text, *match.span("from"), "club")
        to_club = self.registry.resolve_capture(envelope.text, *match.span("to"), "club")
        if subject is None or from_club is None or to_club is None:
            return self._blocked(envelope, started, "UNKNOWN_CRITICAL_ENTITY", grammar_family=grammar)
        if subject.entity_id != scanned_subject.entity_id or from_club.entity_id == to_club.entity_id:
            return self._blocked(envelope, started, "AMBIGUOUS_SUBJECT_BINDING", grammar_family=grammar)
        subject_ref = _entity_ref(envelope, subject, "subject")
        from_ref = _entity_ref(envelope, from_club, "current_club")
        to_ref = _entity_ref(envelope, to_club, "interested_club")
        claim = _evidence(envelope, "departure_claim", *match.span())
        event = TransferInterestEvent(
            story_id=envelope.story_id, source=self._source(envelope), status=FactualStatus.RUMOUR,
            event_type=StrategyBEventType.TRANSFER_RUMOUR, subject=subject_ref,
            current_club=from_ref, interested_clubs=(to_ref,),
            evidence_spans=(claim, subject_ref.evidence, from_ref.evidence, to_ref.evidence),
        )
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family=grammar,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _parse_interest(
        self, envelope: StrategyBSourceEnvelope, started: float, subject: EntityHit,
    ) -> StrategyBParseResult:
        clubs = self.registry.scan(envelope.text, "club")
        interested_hits, current = self._bind_interest(envelope.text, subject, clubs)
        if not interested_hits:
            return self._blocked(envelope, started, "UNKNOWN_INTERESTED_CLUB", grammar_family="interest_clause_binding")
        amounts = self._bound_interest_amounts(envelope.text)
        if len(amounts) > 1:
            return self._blocked(envelope, started, "MULTIPLE_AMOUNTS_REQUIRE_BINDING", grammar_family="interest_clause_binding")
        interest_family = "rumour" if TALKS_RE.search(envelope.text) or BID_RE.search(envelope.text) or AGREEMENT_RE.search(envelope.text) or re.search(r"close to (?:agreeing|signing|joining)|близ\w* к (?:соглашению|переходу)", envelope.text, re.IGNORECASE) else "interest"
        status = self._status(envelope, interest_family)
        if status in {FactualStatus.NEGATED, FactualStatus.DENIED, FactualStatus.HISTORICAL}:
            return self._blocked(envelope, started, "NON_POSITIVE_INTEREST_STATUS", grammar_family="interest_clause_binding")
        event_type = (
            StrategyBEventType.CLUB_INTEREST
            if status == FactualStatus.INTEREST
            else StrategyBEventType.TRANSFER_RUMOUR
        )
        subject_ref = _entity_ref(envelope, subject, "subject")
        interested_refs = tuple(_entity_ref(envelope, club, "interested_club") for club in interested_hits)
        current_ref = _entity_ref(envelope, current, "current_club") if current else None
        money = self._money(envelope, amounts[0]) if amounts else None
        evidence = [subject_ref.evidence, *(club.evidence for club in interested_refs)]
        if current_ref:
            evidence.append(current_ref.evidence)
        if money:
            evidence.append(money.evidence)
        event = TransferInterestEvent(
            story_id=envelope.story_id, source=self._source(envelope), status=status,
            event_type=event_type, subject=subject_ref, current_club=current_ref,
            interested_clubs=interested_refs, amount=money,
            temporal_qualifier=self._temporal(envelope), evidence_spans=tuple(evidence),
        )
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family="interest_clause_binding",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _bind_interest(
        text: str, subject: EntityHit, clubs: tuple[EntityHit, ...],
    ) -> tuple[tuple[EntityHit, ...], EntityHit | None]:
        interested: dict[str, EntityHit] = {}
        current: EntityHit | None = None
        for marker in INTEREST_MARKER_RE.finditer(text):
            clause_start = max(text.rfind(".", 0, marker.start()), text.rfind("!", 0, marker.start()), text.rfind("?", 0, marker.start())) + 1
            clause_end_candidates = [p for p in (text.find(".", marker.end()), text.find("!", marker.end()), text.find("?", marker.end())) if p >= 0]
            clause_end = min(clause_end_candidates) if clause_end_candidates else len(text)
            for club in clubs:
                if clause_start <= club.start_offset < marker.start() and marker.start() - club.end_offset <= 120:
                    interested[club.entity_id] = club
                elif marker.end() <= club.start_offset < subject.start_offset <= clause_end:
                    gap = text[club.end_offset:subject.start_offset]
                    if re.search(r"(?:centre-back|center-back|midfielder|defender|winger|attacker|forward|star|игрок|вингер|защитник|полузащитник)", gap, re.IGNORECASE):
                        current = current or club
            # Russian "interest in Player is shown by Club A and Club B" has
            # the interested clubs after the resolved player.  Bind that
            # inversion only for this exact local relation, never by mention
            # order alone.
            if (
                re.fullmatch(r"интерес\s+к", marker.group(0), re.IGNORECASE)
                and clause_start <= subject.start_offset
                and subject.end_offset <= clause_end
                and re.match(r"\s+проявля\w*\s+", text[subject.end_offset:clause_end], re.IGNORECASE)
            ):
                for club in clubs:
                    if subject.end_offset < club.start_offset < clause_end:
                        interested[club.entity_id] = club
        for club in clubs:
            if club.start_offset > subject.end_offset:
                prefix = text[max(subject.end_offset, club.start_offset - 18):club.start_offset]
                if re.search(r"\bfrom\s*$|\bиз\s+[«\"]?$", prefix, re.IGNORECASE):
                    current = current or club
            elif club.end_offset <= subject.start_offset:
                gap = text[club.end_offset:subject.start_offset]
                if re.fullmatch(r".{0,4}(?:'s\s+|(?:centre-back|center-back|midfielder|defender|winger|attacker|forward|star|youngster|вингер(?:ом)?|защитник(?:ом)?|полузащитник(?:ом)?)\s+).{0,20}", gap, re.IGNORECASE):
                    current = current or club
        if current:
            interested.pop(current.entity_id, None)
        return tuple(sorted(interested.values(), key=lambda hit: hit.entity_id)), current

    def _parse_contract(
        self, envelope: StrategyBSourceEnvelope, started: float, subject: EntityHit, *, extension: bool,
    ) -> StrategyBParseResult:
        clubs = self.registry.scan(envelope.text, "club")
        unique_clubs = tuple({club.entity_id: club for club in clubs}.values())
        contracts = _all_matches(CONTRACT_RE, envelope.text)
        if len(unique_clubs) != 1:
            return self._blocked(envelope, started, "UNKNOWN_OR_MULTIPLE_CONTRACT_CLUBS", grammar_family="contract_extension" if extension else "new_contract")
        if len(contracts) != 1:
            return self._blocked(envelope, started, "MISSING_OR_MULTIPLE_CONTRACT_EVIDENCE", grammar_family="contract_extension" if extension else "new_contract")
        clause_start, clause_end = self._sentence_bounds(envelope.text, contracts[0].start())
        if len(set(re.findall(r"20\d{2}", envelope.text[clause_start:clause_end]))) > 1:
            return self._blocked(envelope, started, "MULTIPLE_CONTRACT_DATES_REQUIRE_BINDING", grammar_family="contract_extension" if extension else "new_contract")
        subject_ref = _entity_ref(envelope, subject, "subject")
        club_ref = _entity_ref(envelope, unique_clubs[0], "club")
        terms = self._contract_terms(envelope, contracts[0])
        event = ContractEvent(
            story_id=envelope.story_id, source=self._source(envelope),
            status=self._status(envelope, "contract"),
            event_type=StrategyBEventType.CONTRACT_EXTENSION if extension else StrategyBEventType.NEW_CONTRACT,
            subject=subject_ref, club=club_ref, terms=terms,
            temporal_qualifier=self._temporal(envelope),
            evidence_spans=(subject_ref.evidence, club_ref.evidence, terms.evidence),
        )
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family="contract_extension" if extension else "new_contract",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _parse_lifecycle(
        self, envelope: StrategyBSourceEnvelope, started: float, subject: EntityHit,
    ) -> StrategyBParseResult:
        text = envelope.text
        clubs = self.registry.scan(text, "club")
        subject_ref = _entity_ref(envelope, subject, "subject")
        if RETURN_LOAN_RE.search(text):
            roles = self._bind_return_loan(text, subject, clubs)
            if roles is None:
                return self._blocked(envelope, started, "AMBIGUOUS_RETURN_LOAN_RELATION", grammar_family="return_from_loan")
            previous, parent = roles
            previous_ref = _entity_ref(envelope, previous, "previous_loan_club")
            parent_ref = _entity_ref(envelope, parent, "parent_club")
            event = PlayerLifecycleEvent(
                story_id=envelope.story_id, source=self._source(envelope),
                status=self._status(envelope, "lifecycle"), event_type=StrategyBEventType.RETURN_FROM_LOAN,
                subject=subject_ref, previous_loan_club=previous_ref, parent_club=parent_ref,
                temporal_qualifier=self._temporal(envelope),
                evidence_spans=(subject_ref.evidence, previous_ref.evidence, parent_ref.evidence),
            )
            grammar = "return_from_loan"
        elif RELEASE_RE.search(text):
            if len(clubs) != 1:
                return self._blocked(envelope, started, "RELEASE_REQUIRES_ONE_EXPLICIT_CLUB", grammar_family="release")
            club_ref = _entity_ref(envelope, clubs[0], "club")
            event_type = StrategyBEventType.CONTRACT_TERMINATION if re.search(r"terminat|расторг", text, re.IGNORECASE) else StrategyBEventType.PLAYER_RELEASE
            event = PlayerLifecycleEvent(
                story_id=envelope.story_id, source=self._source(envelope),
                status=self._status(envelope, "lifecycle"), event_type=event_type,
                subject=subject_ref, club=club_ref, temporal_qualifier=self._temporal(envelope),
                evidence_spans=(subject_ref.evidence, club_ref.evidence),
            )
            grammar = "release_or_termination"
        else:
            explicit_club = clubs[0] if len(clubs) == 1 and re.search(r"contract terminated with|расторг\w* контракт с", text, re.IGNORECASE) else None
            club_ref = _entity_ref(envelope, explicit_club, "club") if explicit_club else None
            evidence = (subject_ref.evidence,) if club_ref is None else (subject_ref.evidence, club_ref.evidence)
            event = PlayerLifecycleEvent(
                story_id=envelope.story_id, source=self._source(envelope),
                status=self._status(envelope, "lifecycle"), event_type=StrategyBEventType.FREE_AGENT,
                subject=subject_ref, club=club_ref, temporal_qualifier=self._temporal(envelope),
                evidence_spans=evidence,
            )
            grammar = "free_agent"
        return StrategyBParseResult(
            source_event_id=envelope.source_event_id, route_decision=RouteDecision.ROUTED,
            event=event, grammar_family=grammar,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _bind_return_loan(
        text: str, subject: EntityHit, clubs: tuple[EntityHit, ...],
    ) -> tuple[EntityHit, EntityHit] | None:
        if len(clubs) != 2:
            return None
        match = re.search(r"returned from (?:his |her )?loan at .+? to ", text, re.IGNORECASE)
        if match:
            before = [club for club in clubs if club.start_offset < match.end()]
            after = [club for club in clubs if club.start_offset >= match.end()]
            if len(before) == 1 and len(after) == 1:
                return before[0], after[0]
        if re.search(r"вернулся из аренды из ", text, re.IGNORECASE):
            ordered = sorted(clubs, key=lambda club: club.start_offset)
            if len(ordered) == 2 and subject.end_offset < ordered[0].start_offset:
                return ordered[0], ordered[1]
        return None

    @staticmethod
    def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
        start = max(text.rfind(".", 0, position), text.rfind("!", 0, position), text.rfind("?", 0, position)) + 1
        endings = [value for value in (text.find(".", position), text.find("!", position), text.find("?", position)) if value >= 0]
        return start, (min(endings) + 1 if endings else len(text))

    @classmethod
    def _bound_transfer_amounts(cls, text: str, claim: re.Match[str]) -> tuple[re.Match[str], ...]:
        bound: list[re.Match[str]] = []
        claim_start, claim_end = cls._sentence_bounds(text, claim.start())
        for amount in _all_matches(AMOUNT_RE, text):
            amount_start, _ = cls._sentence_bounds(text, amount.start())
            bridge = text[claim.end():amount.start()]
            same_sentence = claim_start <= amount.start() <= claim_end
            explicit_followup = (
                claim_end <= amount.start()
                and amount.start() - claim.end() <= 140
                and re.search(r"(?:transfer fee|сумма трансфера)\s*:\s*$", text[amount_start:amount.start()], re.IGNORECASE)
            )
            if (same_sentence and re.search(r"(?:\bfor\b|transfer fee|fee of|paid|\bза\b|сумм\w*)[^.!?\n]{0,45}$", bridge, re.IGNORECASE)) or explicit_followup:
                bound.append(amount)
        return tuple(bound)

    @classmethod
    def _bound_interest_amounts(cls, text: str) -> tuple[re.Match[str], ...]:
        bound: list[re.Match[str]] = []
        for amount in _all_matches(AMOUNT_RE, text):
            start, end = cls._sentence_bounds(text, amount.start())
            clause = text[start:end]
            if re.search(
                r"\bbid\b|\boffer\b|valuation|valued at|asking price|price tag|"
                r"предложени\w*|заявк\w*|оцен\w*|цен\w*",
                clause,
                re.IGNORECASE,
            ):
                bound.append(amount)
        return tuple(bound)

    @staticmethod
    def _money(envelope: StrategyBSourceEnvelope, match: re.Match[str]) -> MoneyValue:
        raw = match.group(0)
        number_match = re.search(r"\d+(?:[.,]\d+)?", raw)
        value = None
        if number_match:
            numeric = float(number_match.group(0).replace(",", "."))
            multiplier = 1_000_000 if re.search(r"m\b|million|млн", raw, re.IGNORECASE) else 1
            value = int(numeric * multiplier)
        currency = "EUR" if "€" in raw or re.search(r"euro|евро", raw, re.IGNORECASE) else "GBP" if "£" in raw or re.search(r"pound|фунт", raw, re.IGNORECASE) else "USD" if "$" in raw else None
        return MoneyValue(raw=raw, value=value, currency=currency, evidence=_evidence(envelope, "amount", *match.span()))

    @staticmethod
    def _contract_terms(envelope: StrategyBSourceEnvelope, match: re.Match[str]) -> ContractTerms:
        raw = match.group(0)
        year = re.search(r"20\d{2}", raw)
        duration = re.search(r"(one|two|three|four|five|six|\d+)[ -]year", raw, re.IGNORECASE)
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
        duration_years = None
        if duration:
            token = duration.group(1).casefold()
            duration_years = int(token) if token.isdigit() else words[token]
        option_match = re.search(r"option[^.!?\n]{0,35}", envelope.text[match.start():match.end() + 80], re.IGNORECASE)
        return ContractTerms(
            raw=raw, new_expiry=year.group(0) if year else None,
            duration_years=duration_years,
            option=option_match.group(0).strip() if option_match else None,
            evidence=_evidence(envelope, "contract", *match.span()),
        )

    @staticmethod
    def _temporal(envelope: StrategyBSourceEnvelope) -> str | None:
        match = TEMPORAL_RE.search(envelope.text)
        return match.group(0) if match else None
