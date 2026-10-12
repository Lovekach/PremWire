"""Small source-local checks for observed publication failures; no network calls."""
from __future__ import annotations

import re


def matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


PROMO = (
    r"follow\s+all\s+the\s+action|priority\s+access[^.!?]{0,80}tickets|exclusively\s+on\s+the\s+\w+\s+app",
    r"комментатор\w*[^.!?]{0,90}угадал\w*\s+точный\s+сч[её]т|утро[^.!?]{0,50}хайлайт",
    r"\b(?:pari\s+secret|secret\s+event|секретн\w*\s+мероприяти\w*|"
    r"закрыт\w*\s+мероприяти\w*)\b",
    r"(?:специально\s+для\s+вас[^.!?]{0,80}хайлайт|ещ[её]\s+не\s+смотрели)",
    r"(?:на\s+трансляции[^.!?]{0,100}(?:групп|вышли)|"
    r"трансляци\w*\s+(?:можно\s+)?(?:посмотреть|смотр)|"
    r"📺\s*(?:FIFA\+|DAZN)|смотреть[^.!?]{0,100}на\s+трансляции)",
)
REACTION_TAIL = re.compile(
    r"(?:🤣|😂|😁|😎|🤡|💩|❤️|🔥|🙏|🗿|💔)\ufe0f?\s*[-—–]\s*[^\n]*",
)


def source_problem(text: str) -> str | None:
    if any(matches(p, text) for p in PROMO):
        return "SKIP_PROMOTIONAL"
    if matches(r"\bhalf[ -]?time\s+scores\b", text) and len(
        re.findall(r"\d+\s*[-:–]\s*\d+", text)
    ) > 1:
        return "SKIP_MULTI_MATCH_LIVE_TABLE"
    if matches(r"этот\s+эпизод[^.!?]{0,70}припоминать|все\s+интриги[^.!?]{0,50}тура", text):
        return "SKIP_LOW_INFORMATION"
    if matches(r"only\s+failed\s+to\s+score\s+against\s+one\s+club", text) and not matches(
        r"\b(?:except|against\s+one\s+club\s*[:—-])", text
    ):
        return "SKIP_INCOMPLETE_TEASER"
    return None


def transient_ttl_minutes(text: str) -> int | None:
    """TTL for live snapshots, not historical reports or ordinary transfers."""
    if matches(r"full[ -]?time|финальн\w*\s+свисток|матч\s+заверш|yesterday|вчера", text):
        return None
    if matches(r"half[ -]?time|are\s+now\s+level|as\s+it\s+stands|are\s+\d+[-:]\d+\s+up|сч[её]т\s+к\s+перерыву", text):
        return 10
    if matches(r"team\s+news|starting\s+(?:xi|line[ -]?up)|\bSTARTS(?:\s|!)|"
               r"выйдет\s+в\s+стартов|стартов\w*\s+состав|injured\s+in\s+the\s+warm-up", text):
        return 30
    return None


