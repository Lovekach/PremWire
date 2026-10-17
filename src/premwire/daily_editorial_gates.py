"""Pure, cutoff-bound editorial gates. No network, model, registry or sender.

SOURCE fidelity is explicitly different from independent confirmation. Unknown
event instants stay empty; a date-only observation is not converted to midnight.
The optional evidence argument accepts only caller-verified structured records.
"""
from dataclasses import replace
from datetime import datetime, timezone, timedelta, date
import hashlib
import json
import re

from .daily_text_digest import MOSCOW, MONTHS
from .daily_fact_digest import VerifiedFact, fact_signature


def instant(value):
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    # An unzoned timestamp is unknown, not silently treated as Moscow or UTC.
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def utc(value):
    parsed = instant(value)
    return parsed.isoformat() if parsed else ''


def family(bundle):
    kind = bundle.event_type
    if kind.startswith('TRANSFER_'): return 'TRANSFER'
    if kind.startswith('CONTRACT_'): return 'CONTRACT'
    if kind in ('MATCH_RESULT', 'MATCH_HALFTIME'): return 'FIXTURE'
    if kind == 'INJURY': return 'INJURY'
    if kind.startswith('APPOINTMENT'): return 'APPOINTMENT'
    return kind


def relation(bundle):
    """Exact SOURCE slots only. Ambiguous directions must never be guessed."""
    kind = family(bundle)
    if kind == 'TRANSFER':
        for f in bundle.verified_facts:
            if f.key in ('transfer', 'transfer_subject') and len(f.values) == 4 and f.values[2]:
                return kind, f.values[0].casefold(), f.values[2].casefold()
            if f.key.endswith('reported_interest'):
                # Two-club full relation: player, origin, destination, provider,
                # status. Short form: player, destination, provider, status.
                if len(f.values) == 5: return kind, f.values[0].casefold(), f.values[2].casefold()
                if len(f.values) == 4: return kind, f.values[0].casefold(), f.values[1].casefold()
            if f.key == 'player_contact': return kind, f.values[0].casefold(), f.values[1].casefold()
        return kind, tuple(p.casefold() for p in bundle.participants)
    if kind == 'FIXTURE':
        competition = next((f.values for f in bundle.verified_facts if f.key == 'competition'), ())
        return kind, tuple(sorted(p.casefold() for p in bundle.participants[:2])), competition, bundle.event_date
    if kind in ('CONTRACT', 'APPOINTMENT'):
        return kind, tuple(p.casefold() for p in bundle.participants[:2])
    if kind == 'INJURY':
        # Separate diagnoses are separate episodes, even for the same person.
        diagnosis = tuple(v for f in bundle.verified_facts for v in f.values
                          if v in ('KNEE', 'HAMSTRING', 'ANKLE', 'ACL'))
        return kind, tuple(p.casefold() for p in bundle.participants), diagnosis
    return kind, tuple(sorted(p.casefold() for p in bundle.participants))


def canonical(bundle):
    identity=relation(bundle)
    if family(bundle)=='FIXTURE' and not bundle.event_date:
        # A SOURCE report window is an identity fallback, NOT an invented
        # fixture date. Otherwise every undated rematch would share one ID.
        reported=instant(bundle.source_published_at)
        identity=(identity,'SOURCE_REPORT_WINDOW',reported.astimezone(MOSCOW).date().isoformat() if reported else '')
    if family(bundle)=='INJURY':
        # Do not give every knee injury in an entire career the same episode ID.
        identity=(identity,bundle.event_date or bundle.source_published_at[:10])
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def stage(bundle):
    kind = bundle.event_type
    if kind in ('TRANSFER_OFFICIAL', 'CONTRACT_SIGNED', 'CONTRACT_EXTENSION', 'APPOINTMENT', 'MATCH_RESULT'): return 5
    if kind == 'INJURY':
        return 4 if any('SURGERY_PERFORMED' in f.values or 'CONFIRMED' in f.values for f in bundle.verified_facts) else 2
    if kind in ('TRANSFER_AGREEMENT', 'CONTRACT_AGREEMENT'): return 3
    if kind in ('TRANSFER_TALKS', 'TRANSFER_CONTACT'): return 2
    if kind == 'MATCH_HALFTIME': return 1
    return 1


