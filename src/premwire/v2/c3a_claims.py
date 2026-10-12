from __future__ import annotations

import re
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from premwire.emblem_registry import normalize_entity_name
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .resolution import ConfidenceType, ResolvedEntity
from .c2b_candidates import CandidateExtractor
from .c2b_models import Candidate, CandidateType
from .c2b_source import SourceTextNormalizer
from .c3b_entities import CanonicalEntityResolverV2


CLAUSE_SEGMENTER_VERSION = "stage-c3a-clause-segmenter-1.0"
CLAIM_GRAPH_VERSION = "stage-c3a-transfer-claim-graph-1.0"


class C3AStrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class TokenSpan(C3AStrictModel):
    token_id: int = Field(ge=0)
    surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "TokenSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("token end_offset must exceed start_offset")
        return self


class SourceClause(C3AStrictModel):
    clause_id: str
    surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    sentence_id: int = Field(ge=0)
    language: Literal["ru", "en", "mixed", "unknown"]
    token_spans: tuple[TokenSpan, ...] = ()

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "SourceClause":
        if self.end_offset <= self.start_offset:
            raise ValueError("clause end_offset must exceed start_offset")
        return self


class GraphEntityType(str, Enum):
    PERSON = "person"
    CLUB = "club"
    ORGANIZATION = "organization"
    UNKNOWN = "unknown"


class GraphEntityCandidate(C3AStrictModel):
    candidate_id: str
    entity_type: GraphEntityType
    surface: str
    normalized_surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    clause_id: str
    registry_entity_id: str | None = None
    registry_entity_type: str | None = None
    canonical_name: str | None = None
    resolved: bool = False


class GraphSignal(C3AStrictModel):
    signal_type: Literal[
        "completed",
        "tentative",
        "negation",
        "official",
        "breaking",
        "journalist_confirmation",
        "loan",
    ]
    surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    clause_id: str
    scope: Literal["action", "relation", "destination", "completed", "clause"]


class GraphValueCandidate(C3AStrictModel):
    candidate_id: str
    value_type: Literal["money", "contract"]
    surface: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    clause_id: str
    parsed: dict[str, str | int | bool | None] = Field(default_factory=dict)


class TransferClaimGraph(C3AStrictModel):
    graph_version: Literal["stage-c3a-transfer-claim-graph-1.0"] = (
        CLAIM_GRAPH_VERSION
    )
    source_event_id: str
    subject_candidate: GraphEntityCandidate | None = None
    action_type: Literal[
        "join",
        "sign",
        "leave_join",
        "loan",
        "buy",
        "interest",
        "negated_transfer",
        "unknown",
    ] = "unknown"
    deal_type: Literal[
        "permanent",
        "loan",
        "loan_with_option",
        "loan_with_obligation",
        "free_transfer",
        "unknown",
    ] = "unknown"
    current_club_candidate: GraphEntityCandidate | None = None
    from_club_candidate: GraphEntityCandidate | None = None
    to_club_candidate: GraphEntityCandidate | None = None
    interested_club_candidates: tuple[GraphEntityCandidate, ...] = ()
    amount_candidates: tuple[GraphValueCandidate, ...] = ()
    contract_candidates: tuple[GraphValueCandidate, ...] = ()
    completed_signal: GraphSignal | None = None
    tentative_signal: GraphSignal | None = None
    negation_signal: GraphSignal | None = None
    official_signal: GraphSignal | None = None
    breaking_signal: GraphSignal | None = None
    journalist_confirmation_signal: GraphSignal | None = None
    source_role: str
    evidence_spans: tuple[tuple[int, int], ...] = ()
    parser_pattern_id: str
    parser_confidence_class: Literal["exact", "blocked", "unsupported"]


class C3AParseResult(C3AStrictModel):
    source_event_id: str
    clauses: tuple[SourceClause, ...]
    graph: TransferClaimGraph | None = None
    status: Literal[
        "official",
        "confirmed_report",
        "reported",
        "rumour",
        "unknown",
    ] = "unknown"
    route: Literal["transfer_v2", "loan_v2", "breaking_v2"] | None = None
    blocking_reasons: tuple[str, ...] = ()


class C3AShadowCopyGate:
    """Allow shadow copy only after a deterministic claim is fully routable."""

    def __init__(self, copy_callable):
        self.copy_callable = copy_callable

    def run(self, result: C3AParseResult):
        if (
            result.graph is None
            or result.route is None
            or result.blocking_reasons
        ):
            return None
        return self.copy_callable(result.graph)


_URL_RE = re.compile(r"https?://\S+|www\.\S+|(?:\w+\.)+\w{2,}/\S*", re.I)
_LINK_BLOCK_RE = re.compile(
    r"^\s*(?:link|watch for more|read more|подробнее|смотреть)\b.*$",
    re.I | re.M,
)
_FOOTER_RE = re.compile(
    r"^\s*(?:source|источник)\s*:\s*.+$|"
    r"^\s*(?:📝\s*)?(?:Main Transfers|Albion)\s*$|"
    r"^\s*@[\w.-]+\s*$",
    re.I | re.M,
)
_INLINE_FOOTER_RE = re.compile(
    r"(?<=[.!?])\s+(?:source|источник)\s*:\s*.+$",
    re.I,
)
_TOKEN_RE = re.compile(r"\w+(?:[-’'][\w]+)*|[€£$]\d+(?:[.,]\d+)?|\S", re.U)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])(?:\s+|\n+)|\n{2,}")
_CLAUSE_SEPARATOR_RE = re.compile(
    r";|(?<!\d),(?!\d)|\s+[—–]\s+|:\s+(?=[«“\"A-ZА-ЯЁ])"
)


