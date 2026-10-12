"""Source-local daily gates; no external facts or inferred fixture context."""
import re
from .russian_editorial import club_entries

QUALITY_REVISION = 'daily-quality-20261012-v1'
PROMO = r'counter[- ]?strike|\bcs\s*2\b|киберспорт|\be[- ]?sports\b|video games|видеоигр|компьютерн\w*\s+игр|giveaway|raffle|розыгрыш|конкурс|FORCE BUY|УЧАСТВУЮ|подписаться|подпишитесь|подписывайтесь|subscribe to|промокод|рекламн\w*\s+интеграц'
MATCH = r'\b(?:матч\w*|сч[её]т|побед\w*|выиграл\w*|уступил\w*|забил\w*|гол\w*|equalis\w*|lead|beat|score|half|red card)\b'

def clubs(text):
    found = set()
    for entry in club_entries():
        forms = (entry.display, *entry.aliases, *entry.forms.values())
        if any(re.search(r'(?<!\w)' + re.escape(f) + r'(?!\w)', text, re.I) for f in forms if f):
            found.add(entry.display)
    if re.search(r'\bМЮ\b', text):
        found.add('Манчестер Юнайтед')
    if re.search(r'\bМан\s+Сити\b', text, re.I):
        found.add('Манчестер Сити')
    # Explicit source-local sides also work for clubs absent from the alias
    # table. That table normalizes spelling; it is NOT a publication whitelist.
    patterns = (
        r'[«"]([^»"\n]{2,40})[»"]\s*(?:vs|—|против)\s*[«"]([^»"\n]{2,40})[»"]',
        r'([A-Z][A-Za-z ]{2,35}?)\s+(?:beat|defeated|lead(?:\s+10-man)?|lost to)\s+([A-Z][A-Za-z ]{2,35}?)(?=\s+at\b|\s+\d|[.!?]|$)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            for side in match.groups():
                # Recursion terminates: each captured side contains no fixture
                # separator or result verb, only a literal source name.
                found.update(clubs(side) or {side.strip()})
    return found

def fixture_pairs(text):
    if re.search(r'последних соперников|в первых.*турах|в этом сезоне|this season|career|травм|injur', text, re.I):
        return set()  # Season records and medical events aren't one fixture.
    pairs = set()
    for clause in re.split(r'[.!?\n]+|,?\s+а\s+', text):
        names = clubs(clause)
        if len(names) == 2 and re.search(MATCH + r'|дожать|не заметил', clause, re.I):
            pairs.add(frozenset(names))
    return pairs

def source_issues(text):
    if re.search(PROMO, text, re.I):
        return ['FOOTBALL_RELEVANCE_PROMOTION']
    if re.search(r'fans screaming|фанаты.*скандир|болельщики.*скандир', text, re.I):
        return ['FOOTBALL_RELEVANCE_FAN_FILLER']
    if re.search(r'следит за (?:шестью|\d+) игроками', text, re.I) and not re.search(r'игроками\s*[:—]', text):
        return ['CONTEXT_UNNAMED_TARGETS']
    # Live commentary without two explicit sides cannot become a standalone
    # evening result. Career records and injury bulletins are independent news.
    live = re.search(r'\d+(?:th|st|nd|rd)? minute|хозяева|соперники|at the break|first goal.*levels|lead.*10-man|сравнял\w*\s+сч[её]т', text, re.I)
    if live and len(clubs(text)) < 2:
        return ['CONTEXT_COMPLETENESS_MATCH']
    if re.search(r'во главе нового клуба', text, re.I) and not clubs(text):
        return ['CONTEXT_UNNAMED_CLUB']
    if re.search(r'opening six games|команда одержала|шесть побед в первых', text, re.I) and not clubs(text):
        return ['CONTEXT_UNNAMED_CLUB']
    if re.search(r'familiar face returns|возвращается на.*в качестве главного тренера', text, re.I):
        return ['EDITORIAL_RETURN_TEASER_WITHOUT_APPOINTMENT']
    return []

def relation_issues(source, text):
    issues = []
    def route(value):
        m = re.search(r'(?:из|from)\s+(.{2,50}?)\s+(?:в|to)\s+([^.!?\n]{2,50})', value, re.I)
        reverse = False
        if not m:
            m = re.search(r'(?:в|to)\s+(.{2,50}?)\s+(?:из|from)\s+([^.!?\n]{2,50})', value, re.I)
            reverse = True
        if m:
            origin, destination = clubs(m[1]), clubs(m[2])
            if reverse:
                origin, destination = destination, origin
            if len(origin) == len(destination) == 1:
                return next(iter(origin)), next(iter(destination))
        return None
    before, after = route(source), route(text)
    if before and after and before != after:
        issues.append('ENTITY_RELATIONS_TRANSFER_ROUTE')
    # Compare explicit Russian subject/object relations, not bags of names.
    relation = r'([А-ЯЁ][А-Яа-яЁё ]{2,35}?)\s+(победил|обыграл|выиграл у|уступил)\s+([А-ЯЁ][А-Яа-яЁё ]{2,35})(?=[,.!]|$)'
    def outcomes(value):
        result = set()
        for m in re.finditer(relation, value):
            left, right = clubs(m[1]), clubs(m[3])
            if len(left) == len(right) == 1:
                winner, loser = (right, left) if m[2] == 'уступил' else (left, right)
                result.add((next(iter(winner)), next(iter(loser))))
        for m in re.finditer(r'([A-Z][A-Za-z ]{1,35}?)\s+(beat|defeated|lost to)\s+([A-Z][A-Za-z ]{1,35})', value):
            left, right = clubs(m[1]), clubs(m[3])
            if len(left) == len(right) == 1:
                winner, loser = (right, left) if m[2] == 'lost to' else (left, right)
                result.add((next(iter(winner)), next(iter(loser))))
        return result
    old, new = outcomes(source), outcomes(text)
    if old and any((loser, winner) in new for winner, loser in old):
        issues.append('ENTITY_RELATIONS_WINNER_REVERSED')
    if re.search(r'\blead\b|вед[её]т', source, re.I) and not re.search(r'full.time|final|побед|выиграл', source, re.I) and re.search(r'побед|выиграл', text, re.I):
        issues.append('FACTUAL_MATCH_STATUS_UPGRADED')
    if re.search(r'подставил', source, re.I) and re.search(r'подставил\s+соперника', text, re.I):
        issues.append('ENTITY_RELATIONS_RED_CARD_SIDE')
    if re.search(r'\bErling\b', source) and re.search(r'\bЭрих\b|\bЭрилинг\b', text):
        issues.append('LANGUAGE_QUALITY')
    # Preserve the explicitly named side playing with ten men.
    match = re.search(r'10-man\s+([A-Z][A-Za-z ]+?)\s+(?:at|in|after|$)', source)
    if match:
        reduced = clubs(match[1])
        for sentence in re.split(r'[.!?]', text):
            if re.search(r'вдесятером|в меньшинстве', sentence, re.I) and len(clubs(sentence)) == 1 and clubs(sentence) != reduced:
                issues.append('ENTITY_RELATIONS_RED_CARD_SIDE')
    if re.search(r'\b(?:хозяев\w*|гост[ьи]\w*)\b', text, re.I) and not re.search(r'\bhome|\baway|хозяев\w*|гост[ьи]\w*', source, re.I):
        issues.append('ENTITY_RELATIONS_HOME_AWAY')
    if re.search(r'\bТюрамини\b|\bЭрилинг\b|При каждом завершении передачи|демонстрируют такую закономерность|забив голы на счету', text, re.I):
        issues.append('LANGUAGE_QUALITY')
    if re.search(r'результат стал (?:важным|значимым) достижением|стал важным шагом|матч завершился со сч[её]том в пользу', text, re.I):
        issues.append('LANGUAGE_GENERIC_FILLER')
    for name in clubs(text):
        if re.search(r'^«?' + re.escape(name) + r'»?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+в\b', text):
            issues.append('LANGUAGE_UNRESOLVED_ROLE')
    if len(re.findall(r'\bИсточник\b\s*(?::|[A-ZА-ЯЁ])', text)) > 1:
        issues.append('LANGUAGE_DUPLICATE_CREDIT')
    return issues

def final_issues(source, text):
    return sorted(set(source_issues(source) + source_issues(text) + relation_issues(source, text)))
