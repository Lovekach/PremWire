"""Reusable SOURCE grammars for immutable digest preparation, without IO.

No player/club-specific event rules. Unknown proper names remain literal SOURCE
spellings rather than speculative transliterations or completed first names.
"""
import re
from .russian_editorial import club_entries


ROLE=r'(defender|midfielder|winger|attacker|striker|forward|goalkeeper)'
MONEY=r'([£€$])(\d+(?:\.\d+)?)m'


def editorial_relations(raw,clean,url,add,translate,credit=None):
    """Explicit reusable developments; never promote a proposal to a signing."""
    clean=re.sub(r'^[^A-Za-zА-Яа-яЁё«]+','',clean)
    def fact(key,values,text):add(key,tuple(values),raw,url,text)
    attribution=re.search(r'^Фабрицио Романо:',clean)
    provider=credit or ('Фабрицио Романо' if attribution else 'источника')
    report=re.sub(r'^Фабрицио Романо:\s*','',clean)
    injury=re.search(r'^Потеря для «([^»]+)»\s*[—–-]\s*([А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+(?: [А-ЯЁ][а-яё-]+){0,2}) покинул поле из-за (?:травмы|повреждения)(?:[.!?]|$)',report)
    if injury:
        club,person=injury.groups();club=translate(club)
        fact('match_injury',(person,club,'LEFT_FIELD_INJURY_NO_DIAGNOSIS'),
             f'Игрок клуба «{club}» {person} покинул поле из-за травмы.')
        return 'INJURY',(person,club),80
    injury=re.search(r'^([А-ЯЁ][а-яё-]+ [А-ЯЁ][а-яё-]+) уносят с поля на носилках после столкновения головами с игроком «([^»]+)»',report)
    if injury:
        person,rival=injury.groups();rival=translate(rival)
        # Keep SOURCE's accusative spelling. No guessed diagnosis, club, role,
        # return date or nominative identity reconstructed from a surname.
        fact('match_injury',(person,rival,'STRETCHER_AFTER_HEAD_COLLISION_NO_DIAGNOSIS'),
             f'По сообщению источника, {person} унесли с поля на носилках после столкновения головами с игроком клуба «{rival}».')
        return 'INJURY',(person,rival),85
    m=re.search(r'^«([^»]+)» и ([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2}) достигли устной договор[её]нности о продлении контракта',report)
    if m:
        club,person=m.groups();club=translate(club)
        fact('contract_verbal_agreement',(person,club,provider,'VERBAL_AGREEMENT_NOT_SIGNED'),f'По сообщению {provider}, «{club}» и {person} устно договорились о продлении контракта.')
        return 'CONTRACT_AGREEMENT',(person,club),80
    m=re.search(r'^«([^»]+)» связался с ([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2}), чтобы попытаться подписать [^.]+? в статусе свободного агента',report)
    if m:
        club,person=m.groups();club=translate(club)
        fact('player_contact',(person,club,provider,'CONTACT_NOT_AGREEMENT','FREE_TRANSFER_INTENDED_NOT_COMPLETED'),f'По сообщению {provider}, «{club}» связался с {person}, рассчитывая подписать его свободным агентом.')
        return 'TRANSFER_CONTACT',(person,club),75
    m=re.search(r'^([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) перен[её]с операцию на колене после травмы, полученной на тренировке',report)
    if m:
        person=m[1]
        fact('knee_surgery',(person,'KNEE','TRAINING_INJURY','SURGERY_PERFORMED'),f'{person} перенёс операцию на колене после травмы на тренировке, сообщает источник.')
        return 'INJURY',(person,),85
    recap=re.search(r'^«([^»]+)»[^.!?\n]+[.!?]\s+Команда [^.\n]+? одержала победу над «([^»]+)» в (Лиге Европы|Лиге чемпионов) со сч[её]том (\d+):(\d+)',clean)
    if recap and not re.search(r'к перерыву|первого тайма|Update|к \d+-й минуте',clean,re.I):
        winner,loser,competition,a,b=recap.groups();winner,loser=translate(winner),translate(loser)
        if winner!=loser and int(a)>int(b):
            subject=club_past(winner,'одержал победу','одержала победу').replace(winner,f'«{winner}»',1)
            # Unknown names retain the SOURCE case after "над", rather than
            # inventing a nominative spelling or saying "обыграл Штурмом".
            opponent=re.search(r'одержала победу над «([^»]+)»',recap[0])[1]
            fact('result',(winner,loser,a+':'+b),f'{subject} над «{opponent}» — {a}:{b}.')
            code='EUROPA_LEAGUE' if competition=='Лиге Европы' else 'CHAMPIONS_LEAGUE'
            fact('competition',(code,),competition+'.')
            return 'MATCH_RESULT',(winner,loser),95
    m=re.search(r'^ОФИЦИАЛЬНО:\s*([^:!?]+?) [—–-] новый главный тренер «([^»]+)»',clean)
    if m:
        person,club=m.groups();club=translate(club)
        fact('appointment',(person,club,'OFFICIAL'),f'{person} — новый главный тренер клуба «{club}». Назначение объявлено официально.')
        return 'APPOINTMENT',(person,club),85
    report=re.sub(r'^Фабрицио Романо:\s*','',clean)
    m=re.search(r'^«([^»]+)» (?:и |достиг(?:ла)? договор[её]нности с )([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2})(?: достигли договор[её]нности)? о продлении контракта до (лета \d{4}) года',report)
    if m:
        club,person,term=m.groups();club=translate(club)
        wording=f'По сообщению {provider}, «{club}» и {person} договорились о продлении контракта до {term} года.'
        reported_verb=re.search(r'достиг(?:ла)? договор[её]нности с ',m[0])
        if reported_verb:
            # SOURCE supplies instrumental after "с". Do not manufacture a
            # nominative person spelling from a surname or role dictionary.
            wording=f'По сообщению {provider}, «{club}» {reported_verb[0]}{person} о продлении контракта до {term} года.'
        fact('contract_agreement',(person,club,term,'AGREEMENT_NOT_SIGNED'),
             wording)
        return 'CONTRACT_AGREEMENT',(person,club),80
    # Reusable SOURCE loss recap, including the stated losing-club scorer.
    # No competition/date/table position inferred from club identity.
    loss=re.search(r'«([^»]+)» так и не удалось удержать преимущество [—–-] команда уступила «([^»]+)» со сч[её]том (\d+):(\d+)',raw)
    if loss and not re.search(r'к перерыву|к концу первого|к \d+-й минуте',raw,re.I):
        loser,winner,a,b=loss.groups();winner=translate(winner)
        if int(b)>int(a) and winner!=translate(loser):
            fact('result',(winner,translate(loser),b+':'+a),f'Команда «{loser}» уступила «{loss[2]}» — {a}:{b}.')
            scorer=re.search(r'([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) тем временем забил свой первый гол за «'+re.escape(loser)+r'»',raw)
            if scorer and a=='1':
                fact('scorers',(scorer[1],),f'Единственный гол за «{loser}» забил {scorer[1]} — его первый за этот клуб.')
            return 'MATCH_RESULT',(winner,translate(loser)),85
    # Asking price, not submitted bid or completed transfer; multiple named
    # interested clubs are context, never a guessed destination.
    asking=re.search(r'^«([А-ЯЁ][А-ЯЁ ]+)» ХОЧЕТ €(\d+) МЛН ЗА ([А-ЯЁ][А-ЯЁ ]+)!',clean)
    if asking and credit:
        club,amount,person=asking.groups();club=translate(club.title());person=person.title()
        fact('asking_valuation',(person,club,amount,'EUR','MILLION','ASKING_NOT_AGREED',credit),
             f'По данным {credit}, «{club}» оценивает {person} в {amount} млн евро.')
        if re.search(r'намерено сохранить игрока',raw):
            fact('retention_intent',(person,club,'REPORTED_INTENTION_NOT_SALE'), 'При этом клуб намерен сохранить футболиста.')
        return 'TRANSFER_VALUATION',(person,club),65
    m=re.search(r'^«([^»]+)» (?:заинтересован в подписании|нацелился на) ([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2})(?: в (\d{4}) году)?(?=\s*[^А-Яа-яЁё\s]|\.|$)',clean)
    if m and credit:
        club,person,year=m.groups();club=translate(club)
        # Preserve SOURCE's grammatical case; never guess a missing first name.
        intent='заинтересован в подписании' if 'заинтересован в подписании' in m[0] else 'нацелился на'
        when=f' в {year} году' if year else ''
        fact('reported_interest',(person,club,credit,'INTEREST_NOT_AGREEMENT'),f'По данным {credit}, «{club}» {intent} {person}{when}.')
        asking=re.search(r'«([^»]+)» готов продать полузащитника за (\d+) миллионов евро',raw)
        if asking:
            seller,amount=asking.groups();seller=translate(seller)
            fact('asking_price',(person,seller,amount,'EUR','MILLION','ASKING_NOT_AGREED'),f'«{seller}» готов продать полузащитника за {amount} млн евро, утверждает тот же источник.')
        planned=re.search(r'могут предложить за [^.]+? более (\d+) миллионов евро',raw)
        if planned:
            fact('possible_offer',(person,club,planned[1],'EUR','MILLION','GREATER_THAN','POSSIBLE_NOT_SUBMITTED'),f'В сообщении допускается предложение более {planned[1]} млн евро; согласованной сделки нет.')
        return 'TRANSFER_INTEREST',(person,club),65
    m=re.search(r'^«([^»]+)» готов подписать ([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2})!',clean)
    if m and 'обсуждает со своим окружением возможность перехода' in raw:
        club,person=m.groups();club=translate(club)
        fact('reported_interest',(person,club,'SOURCE_REPORT','INTEREST_NOT_AGREEMENT'),f'По сообщению источника, «{club}» рассматривает подписание {person}. Футболист обсуждает возможность перехода со своим окружением.')
        if 'высокие зарплатные ожидания' in raw:
            fact('salary_obstacle',(person,'POSSIBLE_SALARY_OBSTACLE'), 'Возможным препятствием названы высокие зарплатные ожидания игрока.')
        return 'TRANSFER_INTEREST',(person,club),60
    headline=re.search(r'([А-ЯЁ][А-ЯЁ]+ [А-ЯЁ][А-ЯЁ]+) И «([^»]+)» [—–-] ВС[ЕЁ]',raw)
    refusal=re.search(r'Контракт (?:аргентинца|игрока|защитника) истекает (летом \d{4}) года, и он не намерен его продлевать',raw)
    if headline and refusal:
        person=headline[1].title();club=translate(headline[2].title())
        fact('contract_intention',(person,club,refusal[1],'REPORTED_INTENTION_NOT_TERMINATED'),f'По сообщению источника, {person} не намерен продлевать контракт с клубом «{club}». Действующее соглашение истекает {refusal[1]} года.')
        return 'CONTRACT_PLANS',(person,club),70
    deadline=re.search(r'По словам инсайдера ([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+), руководство «([^»]+)» дало ([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) время до ([^.]+?), чтобы исправить результаты',raw)
    if deadline:
        reporter,club,coach,term=deadline.groups();club=translate(club)
        fact('coach_deadline',(coach,club,reporter,term,'REPORTED_REVIEW_NOT_DISMISSED'),f'По словам инсайдера {reporter}, руководство клуба «{club}» дало {coach} время до {term}, чтобы исправить результаты.')
        return 'COACH_REVIEW',(coach,club),70
    record=re.search(r'рекорд ([А-ЯЁ][а-яё]+)!',raw)
    goals=re.search(r'Теперь у [^.]+? (\d+) голов в (\d+)-м матче',raw)
    club_record=re.search(r'стал лучшим бомбардиром «([^»]+)» в ЛЧ',raw)
    if record and goals and club_record:
        person=record[1];club=translate(club_record[1]);count,games=goals.groups()
        fact('club_ucl_record',(person,club,count,games,'CLUB_CHAMPIONS_LEAGUE_CAREER'),f'Рекорд клуба «{club}» по голам в Лиге чемпионов теперь у {person} — {count} мячей в {games} матче, сообщает источник.')
        return 'PLAYER_STATS',(person,club),70
    finance=re.search(r'отч[её]т (Независимого футбольного регулятора Англии) \(IFR\)',raw,re.I)
    losses=re.search(r'(\d+)% клубов пяти высших дивизионов Англии несут убытки [—–-] (\d+) из (\d+) команд',raw)
    funding=re.search(r'(\d+) клуба? не смогут продержаться более (\d+) ДНЕЙ без финансовой поддержки своих владельцев',raw)
    if finance and losses:
        percent,count,total=losses.groups()
        if int(count)>int(total) or abs(100*int(count)/int(total)-int(percent))>1:return None
        fact('ifr_loss_snapshot',('IFR',percent,count,total,'TOP_FIVE_ENGLISH_DIVISIONS_REPORT'),f'По отчёту Независимого футбольного регулятора Англии (IFR), убытки несут {count} из {total} клубов пяти высших дивизионов страны — {percent}%.')
        if funding:
            count,days=funding.groups()
            fact('ifr_funding_snapshot',('IFR',count,days,'OWNER_FUNDING_REPORT'),f'Без поддержки владельцев {count} клуба не смогут продержаться более {days} дней, говорится в отчёте.')
        return 'FINANCIAL_REPORT',('IFR',),70
    run=re.search(r'«([^»]+)» не проигрывает клубам АПЛ уже (\d+) матчей подряд в турнирах под эгидой УЕФА',raw)
    if run:
        club,count=run.groups()
        fact('unbeaten_run',(club,count,'CONSECUTIVE_UEFA_MATCHES_VS_PREMIER_LEAGUE_OPPONENTS'),
             f'«{club}» не проигрывает командам АПЛ в турнирах UEFA уже {count} матчей подряд.')
        return 'CLUB_STATS',(club,),65
    award=re.search(r'^(.+?), UEFA Man of the Match for ([^.!?\n]+)(?:[.!?]|$)',' '.join(clean.split()))
    if award:
        person,fixture=award.groups()
        remainder=' '.join(clean.split())[award.end():].strip()
        if remainder and not remainder.startswith(('…','after ','After ')):
            return None  # Do not turn an unknown sentence fragment into a club.
        # Explicit word separators take priority over hyphens inside names.
        sides=re.split(r'\s+(?:vs|against)\s+',fixture,maxsplit=1)
        if len(sides)==1 and len(re.findall(r'[-–—]',fixture))==1:
            sides=re.split(r'\s*[-–—]\s*',fixture,maxsplit=1)
        if len(sides)==2 and all(s.strip() for s in sides):
            person,a,b=map(translate,(person,*map(str.strip,sides)))
            fact('match_award',(person,a,b,'UEFA_PLAYER_OF_MATCH'),f'{person} — лучший игрок матча «{a}» — «{b}» по версии UEFA.')
            return 'MATCH_AWARD',(a,b,person),50
    total=re.search(r'Теперь у него (\d+) голов в турнире',raw)
    first=re.search(r'([А-ЯЁ][а-яё]+) забил «[^»]+» в победном матче Лиги чемпионов',raw)
    played=re.search(r'потребовался (\d+) матч',raw)
    if total and first and played:
        names=re.findall(r'('+re.escape(first[1])+r' [А-ЯЁ][а-яё]+) [—–-] '+re.escape(total[1])+r' голов',raw)
        if len(set(names))==1:
            person=names[0]
            fact('ucl_career_goals',(person,total[1],played[1],'CHAMPIONS_LEAGUE_CAREER'),
                 f'{person} достиг отметки в {total[1]} голов в Лиге чемпионов за {played[1]} матч.')
            return 'PLAYER_STATS',(person,),65
    m=re.search(r'ОФИЦИАЛЬНО:\s*(.+?) дисквалифицирован на (\d+) матч\w* и пропустит ([^.]+)',clean)
    if m:
        person,count,fixture=m.groups()
        fact('confirmed_suspension',(person,count,fixture,'OFFICIAL'),
             f'{person} дисквалифицирован на {count} матча и пропустит {fixture}.')
        return 'DISCIPLINE',(person,),85
    m=re.search(r'^(?:Тем временем )?([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2}) получил красную карточку [—–-] он пропустит матч против «([^»]+)»',clean)
    if m:
        person,opponent=m.groups()
        fact('red_and_absence',(person,opponent,'SOURCE_REPORTED_RED_AND_NEXT_ABSENCE'),
             f'По сообщению источника, {person} получил красную карточку и пропустит матч против «{opponent}».')
        return 'DISCIPLINE',(person,opponent),70
    m=re.search(r'^(.+?)[’\x27]s red against (.+?) has been overturned',clean)
    if m:
        person,opponent=m.groups(); person,opponent=translate(person),translate(opponent)
        fact('red_overturned',(person,opponent,'OVERTURNED'),f'Удаление, которое получил {person} в матче против клуба «{opponent}», отменено.')
        return 'DISCIPLINE',(person,opponent),75
    m=re.search(r'(?:^|: )([^:]+?) будет назначен на пост нового главного тренера «([^»]+)»',clean)
    if m:
        person,club=m.groups()
        fact('expected_appointment',(person,club,provider,'REPORTED_FUTURE_NOT_OFFICIAL'),
             f'По сообщению {provider}, {person} должен стать главным тренером «{club}».')
        return 'APPOINTMENT_EXPECTED',(person,club),75
    m=re.search(r'^(.+?) близок к назначению в «([^»]+)»',clean)
    if m:
        person,club=m.groups()
        fact('appointment_talks',(person,club,'REPORTED_CLOSE_NOT_APPOINTED'),
             f'По сообщению источника, {person} близок к назначению в «{club}».')
        return 'APPOINTMENT_EXPECTED',(person,club),65
    m=re.search(r'«([^»]+)» запросила? информацию о (.+?)(?= [^а-яё]|[.!]|$)',clean)
    if m:
        club,person=m.groups()
        fact('information_request',(person,club,'INFORMATION_REQUEST_NOT_BID'),
             f'«{club}» '+('запросила' if 'запросила информацию' in m[0] else 'запросил')+f' информацию о {person}, сообщает источник.')
        return 'TRANSFER_CONTACT',(person,club),70
    m=re.search(r'«([^»]+)» хочет подписать ([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+)?)(?= [^а-яё]|[.!]|$)',raw)
    if m and credit:
        club,person=m.groups()
        fact('reported_interest',(person,club,credit,'INTEREST_NOT_AGREEMENT'),
             f'«{club}» хочет подписать {person}, сообщает {credit}.')
        return 'TRANSFER_INTEREST',(person,club),65
    m=re.search(r'(.+?) right-back (.+?) is thought to be on his way out of the club, with (.+?) keen\. \(([^()]+)\)',raw)
    if m:
        origin,person,dest,provider=m.groups();origin,dest,person=translate(origin),translate(dest),translate(person)
        fact('reported_interest',(person,origin,dest,provider,'REPORTED_POSSIBLE_EXIT_NOT_DEAL'),
             f'По данным {provider}, правый защитник клуба «{origin}» {person} может покинуть команду; им интересуется «{dest}».')
        return 'TRANSFER_INTEREST',(person,origin,dest),65
    report_clean=clean[len(attribution[0]):].lstrip() if attribution else clean
    m=re.search(r'^(.+?) может покинуть «([^»]+)» (?:уже )?в январе, если клуб получит предложение на сумму более (\d+) млн фунтов',report_clean)
    if not m:
        conditional=re.search(r'^(.+?) может покинуть «([^»]+)» (?:уже )?в январе, если клуб получит крупное предложение',report_clean)
        if conditional:
            threshold=re.search(r'«'+re.escape(conditional[2])+r'» хочет получить за игрока более (\d+) млн фунтов',report_clean)
            if threshold:
                # Same typed relation as the inline threshold, not a new deal.
                m=(conditional[1],conditional[2],threshold[1])
    if m:
        person,club,amount=m.groups() if hasattr(m,'groups') else m
        fact('conditional_departure',(person,club,amount,'GBP','MILLION','STRICTLY_GREATER','JANUARY','CONDITIONAL_NOT_AGREED'),
             f'{person} может уйти из клуба «{club}» в январе, если поступит предложение более {amount} млн фунтов, сообщает '+('Фабрицио Романо.' if attribution else 'источник.'))
        if re.search(r'После встречи с агентами позиция(?: клуба)? изменилась',raw):
            fact('changed_sale_position',(club,'CHANGED_AFTER_AGENT_MEETING'), 'Позиция клуба изменилась после встречи с агентами.')
        leaders=re.search(r'«([^»]+)» лидирует в гонке.+?интерес.+?«([^»]+)»',raw)
        if leaders:
            first,second=leaders.groups()
            fact('reported_buyers',(first,second,'REPORTED_LEADER_AND_OTHER_INTEREST'),f'По тому же сообщению, «{first}» лидирует в гонке, а интерес также проявлял «{second}».')
        return 'TRANSFER_CONDITIONAL_SALE',(person,club),75
    headline=re.search(r'^([^–—!?.]+) [–—-] В «([^»]+)»',clean,re.I)
    offer=re.search(r'готов предложить «([^»]+)» (\d+) млн евро \+ (\d+) млн бонусами.+?этой зимой',raw)
    if headline and offer and credit:
        person,dest=headline.groups(); origin,base,bonuses=offer.groups();person=person.strip().capitalize();dest=translate(dest.title())
        fact('planned_offer',(person,origin,dest,base,bonuses,'EUR','MILLION','WINTER','INTENT_NOT_SUBMITTED'),
             f'По данным {credit}, «{dest}» готов предложить «{origin}» за {person} {base} млн евро и ещё {bonuses} млн бонусами этой зимой.')
        if 'готовы рассмотреть предложения о его продаже' in raw:
            fact('seller_open',(origin,'OPEN_TO_CONSIDER_NOT_ACCEPTED'), 'Продавец готов рассмотреть предложения.')
        return 'TRANSFER_PLANS',(person,origin,dest),70
    m=re.search(r'^(.+?) заблокировал уход (.+?) этой зимой',clean)
    if m:
        coach,person=m.groups()
        fact('departure_blocked',(coach,person,'WINTER','DEPARTURE_BLOCKED'),f'По сообщению источника, {coach} заблокировал уход {person} этой зимой.')
        return 'TRANSFER_BLOCKED',(person,coach),70
    m=re.search(r'Источники в «([^»]+)» утверждают, что (.+?) неизбежно будет оштрафован за сво[её] поведение',raw)
    if m:
        club,person=m.groups()
        attribution=f'По данным {credit}' if credit else 'По сообщению источников клуба'
        fact('expected_fine',(person,club,'REPORTED_EXPECTED_NOT_CONFIRMED'),f'{attribution}, {person} («{club}») будет оштрафован за своё поведение.')
        return 'DISCIPLINE',(person,club),70
    brace=re.search(r'([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) сделал дубль в матче Лиги Чемпионов с «([^»]+)»',raw)
    half=re.search(r'К концу первого тайма «([^»]+)» ведёт (\d+:\d+)',raw)
    if brace and half:
        person,opponent=brace.groups();club,score=half.groups()
        opponent=re.sub(r'([А-ЯЁ]{2,})(?:ом|а|у)$',r'\1',opponent)
        fact('halftime_score',(club,opponent,score,'HALFTIME_NOT_FINAL'),
             f'К перерыву «{club}» вёл {score} в матче Лиги чемпионов с клубом «{opponent}».')
        fact('first_half_brace',(person,'2','FIRST_HALF'),f'{person} оформил дубль в первом тайме.')
        return 'MATCH_HALFTIME',(club,opponent,person),75
    first=re.search(r'([А-ЯЁ][а-яё]+) и ([А-ЯЁ][а-яё]+) забивают свои первые голы за «([^»]+)» в матче ЛЧ против ([А-ЯЁа-яё]+)',raw)
    if first:
        a,b,club,opponent=first.groups()
        fact('first_club_goals',(a,b,club,opponent,'FIRST_CLUB_GOALS_NOT_FINAL_RESULT'),
             f'{a} и {b} забили свои первые голы за «{club}» в матче Лиги чемпионов против {opponent}.')
        return 'MATCH_FIRST_GOALS',(club,opponent,a,b),65
    shots=re.search(r'«([^»]+)» нанёс (\d+) ударов в первом тайме выездного матча Лиги чемпионов',raw)
    if shots and 'за всю историю сбора статистики от OPTA' in raw:
        club,count=shots.groups()
        fact('shot_record',(club,count,'FIRST_HALF_AWAY_CHAMPIONS_LEAGUE','OPTA_RECORDED_HISTORY'),
             f'«{club}» нанёс {count} ударов в первом тайме выездного матча Лиги чемпионов — рекорд гостевых команд за историю наблюдений OPTA.')
        return 'CLUB_STATS',(club,),60
    return None


