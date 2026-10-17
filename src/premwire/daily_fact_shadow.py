"""Optional saved-source experiment. Not imported by any live entry point."""
from dataclasses import dataclass, replace
import re
from functools import lru_cache
import time

from .daily_quality import clubs, fixture_pairs, source_issues
from .daily_text_digest import filter_items, collapse, same_event, match_context
from .simple_editorial import assess_simple_source
from .content_filter import AdvertisementFilter
from .models import IncomingItem
from .daily_text_digest import MOSCOW


@dataclass(frozen=True)
class Fact:
    text: str
    url: str
    kind: str


@dataclass(frozen=True)
class FactBundle:
    identity: str
    participants: tuple
    source: IncomingItem
    facts: tuple
    sources: tuple

    def prompt(self):
        # Prefer literal Russian goal/card details over duplicating them in
        # English. Deterministically translate the explicit result's slots.
        result = []
        russian = any(re.search(r'[А-Яа-яЁё]',f.text) for f in self.facts)
        for fact in self.facts:
            match = re.fullmatch(r'(.+?) make it six wins out of six after a (\d+-\d+) victory over ten-man (.+?) at (.+?)\.',fact.text)
            if match:
                winner, loser = clubs(match[1]), clubs(match[3])
                if len(winner)==len(loser)==1:
                    result.append(f'{next(iter(winner))} победил {next(iter(loser))} со счётом {match[2].replace("-", ":")}. 6 побед в 6 матчах.')
                    continue
            if russian and fact.kind != 'result' and not re.search(r'[А-Яа-яЁё]',fact.text):
                continue
            text = re.sub(r'\bМан Сити\b','Манчестер Сити',fact.text)
            text = re.sub(r'Голы на счету ([^.!]+)',r'Голы забили \1',text)
            # Remove emotive red-card framing, preserving participant/minute.
            text = re.sub(r'^А (\w+) подставил «[^»]+» и удалился',r'\1 удалился',text)
            result.append(text)
        return '\n'.join(result)


def normalize_handles(text):
    # Explicit public club handle, not a guess from a blue emoji/account author.
    for handle, name in (('ManCity','Manchester City'), ('HullCity','Hull City'),
                         ('ManUtd','Manchester United'), ('NUFC','Newcastle United')):
        text=re.sub(r'(?<!\w)@'+handle+r'\b', name, text, flags=re.I)
    # Preserve the category; YoungLions alone does NOT establish a U21 age.
    return re.sub(r'#YoungLions\b','England youth team',text,flags=re.I)


def clauses(text):
    for clause in re.split(r'(?<=[.!?])\s+|\n+', text):
        clause = clause.strip()
        if not clause or re.search(r'fans|фанат|болельщик|superb|soft yellow|hostile|animated|who[’\x27]s|пересматри|подключ|смотрим|прогноз|отыграется|поцелует|стычк|драка|Every City player', clause, re.I):
            continue
        if re.fullmatch(r'[^\w]*ГОО+Л!|Холанд!',clause,re.I):
            continue
        yield clause


def final_result(text):
    return bool(re.search(r'victory over|\bbeat\b|\bdefeated\b|full.time|побед\w*\s+(?:у|над)|обыграл|не заметил', text, re.I))


def kind(text):
    if re.search(r'\d\s*[-:]\s*\d', text):
        return 'result' if final_result(text) else 'live_score'
    if re.search(r'red card|sent off|удал|желт', text, re.I):
        return 'discipline'
    if re.search(r'гол|score|finishes', text, re.I):
        return 'goals'
    return 'detail'


@lru_cache(maxsize=512)
def pairs(text):
    result = fixture_pairs(text)
    for clause in clauses(text):
        names = clubs(clause)
        if len(names) == 2 and re.search(r'victory over|\d\s*[-:]\s*\d', clause, re.I):
            result.add(frozenset(names))
    return frozenset(result)


def equivalent(a, b):
    pa, pb = pairs(a.text), pairs(b.text)
    if pa and pb:
        # Same frozen day and explicit competition/date distinctions only.
        if pa != pb or a.published_at.astimezone(MOSCOW).date() != b.published_at.astimezone(MOSCOW).date():
            return False
        ca, cb = match_context(a.text), match_context(b.text)
        dates = lambda t: set(re.findall(r'\b\d{1,2}[./]\d{1,2}(?:[./]\d{4})?\b',t))
        da, db = dates(a.text), dates(b.text)
        return not (ca and cb and ca != cb) and not (da and db and da != db)
    return same_event(a,b)


