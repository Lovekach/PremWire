"""Stage D.6 trusted-source grounded editorial shadow pipeline.

The source publication is the factual boundary.  The frozen factual subsystem
is deliberately represented only by an advisory status on the input.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from premwire.model_gateway import ModelFailure, ModelGateway, ModelRequest, ModelSuccess, ModelTaskType
from premwire.model_runtime import ModelTaskProfile

from .c4a_entities import StrategyBEntityRegistry
from .resolution import SourceRecord, SourceRegistry


EDITORIAL_PROMPT_VERSION = "stage-d6-source-grounded-v1"
EDITORIAL_VARIANTS = (
    "NEWS_BRIEF",
    "HEADLINE_FACTS",
    "BREAKING_STYLE",
    "CONTEXT_SHORT",
    "AUDIENCE_QUESTION",
)
CATEGORIES = (
    "transfer", "contract", "injury", "match_result", "fixture", "lineup",
    "coach", "quote_interview", "club_news", "player_news", "statistics",
    "general_football",
)
_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "body", "source_facts_used", "source_attribution_preserved"],
    "properties": {
        "headline": {"type": "string", "minLength": 1},
        "body": {"type": "string", "minLength": 1},
        "source_facts_used": {"type": "array", "items": {"type": "string"}},
        "source_attribution_preserved": {"type": "boolean"},
    },
}

_FOOTBALL = re.compile(
    r"\b(футбол|игрок|клуб|трансфер|аренд|контракт|матч|гол|тренер|состав|"
    r"football|player|club|transfer|loan|contract|match|goal|coach|lineup)\w*\b",
    re.I,
)
_CATEGORY_RULES = (
    ("injury", re.compile(r"\b(травм|поврежден|диагноз|injur)\w*\b", re.I)),
    ("match_result", re.compile(r"\b(сч[её]т|победил|проиграл|ничья|final score|won|lost|drew)\b", re.I)),
    ("fixture", re.compile(r"\b(сыграет|встретится|матч состоится|kick.?off|will play)\b", re.I)),
    ("lineup", re.compile(r"\b(состав|line.?up|starting xi)\b", re.I)),
    ("coach", re.compile(r"\b(тренер|coach|manager)\w*\b", re.I)),
    ("quote_interview", re.compile(r"\b(заявил|сказал|интервью|цитат|said|interview)\w*\b", re.I)),
    ("contract", re.compile(r"\b(контракт|соглашени|contract|deal)\w*\b", re.I)),
    ("statistics", re.compile(r"\b(статистик|рекорд|appearances|goals|assists|record)\w*\b", re.I)),
    ("transfer", re.compile(r"\b(трансфер|переход|переш[её]л|переходит|аренд|новый игрок|интерес|переговор|transfer|join|loan|interest|talks|rumou?r)\w*\b", re.I)),
)
_COMPLETED = re.compile(r"\b(переш[её]л|стал\w* игрок|новый игрок|подписал\w*|joined|signed|completed)\w*\b", re.I)
_SPECULATIVE = re.compile(r"\b(интерес|может|возможн|слух|переговор|could|may|interest|rumou?r|talks)\w*\b", re.I)
_OFFICIAL = re.compile(r"\b(официальн|official|announced)\w*\b", re.I)
_LOAN = re.compile(r"\b(аренд|loan)\w*\b", re.I)
_PERMANENT = re.compile(r"\b(постоянн\w* переход|permanent signing|permanent transfer)\b", re.I)
_CONTRACT = re.compile(r"\b(контракт|contract)\w*\b", re.I)
_UNSUPPORTED_CONTEXT = re.compile(r"\b(росси\w*|russia\w*|турецк\w*|turkish)\b", re.I)
_NEGATION = re.compile(r"(?<!\w)(?:не|нет|никогда|not|no|never|denied)(?!\w)", re.I)
_ATTRIBUTION = re.compile(r"\b(по (?:информации|данным)|сообщает|сообщил|according to|reports?|reported by)\b", re.I)
_QUOTE_SPEECH = re.compile(r"(?:[«\"“].{2,}[»\"”][^\w]{0,6}(?:сказал|заявил|said)|(?:сказал|заявил|said)\s*[:—]\s*[«\"“])", re.I | re.S)
_NUMBER = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?(?:/\d+)?%?(?!\d)")
_CURRENCY = re.compile(r"(?:€|£|\$|\b(?:eur|gbp|usd|евро|фунт\w*|доллар\w*)\b)", re.I)
_SCORE = re.compile(r"(?<!\d)\d{1,2}\s*[:\-]\s*\d{1,2}(?!\d)")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:/\d{2})?(?!\d)")
_FROM = re.compile(r"(?:из|from)\s+[«\"“]?([A-Za-zА-ЯЁа-яё][\w'’\-]*)", re.I)
_TO = re.compile(r"(?:в|to)\s+[«\"“]?([A-Za-zА-ЯЁа-яё][\w'’\-]*)", re.I)


@dataclass(frozen=True, slots=True)
class TrustedEditorialSource:
    source_id: str
    source_name: str
    enabled: bool
    trusted_for_editorial: bool
    registry_record: SourceRecord | None


class TrustedSourcePolicy:
    """Joins stable ingestion IDs to the one existing source registry."""

    def __init__(self, project_root: Path) -> None:
        configured = json.loads((project_root / "config" / "sources.json").read_text(encoding="utf-8"))
        registry = SourceRegistry.load(project_root / "config" / "source_registry.json")
        self._sources: dict[str, TrustedEditorialSource] = {}
        for item in configured["sources"]:
            record = registry.resolve(str(item["platform"]), str(item["handle"]))
            enabled = bool(item["enabled"]) and record is not None and record.enabled
            self._sources[str(item["id"])] = TrustedEditorialSource(
                source_id=str(item["id"]),
                source_name=str(item["name"]),
                enabled=enabled,
                trusted_for_editorial=bool(record and record.trusted_for_editorial),
                registry_record=record,
            )

    def resolve(self, source_id: str) -> TrustedEditorialSource | None:
        return self._sources.get(source_id)


@dataclass(frozen=True, slots=True)
class GroundedEditorialConfig:
    shadow_enabled: bool = True
    auto_publish: bool = False

    def __post_init__(self) -> None:
        if self.auto_publish:
            raise ValueError("Stage D.6 never permits auto_publish")


@dataclass(frozen=True, slots=True)
class GroundedStoryInput:
    story_id: str
    source_id: str
    source_text: str
    duplicate: bool = False
    factual_enrichment_status: Literal["ROUTED", "BLOCKED", "NOT_APPLICABLE", "ERROR"] = "NOT_APPLICABLE"
    factual_error: str | None = None


@dataclass(frozen=True, slots=True)
class GroundedEditorialResult:
    story_id: str
    decision: Literal["PUBLISH", "SKIP"]
    category: str
    headline: str | None
    body: str | None
    source_facts_used: tuple[str, ...]
    editorial_variant: str
    source_attribution_preserved: bool
    validator_status: Literal["PASS", "BLOCK", "NOT_RUN"]
    validator_errors: tuple[str, ...]
    fallback_used: bool
    editorial_attempted: bool
    editorial_backend: str
    editorial_status: Literal["ready", "not_ready"]
    model_latency_ms: float = 0.0


def classify_source_text(source_text: str) -> str | None:
    if not source_text.strip() or not _FOOTBALL.search(source_text):
        return None
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(source_text):
            return category
    return "general_football"


def select_editorial_variant(story_id: str, category: str) -> str:
    digest = hashlib.sha256(f"{story_id}|{category}".encode("utf-8")).digest()
    return EDITORIAL_VARIANTS[int.from_bytes(digest[:2], "big") % len(EDITORIAL_VARIANTS)]


def build_source_grounded_prompt(source_text: str, category: str, variant: str) -> str:
    return (
        "You are the PremWire football editor. Write a new PremWire post from SOURCE_TEXT. "
        "Use only information present in SOURCE_TEXT. You may rewrite structure, tone, headline and wording. "
        "Do not invent facts. Do not increase certainty. Do not invent player, club, amount, date, source, quote or status. "
        "If information is missing, omit it. Produce Russian output. Make a structural rewrite, not sentence-by-sentence "
        "synonym replacement; compression and changed fact order are allowed. Preserve material attribution and negation. "
        "Return only the requested JSON. source_facts_used is a concise audit list, not an inferred knowledge graph.\n"
        f"CATEGORY: {category}\nEDITORIAL_VARIANT: {variant}\nSOURCE_TEXT:\n{source_text}"
    )


def _unbalanced_quotes(text: str) -> bool:
    return text.count("«") != text.count("»") or text.count("“") != text.count("”") or text.count('"') % 2 != 0


def _normalized_numbers(text: str) -> set[str]:
    return {value.replace(",", ".") for value in _NUMBER.findall(text)}


def _registry_entity_errors(source: str, generated: str, registry: StrategyBEntityRegistry) -> list[str]:
    errors: list[str] = []
    for entity_type, code in (("player", "UNSUPPORTED_PERSON_ENTITY"), ("club", "UNSUPPORTED_CLUB_ENTITY")):
        source_ids = {hit.entity_id for hit in registry.scan(source, entity_type)}
        output_ids = {hit.entity_id for hit in registry.scan(generated, entity_type)}
        if output_ids - source_ids:
            errors.append(code)
    return errors


def _direction_errors(source: str, generated: str) -> list[str]:
    source_from, source_to = _FROM.findall(source), _TO.findall(source)
    output_from, output_to = _FROM.findall(generated), _TO.findall(generated)
    errors: list[str] = []
    if output_from and not source_from:
        errors.append("UNSUPPORTED_TRANSFER_DIRECTION")
    if source_from and source_to and output_from and output_to:
        sf, st = source_from[0].casefold(), source_to[0].casefold()
        of, ot = output_from[0].casefold(), output_to[0].casefold()
        if (of, ot) == (st, sf) or of != sf or ot != st:
            errors.append("UNSUPPORTED_TRANSFER_DIRECTION")
    return errors


def validate_source_grounded_output(
    source_text: str,
    headline: str,
    body: str,
    registry: StrategyBEntityRegistry,
    *,
    source_attribution_preserved: bool,
) -> list[str]:
    """Additive D.6 layer: D.2 entity semantics plus source-bound facts."""
    generated = f"{headline}\n{body}"
    errors = _registry_entity_errors(source_text, generated, registry)
    source_numbers, output_numbers = _normalized_numbers(source_text), _normalized_numbers(generated)
    extra_numbers = output_numbers - source_numbers
    if extra_numbers:
        errors.append("UNSUPPORTED_NUMBER")
        if _CURRENCY.search(generated):
            errors.append("UNSUPPORTED_AMOUNT")
        if any(value.startswith(("19", "20")) and len(value.split("/")[0]) == 4 for value in extra_numbers):
            errors.append("UNSUPPORTED_DATE")
    source_currencies = {value.casefold() for value in _CURRENCY.findall(source_text)}
    output_currencies = {value.casefold() for value in _CURRENCY.findall(generated)}
    if output_currencies - source_currencies:
        errors.append("UNSUPPORTED_CURRENCY")
    if _SCORE.search(generated) and _SCORE.findall(generated) != _SCORE.findall(source_text):
        errors.append("UNSUPPORTED_SCORE")
    if _YEAR.search(generated) and not set(_YEAR.findall(generated)).issubset(set(_YEAR.findall(source_text))):
        errors.append("UNSUPPORTED_DATE")

    source_speculative = bool(_SPECULATIVE.search(source_text))
    source_completed = bool(_COMPLETED.search(source_text)) and not source_speculative
    output_completed = bool(_COMPLETED.search(generated))
    output_speculative = bool(_SPECULATIVE.search(generated))
    if source_speculative and output_completed:
        errors.append("STATUS_ESCALATION")
    if source_completed and output_speculative:
        errors.append("STATUS_DOWNGRADE")
    if _OFFICIAL.search(generated) and not _OFFICIAL.search(source_text):
        errors.extend(("UNSUPPORTED_OFFICIAL_CLAIM", "STATUS_ESCALATION"))
    if _CONTRACT.search(generated) and not _CONTRACT.search(source_text):
        errors.append("UNSUPPORTED_CONTRACT_CLAIM")
    if _UNSUPPORTED_CONTEXT.search(generated) and not _UNSUPPORTED_CONTEXT.search(source_text):
        errors.append("UNSUPPORTED_CONTEXT_ENTITY")
    if _LOAN.search(source_text) and (_PERMANENT.search(generated) or (output_completed and not _LOAN.search(generated))):
        errors.append("STATUS_ESCALATION")
    if _NEGATION.search(source_text) and not _NEGATION.search(generated):
        errors.append("CONTRADICTORY_NEGATION")
    if _QUOTE_SPEECH.search(generated) and not _QUOTE_SPEECH.search(source_text):
        errors.append("UNSUPPORTED_QUOTE")
    if _ATTRIBUTION.search(source_text) and (not source_attribution_preserved or not _ATTRIBUTION.search(generated)):
        errors.append("UNSUPPORTED_ATTRIBUTION")
    errors.extend(_direction_errors(source_text, generated))
    if _unbalanced_quotes(generated):
        errors.append("UNBALANCED_QUOTES")
    if len(headline) > 180 or len(body) > 900:
        errors.append("EDITORIAL_LENGTH_EXCEEDED")
    return list(dict.fromkeys(errors))


def build_source_grounded_fallback(source_text: str) -> tuple[str, str, tuple[str, ...]] | None:
    """Conservative patterns only; no generic extractive copy fallback."""
    interest = re.search(
        r"(?P<club>[«\"“]?[A-ZА-ЯЁ][^.!?\n]{1,50}?)\s+(?:интересуется|проявляет интерес к)\s+(?P<player>[A-ZА-ЯЁ][\w'’\-]+(?:\s+[A-ZА-ЯЁ][\w'’\-]+)?)",
        source_text,
        re.I,
    )
    if interest:
        club, player = interest.group("club").strip(), interest.group("player").strip()
        return f"{club} интересуется {player}", f"{club} проявляет интерес к {player}.", (club, player, "interest")
    talks = re.search(
        r"(?P<player>[A-ZА-ЯЁ][\w'’\-]+)\s+переходит\s+в\s+(?P<club>[«\"“][^»\"”]+[»\"”])",
        source_text,
        re.I,
    )
    if talks and _SPECULATIVE.search(source_text):
        player, club = talks.group("player"), talks.group("club")
        return (
            f"{player} и {club}: переговоры продолжаются",
            "В публикации говорится о возможном переходе.",
            (player, club, "ongoing transfer talks"),
        )
    completed = re.search(
        r"(?P<player>[A-ZА-ЯЁ][\w'’\-]+(?:\s+[A-ZА-ЯЁ][\w'’\-]+)?)\s*[—–-]\s*новый игрок\s+(?P<club>[«\"“][^»\"”]+[»\"”])",
        source_text,
        re.I,
    )
    if completed:
        player, club = completed.group("player"), completed.group("club")
        if _LOAN.search(source_text):
            return f"{player} стал игроком {club}", "Аренда рассчитана на сезон.", (player, club, "loan")
        return f"{player} стал игроком {club}", "Переход оформлен.", (player, club, "completed")
    return None


class TrustedSourceGroundedPipeline:
    def __init__(
        self,
        *,
        project_root: Path,
        config: GroundedEditorialConfig,
        policy: TrustedSourcePolicy,
        gateway: ModelGateway,
        task_profile: ModelTaskProfile,
        backend_name: str = "mock",
    ) -> None:
        self.project_root = project_root
        self.config = config
        self.policy = policy
        self.gateway = gateway
        self.task_profile = task_profile
        self.backend_name = backend_name
        self.registry = StrategyBEntityRegistry(project_root)

    def _skip(self, story: GroundedStoryInput, error: str, category: str = "general_football") -> GroundedEditorialResult:
        return GroundedEditorialResult(
            story.story_id, "SKIP", category, None, None, (), select_editorial_variant(story.story_id, category),
            False, "NOT_RUN", (error,), False, False, "none", "not_ready",
        )

    def process(self, story: GroundedStoryInput) -> GroundedEditorialResult:
        if not self.config.shadow_enabled:
            return self._skip(story, "SHADOW_DISABLED")
        source = self.policy.resolve(story.source_id)
        if source is None or not source.enabled:
            return self._skip(story, "SOURCE_DISABLED_OR_UNKNOWN")
        if not source.trusted_for_editorial:
            return self._skip(story, "SOURCE_UNTRUSTED")
        if story.duplicate:
            return self._skip(story, "DUPLICATE")
        category = classify_source_text(story.source_text)
        if category is None:
            return self._skip(story, "NON_FOOTBALL_OR_EMPTY")
        variant = select_editorial_variant(story.story_id, category)
        contract = {
            "story_id": story.story_id,
            "source_id": story.source_id,
            "source_name": source.source_name,
            "source_text": story.source_text,
            "category": category,
            "editorial_variant": variant,
            "factual_enrichment_status": story.factual_enrichment_status,
        }
        request = ModelRequest.create(
            task_type=ModelTaskType.COPY_GENERATION,
            prompt=build_source_grounded_prompt(story.source_text, category, variant),
            schema=_OUTPUT_SCHEMA,
            factual_contract=contract,
            task_profile=self.task_profile,
        )
        started = perf_counter()
        response = self.gateway.generate_copy(request)
        latency = (perf_counter() - started) * 1000
        headline: str | None = None
        body: str | None = None
        facts: tuple[str, ...] = ()
        attribution = False
        errors: tuple[str, ...]
        if isinstance(response, ModelSuccess):
            payload = response.payload
            required = {"headline", "body", "source_facts_used", "source_attribution_preserved"}
            if set(payload) == required and isinstance(payload["headline"], str) and isinstance(payload["body"], str) and isinstance(payload["source_facts_used"], (list, tuple)) and all(isinstance(value, str) for value in payload["source_facts_used"]) and isinstance(payload["source_attribution_preserved"], bool):
                headline, body = payload["headline"].strip(), payload["body"].strip()
                facts = tuple(payload["source_facts_used"])
                attribution = payload["source_attribution_preserved"]
                errors = tuple(validate_source_grounded_output(story.source_text, headline, body, self.registry, source_attribution_preserved=attribution)) if headline and body else ("MODEL_OUTPUT_SCHEMA_INVALID",)
            else:
                errors = ("MODEL_OUTPUT_SCHEMA_INVALID",)
        else:
            errors = (f"MODEL_{response.code.value.upper()}",) if isinstance(response, ModelFailure) else ("MODEL_ERROR",)

        fallback_used = False
        backend = self.backend_name
        if errors:
            fallback = build_source_grounded_fallback(story.source_text)
            if fallback is None:
                return GroundedEditorialResult(
                    story.story_id, "SKIP", category, None, None, (), variant, False, "BLOCK", errors,
                    False, True, backend, "not_ready", model_latency_ms=getattr(response, "latency_ms", latency),
                )
            headline, body, facts = fallback
            attribution = not bool(_ATTRIBUTION.search(story.source_text))
            fallback_errors = tuple(validate_source_grounded_output(story.source_text, headline, body, self.registry, source_attribution_preserved=attribution))
            if fallback_errors:
                return GroundedEditorialResult(
                    story.story_id, "SKIP", category, None, None, (), variant, False, "BLOCK", fallback_errors,
                    True, True, backend, "not_ready", model_latency_ms=getattr(response, "latency_ms", latency),
                )
            errors = ()
            fallback_used = True
            backend = "deterministic_fallback"

        return GroundedEditorialResult(
            story.story_id, "PUBLISH", category, headline, body, facts, variant, attribution, "PASS", (),
            fallback_used, True, backend, "ready", model_latency_ms=getattr(response, "latency_ms", latency),
        )


__all__ = [
    "CATEGORIES", "EDITORIAL_PROMPT_VERSION", "EDITORIAL_VARIANTS",
    "GroundedEditorialConfig", "GroundedEditorialResult", "GroundedStoryInput", "TrustedEditorialSource",
    "TrustedSourceGroundedPipeline", "TrustedSourcePolicy", "build_source_grounded_fallback",
    "build_source_grounded_prompt", "classify_source_text", "select_editorial_variant",
    "validate_source_grounded_output",
]
