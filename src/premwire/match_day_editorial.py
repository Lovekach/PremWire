"""Cutoff-bound non-final fixtures and narrow reusable SOURCE relations.

No network, guessed results, lineup inference from pictures or free prose.
"""
from datetime import datetime,timedelta
import hashlib,json,re
from .daily_fact_digest import ImmutableBundle,VerifiedFact
from .daily_text_digest import MOSCOW,MONTHS

PERSON=r'[А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+){1,2}'
MONTH=r'(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)'

class EditionQualityError(ValueError):
    def __init__(self,diagnostics):
        super().__init__('EDITORIAL_EDITION_QUALITY_BLOCKED')
        self.diagnostics=diagnostics

def nonfinal(item,cutoff):
    from .football_data import normalize,instant,COMPETITIONS
    from .editorial_v2 import team_name
    raw=json.loads(item.text);row=normalize(raw,item.published_at.isoformat(),cutoff)
    if raw['score']['duration']!='REGULAR':raise ValueError('NONFINAL_PERIOD_UNSUPPORTED')
    status=row['status'];kick=instant(raw['utcDate']);observed=instant(item.published_at)
    if row['event_date']!=cutoff.astimezone(MOSCOW).date().isoformat():raise ValueError('NONFINAL_NOT_TODAY')
    if status=='SCHEDULED':
        if kick<=instant(cutoff):raise ValueError('STALE_SCHEDULED_KICKOFF')
        if any(v is not None for v in raw['score']['fullTime'].values()) or raw['score']['winner'] is not None:
            raise ValueError('SCHEDULED_SCORE_CONTRADICTION')
        kind='MATCH_SCHEDULED'
    elif status in ('LIVE','HALF_TIME'):
        if kick>=observed or observed-instant(raw['lastUpdated'])>timedelta(minutes=20):raise ValueError('STALE_LIVE_SNAPSHOT')
        if any(type(v) is not int or not 0<=v<=99 for v in row['score']):raise ValueError('INVALID_LIVE_SCORE')
        kind='MATCH_LIVE'
    else:raise ValueError('NONFINAL_STATUS_NOT_SUPPORTED')
    values=(status,team_name(raw['homeTeam']),team_name(raw['awayTeam']),COMPETITIONS[raw['competition']['code']][2],
            kick.astimezone(MOSCOW).isoformat(),observed.astimezone(MOSCOW).isoformat(),
            '' if status=='SCHEDULED' else f"{row['home_score']}:{row['away_score']}")
    text=stage_wording(values)
    f=VerifiedFact('fixture_stage',values,(item.canonical_url,),item.text,(text,text))
    identity=VerifiedFact('fixture_identity',('football-data.org',str(raw['id']),str(raw['homeTeam']['id']),str(raw['awayTeam']['id']),kick.isoformat()),(item.canonical_url,),item.text,('',''))
    d=kick.astimezone(MOSCOW).date();phrase=f'{d.day} {MONTHS[d.month-1]}'
    facts=(f,VerifiedFact('match_date',(d.isoformat(),),(item.canonical_url,),item.text,('','')),
           VerifiedFact('competition',(COMPETITIONS[raw['competition']['code']][0],),(item.canonical_url,),item.text,('','')),identity)
    return ImmutableBundle(hashlib.sha256(('football-data.org:'+str(raw['id'])+':'+kind).encode()).hexdigest()[:16],kind,values[1:3],facts,(item.canonical_url,),90,('fixture_stage',),'⚽ Матчи',
       event_date=d.isoformat(),source_published_at=item.published_at.isoformat(),source_collected_at=item.source_collected_at,
       source_id=item.source_id,match_status=status,date_basis='PROVIDER_KICKOFF_NOT_FINAL')

def stage_wording(v):
    status,home,away,comp,kick,observed,score=v
    k=datetime.fromisoformat(kick);o=datetime.fromisoformat(observed)
    if status=='SCHEDULED':return f'{k.day} {MONTHS[k.month-1]}, {comp}: «{home}» — «{away}». Начало в {k:%H:%M} МСК; матч ещё не начался.'
    stage='перерыв' if status=='HALF_TIME' else 'матч идёт'
    return f'{comp}: «{home}» — «{away}». По состоянию на {o:%H:%M} МСК {stage}, счёт {score} — не окончательный.'

def parse_relation(text):
    text=re.sub(r'\s+',' ',text).strip()
    text=re.sub(r'^[^A-Za-zА-Яа-яЁё«]+','',text)
    m=re.fullmatch(r'ОФИЦИАЛЬНО: ('+PERSON+r') выбыл до ((?:начала|середины|конца) '+MONTH+r') из-за травмы\.',text)
    if m:return 'INJURY','reported_injury_until',(*m.groups(),'SOURCE_OFFICIAL'),f'Официально: {m[1]} выбыл до {m[2]} из-за травмы.'
    m=re.match(r'^ОФИЦИАЛЬНО: ('+PERSON+r') отстранён на (\d{1,3}) дней и оштрафован на (\d{1,6}) евро\. Решение приняла ([^.]+)\.',text)
    if m and 0<int(m[2])<=365:
        return 'DISCIPLINE','reported_suspension',(*m.groups(),'SOURCE_OFFICIAL'),f'Официально: {m[1]} отстранён на {m[2]} дней и оштрафован на {m[3]} евро. Решение приняла {m[4]}.'
    m=re.match(r'^Фабрицио Романо: «([^»]+)» ведёт переговоры о назначении ('+PERSON+r') на должность спортивного директора\.',text)
    if m:return 'APPOINTMENT_TALKS','executive_talks',(*m.groups(),'Фабрицио Романо','TALKS_NOT_APPOINTED'),f'По данным Фабрицио Романо, «{m[1]}» ведёт переговоры о назначении {m[2]} спортивным директором.'
    m=re.fullmatch(r'Официальный стартовый состав «([^»]+)» на матч с «([^»]+)»: ([^.]+)\.',text)
    if m:
        names=tuple(n.strip() for n in m[3].split(','))
        if len(names)==len(set(names))==11 and all(re.fullmatch(PERSON,n) for n in names):
            return 'LINEUP','official_lineup',(m[1],m[2],*names),f'Официальный состав «{m[1]}» на матч с «{m[2]}»: '+', '.join(names)+'.'
    return None

