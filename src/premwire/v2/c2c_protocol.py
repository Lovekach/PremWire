from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .c2b_models import (
    Candidate,
    CandidateBundle,
    CandidateType,
    RelationCandidateSelection,
    SemanticProposal,
    SemanticSignals,
    SourceMetadata,
)
from .orchestration import JsonLlmClient


PROPOSAL_SCHEMA_VERSION = "c2-semantic-proposal-2.0"
EXTRACTION_PROMPT_VERSION = "stage-c2-3.0"
CATEGORY_BY_INDEX = {
    0: "transfer",
    1: "loan",
    2: "club_news",
    3: "quote",
    4: "statistics",
    5: "administrative",
    6: "other",
    7: "unknown",
}
_SENTENCE_END_RE = re.compile(r"(?:[.!?]+(?=\s|$)|\n+)")
_CLAUSE_BREAK_RE = re.compile(r"[,;:](?=\s)")
_TOKEN_RE = re.compile(r"\s+|\S+")


class FlatProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: int
    subject: int
    current_club: int
    from_club: int
    to_club: int
    interested_clubs: list[int]
    money: list[int]
    contracts: list[int]
    claim_span: int
    completed_span: int
    tentative_span: int
    negation_span: int
    official_span: int


@dataclass(frozen=True, slots=True)
class ClaimSpan:
    index: int
    source_text: str
    start_offset: int
    end_offset: int
    detector: str


@dataclass(frozen=True, slots=True)
class ProtocolInputs:
    subjects: tuple[Candidate, ...]
    clubs: tuple[Candidate, ...]
    money: tuple[Candidate, ...]
    contracts: tuple[Candidate, ...]
    claims: tuple[ClaimSpan, ...]
    completed: tuple[Candidate, ...]
    tentative: tuple[Candidate, ...]
    negation: tuple[Candidate, ...]
    official: tuple[Candidate, ...]


class FailureKind(str, Enum):
    JSON_PARSE = "json_parse_failure"
    JSON_SCHEMA = "json_schema_failure"
    SEMANTIC_CANDIDATE = "semantic_candidate_failure"


@dataclass(frozen=True, slots=True)
class AttemptTelemetry:
    attempt: int
    valid_json: bool
    valid_schema: bool
    valid_semantic: bool
    failure_kind: FailureKind | None
    validation_errors: tuple[str, ...]
    response_chars: int
    response_tokens_estimate: int


@dataclass(frozen=True, slots=True)
class ProtocolTelemetry:
    attempts: tuple[AttemptTelemetry, ...]
    repair_attempted: bool
    repair_kind: FailureKind | None
    repair_success: bool
    final_valid: bool
    prompt_characters: int
    prompt_tokens_estimate: int
    candidate_count: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class FlatProposalResult:
    proposal: FlatProposal
    semantic_proposal: SemanticProposal
    inputs: ProtocolInputs
    telemetry: ProtocolTelemetry


class ProtocolOutputError(RuntimeError):
    def __init__(self, telemetry: ProtocolTelemetry) -> None:
        super().__init__("BLOCKED_LLM_INVALID_OUTPUT")
        self.telemetry = telemetry


def repair_mojibake_display(text: str) -> str:
    output: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if token.isspace():
            output.append(token)
            continue
        try:
            repaired = token.encode("cp1251").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            output.append(token)
            continue
        output.append(repaired)
    return "".join(output)


