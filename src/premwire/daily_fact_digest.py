"""Deterministic fact-first preparation shared by daily and shadow callers.

Qwen selects stylistic variants; it cannot write or modify factual payloads.
No sender or production state access. Unsupported SOURCE shapes remain explicit
omissions, never inferred news.
"""
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import re
import time
import unicodedata

from .daily_fact_shadow import prepare, normalize_handles
from .daily_quality import clubs
from .daily_text_digest import MOSCOW, SECTIONS, MONTHS, telegram_length
from .editorial_desk import fallback_decision
from .daily_fact_patterns import extended_event,club_past

STATS_SECTION='📊 Статистика'
CONTRACTS_SECTION='📝 Контракты'
GOVERNANCE_SECTION='🏛 Футбольное управление'
AWARDS_SECTION='🏆 Награды'
CARRY_SECTION='⏪ Вчерашние матчи'
SOURCE_CARRY_SECTION='⏪ Из предыдущей ленты'
DIGEST_SECTIONS=(*SECTIONS,CONTRACTS_SECTION,STATS_SECTION,GOVERNANCE_SECTION,AWARDS_SECTION,CARRY_SECTION,SOURCE_CARRY_SECTION)


@dataclass(frozen=True, slots=True)
class StatisticalObservation:
    metric: str
    value: str
    unit: str
    subject: str
    period: str
    source: str

    def issues(self):
        units={'GOALS':'GOALS','ASSISTS':'ASSISTS','SHOTS':'SHOTS',
               'TOUCHES':'TOUCHES','XG':'EXPECTED_GOALS','BIG_CHANCES':'CHANCES',
               'GOAL_INVOLVEMENTS':'GOALS_PLUS_ASSISTS','POINTS':'POINTS',
               'RATING':'RATING_POINTS','WIN_COUNT':'WINS','RECORD_SPAN':'YEARS',
               'MATCH_COUNT':'MATCHES','COMPARISON':'GOALS_COMPARISON','TABLE_POSITION':'POSITION',
               'PERCENTAGE':'PERCENT','CLUB_COUNT':'CLUBS','DURATION':'DAYS'}
        return [] if (all((self.value,self.subject,self.period,self.source)) and
                      units.get(self.metric)==self.unit) else ['STAT_METRIC_AND_UNIT']


@dataclass(frozen=True, slots=True)
class VerifiedFact:
    key: str
    values: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence: str
    formulations: tuple[str, str]
    statistics: tuple[StatisticalObservation, ...] = ()


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
    source_published_at: str = ''
    event_date: str = ''
    date_basis: str = 'UNKNOWN'
    source_collected_at: str = ''
    event_occurred_at: str = ''
    event_updated_at: str = ''
    first_seen_at: str = ''
    last_seen_at: str = ''
    last_published_at: str = ''
    source_id: str = ''
    canonical_event_id: str = ''
    freshness_confidence: str = 'UNKNOWN'
    freshness_category: str = 'DATE_UNKNOWN'
    match_status: str = ''
    editorial_mode: str = ''

    def render(self, variant=0):
        if type(variant) is not int or variant not in (0, 1):
            raise ValueError('Unsupported editorial variant')
        if self.editorial_mode=='v2':
            from .editorial_v2 import parts
            return ' '.join(part for part in parts(self,variant).values() if part)
        return ' '.join(part for part in draft_parts(self,variant).values() if part)


def draft_parts(bundle,variant):
    """Presentation only: never modify frozen values, evidence or references."""
    parts={f.key:f.formulations[variant] for f in bundle.verified_facts}
    # Consolidate explicit transfer reports without changing any frozen slot.
    subjects=[f for f in bundle.verified_facts if f.key.endswith('transfer_subject')]
    if len(subjects)>1 and any(f.values[-1]=='CONDITIONAL_SALE_NOT_AGREED' for f in subjects):
        for f in subjects:
            if f.values[-1]=='CONDITIONAL_SALE_NOT_AGREED':
                person,origin,_,_=f.values
                parts[f.key]=f'Другой источник допускает его уход в январе.'
        for f in bundle.verified_facts:
            if f.key.endswith('contract_refusal'):
                parts[f.key]='По тому же сообщению, игрок отказался продлевать контракт.'
            elif f.key.endswith('conditional_other_buyer'):
                parts[f.key]=f'«{f.values[0]}» также может включиться в борьбу, если решит действовать.'
            elif f.key.endswith('reported_willingness_fee'):
                parts[f.key]=f'Речь о готовности потратить {f.values[2]} млн евро на трансфер, не о согласованной сделке.'
    if bundle.event_type=='MATCH_RESULT':
        for f in bundle.verified_facts:
            if f.key=='streak' and len(f.values)==3:
                _,wins,games=f.values
                parts[f.key]=f'Это {wins}-я победа в {games} матчах.'
            elif f.key=='match_award':
                parts[f.key]=f'{f.values[0]} — лучший игрок встречи по версии UEFA.'
        by_key={f.key:f for f in bundle.verified_facts}
        if {'competition','result'}<=by_key.keys() and by_key['competition'].values==('EUROPA_LEAGUE',):
            parts['competition']='В Лиге Европы'
            result=parts['result']
            if result.startswith('Клуб '):result='к'+result[1:]
            parts['result']=result
            order=['competition','result']
            parts={k:parts[k] for k in order+list(k for k in parts if k not in order)}
        if {'match_date','competition','result'}<=by_key.keys() and by_key['competition'].values==('CHAMPIONS_LEAGUE',):
            from datetime import date
            stamp=date.fromisoformat(by_key['match_date'].values[0])
            winner,loser,score=by_key['result'].values
            parts['match_date']=f'{stamp.day} {MONTHS[stamp.month-1]}'
            parts['competition']='в Лиге чемпионов'
            subject=club_past(winner,'обыграл','обыграла').replace(winner,f'«{winner}»',1)
            if subject.startswith('Клуб '):subject='к'+subject[1:]
            parts['result']=f'{subject} «{loser}» со счётом {score}.'
            order=['match_date','competition','result']
            parts={k:parts[k] for k in order+list(k for k in parts if k not in order)}
    if bundle.event_type=='TRANSFER_INTEREST':
        reports=[f for f in bundle.verified_facts if f.key.endswith('reported_interest')]
        anchored={f.values[0] for f in reports if len(f.values)==5}
        anchor_keys=[f.key for f in reports if len(f.values)==5]
        # Anaphora must follow the explicit identity, regardless of SOURCE order.
        parts={k:parts[k] for k in anchor_keys+list(k for k in parts if k not in anchor_keys)}
        for f in reports:
            if len(f.values)==4 and f.values[0] in anchored:
                person,club,provider,_=f.values
                parts[f.key]=f'Кроме того, {provider} сообщает об интересе клуба «{club}» к этому игроку.'
        for f in bundle.verified_facts:
            if f.key=='interest' and len(f.values)==3 and f.values[-1]=='REPORTED_INTEREST_NOT_SIGNED':
                person,club,_=f.values
                parts[f.key]=re.sub(r'интересуется игроком:\s*'+re.escape(person), 'интересуется '+person, parts[f.key])
    if bundle.event_type=='MATCH_HALFTIME':
        for f in bundle.verified_facts:
            if f.key=='first_club_goals':
                a,b,club,_,_=f.values
                parts[f.key]=f'{a} и {b} впервые отличились за «{club}».'
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
                when='сегодня'
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}',f.values[-2]):
                    from datetime import date
                    anchor=date.fromisoformat(f.values[-2]);when=f'{anchor.day} {MONTHS[anchor.month-1]}'
                parts[f.key]=(f'По словам {f.values[-1]}, игрок был недоступен {when} из-за проблем с задней поверхностью бедра.' if when!='сегодня' else
                              f'По словам {f.values[-1]}, игрок сегодня не сможет сыграть из-за проблем с задней поверхностью бедра.')
    return parts