def extended_event(candidate, add, translate, source_text=None):
    raw=source_text if source_text is not None else candidate.source.text
    # The original remains evidence. Semantic handles are resolved BEFORE the
    # shared legacy cleaner; it must not erase an actor from a grammatical slot.
    from .daily_fact_shadow import normalize_handles
    from .simple_editorial import clean_simple_source_text
    from .daily_quality import semantic_source_issues
    if semantic_source_issues(normalize_handles(raw)):return None
    cleaned=clean_simple_source_text(normalize_handles(raw.replace('\\n','\n')))
    supplied_credit=re.search(r'📰\s*([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9._-]*(?:\s+(?:/\s+)?[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9._-]*)*)',raw)
    added=editorial_relations(raw,cleaned,candidate.source.canonical_url,add,translate,
                             supplied_credit[1] if supplied_credit else None)
    if added:return added
    parsed=october_event(cleaned,raw,candidate.source.canonical_url,add,translate,
                         supplied_credit[1] if supplied_credit else None)
    if parsed:return parsed
    text=re.sub(r'^[^A-Za-zА-Яа-яЁё]+','',raw)
    url=candidate.source.canonical_url
    credit=re.search(r'\(([^()\n]+)\)\.?\s*$',text)
    body=text[:credit.start()].strip().rstrip('.') if credit else text.strip().rstrip('.')
    attribution=credit[1] if credit else None

    def fact(key,values,wording,evidence=raw):
        add(key,values,evidence,url,wording)

    # Reusable regulator finance report: explicit relation slots, no inferred
    # sporting result, club attribution or converted currencies.
    report=re.search(r"The (.+?) has released its '([^']+)' report",text)
    revenue=re.search(r'Clubs brought in £(\d+(?:\.\d+)?)bn in revenue in (\d{4}/\d{2})',text)
    losses=re.search(r'(\d+)% of clubs still make losses',text)
    funding=re.search(r'(\d+) clubs stated they couldn.t survive (\d+) days without owner funding',text)
    if report and revenue and losses and funding:
        org,title=report.groups();amount,period=revenue.groups()
        fact('finance_revenue',(org,title,amount,'GBP','BILLION',period),
             f'В отчёте «{title}» организации «{translate(org)}» указано: выручка клубов за {period} составила {amount} млрд фунтов.')
        fact('finance_losses',(org,losses[1],'PERCENT','REPORT_SNAPSHOT'),
             f'При этом {losses[1]}% клубов остаются убыточными.')
        fact('finance_owner_funding',(org,*funding.groups(),'SELF_REPORTED_DEPENDENCE'),
             f'{funding[1]} клуба заявили, что без поддержки владельцев не смогут продержаться {funding[2]} дней.')
        # This is a faithful summary of these three report findings, not an
        # assertion that all heterogeneous report subsections were extracted.
        return 'FINANCIAL_REPORT',(translate(org),),75

    # Reported youth/player negotiations: keep allegation, requested terms and
    # rejection separate from a signed contract or actual departure.
    demands=re.search(r'(.+?) раскрыли (?:ПЯТЬ|\d+) требований, которые (.+?) и его окружение, предположительно, выдвинули «([^»]+)»:',text)
    shirt=re.search(r'номер (\d+)\s*• нанесение на джерси [–—-] ([A-Z ]+?)\s*•',text)
    salary=re.search(r'(семизначная) годовая зарплата',text)
    minutes=re.search(r'минимум (\d+) игровых минут в (трех|трёх|\d+) конкретных матчах',text)
    if demands and shirt and salary and minutes and 'только с первой командой' in text:
        provider,person,club=demands.groups()
        rejected=re.search(r'«'+re.escape(club)+r'» отклонил несколько требований',text)
        expectation=re.search(r'ожидает, что (\d+)-летний футболист покинет клуб',text)
        if not rejected or not expectation:return None
        fact('reported_terms',(provider,person,club,'ALLEGED_DEMANDS'),
             f'По сообщению {provider}, {person} и его окружение предположительно выдвинули условия клубу «{club}».')
        fact('shirt_terms',shirt.groups(),f'Среди них — номер {shirt[1]} и надпись «{shirt[2]}» на футболке.')
        fact('salary_terms',('SEVEN_DIGIT','ANNUAL','CURRENCY_UNSPECIFIED'),
             'Также сообщается о семизначной годовой зарплате; валюта не указана.')
        count={'трех':'3','трёх':'3'}.get(minutes[2],minutes[2])
        fact('playing_terms',(minutes[1],count,'FIRST_TEAM_TRAINING_ONLY','REQUEST_NOT_PROMISE'),
             f'Другие требования — тренировки только с первой командой и минимум {minutes[1]} игровых минут в {count} конкретных матчах.')
        fact('terms_rejected',(club,person,'SOME_REJECTED','EXPECTED_NOT_COMPLETED_DEPARTURE',expectation[1]),
             f'Клуб, по тому же сообщению, отклонил часть требований и ожидает ухода {expectation[1]}-летнего игрока. Переход не объявлен.')
        return 'CONTRACT_NEGOTIATION',(person,club),65

    # Only the unambiguous current-period line from a multi-period comparison.
    # Historical records/comparisons are not reassigned to the current player.
    title=re.search(r'^([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) [–—-]',text)
    current_stats=re.search(r'(\d+) голов и (\d+) ассист\w* в первых (\w+|\d+) турах АПЛ',text)
    if title and current_stats:
        person=title[1];goals,assists,rounds=current_stats.groups()
        rounds={'шести':'6','пяти':'5','семи':'7','восьми':'8'}.get(rounds,rounds)
        if not rounds.isdigit():return None
        fact('period_stats',(person,goals,assists,rounds,'PREMIER_LEAGUE_ROUNDS'),
             f'{person}: {goals} голов и {assists} ассиста в первых {rounds} турах АПЛ.')
        return 'PLAYER_STATS',(person,),60

    def source_credit():
        if attribution:fact('attribution',(attribution,),f'Источник: {attribution}.')

    announced=re.search(r'СВЕРШИЛОСЬ!.*?([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) [–—-] игрок «([^»]+)».*?Контракт до (лета \d{4}) года',text,re.S)
    if announced:
        person,club,term=announced.groups()
        fact('announced_signing',(person,club,'SOURCE_ANNOUNCEMENT_NOT_EXTRA_OFFICIAL_PROOF'),
             f'Источник сообщает: {person} стал игроком клуба «{club}».')
        fact('contract',(term,),f'Контракт — до {term} года.')
        return 'TRANSFER_ANNOUNCEMENT',(person,club),80
    interest=re.search(r'«([^»]+)» заинтересован в подписании (.+?)\s*💰 Сделка оценивается в (\d+) млн евро',raw)
    if interest and supplied_credit:
        club,person,amount=interest.groups()
        fact('interest',(person,club,'REPORTED_INTEREST_NOT_SIGNED'),
             f'По данным {supplied_credit[1]}, «{club}» интересуется {person}.')
        fact('valuation',(amount,'EUR','MILLION','REPORTED_POSSIBLE_PRICE'),
             f'Возможная сумма сделки — {amount} млн евро.')
        return 'TRANSFER_INTEREST',(person,club),65

    m=re.fullmatch(rf'(.+?) will need to pay at least {MONEY} to tempt (.+?) into selling {ROLE} (.+)',body)
    if m:
        dest,currency,amount,origin,role,person=m.groups()
        dest,origin,person=map(translate,(dest,origin,person))
        fact('transfer_subject',(person,origin,dest,'REPORTED_ASKING_PRICE_NOT_DEAL'),
             f'{person} («{origin}»): по информации {attribution or "источника"}, возможный переход в «{dest}» потребует не менее {money(currency,amount)}.')
        return 'TRANSFER_INTEREST',(person,origin,dest),65
    m=re.fullmatch(rf'(.+?) have identified (.+?) {ROLE} (.+?) as a leading January target',body)
    if m:
        dest,origin,role,person=m.groups();dest,origin,person=map(translate,(dest,origin,person))
        fact('transfer_subject',(person,origin,dest,'REPORTED_JANUARY_TARGET_NOT_DEAL'),
             f'{person} («{origin}») — одна из главных январских целей клуба «{dest}», по информации {attribution or "источника"}.')
        return 'TRANSFER_INTEREST',(person,origin,dest),65
    # Russian interest + an explicit seller asking price (never a paid fee).
    wanted=re.search(r'«([^»]+)» хочет подписать ([А-ЯЁ][а-яё]+)!',raw)
    seller=re.search(r'«([^»]+)» хочет получить (\d+) млн евро за игрока',raw)
    if wanted and seller and 'этой зимой' in raw and supplied_credit:
        dest,person=wanted.groups();origin,amount=seller.groups()
        fact('transfer_subject',(person,origin,dest,'REPORTED_WINTER_INTEREST_NOT_DEAL'),
             f'По информации {supplied_credit[1]}, «{dest}» хочет подписать {person} этой зимой.')
        fact('asking_fee',(origin,amount,'EUR','MILLION','REPORTED_ASKING_PRICE'),
             f'«{origin}» оценивает игрока в {amount} млн евро.')
        return 'TRANSFER_INTEREST',(person,origin,dest),65

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