def _language(surface: str) -> Literal["ru", "en", "mixed", "unknown"]:
    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", surface))
    has_latin = bool(re.search(r"[A-Za-z]", surface))
    if has_cyrillic and has_latin:
        return "mixed"
    if has_cyrillic:
        return "ru"
    if has_latin:
        return "en"
    return "unknown"


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end <= start:
        return None
    return start, end


class ClauseSegmenter:
    """Create reversible clauses while preserving every original offset."""

    version = CLAUSE_SEGMENTER_VERSION

    def segment(self, original_text: str) -> tuple[SourceClause, ...]:
        excluded = [
            match.span()
            for pattern in (
                _URL_RE,
                _LINK_BLOCK_RE,
                _FOOTER_RE,
                _INLINE_FOOTER_RE,
            )
            for match in pattern.finditer(original_text)
        ]

        def overlaps_excluded(start: int, end: int) -> bool:
            return any(start < blocked_end and end > blocked_start for blocked_start, blocked_end in excluded)

        sentence_spans: list[tuple[int, int]] = []
        cursor = 0
        for boundary in _SENTENCE_BOUNDARY_RE.finditer(original_text):
            span = _trimmed_span(original_text, cursor, boundary.start())
            if span:
                sentence_spans.append(span)
            cursor = boundary.end()
        span = _trimmed_span(original_text, cursor, len(original_text))
        if span:
            sentence_spans.append(span)

        clauses: list[SourceClause] = []
        for sentence_id, (sentence_start, sentence_end) in enumerate(
            sentence_spans
        ):
            sentence = original_text[sentence_start:sentence_end]
            boundaries = [0]
            boundaries.extend(
                match.end() for match in _CLAUSE_SEPARATOR_RE.finditer(sentence)
            )
            boundaries.append(len(sentence))
            for local_start, local_end in zip(boundaries, boundaries[1:]):
                absolute = _trimmed_span(
                    original_text,
                    sentence_start + local_start,
                    sentence_start + local_end,
                )
                if absolute is None:
                    continue
                start, end = absolute
                if overlaps_excluded(start, end):
                    # Keep factual text preceding a URL, but never the URL itself.
                    url_starts = [
                        blocked_start
                        for blocked_start, blocked_end in excluded
                        if start < blocked_end and end > blocked_start
                    ]
                    if not url_starts:
                        continue
                    end = min(url_starts)
                    trimmed = _trimmed_span(original_text, start, end)
                    if trimmed is None:
                        continue
                    start, end = trimmed
                surface = original_text[start:end]
                tokens = tuple(
                    TokenSpan(
                        token_id=token_id,
                        surface=match.group(0),
                        start_offset=start + match.start(),
                        end_offset=start + match.end(),
                    )
                    for token_id, match in enumerate(_TOKEN_RE.finditer(surface))
                )
                clauses.append(
                    SourceClause(
                        clause_id=f"clause-{len(clauses) + 1}",
                        surface=surface,
                        start_offset=start,
                        end_offset=end,
                        sentence_id=sentence_id,
                        language=_language(surface),
                        token_spans=tokens,
                    )
                )
        return tuple(clauses)


_PERSON_LATIN = (
    r"[A-ZÀ-ÖØ-ÞĀ-Ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.-]+"
    r"(?:\s+[A-ZÀ-ÖØ-ÞĀ-Ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.-]+){1,3}"
)
_PERSON_CYRILLIC = (
    r"[А-ЯЁ][А-Яа-яЁё'’.-]+"
    r"(?:\s+[А-ЯЁ][А-Яа-яЁё'’.-]+){1,3}"
)
_EN_CLUB = r"[A-ZÀ-ÖØ-ÞĀ-Ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž0-9.&'’ -]{1,60}?"
_RU_ROLE = (
    r"(?:(?:\d{1,2}-летн\w+)\s+)?"
    r"(?:(?:вратарь|голкипер|защитник|полузащитник|нападающий|вингер)\s+)?"
)
_EN_ROLE = (
    r"(?:(?:full-back|right-back|left-back|centre-back|goalkeeper|keeper|"
    r"defender|midfielder|winger|forward|striker)\s+)?"
)


@dataclass(frozen=True, slots=True)
class GrammarPattern:
    pattern_id: str
    language: Literal["ru", "en"]
    action_type: str
    deal_type: str
    regex: re.Pattern[str]