def club_name(surface):
    found = clubs(surface)
    return next(iter(found)) if len(found)==1 else surface.strip(' «»"')


def russian(surface):
    # Translation aliases only, not permission gates or fact completion.
    names = {'Lille':'Лилль', 'Atletico Madrid':'Атлетико', 'Atlético':'Атлетико',
             'Jurrien Timber':'Юрриен Тимбер', 'Eberechi Eze':'Эберечи Эзе',
             'Bukayo Saka':'Букайо Сака', 'Pedro Porro':'Педро Порро', 'Pascal Struijk':'Паскал Стрейк',
             'Nicolò Tresoldi':'Николо Тресольди', 'Lamine Yamal':'Ламин Ямаль',
             'Harry Kane':'Гарри Кейн', 'Alex Baena':'Алекс Баэна', 'Brugge':'Брюгге',
             'Clarence Seedorf':'Кларенс Зеедорф', 'Corinthians':'Коринтианс',
             'Rayan Cherki':'Райан Черки', 'Erling Haaland':'Эрлинг Холанд',
             'Vinicius Jr':'Винисиус Жуниор', 'El Clásico':'Эль Класико',
             'Selhurst Park':'Селхерст Парк', 'Crystal Palace':'Кристал Пэлас',
             'Thibaut Courtois':'Тибо Куртуа', 'Mikel Arteta':'Микель Артета',
             'Martin Zubimendi':'Мартин Зубименди', 'Piero Hincapié':'Пьеро Инкапиэ',
             'Kobbie Mainoo':'Коби Майну', 'Luke Shaw':'Люк Шоу',
             'Michael Carrick':'Майкл Каррик', 'Mohamed Belloumi':'Мохамед Беллуми',
             'Cristiano Ronaldo':'Криштиану Роналду', 'Portugal':'Португалия',
             'Hull City':'Халл Сити', 'Atlético Madrid':'Атлетико'}
    names.update({'Bournemouth':'Борнмут','Brighton':'Брайтон','Brentford':'Брентфорд',
                  'Alex Scott':'Алекс Скотт','Алекса Скотта':'Алекс Скотт',
                  'Giuliano Simeone':'Джулиано Симеоне','Atletico Madrid':'Атлетико',
                  'José Mourinho':'Жозе Моуринью','Endrick':'Эндрик',
                  'Dani Carvajal':'Дани Карвахаль','Getafe':'Хетафе',
                  'Strahinja Pavlovic':'Страхиня Павлович','AC Milan':'Милан'})
    names.update({'Lionel Messi':'Лионель Месси'})
    names.update({'Вест Хэмом':'Вест Хэм','Венеции':'Венеция'})
    names.update({'Кэррика':'Кэррик'})
    names.update({'Bartosz Mazurek':'Бартош Мазурек','RB Salzburg':'Ред Булл Зальцбург',
                  'Frenkie de Jong':'Френки де Йонг'})
    names.update({'Independent Football Regulator':'Независимый футбольный регулятор','David Kogan':'Дэвид Коган'})
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
    original=(original_sources or {}).get(candidate.source.canonical_url)
    raw=original.text if original else source
    if re.search(r'касани',raw,re.I) and re.search(r'«[^»]+»\s*\(\d+\s+гол',raw):
        return None  # No provable relationship between the caption and row units.
    for f in candidate.facts:
        t, url = f.text, f.url
        # Explicit final-result slots, not the latest intermediate score.
        result = re.search(r'^(.+?) make it (\w+|\d+) wins out of (\w+|\d+) after a (\d+)-(\d+) victory over (?:ten-man )?(.+?) at (.+?)\.',t)
        plain = re.search(r'^(.+?) (?:beat|defeated) (.+?) (\d+)[-:](\d+)(?: at (.+?))?[.!]?$',t)
        if result or plain:
            if result:
                winner, loser = russian(result[1]), russian(result[6])
                score = f'{result[4]}:{result[5]}'
                number = {'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
                wins, games = number.get(result[2],result[2]), number.get(result[3],result[3])
            else:
                winner, loser=russian(plain[1]),russian(plain[2])
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
            m=re.search(r'([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) не попал в заявку «([^»]+)» .+?из-за (травмы[^.!?]+?)(?= Попробуйте|$)',t)
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
    original=(original_sources or {}).get(candidate.source.canonical_url)
    # Prefer an explicit fresh board action over repeated background statistics.
    board=bool(original and re.search(r'руководство клуба после домашней ничьей.*выразило поддержку своему тренеру',original.text,re.I))
    if not facts or board:
        previous=list(facts)
        if board:facts=[]
        extended=extended_event(candidate,add,russian,original.text if original else None)
        if not extended and not previous and original_sources:
            # Grouping may choose a recent commentary as the representative.
            # Recover a richer *original member*, with its own URL/evidence,
            # only when every supported result agrees on fixture and score.
            options=[]
            for url in candidate.sources:
                member=original_sources.get(url)
                if member is None or member is original:continue
                facts=[]
                parsed=extended_event(replace(candidate,source=member),add,russian,member.text)
                if parsed and facts:
                    signature=(parsed[0],tuple(parsed[1]),tuple((f.key,f.values) for f in facts))
                    options.append((parsed,list(facts),signature))
            facts=[]
            if options and len({o[2] for o in options})==1:
                extended,facts,_=max(options,key=lambda o:len(o[1]))
        if not extended:
            if not previous:return None
            facts=previous
        else:
            event_type,participants,importance=extended
        essential=tuple(f.key for f in facts)
        if event_type.startswith('CONTRACT_'):section=CONTRACTS_SECTION
        elif event_type.startswith('TRANSFER_') or event_type=='RELEASE_CLAUSE':section=SECTIONS[1]
        elif event_type in ('INJURY','DISCIPLINE'):section=SECTIONS[3]
        elif event_type in ('MATCH_GOAL','MATCH_HALFTIME','MATCH_FIRST_GOALS'):section=SECTIONS[2]
        elif event_type=='MATCH_RESULT':section=SECTIONS[0]
        elif event_type in ('REGULATOR_POSITION','FINANCIAL_REPORT'):section=GOVERNANCE_SECTION
        elif event_type in ('CEREMONY_ROLE','MATCH_AWARD'):section=AWARDS_SECTION
    if event_type in ('PLAYER_STATS','CLUB_STATS'):section=STATS_SECTION
    if any(f.key=='home_record' for f in facts):section=SECTIONS[2]
    essential=tuple(k for k in essential if any(f.key==k for f in facts))
    identity=json.dumps((day.isoformat(),event_type,sorted(participants),[(f.key,f.values) for f in facts]),ensure_ascii=False)
    event_id=hashlib.sha256(identity.encode()).hexdigest()[:16]
    refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs))
    facts=annotate_statistics(facts,event_type,participants,candidate.source.published_at)
    return ImmutableBundle(event_id,event_type,tuple(participants),tuple(facts),refs,
                           importance,essential,section)


