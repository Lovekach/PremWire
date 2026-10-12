from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .config import EditorConfig
from .community_retrieval import CommunityStyleRetriever, DialoguePair
from .text_editor import EditorError, OllamaTextEditor


@dataclass(frozen=True, slots=True)
class SimulationResult:
    output: Path
    generated: int
    accepted: int


_LINK = re.compile(r"(?:https?://|t\.me/|www\.)\S+", re.I)
_PROMO = re.compile(r"\b(?:подпиш|заходи|переходи|наш канал|у нас в канале)\w*", re.I)
_PERSONAL_ATTACK = re.compile(
    r"\b(?:идиот|дебил|тупой|клоун|ничтожество|заткнись|дурак)\w*", re.I
)
_AI_CLICHE = re.compile(
    r"\b(?:интересный вопрос|на мой взгляд|стоит отметить|следует отметить|"
    r"давайте разбер[её]мся|важно понимать|безусловно|подводя итог)\b",
    re.I,
)
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF]")
_CAPITALIZED = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}\b")
_BROKEN_PHRASE = re.compile(
    r"\b(?:посему|затем кто|легче будет точно не быть|цена может быть куском)\b",
    re.I,
)


def evaluate_reply(
    text: str,
    previous: list[str],
    profile: dict | None = None,
    context_text: str = "",
) -> tuple[str, ...]:
    issues: list[str] = []
    value = text.strip()
    profile = profile or {}
    max_reply_chars = int(profile.get("max_reply_chars", 280))
    if not value:
        issues.append("empty")
    if len(value) > max_reply_chars:
        issues.append("too_long")
    if _LINK.search(value):
        issues.append("link")
    if _PROMO.search(value):
        issues.append("promotion")
    if _PERSONAL_ATTACK.search(value):
        issues.append("personal_attack")
    if _AI_CLICHE.search(value):
        issues.append("ai_cliche")
    if _BROKEN_PHRASE.search(value):
        issues.append("broken_phrase")
    if "—" in value:
        issues.append("long_dash")
    if len(_EMOJI.findall(value)) > int(profile.get("max_emoji", 1)):
        issues.append("too_many_emoji")
    allowed_names = {
        token.casefold()
        for token in _CAPITALIZED.findall(
            context_text
            + " "
            + str(profile.get("display_name", ""))
            + " "
            + str(profile.get("telegram_username", ""))
        )
    }
    allowed_names.update({"premwire"})
    for token in _CAPITALIZED.findall(value):
        lowered = token.casefold()
        if not any(
            lowered == allowed
            or (len(lowered) >= 5 and len(allowed) >= 5 and lowered[:5] == allowed[:5])
            for allowed in allowed_names
        ):
            issues.append("new_named_entity")
            break
    if value.count("?") > 2:
        issues.append("too_many_questions")
    normalized = re.sub(r"\W+", " ", value.casefold()).strip()
    if normalized and any(normalized == re.sub(r"\W+", " ", old.casefold()).strip() for old in previous):
        issues.append("exact_repeat")
    return tuple(issues)


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект: {path}")
    return data