def substantive(bundle):
    return {fact_signature(f) for f in bundle.verified_facts
            if f.key not in ('report_date', 'match_date', 'competition', 'attribution')}


def freshness_gate(bundles, inputs, cutoff, history=()):
    by_url = {i.canonical_url: i for i in inputs}
    day = (cutoff-timedelta(microseconds=1)).astimezone(MOSCOW).date()
    result, decisions = [], []
    for b in bundles:
        sources = [by_url[u] for u in b.source_refs if u in by_url]
        if not sources: raise ValueError('Missing SOURCE clock')
        times = [instant(s.published_at) for s in sources]
        if any(t is None or t >= instant(cutoff) for t in times):
            decisions.append(dict(event_id=b.event_id, sources=b.source_refs, decision='REJECT', category='DATE_UNKNOWN', reason='SOURCE_AFTER_CUTOFF_OR_UNZONED'))
            continue
        # A second SOURCE repeating an existing slot is not a new development.
        # Use the earliest observation of each identical substantive fact; a
        # genuinely new slot may advance the report clock. Preserve the latest
        # SOURCE clock separately for diagnostics.
        slot_times = {}
        for f in b.verified_facts:
            if f.key in ('match_date','report_date','competition','attribution'):continue
            stamps = [instant(by_url[u].published_at) for u in f.source_refs if u in by_url]
            if stamps:slot_times.setdefault(fact_signature(f),[]).extend(stamps)
        reported = max(min(stamps) for stamps in slot_times.values()) if slot_times else max(times)
        source_day = reported.astimezone(MOSCOW).date()
        event_day = date.fromisoformat(b.event_date) if b.event_date else None
        age = (day - (event_day or source_day)).days
        source_age = (day - source_day).days
        status = 'FULL_TIME' if b.event_type == 'MATCH_RESULT' else 'HALF_TIME' if b.event_type == 'MATCH_HALFTIME' else ''
        b = replace(b, source_published_at=utc(max(times)), source_id=','.join(sorted({s.source_id for s in sources})),
                    source_collected_at=max((utc(getattr(s, 'source_collected_at', '')) for s in sources), default=''),
                    first_seen_at=min((utc(getattr(s, 'first_seen_at', '')) for s in sources if getattr(s, 'first_seen_at', '')), default=''),
                    last_seen_at=max((utc(getattr(s, 'last_seen_at', '')) for s in sources), default=''),
                    match_status=status, freshness_confidence='EVENT_DATE_CONFIRMED' if event_day else 'SOURCE_REPORT_ONLY')
        ident = canonical(b)
        previous = [h for h in history if relation(h) == relation(b)]
        if family(b)=='FIXTURE' and not b.event_date:
            core={f.values for f in b.verified_facts if f.key=='result'}
            previous=[h for h in previous if (core and core=={f.values for f in h.verified_facts if f.key=='result'}) or
                      (instant(h.source_published_at) and
                       abs((reported.astimezone(MOSCOW).date()-instant(h.source_published_at).astimezone(MOSCOW).date()).days)<=1)]
        if family(b)=='INJURY':
            previous=[h for h in previous if instant(h.source_published_at) and abs((reported-instant(h.source_published_at)).total_seconds())<=7*86400]
        old = max(previous, key=lambda h: (stage(h), h.source_published_at), default=None)
        reason = ''
        if old and substantive(b) <= set().union(*(substantive(h) for h in previous)):
            reason = 'EVENT_ALREADY_PUBLISHED_NO_MATERIAL_UPDATE'
        elif old and stage(b) < stage(old):
            reason = 'EVENT_REGRESSIVE_STATUS_AFTER_PUBLICATION'
        if old and b.event_type=='MATCH_RESULT' and old.event_type=='MATCH_RESULT' and not reason:
            reason='FINAL_FIXTURE_ALREADY_PUBLISHED_NO_CONTEXT_REPOST'
        prior_slots=set().union(*(substantive(h) for h in previous)) if previous else set()
        material_keys={'fee','contract','contract_agreement','extension','purchase_option','loan_terms','return_date','recovery','diagnosis'}
        material_change=any(f.key in material_keys and fact_signature(f) not in prior_slots for f in b.verified_facts)
        # Same-person unrelated injuries without a bound episode are not linked.
        updated = bool(old and not reason and (stage(b) > stage(old) or material_change or family(b) not in ('TRANSFER', 'CONTRACT', 'INJURY')))
        if old and not updated and not reason:
            reason = 'EVENT_NO_CONFIRMED_STATUS_ADVANCE'
        if old:
            ident = old.canonical_event_id or canonical(old)
        category = 'UPDATED_STORY' if updated else 'FRESH_TODAY' if source_age == 0 and age == 0 else 'RECENT_UNPUBLISHED'
        limits = {'TRANSFER_INTEREST': 0, 'TRANSFER_PLANS': 0, 'TRANSFER_CONTACT': 0, 'TRANSFER_TALKS': 0,
                  'TRANSFER_CONDITIONAL_SALE': 0, 'MATCH_RESULT': 1, 'MATCH_HALFTIME': 0,
                  'INJURY': 1, 'DISCIPLINE': 1, 'TRANSFER_OFFICIAL': 2, 'TRANSFER_ANNOUNCEMENT': 2,
                  'CONTRACT_AGREEMENT': 2, 'CONTRACT_EXTENSION': 2, 'APPOINTMENT': 2}
        limit = limits.get(b.event_type, 1)
        if age > limit or source_age > limit or age < 0:
            category = 'STALE'; reason = reason or 'FRESHNESS_EVENT_TYPE_AGE_POLICY'
        if b.event_type == 'MATCH_HALFTIME' and instant(cutoff) - reported > timedelta(hours=2):
            category = 'STALE'; reason = reason or 'IN_PLAY_SNAPSHOT_EXPIRED'
        # Important old but unprinted news may survive, never masquerade as today.
        if category == 'RECENT_UNPUBLISHED' and b.importance < 60:
            reason = reason or 'RECENT_UNPUBLISHED_LOW_EDITORIAL_VALUE'
        if not event_day and source_age > 0 and category != 'STALE':
            category = 'DATE_UNKNOWN' if not updated else category
        if b.event_type=='MATCH_RESULT' and not event_day and category!='STALE':
            category='DATE_UNKNOWN'
        b = replace(b, canonical_event_id=ident, freshness_category=category,
                    last_published_at=old.last_published_at if old else '')
        if not reason and (source_age > 0 or b.event_type=='MATCH_RESULT' and not event_day) and not any(f.key in ('report_date', 'match_date') for f in b.verified_facts):
            source = max(sources, key=lambda s:s.published_at)
            wording = f'Об этом сообщалось {source_day.day} {MONTHS[source_day.month-1]}.'
            if b.event_type=='MATCH_RESULT':
                wording=f'О результате сообщено {source_day.day} {MONTHS[source_day.month-1]}; дата матча в источнике не уточнена.'
            fact = VerifiedFact('report_date', (source_day.isoformat(), 'PUBLICATION_NOT_EVENT_DATE'),
                                (source.canonical_url,), source.text, (wording, wording))
            b = replace(b, verified_facts=b.verified_facts+(fact,), essential_facts=b.essential_facts+('report_date',))
        decisions.append(dict(event_id=b.event_id, canonical_event_id=ident, sources=b.source_refs,
                              decision='REJECT' if reason else 'KEEP', category=category,
                              reason=reason or 'CUTOFF_BOUND_SOURCE_AND_EVENT_CLOCKS', source_published_at=b.source_published_at,
                              development_reported_at=utc(reported),
                              event_date=b.event_date, confidence=b.freshness_confidence))
        if not reason: result.append(b)
    return result, decisions


