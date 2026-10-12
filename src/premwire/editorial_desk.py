from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Sequence

from .simple_source_risk import SourceRisk, analyze_simple_source_risks
from .editorial_safety import transient_ttl_minutes
from .storage import same_story_text, same_transfer_subject


class StoryCategory(StrEnum):
    MATCH_RESULT = "MATCH_RESULT"
    MATCH_NEWS = "MATCH_NEWS"
    TRANSFER_OFFICIAL = "TRANSFER_OFFICIAL"
    TRANSFER_UPDATE = "TRANSFER_UPDATE"
    INJURY = "INJURY"
    TEAM_NEWS = "TEAM_NEWS"
    MANAGER_NEWS = "MANAGER_NEWS"
    QUOTE = "QUOTE"
    CONTRACT = "CONTRACT"
    TOURNAMENT_NEWS = "TOURNAMENT_NEWS"
    GENERAL_FOOTBALL = "GENERAL_FOOTBALL"


class EditorialMode(StrEnum):
    NORMAL = "NORMAL"
    SAFE_SUMMARY = "SAFE_SUMMARY"


@dataclass(frozen=True, slots=True)
class EditorialDecision:
    category: StoryCategory
    importance: int
    interest: int
    novelty: int
    reason: str
    mode: EditorialMode


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    item_id: int
    decision: EditorialDecision
    priority: float
    freshness_bonus: float
    diversity_adjustment: float
    repetition_penalty: float


_SCORE_RE = re.compile(r"(?<!\d)\d{1,2}\s*[:\-–—]\s*\d{1,2}(?!\d)")
_RESULT_RE = re.compile(
    r"\b(?:beat|defeated?|won|win|victory|draw|lost|loss|full\s*time|"
    r"победил\w*|обыграл\w*|выиграл\w*|поражени\w*|проиграл\w*|ничь\w*|"
    r"грохнул\w*|разгромил\w*|одолел\w*|"
    r"финальн\w*\s+свисток|матч\s+заверш\w*)\b",
    re.I,
)
_MATCH_EVENT_RE = re.compile(
    r"\b(?:match|game|goal|scored?|brace|hat[ -]?trick|assist|red\s+card|sent\s+off|"
    r"penalty|матч|игр(?:а|ы|е|у|ой|ах|ами)\b|гол\w*|забил\w*|дубл\w*|"
    r"хет[ -]?трик|ассист\w*|"
    r"удален\w*|удалён\w*|красн\w*\s+карточк\w*|пенальти)\b",
    re.I,
)
_OFFICIAL_TRANSFER_RE = re.compile(
    r"\b(?:official(?:ly)?|have\s+signed|has\s+signed|signed|joins?|completed\s+the\s+"
    r"(?:move|transfer)|официальн\w*|подписал\w*|переш[её]л\w*|стал\w*\s+игрок\w*)\b",
    re.I,
)
_TRANSFER_RE = re.compile(
    r"\b(?:transfer|sign(?:ing)?|join|loan|deal|bid|offer|talks?|negotiat\w*|interest\w*|"
    r"трансфер\w*|переход\w*|подпис\w*|аренд\w*|сделк\w*|продаж\w*|покупк\w*|"
    r"предложени\w*|предлага\w*|предложил\w*|"
    r"переговор\w*|интерес\w*)\b",
    re.I,
)
_INJURY_RE = re.compile(
    r"\b(?:injur\w*|ruled\s+out|fitness\s+doubt|doubtful|setback|повреждени\w*|"
    r"травм\w*|выбыл\w*|пропустит\w*|не\s+сыграет)\b",
    re.I,
)
_TEAM_RE = re.compile(
    r"\b(?:line[ -]?up|starting\s+xi|squad|team\s+news|roster|стартов\w*\s+состав\w*|"
    r"заявк\w*|состав\w*\s+команд\w*)\b",
    re.I,
)
_MANAGER_RE = re.compile(
    r"\b(?:manager|head\s+coach|coach|sacked|dismissed|appointed|тренер\w*|"
    r"главн\w*\s+тренер\w*|уволен\w*|назначен\w*|возглавил\w*)\b",
    re.I,
)
_QUOTE_RE = re.compile(
    r"(?:[\"«][^\"»]{15,}[\"»]|\b(?:said|says|told|заявил\w*|сказал\w*|"
    r"рассказал\w*|прокомментировал\w*)\b)",
    re.I,
)
_CONTRACT_RE = re.compile(
    r"\b(?:contract|extension|new\s+deal|renew\w*|контракт\w*|продлил\w*|"
    r"продление\w*|нов\w*\s+соглашени\w*)\b",
    re.I,
)
_TOURNAMENT_RE = re.compile(
    r"\b(?:champions\s+league|europa\s+league|world\s+cup|draw|knockout|"
    r"round\s+of\s+\d+|qualif\w*|final|semi[ -]?final|лига\s+чемпион\w*|"
    r"чемпионат\w*\s+мира|жеребьевк\w*|жеребьёвк\w*|плей[ -]?офф|"
    r"вышел\w*\s+в\s+финал|прошел\w*\s+в\s+следующ\w*\s+раунд)\b",
    re.I,
)
_HEADLINE_SHORTHAND_RE = re.compile(
    r"(?:[«\"]?[^.!?\n]{2,60}[»\"]?\s+(?:и|and)\s+[«\"]?[^.!?\n]{2,60}[»\"]?\s*[—–-]\s*"
    r"(?:вс[её]|over|done)\b)",
    re.I,
)