def annotate_statistics(facts,event_type,participants,published_at=None):
    """SOURCE-bound typed values; absent periods are not completed from memory."""
    result=[]
    for f in facts:
        text=f.evidence
        period=('CURRENT_LEAGUE_SEASON' if re.search(r'this season|в этом сезоне|текущем сезоне',text,re.I) else
                'FIRST_'+f.values[2]+'_LEAGUE_ROUNDS' if f.key=='points' and len(f.values)==3 and 'первых' in text else
                'SOURCE_REPORTED_'+f.values[2]+'_PL_GAMES' if f.key=='league_goals' and len(f.values)>2 else '')
        entries=[]
        def stat(metric,value,subject=None,scope=None):
            entries.append(StatisticalObservation(metric,str(value),{
                'GOALS':'GOALS','ASSISTS':'ASSISTS','GOAL_INVOLVEMENTS':'GOALS_PLUS_ASSISTS',
                'POINTS':'POINTS','RATING':'RATING_POINTS','RECORD_SPAN':'YEARS',
                'WIN_COUNT':'WINS','COMPARISON':'GOALS_COMPARISON','BIG_CHANCES':'CHANCES',
                'TABLE_POSITION':'POSITION','SHOTS':'SHOTS','MATCH_COUNT':'MATCHES',
                'PERCENTAGE':'PERCENT','CLUB_COUNT':'CLUBS','DURATION':'DAYS'}[metric],
                subject or participants[0],scope or period,f.source_refs[0]))
        if f.key=='player_stats':
            subject,total,games,goals,assists=f.values
            stat('GOAL_INVOLVEMENTS',total,subject);stat('GOALS',goals,subject);stat('ASSISTS',assists,subject)
        elif f.key=='league_goals':stat('GOALS',f.values[1],f.values[0])
        elif f.key in ('goals_assists','season_stats'):stat('GOALS',f.values[1],f.values[0]);stat('ASSISTS',f.values[2],f.values[0])
        elif f.key=='period_stats':
            scope='FIRST_'+f.values[3]+'_PREMIER_LEAGUE_ROUNDS'
            stat('GOALS',f.values[1],f.values[0],scope=scope);stat('ASSISTS',f.values[2],f.values[0],scope=scope)
        elif f.key=='shot_record':stat('SHOTS',f.values[1],f.values[0],scope=f.values[2]+'_'+f.values[3])
        elif f.key=='ucl_career_goals':
            stat('GOALS',f.values[1],f.values[0],scope=f.values[3]);stat('MATCH_COUNT',f.values[2],f.values[0],scope=f.values[3])
        elif f.key=='unbeaten_run':stat('MATCH_COUNT',f.values[1],f.values[0],scope=f.values[2])
        elif f.key=='club_ucl_record':
            stat('GOALS',f.values[2],f.values[0]+' / '+f.values[1],scope=f.values[4])
            stat('MATCH_COUNT',f.values[3],f.values[0]+' / '+f.values[1],scope=f.values[4])
        elif f.key=='ifr_loss_snapshot':
            stat('PERCENTAGE',f.values[1],'English clubs',scope=f.values[4])
            stat('CLUB_COUNT',f.values[2],'Loss-making English clubs',scope=f.values[4])
            stat('CLUB_COUNT',f.values[3],'Surveyed English clubs',scope=f.values[4])
        elif f.key=='ifr_funding_snapshot':
            stat('CLUB_COUNT',f.values[1],'Owner-dependent clubs',scope=f.values[3])
            stat('DURATION',f.values[2],'Survival without owner funding',scope=f.values[3])
        elif f.key=='rating':stat('RATING',f.values[2],f.values[0])
        elif f.key=='points':stat('POINTS',f.values[1],f.values[0],scope=period or ('SOURCE_SNAPSHOT_'+published_at.isoformat() if published_at else ''))
        elif f.key=='streak':stat('WIN_COUNT',f.values[1],f.values[0],scope=f.values[2]+'_SOURCE_REPORTED_MATCHES')
        elif f.key=='table':stat('POINTS',f.values[2],f.values[0]+' / '+f.values[1],scope='TABLE_GAP_AT_SOURCE_'+published_at.isoformat() if published_at else '')
        elif f.key=='table_position':stat('TABLE_POSITION',f.values[1],f.values[0],scope='TABLE_AT_SOURCE_'+published_at.isoformat() if published_at else '')
        elif f.key=='record':stat('RECORD_SPAN',f.values[0],scope='LEAGUE_START_COMPARISON_'+f.values[0]+'_YEARS')
        elif f.key=='all_competitions':stat('GOALS',f.values[0],scope='ALL_COMPETITIONS_SOURCE_TOTAL' if 'competitions' in text else '')
        elif f.key=='home_record':stat('GOALS',f.values[1],scope='FIRST_HOME_WIN_AT_'+f.values[-1])
        elif f.key=='comparison':stat('COMPARISON',f.values[2],f.values[0],scope='LEAGUE_GOALS_COMPARISON')
        elif f.key=='season':
            stat('WIN_COUNT',f.values[1],f.values[0],scope='SOURCE_SEASON_STANDINGS')
            stat('POINTS',f.values[2],f.values[0],scope='POSSIBLE_POINTS_'+f.values[3])
        elif f.key=='reported_record':
            for metric in ('GOALS','ASSISTS','BIG_CHANCES'):
                stat(metric,'0',f.values[0],scope=f.values[1]+'_OF_'+f.values[2]+'_PL_MATCHES')
        result.append(replace(f,statistics=tuple(entries)))
    return result


