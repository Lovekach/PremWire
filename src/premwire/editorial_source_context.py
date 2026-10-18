"""Narrow missing SOURCE slots, not a second collector or free-text extractor.

Only relations already admitted by existing filters (unsupported grammar) are
eligible. Rejected SOURCE never bypasses its gate. Match context binds exactly
one observed fixture by both club identities and report/event day, not surnames.
"""
from dataclasses import replace
from datetime import datetime
import hashlib,re

ALIASES={66:('Ман Юнайтед','МЮ'),351:('Ноттингем',)}
PERSON=r'[А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+){1,2}'

def validate_fact(fact):
    """Reparse SOURCE independently of frozen wording or proposed model text."""
    text=re.sub(r'\s+',' ',fact.evidence)
    if fact.key=='estimated_transfer_fee':
        matches=re.findall(r'Сумма трансфера может составить около ([€£])(\d+(?:[.,]\d+)?) млн',text)
        return len(matches)==1 and fact.values==(matches[0][1],matches[0][0],'APPROXIMATE_POSSIBLE_NOT_AGREED')
    if fact.key=='decisive_scorer':
        m=re.search(r'Единственный (?:мяч|гол) забил ('+PERSON+r')\.',text)
        return bool(m and fact.values==(m.group(1),'ONLY_GOAL'))
    if fact.key=='equalising_goals':
        m=re.search(r'На гол ([А-ЯЁ][А-ЯЁа-яё-]+) на (\d{1,3})-й минуте хозяева ответили точным ударом ([А-ЯЁ][А-ЯЁа-яё-]+)\.',text)
        return bool(m and fact.values==(*m.groups(),'HOME_REPLY_TO_AWAY'))
    if fact.key=='reported_injury_absence':
        m=re.search(r'ОФИЦИАЛЬНО:\s*('+PERSON+r') выбыл на (\d{1,2})(?:[-–](\d{1,2}))? (дней|недели|недель|месяца|месяцев) из-за травмы\.',text)
        return bool(m and fact.values==(m[1],m[2],m[3] or m[2],m[4],'SOURCE_OFFICIAL'))
    return True

