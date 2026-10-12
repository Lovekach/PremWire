from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .simple_entity_hygiene import likely_same_proper_name


ALLOWED_SEMANTIC_VIOLATIONS = frozenset(
    {
        "UNSUPPORTED_FACT",
        "ENTITY_SUBSTITUTION",
        "RELATION_REVERSAL",
        "ATTRIBUTION_CHANGE",
        "WINNER_LOSER_CHANGE",
        "CLUB_MEMBERSHIP_INFERENCE",
        "COMPETITION_INFERENCE",
        "TRANSFER_CERTAINTY",
        "NUMERIC_CHANGE",
    }
)


@dataclass(frozen=True, slots=True)
class SemanticViolation:
    code: str
    source_evidence: str
    output_evidence: str
    reason: str

    @property
    def detail(self) -> str:
        return (
            f"SOURCE[{self.source_evidence}] OUTPUT[{self.output_evidence}]: "
            f"{self.reason}"
        )


@dataclass(frozen=True, slots=True)
class SemanticVerification:
    safe: bool
    valid: bool
    violations: tuple[SemanticViolation, ...]
    invalid_items: int = 0
    error: str | None = None
    suppressed_items: int = 0


def _fold_evidence(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _evidence_exists(evidence: str, authority: str) -> bool:
    folded = _fold_evidence(evidence)
    return bool(folded) and len(folded) <= 180 and folded in _fold_evidence(authority)


def _known_harmless_equivalence(
    code: str,
    source_evidence: str,
    output_evidence: str,
) -> bool:
    source = _fold_evidence(source_evidence)
    output = _fold_evidence(output_evidence)
    if code == "COMPETITION_INFERENCE":
        last_16 = "last-16" in source or "round of 16" in source
        if last_16 and re.search(r"\b1\s*/\s*8\b", output):
            return True
    if code == "ENTITY_SUBSTITUTION":
        return likely_same_proper_name(source_evidence, output_evidence)
    return False


def parse_semantic_verification(
    payload: Any,
    *,
    source_text: str,
    output_text: str,
) -> SemanticVerification:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return SemanticVerification(False, False, (), error="INVALID_JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("safe"), bool):
        return SemanticVerification(False, False, (), error="INVALID_SHAPE")
    raw_violations = payload.get("violations")
    if not isinstance(raw_violations, list):
        return SemanticVerification(False, False, (), error="INVALID_VIOLATIONS")

    valid_items: list[SemanticViolation] = []
    invalid_items = 0
    suppressed_items = 0
    for raw in raw_violations:
        if not isinstance(raw, dict):
            invalid_items += 1
            continue
        code = str(raw.get("type", ""))
        source_evidence = str(raw.get("source_evidence", "")).strip()
        output_evidence = str(raw.get("output_evidence", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        if (
            code not in ALLOWED_SEMANTIC_VIOLATIONS
            or not reason
            or not _evidence_exists(source_evidence, source_text)
            or not _evidence_exists(output_evidence, output_text)
        ):
            invalid_items += 1
            continue
        if _known_harmless_equivalence(code, source_evidence, output_evidence):
            suppressed_items += 1
            continue
        valid_items.append(
            SemanticViolation(code, source_evidence, output_evidence, reason[:300])
        )

    declared_safe = bool(payload["safe"])
    if declared_safe:
        if raw_violations:
            return SemanticVerification(
                False,
                False,
                tuple(valid_items),
                invalid_items,
                "SAFE_WITH_VIOLATIONS",
            )
        return SemanticVerification(
            True,
            True,
            (),
            invalid_items=invalid_items,
            suppressed_items=suppressed_items,
        )

    if not valid_items:
        if suppressed_items:
            return SemanticVerification(
                True,
                True,
                (),
                invalid_items=invalid_items,
                suppressed_items=suppressed_items,
            )
        return SemanticVerification(
            False,
            False,
            (),
            invalid_items,
            "UNANCHORED_UNSAFE_VERDICT",
            suppressed_items,
        )
    return SemanticVerification(
        False,
        True,
        tuple(valid_items),
        invalid_items,
        suppressed_items=suppressed_items,
    )


SEMANTIC_VERIFIER_SYSTEM_PROMPT = """Compare SOURCE with OUTPUT. SOURCE is the only authority. Do not rewrite, correct SOURCE, use football knowledge or infer facts.

Check concrete meaning in this order:
1. Compare every subject -> action -> object/recipient relation and attribution.
2. Require SOURCE support for every OUTPUT number, age, nationality, person/club identity, player role, club membership, league/competition label, winner, transfer stage and causal explanation.
3. A new concrete modifier is unsupported even when it sounds plausible.

Do not penalize omission, style, Russian grammar or an unambiguous spelling/transliteration variant that still identifies the same entity. Treat last-16, round of 16 and 1/8 final as equivalent. Penalize a genuinely different/corrupted entity, reversed relation, wrong attribution/winner/team, stronger transfer certainty, unsupported context/causality and numeric change.

Every violation must quote one short exact substring from SOURCE and one from OUTPUT. Never invent evidence. Return at most three highest-severity violations with a one-sentence reason. Return safe=true only after all three checks pass. Return only schema-valid JSON."""


SEMANTIC_VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "safe": {"type": "boolean"},
        "violations": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": sorted(ALLOWED_SEMANTIC_VIOLATIONS)},
                    "source_evidence": {"type": "string", "maxLength": 160},
                    "output_evidence": {"type": "string", "maxLength": 160},
                    "reason": {"type": "string", "maxLength": 180},
                },
                "required": ["type", "source_evidence", "output_evidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["safe", "violations"],
    "additionalProperties": False,
}


def build_semantic_verifier_user_prompt(source_text: str, output_text: str) -> str:
    return (
        "SOURCE:\n"
        f"{source_text}\n\n"
        "OUTPUT:\n"
        f"{output_text}\n\n"
        "Check semantic correspondence only. Copy evidence exactly from the blocks."
    )


__all__ = [
    "ALLOWED_SEMANTIC_VIOLATIONS",
    "SEMANTIC_VERIFIER_SCHEMA",
    "SEMANTIC_VERIFIER_SYSTEM_PROMPT",
    "SemanticVerification",
    "SemanticViolation",
    "build_semantic_verifier_user_prompt",
    "parse_semantic_verification",
]