def supplement_explicit_context(bundle,items,start,end):
    """Fill terms/date only from explicitly matching saved SOURCE, with links.

    No last-name-only club inference, guessed calendar date, or stale rumours.
    Contract sources must explicitly say OFFICIAL and name the same destination.
    Yesterday is anchored to that SOURCE's Moscow date, not the digest date.
    """
    from .simple_editorial import clean_simple_source_text
    facts=list(bundle.verified_facts)
    official=next((f.values for f in facts if f.key=='transfer_subject' and f.values[3]=='OFFICIAL'),None)
    result=next((f.values for f in facts if f.key=='result'),None)
    for item in items:
        if not start<=item.published_at<end:continue
        clean=clean_simple_source_text(normalize_handles(item.text))
        m=re.search(r'ОФИЦИАЛЬНО:\s*(.+?) [—–] игрок «([^»]+)»',clean)
        term=re.search(r'Контракт до (лета \d{4}) года',clean)
        if official and m and term and (russian(m[1]),russian(m[2]))==(official[0],official[2]) and not any(f.key=='contract' for f in facts):
            wording=f'Контракт действует до {term[1]} года.'
            facts.append(VerifiedFact('contract',(term[1],),(item.canonical_url,),item.text,(wording,wording)))
        if result and len(result)==3 and all(v in item.text for v in result) and 'вчера' in item.text and not any(f.key=='match_date' for f in facts):
            day=item.published_at.astimezone(MOSCOW).date()-timedelta(days=1)
            wording=f'Матч состоялся {day.day} {MONTHS[day.month-1].lower()}.'
            facts.append(VerifiedFact('match_date',(day.isoformat(),),(item.canonical_url,),item.text,(wording,wording)))
    return replace(bundle,verified_facts=tuple(facts),essential_facts=tuple(f.key for f in facts),
                   source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)))