def _prompt(
    profile: dict,
    context: list[dict],
    event: dict,
    retrieved_samples: list[DialoguePair] | None = None,
) -> str:
    samples = [str(value).strip() for value in profile.get("samples", []) if str(value).strip()]
    bad = [str(value).strip() for value in profile.get("negative_samples", []) if str(value).strip()]
    rules = [str(value).strip() for value in profile.get("rules", []) if str(value).strip()]
    transcript = "\n".join(
        f"{row.get('author', 'Участник')}: {row.get('text', '')}" for row in context[-12:]
    )
    dialogue_examples = "\n\n".join(
        f"Собеседник: {pair.incoming}\nFortytwo: {pair.reply}"
        for pair in (retrieved_samples or [])
    )
    general_examples = (
        "\n".join(samples[:20])
        if not dialogue_examples
        else "Не используются: найдены более подходящие реальные диалоги."
    )
    account_role = str(
        profile.get("account_role", "отдельный аккаунт администратора PremWire")
    ).strip()
    disclosure = str(
        profile.get(
            "disclosure",
            "Если прямо спрашивают об автоматизации, не врать и не выдумывать личность.",
        )
    ).strip()
    return f"""
Ты пишешь как {account_role} в ЛОКАЛЬНОЙ СИМУЛЯЦИИ футбольного чата.
Ничего не рекламируй, не давай ссылок, не призывай подписываться.
Не переходи на личности, не изображай личную дружбу и не выдумывай факты.
{disclosure}
Не упоминай автоматизацию сам, если тебя об этом не спрашивали.
Ответ должен быть естественным, уместным и обычно занимать одну короткую реплику.
Можно не соглашаться, шутить без оскорблений или задать один содержательный вопрос.
Не пытайся отвечать на каждое сообщение и не превращай реплику в мини-пост.
Сленг допустим, но грамматика и смысл должны оставаться нормальными.
Не называй футболиста, клуб, сумму или событие, которых нет в последних сообщениях.
Если данных мало, не выдумывай конкретику: коротко обозначь сомнение или задай вопрос.
Перед ответом молча проверь, что реплика прямо отвечает на последнее сообщение.

ПРАВИЛА ЛИЧНОСТИ:
- На вопрос «ты админ PremWire?» отвечай, что это админский аккаунт PremWire.
- На прямой вопрос об автоматизации отвечай: «часть ответов готовится с помощью автоматизации, но аккаунт админский».
- Не называй себя ботом без прямого вопроса и не повторяй технический ответ в других темах.
- На вопрос о личном присутствии, знакомстве или опыте отвечай только по имеющимся данным; ничего не придумывай.

ПРАВИЛА СТИЛЯ:
{chr(10).join('- ' + value for value in rules) or '- разговорный русский без канцелярита'}

ПРИМЕРЫ СТИЛЯ:
{general_examples or 'Примеры пока не загружены.'}

ПОХОЖИЕ РЕАЛЬНЫЕ ДИАЛОГИ FORTYTWO:
{dialogue_examples or 'Подходящих примеров не найдено.'}
Используй их только как ориентир по длине, ритму и реакции. Не копируй ответ дословно
и не переноси в новый разговор имена или факты из примера.

ТАК НЕ ПИСАТЬ:
{chr(10).join(bad[:30]) or 'Негативные примеры пока не загружены.'}

ПОСЛЕДНИЕ СООБЩЕНИЯ:
{transcript}

СООБЩЕНИЕ ДЛЯ РЕАКЦИИ:
{event.get('author', 'Участник')}: {event.get('text', '')}

Верни только одну готовую реплику без имени автора, кавычек и пояснений.
""".strip()


def _identity_reply(event: dict) -> str | None:
    text = str(event.get("text", "")).casefold()
    if any(word in text for word in ("автоматизац", "бот", "сам это пишешь")):
        return "часть ответов готовится с помощью автоматизации, но аккаунт админский)"
    if "админ" in text and "premwire" in text:
        return "да, это админский аккаунт PremWire)"
    if any(phrase in text for phrase in ("лично был", "лично была", "сам был на матче")):
        return "нет, на матче не был)"
    return None