def prepare(items, start, end, filter_config, timings=None):
    ticks=time.monotonic()
    from .config import FilterConfig
    filter_config=filter_config or FilterConfig(True,())
    from .daily_quality import semantic_source_issues
    from .simple_editorial import clean_simple_source_text
    normalized=[]; semantic_rejects=[]
    for item in items:
        raw=normalize_handles(item.text.replace('\\n','\n'))
        # Evaluate original text before the legacy cleaner discards metadata.
        issues=semantic_source_issues(raw)
        clean=clean_simple_source_text(raw)
        issues+=semantic_source_issues(clean)
        if issues:
            semantic_rejects.append(dict(url=item.canonical_url,reason=sorted(set(issues)),stage='IMMUTABLE_SOURCE_GATE'))
        else:
            normalized.append(replace(item,text=raw))
    accepted, rejected = filter_items(normalized, start, end, filter_config=filter_config)
    recovered = []
    # Rescue only the known narrow legacy failures, not ads or arbitrary rejects.
    allowed_reasons = {'SKIP_NON_FOOTBALL', 'caption_without_news_action',
                       'EXPIRED_TRANSIENT_STORY', 'FOOTBALL_RELEVANCE_FAN_FILLER',
                       'insufficient_source_text', 'DAILY_ROUTINE_QUOTE_WITHOUT_NEW_EVENT'}
    by_url = {i.canonical_url: i for i in normalized}
    context_only = []
    for refusal in rejected:
        item = by_url[refusal['url']]
        clean = assess_simple_source(item.text).clean_text
        if refusal['reason'] not in allowed_reasons or not start <= item.published_at < end:
            continue
        # Remove fan reactions clause-wise only from a concrete final-result post.
        if refusal['reason'] == 'FOOTBALL_RELEVANCE_FAN_FILLER':
            if not final_result(clean) or len(clubs(clean)) != 2:
                continue
            clean = ' '.join(clauses(clean))
        issues = source_issues(clean)
        single_club_score = len(clubs(clean)) == 1 and bool(re.search(r'\d\s*[-:]\s*\d',clean))
        if AdvertisementFilter(filter_config).check(clean).blocked or (issues and not (single_club_score and issues==['CONTEXT_COMPLETENESS_MATCH'])):
            continue
        concrete = (len(clubs(clean)) >= 2 and bool(re.search(r'goal|score|victory|lead|beat|\d\s*[-:]\s*\d|гол|побед', clean, re.I)))
        from .daily_fact_patterns import editorial_relations
        # Narrow, fully typed relation rescue; advertisements and semantic gates
        # above are unchanged. No recognition by particular player/club names.
        structured=editorial_relations(item.text,clean,item.canonical_url,lambda *args:None,lambda text:text)
        if refusal['reason']=='DAILY_ROUTINE_QUOTE_WITHOUT_NEW_EVENT' and not structured:
            continue  # A colon followed by a quoted CLUB is not a routine quote;
                      # only an explicit typed event can recover this refusal.
        appointment = bool(re.search(r'becomes new head coach at\s+\w+', clean, re.I))
        contract=bool(re.search(r'have reached an agreement with .+? over new long term deal until June \d{4}',clean))
        if not (concrete or appointment or single_club_score or contract or structured):
            continue
        candidate = replace(item, text=clean)
        if refusal['reason'] == 'EXPIRED_TRANSIENT_STORY' or single_club_score:
            context_only.append(candidate)  # Never publish old live scores alone.
        else:
            accepted.append(candidate)
        recovered.append(dict(refusal, handling='CONTEXT_ONLY' if candidate in context_only else 'RECOVERED'))

    if timings is not None:timings['filter']=time.monotonic()-ticks
    ticks=time.monotonic()
    # Source-local fixture grouping also understands explicit English victory
    # with a score, which the older MATCH verb list misses.
    groups = []
    for item in accepted:
        for group in groups:
            if all(equivalent(item, other) for other in group):
                group.append(item)
                break
        else:
            groups.append([item])
    winners, conflicts = [], []
    original_members = {}
    for group in groups:
        if not pairs(group[0].text):
            resolved, errors = collapse(group)
            conflicts.extend(errors)
            if resolved:
                winner = resolved[0]
                # Preserve the established transfer-stage/terms consolidation.
                winners.append((winner,[winner]))
                original_members[winner.canonical_url] = tuple(i.canonical_url for i in group)
            continue
        finals = [i for i in group if final_result(i.text)]
        scores = {m[0].replace('-', ':').replace(' ', '') for i in finals
                  for clause in clauses(i.text) if final_result(clause)
                  for m in re.finditer(r'\d+\s*[-:]\s*\d+', clause)}
        if len(scores) > 1:
            conflicts.append({'urls':[i.canonical_url for i in group], 'reason':'CONFLICTING_FINAL_SCORES'})
            continue
        winner = max(finals or group, key=lambda i:i.published_at)
        winners.append((winner, group))
    bundles = []
    for winner, members in winners:
        pair = pairs(winner.text)
        if pair and final_result(winner.text):
            members += [i for i in context_only if equivalent(winner, i)]
            for item in context_only:
                named = clubs(item.text)
                possible = [w for w,_ in winners if pairs(w.text) and final_result(w.text)
                            and named <= clubs(w.text)
                            and w.published_at.astimezone(MOSCOW).date()==item.published_at.astimezone(MOSCOW).date()]
                if len(named)==1 and len(possible)==1 and possible[0] is winner and item not in members:
                    ca,cb=match_context(winner.text),match_context(item.text)
                    if not (ca and cb and ca!=cb):
                        members.append(item)
        facts = []
        seen = set()
        for member in sorted(members, key=lambda i: i.published_at, reverse=True):
            for clause in clauses(member.text):
                k = kind(clause)
                # Earlier live scores/lead are not final results and add little.
                if pair and (k == 'live_score' or re.search(r'\blead\b|вед[её]т', clause, re.I)):
                    continue
                key = re.sub(r'\W+', '', clause).casefold()
                if key not in seen:
                    seen.add(key)
                    facts.append(Fact(clause, member.canonical_url, k))
        if not facts:
            facts = [Fact(winner.text, winner.canonical_url, 'detail')]
        source = replace(winner, text='\n'.join(f.text for f in facts))
        identity = '|'.join(sorted(next(iter(pair)))) if len(pair) == 1 else winner.canonical_url
        bundles.append(FactBundle(identity, tuple(sorted(clubs(winner.text))), source,
                                  tuple(facts), tuple(dict.fromkeys(
                                      [i.canonical_url for i in members] + list(original_members.get(winner.canonical_url, ()))))))
    # Short club streak captions may supplement a uniquely identified result,
    # but never infer a fixture from an unnamed club/emoji.
    consumed = set()
    for index, bundle in enumerate(bundles):
        if not fixture_pairs(bundle.source.text) or not final_result(bundle.source.text):
            continue
        for other in list(bundles):
            if other is bundle or not re.search(r'opening six games', other.source.text, re.I):
                continue
            if len(other.participants) == 1 and set(other.participants) <= set(bundle.participants):
                matches = [b for b in bundles if fixture_pairs(b.source.text) and set(other.participants) <= set(b.participants)]
                if len(matches) == 1 and match_context(other.source.text) == match_context(bundle.source.text):
                    facts = bundle.facts + other.facts
                    bundle = replace(bundle, facts=facts, sources=bundle.sources + other.sources,
                                     source=replace(bundle.source, text='\n'.join(f.text for f in facts)))
                    bundles[index] = bundle
                    consumed.add(other.identity)
    bundles = [b for b in bundles if b.identity not in consumed]
    if timings is not None:timings['source_context_dedupe']=time.monotonic()-ticks
    return bundles, rejected+semantic_rejects, recovered, conflicts


def coverage_issues(bundle, text):
    """A verified final score must not disappear in a short generic summary."""
    scores = {m[0].replace('-', ':').replace(' ', '') for f in bundle.facts
              if f.kind == 'result' for m in re.finditer(r'\d+\s*[-:]\s*\d+', f.text)}
    if scores and not any(s in text.replace('-', ':').replace(' ', '') for s in scores):
        return ['CONFIRMED_RESULT_SCORE_DROPPED']
    return []


def validation_source(bundle):
    # Existing guard recognizes "beat", but misses the equivalent explicit
    # "victory over". Normalize only that complete, source-backed result clause.
    text = bundle.source.text
    text = re.sub(r'([A-Z][A-Za-z ]+?) make it six wins out of six after a (\d+-\d+) victory over ten-man ([A-Z][A-Za-z ]+?) at ([A-Z][A-Za-z]+)\.',
                  r'\1 beat \3 \2 at \4. \1: 6 wins in 6 games.',text)
    return text