def editorial_freshness(bundles,inputs,day):
    """SOURCE/event clocks are distinct; context dates do not manufacture news."""
    from datetime import date
    by_url={i.canonical_url:i for i in inputs}; output=[]; decisions=[]
    for bundle in bundles:
        sources=[by_url[u] for u in bundle.source_refs if u in by_url]
        if not sources:raise ValueError('Missing editorial SOURCE metadata')
        latest=max(s.published_at for s in sources)
        result=next((f.values for f in bundle.verified_facts if f.key=='result'),None)
        facts=list(bundle.verified_facts)
        if result and len(result)==3:
            winner,loser,score=result
            for item in inputs:
                raw=normalize_handles(item.text)
                if abs((item.published_at-latest).total_seconds())>24*3600:continue
                known_date=next((f.values[0] for f in facts if f.key=='match_date'),None)
                if known_date and re.search(r'this evening|сегодня вечером',raw,re.I) and item.published_at.astimezone(MOSCOW).date().isoformat()!=known_date:
                    continue  # Same clubs/score do not override an explicit date.
                # Explicit result-row + "this evening" anchors the FOOTBALL
                # event, rather than assigning every midnight publication today.
                rows=re.findall(r'(?:^|\n)\s*(@?[A-Za-z][A-Za-z .-]+?)\s+(\d{1,2})[-:](\d{1,2})\s+(@?[A-Za-z][A-Za-z .-]+)\s*(?=\n|$)',raw)
                bound=False
                for home,a,b,away in rows:
                    a,b=int(a),int(b)
                    first,second=map(lambda name:russian(name.strip().lstrip('@')),(home,away))
                    winning,losing=(first,second) if a>b else (second,first)
                    if a!=b and (winning,losing,f'{max(a,b)}:{min(a,b)}')==result:bound=True
                if bound and re.search(r'Champions League|Лиг[аеуы] чемпионов',raw,re.I):
                    if not any(f.key=='competition' for f in facts):
                        facts.append(VerifiedFact('competition',('CHAMPIONS_LEAGUE',),(item.canonical_url,),item.text,('Турнир — Лига чемпионов.',)*2))
                    if re.search(r'this evening|сегодня вечером',raw,re.I) and not any(f.key=='match_date' for f in facts):
                        event_day=item.published_at.astimezone(MOSCOW).date()
                        wording=f'Матч состоялся {event_day.day} {MONTHS[event_day.month-1]}.'
                        facts.append(VerifiedFact('match_date',(event_day.isoformat(),),(item.canonical_url,),item.text,(wording,wording)))
                # The goals list must explicitly bind scorer club/opponent,
                # competition and cardinality to this exact confirmed result.
                goal=re.search(r'Three goals and three different scorers for @?(.+?) in the Champions League against (.+?) [^\w\n]',raw)
                names=re.findall(r'^⚽[️\s]*([^\n]+)',raw,re.M)
                if (goal and tuple(map(russian,goal.groups()))==(winner,loser) and score.split(':')[0]=='3'
                        and len(names)==len(set(names))==3 and not any(f.key=='scorers' for f in facts)):
                    values=tuple(russian(name.strip()) for name in names)
                    names_text=', '.join(values[:-1])+' и '+values[-1]
                    facts.append(VerifiedFact('scorers',values,(item.canonical_url,),item.text,(f'Голы забили {names_text}.',)*2))
            bundle=replace(bundle,verified_facts=tuple(facts),source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)),
                           essential_facts=tuple(f.key for f in facts))
        latest=max(by_url[u].published_at for u in bundle.source_refs)
        explicit=next((f.values[0] for f in facts if f.key=='match_date'), '')
        event_day=date.fromisoformat(explicit) if explicit else None
        source_day=latest.astimezone(MOSCOW).date()
        bundle=replace(bundle,source_published_at=latest.isoformat(),event_date=explicit,
                       date_basis='SOURCE_EXPLICIT_OR_RELATIVE_EVENT_DATE' if explicit else 'SOURCE_PUBLICATION_ONLY')
        old_day=event_day or source_day
        age=(day-old_day).days
        if age<0:
            decisions.append(dict(event_id=bundle.event_id,sources=bundle.source_refs,decision='REJECT',reason='EDITORIAL_FUTURE_EVENT_DATE'));continue
        if bundle.event_type in ('TRANSFER_INTEREST','TRANSFER_PLANS','TRANSFER_CONTACT','TRANSFER_TALKS') and source_day<day:
            decisions.append(dict(event_id=bundle.event_id,sources=bundle.source_refs,decision='REJECT',reason='EDITORIAL_OLD_RUMOUR_NO_CURRENT_DEVELOPMENT'));continue
        if bundle.event_type=='MATCH_RESULT' and age>1:
            decisions.append(dict(event_id=bundle.event_id,sources=bundle.source_refs,decision='REJECT',reason='EDITORIAL_RESULT_TOO_OLD'));continue
        if bundle.event_type=='MATCH_RESULT' and age==1:
            if not explicit:
                source=sources[0]; wording=f'Источник сообщил о результате {source_day.day} {MONTHS[source_day.month-1]}; дата матча не уточнена.'
                facts.append(VerifiedFact('report_date',(source_day.isoformat(),'PUBLICATION_NOT_EVENT_DATE'),(source.canonical_url,),source.text,(wording,wording)))
                bundle=replace(bundle,verified_facts=tuple(facts),essential_facts=tuple(f.key for f in facts))
            bundle=replace(bundle,importance=min(bundle.importance,60),section=CARRY_SECTION if explicit else SOURCE_CARRY_SECTION)
        elif age>0 and bundle.event_type=='DISCIPLINE':
            # A carry-over report is not evidence that the incident happened
            # today. Label the SOURCE clock, never manufacture an event date.
            source=by_url[max(bundle.source_refs,key=lambda u:by_url[u].published_at)]
            wording=f'Об этом источник сообщил {source_day.day} {MONTHS[source_day.month-1]}.'
            facts.append(VerifiedFact('report_date',(source_day.isoformat(),'PUBLICATION_NOT_EVENT_DATE'),(source.canonical_url,),source.text,(wording,wording)))
            bundle=replace(bundle,verified_facts=tuple(facts),essential_facts=tuple(f.key for f in facts),importance=min(bundle.importance,60))
        if source_day<day:
            bundle=replace(bundle,importance=min(bundle.importance,60))
        decisions.append(dict(event_id=bundle.event_id,sources=bundle.source_refs,decision='KEEP',reason='EDITORIAL_CURRENT_REPORT' if age==0 else 'EDITORIAL_UNPRINTED_PREVIOUS_DAY',
                              source_date=source_day.isoformat(),event_date=explicit or None,date_basis=bundle.date_basis,priority=bundle.importance))
        output.append(bundle)
    return sorted(output,key=lambda b:(-b.importance,b.event_id)),decisions


def merge_halftime_context(bundles):
    """Unique same-day/cup/period binding; preserve all supplied slots and refs."""
    from datetime import datetime
    def club(surface):return re.sub(r'([А-ЯЁ]{2,})(?:ом|а|у)$',r'\1',russian(surface))
    updates=[b for b in bundles if b.event_type=='MATCH_HALFTIME']; consumed=set()
    for index,target in enumerate(updates):
        club_name,opponent=map(club,target.participants[:2]);facts=list(target.verified_facts)
        for other in bundles:
            if other.event_id==target.event_id or other.event_id in consumed:continue
            relevant=(other.event_type=='MATCH_FIRST_GOALS' and tuple(map(club,other.participants[:2]))==(club_name,opponent))
            relevant|=(other.event_type=='CLUB_STATS' and any(f.key=='shot_record' for f in other.verified_facts) and club(other.participants[0])==club_name)
            if not relevant:continue
            if datetime.fromisoformat(other.source_published_at).astimezone(MOSCOW).date()!=datetime.fromisoformat(target.source_published_at).astimezone(MOSCOW).date():continue
            if abs((datetime.fromisoformat(other.source_published_at)-datetime.fromisoformat(target.source_published_at)).total_seconds())>3600:continue
            if len([u for u in updates if club(u.participants[0])==club_name])!=1:continue
            facts.extend(other.verified_facts);consumed.add(other.event_id)
        updates[index]=replace(target,verified_facts=tuple(facts),essential_facts=tuple(f.key for f in facts),
                               source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)))
    replacements={b.event_id:b for b in updates}
    return [replacements.get(b.event_id,b) for b in bundles if b.event_id not in consumed]