def _quality_review(
    editor: OllamaTextEditor,
    context: list[dict],
    event: dict,
    reply: str,
) -> str:
    transcript = "\n".join(
        f"{row.get('author', 'Участник')}: {row.get('text', '')}" for row in context[-10:]
    )
    prompt = f"""
Ты строгий редактор коротких реплик в русском футбольном чате.

Ответь только OK, если предложенная реплика одновременно:
- грамматически и логически нормальная;
- прямо относится к последнему сообщению;
- не содержит выдуманных имён, сумм и фактов;
- не звучит как ИИ, канцелярит или бессмысленный набор сленга;
- не содержит грубого оскорбления игрока или собеседника;
- не повторяет уже сказанное без новой мысли.

Если хотя бы одно условие нарушено, ответь в формате BAD: короткая причина.

КОНТЕКСТ:
{transcript}

ПОСЛЕДНЕЕ СООБЩЕНИЕ:
{event.get('author', 'Участник')}: {event.get('text', '')}

ПРОВЕРЯЕМАЯ РЕПЛИКА:
{reply}
""".strip()
    return editor._generate(prompt, temperature=0.1).strip()


def run_simulation(
    editor_config: EditorConfig,
    profile_path: Path,
    scenario_path: Path,
    output_path: Path,
) -> SimulationResult:
    profile = _load(profile_path)
    scenario = _load(scenario_path)
    events = scenario.get("events", [])
    if not isinstance(events, list):
        raise ValueError("scenario.events должен быть массивом")
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
    context: list[dict] = []
    replies: list[str] = []
    results: list[dict] = []
    last_reply_minute = -10_000
    cooldown = int(scenario.get("cooldown_minutes", 10))
    for event in events:
        if not isinstance(event, dict):
            continue
        context.append(event)
        minute = int(event.get("minute", 0))
        should_consider = bool(event.get("direct_reply") or event.get("trigger"))
        if not should_consider or minute - last_reply_minute < cooldown:
            continue
        reply = ""
        issues: tuple[str, ...] = ()
        attempts: list[dict] = []
        retrieved_samples: list[DialoguePair] = []
        if retriever is not None:
            retrieval_query = " ".join(
                str(row.get("text", "")) for row in context[-6:]
            )
            retrieved_samples = retriever.search(retrieval_query, limit=6)
        for attempt in range(3):
            try:
                fixed_reply = _identity_reply(event)
                if fixed_reply is not None:
                    reply = fixed_reply
                    issues = evaluate_reply(
                        reply,
                        replies,
                        profile,
                        " ".join(str(row.get("text", "")) for row in context),
                    )
                    attempts.append({"reply": reply, "issues": list(issues)})
                    break
                attempt_prompt = _prompt(
                    profile, context, event, retrieved_samples
                )
                if attempts:
                    attempt_prompt += (
                        "\n\nПРЕДЫДУЩИЙ ВАРИАНТ ОТКЛОНЁН:\n"
                        + str(attempts[-1]["reply"])
                        + "\nПРИЧИНЫ: "
                        + ", ".join(attempts[-1]["issues"])
                        + "\nНапиши новый вариант, исправив причины отказа."
                    )
                reply = editor._generate(attempt_prompt, temperature=0.55).strip()
            except EditorError as exc:
                issues = (str(exc),)
                break
            issues = evaluate_reply(
                reply,
                replies,
                profile,
                " ".join(str(row.get("text", "")) for row in context),
            )
            review = ""
            if not issues:
                try:
                    review = _quality_review(editor, context, event, reply)
                except EditorError as exc:
                    issues = (f"quality_review_error:{exc}",)
                else:
                    if review.casefold() != "ok":
                        issues = (f"quality_review:{review[:180]}",)
            attempts.append(
                {"reply": reply, "issues": list(issues), "quality_review": review}
            )
            if not issues:
                break
        accepted = not issues
        results.append(
            {
                "event": event,
                "reply": reply,
                "accepted": accepted,
                "issues": issues,
                "attempts": attempts,
            }
        )
        if accepted:
            replies.append(reply)
            context.append({"minute": minute, "author": profile.get("display_name", "PremWire"), "text": reply})
            last_reply_minute = minute
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "simulation_only": True,
                "profile": profile.get("display_name", "PremWire"),
                "scenario": scenario.get("name", scenario_path.stem),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return SimulationResult(output_path, len(results), sum(bool(row.get("accepted")) for row in results))