def recover(bundles,inputs,audit):
    originals={i.canonical_url:i for i in inputs};out=list(bundles);used=set();decisions=[]
    for entry in audit.get('unsupported',()):
        refs=tuple(entry.get('sources',()))
        if len(refs)!=1 or refs[0] not in originals:continue
        item=originals[refs[0]];parsed=parse_relation(item.text)
        if not parsed:continue
        kind,key,values,phrase=parsed
        f=VerifiedFact(key,values,(item.canonical_url,),item.text,(phrase,phrase))
        people=values[:2] if kind in ('APPOINTMENT_TALKS','LINEUP') else values[:1]
        section='🚑 Травмы и дисквалификации' if kind in ('INJURY','DISCIPLINE') else '💬 Клубы и тренеры'
        out.append(ImmutableBundle(hashlib.sha256((key+item.canonical_url).encode()).hexdigest()[:16],kind,people,(f,),refs,85,(key,),section,
          source_published_at=item.published_at.isoformat(),source_id=item.source_id,date_basis='SOURCE_REPORT_NOT_EVENT_DATE'))
        used.add(item.canonical_url);decisions.append(dict(sources=refs,reason=key,decision='RECOVER'))
    # A lineup is context of one exact observed same-day fixture, not a second
    # calendar bullet. Ambiguous or unbound pairings are never guessed.
    merged=set()
    for lineup in [b for b in out if b.event_type=='LINEUP']:
        item=originals[lineup.source_refs[0]]
        targets=[b for b in out if b.event_type in ('MATCH_SCHEDULED','MATCH_LIVE') and
          {p.casefold() for p in b.participants}=={p.casefold() for p in lineup.participants} and
          b.event_date==item.published_at.astimezone(MOSCOW).date().isoformat()]
        if len(targets)==1:
            from dataclasses import replace
            target=targets[0];out[out.index(target)]=replace(target,verified_facts=target.verified_facts+lineup.verified_facts,
              essential_facts=target.essential_facts+lineup.essential_facts,source_refs=tuple(dict.fromkeys(target.source_refs+lineup.source_refs)))
            merged.add(lineup.event_id)
    out=[b for b in out if b.event_id not in merged]
    audit['match_day_recovery']=decisions
    audit['unsupported']=[r for r in audit.get('unsupported',()) if not set(r.get('sources',()))<=used]
    return out

def validate(bundle,text):
    for f in bundle.verified_facts:
        if f.key=='fixture_stage':
            from .football_data import source_item
            original=source_item(json.loads(f.evidence),f.values[5])
            checked=nonfinal(original,datetime.fromisoformat(f.values[5])+timedelta(microseconds=1))
            if f.values!=checked.verified_facts[0].values or stage_wording(f.values) not in text:return ['NONFINAL_SOURCE_OR_STAGE_MISMATCH']
        elif f.key in ('reported_injury_until','reported_suspension','executive_talks','official_lineup'):
            parsed=parse_relation(f.evidence)
            if not parsed or parsed[1]!=f.key or parsed[2]!=f.values or parsed[3]!=f.formulations[0]:return ['RELATION_SOURCE_MISMATCH']
    return []

def validate_source_clocks(bundle,inputs):
    originals={i.canonical_url:i for i in inputs}
    for fact in bundle.verified_facts:
        if fact.key!='fixture_stage':continue
        item=originals.get(fact.source_refs[0])
        if item is None or datetime.fromisoformat(fact.values[5])!=item.published_at:return False
        expected=nonfinal(item,item.published_at+timedelta(microseconds=1))
        if expected.verified_facts[0].values!=fact.values:return False
    return True

def edition_gate(selected,inputs,audit,cutoff):
    from .editorial_v2 import score
    today=cutoff.astimezone(MOSCOW).date()
    current=[b for b in selected if b.freshness_category in ('FRESH_TODAY','UPDATED_STORY') or (
        b.source_published_at and datetime.fromisoformat(b.source_published_at).astimezone(MOSCOW).date()==today
        and (not b.event_date or b.event_date==today.isoformat()))]
    material=[b for b in selected if b.event_type in ('INJURY','DISCIPLINE','MATCH_RESULT') or score(b,today)[0]>=60]
    unresolved=[r for r in audit.get('unsupported',()) if re.search(r'ОФИЦИАЛЬНО:.*(?:травм|отстран|контракт)|официальный.*состав',r.get('text',''),re.I)]
    expected=[r for r in audit.get('editorial_v2',()) if r['decision']=='INCLUDE']
    lost=set(r['event_id'] for r in expected)-{b.event_id for b in selected}
    issues=[]
    if selected and not current and not material:issues.append('ONLY_LOW_VALUE_CARRYOVER')
    if not current and unresolved:issues.append('IMPORTANT_SOURCE_UNPARSED')
    if lost and not current:issues.append('IMPORTANT_EVENTS_DID_NOT_FIT_PAYLOAD')
    return dict(status='BLOCKED' if issues else 'PASS',issues=issues,current_events=len(current),
      material_events=len(material),unresolved_official_sources=len(unresolved),parser_unsupported=len(audit.get('unsupported',[])),payload_omitted_events=len(lost))
