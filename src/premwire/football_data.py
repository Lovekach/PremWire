"""Optional licensed match SOURCE. Bounded GETs, no publisher or model.

Only observed FINISHED/REGULAR scores become facts. Retrieval time is evidence
availability, never kickoff or lastUpdated. Historical cutoffs cannot use later
snapshots. Graphics, lineups and speculative completion times are not used.
"""
from datetime import datetime, date, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .daily_match_data import DataUnavailable, NoRedirect

BASE = 'https://api.football-data.org/v4/'
SOURCE_ID = 'football_data_matches'
# Presentation-only exact aliases; never eligibility, fixture identity or a
# fallback entity guess. Unknown labels remain verbatim API names.
DISPLAY = {'Ipswich Town':'Ипсвич','Valencia':'Валенсия','Athletic':'Атлетик',
           'Real Betis':'Бетис','Elversberg':'Эльверсберг','Augsburg':'Аугсбург',
           'Atleti':'Атлетико','Espanyol':'Эспаньол','HSV':'Гамбург','Stuttgart':'Штутгарт',
           'Brest':'Брест','Schalke':'Шальке','Bremen':'Вердер','SC Paderborn':'Падерборн',
           'Venezia FC':'Венеция','Villarreal':'Вильярреал','Elche':'Эльче',
           'Union Berlin':'Унион','Genoa':'Дженоа','RC Lens':'Ланс','Troyes':'Труа'}
COMPETITIONS = {
    'PL': ('PREMIER_LEAGUE', 'Premier League', 'АПЛ'),
    'CL': ('CHAMPIONS_LEAGUE', 'UEFA Champions League', 'Лига чемпионов'),
    'PD': ('LA_LIGA', 'La Liga', 'Ла Лига'),
    'SA': ('SERIE_A', 'Serie A', 'Серия А'),
    'BL1': ('BUNDESLIGA', 'Bundesliga', 'Бундеслига'),
    'FL1': ('LIGUE_1', 'Ligue 1', 'Лига 1'),
}
STATUS = {'SCHEDULED': 'SCHEDULED', 'TIMED': 'SCHEDULED',
          'IN_PLAY': 'LIVE', 'PAUSED': 'HALF_TIME', 'SUSPENDED': 'LIVE',
          'FINISHED': 'FULL_TIME', 'POSTPONED': 'POSTPONED', 'CANCELLED': 'CANCELLED'}


def local_token():
    """Read only this optional key from the established local runtime .env."""
    from .runtime_paths import runtime_path
    try:
        for line in runtime_path('.env').read_text(encoding='utf-8-sig').splitlines():
            name, sep, value = line.strip().partition('=')
            if sep and name.strip() == 'FOOTBALL_DATA_TOKEN':
                return value.strip().strip('\"\'')
    except (OSError, UnicodeError):
        pass
    return ''


def collect_optional(cache, start, end):
    """Production adapter; unavailable optional SOURCE never stops safe news."""
    from .daily_text_digest import MOSCOW
    if end.astimezone(MOSCOW).date()!=datetime.now(timezone.utc).astimezone(MOSCOW).date():
        # Historical replays must not fetch today's mutable API as old evidence.
        return [], None, end
    token=local_token()
    if not token:
        return [], None, end
    return collect(FootballData(token,cache,max_calls=1,budget=12),start,end)


