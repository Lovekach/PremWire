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

from .daily_fact_digest import build_bundles,render,audit_digest,edit_batches,batches
from .daily_text_digest import BatchOllama,MOSCOW,telegram_length
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


def run_shadow(config,env,start,end,state,collect,seed=1):
    ticks=time.monotonic();phases={};day=start.astimezone(MOSCOW).date()
    inputs,collection=collect_shadow(config,env,start,end,state,collect)
    phases['source_collection']=time.monotonic()-ticks
    bundles,audit=build_bundles(inputs,start,end,config.filters,phases)
    timer=time.monotonic();selected=select_for_telegram(bundles,day)
    phases['editorial_selection']=time.monotonic()-timer
    timer=time.monotonic();base,_=render(selected,day)
    phases['deterministic_draft']=time.monotonic()-timer
    frozen=json.dumps([asdict(b) for b in selected],ensure_ascii=False,sort_keys=True)
    timer=time.monotonic()
    editor=OllamaTextEditor(replace(config.editor,timeout_seconds=35,simple_num_predict=550,
                                   keep_alive='1m',simple_seed=seed))
    choices,trace=edit_once(selected,editor,ticks+360)
    phases['qwen_edit_setup_unload']=time.monotonic()-timer
    timer=time.monotonic();body,final=render(selected,day,choices)
    checked=audit_digest(body,final,choices,inputs)
    quality=quality_issues(body,final)
    if checked['essential_facts_lost'] or checked['distorted_events'] or checked['provenance_issues']:
        raise ValueError('Factual validation failed: no payload may be prepared')
    if quality:raise ValueError('Editorial validation failed: no payload may be prepared')
    if frozen!=json.dumps([asdict(b) for b in selected],ensure_ascii=False,sort_keys=True):
        raise ValueError('Model mutated bundle')
    phases['factual_validation']=time.monotonic()-timer
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
        **checked,duplicate_events=len(final)-len({b.event_id for b in final}),
        fact_basis_sha256=hashlib.sha256(frozen.encode()).hexdigest(),
        quality_issues=quality,live_publications=0,
        production_readiness='NOT_READY' if collection['partial_sources'] or audit['unsupported'] else 'SHADOW_VALIDATED_PENDING_APPROVAL',
        scope='Collection through payload preparation, no Telegram delivery')