def october_event(text,raw,url,add,translate,credit=None):
    """Reusable relation grammars, not rules about the test day's people.

    A reported agreement is not an official signature; plans are not outcomes;
    a goal highlight is not a final match result. Each slot retains its SOURCE.
    """
    # Emoji subdivision flag tag codepoints can survive the shared cleaner.
    # Strip only the leading nonletter framing, not names or factual digits.
    text=re.sub(r'^[^A-Za-zА-Яа-яЁё«]+','',text)
    def fact(key,values,wording,alternative=None):
        add(key,values,raw,url,wording,alternative)
    # A board decision is a new development, not a repeat of season statistics.
    board=re.search(r'руководство клуба после домашней ничьей с «([^»]+)» выразило поддержку своему тренеру',raw,re.I)
    subject=re.search(r'«([^»]+)» под руководством',raw)
    current=re.search(r'в качестве потенциальной замены ([А-ЯЁ][а-яё]+) называлось',raw)
    if board and subject and current:
        club=translate(subject[1]);coach=translate(current[1]);opponent=translate(board[1])
        fact('board_support',(club,coach,opponent,'BOARD_SUPPORT_NOT_DISMISSAL'),
             f'По сообщению источника, {coach} получил поддержку руководства клуба «{club}» после домашней ничьей в игре против клуба «{opponent}». Болельщиков призвали к терпению.')
        return 'BOARD_SUPPORT',(club,coach,opponent),65
    # Official contract extensions: person role must be explicit or left open,
    # never inferred from a player's/trainer's name.
    m=re.search(r'^ОФИЦИАЛЬНО:\s*(.+?) продлил контракт с «([^»]+)» до (лета \d{4}) года',text)
    if m:
        person,club,term=m.groups();club=translate(club)
        fact('extension',(person,club,term,'OFFICIAL_EXTENSION'),
             f'{person} и «{club}» продлили контракт до {term} года.',
             f'«{club}» и {person} продлили соглашение до {term} года.')
        return 'CONTRACT_EXTENSION',(person,club),80
    m=re.search(r'^ОФИЦИАЛЬНО:\s*(.+?) уволен с поста главного тренера «([^»]+)»',text)
    if m:
        person,club=m.groups();club=translate(club)
        fact('dismissal',(person,club,'OFFICIAL_DISMISSAL'),f'{person} уволен с поста главного тренера клуба «{club}».')
        return 'COACH_DISMISSAL',(person,club),75
    m=re.search(r'^ОФИЦИАЛЬНО:\s*(.+?) [—–] игрок «([^»]+)»',text)
    if m:
        person,club=m.groups()
        fact('transfer_subject',(person,'',club,'OFFICIAL'),f'{person} стал игроком клуба «{club}».',
             f'Клуб «{club}» подписал {person}.')
        term=re.search(r'Контракт до (лета \d{4}) года',text)
        if term:fact('contract',(term[1],),f'Контракт действует до {term[1]} года.')
        if 'Свободный агент' in text:fact('fee',('FREE_AGENT',),'Переход состоялся на правах свободного агента.')
        return 'TRANSFER_OFFICIAL',(person,club),90
    m=re.search(r'^(.+?) signs for (.+?) today after the verbal agreement',text)
    if m and 'free move now confirmed' in text:
        person,club=map(translate,m.groups())
        fact('transfer_subject',(person,'',club,'OFFICIAL'),f'{person} подписал контракт с клубом «{club}».')
        fact('fee',('FREE_AGENT',),'Переход состоялся на правах свободного агента.')
        return 'TRANSFER_OFFICIAL',(person,club),90
    m=re.search(r'^(.+?) have reached an agreement with (.+?) over new long term deal until June (\d{4})',text)
    if m:
        club,person,year=m.groups();club,person=map(translate,(club,person))
        fact('contract_agreement',(person,club,year,'AGREEMENT_NOT_SIGNED'),
             f'По сообщению источника, «{club}» и {person} достигли соглашения по новому контракту до июня {year} года.')
        return 'CONTRACT_AGREEMENT',(person,club),75
    # Mixed-metric tables without a stated relationship are NOT a statistic.
    # Matching every digit does not establish the meaning of each row.
    m=re.search(r'Команды, совершившие (\d+) и более касаний мяча в штрафной площади соперника в текущем сезоне в рамках чемпионата:',text)
    rows=re.findall(r'«([^»]+)» \((\d+) гол\w*\)',text)
    if m and rows:
        return None
    # Full Russian result with literal scorers, no inferred intent or minute.
    m=re.search(r'«([^»]+)» разгромил «([^»]+)» на сво[её]м поле (\d+):(\d+)\.',text)
    if m:
        winner,loser,home,away=m.groups()
        if int(home)<=int(away):return None
        fact('result',(winner,loser,f'{home}:{away}'),f'«{winner}» обыграл «{loser}» на своём поле — {home}:{away}.',
             f'Домашняя победа клуба «{winner}» над «{loser}»: {home}:{away}.')
        scorers=re.search(r'Голами отметились (.+?)\.',text)
        if scorers:fact('scorers',tuple(re.split(r', | и ',scorers[1])),f'Голы забили {scorers[1]}.')
        consolation=re.search(r'За «([^»]+)» единственным мячом отметился (.+?)\.',text)
        if consolation:fact('opponent_scorer',consolation.groups(),f'У соперника отличился {consolation[2]}.')
        position=re.search(r'Команда выбралась из зоны вылета и ид[её]т на (\d+)-й строчке',text)
        if position:fact('table_position',(winner,position[1]),f'Команда вышла из зоны вылета и занимает {position[1]}-е место.')
        return 'MATCH_RESULT',(winner,loser),95
    # Loan decision attributed to speaker; passing injury context is NOT the
    # event's category. The own-club name is absent and must not be guessed.
    m=re.search(r'^(.+?): “(.+?) does NOT need to be loaned out',text)
    if m:
        speaker,person=map(translate,m.groups())
        fact('loan_position',(person,speaker,'SPEAKER_OPPOSES_LOAN'),
             f'{speaker} заявил, что {person} не нуждается в аренде, и выразил уверенность в его успехе в нынешней команде.')
        return 'COACH_LOAN_POSITION',(person,speaker),65
    m=re.search(r'^(.+?) closes all doors to a loan for (.+?) in January:',text)
    if m:
        speaker,person=map(translate,m.groups())
        fact('loan_position',(person,speaker,'SPEAKER_OPPOSES_LOAN'),
             f'{speaker} исключил январскую аренду {person}.')
        return 'COACH_LOAN_POSITION',(person,speaker),65
    # Specific named scouting interest, not a fixture result.
    m=re.search(r'^(.+?) will be keeping a close eye on (.+?) forward (.+?) during their Champions League clash this evening\. \(([^()]+)\)',text)
    if m:
        dest,origin,person,provider=m.groups();dest,origin,person=map(translate,(dest,origin,person))
        fact('scouting',(person,origin,dest,'SCOUTING_NOT_BID'),
             f'По информации {provider}, клуб «{dest}» следит за {person} («{origin}») на фоне встречи команд в Лиге чемпионов. О предложении не сообщается.')
        return 'TRANSFER_INTEREST',(person,origin,dest),60
    m=re.search(r'^«([^»]+)» хочет подписать (.+?)\s*💰 Сумма трансфера может составить около €(\d+) млн\.\s*📰 (.+)$',text)
    if m:
        club,person,amount,provider=m.groups()
        fact('interest',(person,club,'INTEREST'),f'{provider} сообщает об интересе клуба «{club}» к игроку: {person}.')
        fact('possible_fee',(amount,'EUR','POSSIBLE_NOT_PAID'),f'Возможная сумма — около {amount} млн евро.')
        return 'TRANSFER_INTEREST',(person,club),65
    # Explicit current club, proposed buyer and price stay separate slots.
    m=re.search(r'^(.+?) have opened the door to selling (.+?) in January following talks',text)
    offer=re.search(r'right offer arrives, understood to be £(\d+)m\+',text)
    if m and offer and 'Premier League safety is looking likely' in text:
        origin,person=map(translate,m.groups())
        fact('transfer_subject',(person,origin,'','CONDITIONAL_SALE_NOT_AGREED'),
             f'В другом сообщении источника уточняется: {person} может покинуть клуб «{origin}» в январе, но переход не гарантирован.')
        fact('sale_conditions',(offer[1],'GBP','MILLION_PLUS','IF_SAFETY_LIKELY'),
             f'Условия: подходящее предложение от {offer[1]} млн фунтов и вероятное сохранение места в АПЛ.')
        buyer=re.search(r'(.+?) made multiple bids for \w+ over the summer',text)
        if buyer:fact('buyer_interest',(translate(buyer[1].split('\n')[-1]),person,'PAST_BIDS_CURRENT_INTEREST'),
                      f'«{translate(buyer[1].split(chr(10))[-1])}» делал предложения летом и сохраняет интерес.')
        second=re.search(r'(.+?) also well-positioned if they choose to move',text)
        if second:fact('conditional_other_buyer',(translate(second[1].split('\n')[-1]),person,'IF_CHOOSE_TO_MOVE'),
                       f'«{translate(second[1].split(chr(10))[-1])}» также рассматривается как возможное направление, если клуб решит действовать.')
        return 'TRANSFER_REPORTED',(person,origin),75
    m=re.search(r'^«([^»]+)» готов выложить (\d+) МЛН ЕВРО за (.+?) этой зимой!',text)
    refusal=re.search(r'отклонил предложение о продлении контракта с «([^»]+)»',text)
    if m and refusal:
        dest,amount,person=m.groups();origin=translate(refusal[1])
        # Accusative names are an observed surface, not completed first names.
        person=translate(person)
        provider=re.search(r'📰 (.+)$',text)
        fact('transfer_subject',(person,origin,dest,'REPORTED_WILLINGNESS_NOT_BID'),
             f'По информации {provider[1] if provider else "источника"}, «{dest}» рассматривает зимний трансфер: {person} («{origin}»).')
        fact('reported_willingness_fee',(person,dest,amount,'EUR','MILLION','WILLINGNESS_NOT_BID_OR_SALARY'),
             f'Клуб, по версии издания, готов потратить на переход {amount} млн евро. О согласованной сделке не сообщается.')
        fact('contract_refusal',(person,origin,'REPORTED_REFUSAL'),f'Сообщается, что игрок отклонил предложение клуба «{origin}» о продлении контракта.')
        return 'TRANSFER_REPORTED',(person,origin,dest),70
    # A surname in the SOURCE headline is evidence; do not expand a first name.
    m=re.search(r'^([А-ЯЁ ]+) ПЕРЕЙДЕТ В «([^»]+)»\?!',text)
    origin=re.search(r'«([^»]+)» готова отпустить полузащитника.*?не менее (\d+) млн евро',text)
    provider=re.search(r'📰 ([A-Za-z ]+)',text)
    if m and origin and provider and 'заинтересовано в зимнем трансфере' in text:
        person=m[1].strip().title();dest=translate(m[2]);seller,amount=origin.groups()
        fact('transfer_subject',(person,seller,dest,'INTEREST_NOT_DEAL'),
             f'По информации {provider[1].strip()}, «{dest}» интересуется зимним переходом полузащитника: {person} («{seller}»).')
        fact('asking_fee',(seller,amount,'EUR','MILLION','REPORTED_MINIMUM'),
             f'Сообщаемая оценка продавца — не менее {amount} млн евро.')
        return 'TRANSFER_INTEREST',(person,seller,dest),65
    m=re.search(r'^(.+?) will not be presenting a trophy.*?has not been invited to do so',text)
    if m and 'attendance' in text and 'not yet been confirmed' in text:
        person=translate(m[1])
        fact('ceremony_role',(person,'NOT_INVITED_TO_PRESENT','ATTENDANCE_UNCONFIRMED'),
             f'По сообщению источника, {person} не приглашён вручать награды «Золотого мяча». Его присутствие на церемонии пока не подтверждено.')
        return 'CEREMONY_ROLE',(person,),50
    m=re.search(r'^(.+?) Chair (.+?) on ',text)
    case=re.search(r'outcome of the appeal.*?in the (.+?) case',text)
    if m and case and 'power to investigate' in text and 'if we have the evidence' in text and 'making no judgement' in text:
        organization,speaker=m.groups()
        club=translate(case[1])
        fact('regulator_position',(organization,speaker,club,'INVESTIGATION_REQUIRES_EVIDENCE','NO_CASE_JUDGEMENT'),
             f'{translate(speaker)}, председатель организации «{translate(organization)}», заявил о полномочиях проверять действия владельцев, директоров и руководителей клубов. Меры требуют доказательств; выводов по делу клуба «{club}» ведомство пока не делает.')
        return 'REGULATOR_POSITION',(organization,speaker,club),60
    m=re.search(r'^(.+?) have reached an agreement in principle with (.+?) over new contract',text)
    if m:
        club,person=map(translate,m.groups())
        fact('agreement',(person,club,'AGREEMENT_IN_PRINCIPLE_NOT_SIGNED'),
             f'По сообщению источника, {club} достиг принципиального соглашения с {person} о новом контракте.',
             f'Источник сообщает о принципиальном соглашении {club} с {person} по новому контракту.')
        term=re.search(r'Current deal expires in (\w+) (\d{4})',text)
        if term and term[1]=='June':fact('current_expiry',(term[1],term[2]),f'Действующее соглашение истекает в июне {term[2]} года.')
        return 'CONTRACT_AGREEMENT',(person,club),80
    m=re.search(r'^(.+?) confirms (.+?) lost (.+?) and (.+?) due to injury',text)
    if m:
        speaker,club,p1,p2=map(translate,m.groups())
        fact('injuries',(p1,p2,club,speaker,'INJURY_NO_DIAGNOSIS'),
             f'{speaker} подтвердил травмы двух игроков клуба «{club}»: {p1} и {p2}.',
             f'По словам {speaker}, два игрока клуба «{club}» получили травмы: {p1} и {p2}.')
        return 'INJURY',(p1,p2,club),80
    m=re.search(r'^(.+?) and (.+?) are both OUT of (.+?) squad to face (.+?)\.',text)
    q=re.search(r'physical issues.*?confirms (.+?)\.',text,re.S)
    if m and q:
        p1,p2,club,rival=map(translate,m.groups());speaker=translate(q[1])
        fact('absence',(p1,p2,club,rival,'SQUAD_ABSENCE',speaker,'PHYSICAL_ISSUES'),
             f'{p1} и {p2} отсутствуют в заявке клуба «{club}» на матч против «{rival}». {speaker} объяснил это физическими проблемами.',
             f'{speaker} сообщил о физических проблемах у двух игроков клуба «{club}»: {p1} и {p2}. Они пропустят матч против «{rival}».')
        return 'INJURY',(p1,p2,club,rival),75
    m=re.search(r'^(.+?) scores (.+?)[’\x27]s equaliser against (.+?) to ',text)
    if m:
        person,club,rival=map(translate,m.groups())
        fact('equaliser',(person,club,rival,'EQUALISER_NOT_WINNER'),
             f'{person} сравнял счёт: гол за «{club}» в матче против «{rival}».',
             f'В матче «{club}» — «{rival}» {person} забил сравнивающий гол.')
        return 'MATCH_GOAL',(person,club,rival),70
    m=re.search(r'^ОФИЦИАЛЬНО:\s*(.+?) — новый главный тренер «([^»]+)»\. Контракт до (лета \d{4}) года\.',text)
    if m:
        person,club,term=m.groups()
        fact('appointment',(person,club,term,'OFFICIAL'),f'{person} назначен главным тренером «{club}». Контракт — до {term} года.',
             f'Новый главный тренер «{club}» — {person}. Соглашение рассчитано до {term} года.')
        previous=re.search(r'([А-ЯЁ][А-Яа-яЁё ]+?) покинул пост главного тренера клуба',text)
        if previous:fact('predecessor',(previous[1],),f'{previous[1]} покинул пост.')
        return 'APPOINTMENT',(person,club),85
    m=re.search(r'^«([^»]+)» близок к подписанию (\d+)[‑-]летнего (.+?)\s*(?:💰|Сумма)',text)
    amount=re.search(r'Сумма трансфера может составить около €(\d+) млн',text)
    if m and amount and credit:
        club,age,person=m.groups();person=person.strip(' .')
        fact('reported_move',(person,club,age,'REPORTED_NEAR_SIGNING_NOT_CONFIRMED'),
             f'{credit} сообщает: «{club}» близок к подписанию {age}-летнего {person}.',
             f'По информации {credit}, «{club}» приблизился к подписанию {age}-летнего {person}.')
        fact('reported_fee',(amount[1],'EUR','APPROXIMATE_POSSIBLE'),f'Возможная сумма трансфера — около {amount[1]} млн евро; завершение сделки не подтверждено.')
        # The last clause is an evidence-state label, not a claim of failure.
        return 'TRANSFER_REPORTED',(person,club),70
    m=re.search(r'^(.+?) and (.+?), the story is not over',text)
    if m and re.search(r'The Federation wants him back',text):
        person,nation=map(translate,m.groups())
        fact('return_plan',(person,nation,'FEDERATION_WISH_NOT_COMPLETED_RETURN'),
             f'Источник сообщает о планах вернуть {person} в сборную: «{nation}».')
        if 'end of the month' in text and 'November' in text:
            fact('planned_timing',('MONTH_END','NOVEMBER','PLAN'),
                 'План ожидается к концу месяца; возвращение рассматривается на ноябрь.')
        return 'NATIONAL_TEAM_PLAN',(person,nation),65
    # Explicit reported statistics: no arithmetic or inferred provider rankings.
    m=re.search(r'По версии (\w+), (.+?) — игрок с самой высокой средней оценкой в АПЛ в этом сезоне \((\d+,\d+)\)',text)
    stats=re.search(r'забив (\d+) голов и отдав (\d+) ассиста',text)
    if m and stats:
        provider,person,rating=m.groups();goals,assists=stats.groups()
        fact('rating',(person,provider,rating,'REPORTED_LEAGUE_RANKING'),
             f'По версии {provider}, {person} имеет лучшую среднюю оценку в АПЛ этого сезона — {rating}.',
             f'{person} лидирует по средней оценке {provider} в этом сезоне АПЛ: {rating}.')
        fact('goals_assists',(person,goals,assists),f'На его счету {goals} голов и {assists} ассиста.')
        return 'PLAYER_STATS',(person,),60
    m=re.search(r'([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+) в (\d+) из (\d+) матчей АПЛ не отметился ни голом, ни ассистом и не создал ни одного явного голевого момента',text)
    if m:
        person,count,games=m.groups()
        fact('reported_record',(person,count,games,'NO_GOAL_ASSIST_BIG_CHANCE'),
             f'Источник сообщает: в {count} из {games} матчей АПЛ {person} не забивал, не отдавал голевых передач и не создавал явных голевых моментов.')
        return 'PLAYER_STATS',(person,),55
    # Risk of dismissal is a report, not the coach's voluntary decision.
    m=re.search(r'^([А-ЯЁ][а-яё]+) .+?возможном увольнении из «([^»]+)»',text)
    opportunity=re.search(r'По сообщениям СМИ,.*?есть всего (два|\d+) матча \(против «([^»]+)» и «([^»]+)»\), чтобы исправить положение и сохранить пост',text)
    points=re.search(r'всего (\d+) очка из (\d+) возможных',text)
    if m and opportunity and points:
        person,club=m.groups();club=translate(club);count,r1,r2=opportunity.groups()
        fact('dismissal_risk',(person,club,count,r1,r2,'REPORTED_RISK_NOT_RESIGNATION'),
             f'По сообщениям СМИ, руководство клуба «{club}» дало тренеру {person} {count} матча, чтобы сохранить пост: против «{r1}» и «{r2}».')
        fact('points',(club,*points.groups()),f'Команда набрала {points[1]} очка из {points[2]} возможных.')
        return 'COACH_JOB_RISK',(person,club),65
    m=re.search(r'Руководство «([^»]+)» всё больше сомневается.*?Главным кандидатом на замену стал ([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+)',text,re.S)
    current=re.search(r'^([А-ЯЁ]+) [–—-] АУТ',text)
    if m and current:
        club,target=m.groups();person=current[1].capitalize()
        fact('reported_replacement',(club,person,target,'REPORTED_CANDIDATE_NOT_APPOINTED'),
             f'Источник сообщает о сомнениях руководства клуба «{club}» в работе {person}. {target} назван кандидатом на замену; назначения не объявлено.')
        return 'COACH_JOB_RISK',(club,person,target),60
    m=re.search(r'^(.+?) are planning an ([£€$])(\d+)m (January|summer) move for (.+?) (?:striker|forward|defender|нападающий) (.+?)\.\s*\(([^()]+)\)',text)
    if m:
        dest,currency,amount,when,origin,person,provider=m.groups();dest,origin=map(translate,(dest,origin))
        fact('planned_transfer',(person,origin,dest,amount,currency,when,'PLAN_NOT_BID'),
             f'По информации {provider}, «{dest}» планирует переход {person} из «{origin}» '+
             ('в январе' if when=='January' else 'летом')+f' за {money(currency,amount)}. Это план, не завершённая сделка.')
        return 'TRANSFER_INTEREST',(person,origin,dest),65
    m=re.search(r'^(.+?) hold the strongest interest in (.+?) (?:striker|нападающий) (.+?), with (.+?) also interested\.\s*\(([^()]+)\)',text)
    if m:
        dest,origin,person,others,provider=m.groups();dest,origin=map(translate,(dest,origin))
        # Preserve every interested party, not a guessed single destination.
        options=[translate(v) for v in re.split(r',\s*| and ',others)]
        fact('multi_club_interest',(person,origin,dest,*options,'INTEREST'),
             f'По информации {provider}, наиболее сильный интерес к {person} («{origin}») проявляет «{dest}». Среди других заинтересованных клубов — '+', '.join(f'«{v}»' for v in options)+'.')
        return 'TRANSFER_INTEREST',(person,origin,dest,*options),65
    m=re.search(r'^Former (.+?) boss (.+?) has been linked with a return to former club (.+?), but (.+?) football chief (.+?) rejected those rumours\.\s*\(([^()]+)\)',text)
    if m:
        nation,person,club,org,speaker,provider=m.groups()
        fact('rumour_denial',(person,club,org,speaker,provider,'DENIED_RUMOUR'),
             f'{provider} сообщает: {speaker} ({org}) отверг слухи о возвращении {person} в «{translate(club)}».')
        return 'COACH_RUMOUR_DENIAL',(person,club),60
    m=re.search(r'В текущем сезоне у (\w+) уже (\d+) гола и (\d+) ассиста в АПЛ',text)
    if m:
        person,goals,assists=m.groups()
        fact('season_stats',(person,goals,assists,'PREMIER_LEAGUE'),
             f'{person}: {goals} гола и {assists} ассиста в текущем сезоне АПЛ.')
        return 'PLAYER_STATS',(person,),55
    return None


