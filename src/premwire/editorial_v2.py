"""Pure editorial layer: no collection, model lifecycle, state or publisher.

Frozen evidence/values are never rewritten. A closed presentation grammar makes
every optional model choice independently reproducible, not free factual prose.
Scores are editorial judgements, NOT inferred standings or surprise statistics.
"""
from dataclasses import asdict, replace
from datetime import date
import hashlib
import json
import re

REVISION='editorial-v2-20261018-v1'
# football-data.org ID + exact full provider name, observed in retained API
# evidence. Russian labels are reviewed editorial translations, not identities.
TEAMS={
 2:('TSG 1899 Hoffenheim','Хоффенхайм'),3:('Bayer 04 Leverkusen','Байер'),
 4:('Borussia Dortmund','Боруссия Дортмунд'),5:('FC Bayern München','Бавария'),
 6:('FC Schalke 04','Шальке'),7:('Hamburger SV','Гамбург'),10:('VfB Stuttgart','Штутгарт'),
 12:('SV Werder Bremen','Вердер'),15:('1. FSV Mainz 05','Майнц'),16:('FC Augsburg','Аугсбург'),
 17:('SC Freiburg','Фрайбург'),18:('Borussia Mönchengladbach','Боруссия Мёнхенгладбах'),
 28:('1. FC Union Berlin','Унион'),29:('SC Paderborn 07','Падерборн'),
 57:('Arsenal FC','Арсенал'),58:('Aston Villa FC','Астон Вилла'),61:('Chelsea FC','Челси'),
 62:('Everton FC','Эвертон'),63:('Fulham FC','Фулхэм'),64:('Liverpool FC','Ливерпуль'),
 65:('Manchester City FC','Манчестер Сити'),66:('Manchester United FC','Манчестер Юнайтед'),
 67:('Newcastle United FC','Ньюкасл'),71:('Sunderland AFC','Сандерленд'),
 77:('Athletic Club','Атлетик'),78:('Club Atlético de Madrid','Атлетико'),
 79:('CA Osasuna','Осасуна'),80:('RCD Espanyol de Barcelona','Эспаньол'),81:('FC Barcelona','Барселона'),
 84:('Málaga CF','Малага'),90:('Real Betis Balompié','Бетис'),92:('Real Sociedad de Fútbol','Реал Сосьедад'),94:('Villarreal CF','Вильярреал'),95:('Valencia CF','Валенсия'),
 99:('ACF Fiorentina','Фиорентина'),100:('AS Roma','Рома'),103:('Bologna FC 1909','Болонья'),
 107:('Genoa CFC','Дженоа'),108:('FC Internazionale Milano','Интер'),113:('SSC Napoli','Наполи'),
 115:('Udinese Calcio','Удинезе'),263:('Deportivo Alavés','Алавес'),285:('Elche CF','Эльче'),
 322:('Hull City AFC','Халл'),341:('Leeds United FC','Лидс'),349:('Ipswich Town FC','Ипсвич'),
 351:('Nottingham Forest FC','Ноттингем Форест'),354:('Crystal Palace FC','Кристал Пэлас'),
 397:('Brighton & Hove Albion FC','Брайтон'),402:('Brentford FC','Брентфорд'),454:('Venezia FC','Венеция'),
 512:('Stade Brestois 29','Брест'),516:('Olympique de Marseille','Марсель'),519:('AJ Auxerre','Осер'),
 521:('Lille OSC','Лилль'),524:('Paris Saint-Germain FC','ПСЖ'),525:('FC Lorient','Лорьян'),
 529:('Stade Rennais FC 1901','Ренн'),531:('ES Troyes AC','Труа'),532:('Angers SCO','Анже'),
 533:('Le Havre AC','Гавр'),546:('Racing Club de Lens','Ланс'),548:('AS Monaco FC','Монако'),
 558:('RC Celta de Vigo','Сельта'),576:('RC Strasbourg Alsace','Страсбур'),
 719:('SV 07 Elversberg','Эльверсберг'),721:('RB Leipzig','РБ Лейпциг'),
 1044:('AFC Bournemouth','Борнмут'),1045:('Paris FC','Париж'),
 5335:('Real Racing Club de Santander','Расинг Сантандер'),5890:('US Lecce','Лечче'),
 7397:('Como 1907','Комо'),
}
COMPETITIONS={'PREMIER_LEAGUE':'АПЛ','CHAMPIONS_LEAGUE':'Лига чемпионов',
 'EUROPA_LEAGUE':'Лига Европы','LA_LIGA':'Ла Лига','BUNDESLIGA':'Бундеслига',
 'SERIE_A':'Серия А','LIGUE_1':'Лига 1'}