def build_claim_spans(text: str) -> tuple[ClaimSpan, ...]:
    raw: list[tuple[int, int, str]] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.start()
        if end > start and text[start:end].strip():
            left = start + len(text[start:end]) - len(text[start:end].lstrip())
            right = end - (len(text[start:end]) - len(text[start:end].rstrip()))
            raw.append((left, right, "sentence"))
        start = match.end()
    if start < len(text) and text[start:].strip():
        left = start + len(text[start:]) - len(text[start:].lstrip())
        right = len(text) - (len(text[start:]) - len(text[start:].rstrip()))
        raw.append((left, right, "sentence"))

    for sentence_start, sentence_end, _ in list(raw):
        sentence = text[sentence_start:sentence_end]
        clause_start = 0
        for match in _CLAUSE_BREAK_RE.finditer(sentence):
            clause_end = match.start()
            part = sentence[clause_start:clause_end]
            if len(part.split()) >= 3:
                left = sentence_start + clause_start + len(part) - len(part.lstrip())
                right = sentence_start + clause_end - (len(part) - len(part.rstrip()))
                raw.append((left, right, "clause"))
            clause_start = match.end()
        part = sentence[clause_start:]
        if len(part.split()) >= 3:
            left = sentence_start + clause_start + len(part) - len(part.lstrip())
            right = sentence_end - (len(part) - len(part.rstrip()))
            if (left, right) != (sentence_start, sentence_end):
                raw.append((left, right, "clause"))

    unique: dict[tuple[int, int], str] = {}
    for left, right, detector in raw:
        if right > left:
            unique.setdefault((left, right), detector)
    ordered = sorted(unique.items(), key=lambda item: (item[0][0], item[0][1]))
    return tuple(
        ClaimSpan(
            index=index,
            source_text=text[left:right],
            start_offset=left,
            end_offset=right,
            detector=detector,
        )
        for index, ((left, right), detector) in enumerate(ordered)
    )


def build_protocol_inputs(
    bundle: CandidateBundle, original_text: str
) -> ProtocolInputs:
    candidates = bundle.candidates

    def unique(items: list[Candidate]) -> tuple[Candidate, ...]:
        output: list[Candidate] = []
        seen: set[tuple[Any, ...]] = set()
        for item in items:
            identity: tuple[Any, ...] = (
                item.candidate_type.value,
                (
                    "registry",
                    *sorted(item.registry_matches),
                )
                if item.registry_matches
                else ("surface", item.normalized_surface),
            )
            if identity in seen:
                continue
            seen.add(identity)
            output.append(item)
        return tuple(output)

    return ProtocolInputs(
        subjects=unique([
            item for item in candidates
            if item.candidate_type in {
                CandidateType.PERSON,
                CandidateType.CLUB,
                CandidateType.ORGANIZATION,
            }
        ]),
        clubs=unique([
            item for item in candidates if item.candidate_type is CandidateType.CLUB
        ]),
        money=unique([
            item for item in candidates if item.candidate_type is CandidateType.MONEY
        ]),
        contracts=unique([
            item for item in candidates
            if item.candidate_type is CandidateType.CONTRACT
        ]),
        claims=build_claim_spans(original_text),
        completed=unique([
            item for item in candidates
            if item.candidate_type is CandidateType.COMPLETED_MOVE
        ]),
        tentative=unique([
            item for item in candidates
            if item.candidate_type is CandidateType.TENTATIVE
        ]),
        negation=unique([
            item for item in candidates
            if item.candidate_type is CandidateType.NEGATION
        ]),
        official=unique([
            item for item in candidates
            if item.candidate_type is CandidateType.OFFICIAL
        ]),
    )


def _scalar_schema(size: int) -> dict[str, Any]:
    return {"type": "integer", "enum": [-1, *range(size)]}


def _array_schema(size: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "integer", "enum": list(range(size))},
        "uniqueItems": True,
        "maxItems": size,
    }