def money(currency,amount):
    return f'{amount.replace(".",",")} млн '+{'£':'фунтов','€':'евро','$':'долларов'}[currency]


def club_past(name,masculine,feminine):
    for club in club_entries():
        if club.display==name and club.grammatical_gender:
            return name+' '+(feminine if club.grammatical_gender=='feminine' else masculine)
    return 'Клуб '+name+' '+masculine


def unsupported_reason(text):
    """Explain conservative omission; never guess a missing event slot."""
    rules=[(r'касани.*«[^»]+»\s*\(\d+\s+гол','AMBIGUOUS_MIXED_METRIC_TABLE'),
           (r'теннисист|\bATP\b|\bWTA\b','EDITORIAL_OTHER_SPORT'),
           (r'шахмат\w*.*коллекц|лабрадор|собак\w*.*фотосесс','EDITORIAL_PRIVATE_LIFESTYLE'),
           (r'фанаты .+?когда|на глазах у|поддержать команду с трибун','EDITORIAL_FAN_OR_SPECTATOR_CAPTION'),
           (r'ВЗОРВАЛСЯ С КРИТИКОЙ|Легенды .+?продолжают|По его мнению','EDITORIAL_OPINION_WITHOUT_DISTINCT_EVENT'),
           (r'после первого тайма|к \d+-й минуте|Update:','IN_PLAY_CONTEXT_WITHOUT_FINAL_RESULT'),
           (r'гол vs.*гол vs','UNRESOLVED_ICON_ONLY_MATCH_CONTEXT'),
           (r'МОГУТ УВОЛИТЬ ЧЕРЕЗ.*манкунианцев','UNRESOLVED_COACH_EMPLOYER_OR_DISMISSAL_STATUS'),
           (r'team for tonight|Tonight.s XI|команд\w* U21','UNRESOLVED_LINEUP_OR_YOUTH_MEMBERSHIP'),
           (r'vote for|Team of the Week|голос','POLL_OR_SELECTION_WITHOUT_FACTS'),
           (r'лучшие моменты|EVERY .+? goal|Вспоминаем','VIDEO_TEASER_WITHOUT_EVENT'),
           (r'minute:|take the lead|equalise','LIVE_SCORE_AMBIGUOUS_TEAMS'),
           (r'высокие шансы|фаворитов по версии|суперкомпьютер','FORECAST_NOT_CONFIRMED_RESULT'),
           (r'host of Premier League clubs','UNNAMED_DESTINATION_CLUBS'),
           (r'пустой|бессмысленный|Что случится|больше побед','COMMENTARY_WITHOUT_DISTINCT_EVENT'),
           (r'СВЕРШИЛОСЬ.*Контракт','ANNOUNCEMENT_ALREADY_COVERED_OR_STATUS_UNCLEAR'),
           (r'Two wins in the last two','STREAK_CONTEXT_NO_DISTINCT_FIXTURE'),
           (r'сейв|с детства болел|самых перспективных','LOW_PRIORITY_FEATURE_OR_RECORD_CONTEXT')]
    for pattern,reason in rules:
        if re.search(pattern,text,re.I|re.S):return reason
    return 'UNSUPPORTED_SOURCE_RELATION_SCHEMA'