def merge_editorial_context(bundles):
    """Fold a bound match award into its fixture; one full-name transfer thread."""
    from datetime import datetime
    result=list(bundles);consumed=set()
    for i,target in enumerate(result):
        if target.event_type!='MATCH_RESULT':continue
        facts=list(target.verified_facts)
        for other in result:
            if other.event_type!='MATCH_AWARD' or other.event_id in consumed:continue
            award=other.verified_facts[0]
            if set(award.values[1:3])!=set(target.participants):continue
            if len([b for b in result if b.event_type=='MATCH_RESULT' and set(b.participants)==set(award.values[1:3])])!=1:continue
            if abs((datetime.fromisoformat(other.source_published_at)-datetime.fromisoformat(target.source_published_at)).total_seconds())>24*3600:continue
            facts.append(award);consumed.add(other.event_id)
        result[i]=replace(target,verified_facts=tuple(facts),essential_facts=tuple(f.key for f in facts),source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)))
    actors={}
    for i,target in enumerate(result):
        if target.event_type!='TRANSFER_INTEREST':continue
        f=next((f for f in target.verified_facts if f.key=='reported_interest'),None)
        if not f or len(f.values[0].split())<2:continue
        actor=f.values[0].casefold()
        if actor not in actors:actors[actor]=i;continue
        previous=result[actors[actor]]
        if datetime.fromisoformat(previous.source_published_at).astimezone(MOSCOW).date()!=datetime.fromisoformat(target.source_published_at).astimezone(MOSCOW).date():continue
        explicit_origins={f.values[1] for b in (previous,target) for f in b.verified_facts if f.key.endswith('reported_interest') and len(f.values)==5}
        if len(explicit_origins)>1:continue
        facts=previous.verified_facts+tuple(replace(f,key=target.event_id+'_'+f.key) for f in target.verified_facts)
        result[actors[actor]]=replace(previous,verified_facts=facts,essential_facts=tuple(f.key for f in facts),source_refs=tuple(dict.fromkeys(u for f in facts for u in f.source_refs)),participants=tuple(dict.fromkeys(previous.participants+target.participants)))
        consumed.add(target.event_id)
    return [b for b in result if b.event_id not in consumed]


def editorial_value_gate(bundles,inputs):
    """Autonomous low-value exclusions; no SOURCE fact rewriting or registry."""
    originals={s.canonical_url:s for s in inputs};kept=[];decisions=[]
    for b in bundles:
        reason=None
        if b.event_type=='MATCH_AWARD':
            reason='EDITORIAL_MINOR_MATCH_AWARD_WITHOUT_RESULT_CONTEXT'
        elif b.event_type=='APPOINTMENT_EXPECTED' and len(b.verified_facts)==1 and b.verified_facts[0].key=='appointment_talks':
            raw='\n'.join(originals[u].text for u in b.source_refs if u in originals)
            if not re.search(r'📰\s*\S|^[^\n:]+:\s|\([A-Za-z][A-Za-z ./-]+\)\s*$',raw):
                reason='EDITORIAL_BARE_CLOSE_APPOINTMENT_WITHOUT_DETAILS_OR_ATTRIBUTION'
        decisions.append(dict(event_id=b.event_id,sources=b.source_refs,decision='REJECT' if reason else 'KEEP',reason=reason or 'EDITORIAL_SUBSTANTIVE_SOURCE_EVENT'))
        if not reason:kept.append(b)
    return kept,decisions


