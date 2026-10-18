"""Optional bounded API-SPORTS fixture data. No media, paid setup or publisher.

GET only; secrets remain in headers and never enter cache/error diagnostics.
Fresh live snapshots cannot be used as historical cutoff evidence. Cache stores
retrieval time separately from fixture kickoff (kickoff is NOT completion time).
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

BASE = 'https://v3.football.api-sports.io/'
STATUS = {'NS':'SCHEDULED','TBD':'SCHEDULED','1H':'LIVE','2H':'LIVE','ET':'LIVE',
          'BT':'LIVE','P':'LIVE','HT':'HALF_TIME','FT':'FULL_TIME','AET':'FULL_TIME',
          'PEN':'FULL_TIME','PST':'POSTPONED','CANC':'CANCELLED','SUSP':'LIVE','INT':'LIVE'}


class DataUnavailable(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject BEFORE a credential-bearing follow-up request is transmitted.
        raise DataUnavailable('API_REDIRECT_REJECTED')


class MatchData:
    def __init__(self, key, cache, *, opener=None, max_calls=6, budget=25, clock=time.monotonic):
        self.key=key;self.cache=Path(cache);self.opener=opener or build_opener(NoRedirect()).open;self.calls=0
        self.max_calls=max_calls;self.clock=clock;self.deadline=clock()+budget;self.last=0
        self.diagnostics=[]

    def get(self, endpoint, **params):
        if endpoint not in ('status','fixtures','fixtures/events'):raise ValueError('Unsupported endpoint')
        query=urlencode(sorted(params.items()));url=BASE+endpoint+('?' + query if query else '')
        path=self.cache/(hashlib.sha256(url.encode()).hexdigest()+'.json')
        now=datetime.now(timezone.utc)
        try:
            cached=json.loads(path.read_text(encoding='utf-8'))
            stamp=datetime.fromisoformat(cached['retrieved_at'])
            if cached['url']==url and timedelta(0)<=now-stamp<timedelta(minutes=10):
                return cached
        except (OSError,ValueError,KeyError,TypeError):pass
        if not self.key:raise DataUnavailable('API_KEY_UNAVAILABLE')
        if self.calls>=self.max_calls or self.clock()>=self.deadline:raise DataUnavailable('API_BUDGET_EXHAUSTED')
        # A single finite batch, no retry storm on denied plans or throttling.
        delay=max(0,1-(self.clock()-self.last))
        if delay:
            if self.clock()+delay>=self.deadline:raise DataUnavailable('API_BUDGET_EXHAUSTED')
            time.sleep(delay)
        self.calls+=1;self.last=self.clock()
        request=Request(url,headers={'x-apisports-key':self.key,'User-Agent':'PremWire-fixture-audit/1.0'})
        try:
            with self.opener(request,timeout=min(8,max(.1,self.deadline-self.clock()))) as response:
                if response.geturl()!=url:raise DataUnavailable('API_REDIRECT_REJECTED')
                content=response.read(4*1024*1024+1)
            if len(content)>4*1024*1024:raise DataUnavailable('API_RESPONSE_TOO_LARGE')
            payload=json.loads(content)
        except HTTPError as exc:
            raise DataUnavailable('API_RATE_LIMIT' if exc.code==429 else 'API_HTTP_'+str(exc.code)) from None
        except (URLError,OSError,ValueError,TimeoutError):raise DataUnavailable('API_NETWORK_OR_JSON_FAILURE') from None
        errors=payload.get('errors')
        if errors:
            # Never echo arbitrary server fields/body; plan errors are data, not
            # authority to upgrade a subscription or bypass restrictions.
            raise DataUnavailable('API_PLAN_RESTRICTED' if isinstance(errors,dict) and 'plan' in errors else 'API_REQUEST_REJECTED')
        record=dict(url=url,retrieved_at=now.isoformat(),response=payload.get('response',[]))
        if endpoint=='status':
            # Account/subscription fields are not cached; quota only.
            r=record['response'];record['response']={'requests':r.get('requests',{}),'subscription_active':r.get('subscription',{}).get('active')}
        self.cache.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8');temporary.replace(path)
        return record

    def fixtures(self, day, league, season):
        if type(league) is not int or type(season) is not int:raise ValueError('Explicit league/season required')
        return self.get('fixtures',date=day.isoformat(),league=league,season=season,timezone='UTC')

    def events(self, fixture_id):
        if type(fixture_id) is not int or fixture_id<=0:raise ValueError('Explicit fixture ID required')
        return self.get('fixtures/events',fixture=fixture_id)


def normalize_fixture(raw, retrieved_at, cutoff, *, historical=False, diagnostic=False):
    """Strict fixture evidence with its availability clock, never future leak."""
    retrieved=datetime.fromisoformat(retrieved_at)
    if retrieved.tzinfo is None or cutoff.tzinfo is None:raise ValueError('Aware evidence clocks required')
    if retrieved>=cutoff and not diagnostic:raise DataUnavailable('FUTURE_EVIDENCE_NOT_AVAILABLE_AT_CUTOFF')
    fixture=raw['fixture'];teams=raw['teams'];goals=raw['goals'];league=raw['league']
    start=datetime.fromisoformat(fixture['date'])
    if start.tzinfo is None:raise DataUnavailable('API_UNZONED_FIXTURE')
    status=STATUS.get(fixture['status']['short'])
    if status is None:raise DataUnavailable('API_UNKNOWN_MATCH_STATUS')
    if start>=cutoff and status in ('FULL_TIME','LIVE','HALF_TIME'):raise DataUnavailable('API_FIXTURE_AFTER_CUTOFF')
    if teams['home']['id']==teams['away']['id']:raise DataUnavailable('API_IDENTICAL_TEAMS')
    if any(type(v) is not int or v<0 for v in goals.values() if v is not None):raise DataUnavailable('API_INVALID_SCORE')
    if status=='FULL_TIME' and (goals.get('home') is None or goals.get('away') is None):raise DataUnavailable('API_FINAL_SCORE_MISSING')
    from .daily_text_digest import MOSCOW
    return dict(fixture_id=fixture['id'],home=teams['home']['name'],away=teams['away']['name'],
                home_id=teams['home']['id'],away_id=teams['away']['id'],status=status,
                kickoff_at=start.astimezone(timezone.utc).isoformat(),
                event_date=start.astimezone(MOSCOW).date().isoformat(),
                score=(goals.get('home'),goals.get('away')),competition=league['name'],league_id=league['id'],
                available_at=retrieved.astimezone(timezone.utc).isoformat(),source=BASE+'fixtures?id='+str(fixture['id']))


def bind_fixture(bundle, fixtures):
    """Only exact canonical SOURCE/alias team pairs, competition and date.

    An unresolved Cyrillic inflection is an omission, never a fuzzy identity.
    Returned records are context evidence; this function never overwrites score.
    """
    from .russian_editorial import club_entries
    def name(value):
        value=value.casefold().strip(' «»')
        matches={e.entity_id for e in club_entries() if value in {s.casefold() for s in e.aliases}}
        return next(iter(matches)) if len(matches)==1 else value
    teams={name(v) for v in bundle.participants[:2]}
    competition=next((f.values[0] for f in bundle.verified_facts if f.key=='competition'),'')
    competitions={'CHAMPIONS_LEAGUE':'UEFA Champions League','EUROPA_LEAGUE':'UEFA Europa League','PREMIER_LEAGUE':'Premier League',
                  'LA_LIGA':'La Liga','SERIE_A':'Serie A','BUNDESLIGA':'Bundesliga','LIGUE_1':'Ligue 1'}
    typed=next((f.values for f in bundle.verified_facts if f.key=='fixture_identity'),None)
    if typed:
        matches=[f for f in fixtures if f.get('provider')==typed[0] and str(f['fixture_id'])==typed[1]
                 and str(f['home_id'])==typed[2] and str(f['away_id'])==typed[3] and f['kickoff_at']==typed[4]]
        if len(matches)!=1:return None,'MATCH_IDENTITY_OR_DATE_NOT_UNIQUE'
        match=matches[0]
        if match['status']!='FULL_TIME':return None,'MATCH_NOT_FINAL'
        result=next((f.values for f in bundle.verified_facts if f.key in ('result','draw_result')),None)
        if not result:return None,'SOURCE_RESULT_MISSING'
        score=match['score']
        expected=f'{max(score)}:{min(score)}'
        return (match,'PROVIDER_SOURCE_CONSISTENT_NOT_INDEPENDENT') if result[2]==expected else (None,'MATCH_SOURCE_API_CONFLICT')
    matches=[f for f in fixtures if {name(f['home']),name(f['away'])}==teams
             and bundle.event_date and f['event_date']==bundle.event_date
             and competition in competitions and f['competition']==competitions[competition]]
    if len(matches)!=1:return None,'MATCH_IDENTITY_OR_DATE_NOT_UNIQUE'
    match=matches[0]
    if match['status']!='FULL_TIME':return None,'MATCH_NOT_FINAL'
    result=next((f.values for f in bundle.verified_facts if f.key=='result'),None)
    if not result:return None,'SOURCE_RESULT_MISSING'
    home,away=match['score'];winner=match['home'] if home>away else match['away']
    if home==away or name(winner)!=name(result[0]) or f'{max(home,away)}:{min(home,away)}'!=result[2]:
        return None,'MATCH_SOURCE_API_CONFLICT'
    return match,'EXTERNALLY_CONFIRMED'


def match_context_gate(bundles, inputs, cutoff, fixtures=()):
    """Audit SOURCE-only enrichment; use independent snapshots only if bound.

    A denied/missing API is not proof of a false result. Do not fabricate an
    independent confirmation, date or scorer to fill the gaps. Exact bound
    contradictory evidence excludes the story rather than silently fixing it.
    """
    from dataclasses import replace
    result=[];checks=[]
    for b in bundles:
        if b.event_type not in ('MATCH_RESULT','MATCH_HALFTIME'):
            result.append(b);continue
        status='FULL_TIME' if b.event_type=='MATCH_RESULT' else 'HALF_TIME'
        fields={f.key for f in b.verified_facts}
        available=[f for f in fixtures if datetime.fromisoformat(f['available_at'])<cutoff]
        bound,reason=bind_fixture(b,available) if available and status=='FULL_TIME' else (None,'SOURCE_VERIFIED_NOT_INDEPENDENTLY_CONFIRMED')
        rejected=reason in ('MATCH_SOURCE_API_CONFLICT','MATCH_NOT_FINAL')
        checks.append(dict(event_id=b.event_id,status=status,verification=reason,
                           confirmed_source_fields=sorted(fields & {'result','competition','match_date','scorers','red_card'}),
                           missing_fields=sorted({'competition','match_date','scorers'}-fields),
                           decision='REJECT' if rejected else 'KEEP',
                           external_fixture_id=bound['fixture_id'] if bound else None,
                           source_context_enriched=bool(fields & {'competition','match_date','scorers','red_card'})))
        if not rejected:result.append(replace(b,match_status=status))
    return result,checks
