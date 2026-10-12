from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def normalize_entity_name(value: str) -> str:
    """Normalize Cyrillic and Latin names for whole-alias matching."""
    decomposed = unicodedata.normalize("NFKD", value.casefold().replace("ё", "е"))
    characters = (
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join("".join(character if character.isalnum() else " " for character in characters).split())


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item


@dataclass(frozen=True, slots=True)
class EmblemEntry:
    id: str
    name: str
    output: Path
    aliases: tuple[str, ...]
    entity_type: str = "club"
    verification_status: str = "verified"

    @property
    def ready(self) -> bool:
        # Entity resolution is shared factual functionality. Physical crest
        # files are not required by the text pipeline.
        return self.verification_status == "verified"


class EmblemRegistry:
    def __init__(self, entries: Iterable[EmblemEntry]) -> None:
        self.entries = tuple(entries)
        ids = [entry.id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Emblem IDs must be unique")

        self._by_id = {entry.id: entry for entry in self.entries}
        self._by_alias: dict[str, EmblemEntry] = {}
        ambiguous_aliases: set[str] = set()
        for entry in self.entries:
            if entry.verification_status != "verified":
                continue
            for alias in entry.aliases:
                normalized = normalize_entity_name(alias)
                if len(normalized) < 3:
                    continue
                if normalized in ambiguous_aliases:
                    continue
                existing = self._by_alias.get(normalized)
                if existing is not None and existing.id != entry.id:
                    self._by_alias.pop(normalized, None)
                    ambiguous_aliases.add(normalized)
                    continue
                self._by_alias[normalized] = entry

    @classmethod
    def load(cls, project_root: Path, registry_path: Path | None = None) -> "EmblemRegistry":
        paths = (
            [registry_path]
            if registry_path is not None
            else [
                project_root / "config" / "club-emblems.json",
                project_root / "config" / "national-emblems.json",
            ]
        )
        raw_entries: list[dict[str, Any]] = []
        for path in paths:
            if path is None or not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            for key in ("clubs", "entities", "national_teams"):
                value = payload.get(key)
                if isinstance(value, list):
                    raw_entries.extend(item for item in value if isinstance(item, dict))

        entries: list[EmblemEntry] = []
        for raw in raw_entries:
            entry_id = str(raw["id"])
            name = str(raw.get("name") or raw.get("name_ru") or entry_id)
            aliases = {entry_id, entry_id.replace("-", " "), name}
            for key in ("name_en", "short_name", "aliases", "aliases_ru", "aliases_en"):
                aliases.update(_strings(raw.get(key)))
            entries.append(
                EmblemEntry(
                    id=entry_id,
                    name=name,
                    output=project_root / str(raw["output"]),
                    aliases=tuple(sorted(aliases, key=str.casefold)),
                    entity_type=str(raw.get("entity_type", "club")),
                    verification_status=str(raw.get("verification_status", "verified")),
                )
            )
        return cls(entries)

    def by_id(self, entry_id: str) -> EmblemEntry:
        try:
            return self._by_id[entry_id]
        except KeyError as exc:
            raise KeyError(f"Unknown emblem ID: {entry_id}") from exc

    def resolve(self, name_or_alias: str) -> EmblemEntry | None:
        direct = self._by_id.get(name_or_alias)
        if direct is not None and direct.verification_status == "verified":
            return direct
        return self._by_alias.get(normalize_entity_name(name_or_alias))

    def find_in_text(self, text: str, *, limit: int = 3) -> tuple[EmblemEntry, ...]:
        normalized_text = f" {normalize_entity_name(text)} "
        matches: list[tuple[int, int, EmblemEntry]] = []
        for alias, entry in self._by_alias.items():
            needle = f" {alias} "
            position = normalized_text.find(needle)
            if position >= 0:
                matches.append((position, -len(alias), entry))

        found: list[EmblemEntry] = []
        seen: set[str] = set()
        for _, _, entry in sorted(matches, key=lambda item: (item[0], item[1])):
            if entry.id in seen or not entry.ready:
                continue
            found.append(entry)
            seen.add(entry.id)
            if len(found) >= limit:
                break
        return tuple(found)