def stat_scope(observation):
    # Snapshot timestamps describe observations, not different competitions.
    period = re.sub(r'(TABLE_(?:GAP_)?AT_SOURCE_|SOURCE_SNAPSHOT_).*', r'\1', observation.period)
    return observation.metric, observation.unit, observation.subject.casefold(), period


def fixed_period(period):
    return bool(re.search(r'FIRST_\d+_|_OF_\d+_|SOURCE_REPORTED_\d+_|^\d+_SOURCE_REPORTED_MATCHES$|REPORT|FIRST_HOME_WIN|HALF|SOURCE_FIXTURE', period))


def statistics_gate(bundles, cutoff, evidence=(), inputs=()):
    """Never compute a new total. Unresolved comparable conflicts fail closed.

    Later *comparable* SOURCE observations supersede earlier observations only
    when their clocks differ; equal-time contradictions reject both. Historical
    fixed windows are not tested as live totals. External evidence must be
    independent, exactly scoped and available by cutoff, not a mirror/retrieval.
    """
    rows=[];sources={i.canonical_url:i for i in inputs}
    for b in bundles:
        for f in b.verified_facts:
            for s in f.statistics:
                observed=instant(sources[s.source].published_at) if s.source in sources else instant(b.source_published_at)
                rows.append((b,f,s,observed))
    decisions=[]; rejected=set(); omitted={}
    for b,f,s,observed in rows:
        status='SOURCE_VERIFIED'; reason='SOURCE_FIDELITY_ONLY_NOT_INDEPENDENT_CONFIRMATION'
        if not observed or observed >= instant(cutoff) or not s.period or s.issues():
            status='UNVERIFIED';reason='MISSING_VALID_STAT_SCOPE_OR_OBSERVATION_DATE'
        else:
            minimum_recheck=observed
            comparable=[r for r in rows if stat_scope(r[2])==stat_scope(s) and r[0].event_id!=b.event_id and r[3] and r[3]>=observed]
            conflicting=[r for r in comparable if r[2].value!=s.value]
            if comparable:minimum_recheck=max([observed]+[r[3] for r in comparable])
            if conflicting:
                status='STALE' if any(r[3]>observed for r in conflicting) else 'CONFLICTING'
                reason='NEWER_COMPARABLE_SOURCE' if status=='STALE' else 'SAME_CUTOFF_COMPARABLE_CONFLICT'
            elif not fixed_period(s.period) and observed.astimezone(MOSCOW).date()<(cutoff-timedelta(microseconds=1)).astimezone(MOSCOW).date():
                status='STALE';reason='DYNAMIC_STAT_NEEDS_CURRENT_CONFIRMATION'
            for other in bundles:
                # A later relevant final match invalidates an earlier live
                # aggregate; it does NOT tell us how much to add to the total.
                if fixed_period(s.period) or other.event_type!='MATCH_RESULT' or not other.source_published_at: continue
                if other.event_date and date.fromisoformat(other.event_date)<observed.astimezone(MOSCOW).date():continue
                common=any(p.casefold() in s.subject.casefold().split(' / ') for p in other.participants)
                competition=next((v.values[0] for v in other.verified_facts if v.key=='competition'), '')
                compatible=('CHAMPIONS_LEAGUE' in s.period and competition=='CHAMPIONS_LEAGUE')
                compatible|=('LEAGUE' in s.period and 'CHAMPIONS' not in s.period and competition=='PREMIER_LEAGUE')
                if common and compatible and instant(other.source_published_at)>observed:
                    status='STALE';reason='LATER_RELEVANT_MATCH_REQUIRES_RECHECK'
                    minimum_recheck=max(minimum_recheck,instant(other.source_published_at))
            external=[e for e in evidence if e.get('scope')==stat_scope(s) and e.get('independent') is True
                      and instant(e.get('available_at')) and instant(e['available_at'])<instant(cutoff)
                      and instant(e.get('observed_at')) and instant(e['observed_at'])>=minimum_recheck
                      and (not conflicting or instant(e['observed_at'])>minimum_recheck)]
            if external:
                latest=max(external,key=lambda e:instant(e['observed_at']))
                if str(latest.get('value'))==s.value:
                    status='EXTERNALLY_CONFIRMED';reason='EXACT_COMPARABLE_INDEPENDENT_OBSERVATION'
                else:
                    status='CONFLICTING';reason='INDEPENDENT_VALUE_DIFFERS_NO_ARITHMETIC_REPAIR'
        if status in ('STALE','CONFLICTING','UNVERIFIED'):
            if f.key in b.essential_facts:
                rejected.add(b.event_id)
            else:
                omitted.setdefault(b.event_id,set()).add(f.key)
        decisions.append(dict(event_id=b.event_id, fact=f.key, metric=s.metric, value=s.value, period=s.period,
                              subject=s.subject, observed_at=utc(observed), source=s.source,
                              status=status, decision='REJECT' if b.event_id in rejected else 'OMIT_OPTIONAL_CONTEXT' if f.key in omitted.get(b.event_id,set()) else 'KEEP', reason=reason))
    # Essential slots are never dropped. Optional stale table context must not
    # suppress the independent, confirmed result/goals/red card of a match.
    return [replace(b,verified_facts=tuple(f for f in b.verified_facts if f.key not in omitted.get(b.event_id,set())))
            for b in bundles if b.event_id not in rejected], decisions


