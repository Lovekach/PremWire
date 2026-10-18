"""Cutoff-bound match coverage diagnostics; never invent a calendar or scores.

No network, sender, credentials or registry gate. A missing licensed calendar
means UNKNOWN, not zero fixtures and not a reason to suppress safe other news.
Only a transport-confirmed complete competition snapshot may prove a denominator.
"""
from datetime import datetime, time, timedelta
import re

P0=('PREMIER_LEAGUE','CHAMPIONS_LEAGUE','EUROPA_LEAGUE')
P1=('LA_LIGA','SERIE_A','BUNDESLIGA','LIGUE_1')
COMPETITIONS={'Premier League':'PREMIER_LEAGUE','UEFA Champions League':'CHAMPIONS_LEAGUE',
              'UEFA Europa League':'EUROPA_LEAGUE','La Liga':'LA_LIGA','Serie A':'SERIE_A',
              'Bundesliga':'BUNDESLIGA','Ligue 1':'LIGUE_1'}


def _instant(value):
    try:
        stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError,TypeError):
        return None


def source_gaps(inputs, selected, audit):
    """Explain missing text evidence, not a count of independently known games."""
    represented={url for b in selected for url in b.source_refs}
    rows=[]
    for item in inputs:
        if item.canonical_url in represented:continue
        text=item.text
        if not re.search(r'матч|побед|сч[её]т|проигр|уступ|выигр|ничь|три очка',text,re.I):continue
        if not re.search(r'«[^»]+»',text):continue
        stages=[]
        for stage in ('rejected','unsupported','conflicts','editorial_freshness','freshness_gate','final_digest_gate','match_enrichment'):
            for row in audit.get(stage,()):
                refs=row.get('sources',row.get('urls',(row.get('url'),)))
                if item.canonical_url in refs:
                    stages.append(dict(stage=stage,reason=row.get('reason')))
        reason='PARSER_UNSUPPORTED'
        detail='UNSUPPORTED_RESULT_OR_CONTEXT_FORMAT'
        if any(r['stage']=='rejected' for r in stages):reason='FACT_BUNDLE_REJECTED';detail='SOURCE_FILTER_REJECTED'
        elif re.search(r'откры[лт]\w* сч[её]т|во втором тайме|начало трансляции|сегодня.*попробует',text,re.I):
            detail='LIVE_OR_PREVIEW_NOT_PROOF_OF_FULL_TIME'
        elif not re.search(r'\b\d{1,2}\s*[:-]\s*\d{1,2}\b',text):
            detail='EXPLICIT_FINAL_SCORE_MISSING'
        rows.append(dict(source=item.canonical_url,source_published_at=item.published_at.isoformat(),
                         reason=reason,detail=detail,stages=stages))
    return rows