def relation_violations(source: str, output: str) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []

    def flag(condition: bool, code: str, detail: str) -> None:
        if condition:
            found.append((code, detail))

    flag(any(matches(p, output) for p in PROMO) or bool(REACTION_TAIL.search(output)),
         "PUBLIC_PROMO_LEAK", "Удалить рекламу, трансляционные приглашения и реакционный опрос.")
    live = transient_ttl_minutes(source) == 10
    flag(matches(r"has\s+been\s+offered\s+to", source) and matches(r"получил\w*\s+предложени", output),
         "OFFER_DIRECTION_REVERSED", "Игрока предложили клубу, а не клуб сделал предложение игроку.")
    flag(matches(r"first\s+start", source) and matches(r"первая\s+игра|первый\s+матч", output),
         "FIRST_START_AS_DEBUT", "Первый выход в старте не означает дебют за клуб.")
    flag(matches(r"26-year", source) and matches(r"26[ -]матч", output),
         "STAT_UNIT_CHANGED", "В источнике 26 лет, не 26 матчей.")
    flag(matches(r"Villarreal", source) and matches(r"Бильбар|Бильбао", output),
         "OPPONENT_CHANGED", "Villarreal — Вильярреал; нельзя заменить соперника.")
    flag(live and matches(r"(?:матч|встреча)\b[^.!?]{0,60}\bзаверш|итогов\w*\s+сч[её]т|одержал\w*\s+побед|победил\w*|обыграл\w*", output),
         "LIVE_SCORE_AS_FINAL", "Источник сообщает текущий счёт, а не итог матча.")
    flag(matches(r"\bFA\s+Cup\b|Куб\w*\s+Англии", output) and not matches(r"\bFA\s+Cup\b|Куб\w*\s+Англии", source),
         "UNSUPPORTED_COMPETITION", "Источник не называет Кубок Англии.")
    flag(matches(r"\bпоказал\w*\b", source) and not matches(r"заявил|сказал|рассказал|[«\"]", source)
         and matches(r"заявил|сказал|рассказал", output),
         "INVENTED_SPEECH", "Оценка действий не является заявлением игрока.")
    flag(matches(r"only\s+failed\s+to\s+score\s+against\s+one\s+club", source)
         and matches(r"во\s+всех\s+матчах|в\s+каждом\s+матче", output),
         "STAT_QUANTIFIER_CHANGED", "Против всех соперников не означает в каждом матче.")
    flag(matches(r"YoungLionesses", source) and matches(r"🆚\s*Canada", source)
         and not matches(r"Англи|английск", output),
         "TEAM_ROLES_CHANGED", "YoungLionesses — сборная Англии; Канада является соперником.")
    flag(matches(r"as\s+it\s+stands", source) and matches(r"занимает\s+второе\s+место", output)
         and not matches(r"пока|текущ|предварительн|при\s+таком\s+сч[её]те", output),
         "LIVE_TABLE_CERTAINTY", "Место в таблице предварительное, при текущем счёте.")
    flag(matches(r"чист\w*\s+расход", source) and not re.search(r"\d", source)
         and matches(r"оказал\w*\s+в\s+плюсе|получил\w*\s+прибыл", output)
         and not matches(r"в\s+плюсе|прибыл", source),
         "UNREAD_CHART_INFERENCE", "Текст источника не содержит вывода о прибыли.")
    for source_pattern, bad_pattern, detail in (
        (r"Andoni", r"Анони", "Имя Andoni: Андони."),
        (r"Gakpo", r"Гакопо", "Фамилия Gakpo: Гакпо."),
        (r"pre-season", r"предсезонн\w*\s+турнир", "Pre-season означает предсезонную подготовку, не турнир."),
        (r"as\s+number\s+9", r"под\s+девятым\s+номером", "Number 9 здесь позиция нападающего, не номер футболки."),
        (r"close\s+the\s+game", r"закрыть\s+сч[её]т", "Речь о том, чтобы довести матч до нужного результата."),
        (r"League\s+One", r"Лиг[аиуе]\s+одного", "Название: английская Лига 1."),
        (r"CPFC|Crystal\s+Palace", r"Эйгл", "Eagles здесь Кристал Пэлас."),
        (r"initial\s+approaches\s+made", r"подготовлен\w*\s+первые\s+подходы", "Первые контакты уже состоялись."),
    ):
        flag(matches(source_pattern, source) and matches(bad_pattern, output), "FOOTBALL_TRANSLATION_ERROR", detail)
    # Russian-source names must not acquire two invented capitalised components.
    source_stems = {w.casefold()[:5] for w in re.findall(r"[А-ЯЁ][а-яё]{2,}", source)}
    for name in re.findall(r"\b[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){2,3}\b", output):
        parts = name.split()
        if parts[-1].casefold()[:5] in source_stems and sum(p.casefold()[:5] not in source_stems for p in parts[:-1]) >= 2:
            flag(True, "INVENTED_NAME_COMPONENTS", f"В имени добавлены неподтверждённые части: {name}.")
    return tuple(found)