_DEFAULT_SCORES: dict[StoryCategory, tuple[int, int, int]] = {
    StoryCategory.MATCH_RESULT: (78, 80, 62),
    StoryCategory.MATCH_NEWS: (67, 73, 60),
    StoryCategory.TRANSFER_OFFICIAL: (62, 66, 55),
    StoryCategory.TRANSFER_UPDATE: (48, 53, 48),
    StoryCategory.INJURY: (68, 70, 60),
    StoryCategory.TEAM_NEWS: (55, 58, 50),
    StoryCategory.MANAGER_NEWS: (65, 68, 58),
    StoryCategory.QUOTE: (55, 65, 58),
    StoryCategory.CONTRACT: (58, 60, 52),
    StoryCategory.TOURNAMENT_NEWS: (70, 72, 62),
    StoryCategory.GENERAL_FOOTBALL: (45, 50, 52),
}


def same_editorial_story(first: str, second: str) -> bool:
    if same_story_text(first, second):
        return True
    transfer_categories = {
        StoryCategory.TRANSFER_OFFICIAL,
        StoryCategory.TRANSFER_UPDATE,
    }
    return (
        classify_story(first) in transfer_categories
        and classify_story(second) in transfer_categories
        and same_transfer_subject(first, second)
    )


def classify_story(source_text: str) -> StoryCategory:
    if _SCORE_RE.search(source_text) and _RESULT_RE.search(source_text):
        return StoryCategory.MATCH_RESULT
    if _RESULT_RE.search(source_text) and _MATCH_EVENT_RE.search(source_text):
        return StoryCategory.MATCH_RESULT
    if _OFFICIAL_TRANSFER_RE.search(source_text) and _TRANSFER_RE.search(source_text):
        return StoryCategory.TRANSFER_OFFICIAL
    # Career statistics often contain the word "матч". A concrete bid,
    # negotiation or move remains transfer news unless the source also states
    # an actual result, which is handled above.
    if _TRANSFER_RE.search(source_text):
        return StoryCategory.TRANSFER_UPDATE
    if _MATCH_EVENT_RE.search(source_text):
        return StoryCategory.MATCH_NEWS
    if _INJURY_RE.search(source_text):
        return StoryCategory.INJURY
    if _MANAGER_RE.search(source_text):
        return StoryCategory.MANAGER_NEWS
    if _QUOTE_RE.search(source_text) and not _TRANSFER_RE.search(source_text):
        return StoryCategory.QUOTE
    if _CONTRACT_RE.search(source_text):
        return StoryCategory.CONTRACT
    if _TEAM_RE.search(source_text):
        return StoryCategory.TEAM_NEWS
    if _TOURNAMENT_RE.search(source_text):
        return StoryCategory.TOURNAMENT_NEWS
    return StoryCategory.GENERAL_FOOTBALL


def recommended_mode(source_text: str) -> EditorialMode:
    risks = analyze_simple_source_risks(source_text)
    relation_count = len(
        risks.intersection({SourceRisk.MULTI_ENTITY, SourceRisk.MULTI_CLUB})
    )
    if (
        SourceRisk.DENSE_NUMERIC in risks
        or len(source_text) > 550
        or _HEADLINE_SHORTHAND_RE.search(source_text)
        or (SourceRisk.SCORE_RESULT in risks and relation_count > 0)
    ):
        return EditorialMode.SAFE_SUMMARY
    return EditorialMode.NORMAL


def fallback_decision(source_text: str) -> EditorialDecision:
    category = classify_story(source_text)
    importance, interest, novelty = _DEFAULT_SCORES[category]
    return EditorialDecision(
        category=category,
        importance=importance,
        interest=interest,
        novelty=novelty,
        reason=f"Deterministic {category.value.lower()} fallback",
        mode=recommended_mode(source_text),
    )