COMP_FORMS={'АПЛ':'в АПЛ','Лига чемпионов':'в Лиге чемпионов','Лига Европы':'в Лиге Европы',
 'Ла Лига':'в Ла Лиге','Бундеслига':'в Бундеслиге','Серия А':'в Серии А','Лига 1':'в Лиге 1'}
FEMININE={'Барселона','Бавария','Рома','Сельта','Осасуна','Фиорентина','Болонья',
 'Боруссия Дортмунд','Боруссия Мёнхенгладбах','Астон Вилла','Венеция'}
# Explicit audience priors, not a claim about current form or league position.
AUDIENCE={5:18,57:16,61:14,64:18,65:18,66:18,78:12,81:18,108:14,113:12,
          524:16,4:12,721:10,3:10,516:10,548:8,99:8,100:10}
CATEGORIES={'TRANSFER_INTEREST':'TRANSFERS','TRANSFER_OFFICIAL':'TRANSFERS',
 'INJURY':'INJURIES','DISCIPLINE':'INJURIES','CONTRACT':'CONTRACTS','PLAYER_STATS':'STATISTICS',
 'MATCH_RESULT':'IMPORTANT_MATCHES'}
HEADINGS={'TOP_STORIES':'🔥 Главное','IMPORTANT_MATCHES':'⚽ Матчи',
 'TRANSFERS':'🔄 Трансферы','CLUB_AND_COACH_NEWS':'🏟 Клубы и тренеры',
 'INJURIES':'🩺 Травмы','CONTRACTS':'📝 Контракты','STATISTICS':'📊 Статистика',
 'OTHER_RESULTS':'Коротко о результатах'}

def team_name(team):
    known=TEAMS.get(team.get('id'))
    return known[1] if known and team.get('name')==known[0] else team.get('name','')

def provider_aliases(bundles):
    """Exact observed ID/name bindings for cross-source dedupe, not fuzzy names.

    Ambiguous aliases are excluded. Unknown IDs retain the existing mechanism.
    This does not change any frozen participant/value or require a registry.
    """
    from .russian_editorial import club_entries
    registered={s.casefold():e.entity_id for e in club_entries() for s in (*e.aliases,e.display)}
    candidates={}
    for bundle in bundles:
        for f in bundle.verified_facts:
            if f.key!='fixture_identity' or not f.values or f.values[0]!='football-data.org':continue
            try:
                raw=json.loads(f.evidence)
                for key in ('homeTeam','awayTeam'):
                    team=raw[key];known=TEAMS.get(team['id'])
                    if not known or team['name']!=known[0]:continue
                    canonical=registered.get(known[1].casefold(),known[1].casefold())
                    for name in (team['name'],team['shortName'],known[1],*getattr_aliases(team['id'])):
                        candidates.setdefault(name.casefold(),set()).add((team['id'],canonical))
            except (KeyError,TypeError,ValueError):continue
    return {name:next(iter(bindings))[1] for name,bindings in candidates.items()
            if len(bindings)==1 and (name not in registered or registered[name]==next(iter(bindings))[1])}

def getattr_aliases(team_id):
    # Shared reviewed abbreviations are bound to the same verified provider ID.
    from .editorial_source_context import ALIASES
    return ALIASES.get(team_id,())