def build_dynamic_schema(inputs: ProtocolInputs) -> dict[str, Any]:
    properties = {
        "category": {"type": "integer", "enum": list(CATEGORY_BY_INDEX)},
        "subject": _scalar_schema(len(inputs.subjects)),
        "current_club": _scalar_schema(len(inputs.clubs)),
        "from_club": _scalar_schema(len(inputs.clubs)),
        "to_club": _scalar_schema(len(inputs.clubs)),
        "interested_clubs": _array_schema(len(inputs.clubs)),
        "money": _array_schema(len(inputs.money)),
        "contracts": _array_schema(len(inputs.contracts)),
        "claim_span": {
            "type": "integer",
            "enum": list(range(len(inputs.claims))) or [-1],
        },
        "completed_span": _scalar_schema(len(inputs.completed)),
        "tentative_span": _scalar_schema(len(inputs.tentative)),
        "negation_span": _scalar_schema(len(inputs.negation)),
        "official_span": _scalar_schema(len(inputs.official)),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _numbered(items: tuple[Any, ...]) -> list[list[Any]]:
    return [
        [
            index,
            (
                item.candidate_type.value
                if isinstance(item, Candidate)
                else item.detector
            ),
            repair_mojibake_display(
                item.surface if isinstance(item, Candidate) else item.source_text
            ),
        ]
        for index, item in enumerate(items)
    ]


def _candidate_payload(inputs: ProtocolInputs) -> dict[str, list[list[Any]]]:
    return {
        "subjects": _numbered(inputs.subjects),
        "clubs": _numbered(inputs.clubs),
        "money": _numbered(inputs.money),
        "contracts": _numbered(inputs.contracts),
        "claims": _numbered(inputs.claims),
        "completed": _numbered(inputs.completed),
        "tentative": _numbered(inputs.tentative),
        "negation": _numbered(inputs.negation),
        "official": _numbered(inputs.official),
    }


def build_protocol_prompt(
    metadata: SourceMetadata, inputs: ProtocolInputs
) -> str:
    candidates = _candidate_payload(inputs)
    return (
        "Constrained football-news selector. Select numeric indices only; never "
        "invent. Use -1 for absent scalar and [] for absent array. Categories: "
        "0 transfer/signing/move/transfer interest, 1 loan only when loan is "
        "explicit, 2 non-transfer club news, 3 a quote not primarily about a "
        "transfer, 4 performance statistics (not fees), 5 governance/admin, "
        "6 other, 7 unknown. For transfer/loan select the player or coach as "
        "subject when present. current_club/from_club is the club being left; "
        "to_club is the explicit destination. Do not infer to_club from interest; "
        "put interested teams in interested_clubs. from_club and to_club must "
        "differ. For non-transfer stories leave club relations absent unless "
        "essential. Select money/contracts only when they belong to the primary "
        "football deal; ignore administrative finance and unrelated figures. "
        "If both base and total fee are stated, select only the explicit overall "
        "total. Preserve relevant negation, tentative, completed and official "
        "signals. Choose the shortest claim span expressing the main event. "
        "Return JSON only.\n"
        f"role={metadata.source_role}\n"
        "text="
        f"{json.dumps(repair_mojibake_display(metadata.original_text), ensure_ascii=False)}\n"
        f"candidates={json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}"
    )


def _schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["root:wrong_type"]
    errors: list[str] = []
    required = set(schema["required"])
    properties = schema["properties"]
    missing = required - set(value)
    errors.extend(f"{field}:missing_required_field" for field in sorted(missing))
    extra = set(value) - set(properties)
    errors.extend(f"{field}:additional_property" for field in sorted(extra))
    for field in required & set(value):
        spec = properties[field]
        item = value[field]
        if spec["type"] == "integer":
            if type(item) is not int:
                errors.append(f"{field}:wrong_type")
            elif item not in spec["enum"]:
                errors.append(f"{field}:out_of_range")
        elif spec["type"] == "array":
            if not isinstance(item, list):
                errors.append(f"{field}:wrong_type")
                continue
            if len(item) > spec["maxItems"]:
                errors.append(f"{field}:too_many_items")
            integer_items = [selected for selected in item if type(selected) is int]
            if len(integer_items) != len(set(integer_items)):
                errors.append(f"{field}:duplicate_candidate")
            allowed = set(spec["items"]["enum"])
            for selected in item:
                if type(selected) is not int:
                    errors.append(f"{field}:item_wrong_type")
                elif selected not in allowed:
                    errors.append(f"{field}:out_of_range")
    return errors


def _semantic_errors(value: FlatProposal, inputs: ProtocolInputs) -> list[str]:
    errors: list[str] = []
    if value.claim_span < 0:
        errors.append("claim_span:required")
        return errors
    selected_subject = (
        inputs.subjects[value.subject] if value.subject >= 0 else None
    )
    if value.category in {0, 1}:
        people = [
            item
            for item in inputs.subjects
            if item.candidate_type is CandidateType.PERSON
        ]
        if people and (
            selected_subject is None
            or selected_subject.candidate_type is not CandidateType.PERSON
        ):
            errors.append("subject:transfer_requires_person_in_claim")
    elif (
        value.current_club >= 0
        or value.from_club >= 0
        or value.to_club >= 0
        or value.interested_clubs
        or value.money
        or value.contracts
    ):
        errors.append("non_transfer:deal_fields_must_be_absent")

    if inputs.negation and value.negation_span < 0:
        errors.append("negation_span:source_negation_must_be_selected")
    if value.from_club >= 0 and value.to_club >= 0:
        left = inputs.clubs[value.from_club]
        right = inputs.clubs[value.to_club]
        if (
            left.registry_matches
            and right.registry_matches
            and set(left.registry_matches) & set(right.registry_matches)
        ):
            errors.append("from_club,to_club:identical_resolved_club")
    return errors


def _validate_response(
    raw: str, schema: dict[str, Any], inputs: ProtocolInputs, attempt: int
) -> tuple[FlatProposal | None, AttemptTelemetry]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, AttemptTelemetry(
            attempt=attempt,
            valid_json=False,
            valid_schema=False,
            valid_semantic=False,
            failure_kind=FailureKind.JSON_PARSE,
            validation_errors=(f"json:{exc.msg}",),
            response_chars=len(raw),
            response_tokens_estimate=max(1, len(raw) // 4),
        )
    schema_errors = _schema_errors(value, schema)
    if schema_errors:
        return None, AttemptTelemetry(
            attempt=attempt,
            valid_json=True,
            valid_schema=False,
            valid_semantic=False,
            failure_kind=FailureKind.JSON_SCHEMA,
            validation_errors=tuple(schema_errors),
            response_chars=len(raw),
            response_tokens_estimate=max(1, len(raw) // 4),
        )
    try:
        proposal = FlatProposal.model_validate(value)
    except ValidationError as exc:
        return None, AttemptTelemetry(
            attempt=attempt,
            valid_json=True,
            valid_schema=False,
            valid_semantic=False,
            failure_kind=FailureKind.JSON_SCHEMA,
            validation_errors=tuple(
                f"{'.'.join(map(str, item['loc']))}:{item['type']}"
                for item in exc.errors()
            ),
            response_chars=len(raw),
            response_tokens_estimate=max(1, len(raw) // 4),
        )
    semantic_errors = _semantic_errors(proposal, inputs)
    if semantic_errors:
        return None, AttemptTelemetry(
            attempt=attempt,
            valid_json=True,
            valid_schema=True,
            valid_semantic=False,
            failure_kind=FailureKind.SEMANTIC_CANDIDATE,
            validation_errors=tuple(semantic_errors),
            response_chars=len(raw),
            response_tokens_estimate=max(1, len(raw) // 4),
        )
    return proposal, AttemptTelemetry(
        attempt=attempt,
        valid_json=True,
        valid_schema=True,
        valid_semantic=True,
        failure_kind=None,
        validation_errors=(),
        response_chars=len(raw),
        response_tokens_estimate=max(1, len(raw) // 4),
    )


def to_semantic_proposal(
    proposal: FlatProposal, inputs: ProtocolInputs
) -> SemanticProposal:
    def selected(items: tuple[Candidate, ...], index: int) -> str | None:
        return items[index].candidate_id if index >= 0 else None

    claim = inputs.claims[proposal.claim_span]
    return SemanticProposal(
        semantic_category=CATEGORY_BY_INDEX[proposal.category],
        primary_subject_candidate_id=selected(inputs.subjects, proposal.subject),
        relation_candidates=RelationCandidateSelection(
            current_club=selected(inputs.clubs, proposal.current_club),
            from_club=selected(inputs.clubs, proposal.from_club),
            to_club=selected(inputs.clubs, proposal.to_club),
            interested_clubs=[
                inputs.clubs[index].candidate_id
                for index in proposal.interested_clubs
            ],
        ),
        money_candidate_ids=[
            inputs.money[index].candidate_id for index in proposal.money
        ],
        contract_candidate_ids=[
            inputs.contracts[index].candidate_id for index in proposal.contracts
        ],
        primary_claim_quote=claim.source_text,
        signals=SemanticSignals(
            completed_move_explicit=proposal.completed_span >= 0,
            tentative=proposal.tentative_span >= 0,
            negated=proposal.negation_span >= 0,
            official_announcement_signal=proposal.official_span >= 0,
            confirmed_journalist_signal=False,
        ),
        supporting_quotes=[],
    )


class FlatProposalRunner:
    def __init__(
        self,
        *,
        client: JsonLlmClient,
        temperature: float = 0.0,
        seed: int | None = 42,
        timeout: int = 240,
    ) -> None:
        self.client = client
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout

    def run(
        self, *, metadata: SourceMetadata, candidates: CandidateBundle
    ) -> FlatProposalResult:
        inputs = build_protocol_inputs(candidates, metadata.original_text)
        schema = build_dynamic_schema(inputs)
        prompt = build_protocol_prompt(metadata, inputs)
        started = time.perf_counter()
        first_raw = self.client.generate(
            prompt=prompt,
            schema=schema,
            temperature=self.temperature,
            seed=self.seed,
            timeout=self.timeout,
        )
        proposal, first = _validate_response(first_raw, schema, inputs, 1)
        attempts = [first]
        repair_kind = first.failure_kind
        if proposal is None:
            repair_prompt = (
                "Correct the JSON. Return only JSON matching this schema and allowed "
                "numeric values. from_club and to_club must be different; use -1 "
                "when one is absent. For transfer/loan choose a person subject when "
                "available. Non-transfer categories must leave deal fields absent. "
                "Preserve source negation. Do not reinterpret facts.\n"
                f"errors={json.dumps(first.validation_errors, ensure_ascii=False)}\n"
                f"schema={json.dumps(schema, separators=(',', ':'))}\n"
                "text="
                f"{json.dumps(repair_mojibake_display(metadata.original_text), ensure_ascii=False)}\n"
                "candidates="
                f"{json.dumps(_candidate_payload(inputs), ensure_ascii=False, separators=(',', ':'))}\n"
                f"invalid={first_raw}"
            )
            repaired_raw = self.client.generate(
                prompt=repair_prompt,
                schema=schema,
                temperature=self.temperature,
                seed=self.seed,
                timeout=self.timeout,
            )
            proposal, repaired = _validate_response(repaired_raw, schema, inputs, 2)
            attempts.append(repaired)
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry = ProtocolTelemetry(
            attempts=tuple(attempts),
            repair_attempted=len(attempts) == 2,
            repair_kind=repair_kind if len(attempts) == 2 else None,
            repair_success=len(attempts) == 2 and proposal is not None,
            final_valid=proposal is not None,
            prompt_characters=len(prompt),
            prompt_tokens_estimate=max(1, len(prompt) // 4),
            candidate_count=sum(
                len(value)
                for value in (
                    inputs.subjects, inputs.clubs, inputs.money, inputs.contracts,
                    inputs.claims, inputs.completed, inputs.tentative,
                    inputs.negation, inputs.official,
                )
            ),
            latency_ms=latency_ms,
        )
        if proposal is None:
            raise ProtocolOutputError(telemetry)
        return FlatProposalResult(
            proposal=proposal,
            semantic_proposal=to_semantic_proposal(proposal, inputs),
            inputs=inputs,
            telemetry=telemetry,
        )