def validated_decision(
    source_text: str,
    payload: Mapping[str, object] | None,
) -> EditorialDecision:
    fallback = fallback_decision(source_text)
    if not payload:
        return fallback
    try:
        qwen_category = StoryCategory(str(payload.get("category", fallback.category.value)))
    except ValueError:
        qwen_category = fallback.category
    category = (
        qwen_category
        if fallback.category is StoryCategory.GENERAL_FOOTBALL
        else fallback.category
    )

    def raw_score(name: str, default: int) -> int:
        try:
            return max(0, min(100, int(payload.get(name, default))))
        except (TypeError, ValueError):
            return default

    scores = {
        "importance": raw_score("importance", fallback.importance),
        "interest": raw_score("interest", fallback.interest),
        "novelty": raw_score("novelty", fallback.novelty),
    }
    if max(scores.values()) <= 10:
        scores = {name: value * 10 for name, value in scores.items()}

    reason = re.sub(r"\s+", " ", str(payload.get("reason", fallback.reason))).strip()
    reason = re.sub(r"[\u3400-\u9fff]+", "", reason).strip(" ,.;:-")
    return EditorialDecision(
        category=category,
        importance=scores["importance"],
        interest=scores["interest"],
        novelty=scores["novelty"],
        reason=reason[:160] or fallback.reason,
        mode=fallback.mode,
    )


def decision_from_row(row: Mapping[str, object]) -> EditorialDecision:
    source_text = str(row["clean_source_text"] or row["source_text"] or "")
    payload = None
    if row["editorial_category"]:
        payload = {
            "category": row["editorial_category"],
            "importance": row["editorial_importance"],
            "interest": row["editorial_interest"],
            "novelty": row["editorial_novelty"],
            "reason": row["editorial_reason"],
            "recommended_mode": row["editorial_mode"],
        }
    return validated_decision(source_text, payload)


def diversity_adjustment(
    category: StoryCategory,
    recent_categories: Sequence[StoryCategory],
) -> float:
    if not recent_categories:
        return 0.0
    streak = 0
    for previous in recent_categories:
        if previous is category:
            streak += 1
        else:
            break
    if streak >= 3:
        return -4.0
    if streak == 2:
        return -2.0
    leading = recent_categories[0]
    leading_streak = 0
    for previous in recent_categories:
        if previous is leading:
            leading_streak += 1
        else:
            break
    if leading_streak >= 3 and category is not leading:
        return 7.0
    if leading_streak == 2 and category is not leading:
        return 4.0
    if recent_categories[:5].count(category) >= 3:
        return -2.0
    return 0.0


def rank_candidates(
    rows: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    freshness_minutes: int,
    recent_categories: Sequence[StoryCategory] = (),
    recent_story_texts: Sequence[str] = (),
) -> tuple[RankedCandidate, ...]:
    current = now.astimezone(timezone.utc)
    ranked: list[RankedCandidate] = []
    for row in rows:
        decision = decision_from_row(row)
        published_at = datetime.fromisoformat(
            str(row["source_published_at"] or row["discovered_at"])
        ).astimezone(timezone.utc)
        age_minutes = max(0.0, (current - published_at).total_seconds() / 60)
        transient_ttl = transient_ttl_minutes(str(row["source_text"] or ""))
        if transient_ttl is not None and age_minutes > transient_ttl:
            continue
        freshness = max(0.0, 8.0 * (1.0 - age_minutes / freshness_minutes))
        diversity = diversity_adjustment(decision.category, recent_categories)
        repetition = -30.0 if any(
            same_editorial_story(str(row["source_text"] or ""), previous)
            for previous in recent_story_texts
        ) else 0.0
        priority = (
            decision.importance * 0.60
            + decision.interest * 0.25
            + decision.novelty * 0.15
            + freshness
            + diversity
            + repetition
        )
        ranked.append(
            RankedCandidate(
                item_id=int(row["id"]),
                decision=decision,
                priority=round(priority, 2),
                freshness_bonus=round(freshness, 2),
                diversity_adjustment=diversity,
                repetition_penalty=repetition,
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda item: (-item.priority, -item.decision.importance, item.item_id),
        )
    )


__all__ = [
    "EditorialDecision",
    "EditorialMode",
    "RankedCandidate",
    "StoryCategory",
    "classify_story",
    "decision_from_row",
    "diversity_adjustment",
    "fallback_decision",
    "rank_candidates",
    "recommended_mode",
    "same_editorial_story",
    "validated_decision",
]
