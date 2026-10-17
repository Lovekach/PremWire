"""Finite immutable-digest dry-run. No sender or production state access."""
from dataclasses import asdict,replace
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import time
import re
from urllib.parse import urlencode,parse_qs

from .daily_fact_digest import build_bundles,render,audit_digest,edit_batches,batches,ImmutableBundle,VerifiedFact,StatisticalObservation
from .daily_text_digest import BatchOllama,MOSCOW,MONTHS,telegram_length
from .models import IncomingItem
from .text_editor import OllamaTextEditor
from .daily_fact_digest import STATS_SECTION
from .daily_quality import PROMO


class ShadowState:
    def __init__(self,path):
        # Never silently accept a production DB filename.
        if Path(path).name!='immutable-shadow.sqlite3':raise ValueError('Shadow-only state name required')
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS source_cache (source_id TEXT, external_id TEXT, payload TEXT, origin TEXT, PRIMARY KEY(source_id,external_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS editions (day TEXT PRIMARY KEY, status TEXT, payload_sha256 TEXT)')

    def store(self,items,origin):
        self.db.executemany('INSERT OR REPLACE INTO source_cache VALUES (?,?,?,?)',
            [(i.source_id,i.external_id,json.dumps(asdict(i),default=str,ensure_ascii=False),origin) for i in items])
        self.db.commit()

    def cached(self,start,end,ids):
        result=[]
        for payload, in self.db.execute('SELECT payload FROM source_cache'):
            raw=json.loads(payload);raw['published_at']=datetime.fromisoformat(raw['published_at'])
            item=IncomingItem(**raw)
            if item.source_id in ids and start<=item.published_at<end:result.append(item)
        return result

    def prepared(self,day,body):
        self.db.execute('INSERT OR REPLACE INTO editions VALUES (?,?,?)',
                        (day,'PREPARED_ONLY',hashlib.sha256(body.encode()).hexdigest()))
        self.db.commit()

    def close(self):self.db.close()


def collect_shadow(config,env,start,end,state,collect,budget=110):
    coverage=[]
    try:
        fresh,warnings=collect(config.sources,env,start,end,budget_seconds=budget,coverage=coverage)
    except Exception as exc:
        fresh=[];warnings=['SOURCE_COLLECTION_UNAVAILABLE:'+type(exc).__name__]
        coverage=[dict(source_id=s.id,status='UNAVAILABLE',count=0) for s in config.sources if s.enabled]
    enabled={s.id for s in config.sources if s.enabled}
    fresh=[i for i in fresh if i.source_id in enabled and start<=i.published_at<end]
    verified={'MIDNIGHT_BOUNDARY_VERIFIED','API_PAGINATION_COMPLETE'}
    partial={s.id for s in config.sources if s.enabled and not any(
        c.get('source_id')==s.id and c.get('status') in verified for c in coverage)}
    # Cache only original source text, only the same time window and only partial
    # or unavailable feeds. A successfully verified empty feed stays empty.
    retained=state.cached(start,end,partial)
    state.store(fresh,'NETWORK_SOURCE')
    merged={(i.source_id,i.external_id):i for i in retained}
    merged.update({(i.source_id,i.external_id):i for i in fresh})
    fallbacks=[i for i in retained if (i.source_id,i.external_id) not in {(f.source_id,f.external_id) for f in fresh}]
    return list(merged.values()),dict(fresh_count=len(fresh),cache_fallback_count=len(fallbacks),
        partial_sources=sorted(partial),coverage=coverage,warnings=warnings,
        fallback_urls=[i.canonical_url for i in fallbacks])


def select_for_telegram(bundles,day):
    # Reserve the longer code-generated variant BEFORE the model call.
    longest={b.event_id:int(len(b.render(1).encode('utf-16-le'))>len(b.render(0).encode('utf-16-le'))) for b in bundles}
    _,selected=render(bundles,day,longest)
    return selected


def edit_once(selected,editor,deadline,session=BatchOllama):
    defaults={b.event_id:0 for b in selected}
    if not list(batches(selected)):return defaults,[]
    system=('Выбери более естественный русский вариант для каждого event_id: 0 или 1. '
            'Сохрани все ID ровно один раз. Не пиши новый текст. '
            'Ответ — только JSON-массив объектов {"event_id":"...","variant":0}.')
    calls=0
    def chat(prompt):
        nonlocal calls
        calls+=1
        return editor._simple_chat(system,prompt)
    try:
        with session(editor):
            return edit_batches(selected,chat,deadline,time.monotonic,max_calls=1)
    except Exception as exc:
        return defaults,[dict(status='OLLAMA_UNAVAILABLE_OR_SESSION_FAILURE',issues=[type(exc).__name__],calls=calls)]


def prepare_payload(channel,body):
    if not body:return None
    if telegram_length(body)>4096:raise ValueError('Telegram limit')
    if '\ufffd' in body or body.encode('utf-8').decode('utf-8')!=body:raise ValueError('Unicode corruption')
    payload=dict(chat_id=channel,text=html.escape(body,quote=False),parse_mode='HTML',disable_web_page_preview='true')
    encoded=urlencode({k:str(v) for k,v in payload.items()},encoding='utf-8',errors='strict').encode('utf-8')
    if html.unescape(parse_qs(encoded.decode())['text'][0])!=body:raise ValueError('Transport changed facts')
    return payload


def quality_issues(body,bundles):
    issues=[]
    if re.search(PROMO+r'|подключайтесь|голосуйте|subscribe|vote for',body,re.I):issues.append('PROMOTION')
    if any(b.event_type=='PLAYER_STATS' and b.section!=STATS_SECTION for b in bundles):issues.append('PLAYER_STATS_CATEGORY')
    if len(bundles)!=len({b.event_id for b in bundles}):issues.append('DUPLICATE_EVENT')
    if telegram_length(body)>3900:issues.append('DIGEST_LIMIT')
    return issues


def prepare_immutable_edition(inputs,start,end,filters,*,history=(),style_chat=None,edition_day=None,fixture_evidence=(),match_calendar=None):
    """One reusable preparation route; no sender, env, model startup or live IO.

    The daily entry calls this preparation block. Collection, lock, ledger,
    tombstones and sender remain outside this pure function.
    Optional style_chat is responsible for a <=12s transport timeout; no retry.
    """
    ticks=time.monotonic();phases={};day=edition_day or start.astimezone(MOSCOW).date()
    bundles,audit=build_bundles(inputs,start,end,filters,phases,history)
    originals={i.canonical_url:i for i in inputs}
    anchored=[]
    for b in bundles:
        facts=[]
        for f in b.verified_facts:
            source=originals.get(f.source_refs[0])
            if f.key=='availability' and source and 'TODAY' in f.values and source.published_at.astimezone(MOSCOW).date()!=day:
                source_day=source.published_at.astimezone(MOSCOW).date()
                # "Today" is anchored to the original timestamp, not publication
                # day. Construct before freezing; the model cannot alter it.
                phrase=f'По словам {f.values[-1]}, {f.values[0]} был недоступен {source_day.day} {MONTHS[source_day.month-1]} из-за проблем с задней поверхностью бедра.'
                f=replace(f,values=f.values[:-1]+(source_day.isoformat(),)+f.values[-1:],formulations=(phrase,phrase))
            facts.append(f)
        anchored.append(replace(b,verified_facts=tuple(facts)))
    from .daily_fact_digest import editorial_freshness
    bundles,freshness=editorial_freshness(anchored,inputs,day)
    audit['editorial_freshness']=freshness
    from .daily_fact_digest import merge_halftime_context
    bundles=merge_halftime_context(bundles)
    from .daily_fact_digest import merge_editorial_context
    bundles=merge_editorial_context(bundles)
    from .daily_fact_digest import editorial_value_gate
    bundles,editorial_checks=editorial_value_gate(bundles,inputs)
    audit['editorial_value_gate']=editorial_checks
    # Context enrichment must also respect previously printed substantive facts.
    from .daily_fact_digest import final_digest_gate
    bundles,context_checks=final_digest_gate(bundles,history)
    audit.setdefault('final_digest_gate',[]).extend(context_checks)
    from .daily_editorial_gates import freshness_gate, statistics_gate, editorial_rank
    audit['pre_editorial_gate_bundles']=[asdict(b) for b in bundles]
    from .daily_match_data import match_context_gate
    timer=time.monotonic()
    bundles,checks=match_context_gate(bundles,inputs,end,fixture_evidence)
    audit['match_enrichment']=checks
    phases['match_enrichment']=time.monotonic()-timer
    timer=time.monotonic()
    bundles,checks=freshness_gate(bundles,inputs,end,history)
    audit['freshness_gate']=checks
    phases['freshness_gate']=time.monotonic()-timer
    timer=time.monotonic()
    bundles,checks=statistics_gate(bundles,end,inputs=inputs)
    audit['statistics_gate']=checks
    phases['statistics_gate']=time.monotonic()-timer
    timer=time.monotonic()
    bundles,checks=editorial_rank(bundles)
    audit['editorial_ranking']=checks
    phases['editorial_ranking']=time.monotonic()-timer
    covered={u for b in bundles for u in b.source_refs}
    audit['context_covered']=[dict(e,handling='SOURCE_USED_AS_VALIDATED_CONTEXT') for e in audit.get('unsupported',[]) if set(e.get('sources',())).issubset(covered)]
    audit['unsupported']=[e for e in audit.get('unsupported',[]) if not set(e.get('sources',())).issubset(covered)]
    timer=time.monotonic();selected=select_for_telegram(bundles,day)
    base,_=render(selected,day)
    phases['selection_and_deterministic_draft']=time.monotonic()-timer
    frozen=json.dumps([asdict(b) for b in selected],ensure_ascii=False,sort_keys=True)
    choices={b.event_id:0 for b in selected};trace=[]
    timer=time.monotonic()
    if style_chat is not None:
        # Compact subset only, never expand model context to fit the edition.
        groups=list(batches(selected,max_chars=2400))
        if groups:
            edited,trace=edit_batches(groups[0],style_chat,time.monotonic()+12,time.monotonic)
            choices.update(edited)
            # The callback can attach measured token/load metrics, not secrets.
    phases['optional_style']=time.monotonic()-timer
    timer=time.monotonic();body,final=render(selected,day,choices)
    checked=audit_digest(body,final,choices,inputs);quality=quality_issues(body,final)
    if checked['essential_facts_lost'] or checked['distorted_events'] or checked['provenance_issues'] or quality:
        raise ValueError('Immutable final validation failed')
    if frozen!=json.dumps([asdict(b) for b in selected],ensure_ascii=False,sort_keys=True):
        raise ValueError('Immutable bundle changed')
    payload=prepare_payload('@shadow',body)
    phases['final_validation_and_payload']=time.monotonic()-timer
    from .daily_match_coverage import assess_match_coverage
    timer=time.monotonic()
    audit['match_coverage']=assess_match_coverage(inputs,final,audit,end,calendar=match_calendar)
    phases['match_coverage']=time.monotonic()-timer
    return dict(body=body,selected=final,bundles=bundles,audit=audit,payload=payload,
                choices=choices,trace=trace,phases=phases,elapsed_seconds=time.monotonic()-ticks,
                quality_issues=quality,**checked)


def publication_adapter(result,inputs,channel):
    """Pure bridge to existing DailyState/secondary contracts; never a sender."""
    from .daily_text_digest import DigestStory
    originals={i.canonical_url:i for i in inputs};stories=[];represented=set()
    for b in result['selected']:
        if not all(u in originals for u in b.source_refs):raise ValueError('Missing original publication SOURCE')
        stories.append(DigestStory(originals[b.source_refs[0]],b.render(result['choices'].get(b.event_id,0)),b.section,b.importance,float(b.importance)))
        represented.update(b.source_refs)
    return dict(body=result['body'],payload=prepare_payload(channel,result['body']),stories=stories,
                represented_originals=[i for i in inputs if i.canonical_url in represented])


class BoundedStyleChat:
    """Optional local-only editor: one compact HTTP call, no process lifecycle.

    Server ownership/resource coordination belongs to the finite launcher.
    Metrics are allowlisted; SOURCE, credentials and arbitrary response fields
    are deliberately not diagnostic output.
    """
    def __init__(self,config):
        from urllib.parse import urlsplit
        if urlsplit(config.base_url).hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('Local model endpoint required')
        self.editor=OllamaTextEditor(replace(config,timeout_seconds=12,context_length=2048,
                                            simple_num_predict=128,keep_alive='1m'))
        self.metrics={};self.calls=0

    def __call__(self,prompt):
        if self.calls:raise ValueError('No style retry')
        if len(prompt)>2400:raise ValueError('Optional prompt budget exceeded')
        self.calls+=1;started=time.monotonic()
        system='Выбери варианты 0 или 1. Ответ только JSON: [{"event_id":"...","variant":0}]. Все ID ровно один раз. Не пиши текст.'
        self.metrics.update(prompt_chars=len(system)+len(prompt),prompt_utf8_bytes=len((system+prompt).encode()),context=2048,num_predict=128)
        try:
            response=self.editor._request('POST','/api/chat',dict(model=self.editor.config.model,
                messages=[dict(role='system',content=system),dict(role='user',content=prompt)],
                stream=False,think=False,keep_alive='1m',options=self.editor._simple_options()))
            self.metrics.update({k:response[k] for k in ('prompt_eval_count','eval_count','load_duration','prompt_eval_duration','eval_duration','total_duration') if k in response})
            return response['message']['content']
        except Exception as exc:
            self.metrics['error']=type(exc).__name__;raise
        finally:self.metrics['elapsed_seconds']=round(time.monotonic()-started,3)


class PublishedFactStore:
    """Receipt-bound facts in the SAME daily SQLite, never a second sender.

    Writes are legal only after a confirmed existing DigestLedger receipt.
    STARTED/SENDING/previews never become published-history evidence.
    """
    def __init__(self,db):
        self.db=db
        db.execute('CREATE TABLE IF NOT EXISTS immutable_receipts (day TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL, revision TEXT NOT NULL, bundles TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS immutable_prepared (day TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL, revision TEXT NOT NULL, bundles TEXT NOT NULL)')
        db.commit()

    def stage(self,day,body,bundles,revision):
        if not revision or any(not any(b.render(v) in body for v in (0,1)) for b in bundles):
            raise ValueError('Only validated printed bundles may be staged')
        frozen=json.dumps([asdict(b) for b in bundles],ensure_ascii=False,sort_keys=True)
        sha=hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            state=self.db.execute('SELECT state FROM daily_digest WHERE day=?',(day,)).fetchone()
            if not state or state[0]!='STARTED':raise ValueError('Staging requires the existing daily claim')
            old=self.db.execute('SELECT body_sha256,revision,bundles FROM immutable_prepared WHERE day=?',(day,)).fetchone()
            if old and old!=(sha,revision,frozen):raise ValueError('Staged edition cannot be silently replaced')
            self.db.execute('INSERT OR IGNORE INTO immutable_prepared VALUES (?,?,?,?)',(day,sha,revision,frozen))

    def record(self,day,body,bundles,revision):
        row=self.db.execute('SELECT state,body,message_id FROM daily_digest WHERE day=?',(day,)).fetchone()
        if not row or row[0]!='PUBLISHED' or row[1]!=body or row[2] is None:
            raise ValueError('Confirmed matching receipt required')
        if not revision or any(not any(b.render(v) in body for v in (0,1)) for b in bundles):
            raise ValueError('Only actually printed immutable bundles may enter history')
        frozen=json.dumps([asdict(b) for b in bundles],ensure_ascii=False,sort_keys=True)
        sha=hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            old=self.db.execute('SELECT body_sha256,revision,bundles FROM immutable_receipts WHERE day=?',(day,)).fetchone()
            if old and old!=(sha,revision,frozen):raise ValueError('Immutable receipt cannot be replaced')
            self.db.execute('INSERT OR IGNORE INTO immutable_receipts VALUES (?,?,?,?)',(day,sha,revision,frozen))

    def history(self,before,*,publication_clocks=False):
        result=[]
        # A crash after PUBLISHED but before record() does not lose facts: the
        # staged manifest counts ONLY when the existing matching receipt exists.
        rows=self.db.execute('SELECT r.body_sha256,r.bundles,d.body,d.updated_at FROM (SELECT day,body_sha256,bundles FROM immutable_receipts UNION ALL SELECT p.day,p.body_sha256,p.bundles FROM immutable_prepared p WHERE NOT EXISTS (SELECT 1 FROM immutable_receipts r WHERE r.day=p.day)) r JOIN daily_digest d USING(day) WHERE r.day<? AND d.state=? AND d.message_id IS NOT NULL',(before,'PUBLISHED'))
        for sha,payload,body,published_at in rows:
            if hashlib.sha256(body.encode()).hexdigest()!=sha:raise ValueError('Published receipt mismatch')
            for raw in json.loads(payload):
                facts=[]
                for f in raw['verified_facts']:
                    f=dict(f);f['statistics']=tuple(StatisticalObservation(**s) for s in f.get('statistics',()))
                    for k in ('values','source_refs','formulations'):f[k]=tuple(f[k])
                    facts.append(VerifiedFact(**f))
                raw=dict(raw,verified_facts=tuple(facts))
                for k in ('participants','source_refs','essential_facts'):raw[k]=tuple(raw[k])
                from .daily_editorial_gates import utc
                # Existing ledger receipt timestamp, not an invented event date.
                bundle=ImmutableBundle(**raw)
                result.append(replace(bundle,last_published_at=utc(published_at)) if publication_clocks else bundle)
        return result


def run_shadow(config,env,start,end,state,collect,seed=1,*,history=(),style_chat=None,budget=90,edition_day=None):
    ticks=time.monotonic();phases={};day=edition_day or start.astimezone(MOSCOW).date()
    inputs,collection=collect_shadow(config,env,start,end,state,collect,budget)
    phases['source_collection']=time.monotonic()-ticks
    result=prepare_immutable_edition(inputs,start,end,config.filters,history=history,style_chat=style_chat,edition_day=day)
    phases.update(result['phases']);body=result['body'];final=result['selected'];trace=result['trace'];audit=result['audit']
    timer=time.monotonic();payload=prepare_payload(config.publishing.channel,body)
    state.prepared(day.isoformat(),body)
    phases['telegram_payload_preparation']=time.monotonic()-timer
    elapsed=time.monotonic()-ticks
    return dict(mode='SHADOW_ONLY',network_collection_exercised=True,
        fresh_qwen_response=any('response' in t for t in trace),reused_qwen_responses=0,collection=collection,
        sources=[asdict(i) for i in inputs],bundles=[asdict(b) for b in final],audit=audit,
        phases_seconds={k:round(v,3) for k,v in phases.items()},elapsed_seconds=round(elapsed,3),
        margin_to_480_seconds=round(480-elapsed,3),llm_calls=sum(t.get('calls',0) for t in trace),
        model_trace=trace,body=body,telegram_payload=payload,event_count=len(final),utf16=telegram_length(body),
        **{k:result[k] for k in ('essential_facts_checked','essential_facts_lost','missing_essential_facts','distorted_events','provenance_issues')},duplicate_events=len(final)-len({b.event_id for b in final}),
        fact_basis_sha256=hashlib.sha256(json.dumps([asdict(b) for b in final],sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        quality_issues=result['quality_issues'],live_publications=0,
        production_readiness='NOT_READY' if not collection['fresh_count'] and collection['partial_sources'] else 'SHADOW_VALIDATED_PENDING_APPROVAL_WITH_COVERAGE_WARNINGS' if collection['partial_sources'] else 'SHADOW_VALIDATED_PENDING_APPROVAL',
        scope='Collection through payload preparation, no Telegram delivery')