def enrich(bundles,inputs,audit):
    from .daily_fact_digest import ImmutableBundle,VerifiedFact
    from .daily_text_digest import MOSCOW
    from .editorial_v2 import fixture,team_name,TEAMS
    from .russian_editorial import club_entries
    originals={i.canonical_url:i for i in inputs}
    output=list(bundles);decisions=[];used=set();blocked=set()
    def add(key,values,phrase,item):
        return VerifiedFact(key,tuple(values),(item.canonical_url,),item.text,(phrase,phrase))
    def mentions(text,team):
        known=TEAMS.get(team['id'])
        if not known or team['name']!=known[0]:return False
        aliases={team['name'],team['shortName'],known[1],*ALIASES.get(team['id'],())}
        for entry in club_entries():
            if entry.display==known[1]:aliases.update(entry.aliases)
        return any(re.search(r'(?<!\w)'+re.escape(a)+r'(?!\w)',text,re.I) for a in aliases)
    # Preserve an explicit conditional amount that the older interest grammar
    # omitted. Scope must be unambiguous: one club, one stated signing target,
    # one amount, one original publication. Never a submitted/agreed fee.
    for pos,b in enumerate(output):
        if b.event_type!='TRANSFER_INTEREST' or len(b.source_refs)!=1:continue
        item=originals.get(b.source_refs[0])
        if item is None or any(f.key=='estimated_transfer_fee' for f in b.verified_facts):continue
        if len(re.findall('хочет подписать',item.text))!=1 or len(set(re.findall(r'«([^»]+)»',item.text)))!=1:continue
        matches=re.findall(r'Сумма трансфера может составить около ([€£])(\d+(?:[.,]\d+)?) млн',item.text)
        if len(matches)!=1:continue
        currency,value=matches[0]
        f=add('estimated_transfer_fee',(value,currency,'APPROXIMATE_POSSIBLE_NOT_AGREED'),
          f'Возможная сумма трансфера — около {value} млн '+('евро.' if currency=='€' else 'фунтов.'),item)
        if validate_fact(f):
            output[pos]=replace(b,verified_facts=b.verified_facts+(f,),essential_facts=b.essential_facts+(f.key,))
            decisions.append(dict(sources=b.source_refs,reason='EXPLICIT_CONDITIONAL_FEE_RESTORED'))
    for row in audit.get('unsupported',()):
        refs=row.get('sources',())
        if len(refs)!=1 or refs[0] not in originals:continue
        item=originals[refs[0]];text=row.get('text','')
        if any(u==item.canonical_url for b in output for u in b.source_refs):continue
        injury=re.search(r'ОФИЦИАЛЬНО:\s*('+PERSON+r') выбыл на (\d{1,2})(?:[-–](\d{1,2}))? (дней|недели|недель|месяца|месяцев) из-за травмы\.',text)
        if injury:
            person,lo,hi,unit=injury.groups()
            if not 0<int(lo)<=52 or hi and not int(lo)<=int(hi)<=52:continue
            duration=lo+('–'+hi if hi else '')
            phrase=f'Официально: {person} выбыл на {duration} {unit} из-за травмы.'
            f=add('reported_injury_absence',(person,lo,hi or lo,unit,'SOURCE_OFFICIAL'),phrase,item)
            if not validate_fact(f):
                decisions.append(dict(sources=refs,reason='ORIGINAL_SOURCE_REPARSE_FAILED'));continue
            event_id=hashlib.sha256(('reported_injury_absence:'+item.canonical_url).encode()).hexdigest()[:16]
            output.append(ImmutableBundle(event_id,'INJURY',(person,),(f,),(item.canonical_url,),85,
              (f.key,),'🚑 Травмы и дисквалификации',source_published_at=item.published_at.isoformat(),
              source_id=item.source_id,date_basis='SOURCE_REPORT_NOT_INJURY_DATE'))
            used.add(item.canonical_url);decisions.append(dict(sources=refs,reason='EXPLICIT_OFFICIAL_ABSENCE_RANGE'));continue
        candidates=[]
        for pos,b in enumerate(output):
            raw=fixture(b)
            if raw is None:continue
            # An old caption or live update cannot enrich a different day's game.
            if item.published_at.astimezone(MOSCOW).date().isoformat()!=b.event_date:continue
            if item.published_at<datetime.fromisoformat(raw['utcDate'].replace('Z','+00:00')):continue
            if not all(mentions(text,raw[k]) for k in ('homeTeam','awayTeam')):continue
            candidates.append((pos,b,raw))
        if len(candidates)!=1:continue
        pos,b,raw=candidates[0];hs,aws=raw['score']['fullTime']['home'],raw['score']['fullTime']['away']
        facts=[]
        scorer=re.search(r'Единственный (?:мяч|гол) забил ('+PERSON+r')\.',text)
        if hs+aws==1 and scorer and re.search(r'обыгра[лaа]+|победил[а]?',text,re.I):
            person=scorer.group(1)
            facts.append(add('decisive_scorer',(person,'ONLY_GOAL'),f'Единственный гол забил {person}.',item))
        draw=re.search(r'На гол ([А-ЯЁ][А-ЯЁа-яё-]+) на (\d{1,3})-й минуте хозяева ответили точным ударом ([А-ЯЁ][А-ЯЁа-яё-]+)\.',text)
        if hs==aws==1 and draw and 0<int(draw.group(2))<=120:
            first,minute,reply=draw.groups()
            facts.append(add('equalising_goals',(first,minute,reply,'HOME_REPLY_TO_AWAY'),
              f'На гол {first} на {minute}-й минуте хозяева ответили точным ударом {reply}.',item))
        facts=[f for f in facts if validate_fact(f)]
        if not facts:continue
        # Conflicting independent captions must not overwrite an earlier slot.
        existing={f.key:f.values for f in b.verified_facts}
        conflicts={f.key for f in facts if f.key in existing and existing[f.key]!=f.values}
        if conflicts or any((b.event_id,f.key) in blocked for f in facts):
            blocked.update((b.event_id,key) for key in conflicts)
            for f in b.verified_facts:
                if f.key in conflicts:used.difference_update(f.source_refs)
            kept=tuple(f for f in b.verified_facts if f.key not in conflicts)
            output[pos]=replace(b,verified_facts=kept,
              essential_facts=tuple(k for k in b.essential_facts if k not in conflicts),
              source_refs=tuple(dict.fromkeys(u for f in kept for u in f.source_refs)))
            decisions.append(dict(sources=refs,reason='CONFLICTING_CONTEXT_OMITTED'));continue
        facts=[f for f in facts if f.key not in existing]
        if not facts:continue
        output[pos]=replace(b,verified_facts=b.verified_facts+tuple(facts),
          essential_facts=b.essential_facts+tuple(f.key for f in facts),
          source_refs=tuple(dict.fromkeys((*b.source_refs,item.canonical_url))))
        used.add(item.canonical_url);decisions.append(dict(sources=refs,reason='EXACT_FIXTURE_CONFIRMED_CONTEXT'))
    audit['source_context_recovery']=decisions
    audit['unsupported']=[r for r in audit.get('unsupported',()) if not set(r.get('sources',()))<=used]
    return output
