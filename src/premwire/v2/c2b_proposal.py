from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .c2b_evidence import EvidenceBuilder
from .c2b_models import CandidateBundle, CandidateType, SemanticProposal, SourceMetadata
from .orchestration import JsonLlmClient, LlmOutputError


PROPOSAL_SCHEMA_VERSION = "c2-semantic-proposal-1.0"
EXTRACTION_PROMPT_VERSION = "stage-c2-2.0"


@dataclass(frozen=True, slots=True)
class CompactProposalResult:
    proposal: SemanticProposal
    repaired: bool
    first_attempt_valid: bool
    latency_ms: int
    raw_response: str


def _candidate_payload(bundle: CandidateBundle) -> list[dict[str, Any]]:
    return [
        {
            "id": item.candidate_id,
            "type": item.candidate_type.value,
            "surface": item.surface,
            "registry_matches": item.registry_matches,
            "type_hints": item.type_hints,
        }
        for item in bundle.candidates
    ]


def build_compact_prompt(
    metadata: SourceMetadata, candidates: CandidateBundle
) -> str:
    source_summary = {
        "platform": metadata.platform,
        "canonical_source_name": metadata.canonical_source_name,
        "source_role": metadata.source_role,
        "trust_tier": metadata.trust_tier,
    }
    return (
        "You are a constrained semantic selector for football news. "
        "Return JSON only, matching the supplied schema. Select only candidate IDs "
        "from the list. Never invent names or facts. Quotes must be exact substrings "
        "of original_text and must not be wrapped in decorative quotation marks unless "
        "those marks are present in original_text. For transfer/loan, select the named "
        "player or coach as primary_subject_candidate_id whenever a person candidate "
        "exists. Use null/[] when the text does not support a selection. "
        "Select every money or contract candidate that explicitly belongs to the "
        "primary football deal; ignore administrative finance and unrelated numbers. "
        "Do not infer a destination from interest, and preserve local negation. "
        "semantic_category must describe the source event.\n"
        f"prompt_version={EXTRACTION_PROMPT_VERSION}\n"
        f"source_summary={json.dumps(source_summary, ensure_ascii=False)}\n"
        f"original_text={json.dumps(metadata.original_text, ensure_ascii=False)}\n"
        f"candidates={json.dumps(_candidate_payload(candidates), ensure_ascii=False)}"
    )


class CompactProposalRunner:
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
    ) -> CompactProposalResult:
        prompt = build_compact_prompt(metadata, candidates)
        schema = SemanticProposal.model_json_schema()
        started = time.perf_counter()
        raw = self.client.generate(
            prompt=prompt,
            schema=schema,
            temperature=self.temperature,
            seed=self.seed,
            timeout=self.timeout,
        )
        try:
            proposal = self._parse_and_validate(
                raw, candidates, metadata.original_text
            )
            return CompactProposalResult(
                proposal=proposal,
                repaired=False,
                first_attempt_valid=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                raw_response=raw,
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            repair = (
                "Return only corrected JSON matching the schema. Select only IDs "
                "present in candidates and keep quotes verbatim. Do not add facts.\n"
                f"original_text={json.dumps(metadata.original_text, ensure_ascii=False)}\n"
                f"candidates={json.dumps(_candidate_payload(candidates), ensure_ascii=False)}\n"
                f"invalid_response={raw}"
            )
            repaired_raw = self.client.generate(
                prompt=repair,
                schema=schema,
                temperature=self.temperature,
                seed=self.seed,
                timeout=self.timeout,
            )
            try:
                proposal = self._parse_and_validate(
                    repaired_raw, candidates, metadata.original_text
                )
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                raise LlmOutputError("BLOCKED_LLM_INVALID_OUTPUT") from exc
            return CompactProposalResult(
                proposal=proposal,
                repaired=True,
                first_attempt_valid=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                raw_response=repaired_raw,
            )

    @staticmethod
    def _parse_and_validate(
        raw: str, candidates: CandidateBundle, original_text: str
    ) -> SemanticProposal:
        if raw.lstrip().startswith("```"):
            raise ValueError("markdown fences are forbidden")
        proposal = SemanticProposal.model_validate(json.loads(raw))
        by_id = candidates.by_id()

        def require(candidate_id: str | None, allowed: set[CandidateType]) -> None:
            if candidate_id is None:
                return
            candidate = by_id.get(candidate_id)
            if candidate is None:
                raise ValueError(f"unknown candidate id: {candidate_id}")
            if candidate.candidate_type not in allowed:
                raise ValueError(f"candidate type mismatch: {candidate_id}")

        require(
            proposal.primary_subject_candidate_id,
            {CandidateType.PERSON, CandidateType.CLUB, CandidateType.ORGANIZATION},
        )
        relations = proposal.relation_candidates
        for candidate_id in (
            relations.current_club,
            relations.from_club,
            relations.to_club,
            *relations.interested_clubs,
        ):
            require(candidate_id, {CandidateType.CLUB})
        for candidate_id in proposal.money_candidate_ids:
            require(candidate_id, {CandidateType.MONEY})
        for candidate_id in proposal.contract_candidate_ids:
            require(candidate_id, {CandidateType.CONTRACT})
        EvidenceBuilder().build_quote(
            field="primary_claim",
            quote=proposal.primary_claim_quote,
            original_text=original_text,
            source_event_id=candidates.source_event_id,
        )
        for quote in proposal.supporting_quotes:
            EvidenceBuilder().build_quote(
                field="supporting_quote",
                quote=quote,
                original_text=original_text,
                source_event_id=candidates.source_event_id,
            )
        return proposal