def editorial_rank(bundles):
    """Transparent soft scores and diversity; significance may beat category."""
    base={'MATCH_RESULT':28,'TRANSFER_OFFICIAL':25,'APPOINTMENT':24,'INJURY':21,'DISCIPLINE':20,
          'CONTRACT_EXTENSION':21,'CONTRACT_AGREEMENT':17,'PLAYER_STATS':13,'CLUB_STATS':12,
          'TRANSFER_INTEREST':6,'TRANSFER_CONTACT':8,'MATCH_HALFTIME':4}
    remaining=list(bundles); result=[]; decisions=[]; topics={};actors={}
    while remaining:
        scored=[]
        for b in remaining:
            text=' '.join(f.evidence for f in b.verified_facts)
            competition=12 if re.search(r'Лиг[аеуы] чемпионов|Champions League|Premier League|АПЛ',text,re.I) else 6 if re.search(r'Лиг[аеуы] Европы|Europa League',text,re.I) else 0
            # Audience/club signal is coverage context, not a mandatory registry.
            audience=6 if competition or re.search(r'РПЛ|сборн\w+ России|Russian Premier',text,re.I) else 0
            credibility=8 if re.search(r'ОФИЦИАЛЬНО|official|Фабрицио Романо|UEFA|Fabrizio Romano',text,re.I) else 2
            fresh={'FRESH_TODAY':32,'UPDATED_STORY':34,'RECENT_UNPUBLISHED':-6,'DATE_UNKNOWN':-12}.get(b.freshness_category,0)
            theme=family(b); diversity=-min(topics.get(theme,0)*8,32)
            repetition=-min(max((actors.get(p.casefold(),0) for p in b.participants), default=0)*6,18)
            components=dict(importance=b.importance//2,event_value=base.get(b.event_type,12),freshness=fresh,
                            competition=competition,audience=audience,credibility=credibility,
                            confirmed_details=min(len(b.essential_facts)*2,10),novelty=5 if b.freshness_category=='UPDATED_STORY' else 0,
                            topic_diversity=diversity,actor_diversity=repetition)
            scored.append((sum(components.values()),b.event_id,b,components))
        score,_,b,components=min(scored,key=lambda r:(-r[0],r[1]))
        result.append(b);remaining.remove(b);theme=family(b);topics[theme]=topics.get(theme,0)+1
        for p in b.participants:actors[p.casefold()]=actors.get(p.casefold(),0)+1
        decisions.append(dict(event_id=b.event_id,rank=len(result),score=score,components=components,category=b.freshness_category))
    return result,decisions
