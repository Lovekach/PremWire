from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
CAPITALIZED_RE = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}\b")
STOP = set(
    "и в во на но а я ты он она они мы вы это то что как не да нет ну же бы с со к у о "
    "об от до за из по для или если уже еще ещё там тут так только просто мне тебя его её "
    "их когда где кто чем чтобы потому тоже был была были есть будет надо можно вообще вот"
    .split()
)


def _tokens(text: str) -> list[str]:
    return [
        token.casefold()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.casefold() not in STOP
    ]


def _trigrams(text: str) -> set[str]:
    value = " " + re.sub(r"\s+", " ", text.casefold()).strip() + " "
    return {value[index : index + 3] for index in range(max(0, len(value) - 2))}


@dataclass(frozen=True, slots=True)
class DialoguePair:
    id: int
    incoming: str
    reply: str
    tokens: tuple[str, ...]
    trigrams: frozenset[str]


class CommunityStyleRetriever:
    def __init__(self, corpus_path: Path) -> None:
        pairs: list[DialoguePair] = []
        for line in corpus_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            incoming = str(row.get("incoming", "")).strip()
            reply = str(row.get("reply", "")).strip()
            if not incoming or not reply:
                continue
            pairs.append(
                DialoguePair(
                    id=int(row.get("id", len(pairs) + 1)),
                    incoming=incoming,
                    reply=reply,
                    tokens=tuple(_tokens(incoming)),
                    trigrams=frozenset(_trigrams(incoming)),
                )
            )
        self.pairs = tuple(pairs)
        document_frequency: Counter[str] = Counter()
        for pair in self.pairs:
            document_frequency.update(set(pair.tokens))
        total = max(1, len(self.pairs))
        self.idf = {
            token: math.log((total + 1) / (count + 1)) + 1
            for token, count in document_frequency.items()
        }

    def search(self, query: str, *, limit: int = 6) -> list[DialoguePair]:
        query_tokens = _tokens(query)
        query_counts = Counter(query_tokens)
        query_trigrams = _trigrams(query)
        query_names = {name.casefold() for name in CAPITALIZED_RE.findall(query)}
        scored: list[tuple[float, DialoguePair]] = []
        for pair in self.pairs:
            pair_counts = Counter(pair.tokens)
            token_score = sum(
                min(count, pair_counts.get(token, 0)) * self.idf.get(token, 1.0)
                for token, count in query_counts.items()
            )
            union = len(query_trigrams | pair.trigrams)
            trigram_score = (
                len(query_trigrams & pair.trigrams) / union if union else 0.0
            )
            if token_score <= 0 and trigram_score < 0.04:
                continue
            reply_names = {
                name.casefold() for name in CAPITALIZED_RE.findall(pair.reply)
            }
            foreign_names = reply_names - query_names
            name_penalty = 2.5 * len(foreign_names)
            question_bonus = 0.3 if ("?" in query) == ("?" in pair.incoming) else 0.0
            score = token_score + trigram_score * 4 + question_bonus - name_penalty
            if score > 0:
                scored.append((score, pair))
        scored.sort(key=lambda value: (-value[0], value[1].id))
        selected: list[DialoguePair] = []
        seen_replies: set[str] = set()
        for _, pair in scored:
            normalized = re.sub(r"\W+", " ", pair.reply.casefold()).strip()
            if not normalized or normalized in seen_replies:
                continue
            selected.append(pair)
            seen_replies.add(normalized)
            if len(selected) >= limit:
                break
        return selected
