"""Reusable SOURCE grammars for the optional immutable digest, not live gates.

No player/club-specific event rules. Unknown proper names remain literal SOURCE
spellings rather than speculative transliterations or completed first names.
"""
import re
from .russian_editorial import club_entries


ROLE=r'(defender|midfielder|winger|attacker|striker|forward|goalkeeper)'
MONEY=r'([£€$])(\d+(?:\.\d+)?)m'


def extended_event(candidate, add, translate, source_text=None):
    raw=source_text if source_text is not None else candidate.source.text
    text=re.sub(r'^[^A-Za-zА-Яа-яЁё]+','',raw)
    url=candidate.source.canonical_url
    credit=re.search(r'\(([^()\n]+)\)\.?\s*$',text)
    body=text[:credit.start()].strip().rstrip('.') if credit else text.strip().rstrip('.')
    attribution=credit[1] if credit else None

    def fact(key,values,wording,evidence=raw):
        add(key,values,evidence,url,wording)

    def source_credit():
        if attribution:fact('attribution',(attribution,),f'Источник: {attribution}.')

    m=re.fullmatch(rf'(.+?) are interested in (.+?) {ROLE} (.+?), with the .+? club reportedly open to offers of {MONEY}',body)
    if m:
        dest,origin,role,person,currency,amount=m.groups()
        dest,origin=translate(dest),translate(origin)
        fact('interest',(person,origin,dest,'INTEREST',role),
             f'{dest} интересуется игроком {person} ({origin}).')
        fact('asking_price',(origin,currency,amount,'million','REPORTED_OPEN_TO_OFFERS'),
             f'По сообщению источника, {club_past(origin,"готов","готова")} рассмотреть предложения на {money(currency,amount)}.')
        source_credit()
        return 'TRANSFER_INTEREST',(person,origin,dest),65

    m=re.fullmatch(rf'(.+?) have joined the race to sign (.+?) {ROLE} (.+)',body)
    if m:
        dest,origin,role,person=m.groups();dest,origin=translate(dest),translate(origin)
        fact('interest',(person,origin,dest,'INTEREST',role),
             f'{club_past(dest,"включился","включилась")} в борьбу за переход {person} ({origin}).')
        source_credit()
        return 'TRANSFER_INTEREST',(person,origin,dest),60

    m=re.fullmatch(rf'(.+?) have increased the release clause of {ROLE} (.+?) to {MONEY} amid interest from (.+)',body)
    if m:
        origin,role,person,currency,amount,dest=m.groups();origin,dest=translate(origin),translate(dest)
        fact('release_clause',(person,origin,currency,amount,'million',role),
             f'{club_past(origin,"увеличил","увеличила")} отступные за {person} до {money(currency,amount)}.')
        fact('interest',(person,dest,'INTEREST'),f'Игроком интересуется {dest}.')
        source_credit()
        return 'RELEASE_CLAUSE',(person,origin,dest),65

    m=re.fullmatch(rf"(.+?) have held fresh talks with (.+?)'s (\d+)-year-old {ROLE} (.+?), but they have not made him an offer as they believe he is favouring a move to (.+?) or (.+)",body)
    if m:
        dest,origin,age,role,person,option1,option2=m.groups()
        dest,origin,option1,option2=map(translate,(dest,origin,option1,option2))
        fact('talks',(person,origin,dest,age,role,'TALKS'),
             f'{club_past(dest,"провёл","провела")} новые переговоры с {person} ({origin}, {age} лет).')
        fact('no_offer',(dest,person,'NO_PLAYER_OFFER'), 'Предложения игроку пока не было.')
        fact('club_belief',(dest,person,option1,option2,'CLUB_BELIEF_NOT_CONFIRMED_PREFERENCE'),
             f'В клубе считают, что он предпочитает другие направления: {option1} или {option2}.')
        source_credit()
        return 'TRANSFER_TALKS',(person,origin,dest,option1,option2),70

    m=re.search(r'^(.+?) work on new contracts but also on midfield options for (\d{4}), more likely summer than January\.',text)
    deals=re.search(r'After (.+?)’s new deal and (.+?) done, talks will follow with (.+?) — still early stages\.',text)
    if m and deals:
        club,year=translate(m[1]),m[2];first,second,target=map(translate,deals.groups())
        fact('plans',(club,year,'MIDFIELD','SUMMER_MORE_LIKELY_THAN_JANUARY'),
             f'{club} работает над новыми контрактами. Усиление полузащиты рассматривается на {year} год. Более вероятный срок — лето, а не январь.',m[0])
        fact('contract_stage',(club,first,second,target,'EARLY_TALKS_FOLLOW'),
             f'После соглашений с {first} и {second} планируются переговоры с {target}; работа над его контрактом пока на ранней стадии.',deals[0])
        return 'CONTRACT_PLANS',(club,first,second,target),65

    m=re.search(r'^(.+?) reaches (\d+) Premier League goals in (\d+) games\.',text)
    if not m:
        m=re.search(r'(?:^|\n)(.+?) with (\d+) goals in (\d+) PL games this season',text)
    if m:
        person,goals,games=translate(m[1]),m[2],m[3]
        fact('league_goals',(person,goals,games,'PREMIER_LEAGUE'),
             f'{person} забил {goals} голов в {games} матчах Премьер-лиги.',m[0].lstrip('\n'))
        return 'PLAYER_STATS',(person,),55

    m=re.fullmatch(r'(.+?): “(.+?) has a tight hamstring, this is why he’s not available today”\.?',text)
    if m:
        speaker,person=map(translate,m.groups())
        fact('availability',(person,'TODAY','TIGHT_HAMSTRING','UNAVAILABLE',speaker),
             f'По словам {speaker}, {person} сегодня не сможет сыграть из-за проблем с задней поверхностью бедра.')
        return 'INJURY',(person,),60
    return None


def money(currency,amount):
    return f'{amount.replace(".",",")} млн '+{'£':'фунтов','€':'евро','$':'долларов'}[currency]


def club_past(name,masculine,feminine):
    for club in club_entries():
        if club.display==name and club.grammatical_gender:
            return name+' '+(feminine if club.grammatical_gender=='feminine' else masculine)
    return 'Клуб '+name+' '+masculine
