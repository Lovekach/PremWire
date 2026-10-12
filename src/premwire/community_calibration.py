from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .community_simulator import evaluate_reply
from .community_retrieval import CommunityStyleRetriever
from .config import EditorConfig
from .text_editor import EditorError, OllamaTextEditor


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    json_output: Path
    markdown_output: Path
    total: int
    machine_accepted: int


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Ожидался JSON-объект: {path}")
    return value


def _chat_json(
    editor: OllamaTextEditor,
    prompt: str,
    schema: dict[str, Any],
    *,
    temperature: float,
) -> dict[str, Any]:
    payload = editor._request(
        "POST",
        "/api/chat",
        {
            "model": editor.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "format": schema,
            "options": {"temperature": temperature, "num_predict": 900},
        },
    )
    try:
        content = str(payload["message"]["content"])
        result = json.loads(content)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EditorError("Ollama вернула некорректный пакет калибровки") from exc
    if not isinstance(result, dict):
        raise EditorError("Пакет калибровки не является JSON-объектом")
    return result


def _generation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "reply": {"type": ["string", "null"]},
                    },
                    "required": ["id", "reply"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "accepted": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "accepted", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _case_text(case: dict[str, Any]) -> str:
    context = "\n".join(str(value) for value in case.get("context", []))
    mode = "ответ обязателен" if case.get("should_reply", True) else "лучше промолчать"
    return (
        f"КЕЙС {case['id']} ({case.get('topic', '')}; {mode})\n"
        f"{context}\n{case.get('target', '')}"
    ).strip()


def _message_body(value: object) -> str:
    text = str(value).strip()
    return text.split(":", 1)[1].strip() if ":" in text else text


def _generation_prompt(
    profile: dict[str, Any],
    batch: list[dict[str, Any]],
    retrieved: dict[int, str] | None = None,
) -> str:
    rules = "\n".join(f"- {value}" for value in profile.get("rules", []))
    retrieved = retrieved or {}
    cases = "\n\n".join(
        _case_text(case)
        + (
            "\nПОХОЖИЕ РЕАЛЬНЫЕ ДИАЛОГИ (только ориентир по манере, не копировать факты):\n"
            + retrieved[int(case["id"])]
            if retrieved.get(int(case["id"]))
            else ""
        )
        for case in batch
    )
    return f"""
Ты готовишь короткие ответы отдельного админ-аккаунта @PremWireAdm в ЛОКАЛЬНОМ тесте.
Для каждого кейса верни одну разговорную реплику или null, если лучше промолчать.
Не выдумывай фамилии, суммы, клубы, личный опыт и знакомства.
Не рекламируй паблик, не давай ссылок, не оскорбляй людей.
Нормальная грамматика важнее попытки вставить сленг.
Не копируй целиком ни одну реплику из контекста.

ПРАВИЛА:
{rules}

    КЕЙСЫ:
{cases}
""".strip()


def _review_prompt(batch: list[dict[str, Any]], replies: dict[int, str | None]) -> str:
    rows: list[str] = []
    for case in batch:
        reply = replies.get(int(case["id"]))
        rows.append(_case_text(case) + f"\nОТВЕТ: {reply!r}")
    return """
Ты строгий редактор ответов в русском футбольном чате.
Для каждого кейса поставь accepted=true только если ответ естественный, грамматически нормальный,
относится к последней реплике, не выдумывает факты и не содержит рекламы или оскорбления.
Если в кейсе лучше промолчать, null является правильным ответом.
Если ответ плохой, кратко напиши причину по-русски.

""".strip() + "\n\n" + "\n\n".join(rows)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Калибровка @PremWireAdm",
        "",
        "Отметь для каждого кейса один вариант и сообщи мне номера.",
        "",
    ]
    for row in payload["results"]:
        lines.extend(
            [
                f"## {row['id']}. {row['topic']}",
                "",
                "**Контекст**",
                "",
                *[f"> {value}" for value in row["context"]],
                f"> {row['target']}",
                "",
                "**Ответ @PremWireAdm**",
                "",
                f"> {row['reply'] if row['reply'] is not None else '[промолчать]'}",
                "",
                f"Машинная проверка: **{'прошёл' if row['machine_accepted'] else 'не прошёл'}**. {row['machine_reason']}",
                "",
                "- [ ] Нравится",
                "- [ ] Переделать",
                "- [ ] Лучше промолчать",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def run_calibration(
    editor_config: EditorConfig,
    profile_path: Path,
    cases_path: Path,
    json_output: Path,
    markdown_output: Path,
    *,
    batch_size: int = 6,
) -> CalibrationResult:
    profile = _load(profile_path)
    cases_payload = _load(cases_path)
    cases = cases_payload.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("community calibration cases должен быть массивом")
    model = str(profile.get("model", "")).strip()
    if model:
        editor_config = replace(editor_config, model=model)
    editor = OllamaTextEditor(editor_config)
    retriever: CommunityStyleRetriever | None = None
    corpus_value = str(profile.get("retrieval_corpus", "")).strip()
    if corpus_value:
        corpus_path = Path(corpus_value)
        if not corpus_path.is_absolute():
            corpus_path = profile_path.parents[1] / corpus_path
        if corpus_path.exists():
            retriever = CommunityStyleRetriever(corpus_path)
    results: list[dict[str, Any]] = []

    for start in range(0, len(cases), batch_size):
        batch = [case for case in cases[start : start + batch_size] if isinstance(case, dict)]
        retrieved: dict[int, str] = {}
        if retriever is not None:
            for case in batch:
                query = " ".join(
                    [str(value) for value in case.get("context", [])]
                    + [str(case.get("target", ""))]
                )
                pairs = retriever.search(query, limit=3)
                retrieved[int(case["id"])] = "\n".join(
                    f"Собеседник: {pair.incoming}\nFortytwo: {pair.reply}"
                    for pair in pairs
                )
        generated = _chat_json(
            editor,
            _generation_prompt(profile, batch, retrieved),
            _generation_schema(),
            temperature=0.5,
        )
        replies = {
            int(item["id"]): item.get("reply")
            for item in generated.get("items", [])
            if isinstance(item, dict) and "id" in item
        }
        for case in batch:
            case_id = int(case["id"])
            if case.get("fixed_reply") is not None:
                replies[case_id] = str(case["fixed_reply"])
            elif not bool(case.get("should_reply", True)):
                replies[case_id] = None
        reviewed = _chat_json(
            editor,
            _review_prompt(batch, replies),
            _review_schema(),
            temperature=0.1,
        )
        reviews = {
            int(item["id"]): item
            for item in reviewed.get("items", [])
            if isinstance(item, dict) and "id" in item
        }
        for case in batch:
            case_id = int(case["id"])
            reply_value = replies.get(case_id)
            reply = None if reply_value is None else str(reply_value).strip() or None
            context_text = " ".join(str(value) for value in case.get("context", []))
            context_text += " " + str(case.get("target", ""))
            previous = [
                _message_body(value) for value in case.get("context", [])
            ] + [_message_body(case.get("target", ""))]
            heuristic_issues = (
                () if reply is None else evaluate_reply(reply, previous, profile, context_text)
            )
            review = reviews.get(case_id, {})
            review_accepted = bool(review.get("accepted"))
            if not bool(case.get("should_reply", True)) and reply is None:
                review_accepted = True
                review = {"reason": "Правильное молчание в нежелательной ситуации"}
            elif case.get("fixed_reply") is not None:
                review_accepted = True
                review = {"reason": "Фиксированный безопасный ответ"}
            elif reply is None:
                review_accepted = False
                review = {"reason": "Модель выбрала молчание, хотя кейс рассчитан на ответ"}
            machine_accepted = not heuristic_issues and review_accepted
            reason_parts = list(heuristic_issues)
            if review.get("reason"):
                reason_parts.append(str(review["reason"]))
            results.append(
                {
                    "id": case_id,
                    "topic": str(case.get("topic", "")),
                    "context": [str(value) for value in case.get("context", [])],
                    "target": str(case.get("target", "")),
                    "should_reply": bool(case.get("should_reply", True)),
                    "reply": reply,
                    "machine_accepted": machine_accepted,
                    "machine_reason": "; ".join(reason_parts) or "OK",
                }
            )

    payload = {
        "simulation_only": True,
        "profile": profile.get("display_name", "PremWire Admin"),
        "telegram_username": profile.get("telegram_username", "@PremWireAdm"),
        "case_set": cases_payload.get("name", cases_path.stem),
        "results": results,
    }
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_output.write_text(_markdown(payload), encoding="utf-8")
    return CalibrationResult(
        json_output,
        markdown_output,
        len(results),
        sum(bool(row["machine_accepted"]) for row in results),
    )
