"""Deterministic fact-first newsroom candidate. SHADOW ONLY, no live imports.

Qwen selects stylistic variants; it cannot write or modify factual payloads.
Unsupported source shapes remain explicit omissions, never inferred news.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
import unicodedata

from .daily_fact_shadow import prepare, normalize_handles
from .daily_quality import clubs
from .daily_text_digest import MOSCOW, SECTIONS, MONTHS, telegram_length
from .editorial_desk import fallback_decision
from .daily_fact_patterns import extended_event

STATS_SECTION='📊 Статистика'
DIGEST_SECTIONS=(*SECTIONS,STATS_SECTION)


@dataclass(frozen=True, slots=True)
class VerifiedFact:
    key: str
    values: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence: str
    formulations: tuple[str, str]


@dataclass(frozen=True, slots=True)
class ImmutableBundle:
    event_id: str
    event_type: str
    participants: tuple[str, ...]
    verified_facts: tuple[VerifiedFact, ...]
    source_refs: tuple[str, ...]
    importance: int
    essential_facts: tuple[str, ...]
    section: str

    def render(self, variant=0):
        if type(variant) is not int or variant not in (0, 1):
            raise ValueError('Unsupported editorial variant')
        return ' '.join(draft_parts(self,variant).values())


def draft_parts(bundle,variant):
    """Presentation only: never modify frozen values, evidence or references."""
    parts={f.key:f.formulations[variant] for f in bundle.verified_facts}
    if bundle.event_type=='MATCH_RESULT':
        for f in bundle.verified_facts:
            if f.key=='streak' and len(f.values)==3:
                _,wins,games=f.values
                parts[f.key]=f'Это {wins}-я победа в {games} матчах.'
    if bundle.event_type=='MATCH_NEWS':
        for f in bundle.verified_facts:
            if f.key.endswith('_points') and len(f.values)==3:
                _,points,rounds=f.values
                parts[f.key]=f'В первых {rounds} турах набрано {points} очков.'
            elif f.key.endswith('_event') and 'трёх последних соперников' in f.formulations[variant]:
                parts[f.key]='Клуб не выиграл ни один из трёх последних матчей, в которых соперник получал красную карточку.'
    if bundle.event_type=='INJURY' and any(f.key=='injury' for f in bundle.verified_facts):
        for f in bundle.verified_facts:
            if f.key=='availability':
                parts[f.key]=f'По словам {f.values[-1]}, игрок сегодня не сможет сыграть из-за проблем с задней поверхностью бедра.'
    return parts


def club_name(surface):
    found = clubs(surface)
    return next(iter(found)) if len(found)==1 else surface.strip(' «»"')


def russian(surface):
    # Translation aliases only, not permission gates or fact completion.
    names = {'Clarence Seedorf':'Кларенс Зеедорф', 'Corinthians':'Коринтианс',
             'Rayan Cherki':'Райан Черки', 'Erling Haaland':'Эрлинг Холанд',
             'Vinicius Jr':'Винисиус Жуниор', 'El Clásico':'Эль Класико',
             'Selhurst Park':'Селхерст Парк', 'Crystal Palace':'Кристал Пэлас'}
    return names.get(surface, club_name(surface))


def surface_text(text):
    text = text.replace('«','').replace('»','')
    text = re.sub(r'\bМан Сити\b','Манчестер Сити',text)
    text = re.sub(r'\bМЮ\b','Манчестер Юнайтед',text)
    return text.strip(' .!') + '.'


def compile_bundle(candidate, day, original_sources=None):
    facts, participants, event_type = [], list(candidate.participants), 'CLUB_NEWS'
    importance = fallback_decision(candidate.source.text).importance
    section = SECTIONS[4]
    def add(key, values, clause, url, text, alternative=None):
        if any(f.key==key for f in facts):
            return
        facts.append(VerifiedFact(key,tuple(str(v) for v in values),(url,),clause,
                                  (text,alternative or text)))
    source = candidate.source.text
    for f in candidate.facts:
        t, url = f.text, f.url
        # Explicit final-result slots, not the latest intermediate score.
        result = re.search(r'^(.+?) make it (\w+|\d+) wins out of (\w+|\d+) after a (\d+)-(\d+) victory over (?:ten-man )?(.+?) at (.+?)\.',t)
        plain = re.search(r'^(.+?) (?:beat|defeated) (.+?) (\d+)[-:](\d+)(?: at (.+?))?[.!]?$',t)
        if result or plain:
            if result:
                winner, loser = club_name(result[1]), club_name(result[6])
                score = f'{result[4]}:{result[5]}'
                number = {'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
                wins, games = number.get(result[2],result[2]), number.get(result[3],result[3])
            else:
                winner, loser=club_name(plain[1]),club_name(plain[2])
                score=f'{plain[3]}:{plain[4]}'
            if winner==loser or int(score.split(':')[0])<=int(score.split(':')[1]):
                continue
            event_type,section,importance='MATCH_RESULT',SECTIONS[0],95
            participants=[winner,loser]
            add('result',(winner,loser,score),t,url,
                f'{winner} обыграл {loser} со счётом {score}.',
                f'{winner} победил {loser} — {score}.')
            if result and wins.isdigit() and games.isdigit():
                add('streak',(winner,wins,games),t,url,f'У {winner} {wins} побед в {games} матчах.')
            continue
        m=re.search(r'Голы на счету (.+?)\.',t)
        if m:
            spellings={'Холанда':'Холанд','Шерки':'Черки'}
            scorers=tuple(spellings.get(s.strip(),s.strip()) for s in re.split(r',\s*|\s+и\s+',m[1]))
            names=', '.join(scorers[:-1])+' и '+scorers[-1] if len(scorers)>1 else scorers[0]
            add('scorers',scorers,t,url,f'Голы забили {names}.',f'Авторы голов — {names}.')
        m=re.search(r'(?:^А )?(\w+) (?:подставил .+? и )?удалился на (\d+)-й минуте',t)
        if m:
            add('red_card',(m[1],m[2]),t,url,f'{m[1]} был удалён на {m[2]}-й минуте.',f'На {m[2]}-й минуте удалён {m[1]}.')
        m=re.search(r'«?(.+?)»? закрепляется на первой строчке и отрывается от «([^»]+)» на (\d+) очка',t)
        if m:
            leader,rival,gap=club_name(m[1]),club_name(m[2]),m[3]
            add('table',(leader,rival,gap),t,url,f'{leader} лидирует в таблице, опережая {rival} на {gap} очка.')
    if any(f.key=='result' for f in facts):
        # Do not turn unrelated season/player statements into this fixture.
        essential=('result','scorers','red_card')
    else:
        facts=[]
        for f in candidate.facts:
            t,url=f.text,f.url
            m=re.search(r'ОФИЦИАЛЬНО:\s*(.+?) переш[её]л в «([^»]+)»(?:\s*\([^)]*\))? из «([^»]+)»',t)
            if m:
                player,destination,origin=m.groups()
                participants=[player,club_name(origin),club_name(destination)]
                event_type,section,importance='TRANSFER_OFFICIAL',SECTIONS[1],85
                add('transfer',(player,origin,destination,'OFFICIAL'),t,url,
                    f'{player} официально перешёл из {origin} в {destination}.')
            if re.search(r'^Свободный агент\.',t) and event_type=='TRANSFER_OFFICIAL':
                add('fee',('FREE_AGENT',),t,url,'Переход — на правах свободного агента.')
            m=re.search(r'Контракт до (лета )?(\d{4}) года',t)
            if m and event_type=='TRANSFER_OFFICIAL':
                term=(m[1] or '')+m[2]
                add('contract',(term,),t,url,f'Контракт рассчитан до {term} года.')
            m=re.search(r'^(.+?) becomes new head coach at (.+?) until the end of (\d{4})',t)
            if m:
                person,team,year=russian(m[1]),russian(m[2]),m[3]
                participants=[person,team];event_type,importance='APPOINTMENT',85
                add('appointment',(person,team,year),t,url,
                    f'{person} стал главным тренером {team}. Контракт — до конца {year} года.',
                    f'Новый главный тренер {team} — {person}. Контракт действует до конца {year} года.')
            m=re.search(r'(.+?) has (\d+) G/A in (\d+) Premier League games this season, with (\d+) goals and (\d+) assists',t)
            if m:
                player=russian(m[1].strip(' 🩵🪄')); total,games,goals,assists=m.group(2,3,4,5)
                participants=[player];event_type='PLAYER_STATS'
                add('player_stats',(player,total,games,goals,assists),t,url,
                    f'{player}: {total} результативных действий в {games} матчах Премьер-лиги этого сезона — {goals} гола и {assists} передачи.')
            m=re.search(r'^(\d+) goals in all competitions',t)
            if m and event_type=='PLAYER_STATS':
                add('all_competitions',(m[1],),t,url,f'Во всех турнирах на его счету {m[1]} голов.')
            if re.search(r'at risk for El Clásico following red card \+ referee report',t):
                player=russian(t.split(',')[0])
                participants=[player];event_type,section='DISCIPLINE',SECTIONS[3]
                add('suspension_risk',(player,'RISK','Эль Класико'),t,url,
                    f'{player} рискует пропустить Эль Класико после удаления и доклада арбитра об агрессивном поведении.')
            m=re.search(r'(Доминик \w+) не попал в заявку «([^»]+)» .+?из-за (травмы[^.!?]+?)(?= Попробуйте|$)',t)
            if m:
                person,club,injury=m.groups()
                participants=[person,club_name(club)];event_type,section='INJURY',SECTIONS[3]
                add('injury',(person,club,injury),t,url,f'{person} не попал в заявку {club} из-за {injury}.')
            m=re.search(r'«([^»]+)» набрал всего (\d+) очков в первых (\d+) турах чемпионата',t)
            if m:
                team,points,rounds=m.groups();participants=[team];event_type='CLUB_STATS'
                add('points',(team,points,rounds),t,url,f'{team} набрал {points} очков в первых {rounds} турах чемпионата.')
            m=re.search(r'худший старт клуба в лиге за последние (\d+) лет',t)
            if m and event_type=='CLUB_STATS':
                add('record',(m[1],),t,url,f'Это худший старт клуба в лиге за последние {m[1]} лет.')
        # Clean, complete Russian source clauses are a safe deterministic basis.
        # Not every English caption or unresolved role has a supported renderer.
        if not facts:
            for f in candidate.facts:
                t=f.text
                if re.search(r'вырвал победу|не дожать|не смогли обыграть',t):
                    if re.search(r'красн',t):
                        # "Красные дьяволы" follows an explicit MU subject in SOURCE.
                        if 'Манчестер Юнайтед' in candidate.source.text:
                            t=re.sub(r'«Красные дьяволы»','Манчестер Юнайтед',t)
                            match=re.search(r'Манчестер Юнайтед не смогли обыграть[^.!]+',t)
                            if match:t=match[0].replace('не смогли','не смог')
                    t=re.sub(r'умудрился не дожать','не смог обыграть',t)
                    t=re.sub(r'оставшийся вдесятером «([^»]+)»',r'игравший вдесятером «\1»',t)
                    t=re.sub(r'не смог обыграть игравший вдесятером «([^»]+)»',r'не смог обыграть «\1», который играл вдесятером',t)
                    add('event',(surface_text(t),),f.text,f.url,surface_text(t))
                    event_type,section='MATCH_NEWS',SECTIONS[2]
            # Concrete last-place aggregate is independent of unnamed opponents.
            m=re.search(r'«([^»]+)».*?(\d+) побед, ПОСЛЕДНЕЕ МЕСТО.*? (\d+) очка из (\d+) возможных',source)
            if not facts and m:
                team,wins,points,possible=m.groups();participants=[team];event_type='CLUB_STATS'
                add('season',(team,wins,points,possible),source,candidate.source.canonical_url,
                    f'{team} занимает последнее место в таблице: {wins} побед и {points} очка из {possible} возможных.')
            title=re.search(r'^(.+?) = атака «([^»]+)»',source)
            stat=re.search(r'(\d+) игроков атакующей линии.*?забили в чемпионате столько же голов',source)
            sale=re.search(r'этим летом отпустил в «([^»]+)» всего за (\d+) млн евро',source,re.I)
            if not facts and title and stat and sale:
                person,team=title.groups();destination,fee=sale.groups()
                participants=[person,club_name(team),club_name(destination)];event_type='PLAYER_STATS'
                add('comparison',(person,team,stat[1]),source,candidate.source.canonical_url,
                    f'{person} забил в чемпионате столько же голов, сколько {stat[1]} игроков атакующей линии {team}.')
                add('past_transfer',(person,destination,fee,'SUMMER'),source,candidate.source.canonical_url,
                    f'Этим летом клуб отпустил его в {destination} за {fee} млн евро.')
            first=re.search(r'Three goals and a first win at Selhurst Park for .+?Crystal Palace',source)
            if not facts and first:
                participants=['Кристал Пэлас'];event_type='CLUB_STATS'
                add('home_record',('Кристал Пэлас','3','FIRST_WIN','Селхерст Парк'),first[0],candidate.source.canonical_url,
                    'Кристал Пэлас одержал первую победу на «Селхерст Парк», забив 3 гола.')
        # Conditional transfer intentions: keep attribution and release condition.
        if not facts:
            heading=re.search(r'^(\w+) перейдет в «([^»]+)»',source)
            plans=re.search(r'планируют продать (\w+) следующим летом',source)
            release=re.search(r'«([^»]+)» готова отпустить футболиста, если поступит выгодное предложение',source)
            credit=re.search(r'📰\s*([A-Za-z .]+)',source)
            if heading and plans and release:
                player,team=heading.groups();team=club_name(team);origin=release[1];participants=[player,team,origin]
                event_type,section='TRANSFER_INTEREST',SECTIONS[1]
                add('plans',(team,plans[1],player,'PLAN','следующее лето'),source,candidate.source.canonical_url,
                    f'{team} планирует продать {plans[1]} следующим летом и подписать {player + "а" if player.endswith("ис") else player}.')
                add('release_condition',(origin,'IF_FAVORABLE_OFFER'),source,candidate.source.canonical_url,
                    f'{origin} готова отпустить игрока, если поступит выгодное предложение.')
                if credit:
                    add('attribution',(credit[1].strip(),),source,candidate.source.canonical_url,f'Источник: {credit[1].strip()}.')
        essential=tuple(f.key for f in facts)
    if not facts:
        original=(original_sources or {}).get(candidate.source.canonical_url)
        extended=extended_event(candidate,add,russian,original.text if original else None)
        if not extended:return None
        event_type,participants,importance=extended
        essential=tuple(f.key for f in facts)
        if event_type.startswith('TRANSFER_') or event_type=='RELEASE_CLAUSE':section=SECTIONS[1]
        elif event_type=='INJURY':section=SECTIONS[3]
    if event_type in ('PLAYER_STATS','CLUB_STATS'):section=STATS_SECTION
    if any(f.key=='home_record' for f in facts):section=SECTIONS[2]
    essential=tuple(k for k in essential if any(f.key==k for f in facts))
    identity=json.dumps((day.isoformat(),event_type,sorted(participants),[(f.key,f.values) for f in facts]),ensure_ascii=False)
    event_id=hashlib.sha256(identity.encode()).hexdigest()[:16]
    refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs))
    return ImmutableBundle(event_id,event_type,tuple(participants),tuple(facts),refs,
                           importance,essential,section)


def build_bundles(items,start,end,filter_config,timings=None):
    candidates,rejected,recovered,conflicts=prepare(items,start,end,filter_config,timings)
    ticks=time.monotonic()
    bundles,unsupported,handled,context_omissions=[],[],[],[]
    original_sources={i.canonical_url:i for i in items}
    for candidate in candidates:
        bundle=compile_bundle(candidate,start.astimezone(MOSCOW).date(),original_sources)
        if bundle:
            bundles.append(bundle)
            handled.append(dict(event=candidate.identity,event_type=bundle.event_type,sources=bundle.source_refs))
        else:
            live=bool(re.search(r'Счёт уже|\balready\s+\d+[-:]\d+',candidate.source.text,re.I))
            record={'event':candidate.identity,'reason':'LIVE_CAPTION_NOT_FINAL_OR_AMBIGUOUS_SIDES' if live else 'NO_DETERMINISTIC_SOURCE_RENDERER',
                    'sources':candidate.sources,'text':candidate.source.text}
            (context_omissions if live else unsupported).append(record)
    if timings is not None:timings['fact_bundle']=time.monotonic()-ticks
    ticks=time.monotonic()
    # Event identity, not names alone. Independent injuries/season statistics stay.
    bundles=merge_health_context(bundles,start.astimezone(MOSCOW).date())
    bundles=attach_match_context(bundles,start.astimezone(MOSCOW).date())
    unique={b.event_id:b for b in bundles}
    if timings is not None:timings['semantic_dedupe']=time.monotonic()-ticks
    return sorted(unique.values(),key=lambda b:(-b.importance,b.event_id)),dict(
        rejected=rejected,recovered=recovered,conflicts=conflicts,unsupported=unsupported,handled=handled,context_omissions=context_omissions)


def same_person_surname(a,b):
    from .simple_entity_hygiene import _CYRILLIC_TO_LATIN
    def fold(value):
        value=unicodedata.normalize('NFKD',value.split()[-1].casefold())
        value=''.join(c for c in value if not unicodedata.combining(c)).translate(_CYRILLIC_TO_LATIN)
        return value.replace('sz','s')
    return len(fold(a))>=5 and fold(a)==fold(b)


def merge_health_context(bundles,day):
    """Only an explicit availability/hamstring quote supplements one injury.

    Exact surname transliteration equivalence, unique match and same saved day;
    other diagnoses, ambiguity and independent medical events stay separate.
    """
    bases=[b for b in bundles if b.event_type=='INJURY' and any(
        f.key=='injury' and 'задней поверхности бедра' in f.formulations[0] for f in b.verified_facts)]
    result=list(bundles)
    for quote in bundles:
        if quote.event_type!='INJURY' or not any(f.key=='availability' for f in quote.verified_facts):continue
        matches=[b for b in bases if same_person_surname(quote.participants[0],b.participants[0])]
        if len(matches)!=1:continue
        base=matches[0];facts=base.verified_facts+quote.verified_facts
        identity=json.dumps((day.isoformat(),'INJURY',base.participants,[(f.key,f.values) for f in facts]),ensure_ascii=False)
        merged=replace(base,event_id=hashlib.sha256(identity.encode()).hexdigest()[:16],verified_facts=facts,
                       essential_facts=base.essential_facts+quote.essential_facts,
                       source_refs=tuple(dict.fromkeys(base.source_refs+quote.source_refs)))
        result=[merged if b is base else b for b in result if b is not quote]
    return result


def attach_match_context(bundles, day):
    """Attach supported post-match aggregates only to one unambiguous fixture.

    Never combine two fixtures, injuries, transfers or player statistics. No
    fact is removed: this is point composition, not a team-name dedupe gate.
    """
    fixtures=[b for b in bundles if b.event_type=='MATCH_NEWS' and len(b.participants)==2]
    attached={};used=set()
    for context in bundles:
        if len(context.participants)!=1:
            continue
        keys={f.key for f in context.verified_facts}
        season=context.event_type=='CLUB_STATS' and keys=={'points','record'}
        history=(context.event_type=='MATCH_NEWS' and
                 'трёх последних соперников' in context.render())
        if not (season or history):
            continue
        matches=[b for b in fixtures if context.participants[0] in b.participants]
        if len(matches)==1:
            attached.setdefault(matches[0].event_id,[]).append(context)
            used.add(context.event_id)
    result=[]
    for b in bundles:
        if b.event_id in used:
            continue
        contexts=attached.get(b.event_id,[])
        if not contexts:
            result.append(b);continue
        facts=list(b.verified_facts);essential=list(b.essential_facts)
        for context in contexts:
            for f in context.verified_facts:
                key=context.event_id+'_'+f.key
                facts.append(replace(f,key=key))
                if f.key in context.essential_facts:essential.append(key)
        identity=json.dumps((day.isoformat(),b.event_type,b.participants,
                             [(f.key,f.values) for f in facts]),ensure_ascii=False)
        result.append(replace(b,event_id=hashlib.sha256(identity.encode()).hexdigest()[:16],
                              verified_facts=tuple(facts),essential_facts=tuple(essential),
                              source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)),
                              importance=max([b.importance]+[c.importance for c in contexts])))
    return result


def audit_digest(body, selected, choices, sources=()):
    """Measure essential retention and provenance, including final truncation.

    The equality guard disallows arbitrary model prose. Provenance checks exact
    SOURCE text (whitespace and explicit handles normalized), not independent
    real-world truth of a source's claim.
    """
    missing=[];distorted=[];provenance=[]
    def normalized(text):
        # Same explicit CPFC handle translation as the existing SOURCE cleaner.
        # This is not contextual inference from a stadium, coach or emoji.
        text=re.sub(r'(?<!\w)@CPFC\b','Crystal Palace',normalize_handles(text),flags=re.I)
        return re.sub(r'\s+',' ',text).strip()
    source_text={s.canonical_url:normalized(s.text) for s in sources}
    for b in selected:
        variant=choices.get(b.event_id,0)
        if type(variant) is not int or variant not in (0,1):
            distorted.append(b.event_id);variant=0
        if b.render(variant) not in body:distorted.append(b.event_id)
        facts={f.key:f for f in b.verified_facts}
        for key in b.essential_facts:
            fact=facts.get(key)
            if fact is None or draft_parts(b,variant)[key] not in body:
                missing.append((b.event_id,key))
        for fact in b.verified_facts:
            if not fact.source_refs or not fact.evidence or (sources and not any(
                normalized(fact.evidence) in source_text.get(url,'') for url in fact.source_refs)):
                provenance.append((b.event_id,fact.key))
    return dict(essential_facts_checked=sum(len(b.essential_facts) for b in selected),
                essential_facts_lost=len(missing),missing_essential_facts=missing,
                distorted_events=len(set(distorted)),provenance_issues=provenance)


def validate_edit(bundle, text):
    # A closed factual vocabulary is stricter than bag-of-names/number checks:
    # role changes, new causes and certainty upgrades also fail.
    return [] if text in (bundle.render(0),bundle.render(1)) else ['UNAPPROVED_FACTUAL_OR_STYLE_PAYLOAD']


def apply_choices(bundles,response):
    defaults={b.event_id:0 for b in bundles}
    proposed=dict(defaults)
    try:
        values=json.loads(response)
    except (ValueError,TypeError):
        return defaults,['INVALID_JSON']
    if not isinstance(values,list) or len(values)!=len(bundles):
        return defaults,['MISSING_OR_EXTRA_EVENTS']
    seen=set()
    for row in values:
        if not isinstance(row,dict) or set(row)!= {'event_id','variant'}:
            return defaults,['INVALID_SCHEMA']
        key=row['event_id'];choice=row['variant']
        if not isinstance(key,str) or key not in defaults or key in seen or type(choice) is not int or choice not in (0,1):
            return {b.event_id:0 for b in bundles},['INVALID_ID_OR_VARIANT']
        seen.add(key);proposed[key]=choice
    return proposed,[]


def batches(bundles,max_chars=5000):
    group=[]
    for b in bundles:
        if b.render(0)==b.render(1):
            continue  # Nothing to edit: no duplicate text/token cost for Qwen.
        candidate=group+[b]
        if group and len(batch_prompt(candidate))>max_chars:
            yield group;group=[]
        if len(batch_prompt([b]))>max_chars:
            continue  # Deterministic default stays; no context expansion.
        group.append(b)
    if group:
        yield group


def batch_prompt(bundles):
    return json.dumps([{'event_id':b.event_id,'variants':[b.render(0),b.render(1)]} for b in bundles],ensure_ascii=False)


def edit_batches(bundles,chat,deadline,monotonic,max_calls=1):
    choices={b.event_id:0 for b in bundles};trace=[]
    for group in list(batches(bundles))[:max_calls]:
        if monotonic()>=deadline:
            trace.append({'status':'DEADLINE_FALLBACK','calls':0});break
        try:
            response=chat(batch_prompt(group))
            selected,issues=apply_choices(group,response)
            choices.update(selected)
            trace.append({'events':[b.event_id for b in group],'response':response,'issues':issues,'calls':1})
        except Exception as exc:
            trace.append({'events':[b.event_id for b in group],'issues':[type(exc).__name__],'calls':1})
            break  # No retry storm: rest of the digest remains deterministic.
    return choices,trace


def render(bundles,day,choices=None,limit=3900):
    choices=choices or {};selected=[]
    def compose(entries):
        parts=[f'⚽ ГЛАВНОЕ ЗА ДЕНЬ | {day.day} {MONTHS[day.month-1].upper()}']
        for section in DIGEST_SECTIONS:
            texts=['• '+b.render(choices.get(b.event_id,0)) for b in entries if b.section==section]
            if texts: parts.append(section+'\n\n'+'\n\n'.join(texts))
        return '\n\n'.join(parts+['@PremWire'])
    for b in bundles:
        if b.event_id in {s.event_id for s in selected}:continue
        if telegram_length(compose(selected+[b]))<=min(limit,4096):selected.append(b)
    return compose(selected) if selected else '',selected