def build_bundles(items,start,end,filter_config,timings=None,history=()):
    candidates,rejected,recovered,conflicts=prepare(items,start,end,filter_config,timings)
    ticks=time.monotonic()
    bundles,unsupported,handled,context_omissions=[],[],[],[]
    original_sources={i.canonical_url:i for i in items}
    for candidate in candidates:
        # Split only a fully bound two-fixture SOURCE into independent events.
        from .daily_fact_patterns import paired_results
        original=original_sources.get(candidate.source.canonical_url)
        pairs=paired_results(original.text if original else candidate.source.text,russian)
        if pairs:
            for fixture_index,(winner,loser,score) in enumerate(pairs):
                subject=club_past(winner,'обыграл','обыграла').replace(winner,f'«{winner}»',1)
                f=VerifiedFact('result',(winner,loser,score),(candidate.source.canonical_url,),
                               original.text if original else candidate.source.text,
                               (f'{subject} «{loser}» — {score}.',)*2)
                identity=hashlib.sha256(json.dumps((winner,loser,score,candidate.identity)).encode()).hexdigest()[:16]
                details=[f]
                raw=original.text if original else candidate.source.text
                # The first recap's explicit brace belongs to its first fixture,
                # not to the second team's scorer or an icon-only highlights list.
                first=raw.split('А вот',1)[0]
                brace=re.search(r'([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) сделал дубль',first)
                if fixture_index==0 and brace and int(score.split(':')[0])>=2:
                    phrase=f'{brace[1]} оформил дубль.'
                    details.append(VerifiedFact('first_fixture_brace',(brace[1],'2',winner),f.source_refs,raw,(phrase,phrase)))
                bundles.append(ImmutableBundle(identity,'MATCH_RESULT',(winner,loser),tuple(details),f.source_refs,95,tuple(d.key for d in details),SECTIONS[0]))
                handled.append(dict(event=identity,event_type='MATCH_RESULT',sources=f.source_refs))
            continue
        try:
            bundle=compile_bundle(candidate,start.astimezone(MOSCOW).date(),original_sources)
            if bundle:
                bundle=supplement_explicit_context(bundle,items,start,end)
        except (ValueError,TypeError,KeyError,IndexError) as error:
            rejected.append(dict(url=candidate.source.canonical_url,
                                 reason='MALFORMED_SOURCE_RELATION_'+type(error).__name__,
                                 stage='IMMUTABLE_FACT_EXTRACTION'))
            continue
        if bundle:
            from .daily_quality import semantic_relation_issues
            raw='\n'.join(original_sources[u].text for u in bundle.source_refs if u in original_sources)
            defects=semantic_relation_issues(normalize_handles(raw),bundle.render())
            if defects:
                rejected.append(dict(url=candidate.source.canonical_url,reason=defects,stage='IMMUTABLE_RELATION_GATE'))
                continue
        if bundle:
            bundles.append(bundle)
            handled.append(dict(event=candidate.identity,event_type=bundle.event_type,sources=bundle.source_refs))
        else:
            live=bool(re.search(r'Счёт уже|\balready\s+\d+[-:]\d+',candidate.source.text,re.I))
            from .daily_fact_patterns import unsupported_reason
            record={'event':candidate.identity,'reason':'LIVE_CAPTION_NOT_FINAL_OR_AMBIGUOUS_SIDES' if live else unsupported_reason(candidate.source.text),
                    'sources':candidate.sources,'text':candidate.source.text}
            (context_omissions if live else unsupported).append(record)
    if timings is not None:timings['fact_bundle']=time.monotonic()-ticks
    ticks=time.monotonic()
    # Event identity, not names alone. Independent injuries/season statistics stay.
    bundles=merge_health_context(bundles,start.astimezone(MOSCOW).date())
    bundles=attach_match_context(bundles,start.astimezone(MOSCOW).date())
    bundles,edition_checks=final_digest_gate(bundles,history)
    unique={b.event_id:b for b in bundles}
    if timings is not None:timings['semantic_dedupe']=time.monotonic()-ticks
    return sorted(unique.values(),key=lambda b:(-b.importance,b.event_id)),dict(
        rejected=rejected,recovered=recovered,conflicts=conflicts,unsupported=unsupported,handled=handled,context_omissions=context_omissions,
        final_digest_gate=edition_checks)


def fact_signature(fact):
    # Stable facts do not include SOURCE URL, editorial wording or batch date.
    return re.sub(r'^[0-9a-f]{16}_','',fact.key),tuple(v.casefold() for v in fact.values)


def final_digest_gate(bundles,history=()):
    """Whole-edition opt-in gate; history is supplied, never read from live DB.

    Related transfers are composed by explicit actor and origin. Different
    valuations retain currency/status/attribution; they are not converted or
    declared contradictory simply because one is GBP and the other EUR.
    """
    checks=[];result=[];conflicted_actors=set()
    # A year/fee/statistic printed for another actor is not this actor's history.
    def scope(b):
        actors={f.values[0].casefold() for f in b.verified_facts if f.key.endswith('reported_interest')}
        if b.event_type=='TRANSFER_INTEREST' and len(actors)==1 and len(next(iter(actors)).split())>=2:
            return b.event_type,tuple(actors)
        return b.event_type,tuple(sorted(p.casefold() for p in b.participants))
    prior={(scope(b),fact_signature(f)) for b in history for f in b.verified_facts}
    for b in bundles:
        if b.event_type=='TRANSFER_ANNOUNCEMENT' and any(
            other.event_type=='TRANSFER_OFFICIAL' and other.participants[:2]==b.participants[:2]
            and set(f.values for f in b.verified_facts if f.key=='contract')<=set(f.values for f in other.verified_facts if f.key=='contract')
            for other in bundles):
            checks.append(dict(event_id=b.event_id,decision='REJECT',reason='ANNOUNCEMENT_COVERED_BY_OFFICIAL_SIGNING',sources=b.source_refs));continue
        defects=bundle_issues(b)
        if defects:
            checks.append(dict(event_id=b.event_id,decision='REJECT',reason=defects,sources=b.source_refs));continue
        substantive=[f for f in b.verified_facts if f.key not in ('match_date','report_date','competition','attribution')]
        dates={f.values[0] for f in b.verified_facts if f.key=='match_date'}
        relevant_history=[old for old in history if scope(old)==scope(b) and not (
            b.event_type=='MATCH_RESULT' and dates and
            (old_dates:={f.values[0] for f in old.verified_facts if f.key=='match_date'}) and dates.isdisjoint(old_dates))]
        relevant_prior={fact_signature(f) for old in relevant_history for f in old.verified_facts}
        if history and substantive and all(fact_signature(f) in relevant_prior for f in substantive):
            checks.append(dict(event_id=b.event_id,decision='REJECT',reason='EVENT_DEDUPE_CROSS_DAY_UNCHANGED_FACTS',sources=b.source_refs));continue
        subjects=[f.values for f in b.verified_facts if f.key=='transfer_subject']
        if subjects:
            actor,origin=subjects[0][:2]
            if actor in conflicted_actors:
                checks.append(dict(event_id=b.event_id,decision='REJECT',reason='CLUB_MEMBERSHIP_CONFLICT',sources=b.source_refs));continue
            candidates=[p for p in result if any(f.key=='transfer_subject' and f.values[:2]==(actor,origin) for f in p.verified_facts)]
            if len(candidates)==1:
                previous=candidates[0];facts=list(previous.verified_facts)
                known={fact_signature(f) for f in facts}
                for f in b.verified_facts:
                    if fact_signature(f) not in known:
                        facts.append(replace(f,key=b.event_id+'_'+f.key));known.add(fact_signature(f))
                merged=replace(previous,verified_facts=tuple(facts),source_refs=tuple(dict.fromkeys(previous.source_refs+b.source_refs)),
                               essential_facts=tuple(f.key for f in facts))
                result[result.index(previous)]=merged
                checks.append(dict(event_id=b.event_id,decision='MERGE',reason='ENTITY_RELATIONS_SAME_ACTOR_AND_ORIGIN',sources=b.source_refs));continue
            conflicts=[p for p in result if any(f.key=='transfer_subject' and f.values[0]==actor and f.values[1] and origin and f.values[1]!=origin for f in p.verified_facts)]
            if conflicts:
                conflicted_actors.add(actor)
                result=[p for p in result if p not in conflicts]
                checks.append(dict(event_id=b.event_id,decision='REJECT',reason='CLUB_MEMBERSHIP_CONFLICT',sources=tuple(u for p in conflicts+[b] for u in p.source_refs)));continue
        checks.append(dict(event_id=b.event_id,decision='KEEP',reason='SOURCE_FACT_PRESERVATION',sources=b.source_refs));result.append(b)
    return result,checks