def instant(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('UNZONED_EVIDENCE')
    return stamp.astimezone(timezone.utc)


class FootballData:
    """At most six calls/55s, >=6.1s spacing, no retries/redirects/error bodies.

    Cache contains allowlisted public match fields only, not response headers,
    account data, arbitrary fields or credentials. Caller owns its private path.
    """
    def __init__(self, token, cache, *, opener=None, clock=time.monotonic,
                 sleep=time.sleep, budget=55, max_calls=6):
        self._token = token
        self.cache = Path(cache)
        self.opener = opener or build_opener(NoRedirect()).open
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + min(55, max(0, budget))
        self.max_calls = min(6, max_calls)
        self.calls = 0
        self.last = None

    def matches(self, code, first, last):
        if code not in (*COMPETITIONS,'ALL') or type(first) is not date or type(last) is not date or not 0 < (last-first).days <= 4:
            raise ValueError('EXPLICIT_BOUNDED_DATE_RANGE_REQUIRED')
        params={'dateFrom': first.isoformat(), 'dateTo': last.isoformat()}
        endpoint='competitions/'+code+'/matches'
        if code=='ALL':
            endpoint='matches'
            params['competitions']=','.join(COMPETITIONS)
        url = BASE + endpoint + '?' + urlencode(params)
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        now = datetime.now(timezone.utc)
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached['url'] == url and isinstance(cached['matches'],list) and timedelta(0) <= now-instant(cached['retrieved_at']) < timedelta(minutes=5):
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not self._token:
            raise DataUnavailable('FOOTBALL_DATA_TOKEN_UNAVAILABLE')
        delay = max(0, 6.1-(self.clock()-self.last)) if self.last is not None else 0
        if self.calls >= self.max_calls or self.clock()+delay >= self.deadline:
            raise DataUnavailable('FOOTBALL_DATA_BUDGET_EXHAUSTED')
        if delay:
            self.sleep(delay)
        self.calls += 1
        self.last = self.clock()
        request = Request(url, headers={'X-Auth-Token': self._token, 'User-Agent': 'PremWire-matches/1.0'})
        try:
            with self.opener(request, timeout=min(4, max(.1, self.deadline-self.clock()))) as response:
                if response.geturl() != url:
                    raise DataUnavailable('FOOTBALL_DATA_REDIRECT_REJECTED')
                raw = response.read(4*1024*1024+1)
            if len(raw) > 4*1024*1024:
                raise ValueError('oversize')
            payload = json.loads(raw)
            rows = payload['matches']
            if not isinstance(rows, list) or payload['resultSet']['count'] != len(rows):
                raise ValueError('schema')
            if code=='ALL':
                if set(payload['filters']['competitions'].split(','))!=set(COMPETITIONS):
                    raise ValueError('missing or ignored competition filter')
            elif payload['competition']['code']!=code:
                raise ValueError('wrong competition')
            # Source schema: preserve only fields needed to validate identities,
            # season, clocks, match status and score. No media or arbitrary data.
            rows = [{k: row[k] for k in ('id', 'utcDate', 'status', 'lastUpdated', 'competition', 'season', 'homeTeam', 'awayTeam', 'score')} for row in rows]
            for row in rows:
                row['competition'] = {k: row['competition'][k] for k in ('id', 'code', 'name')}
                row['season'] = {k: row['season'][k] for k in ('startDate', 'endDate')}
                for side in ('homeTeam', 'awayTeam'):
                    row[side] = {k: row[side][k] for k in ('id', 'name', 'shortName')}
                row['score'] = {k: row['score'][k] for k in ('winner', 'duration', 'fullTime')}
                if row['competition']['code'] not in (COMPETITIONS if code=='ALL' else (code,)):
                    raise ValueError('wrong competition')
        except HTTPError as exc:
            raise DataUnavailable('FOOTBALL_DATA_HTTP_'+str(exc.code)) from None
        except (URLError, OSError, ValueError, KeyError, TypeError, TimeoutError):
            raise DataUnavailable('FOOTBALL_DATA_NETWORK_OR_SCHEMA_FAILURE') from None
        record = dict(url=url, retrieved_at=datetime.now(timezone.utc).isoformat(), matches=rows)
        temporary = path.with_suffix('.tmp')
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        except OSError:
            pass  # A read-only cache cannot suppress already verified SOURCE.
        return record


def normalize(raw, observed, cutoff):
    """Strict normalization; awarded/extra-time/penalties fail closed for now."""
    from .daily_text_digest import MOSCOW
    if not isinstance(raw,dict) or any(not isinstance(raw.get(key),dict) for key in ('competition','season','homeTeam','awayTeam','score')) or not isinstance(raw['score'].get('fullTime'),dict):
        raise ValueError('INVALID_FIXTURE_SCHEMA')
    observed, kickoff, updated = map(instant, (observed, raw['utcDate'], raw['lastUpdated']))
    if observed >= cutoff or updated > observed:
        raise ValueError('FUTURE_EVIDENCE')
    code = raw['competition']['code']
    if code not in COMPETITIONS:
        raise ValueError('UNSUPPORTED_COMPETITION')
    ids = (raw['id'], raw['homeTeam']['id'], raw['awayTeam']['id'])
    if any(type(v) is not int or v <= 0 for v in ids) or ids[1] == ids[2]:
        raise ValueError('INVALID_IDENTITY')
    for side in ('homeTeam', 'awayTeam'):
        if any(not isinstance(raw[side][key], str) or not re.fullmatch(r"[^\n\r<>]{1,90}", raw[side][key]) for key in ('name', 'shortName')):
            raise ValueError('INVALID_TEAM_NAME')
    if not date.fromisoformat(raw['season']['startDate']) <= kickoff.date() <= date.fromisoformat(raw['season']['endDate']):
        raise ValueError('FIXTURE_OUTSIDE_SEASON')
    status = STATUS.get(raw['status'])
    if not status:
        raise ValueError('UNSUPPORTED_STATUS')
    score = raw['score']['fullTime']
    values = (score.get('home'), score.get('away'))
    if status == 'FULL_TIME':
        if kickoff >= observed or raw['score']['duration'] != 'REGULAR' or any(type(v) is not int or not 0 <= v <= 99 for v in values):
            raise ValueError('UNSUPPORTED_OR_INVALID_FINAL_SCORE')
        winner = 'DRAW' if values[0] == values[1] else 'HOME_TEAM' if values[0] > values[1] else 'AWAY_TEAM'
        if raw['score']['winner'] != winner:
            raise ValueError('WINNER_SCORE_CONFLICT')
    return dict(fixture_id=raw['id'], competition=COMPETITIONS[code][1], competition_code=code,
                home=raw['homeTeam']['shortName'], away=raw['awayTeam']['shortName'],
                home_team=raw['homeTeam']['shortName'],away_team=raw['awayTeam']['shortName'],
                match_status=status,home_score=values[0],away_score=values[1],
                home_id=ids[1], away_id=ids[2], status=status, score=values,
                kickoff_at=kickoff.isoformat(), event_date=kickoff.astimezone(MOSCOW).date().isoformat(),
                available_at=observed.isoformat(), source_timestamp=updated.isoformat(),
                source=BASE+'matches/'+str(raw['id']), verification_status='PROVIDER_OBSERVED',
                provider='football-data.org')


def source_item(raw, observed):
    from .models import IncomingItem
    stamp = instant(observed)
    return IncomingItem(SOURCE_ID, str(raw['id']), json.dumps(raw, ensure_ascii=False, sort_keys=True),
                        BASE+'matches/'+str(raw['id']), stamp, source_collected_at=stamp.isoformat(),
                        first_seen_at=stamp.isoformat(), last_seen_at=stamp.isoformat())


def compile_sources(items, cutoff):
    """Compile typed original JSON, not a fabricated prose SOURCE or LLM output."""
    from .daily_fact_digest import ImmutableBundle, VerifiedFact, russian
    from .daily_text_digest import MOSCOW, MONTHS, SECTIONS
    from .russian_editorial import club_entries
    def display(team):
        aliases = {s.casefold(): e.display for e in club_entries() for s in e.aliases}
        return aliases.get(team.casefold(), DISPLAY.get(team, russian(team)))
    bundles, decisions = [], []
    for item in items:
        try:
            raw = json.loads(item.text)
            row = normalize(raw, item.published_at.isoformat(), cutoff)
            if item.source_id != SOURCE_ID or item.external_id != str(row['fixture_id']) or item.canonical_url != row['source'] or instant(item.source_collected_at) < instant(row['available_at']):
                raise ValueError('SOURCE_IDENTITY_MISMATCH')
            if row['status'] != 'FULL_TIME':
                from .match_day_editorial import nonfinal
                bundles.append(nonfinal(item,cutoff))
                decisions.append(dict(url=item.canonical_url,decision='KEEP',reason='TYPED_PROVIDER_NONFINAL_STAGE'))
                continue
            home, away = display(row['home']), display(row['away'])
            hs, aws = row['score']
            if hs == aws:
                key, values = 'draw_result', (home, away, f'{hs}:{aws}')
                phrase = f'«{home}» и «{away}» сыграли вничью — {hs}:{aws}.'
            else:
                winner, loser = (home, away) if hs > aws else (away, home)
                key, values = 'result', (winner, loser, f'{max(hs,aws)}:{min(hs,aws)}')
                phrase = f'Клуб «{winner}» обыграл «{loser}» — {max(hs,aws)}:{min(hs,aws)}.'
            day = date.fromisoformat(row['event_date'])
            facts = []
            for k, v, wording in (
                (key, values, phrase),
                ('competition', (COMPETITIONS[row['competition_code']][0],), f'Турнир: {COMPETITIONS[row["competition_code"]][2]}.'),
                ('match_date', (row['event_date'],), f'Матч состоялся {day.day} {MONTHS[day.month-1]}.'),
                ('fixture_identity', ('football-data.org', str(row['fixture_id']), str(row['home_id']), str(row['away_id']), row['kickoff_at']), ''),
            ):
                facts.append(VerifiedFact(k, tuple(v), (item.canonical_url,), item.text, (wording, wording)))
            eid = hashlib.sha256(('football-data.org:'+str(row['fixture_id'])).encode()).hexdigest()[:16]
            bundles.append(ImmutableBundle(eid, 'MATCH_RESULT', (home, away), tuple(facts),
                (item.canonical_url,), 96 if row['competition_code'] in ('PL','CL') else 90,
                (key, 'competition', 'match_date'), SECTIONS[2],
                source_published_at=item.published_at.isoformat(), event_date=row['event_date'],
                source_collected_at=row['available_at'], event_updated_at=row['source_timestamp'],
                date_basis='PROVIDER_KICKOFF_DATE_NOT_COMPLETION', source_id=SOURCE_ID, match_status='FULL_TIME'))
            decisions.append(dict(url=item.canonical_url, decision='KEEP', reason='TYPED_PROVIDER_FINAL'))
        except (ValueError, KeyError, TypeError, IndexError):
            decisions.append(dict(url=item.canonical_url, decision='REJECT', reason='INVALID_OR_NONFINAL_PROVIDER_SOURCE'))
    return bundles, decisions


def merge_results(bundles, history=()):
    """One exact fixture per edition; no fuzzy names or score substitution.

    Provider IDs bind provider history. Cross-provider identity requires exact
    aliases, explicit event date and competition. Conflicts reject both, while
    a prior printed undated same result in a 24h report window prevents repost.
    """
    from .russian_editorial import club_entries
    from dataclasses import replace
    from .editorial_v2 import provider_aliases
    observed_aliases=provider_aliases((*bundles,*history))
    def name(value):
        known = {s.casefold(): e.entity_id for e in club_entries() for s in (*e.aliases, e.display)}
        return observed_aliases.get(value.casefold(),known.get(value.casefold(), value.casefold()))
    def identity(b):
        competition = next((f.values[0] for f in b.verified_facts if f.key == 'competition'), '')
        return tuple(sorted(name(p) for p in b.participants[:2])), competition, b.event_date
    def result(b):
        f = next((f for f in b.verified_facts if f.key in ('result', 'draw_result')), None)
        if not f: return None
        return f.key, tuple(name(v) for v in f.values[:2]), f.values[2]
    def provider_id(b):
        return next((f.values[:2] for f in b.verified_facts if f.key == 'fixture_identity'), None)
    def compatible_caption(caption, typed):
        if caption.event_type!='MATCH_RESULT' or not provider_id(typed) or provider_id(caption):return False
        a,b=identity(caption),identity(typed)
        if a[0]!=b[0] or a[1] and a[1]!=b[1] or result(caption)!=result(typed):return False
        if caption.event_date:return caption.event_date==typed.event_date
        # Missing date is NOT filled in. A unique same-result caption in this
        # report window is suppressed in favour of the fully scoped API fact.
        fixture=next(f for f in typed.verified_facts if f.key=='fixture_identity')
        if not caption.source_published_at:return False
        kickoff=instant(fixture.values[4]);reported=instant(caption.source_published_at)
        return kickoff<=reported<=kickoff+timedelta(hours=24)
    if not any(provider_id(b) for b in (*bundles,*history)):
        return list(bundles), []  # Preserve the accepted legacy-only gate path.
    groups, output, decisions = {}, [], []
    for b in bundles:
        if any(compatible_caption(b,old) for old in history):
            decisions.append(dict(event_id=b.event_id,sources=b.source_refs,decision='REJECT',reason='CAPTION_OF_ALREADY_PUBLISHED_FIXTURE'))
            continue
        candidates=[p for p in bundles if compatible_caption(b,p)]
        if len({provider_id(p) for p in candidates})>1 and (not b.event_date or not identity(b)[1]):
            decisions.append(dict(event_id=b.event_id,sources=b.source_refs,decision='REJECT',reason='AMBIGUOUS_PROVIDER_BINDING_CAPTION_OMITTED'))
            continue
        if len({provider_id(p) for p in candidates})==1:
            typed=candidates[0]
            if not b.event_date:
                decisions.append(dict(event_id=b.event_id,sources=b.source_refs,decision='REJECT',reason='UNDATED_CAPTION_COVERED_BY_UNIQUE_VERIFIED_FINAL'))
                continue
            # Exact teams/date/score and one observed fixture safely supply a
            # missing competition/identity; keep original confirmed context.
            keys={f.key for f in b.verified_facts}
            extra=tuple(f for f in typed.verified_facts if f.key in ('competition','fixture_identity') and f.key not in keys)
            b=replace(b,verified_facts=b.verified_facts+extra,
                source_refs=tuple(dict.fromkeys(b.source_refs+tuple(u for f in extra for u in f.source_refs))),
                essential_facts=b.essential_facts+tuple(f.key for f in extra if f.key!='fixture_identity'))
        if b.event_type != 'MATCH_RESULT' or not b.event_date:
            output.append(b)
            continue
        duplicate = any(old.event_type == 'MATCH_RESULT' and (
            provider_id(b) and provider_id(b) == provider_id(old) or
            identity(b) == identity(old) or
            compatible_caption(old,b) or
            (not old.event_date and identity(b)[:2] == identity(old)[:2] and result(b) == result(old)
             and old.source_published_at and b.source_published_at and
             abs((instant(b.source_published_at)-instant(old.source_published_at)).total_seconds()) <= 86400)
        ) for old in history)
        if duplicate:
            decisions.append(dict(event_id=b.event_id, sources=b.source_refs, decision='REJECT', reason='FIXTURE_ALREADY_PUBLISHED'))
        else:
            groups.setdefault(identity(b), []).append(b)
    for group in groups.values():
        signatures = {result(b) for b in group}
        if len(signatures) > 1:
            decisions.extend(dict(event_id=b.event_id, sources=b.source_refs, decision='REJECT', reason='EXACT_FIXTURE_SCORE_CONFLICT') for b in group)
            continue
        # Prefer richer original SOURCE details; same final is not a second item.
        keep = max(group, key=lambda b: (len(b.verified_facts), bool(provider_id(b)), b.event_id))
        typed=next((b for b in group if provider_id(b)),None)
        if typed is not None and not provider_id(keep):
            fact=next(f for f in typed.verified_facts if f.key=='fixture_identity')
            keep=replace(keep,verified_facts=keep.verified_facts+(fact,),source_refs=tuple(dict.fromkeys(keep.source_refs+fact.source_refs)))
        output.append(keep)
        decisions.extend(dict(event_id=b.event_id, sources=b.source_refs, decision='MERGE', reason='SAME_EXACT_FIXTURE') for b in group if b.event_id!=keep.event_id)
    return output, decisions


def collect(client, start, now):
    """Real bounded calendar collection, same/previous Moscow day, partial safe."""
    from .daily_text_digest import MOSCOW
    first = max(start.astimezone(MOSCOW).date()-timedelta(days=1), now.astimezone(MOSCOW).date()-timedelta(days=1))
    # UTC query is a superset of local dates. API dateTo is exclusive.
    last = now.astimezone(MOSCOW).date()+timedelta(days=1)
    records, errors, complete = [], [], []
    for code in ('ALL',):
        try:
            snapshot = client.matches(code, first-timedelta(days=1), last)
            records.append(snapshot)
            complete.extend(value[0] for value in COMPETITIONS.values())
        except DataUnavailable as exc:
            errors.append(dict(competition=code, reason=str(exc)))
            if str(exc) in ('FOOTBALL_DATA_HTTP_401', 'FOOTBALL_DATA_HTTP_429', 'FOOTBALL_DATA_TOKEN_UNAVAILABLE'):
                break
    # Honest final refresh cutoff: after the observed responses, not batch start.
    cutoff = datetime.now(timezone.utc)
    fixtures, items = [], []
    for record in records:
        for raw in record['matches']:
            try:
                row = normalize(raw, record['retrieved_at'], cutoff)
                fixtures.append(row)
                if first.isoformat() <= row['event_date'] <= now.astimezone(MOSCOW).date().isoformat() and row['status'] in ('FULL_TIME','SCHEDULED','LIVE','HALF_TIME'):
                    items.append(source_item(raw, record['retrieved_at']))
            except (ValueError, KeyError, TypeError):
                errors.append(dict(competition=raw.get('competition',{}).get('code') if isinstance(raw,dict) and isinstance(raw.get('competition'),dict) else None, reason='ROW_REJECTED'))
    return items, dict(available_at=max((r['retrieved_at'] for r in records), default=cutoff.isoformat()),
                      window_start=datetime.combine(first-timedelta(days=1), datetime.min.time(), timezone.utc).isoformat(),
                      window_end=datetime.combine(last, datetime.min.time(), timezone.utc).isoformat(),
                      requested_competitions=['PREMIER_LEAGUE','CHAMPIONS_LEAGUE','EUROPA_LEAGUE','LA_LIGA','SERIE_A','BUNDESLIGA','LIGUE_1'],
                      complete_competitions=complete, fixtures=fixtures, errors=errors), cutoff