def fixture(bundle):
    """Read SOURCE, not model output or a bag of numeric tokens."""
    identity=next((f for f in bundle.verified_facts if f.key=='fixture_identity' and
                   f.values[0]=='football-data.org'),None)
    if identity is None:return None
    raw=json.loads(identity.evidence)
    # Availability/cutoff is validated upstream against the ORIGINAL API item.
    # A merged caption can have an earlier publication clock: never substitute
    # that caption clock for the API observation or infer a completion instant.
    if (str(raw['id'])!=identity.values[1] or
        tuple(map(str,(raw['homeTeam']['id'],raw['awayTeam']['id'])))!=identity.values[2:4] or
        raw['status']!='FINISHED' or raw['score']['duration']!='REGULAR'):
        raise ValueError('EDITORIAL_FIXTURE_IDENTITY_MISMATCH')
    a,b=raw['score']['fullTime']['home'],raw['score']['fullTime']['away']
    if any(type(v) is not int or not 0<=v<=99 for v in (a,b)):
        raise ValueError('EDITORIAL_INVALID_FINAL_SCORE')
    if raw['score']['winner']!=('DRAW' if a==b else 'HOME_TEAM' if a>b else 'AWAY_TEAM'):
        raise ValueError('EDITORIAL_WINNER_CONTRADICTION')
    return raw