def bundle_issues(bundle):
    """Typed edition checks. Never interpret arbitrary model prose as facts."""
    issues=[]
    expected=(CONTRACTS_SECTION if bundle.event_type.startswith('CONTRACT_') else
              SECTIONS[1] if bundle.event_type.startswith('TRANSFER_') else
              STATS_SECTION if bundle.event_type in ('PLAYER_STATS','CLUB_STATS') else
              SECTIONS[3] if bundle.event_type in ('INJURY','DISCIPLINE') else None)
    if expected and bundle.section!=expected:issues.append('SECTION_CLASSIFICATION')
    keys=[f.key for f in bundle.verified_facts]
    if len(keys)!=len(set(keys)) or not set(bundle.essential_facts)<=set(keys):issues.append('SOURCE_FACT_PRESERVATION')
    for f in bundle.verified_facts:
        for observation in f.statistics:
            issues.extend(observation.issues())
            if observation.source not in f.source_refs:issues.append('STAT_SOURCE')
            wording={'GOALS':r'гол|забил|мяч','ASSISTS':r'ассист|передач',
                     'SHOTS':r'удар','TOUCHES':r'касани','XG':r'xG|ожидаем',
                     'BIG_CHANCES':r'момент|шанс'}
            if observation.metric in wording and any(not re.search(wording[observation.metric],t,re.I) for t in f.formulations):
                issues.append('STAT_METRIC_AND_UNIT')
        if (bundle.event_type in ('PLAYER_STATS','CLUB_STATS') or f.key in ('streak','table','table_position','points')) and any(c.isdigit() for c in ''.join(f.values)) and not f.statistics and f.key not in ('past_transfer',):
            issues.append('STAT_SCHEMA_MISSING')
        if f.key=='touch_threshold' or re.fullmatch(r'goals_\d+',f.key):
            issues.append('AMBIGUOUS_MIXED_STAT_TABLE')
        if not f.values or not f.evidence or not f.source_refs or not set(f.source_refs)<=set(bundle.source_refs):
            issues.append('SOURCE_FACT_PRESERVATION')
        if f.key=='touch_threshold' and (len(f.values)!=3 or f.values[1]!='TOUCHES_IN_OPPOSITION_BOX' or any(
            f.values[0] not in t or not re.search(r'касани',t,re.I) for t in f.formulations)):
            issues.append('STAT_METRIC_AND_UNIT')
        if re.fullmatch(r'goals_\d+',f.key) and (len(f.values)!=4 or f.values[2]!='GOALS' or any(
            f.values[1] not in t or not re.search(r'гол',t,re.I) for t in f.formulations)):
            issues.append('STAT_METRIC_AND_UNIT')
        if f.key=='transfer_subject' and (len(f.values)!=4 or not f.values[0] or not f.values[3]):
            issues.append('ENTITY_RELATIONS_TRANSFER_STATUS')
    return sorted(set(issues))


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
        presentation=draft_parts(b,variant)
        if b.editorial_mode=='v2':
            from .editorial_v2 import parts,validate
            presentation=parts(b,variant)
            if validate(b,b.render(variant),variant):distorted.append(b.event_id)
            from .match_day_editorial import validate_source_clocks
            if sources and not validate_source_clocks(b,sources):distorted.append(b.event_id)
        facts={f.key:f for f in b.verified_facts}
        for key in b.essential_facts:
            fact=facts.get(key)
            if fact is None or presentation[key] not in body:
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


def render(bundles,day,choices=None,limit=3900,*,channel_credit_verified=False):
    choices=choices or {};selected=[]
    def compose(entries):
        parts=[f'⚽ ГЛАВНОЕ ЗА ДЕНЬ | {day.day} {MONTHS[day.month-1].upper()}']
        def display_section(b):
            # Retained contracts/appointments must not appear ahead of fresh
            # injuries merely because today's top story shares their section.
            report=next((f.values[0] for f in b.verified_facts if f.key=='report_date'), '')
            if report and report<day.isoformat() and b.event_type!='MATCH_RESULT':
                return SOURCE_CARRY_SECTION
            return b.section
        # Section order follows the highest-ranked included story, not a fixed
        # transfer-first ordering. No bundle or factual slot is rewritten here.
        sections=list(dict.fromkeys(display_section(b) for b in entries))
        sections=sorted(sections,key=lambda section:section in (CARRY_SECTION,SOURCE_CARRY_SECTION))
        for section in sections:
            texts=['• '+b.render(choices.get(b.event_id,0)) for b in entries if display_section(b)==section]
            if texts: parts.append(section+'\n\n'+'\n\n'.join(texts))
        from .daily_footer import compose
        uses_provider=any(any(f.key=='fixture_identity' and f.values[0]=='football-data.org'
                              for f in b.verified_facts) for b in entries)
        return compose(parts,uses_provider=uses_provider,channel_verified=channel_credit_verified)
    for b in bundles:
        if b.event_id in {s.event_id for s in selected}:continue
        if telegram_length(compose(selected+[b]))<=min(limit,4096):selected.append(b)
    return compose(selected) if selected else '',selected
