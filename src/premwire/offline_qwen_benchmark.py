from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .config import load_config
from .simple_editorial import assess_simple_source
from .simple_semantic_verifier import SEMANTIC_VERIFIER_SYSTEM_PROMPT
from .text_editor import OllamaTextEditor


MODELS = ("qwen3.5:4b", "qwen3.5:9b")

CATEGORY_QUOTAS = {
    "OFFICIAL_TRANSFER": 4,
    "AGREEMENT": 4,
    "TALKS": 4,
    "INTEREST": 4,
    "LOAN": 3,
    "CONTRACT": 3,
    "INJURY": 3,
    "MATCH_RESULT": 3,
    "QUOTE": 3,
    "MANAGER_CLUB_NEWS": 3,
    "GENERAL_FOOTBALL_NEWS": 2,
}

# Manually reviewed real production item IDs. Automatic keyword selection was
# deliberately rejected because words such as "official" also occur in referee
# and ticket-package stories. Each label below describes only what the source
# itself supports; it is not a production publication gate.
CURATED_CASES: tuple[dict[str, Any], ...] = (
    {"id": 3007, "category": "OFFICIAL_TRANSFER", "status": "OFFICIAL", "entities": [["Эшли Филлипс", "Ashley Phillips"], ["Мидлсбро", "Middlesbrough"], ["Тоттенхэм", "Tottenham"]], "numbers": ["21", "7"]},
    {"id": 2603, "category": "OFFICIAL_TRANSFER", "status": "OFFICIAL", "entities": [["Рексем", "Wrexham"], ["Энтони Паттерсон", "Anthony Patterson"], ["Сандерленд", "Sunderland"]], "numbers": ["8"]},
    {"id": 2561, "category": "OFFICIAL_TRANSFER", "status": "OFFICIAL", "entities": [["Алексис Санчес", "Alexis Sanchez"], ["Монреаль", "Montreal"], ["Севилья", "Sevilla"]], "numbers": ["31", "2027"]},
    {"id": 2057, "category": "OFFICIAL_TRANSFER", "status": "OFFICIAL", "entities": [["Душан Тадич", "Dusan Tadic"], ["НЕК", "NEC"], ["Аль-Вахда", "Al Wahda"]], "numbers": ["2028"]},
    {"id": 2818, "category": "AGREEMENT", "status": "AGREEMENT", "entities": [["Атлетико Минеиро", "Atletico Mineiro", "Atletico MG"], ["Фред", "Fred"], ["Фенербахче", "Fenerbahce", "Fenerbahçe"]], "numbers": ["2", "500", "3.5", "1"]},
    {"id": 2847, "category": "AGREEMENT", "status": "AMBIGUOUS", "entities": [["Страсбур", "Strasbourg"], ["Штурм", "Sturm Graz"], ["Джейланд Митчелл", "Jeyland Mitchell"]], "numbers": ["5"]},
    {"id": 2592, "category": "AGREEMENT", "status": "AGREEMENT", "entities": [["Фиорентина", "Fiorentina"], ["Матео Пеллегрино", "Mateo Pellegrino"], ["Парма", "Parma"]], "numbers": ["20"]},
    {"id": 848, "category": "AGREEMENT", "status": "AGREEMENT", "entities": [["Ипсвич", "Ipswich"], ["Селтик", "Celtic"], ["Дайдзен Маэда", "Daizen Maeda"]], "numbers": ["10"]},
    {"id": 2823, "category": "TALKS", "status": "TALKS", "entities": [["Пари Сен-Жермен", "ПСЖ", "Paris Saint-Germain", "PSG"], ["Аякс", "Ajax"], ["Мика Годтс", "Mika Godts"]], "numbers": ["2"]},
    {"id": 2602, "category": "TALKS", "status": "AMBIGUOUS", "entities": [["Манчестер Сити", "Manchester City", "Man City"], ["Барселона", "Barcelona", "Barça"], ["Родри", "Rodri"]], "numbers": ["60"]},
    {"id": 2574, "category": "TALKS", "status": "TALKS", "entities": [["Интер", "Inter Milan"], ["Тоттенхэм", "Tottenham"], ["Джед Спенс", "Djed Spence"]], "numbers": []},
    {"id": 1522, "category": "TALKS", "status": "TALKS", "entities": [["Лидс Юнайтед", "Leeds United"], ["Джеймс Траффорд", "James Trafford"]], "numbers": []},
    {"id": 1394, "category": "INTEREST", "status": "INTEREST", "entities": [["Манчестер Юнайтед", "Manchester United"], ["Реал Мадрид", "Real Madrid"], ["Эдуардо Камавинга", "Eduardo Camavinga"], ["Франция", "France"]], "numbers": ["23", "2026"]},
    {"id": 1202, "category": "INTEREST", "status": "INTEREST", "entities": [["Реал Мадрид", "Real Madrid"], ["Барселона", "Barcelona"], ["Ферран Торрес", "Ferran Torres"], ["ПСЖ", "PSG"]], "numbers": []},
    {"id": 1074, "category": "INTEREST", "status": "INTEREST", "entities": [["Матиас Галарса", "Matias Galarza"], ["Ривер Плейт", "River Plate"], ["Хетафе", "Getafe"], ["Болонья", "Bologna"], ["Фабрицио Романо", "Fabrizio Romano"]], "numbers": []},
    {"id": 261, "category": "INTEREST", "status": "INTEREST", "entities": [["Арсенал", "Arsenal"], ["Манчестер Сити", "Manchester City"], ["Лилль", "Lille"], ["Айюб Буадди", "Ayyoub Bouaddi"]], "numbers": ["85"]},
    {"id": 2833, "category": "LOAN", "status": "OFFICIAL", "entities": [["Харри Хауэлл", "Harry Howell"], ["Лестер", "Leicester"], ["Брайтон", "Brighton"]], "numbers": []},
    {"id": 2588, "category": "LOAN", "status": "AMBIGUOUS", "entities": [["Челси", "Chelsea"], ["Михаил Мудрик", "Михайло Мудрик", "Mykhaylo Mudryk"], ["Ковентри Сити", "Coventry City"]], "numbers": []},
    {"id": 2578, "category": "LOAN", "status": "EXPECTED", "entities": [["Эванн Гессан", "Evann Guessand"], ["Кристал Пэлас", "Crystal Palace"], ["Астон Вилла", "Aston Villa"]], "numbers": []},
    {"id": 1882, "category": "CONTRACT", "status": "AGREEMENT", "entities": [["Эдин Джеко", "Edin Dzeko"], ["Шальке 04", "Schalke 04"]], "numbers": ["1", "2027"]},
    {"id": 1485, "category": "CONTRACT", "status": "OFFICIAL", "entities": [["Фредди Вудмен", "Freddie Woodman"], ["Ливерпуль", "Liverpool"]], "numbers": ["2029"]},
    {"id": 616, "category": "CONTRACT", "status": "INTEREST", "entities": [["Алекс Скотт", "Alex Scott"], ["Борнмут", "Bournemouth"], ["АПЛ", "Premier League"]], "numbers": ["22", "37", "3", "1"]},
    {"id": 1532, "category": "INJURY", "status": "NOT_APPLICABLE", "entities": [["Френки де Йонг", "Frenkie de Jong"], ["Барселона", "Barcelona"]], "numbers": []},
    {"id": 2483, "category": "INJURY", "status": "NOT_APPLICABLE", "entities": [["Рейнджерс", "Rangers"], ["Юссеф Шермити", "Youssef Chermiti"], ["Хиберниан", "Hibernian"]], "numbers": []},
    {"id": 2192, "category": "INJURY", "status": "EXPECTED", "entities": [["Карлос Балеба", "Carlos Baleba"], ["Брайтон", "Brighton"], ["Лилль", "Lille"]], "numbers": ["22", "31", "2023"]},
    {"id": 2985, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Брентфорд", "Brentford"], ["Айнтрахт", "Eintracht"], ["Кевин Шаде", "Kevin Schade"], ["Игор Тиаго", "Igor Thiago"], ["Мамаду Сангаре", "Mamadou Sangare"]], "numbers": ["7:0", "2", "1"]},
    {"id": 2981, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Милан", "Milan"], ["Манчестер Юнайтед", "Manchester United"], ["Рубен Аморим", "Ruben Amorim"], ["Самюэл Чуквуэзе", "Samuel Chukwueze"], ["Бруну Фернандеш", "Bruno Fernandes"], ["Маркус Рэшфорд", "Marcus Rashford"]], "numbers": ["4:2", "14", "1", "2", "4"]},
    {"id": 59, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Англия", "England"], ["Франция", "France"], ["Томас Тухель", "Thomas Tuchel"], ["Букайо Сака", "Bukayo Saka"], ["Усман Дембеле", "Ousmane Dembele"], ["Джуд Беллингем", "Jude Bellingham"]], "numbers": ["2026", "4:0", "6:4", "1"]},
    {"id": 2855, "category": "QUOTE", "status": "NOT_APPLICABLE", "entities": [["Алексис Мак Аллистер", "Alexis Mac Allister"], ["Карлос Мак Аллистер", "Carlos"], ["Ливерпуль", "Liverpool"]], "numbers": []},
    {"id": 2830, "category": "QUOTE", "status": "NOT_APPLICABLE", "entities": [["Майкл Каррик", "Michael Carrick"], ["Маркус Рэшфорд", "Marcus Rashford"], ["The Athletic", "Атлетик"]], "numbers": []},
    {"id": 2815, "category": "QUOTE", "status": "NOT_APPLICABLE", "entities": [["Майкл Каррик", "Michael Carrick"], ["Sky Sports", "Скай Спортс"]], "numbers": []},
    {"id": 2125, "category": "MANAGER_CLUB_NEWS", "status": "OFFICIAL", "entities": [["Италия", "Italy"], ["Клаудио Раньери", "Claudio Ranieri"], ["Роберто Манчини", "Roberto Mancini"]], "numbers": []},
    {"id": 1559, "category": "MANAGER_CLUB_NEWS", "status": "EXPECTED", "entities": [["Аргентина", "Argentina", "AFA"], ["Лионель Скалони", "Lionel Scaloni"]], "numbers": ["2026", "2022"]},
    {"id": 266, "category": "MANAGER_CLUB_NEWS", "status": "OFFICIAL", "entities": [["Бельгия", "Belgian FA"], ["Руди Гарсия", "Rudi Garcia"]], "numbers": []},
    {"id": 1469, "category": "GENERAL_FOOTBALL_NEWS", "status": "NOT_APPLICABLE", "entities": [["Англия", "England"], ["Уэльс", "Wales"], ["Аргентина", "Argentina"]], "numbers": ["2026", "384", "2", "17"]},
    {"id": 659, "category": "GENERAL_FOOTBALL_NEWS", "status": "NOT_APPLICABLE", "entities": [["Энтони Тейлор", "Anthony Taylor"], ["Испания", "Spain"], ["Португалия", "Portugal"]], "numbers": ["20", "831", "47", "6", "1-0"]},
)

# Source-only selection made before looking at hardened-model outputs. These
# real items do not overlap CURATED_CASES and are held out from prompt tuning.
UNSEEN_CASES: tuple[dict[str, Any], ...] = (
    {"id": 1089, "category": "INTEREST", "status": "AMBIGUOUS", "entities": [["Николас Джексон", "Nicolas Jackson"], ["Астон Вилла", "Aston Villa"], ["Унаи Эмери", "Unai Emery"], ["Челси", "Chelsea"], ["Морган Роджерс", "Morgan Rogers"], ["Вильярреал", "Villarreal"], ["MailSport"]], "numbers": ["117", "25"]},
    {"id": 1904, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Рексем", "Рексхэм", "Wrexham"], ["Райан Рейнольдс", "Ryan Reynolds"], ["Лидс", "Leeds"], ["Ноттингем Форест", "Nottingham Forest"], ["Челси", "Chelsea"]], "numbers": ["3:2", "85", "3:3", "4:3", "2:4"]},
    {"id": 2415, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Манчестер Сити", "Manchester City"], ["Атлетико Мадрид", "Atletico Madrid"], ["Омар Мармуш", "Omar Marmoush"], ["Антуан Семеньо", "Antoine Semenyo"], ["Энцо Мареска", "Enzo Maresca"], ["Арсенал", "Arsenal"]], "numbers": ["3:1"]},
    {"id": 2575, "category": "AGREEMENT", "status": "AMBIGUOUS", "entities": [["Лацио", "Lazio"], ["Интер", "Inter"], ["Давиде Фраттези", "Davide Frattesi"]], "numbers": ["15"]},
    {"id": 2589, "category": "AGREEMENT", "status": "AMBIGUOUS", "entities": [["Интер", "Inter"], ["Тоттенхэм", "Tottenham"], ["Джед Спенс", "Djed Spence"], ["Фабрицио Романо", "Fabrizio Romano"]], "numbers": ["30"]},
    {"id": 2890, "category": "TALKS", "status": "TALKS", "entities": [["Арсенал", "Arsenal"], ["Манчестер Юнайтед", "Manchester United"], ["Тоттенхэм", "Tottenham"], ["Галатасарай", "Galatasaray"], ["Виктор Осимхен", "Victor Osimhen"], ["Teamtalk"]], "numbers": []},
    {"id": 2848, "category": "LOAN", "status": "AGREEMENT", "entities": [["Фейеноорд", "Feyenoord"], ["Хави Лопес", "Javi López", "Javi Lopez"], ["Реал Сосьедад", "Real Sociedad"], ["Роттердам", "Rotterdam"]], "numbers": []},
    {"id": 2858, "category": "LOAN", "status": "EXPECTED", "entities": [["Селтик", "Celtic"], ["Челси", "Chelsea"], ["Лэндон Эменало", "Landon Emenalo"]], "numbers": []},
    {"id": 2979, "category": "MATCH_RESULT", "status": "NOT_APPLICABLE", "entities": [["Морган Роджерс", "Morgan Rogers"], ["Челси", "Chelsea"], ["Реал Сосьедад", "Real Sociedad"], ["Жоао Педро", "Joao Pedro"]], "numbers": ["138", "3:1"]},
    {"id": 2980, "category": "TALKS", "status": "TALKS", "entities": [["Астон Вилла", "Aston Villa"], ["Зион Судзуки", "Zion Suzuki"], ["ПСЖ", "PSG"], ["Парма", "Parma"], ["Фабрицио Романо", "Fabrizio Romano"]], "numbers": ["30", "23", "20", "28", "5"]},
    {"id": 3000, "category": "TALKS", "status": "TALKS", "entities": [["Астон Вилла", "Aston Villa"], ["Зион Судзуки", "Zion Suzuki"], ["ПСЖ", "PSG"], ["Фабрицио Романо", "Fabrizio Romano"]], "numbers": []},
    {"id": 3005, "category": "GENERAL_FOOTBALL_NEWS", "status": "EXPECTED", "entities": [["Барселона", "Barcelona"], ["Жоау Канселу", "Joao Cancelo"], ["Аль-Хиляль", "Al-Hilal"], ["Фабрицио Романо", "Fabrizio Romano"]], "numbers": []},
)

_CATEGORY_PATTERNS = {
    "LOAN": re.compile(r"\b(?:loan|аренд)\w*\b", re.I),
    "INJURY": re.compile(r"\b(?:injur|травм|surgery|операци)\w*\b", re.I),
    "CONTRACT": re.compile(
        r"\b(?:contract|контракт|extension|renew(?:al|ed)?|продли)\w*\b", re.I
    ),
    "TALKS": re.compile(r"\b(?:talks?|negotiat|переговор)\w*\b", re.I),
    "INTEREST": re.compile(
        r"\b(?:interest(?:ed)?|keen|target(?:ing)?|monitor(?:ing)?|интерес|"
        r"нацел|рассматрива)\w*\b",
        re.I,
    ),
    "AGREEMENT": re.compile(
        r"\b(?:agreement|agreed|agree(?:s|d)?|соглашени|согласова|договори)\w*\b",
        re.I,
    ),
    "OFFICIAL_TRANSFER": re.compile(
        r"\b(?:official(?:ly)?|here\s+we\s+go|signed|signs|completed|"
        r"официальн|подписал[аи]?|объявил[аи]?\s+о\s+(?:переходе|трансфере)|"
        r"переш[её]л|завершил[аи]?\s+трансфер)\w*\b",
        re.I,
    ),
    "MATCH_RESULT": re.compile(
        r"(?:\b\d+\s*[-:]\s*\d+\b|\b(?:match|матч|defeat|beat|draw|"
        r"побед|поражени|ничь|гол)\w*\b)",
        re.I,
    ),
    "QUOTE": re.compile(
        r"(?:[\"“”«»].{12,}[\"“”«»]|\b(?:said|says|told|заявил|сказал|"
        r"отметил|признался)\w*\b)",
        re.I,
    ),
    "MANAGER_CLUB_NEWS": re.compile(
        r"\b(?:manager|coach|тренер|уволен|назначен|appointed|sacked|board|"
        r"руководств)\w*\b",
        re.I,
    ),
}

_STATUS_PATTERNS = {
    "OFFICIAL": re.compile(
        r"\b(?:official(?:ly)?|here\s+we\s+go|signed|signs|completed|"
        r"официальн|подписал[аи]?|объявил[аи]?\s+о\s+(?:переходе|трансфере)|"
        r"переш[её]л|завершил[аи]?\s+трансфер)\w*\b",
        re.I,
    ),
    "AGREEMENT": re.compile(
        r"\b(?:agreement|agreed|agree(?:s|d)?|соглашени|согласова|договори)\w*\b",
        re.I,
    ),
    "TALKS": re.compile(r"\b(?:talks?|negotiat|переговор)\w*\b", re.I),
    "INTEREST": re.compile(
        r"\b(?:interest(?:ed)?|keen|target(?:ing)?|monitor(?:ing)?|интерес|"
        r"нацел|рассматрива)\w*\b",
        re.I,
    ),
    "OFFER": re.compile(r"\b(?:bid|offer|предложени|заявк)\w*\b", re.I),
    "MEDICAL_PLANNED": re.compile(
        r"\b(?:medical|медосмотр)\w*\b.*\b(?:planned|scheduled|expected|"
        r"пройд[её]т|запланирован|ожида)\w*\b",
        re.I | re.S,
    ),
    "EXPECTED": re.compile(
        r"\b(?:expected|set\s+to|likely|ожида|должен|планиру)\w*\b", re.I
    ),
}

_NUMBER_RE = re.compile(
    r"(?<!\w)(\d+(?:[.,]\d+)?(?:\s*[-/]\s*\d+(?:[.,]\d+)?)?)"
    r"(?:\s*(?:%|[kmb]|bn|million|млн|млрд))?",
    re.I,
)
_URL_OR_HANDLE_RE = re.compile(r"(?:https?://|www\.|t\.me/|x\.com/|(?<!\w)@\w+)", re.I)
_STATE_PRIORITY = {
    "published": 0,
    "approved": 1,
    "queued": 2,
    "baseline": 3,
    "cancelled_editor_deleted": 4,
    "cancelled_source_deleted": 5,
    "rejected_quality": 6,
    "rejected_manual": 7,
    "rejected_low_priority": 8,
}


def classify_story(text: str) -> str:
    matches = {name for name, pattern in _CATEGORY_PATTERNS.items() if pattern.search(text)}
    # Transfer certainty is the most useful benchmark axis. Loan/injury/contract
    # remain distinct beats even when a source also contains a generic status word.
    for name in ("LOAN", "INJURY", "CONTRACT", "TALKS", "INTEREST", "AGREEMENT", "OFFICIAL_TRANSFER"):
        if name in matches:
            return name
    for name in ("MATCH_RESULT", "QUOTE", "MANAGER_CLUB_NEWS"):
        if name in matches:
            return name
    return "GENERAL_FOOTBALL_NEWS"


def expected_transfer_status(text: str) -> str:
    found = [name for name, pattern in _STATUS_PATTERNS.items() if pattern.search(text)]
    if not found:
        return "NOT_APPLICABLE"
    certainty = {"OFFICIAL", "AGREEMENT", "TALKS", "INTEREST", "OFFER"}
    if len(certainty.intersection(found)) > 1:
        return "AMBIGUOUS"
    # A planned medical or expected action must remain below a completed deal.
    for name in (
        "OFFICIAL",
        "AGREEMENT",
        "TALKS",
        "INTEREST",
        "OFFER",
        "MEDICAL_PLANNED",
        "EXPECTED",
    ):
        if name in found:
            return name
    return "AMBIGUOUS"


def extract_numbers(text: str) -> list[str]:
    return list(dict.fromkeys(re.sub(r"\s+", "", value) for value in _NUMBER_RE.findall(text)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_type(source_id: str) -> str:
    if source_id.startswith("telegram_"):
        return "telegram"
    if source_id.startswith("x_"):
        return "x"
    return "other"


def build_dataset(database: Path, *, limit: int = 36) -> dict[str, Any]:
    """Select real items without ever opening the production database writable."""

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in CURATED_CASES)
        rows = connection.execute(
            f"""
            SELECT id, source_id, external_id, source_text, state, source_published_at
            FROM items
            WHERE id IN ({placeholders})
            """,
            [case["id"] for case in CURATED_CASES],
        ).fetchall()

    by_id = {int(row["id"]): row for row in rows}
    missing = [case["id"] for case in CURATED_CASES if case["id"] not in by_id]
    if missing:
        raise RuntimeError(f"CURATED_SOURCE_ITEMS_MISSING: {missing}")

    selected: list[dict[str, Any]] = []
    for truth in CURATED_CASES[:limit]:
        row = by_id[truth["id"]]
        original = str(row["source_text"]).strip()
        decision = assess_simple_source(original)
        if not decision.allowed:
            raise RuntimeError(
                f"CURATED_SOURCE_BLOCKED: item={row['id']} reason={decision.reason}"
            )
        case = {
            "case_id": "",
            "source_id": str(row["source_id"]),
            "source_item_id": int(row["id"]),
            "source_external_id": str(row["external_id"]),
            "source_type": _source_type(str(row["source_id"])),
            "source_state_at_selection": str(row["state"]),
            "source_published_at": str(row["source_published_at"]),
            "original_source_text": original,
            "cleaned_source_text": decision.clean_text,
            "story_category": truth["category"],
            "expected_entities": [
                {"canonical": aliases[0], "acceptable": aliases}
                for aliases in truth["entities"]
            ],
            "expected_numbers": truth["numbers"],
            "expected_transfer_status": truth["status"],
            "has_url_or_handle": bool(_URL_OR_HANDLE_RE.search(original)),
            "label_notes": (
                "Manually source-reviewed label; AMBIGUOUS means only that certainty "
                "must not be upgraded. Entity aliases are benchmark-only."
            ),
        }
        selected.append(case)

    if len(selected) != min(limit, sum(CATEGORY_QUOTAS.values())):
        counts = {name: sum(c["story_category"] == name for c in selected) for name in CATEGORY_QUOTAS}
        raise RuntimeError(f"DATASET_COVERAGE_INCOMPLETE: {counts}")
    for index, case in enumerate(selected, 1):
        case["case_id"] = f"PWQ-{index:03d}"

    return {
        "schema_version": 1,
        "selection": {
            "database": str(database),
            "database_sha256": _sha256(database),
            "sqlite_mode": "ro",
            "case_count": len(selected),
            "category_quotas": CATEGORY_QUOTAS,
        },
        "cases": selected,
    }


def build_unseen_dataset(database: Path) -> dict[str, Any]:
    """Build the preselected held-out real-source set with SQLite in read-only mode."""

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in UNSEEN_CASES)
        rows = connection.execute(
            f"""
            SELECT id, source_id, external_id, source_text, state, source_published_at
            FROM items
            WHERE id IN ({placeholders})
            """,
            [case["id"] for case in UNSEEN_CASES],
        ).fetchall()

    by_id = {int(row["id"]): row for row in rows}
    missing = [case["id"] for case in UNSEEN_CASES if case["id"] not in by_id]
    if missing:
        raise RuntimeError(f"UNSEEN_SOURCE_ITEMS_MISSING: {missing}")

    selected: list[dict[str, Any]] = []
    for index, truth in enumerate(UNSEEN_CASES, 1):
        row = by_id[truth["id"]]
        original = str(row["source_text"]).strip()
        decision = assess_simple_source(original)
        if not decision.allowed:
            raise RuntimeError(
                f"UNSEEN_SOURCE_BLOCKED: item={row['id']} reason={decision.reason}"
            )
        selected.append(
            {
                "case_id": f"PWU-{index:03d}",
                "source_id": str(row["source_id"]),
                "source_item_id": int(row["id"]),
                "source_external_id": str(row["external_id"]),
                "source_type": _source_type(str(row["source_id"])),
                "source_state_at_selection": str(row["state"]),
                "source_published_at": str(row["source_published_at"]),
                "original_source_text": original,
                "cleaned_source_text": decision.clean_text,
                "story_category": truth["category"],
                "expected_entities": [
                    {"canonical": aliases[0], "acceptable": aliases}
                    for aliases in truth["entities"]
                ],
                "expected_numbers": truth["numbers"],
                "expected_transfer_status": truth["status"],
                "has_url_or_handle": bool(_URL_OR_HANDLE_RE.search(original)),
                "label_notes": (
                    "Held-out source-only selection made before hardened-model output; "
                    "labels are benchmark evidence, not publication gates."
                ),
            }
        )

    return {
        "schema_version": 1,
        "selection": {
            "database": str(database),
            "database_sha256": _sha256(database),
            "sqlite_mode": "ro",
            "case_count": len(selected),
            "held_out": True,
        },
        "cases": selected,
    }


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def performance_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in results if not row.get("error")]
    if not successful:
        return {}
    cold = successful[0]
    warm = successful[1:]
    warm_latencies = [float(row["latency_seconds"]) for row in warm]
    speeds = [
        float(row["eval_count"]) / (float(row["eval_duration_ns"]) / 1_000_000_000)
        for row in warm
        if row.get("eval_count") and row.get("eval_duration_ns")
    ]
    return {
        "cold_total_seconds": cold["latency_seconds"],
        "cold_load_seconds": (cold.get("load_duration_ns") or 0) / 1_000_000_000,
        "warm_request_count": len(warm),
        "warm_median_seconds": statistics.median(warm_latencies) if warm_latencies else None,
        "warm_p90_seconds": _percentile(warm_latencies, 0.90),
        "warm_tokens_per_second": statistics.median(speeds) if speeds else None,
        "peak_gpu_used_mb": max((row.get("gpu_used_mb") or 0) for row in successful) or None,
        "peak_ollama_ram_mb": max((row.get("ollama_ram_mb") or 0) for row in successful) or None,
    }


def quality_summary(
    evaluation: dict[str, Any],
    model_key: str,
    model_results: dict[str, Any],
) -> dict[str, Any]:
    rows = [entry[model_key] for entry in evaluation["evaluations"]]
    total = len(rows)
    severity = {
        name: sum(row["severity"] == name for row in rows)
        for name in ("HARD_ERROR", "SOFT_ERROR", "PASS")
    }
    result_rows = model_results["results"]
    hallucinations = sum(bool(row["hallucination"]) for row in rows)
    return {
        "total_cases": total,
        "hard_error_count": severity["HARD_ERROR"],
        "hard_error_rate": severity["HARD_ERROR"] / total,
        "soft_error_count": severity["SOFT_ERROR"],
        "hallucination_count": hallucinations,
        "hallucination_rate": hallucinations / total,
        "transfer_status_errors": sum(bool(row["transfer_status_error"]) for row in rows),
        "numeric_preservation_errors": sum(bool(row["numeric_preservation_error"]) for row in rows),
        "entity_preservation_errors": sum(bool(row["entity_preservation_error"]) for row in rows),
        "sanitizer_failures": sum(bool(row["sanitizer_failure"]) for row in rows),
        "pass_count": severity["PASS"],
        "pass_rate": severity["PASS"] / total,
        "average_style_score": statistics.mean(float(row["style_score"]) for row in rows),
        "average_output_length": statistics.mean(
            len(row["sanitized_output"]) for row in result_rows
        ),
    }


def _memory_sample() -> tuple[int | None, float | None]:
    gpu: int | None = None
    ram: float | None = None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        values = [int(line.strip()) for line in completed.stdout.splitlines() if line.strip().isdigit()]
        gpu = max(values) if values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        command = (
            "(Get-Process -ErrorAction SilentlyContinue | "
            "Where-Object {$_.ProcessName -like 'ollama*'} | "
            "Measure-Object -Property WorkingSet64 -Sum).Sum"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        value = completed.stdout.strip()
        ram = int(value) / 1024 / 1024 if value.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return gpu, ram


class InstrumentedEditor(OllamaTextEditor):
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.raw_model_output = ""
        self.repair_raw_model_output = ""
        self.semantic_raw_outputs: list[str] = []
        self.response_metadata_list: list[dict[str, Any]] = []

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = super()._request(method, path, payload)
        if path == "/api/chat":
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            system = (
                str(messages[0].get("content", ""))
                if messages and isinstance(messages[0], dict)
                else ""
            )
            if system == SEMANTIC_VERIFIER_SYSTEM_PROMPT:
                request_kind = "verifier"
                try:
                    self.semantic_raw_outputs.append(str(response["message"]["content"]))
                except (KeyError, TypeError):
                    self.semantic_raw_outputs.append("")
            elif "исправляешь фактический дефект" in system:
                request_kind = "repair"
            else:
                request_kind = "rewrite"
            metadata = {
                key: value
                for key, value in response.items()
                if key not in {"message", "response", "context"}
            }
            metadata["request_kind"] = request_kind
            self.response_metadata_list.append(metadata)
        return response

    def _generate_simple_rewrite(self, source_text: str) -> str:
        value = super()._generate_simple_rewrite(source_text)
        self.raw_model_output = value
        return value

    def _generate_simple_repair(self, source_text, candidate, violations):
        value = super()._generate_simple_repair(source_text, candidate, violations)
        self.repair_raw_model_output = value
        return value


def _sum_metadata(metadata: list[dict[str, Any]], key: str) -> int | None:
    values = [row.get(key) for row in metadata if row.get(key) is not None]
    return sum(int(value) for value in values) if values else None


def run_model(
    root: Path,
    dataset: dict[str, Any],
    model: str,
    *,
    temperature: float | None = None,
) -> dict[str, Any]:
    if model not in MODELS:
        raise ValueError(f"Unsupported benchmark model: {model}")
    app_config = load_config(root)
    editor_config = replace(
        app_config.editor,
        model=model,
        temperature=(app_config.editor.temperature if temperature is None else temperature),
    )
    editor = InstrumentedEditor(editor_config)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(dataset["cases"]):
        print(
            f"BENCHMARK_PROGRESS model={model} case={index + 1}/{len(dataset['cases'])} "
            f"id={case['case_id']}",
            flush=True,
        )
        editor.raw_model_output = ""
        editor.repair_raw_model_output = ""
        editor.semantic_raw_outputs = []
        editor.response_metadata_list = []
        started = time.perf_counter()
        error: str | None = None
        final = ""
        try:
            final = editor.simple_rewrite(case["source_id"], case["original_source_text"])
        except Exception as exc:  # benchmark must preserve per-case failure evidence
            error = f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - started
        gpu, ram = _memory_sample()
        metadata = editor.response_metadata_list
        rows.append(
            {
                "case_id": case["case_id"],
                "sequence": index + 1,
                "temperature_state": "cold" if index == 0 else "warm",
                "raw_model_output": editor.raw_model_output,
                "repair_raw_model_output": editor.repair_raw_model_output,
                "semantic_raw_outputs": list(editor.semantic_raw_outputs),
                "sanitized_output": final,
                "latency_seconds": latency,
                "ollama_request_count": len(metadata),
                "ollama_requests": metadata,
                "prompt_eval_count": _sum_metadata(metadata, "prompt_eval_count"),
                "eval_count": _sum_metadata(metadata, "eval_count"),
                "load_duration_ns": _sum_metadata(metadata, "load_duration"),
                "prompt_eval_duration_ns": _sum_metadata(metadata, "prompt_eval_duration"),
                "eval_duration_ns": _sum_metadata(metadata, "eval_duration"),
                "total_duration_ns": _sum_metadata(metadata, "total_duration"),
                "gpu_used_mb": gpu,
                "ollama_ram_mb": ram,
                "simple_trace": dict(editor.last_simple_trace),
                "error": error,
            }
        )
    return {
        "schema_version": 2,
        "model": model,
        "pipeline": (
            "OllamaTextEditor.simple_rewrite -> tiny_source_aware_cleanup -> "
            "sanitize_simple_publication_text -> simple factual guard -> "
            "at most one targeted repair"
        ),
        "editor_config": asdict(editor_config),
        "model_only_difference": True,
        "results": rows,
        "performance": performance_summary(rows),
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "CATEGORY_QUOTAS",
    "MODELS",
    "build_dataset",
    "build_unseen_dataset",
    "classify_story",
    "expected_transfer_status",
    "extract_numbers",
    "performance_summary",
    "quality_summary",
    "read_json",
    "run_model",
    "write_json",
]