def fingerprint(bundle):
    return hashlib.sha256(json.dumps(dict(participants=bundle.participants,
      facts=[asdict(f) for f in bundle.verified_facts],essential=bundle.essential_facts,
      refs=bundle.source_refs),sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def category(bundle):
    kind=bundle.event_type
    if 'TRANSFER' in kind:return 'TRANSFERS'
    if 'CONTRACT' in kind:return 'CONTRACTS'
    if 'STATS' in kind or 'RECORD' in kind or kind=='FINANCIAL_REPORT':return 'STATISTICS'
    return CATEGORIES.get(kind,'CLUB_AND_COACH_NEWS')

def score(bundle,day):
    raw=fixture(bundle);facts={f.key:f for f in bundle.verified_facts}
    recent=bool(bundle.event_date and bundle.event_date<day.isoformat())
    confirmed=6 if raw or 'OFFICIAL' in bundle.event_type or any('OFFICIAL' in f.values or 'SOURCE_OFFICIAL' in f.values for f in bundle.verified_facts) else 2
    detail_keys=set(facts)-{'result','draw_result','competition','match_date','fixture_identity','report_date'}
    components=dict(base=10,freshness=20 if bundle.freshness_category in ('FRESH_TODAY','UPDATED_STORY') else 8,
      competition=0,audience=0,scale=0,confirmed_context=min(len(detail_keys)*5,20),
      confirmation=confirmed,previous_day=-30 if recent else 0,surprise=0,consequences=0,
      novelty=8 if bundle.freshness_category=='UPDATED_STORY' else 0)
    if raw:
        comp=raw['competition']['code'];a=raw['score']['fullTime']['home'];b=raw['score']['fullTime']['away']
        components.update(competition=12 if comp in ('PL','CL') else 8,
          audience=max(AUDIENCE.get(raw['homeTeam']['id'],0),AUDIENCE.get(raw['awayTeam']['id'],0)),
          scale=(30 if abs(a-b)>=5 else 20 if abs(a-b)>=3 else 0)+(12 if a+b>=7 else 6 if a+b>=5 else 0))
        # Context includes only actual non-metadata verified claims.
    elif bundle.event_type=='MATCH_RESULT':
        components['competition']=12 if facts.get('competition') and facts['competition'].values[0] in ('PREMIER_LEAGUE','CHAMPIONS_LEAGUE') else 8
        result=next((f for f in bundle.verified_facts if f.key in ('result','draw_result')),None)
        if result and re.fullmatch(r'\d{1,2}:\d{1,2}',result.values[-1]):
            a,b=map(int,result.values[-1].split(':'))
            components['scale']=(30 if abs(a-b)>=5 else 20 if abs(a-b)>=3 else 0)+(12 if a+b>=7 else 6 if a+b>=5 else 0)
        exact={v[1]:AUDIENCE.get(k,0) for k,v in TEAMS.items()}
        components['audience']=max((exact.get(p,0) for p in bundle.participants),default=0)
        if 'scorers' in detail_keys or any('first_' in k for k in detail_keys):components['confirmed_context']+=12
    else:
        components['base']=35 if bundle.event_type=='APPOINTMENT' else 30
        exact={v[1]:AUDIENCE.get(k,0) for k,v in TEAMS.items()}
        components['audience']=max((exact.get(p,0) for p in bundle.participants),default=0)
        if bundle.event_type=='DISCIPLINE':components['consequences']=15
        if bundle.event_type=='TRANSFER_INTEREST':components['base']=12
        if bundle.freshness_category not in ('FRESH_TODAY','UPDATED_TODAY'):
            components['previous_day']-=10
    return sum(components.values()),components

def select(bundles,day):
    """No genre quotas: low-signal scores do not become filler by fitting."""
    ranked=[];decisions=[];seen=set()
    for b in bundles:
        # A canonical story may have distinct confirmed development stages.
        # Only identical factual payloads consolidate here; relation-aware
        # merging/conflicts/history dedupe already ran upstream.
        identity=(b.canonical_event_id or b.event_id,b.event_type,
                  tuple(sorted((f.key,f.values) for f in b.verified_facts)))
        if identity in seen:
            decisions.append(dict(event_id=b.event_id,sources=b.source_refs,decision='EXCLUDE',reason='CONSOLIDATED_EXACT_EVENT'));continue
        seen.add(identity)
        value,components=score(b,day);raw=fixture(b)
        previous=bool(b.event_date and b.event_date<day.isoformat())
        threshold=(60 if raw else 50) if previous and b.event_type=='MATCH_RESULT' else 55 if raw else 40
        # A single bare transfer-interest claim is not a substantive story.
        if b.event_type=='TRANSFER_INTEREST':threshold=40
        meaningful=[f for f in b.verified_facts if f.key not in ('report_date','fixture_identity')]
        thin_interest=(b.event_type=='TRANSFER_INTEREST' and len(meaningful)<2 and not any(
            re.search(r'Фабрицио Романо|Fabrizio Romano|David Ornstein|Д[эе]вид Орнштейн',f.evidence,re.I) for f in meaningful))
        keep=value>=threshold and not thin_interest
        row=dict(event_id=b.event_id,sources=b.source_refs,score=value,components=components,
          threshold=threshold,category=category(b),decision='INCLUDE' if keep else 'EXCLUDE',
          reason='NEWSWORTHY' if keep else 'THIN_INTEREST_REPORT' if thin_interest else 'PREVIOUS_DAY_LOW_NEWS_VALUE' if previous else 'LOW_NEWS_VALUE')
        decisions.append(row)
        if keep:ranked.append((value,b))
    ranked.sort(key=lambda v:(-v[0],v[1].event_id))
    selected=[]
    for value,b in ranked:
        kind=category(b)
        # Lead slots are reserved for genuinely high scoring items, not filled.
        if value>=70 and kind!='STATISTICS' and len([s for s in selected if s.section==HEADINGS['TOP_STORIES']])<4:
            section=HEADINGS['TOP_STORIES']
        else:section=HEADINGS[kind]
        if b.event_type=='MATCH_RESULT' and b.event_date and b.event_date<day.isoformat():
            from .daily_fact_digest import CARRY_SECTION
            section=CARRY_SECTION
        selected.append(replace(b,section=section,editorial_mode='v2'))
    return selected,decisions

def parts(bundle,variant):
    """A source-slot aligned grammar; no new factual slots or discarded keys."""
    from .daily_fact_digest import draft_parts,MONTHS
    original=draft_parts(replace(bundle,editorial_mode=''),variant)
    raw=fixture(bundle)
    if raw is None:
        # Generic verified relations retain their audited grammar. Remove only
        # redundant 'Клуб' label before a quoted identity, no factual rewrite.
        def clean(v):
            v=re.sub(r'^Клуб (?=«)','',v)
            v=re.sub(r'\bклуба (?=«)','',v)
            if variant==1:
                v=re.sub(r'^По (?:сообщению|данным) ([^,]+), ',r'\1: ',v)
            return v
        return {k:clean(v) for k,v in original.items()}
    a=raw['score']['fullTime']['home'];b=raw['score']['fullTime']['away']
    home,away=team_name(raw['homeTeam']),team_name(raw['awayTeam'])
    stamp=date.fromisoformat(bundle.event_date)
    when=f'{stamp.day} {MONTHS[stamp.month-1]}'
    comp=COMPETITIONS[next(f.values[0] for f in bundle.verified_facts if f.key=='competition')]
    result_key='draw_result' if a==b else 'result'
    if variant==1:
        clause=f'«{home}» — «{away}» — {a}:{b}.'
    elif a==b:
        clause=f'«{home}» и «{away}» сыграли вничью — {a}:{b}.'
    else:
        winner,loser=(home,away) if a>b else (away,home)
        verb=('разгромила' if winner in FEMININE else 'разгромил') if abs(a-b)>=3 else ('обыграла' if winner in FEMININE else 'обыграл')
        # Unknown clubs receive a neutral scoreboard, not guessed morphology.
        known={v[1] for v in TEAMS.values()}
        clause=(f'«{winner}» {verb} «{loser}» — {max(a,b)}:{min(a,b)}.' if winner in known
                else f'«{home}» — «{away}» — {a}:{b}.')
    # Competition and date share one compact prefix but retain separate slots.
    result=dict(original)
    result['competition']=COMP_FORMS[comp] if variant==0 else COMP_FORMS[comp].capitalize()
    result['match_date']=when
    result[result_key]=clause
    order=['match_date','competition',result_key] if variant==0 else ['competition','match_date',result_key]
    return {k:result[k] for k in order+list(k for k in result if k not in order)}

def validate(bundle,text,variant=0):
    """Independent typed-SOURCE assertions, then exact closed-grammar equality.

    Not a claim of independent real-world verification of provider statements.
    Names/numbers alone cannot authorize a new causal claim or status upgrade.
    """
    problems=[];raw=fixture(bundle)
    from .editorial_source_context import validate_fact
    if any(not validate_fact(f) for f in bundle.verified_facts):problems.append('SOURCE_CONTEXT_SLOT_MISMATCH')
    if raw:
        from .football_data import COMPETITIONS as COMPS
        from .daily_text_digest import MOSCOW
        from datetime import datetime
        facts={f.key:f for f in bundle.verified_facts}
        a,b=raw['score']['fullTime']['home'],raw['score']['fullTime']['away']
        key='draw_result' if a==b else 'result'
        fact=facts.get(key)
        # Validate canonical values using the ORIGINAL compiler's exact identity
        # resolution, not the display labels chosen by this presentation layer.
        from .daily_fact_digest import russian,club_name
        from .football_data import DISPLAY
        from .russian_editorial import club_entries
        aliases={s.casefold():e.display for e in club_entries() for s in (*e.aliases,e.display)}
        names=[aliases.get(t['shortName'].casefold(),DISPLAY.get(t['shortName'],russian(t['shortName']))) for t in (raw['homeTeam'],raw['awayTeam'])]
        expected=tuple(names)+ (f'{a}:{b}',) if a==b else ((names[0],names[1],f'{a}:{b}') if a>b else (names[1],names[0],f'{b}:{a}'))
        bound=provider_aliases([bundle])
        def canonical(values):return tuple(bound.get(v.casefold(),v.casefold()) for v in values[:2])+values[2:]
        if not fact or canonical(fact.values)!=canonical(expected):problems.append('RESULT_PARTICIPANTS_OR_SCORE')
        if facts.get('competition') is None or facts['competition'].values!=(COMPS[raw['competition']['code']][0],):problems.append('COMPETITION')
        real_day=datetime.fromisoformat(raw['utcDate'].replace('Z','+00:00')).astimezone(MOSCOW).date().isoformat()
        if bundle.event_date!=real_day or facts.get('match_date') is None or facts['match_date'].values!=(real_day,):problems.append('EVENT_DATE')
    expected=' '.join(v for v in parts(bundle,variant).values() if v)
    if text!=expected:problems.append('UNAPPROVED_ASSERTION_OR_ESSENTIAL_LOSS')
    return problems