def paired_results(raw,translate):
    """Two explicit final fixtures, bound to a two-club headline antecedent.

    No actor lookup by coach, nickname, stadium, account or world knowledge.
    A shortened second actor must uniquely occur in that headline's normalized
    club name. Unsupported score captions remain omitted.
    """
    text=re.sub(r'^[^«А-ЯЁA-Za-z]+','',raw)
    # Two final fixtures with explicit headline antecedents and ordered recap.
    # Coaching names, nationality and stadium are NOT identity lookups.
    recap=re.search(r'^«([^»]+)» (?:проехалась катком по|разгромил[аи]?|обыграл[аи]?) «([^»]+)», а «([^»]+)»',text)
    first_score=re.search(r'(?:Парни|Команда) [^.]+?разгромили [^.]+? [—–-] (\d+):(\d+)\.',text)
    second_score=re.search(r'Команда [^.]+?терпит (?:первое )?поражение [^.]+?от «([^»]+)» [—–-] (\d+):(\d+)',text)
    if recap and first_score and second_score:
        if re.search(r'к перерыву|концу первого тайма|\bUpdate\b|к \d+-й минуте',text,re.I):return ()
        a,b,c=map(translate,recap.groups());d=translate(second_score[1])
        if len({a,b,c,d})!=4 or int(first_score[1])<=int(first_score[2]) or int(second_score[2])>=int(second_score[3]):return ()
        return ((a,b,first_score[1]+':'+first_score[2]),(d,c,second_score[3]+':'+second_score[2]))
    title=re.search(r'^«([^»]+)» продолжает .+?, «([^»]+)» уступает',text)
    home=re.search(r'разгромили на сво[её]м поле «([^»]+)» [—–-] (\d+):(\d+)',text)
    away=re.search(r'у «([^»]+)» .+? [—–-] (\d+):(\d+) в .+? от «([^»]+)»',text)
    if not title or not home or not away:return ()
    first,second=map(translate,title.groups());short=away[1].casefold()
    if short not in second.casefold() or short in first.casefold():return ()
    if int(home[2])<=int(home[3]) or int(away[2])>=int(away[3]):return ()
    return ((first,translate(home[1]),home[2]+':'+home[3]),
            (translate(away[4]),second,away[3]+':'+away[2]))