_COMPLETED_PATTERNS = (
    GrammarPattern(
        "ru.subject_move_to_from",
        "ru",
        "join",
        "permanent",
        re.compile(
            rf"{_RU_ROLE}(?P<subject>{_PERSON_CYRILLIC})\s+"
            r"(?P<action>переш[её]л|переходит|присоединился)\s+"
            r"в\s+«(?P<to>[^»]+)»(?:\s+\([^)]*\))?\s+"
            r"из\s+(?:[а-яё-]+\s+)?«(?P<from>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.club_signed_subject_from",
        "ru",
        "sign",
        "permanent",
        re.compile(
            rf"«(?P<to>[^»]+)»\s+"
            r"(?P<action>подписал[аи]?|выкупил[аи]?)\s+"
            rf"{_RU_ROLE}(?P<subject>{_PERSON_CYRILLIC})\s+"
            r"(?:из|у)\s+«(?P<from>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.club_loaned_subject_from",
        "ru",
        "loan",
        "loan",
        re.compile(
            rf"«(?P<to>[^»]+)»\s+"
            r"(?P<action>арендовал[аи]?)\s+"
            rf"{_RU_ROLE}(?P<subject>{_PERSON_CYRILLIC})\s+"
            r"у\s+«(?P<from>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.subject_loaned_by_from",
        "ru",
        "loan",
        "loan",
        re.compile(
            rf"(?P<subject>{_PERSON_CYRILLIC})\s+арендован[а]?\s+"
            r"«(?P<to>[^»]+)»\s+у\s+«(?P<from>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.subject_signed_contract_with",
        "ru",
        "sign",
        "permanent",
        re.compile(
            rf"(?P<subject>{_PERSON_CYRILLIC})\s+подписал[а]?\s+"
            r"контракт\s+с\s+«(?P<to>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.subject_joined_on_loan",
        "ru",
        "loan",
        "loan",
        re.compile(
            rf"{_RU_ROLE}(?P<subject>{_PERSON_CYRILLIC})\s+"
            r"(?P<action>переш[её]л|переходит|присоединился)\s+"
            r"в\s+«(?P<to>[^»]+)»\s+на\s+правах\s+аренды",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.subject_joined_to_from",
        "en",
        "join",
        "permanent",
        re.compile(
            rf"(?P<subject>{_PERSON_LATIN})\s+"
            r"(?P<action>has joined|joined)\s+"
            rf"(?P<to>{_EN_CLUB})\s+"
            r"(?:(?P<loan>on (?:a )?(?:season-long )?loan(?: deal)?)\s+)?"
            rf"from\s+(?P<from>{_EN_CLUB})"
            r"(?=[.,;\n]|\s+on\s+(?:(?:a|an|\d)\b|loan\b)|\s+for\s+|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.club_signed_subject_from",
        "en",
        "sign",
        "permanent",
        re.compile(
            rf"(?P<to>{_EN_CLUB})\s+(?:have|has)\s+signed\s+"
            rf"{_EN_ROLE}(?P<subject>{_PERSON_LATIN})\s+"
            r"(?:(?P<loan>on (?:a )?(?:season-long )?loan)\s+)?"
            rf"from\s+(?P<from>{_EN_CLUB})"
            r"(?=[.,;\n]|\s+on\s+(?:(?:a|an|\d)\b|loan\b)|\s+for\s+|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.subject_left_from_for_to",
        "en",
        "leave_join",
        "permanent",
        re.compile(
            rf"(?P<subject>{_PERSON_LATIN})\s+left\s+"
            rf"(?P<from>{_EN_CLUB})\s+for\s+(?P<to>{_EN_CLUB})"
            r"(?=[.,;\n]|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.club_signed_subject_on_loan",
        "en",
        "loan",
        "loan",
        re.compile(
            rf"(?P<to>{_EN_CLUB})\s+(?:have|has)\s+signed\s+"
            r"(?![^.,\n]*\bfrom\b[^.,\n]*\bon\s+(?:a\s+)?"
            r"(?:season-long\s+)?loan)"
            rf"{_EN_ROLE}(?P<subject>{_PERSON_LATIN})\s+"
            r"(?P<loan>on (?:a )?(?:season-long )?loan)"
            r"(?=[.,;\n]|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.subject_signed_for",
        "en",
        "sign",
        "permanent",
        re.compile(
            rf"(?P<subject>{_PERSON_LATIN})\s+signed\s+for\s+"
            rf"(?P<to>{_EN_CLUB})(?=[.,;\n]|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.subject_completed_move_to",
        "en",
        "join",
        "permanent",
        re.compile(
            rf"(?P<subject>{_PERSON_LATIN})\s+completed\s+(?:a|his|her)\s+"
            rf"move\s+to\s+(?P<to>{_EN_CLUB})(?=[.,;\n]|$)",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.deal_agreed_subject_join_to_from",
        "en",
        "join",
        "permanent",
        re.compile(
            rf"deal\s+agreed\s+for\s+(?P<subject>{_PERSON_LATIN})\s+"
            rf"to\s+join\s+(?P<to>{_EN_CLUB})\s+from\s+"
            rf"(?P<from>{_EN_CLUB})(?=[.,;\n]|$)",
            re.I,
        ),
    ),
)

_INTEREST_PATTERNS = (
    GrammarPattern(
        "ru.club_talks_subject",
        "ru",
        "interest",
        "unknown",
        re.compile(
            r"«(?P<interested>[^»]+)»\s+"
            r"(?P<action>вед[её]т переговоры о подписании|"
            r"рассматривает возможность подписания|"
            r"заинтересован[а]? в подписании|интересуется)\s+"
            rf"(?P<subject>{_PERSON_CYRILLIC})",
            re.I,
        ),
    ),
    GrammarPattern(
        "ru.subject_could_join",
        "ru",
        "interest",
        "unknown",
        re.compile(
            rf"(?P<subject>{_PERSON_CYRILLIC})\s+"
            r"(?P<action>может перейти|может присоединиться)\s+"
            r"в\s+«(?P<interested>[^»]+)»",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.club_interest_subject",
        "en",
        "interest",
        "unknown",
        re.compile(
            rf"(?P<interested>{_EN_CLUB})\s+(?:are|is)\s+"
            r"(?P<negation>not\s+)?"
            r"(?P<action>interested in|considering|monitoring|"
            r"looking to sign)\s+"
            rf"(?P<subject>{_PERSON_LATIN})",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.club_opened_talks_for_subject",
        "en",
        "interest",
        "unknown",
        re.compile(
            rf"(?P<interested>{_EN_CLUB})\s+"
            r"(?P<action>have opened talks for|has opened talks for|"
            r"opened talks for)\s+"
            rf"(?:(?P<current>{_EN_CLUB})['’]s\s+)?"
            rf"(?P<subject>{_PERSON_LATIN})",
            re.I,
        ),
    ),
    GrammarPattern(
        "en.subject_could_join",
        "en",
        "interest",
        "unknown",
        re.compile(
            rf"(?P<subject>{_PERSON_LATIN})\s+"
            r"(?P<action>could join|may join)\s+"
            rf"(?P<interested>{_EN_CLUB})(?=[.,;\n]|$)",
            re.I,
        ),
    ),
)

_LOAN_MARKER_RE = re.compile(
    r"\b(on (?:a )?(?:season-long )?loan|loan deal|loaned|аренд\w*|"
    r"на правах аренды)\b",
    re.I,
)
_OFFICIAL_SIGNAL_RE = re.compile(
    r"\b(official|официально|announce[ds]?|confirm(?:ed)?)\b",
    re.I,
)
_BREAKING_SIGNAL_RE = re.compile(r"\b(breaking|срочно|here we go)\b", re.I)
_TRUSTED_JOURNALIST_ATTRIBUTION_RE = re.compile(
    r"\b(Fabrizio Romano|Фабрицио Романо|Ben Jacobs|Бен Джейкобс)\b",
    re.I,
)
_HISTORICAL_EVENT_RE = re.compile(
    r"\b(on this day|in \d{4}|last season|previously|"
    r"в этот день|в \d{4}(?:-м)? году|в прошлом сезоне|ранее)\b",
    re.I,
)
_FREE_TRANSFER_RE = re.compile(r"\b(free agent|свободн\w+ агент)\b", re.I)
_SECONDARY_TRANSFER_CLAIM_RE = re.compile(
    r"\b(reports? about (?:a )?possible (?:loan|transfer)|"
    r"already completed .+? deal)\b",
    re.I,
)
_COMPLETED_SIGNAL_RE = re.compile(
    r"\b(has joined|joined|left|have signed|has signed|signed for|"
    r"loaned|has been loaned|"
    r"completed (?:a|his|her) move|deal agreed|here we go|"
    r"переш[её]л|переходит|присоединился|подписал[аи]?|"
    r"арендовал[аи]?|арендован[а]?|выкупил[аи]?)\b",
    re.I,
)
_TENTATIVE_SIGNAL_RE = re.compile(
    r"\b(interested in|considering|willing to sell|could join|may join|"
    r"talks ongoing|opened talks|in talks|negotiations|monitoring|"
    r"weighing up|looking to sign|"
    r"рассматривает|заинтересован\w*|может перейти|готов\w* продать|"
    r"вед[её]т переговоры|переговоры|следит за)\b",
    re.I,
)
_NEGATION_SIGNAL_RE = re.compile(
    r"\b(not considering selling|did not join|has not joined|"
    r"no intention of letting .+? leave|"
    r"talks will not proceed|deal is not agreed|not proceeding|"
    r"will not advance|no intention to sell|does not intend to sell|"
    r"not looking to sign|not true|"
    r"reports? about .+? (?:are|is) inaccurate|"
    r"не рассматривает продажу|не переш[её]л|не присоединился|"
    r"не намерен отпускать|"
    r"переговоры не продвигаются|сделка не согласована|"
    r"не намерен продавать|не планирует продавать)\b",
    re.I,
)
_NEXT_MONEY_MARKER_RE = re.compile(
    r"\b(fee|transfer fee|loan fee|deal worth|"
    r"сумма трансфера|стоимость сделки|сумма аренды)\b",
    re.I,
)
_SALARY_MARKER_RE = re.compile(
    r"\b(salary|wage|per week|per season|зарплат\w*|в неделю|за сезон)\b",
    re.I,
)
_CONTRACT_MARKER_RE = re.compile(
    r"\b(contract|deal through|signed until|"
    r"контракт|соглашение)\b",
    re.I,
)


class DeterministicEntityResolver:
    """Resolve only reviewed aliases and existing canonical/emblem registries."""

    def __init__(self, project_root: Path) -> None:
        self.v2 = CanonicalEntityResolverV2(project_root)

    def resolve(self, surface: str, expected_type: str):
        resolved = self.v2.resolve(surface, entity_type=expected_type)
        return ResolvedEntity(
            entity_id=resolved.entity_id,
            entity_type=resolved.entity_type,
            canonical_name=resolved.canonical_name or surface,
            display_name_ru=resolved.display_name_ru,
            surname_display=None,
            status="verified" if resolved.resolved else "unresolved",
            provenance=resolved.match_method.value,
            confidence_type=(
                ConfidenceType.NORMALIZED_ALIAS
                if resolved.resolved
                else ConfidenceType.UNRESOLVED
            ),
            grammatical_forms={},
        )


class DeterministicTransferClaimParser:
    """Parse only an explicit, allow-listed transfer grammar."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.segmenter = ClauseSegmenter()
        self.entities = DeterministicEntityResolver(project_root)
        self.candidate_extractor = CandidateExtractor(
            project_root=project_root,
            reviewed_registry_path=(
                project_root
                / "config"
                / "entity_registry_stage_c2_reviewed.json"
            ),
        )
        self.normalizer = SourceTextNormalizer()

    @staticmethod
    def _clause_for_offset(
        clauses: tuple[SourceClause, ...], offset: int
    ) -> SourceClause:
        for clause in clauses:
            if clause.start_offset <= offset < clause.end_offset:
                return clause
        raise ValueError(f"offset {offset} does not belong to a clause")

    def _entity(
        self,
        *,
        source_event_id: str,
        match: re.Match[str],
        group: str,
        entity_type: GraphEntityType,
        clauses: tuple[SourceClause, ...],
    ) -> GraphEntityCandidate | None:
        if group not in match.groupdict() or match.group(group) is None:
            return None
        group_surface = match.group(group)
        raw_surface = group_surface.strip(" \t\r\n«»“”\"'.,")
        if entity_type is GraphEntityType.PERSON:
            name_matches = [
                *re.finditer(_PERSON_LATIN, raw_surface),
                *re.finditer(_PERSON_CYRILLIC, raw_surface),
            ]
            name_matches.sort(key=lambda item: (item.start(), item.end()))
            if name_matches:
                chosen = name_matches[-1]
                surface = chosen.group(0)
                surface_offset = group_surface.find(raw_surface) + chosen.start()
            else:
                surface = raw_surface
                surface_offset = group_surface.find(raw_surface)
        else:
            surface = re.sub(
                r"\s+(?:have|has)$", "", raw_surface, flags=re.I
            )
            surface_offset = group_surface.find(surface)
        raw_start = match.start(group)
        start = raw_start + max(0, surface_offset)
        end = start + len(surface)
        expected_type = "player" if entity_type is GraphEntityType.PERSON else "club"
        resolved = self.entities.resolve(surface, expected_type)
        clause = self._clause_for_offset(clauses, start)
        return GraphEntityCandidate(
            candidate_id=f"{source_event_id}:{entity_type.value}:{start}:{end}",
            entity_type=entity_type,
            surface=surface,
            normalized_surface=normalize_entity_name(surface),
            start_offset=start,
            end_offset=end,
            clause_id=clause.clause_id,
            registry_entity_id=resolved.entity_id,
            registry_entity_type=resolved.entity_type if resolved.resolved else None,
            canonical_name=resolved.canonical_name if resolved.resolved else None,
            resolved=resolved.resolved,
        )

    @staticmethod
    def _signal(
        *,
        source_event_id: str,
        signal_type: str,
        match: re.Match[str],
        clauses: tuple[SourceClause, ...],
        scope: str,
    ) -> GraphSignal:
        start, end = match.span()
        clause = DeterministicTransferClaimParser._clause_for_offset(
            clauses, start
        )
        return GraphSignal(
            signal_type=signal_type,
            surface=match.group(0),
            start_offset=start,
            end_offset=end,
            clause_id=clause.clause_id,
            scope=scope,
        )

    @staticmethod
    def _global_match(
        pattern: re.Pattern[str],
        original_text: str,
        clause: SourceClause,
    ) -> re.Match[str] | None:
        return pattern.search(
            original_text,
            clause.start_offset,
            clause.end_offset,
        )

    @staticmethod
    def _next_clause(
        clauses: tuple[SourceClause, ...], clause: SourceClause
    ) -> SourceClause | None:
        for index, item in enumerate(clauses):
            if item.clause_id != clause.clause_id:
                continue
            if index + 1 >= len(clauses):
                return None
            candidate = clauses[index + 1]
            return (
                candidate
                if candidate.sentence_id == clause.sentence_id
                else None
            )
        return None

    @staticmethod
    def _next_clause_any(
        clauses: tuple[SourceClause, ...], clause: SourceClause
    ) -> SourceClause | None:
        for index, item in enumerate(clauses):
            if item.clause_id == clause.clause_id:
                return clauses[index + 1] if index + 1 < len(clauses) else None
        return None

    @staticmethod
    def _sentence_scoped_match(
        pattern: re.Pattern[str],
        original_text: str,
        clauses: tuple[SourceClause, ...],
        clause: SourceClause,
    ) -> re.Match[str] | None:
        same_sentence = [
            item for item in clauses if item.sentence_id == clause.sentence_id
        ]
        if not same_sentence:
            return None
        return pattern.search(
            original_text,
            clause.start_offset,
            max(item.end_offset for item in same_sentence),
        )

    @staticmethod
    def _clause_containing_candidate(
        clauses: tuple[SourceClause, ...], candidate: Candidate
    ) -> SourceClause | None:
        for clause in clauses:
            if (
                clause.start_offset <= candidate.start_offset
                and candidate.end_offset <= clause.end_offset
            ):
                return clause
        return None

    @staticmethod
    def _safe_parsed(
        parsed: dict[str, object],
    ) -> dict[str, str | int | bool | None]:
        safe: dict[str, str | int | bool | None] = {}
        for key, value in parsed.items():
            if value is None or isinstance(value, (str, int, bool)):
                safe[key] = value
            else:
                safe[key] = str(value)
        return safe

    def _value_candidate(
        self,
        candidate: Candidate,
        clause: SourceClause,
        value_type: Literal["money", "contract"],
    ) -> GraphValueCandidate:
        return GraphValueCandidate(
            candidate_id=candidate.candidate_id,
            value_type=value_type,
            surface=candidate.surface,
            start_offset=candidate.start_offset,
            end_offset=candidate.end_offset,
            clause_id=clause.clause_id,
            parsed=self._safe_parsed(candidate.parsed),
        )

    def _bind_values(
        self,
        *,
        graph: TransferClaimGraph,
        original_text: str,
        clauses: tuple[SourceClause, ...],
        claim_clause: SourceClause,
    ) -> tuple[TransferClaimGraph, tuple[str, ...]]:
        bundle = self.candidate_extractor.extract(
            source_event_id=1,
            text=self.normalizer.normalize(original_text),
        )
        next_clause = self._next_clause_any(clauses, claim_clause)
        money: list[GraphValueCandidate] = []
        contracts: list[GraphValueCandidate] = []
        for candidate in bundle.candidates:
            candidate_clause = self._clause_containing_candidate(
                clauses, candidate
            )
            if candidate_clause is None:
                continue
            if candidate.candidate_type is CandidateType.MONEY:
                same_clause = candidate_clause.clause_id == claim_clause.clause_id
                explicit_next = (
                    next_clause is not None
                    and candidate_clause.clause_id == next_clause.clause_id
                    and _NEXT_MONEY_MARKER_RE.search(next_clause.surface)
                    is not None
                )
                if not same_clause and not explicit_next:
                    continue
                local_context = candidate_clause.surface
                if _SALARY_MARKER_RE.search(local_context):
                    continue
                money.append(
                    self._value_candidate(candidate, candidate_clause, "money")
                )
            elif candidate.candidate_type is CandidateType.CONTRACT:
                same_clause = candidate_clause.clause_id == claim_clause.clause_id
                explicit_next = (
                    next_clause is not None
                    and candidate_clause.clause_id == next_clause.clause_id
                    and _CONTRACT_MARKER_RE.search(next_clause.surface)
                    is not None
                )
                if same_clause or explicit_next:
                    contracts.append(
                        self._value_candidate(
                            candidate, candidate_clause, "contract"
                        )
                    )
        # Nested money detector hits are removed in favour of the widest span.
        widest_money: list[GraphValueCandidate] = []
        for item in sorted(
            money,
            key=lambda value: (
                value.start_offset,
                -(value.end_offset - value.start_offset),
            ),
        ):
            if any(
                existing.start_offset <= item.start_offset
                and item.end_offset <= existing.end_offset
                for existing in widest_money
            ):
                continue
            widest_money.append(item)
        blockers: list[str] = []
        if len(widest_money) > 1:
            compound = self._compound_base_and_bonus_amount(
                widest_money, original_text, clauses
            )
            if compound is None:
                blockers.append("BLOCKED_AMBIGUOUS_AMOUNT_BINDING")
                widest_money = []
            else:
                widest_money = [compound]
        if len(contracts) > 1:
            blockers.append("BLOCKED_AMBIGUOUS_CONTRACT_BINDING")
            contracts = []
        evidence = {
            *graph.evidence_spans,
            *(
                (item.start_offset, item.end_offset)
                for item in (*widest_money, *contracts)
            ),
        }
        return (
            graph.model_copy(
                update={
                    "amount_candidates": tuple(widest_money),
                    "contract_candidates": tuple(contracts),
                    "evidence_spans": tuple(sorted(evidence)),
                }
            ),
            tuple(blockers),
        )

    @staticmethod
    def _compound_base_and_bonus_amount(
        money: list[GraphValueCandidate],
        original_text: str,
        clauses: tuple[SourceClause, ...],
    ) -> GraphValueCandidate | None:
        if len(money) != 2:
            return None
        first, second = sorted(money, key=lambda item: item.start_offset)
        if first.clause_id != second.clause_id:
            return None
        connector = original_text[first.end_offset : second.start_offset]
        if re.fullmatch(r"\s*\+\s*", connector) is None:
            return None
        clause = next(
            (item for item in clauses if item.clause_id == second.clause_id),
            None,
        )
        if clause is None:
            return None
        suffix = original_text[second.end_offset : clause.end_offset]
        if re.search(
            r"\b(?:add-ons?|bonuses|бонус\w*|в качестве бонусов)\b",
            suffix,
            re.I,
        ) is None:
            return None
        first_value = first.parsed.get("amount_value")
        second_value = second.parsed.get("amount_value")
        if not isinstance(first_value, int) or not isinstance(second_value, int):
            return None
        first_currency = first.parsed.get("currency")
        second_currency = second.parsed.get("currency")
        if first_currency != second_currency:
            return None
        return GraphValueCandidate(
            candidate_id=f"compound-{first.candidate_id}-{second.candidate_id}",
            value_type="money",
            surface=original_text[first.start_offset : second.end_offset],
            start_offset=first.start_offset,
            end_offset=second.end_offset,
            clause_id=first.clause_id,
            parsed={
                "currency": str(first_currency),
                "amount_value": first_value + second_value,
                "amount_type": "base_plus_add_ons",
                "includes_add_ons": True,
            },
        )

    @staticmethod
    def _relation_blockers(
        graph: TransferClaimGraph,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        subject = graph.subject_candidate
        if subject is None:
            blockers.append("BLOCKED_MISSING_SUBJECT")
        elif not subject.resolved:
            blockers.append("BLOCKED_UNRESOLVED_SUBJECT")
        elif subject.registry_entity_type not in {"player"}:
            blockers.append("BLOCKED_SUBJECT_NOT_PLAYER")

        destination = graph.to_club_candidate
        if destination is None:
            blockers.append("BLOCKED_MISSING_TO_CLUB")
        elif not destination.resolved:
            blockers.append("BLOCKED_UNRESOLVED_TO_CLUB")
        elif destination.registry_entity_type != "club":
            blockers.append("BLOCKED_TO_ENTITY_NOT_CLUB")

        origin = graph.from_club_candidate
        if graph.deal_type != "loan":
            if origin is None:
                blockers.append("BLOCKED_MISSING_FROM_CLUB")
            elif not origin.resolved:
                blockers.append("BLOCKED_UNRESOLVED_FROM_CLUB")
            elif origin.registry_entity_type != "club":
                blockers.append("BLOCKED_FROM_ENTITY_NOT_CLUB")
        elif origin is not None:
            if not origin.resolved:
                blockers.append("BLOCKED_UNRESOLVED_FROM_CLUB")
            elif origin.registry_entity_type != "club":
                blockers.append("BLOCKED_FROM_ENTITY_NOT_CLUB")

        if (
            origin is not None
            and destination is not None
            and origin.registry_entity_id is not None
            and destination.registry_entity_id is not None
            and origin.registry_entity_id == destination.registry_entity_id
        ):
            blockers.append("BLOCKED_FROM_TO_IDENTICAL")
        return tuple(dict.fromkeys(blockers))

    @staticmethod
    def _status(
        graph: TransferClaimGraph, blockers: tuple[str, ...] = ()
    ) -> str:
        official_roles = {
            "official_club",
            "official_league",
            "official_federation",
            "official_player",
        }
        if (
            graph.negation_signal is not None
            or "BLOCKED_HISTORICAL_EVENT" in blockers
        ):
            return "unknown"
        if graph.tentative_signal is not None or graph.action_type == "interest":
            return "rumour"
        if (
            graph.source_role in official_roles
            and graph.completed_signal is not None
            and graph.official_signal is not None
        ):
            return "official"
        if (
            (
                graph.source_role == "journalist"
                or graph.journalist_confirmation_signal is not None
            )
            and graph.completed_signal is not None
        ):
            return "confirmed_report"
        if graph.completed_signal is not None:
            return "reported"
        return "unknown"

    @staticmethod
    def _route(
        graph: TransferClaimGraph,
        *,
        status: str,
        blockers: tuple[str, ...],
    ) -> str | None:
        if blockers:
            return None
        if status not in {"official", "confirmed_report", "reported"}:
            return None
        trusted_breaking_source = (
            graph.source_role
            in {
                "journalist",
                "official_club",
                "official_league",
                "official_federation",
                "official_player",
            }
            or graph.journalist_confirmation_signal is not None
        )
        if (
            graph.breaking_signal is not None
            and trusted_breaking_source
            and status in {"official", "confirmed_report"}
        ):
            return "breaking_v2"
        if graph.deal_type in {
            "loan",
            "loan_with_option",
            "loan_with_obligation",
        }:
            return "loan_v2"
        if graph.deal_type in {"permanent", "free_transfer"}:
            return "transfer_v2"
        return None

    @staticmethod
    def _evidence_blockers(
        graph: TransferClaimGraph, original_text: str
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        for start, end in graph.evidence_spans:
            if start < 0 or end <= start or end > len(original_text):
                blockers.append("BLOCKED_EVIDENCE_OUTSIDE_SOURCE")
        return tuple(dict.fromkeys(blockers))

    def parse(
        self,
        *,
        source_event_id: str,
        original_text: str,
        source_role: str,
    ) -> C3AParseResult:
        clauses = self.segmenter.segment(original_text)
        matches: list[tuple[GrammarPattern, re.Match[str], SourceClause]] = []
        for clause in clauses:
            for pattern in _COMPLETED_PATTERNS:
                for local_match in pattern.regex.finditer(clause.surface):
                    # Re-run on the exact original slice so group offsets are global.
                    global_match = pattern.regex.search(
                        original_text,
                        clause.start_offset + local_match.start(),
                        clause.start_offset + local_match.end(),
                    )
                    if global_match is not None:
                        matches.append((pattern, global_match, clause))
        unique = {
            (
                pattern.pattern_id,
                match.start(),
                match.end(),
            ): (pattern, match, clause)
            for pattern, match, clause in matches
        }
        matches = list(unique.values())
        if not matches:
            interest_matches: list[
                tuple[GrammarPattern, re.Match[str], SourceClause]
            ] = []
            for clause in clauses:
                for pattern in _INTEREST_PATTERNS:
                    for local_match in pattern.regex.finditer(clause.surface):
                        global_match = pattern.regex.search(
                            original_text,
                            clause.start_offset + local_match.start(),
                            clause.start_offset + local_match.end(),
                        )
                        if global_match is not None:
                            interest_matches.append(
                                (pattern, global_match, clause)
                            )
            interest_unique = {
                (pattern.pattern_id, match.start(), match.end()): (
                    pattern,
                    match,
                    clause,
                )
                for pattern, match, clause in interest_matches
            }
            interest_matches = list(interest_unique.values())
            if len(interest_matches) > 1:
                return C3AParseResult(
                    source_event_id=source_event_id,
                    clauses=clauses,
                    blocking_reasons=("BLOCKED_MULTIPLE_CLAIMS",),
                )
            if interest_matches:
                pattern, match, clause = interest_matches[0]
                if _SECONDARY_TRANSFER_CLAIM_RE.search(
                    original_text, match.end()
                ):
                    return C3AParseResult(
                        source_event_id=source_event_id,
                        clauses=clauses,
                        blocking_reasons=("BLOCKED_MULTIPLE_CLAIMS",),
                    )
                subject = self._entity(
                    source_event_id=source_event_id,
                    match=match,
                    group="subject",
                    entity_type=GraphEntityType.PERSON,
                    clauses=clauses,
                )
                interested = self._entity(
                    source_event_id=source_event_id,
                    match=match,
                    group="interested",
                    entity_type=GraphEntityType.CLUB,
                    clauses=clauses,
                )
                current = self._entity(
                    source_event_id=source_event_id,
                    match=match,
                    group="current",
                    entity_type=GraphEntityType.CLUB,
                    clauses=clauses,
                )
                tentative_match = self._global_match(
                    _TENTATIVE_SIGNAL_RE, original_text, clause
                )
                negation_match = self._global_match(
                    _NEGATION_SIGNAL_RE, original_text, clause
                )
                if negation_match is None:
                    negation_match = self._sentence_scoped_match(
                        _NEGATION_SIGNAL_RE,
                        original_text,
                        clauses,
                        clause,
                    )
                tentative = (
                    self._signal(
                        source_event_id=source_event_id,
                        signal_type="tentative",
                        match=tentative_match,
                        clauses=clauses,
                        scope="action",
                    )
                    if tentative_match
                    else None
                )
                negation = (
                    self._signal(
                        source_event_id=source_event_id,
                        signal_type="negation",
                        match=negation_match,
                        clauses=clauses,
                        scope="action",
                    )
                    if negation_match
                    else None
                )
                evidence = {
                    (match.start(), match.end()),
                    *(
                        (candidate.start_offset, candidate.end_offset)
                        for candidate in (subject, interested, current)
                        if candidate is not None
                    ),
                }
                graph = TransferClaimGraph(
                    source_event_id=source_event_id,
                    subject_candidate=subject,
                    action_type=(
                        "negated_transfer" if negation else "interest"
                    ),
                    deal_type="unknown",
                    current_club_candidate=current,
                    interested_club_candidates=(
                        (interested,) if interested is not None else ()
                    ),
                    tentative_signal=tentative,
                    negation_signal=negation,
                    source_role=source_role,
                    evidence_spans=tuple(sorted(evidence)),
                    parser_pattern_id=pattern.pattern_id,
                    parser_confidence_class="blocked",
                )
                blockers = ["BLOCKED_TENTATIVE"]
                if negation is not None:
                    blockers.insert(0, "BLOCKED_NEGATED_CLAIM")
                return C3AParseResult(
                    source_event_id=source_event_id,
                    clauses=clauses,
                    graph=graph,
                    status="unknown" if negation is not None else "rumour",
                    blocking_reasons=tuple(blockers),
                )
            return C3AParseResult(
                source_event_id=source_event_id,
                clauses=clauses,
                blocking_reasons=("BLOCKED_UNSUPPORTED_SYNTAX",),
            )
        if len(matches) > 1:
            return C3AParseResult(
                source_event_id=source_event_id,
                clauses=clauses,
                blocking_reasons=("BLOCKED_MULTIPLE_CLAIMS",),
            )
        pattern, match, clause = matches[0]
        raw_subject = match.groupdict().get("subject") or ""
        if re.search(r"(?:\b(?:and|и)\b|,)", raw_subject, re.I):
            return C3AParseResult(
                source_event_id=source_event_id,
                clauses=clauses,
                blocking_reasons=("BLOCKED_MULTIPLE_SUBJECTS",),
            )
        subject = self._entity(
            source_event_id=source_event_id,
            match=match,
            group="subject",
            entity_type=GraphEntityType.PERSON,
            clauses=clauses,
        )
        from_club = self._entity(
            source_event_id=source_event_id,
            match=match,
            group="from",
            entity_type=GraphEntityType.CLUB,
            clauses=clauses,
        )
        to_club = self._entity(
            source_event_id=source_event_id,
            match=match,
            group="to",
            entity_type=GraphEntityType.CLUB,
            clauses=clauses,
        )
        loan_match = _LOAN_MARKER_RE.search(clause.surface)
        free_transfer_match = _FREE_TRANSFER_RE.search(original_text)
        deal_type = (
            "loan"
            if loan_match
            else "free_transfer"
            if free_transfer_match
            else pattern.deal_type
        )
        action_type = "loan" if loan_match else pattern.action_type
        completed_match = _COMPLETED_SIGNAL_RE.search(
            original_text, match.start(), match.end()
        )
        completed = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="completed",
                match=completed_match,
                clauses=clauses,
                scope="completed",
            )
            if completed_match
            else None
        )
        official_match = _OFFICIAL_SIGNAL_RE.search(original_text)
        official = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="official",
                match=official_match,
                clauses=clauses,
                scope="clause",
            )
            if official_match
            else None
        )
        breaking_match = _BREAKING_SIGNAL_RE.search(original_text)
        breaking = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="breaking",
                match=breaking_match,
                clauses=clauses,
                scope="clause",
            )
            if breaking_match
            else None
        )
        journalist_match = _TRUSTED_JOURNALIST_ATTRIBUTION_RE.search(
            original_text
        )
        journalist_confirmation = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="journalist_confirmation",
                match=journalist_match,
                clauses=clauses,
                scope="clause",
            )
            if journalist_match
            else None
        )
        tentative_match = self._global_match(
            _TENTATIVE_SIGNAL_RE, original_text, clause
        )
        tentative = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="tentative",
                match=tentative_match,
                clauses=clauses,
                scope="action",
            )
            if tentative_match
            else None
        )
        negation_match = self._global_match(
            _NEGATION_SIGNAL_RE, original_text, clause
        )
        if negation_match is None:
            adjacent = self._next_clause(clauses, clause)
            if (
                adjacent is not None
                and re.search(
                    r"\b(deal|talks|сделка|переговоры)\b",
                    adjacent.surface,
                    re.I,
                )
            ):
                negation_match = self._global_match(
                    _NEGATION_SIGNAL_RE, original_text, adjacent
                )
        negation = (
            self._signal(
                source_event_id=source_event_id,
                signal_type="negation",
                match=negation_match,
                clauses=clauses,
                scope="completed",
            )
            if negation_match
            else None
        )
        evidence = {
            (match.start(), match.end()),
            *(
                (candidate.start_offset, candidate.end_offset)
                for candidate in (subject, from_club, to_club)
                if candidate is not None
            ),
            *(
                (signal.start_offset, signal.end_offset)
                for signal in (
                    completed,
                    tentative,
                    negation,
                    official,
                    breaking,
                    journalist_confirmation,
                )
                if signal is not None
            ),
        }
        graph = TransferClaimGraph(
            source_event_id=source_event_id,
            subject_candidate=subject,
            action_type=action_type,
            deal_type=deal_type,
            from_club_candidate=from_club,
            to_club_candidate=to_club,
            completed_signal=completed,
            tentative_signal=tentative,
            negation_signal=negation,
            official_signal=official,
            breaking_signal=breaking,
            journalist_confirmation_signal=journalist_confirmation,
            source_role=source_role,
            evidence_spans=tuple(sorted(evidence)),
            parser_pattern_id=pattern.pattern_id,
            parser_confidence_class="exact",
        )
        graph, value_blockers = self._bind_values(
            graph=graph,
            original_text=original_text,
            clauses=clauses,
            claim_clause=clause,
        )
        blockers = [
            *value_blockers,
            *self._relation_blockers(graph),
            *(
                ("BLOCKED_NEGATED_CLAIM",)
                if negation is not None
                else ("BLOCKED_TENTATIVE",)
                if tentative is not None
                else ()
            ),
        ]
        if _HISTORICAL_EVENT_RE.search(original_text):
            blockers.append("BLOCKED_HISTORICAL_EVENT")
        blockers.extend(self._evidence_blockers(graph, original_text))
        blockers_tuple = tuple(dict.fromkeys(blockers))
        status = self._status(graph, blockers_tuple)
        route = self._route(
            graph,
            status=status,
            blockers=blockers_tuple,
        )
        return C3AParseResult(
            source_event_id=source_event_id,
            clauses=clauses,
            graph=graph,
            status=status,
            route=route,
            blocking_reasons=blockers_tuple,
        )
