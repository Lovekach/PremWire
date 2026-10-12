from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RegistryEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntityHit:
    entity_id: str
    entity_type: str
    canonical_name: str
    source_mention: str
    start_offset: int
    end_offset: int


_PLAYER_SEPARATOR_RE = re.compile(r"[\s\-‐‑‒–—'’]+")


def _normalized_alias(alias: str) -> str:
    return " ".join(alias.casefold().split())


def _player_variant_key(alias: str) -> str:
    """Compare only spacing/hyphen/apostrophe variants, never fuzzy spellings."""
    return "".join(part for part in _PLAYER_SEPARATOR_RE.split(alias.casefold()) if part)


def _player_variant_pattern(alias: str) -> re.Pattern[str] | None:
    parts = tuple(part for part in _PLAYER_SEPARATOR_RE.split(alias) if part)
    if len(parts) < 2:
        return None
    return re.compile(
        rf"(?<!\w)@?{'[\\s\\-‐‑‒–—\'’]+'.join(re.escape(part) for part in parts)}(?!\w)",
        re.IGNORECASE,
    )


class StrategyBEntityRegistry:
    """Read-only exact/reviewed registry. Unknown entities are never created."""

    def __init__(self, project_root: Path) -> None:
        entities: dict[tuple[str, str], RegistryEntity] = {}
        reviewed_alias_owners: dict[tuple[str, str], set[tuple[str, str]]] = {}

        base = json.loads((project_root / "config" / "entity_registry.json").read_text(encoding="utf-8"))
        for raw in base["entities"]:
            aliases = {
                str(raw["canonical_name"]),
                str(raw.get("display_name_ru") or ""),
                *(str(item["alias"]) for item in raw.get("aliases", [])),
            }
            key = (str(raw["entity_type"]), str(raw["entity_id"]))
            entities[key] = RegistryEntity(key[1], key[0], str(raw["canonical_name"]), tuple(a for a in aliases if a))

        for filename in (
            "entity_registry_stage_c2_reviewed.json",
            "entity_registry_stage_c3b_reviewed.json",
            "entity_registry_stage_c4a_reviewed.json",
        ):
            payload = json.loads((project_root / "config" / filename).read_text(encoding="utf-8"))
            for raw in payload["aliases"]:
                if raw.get("reviewed") is not True:
                    continue
                key = (str(raw["entity_type"]), str(raw["entity_id"]))
                previous = entities.get(key)
                aliases = set(previous.aliases if previous else ())
                aliases.update((str(raw["alias"]), str(raw["canonical_name"])))
                entities[key] = RegistryEntity(key[1], key[0], str(raw["canonical_name"]), tuple(sorted(aliases)))
                reviewed_alias_owners.setdefault((key[0], _normalized_alias(str(raw["alias"]))), set()).add(key)
                reviewed_alias_owners.setdefault((key[0], _normalized_alias(str(raw["canonical_name"]))), set()).add(key)

        clubs = json.loads((project_root / "config" / "club-emblems.json").read_text(encoding="utf-8"))
        for raw in clubs["clubs"]:
            key = ("club", f"club-{raw['id']}")
            previous = entities.get(key)
            aliases = set(previous.aliases if previous else ())
            aliases.update({
                str(raw.get("name") or ""), str(raw.get("name_en") or ""),
                *(str(item) for item in raw.get("aliases_ru", [])),
                *(str(item) for item in raw.get("aliases_en", [])),
            })
            canonical = str(raw.get("name_en") or raw.get("name") or (previous.canonical_name if previous else raw["id"]))
            entities[key] = RegistryEntity(key[1], key[0], canonical, tuple(a for a in aliases if len(a) >= 3))

        # Explicitly reviewed aliases take precedence over broad emblem-library
        # aliases. Without this pass one mention such as "Freiburg" can resolve
        # to two canonical IDs and either create a duplicate relation or block a
        # valid story. Unreviewed collisions remain ambiguous and therefore
        # fail closed in resolve_capture.
        filtered: list[RegistryEntity] = []
        for key, entity in entities.items():
            aliases = tuple(
                alias
                for alias in entity.aliases
                if reviewed_alias_owners.get((entity.entity_type, _normalized_alias(alias)), {key}) == {key}
            )
            if aliases:
                filtered.append(
                    RegistryEntity(
                        entity.entity_id,
                        entity.entity_type,
                        entity.canonical_name,
                        aliases,
                    )
                )
        # Surname-only forms are allowed only when explicitly reviewed.  Inferring
        # them from every unique registry surname looks safe locally but can turn
        # ordinary prose into a player mention in a different corpus.
        self.entities = tuple(filtered)
        self._player_variant_owners: dict[str, set[str]] = {}
        for entity in self.entities:
            if entity.entity_type != "player":
                continue
            for alias in entity.aliases:
                if _player_variant_pattern(alias) is not None:
                    self._player_variant_owners.setdefault(_player_variant_key(alias), set()).add(entity.entity_id)

    def scan(self, text: str, entity_type: str) -> tuple[EntityHit, ...]:
        by_id: dict[str, EntityHit] = {}
        for entity in self.entities:
            if entity.entity_type != entity_type:
                continue
            best: EntityHit | None = None
            for alias in sorted(entity.aliases, key=len, reverse=True):
                match = re.search(rf"(?<!\w)@?{re.escape(alias)}(?!\w)", text, re.IGNORECASE)
                if match is None and entity_type == "player":
                    variant = _player_variant_pattern(alias)
                    owners = self._player_variant_owners.get(_player_variant_key(alias), set())
                    if variant is not None and owners == {entity.entity_id}:
                        match = variant.search(text)
                if not match:
                    continue
                mention = match.group(0).lstrip("@")
                start = match.end() - len(mention)
                candidate = EntityHit(entity.entity_id, entity.entity_type, entity.canonical_name, mention, start, match.end())
                if best is None or len(candidate.source_mention) > len(best.source_mention):
                    best = candidate
            if best is not None:
                by_id[entity.entity_id] = best
        return tuple(sorted(by_id.values(), key=lambda hit: (hit.start_offset, hit.entity_id)))

    def resolve_capture(self, text: str, start: int, end: int, entity_type: str) -> EntityHit | None:
        surface = text[start:end].strip(" \t\n.,;:!?—–-\"'«»")
        adjusted = text.find(surface, start, end + 1)
        hits = self.scan(surface, entity_type)
        # Russian compact headlines commonly use a quoted club in the genitive
        # case (for example, "new player of <club>").  Apply the smallest
        # deterministic morphology rule only inside an already structural
        # grammar capture: a Cyrillic surface ending in ``а`` or ``я`` may resolve to a
        # single reviewed/base club alias with that final letter removed.  This
        # does not expand global scanning or create an entity from prose.
        if not hits and entity_type == "club" and re.fullmatch(r"[А-Яа-яЁё-]{4,}[ая]", surface):
            bases = (surface[:-1],) if surface.endswith("а") else (surface[:-1], surface[:-1] + "й")
            candidates = {hit.entity_id: hit for base in bases for hit in self.scan(base, entity_type)}
            if len(candidates) == 1:
                hit = next(iter(candidates.values()))
                return EntityHit(
                    hit.entity_id, hit.entity_type, hit.canonical_name, surface,
                    adjusted, adjusted + len(surface),
                )
        if len(hits) != 1:
            return None
        hit = hits[0]
        return EntityHit(
            hit.entity_id, hit.entity_type, hit.canonical_name, hit.source_mention,
            adjusted + hit.start_offset, adjusted + hit.end_offset,
        )