def assess_match_coverage(inputs, selected, audit, cutoff, *, calendar=None):
    """calendar: immutable observed snapshot, not an assumed league schedule.

    Normalized fixture rows follow daily_match_data.normalize_fixture. An
    Only exact API binding or an already-verified fixture identity can associate
    a result. Unknown dates
    and informal team-name guesses cannot count as successful coverage.
    """
    gaps=source_gaps(inputs,selected,audit)
    result=dict(status='COVERAGE_UNKNOWN',requested_competitions=list(P0+P1),
                complete_competitions=[],scheduled_matches=None,finished_before_cutoff=None,
                confirmed_results=None,included_results=None,missing_results=None,
                source_result_count=sum(b.event_type=='MATCH_RESULT' for b in selected),
                reason='NO_ACCESSIBLE_COMPLETE_MATCH_CALENDAR',fixtures=[],source_gaps=gaps,
                does_not_block_publication=True)
    if not isinstance(calendar,dict) or not calendar:return result
    observed=_instant(calendar.get('available_at'))
    if not observed or observed>=cutoff:
        result['reason']='CALENDAR_UNAVAILABLE_AT_HISTORICAL_CUTOFF';return result
    if (not isinstance(calendar.get('fixtures',()),(tuple,list)) or
        not isinstance(calendar.get('complete_competitions',()),(tuple,list,set)) or
        not isinstance(calendar.get('requested_competitions',P0+P1),(tuple,list,set))):
        result['reason']='INVALID_CALENDAR_SCHEMA';return result
    complete=set(calendar.get('complete_competitions',())) & set(P0+P1)
    requested=set(calendar.get('requested_competitions',P0+P1))
    from .daily_text_digest import MOSCOW
    midnight=datetime.combine(cutoff.astimezone(MOSCOW).date(),time.min,MOSCOW)
    scope_start=_instant(calendar.get('window_start'))
    scope_end=_instant(calendar.get('window_end'))
    window_complete=bool(scope_start and scope_end and scope_start<=midnight and scope_end>=cutoff)
    rows=[];seen={};invalid=False;conflicting=set();canonical_rows={}
    for row in calendar.get('fixtures',()):
        if not isinstance(row,dict):invalid=True;continue
        key=str(row.get('fixture_id',''));available=_instant(row.get('available_at'))
        kickoff=_instant(row.get('kickoff_at'))
        ids=(row.get('home_id'),row.get('away_id'))
        valid_ids=all(type(v) is int and v>0 for v in ids)
        if not key or not available or available>=cutoff or not kickoff or not valid_ids or ids[0]==ids[1]:
            invalid=True;continue
        if not all(isinstance(row.get(k),str) and row[k].strip() for k in ('home','away','event_date','source')):
            invalid=True;continue
        if row['event_date']!=kickoff.astimezone(MOSCOW).date().isoformat():invalid=True;continue
        if key in seen:
            if seen[key]!=row:invalid=True;conflicting.add(key)
            continue
        seen[key]=row
        competition=COMPETITIONS.get(row.get('competition'))
        if competition not in requested:invalid=True;continue
        if not midnight<=kickoff<midnight+timedelta(days=1):continue  # today's Moscow calendar only
        # A fixture starting after cutoff cannot already be final at cutoff.
        status=row.get('status')
        if status not in ('FULL_TIME','LIVE','HALF_TIME','SCHEDULED','POSTPONED','CANCELLED'):
            invalid=True;continue
        if status=='FULL_TIME' and kickoff>=cutoff:invalid=True;continue
        score=row.get('score',())
        confirmed=(status=='FULL_TIME' and isinstance(score,(list,tuple)) and len(score)==2 and all(type(n) is int and n>=0 for n in score))
        if status=='FULL_TIME' and not confirmed:invalid=True
        canonical=(competition,tuple(sorted(ids)),kickoff.isoformat())
        if canonical in canonical_rows:
            previous=canonical_rows[canonical]
            if previous.get('score',())!=score or previous['status']!=status:
                invalid=True;conflicting.update((key,str(previous['fixture_id'])))
            continue
        canonical_rows[canonical]=row
        included=False
        if confirmed:
            for b in selected:
                if b.event_type!='MATCH_RESULT':continue
                # Existing binder checks exact pair, explicit date, competition
                # and score. Never count a loose surname or shared club token.
                from .daily_match_data import bind_fixture
                bound,_=bind_fixture(b,[row])
                if bound:included=True;break
        editorial_refs={url for d in audit.get('editorial_v2',()) if d.get('decision')=='EXCLUDE' for url in d.get('sources',())}
        excluded=confirmed and row.get('source') in editorial_refs
        reason=('INCLUDED' if included else 'EDITORIALLY_EXCLUDED' if excluded else 'NOT_COLLECTED' if confirmed else
                'SCHEDULED_AFTER_CUTOFF' if kickoff>=cutoff else 'UNVERIFIED')
        rows.append(dict(fixture_id=key,competition=row.get('competition'),status=status,
                         confirmed=confirmed,included=included,reason=reason,source=row.get('source')))
    rows=[r for r in rows if r['fixture_id'] not in conflicting]
    finals=[r for r in rows if r['confirmed']]
    missing=[r for r in finals if not r['included']]
    # Partial calendars may establish a lower bound, never an all-leagues total.
    scope_complete=bool(complete) and bool(requested) and requested<=complete and window_complete and not invalid and not calendar.get('errors')
    result.update(status='COVERAGE_OK' if scope_complete and not missing else 'COVERAGE_PARTIAL',
                  reason='CALENDAR_SCOPE_COMPLETE' if scope_complete else 'CALENDAR_SCOPE_INCOMPLETE',
                  requested_competitions=sorted(requested),complete_competitions=sorted(complete),
                  scheduled_matches=len(rows) if scope_complete else None,
                  finished_before_cutoff=len(finals) if scope_complete else None,
                  confirmed_results=len(finals),included_results=len(finals)-len(missing),missing_results=len(missing),
                  confirmed_results_are_lower_bound=not scope_complete,fixtures=rows)
    result['editorially_excluded_results']=sum(r['reason']=='EDITORIALLY_EXCLUDED' for r in rows)
    if len(finals)>=2 and len(missing)==len(finals) and any(r['reason']!='EDITORIALLY_EXCLUDED' for r in missing):
        result.update(status='COVERAGE_CRITICAL',reason='CONFIRMED_SIGNIFICANT_FINALS_ABSENT')
    return result
