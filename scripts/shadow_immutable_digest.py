"""Three independent fresh local-Qwen runs, saved SOURCES only. No live IO."""
import argparse
from dataclasses import asdict,replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
import os
import subprocess

from premwire.config import load_config
from premwire.models import IncomingItem
from premwire.daily_fact_digest import build_bundles, edit_batches, render, validate_edit, audit_digest
from premwire.daily_text_digest import telegram_length, MOSCOW
from premwire.text_editor import OllamaTextEditor
from premwire.daily_strategy import confirmed_published_history
from premwire.daily_fact_e2e import quality_issues,prepare_payload,prepare_immutable_edition,PublishedFactStore,ShadowState,collect_shadow,BoundedStyleChat


def route_replay(root,config,runs):
    """Replay the existing ledger/sender contract with an in-memory fake sender.

    No production DB, credentials, network or actual Telegram class is opened.
    """
    from premwire.daily_text_digest import DigestLedger,publish_prepared
    output=root/'artifacts/daily-route-shadow'/datetime.now().strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    revision=hashlib.sha256(b''.join((root/p).read_bytes() for p in (
        'src/premwire/daily_fact_digest.py','src/premwire/daily_fact_patterns.py',
        'src/premwire/daily_fact_e2e.py'))).hexdigest()
    for run in range(1,runs+1):
        folder=output/str(run);folder.mkdir()
        state=ShadowState(folder/'immutable-shadow.sqlite3')
        ledger=DigestLedger(folder/'immutable-shadow.sqlite3');store=PublishedFactStore(ledger.db)
        try:
            for name in ('20261011-205600-live','20261012-205602-live','20261013-205602-live'):
                ticks=time.monotonic();snapshot=root/'artifacts/daily-text-digest'/name
                window=json.loads((snapshot/'report.json').read_text(encoding='utf-8'))
                start,end=map(datetime.fromisoformat,(window['window_start'],window['window_end']))
                raw=json.loads((snapshot/'sources.json').read_text(encoding='utf-8'))
                items=[IncomingItem(**dict(r,published_at=datetime.fromisoformat(r['published_at']))) for r in raw]
                state.store(items,'SAVED_SOURCE_NOT_NETWORK')
                def unavailable(*a,**k):raise ConnectionError('simulated source outage')
                # Exercise the real same-day source fallback with saved originals.
                inputs,collection=collect_shadow(config,{},start,end,state,unavailable)
                ingestion=time.monotonic()-ticks
                day=start.astimezone(MOSCOW).date();stamp=day.isoformat()
                result=prepare_immutable_edition(inputs,start,end,config.filters,history=store.history(stamp))
                before=time.monotonic()
                if not ledger.claim(stamp):raise RuntimeError('Shadow day already handled')
                class OfflineReceipt:
                    calls=0
                    def send_text(self,channel,text):
                        self.calls+=1
                        if channel!='@shadow' or text!=result['payload']['text']:raise ValueError('Transport mismatch')
                        return self.calls
                api=OfflineReceipt()
                now=datetime.combine(day,datetime.strptime('20:57','%H:%M').time(),MOSCOW)
                if result['body']:
                    store.stage(stamp,result['body'],result['selected'],revision)
                    publish_prepared(ledger,day,result['body'],api,'@shadow',now=now)
                    store.record(stamp,result['body'],result['selected'],revision)
                else:ledger.state(stamp,'NO_NEWS')
                duplicate_blocked=not ledger.claim(stamp)
                if not duplicate_blocked or api.calls>1:raise AssertionError('Idempotency failed')
                result['phases']['sqlite_receipt_and_idempotency']=time.monotonic()-before
                result['phases']['saved_ingestion_outage_fallback']=ingestion
                result.update(mode='SHADOW_ONLY',run=run,day=stamp,source_count=len(inputs),
                    collection=collection,elapsed_seconds=round(time.monotonic()-ticks,3),
                    events=len(result['selected']),utf16=telegram_length(result['body']),
                    duplicate_blocked=duplicate_blocked,llm_calls=0,live_sends=0,
                    scope='SAVED SOURCE TO SIMULATED RECEIPT; NOT NETWORK SLA')
                (folder/(stamp+'.json')).write_text(json.dumps(result,default=lambda o:asdict(o),ensure_ascii=False,indent=2),encoding='utf-8')
                (folder/(stamp+'.txt')).write_text(result['body'],encoding='utf-8')
                compact={k:result[k] for k in ('run','day','elapsed_seconds','events','utf16','essential_facts_lost','distorted_events','llm_calls','duplicate_blocked')}
                rows.append(compact);print(json.dumps(compact),flush=True)
        finally:ledger.db.close();state.close()
    (output/'summary.json').write_text(json.dumps(dict(runs=rows,code_revision=revision,
        settings_sha256=hashlib.sha256((root/'config/settings.json').read_bytes()).hexdigest()),indent=2),encoding='utf-8')
    print('ROUTE_ARTIFACTS: '+str(output),flush=True)


class IsolatedOllama:
    """Own a finite hidden server on a separate port; never unload live Ollama."""
    def __init__(self,editor,log_path=None):self.editor=editor;self.process=None;self.job=None;self.log_path=log_path;self.log=None
    def __enter__(self):
        if os.name=='nt':
            # Fail closed if another backend exists or inventory is unavailable.
            system32=Path(os.environ.get('WINDIR',r'C:\Windows'))/'System32'
            inventory=subprocess.run([str(system32/'WindowsPowerShell/v1.0/powershell.exe'),'-NoProfile','-NonInteractive','-Command',
                "@(Get-Process | Where-Object { $_.ProcessName -in @('ollama','llama-server') }).Count"],
                capture_output=True,text=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW)
            if inventory.returncode or inventory.stdout.strip()!='0':
                raise RuntimeError('MODEL_RESOURCE_BUSY_OR_UNKNOWN')
            vram=subprocess.run([str(system32/'nvidia-smi.exe'),
                '--query-gpu=memory.free','--format=csv,noheader,nounits'],
                capture_output=True,text=True,timeout=3,creationflags=subprocess.CREATE_NO_WINDOW)
            if vram.returncode or not vram.stdout.strip().isdigit() or int(vram.stdout.strip())<7000:
                raise RuntimeError('MODEL_RESOURCE_BUSY_OR_UNKNOWN')
        # Refuse an occupied endpoint, rather than taking ownership of its model.
        try:self.editor.available_models()
        except Exception:pass
        else:raise RuntimeError('Shadow port already occupied; refusing to touch it')
        executable=Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe'
        env=dict(os.environ,OLLAMA_HOST='127.0.0.1:11439')
        if not executable.is_file():raise FileNotFoundError('OLLAMA_EXECUTABLE_UNAVAILABLE')
        if self.log_path:self.log=open(self.log_path,'wb')
        self.process=subprocess.Popen([str(executable),'serve'],env=env,
            stdout=self.log or subprocess.DEVNULL,stderr=self.log or subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            if os.name=='nt':
                from premwire_supervisor import create_kill_on_close_job,assign_to_job
                kernel,handle=create_kill_on_close_job();self.job=(kernel,handle)
                assign_to_job(kernel,handle,self.process)
            for _ in range(40):
                if self.process.poll() is not None:raise RuntimeError('Isolated Ollama exited')
                try:
                    ready,_=self.editor.verify()
                    if ready:return self.editor
                except Exception:pass
                time.sleep(.25)
            raise RuntimeError('Isolated Ollama startup timeout')
        except Exception:
            self.__exit__();raise
    def __exit__(self,*_):
        try:
            if self.process is not None and self.process.poll() is None:
                # Terminate only the owned job, even when generation timed out.
                # A second unload request previously added five seconds and
                # could wait behind an unfinished generation.
                self.process.terminate();self.process.wait(timeout=3)
        finally:
            if self.job:
                kernel,handle=self.job;kernel.CloseHandle(handle);self.job=None
            if self.log:self.log.close();self.log=None


def network_shadow(root,config,args):
    from datetime import date,timedelta,time as daytime
    from premwire.env import load_env
    from premwire.daily_collection import collect_day
    from premwire.daily_fact_e2e import run_shadow
    # Env stays in memory: no credentials go into SOURCE/diagnostic JSON.
    env=load_env(root/'.env');now=datetime.now(MOSCOW);history=[];history_ticks=time.monotonic()
    for previous in args.history:
        past=json.loads((previous/'report.json').read_text(encoding='utf-8'))
        if past.get('mode')!='LIVE':raise ValueError('Only previously published saved history')
        original=json.loads((previous/'sources.json').read_text(encoding='utf-8'))
        published={r['source']['canonical_url'] for r in past['selected_sources']}
        historical=[IncomingItem(**dict(r,published_at=datetime.fromisoformat(r['published_at']))) for r in original if r['canonical_url'] in published]
        lower,upper=map(datetime.fromisoformat,(past['window_start'],past['window_end']))
        bundles,_=build_bundles(historical,lower,upper,config.filters)
        proved,_=confirmed_published_history(bundles,past['selected_sources']);history.extend(proved)
    history_seconds=time.monotonic()-history_ticks
    day=date.fromisoformat(args.network_date) if args.network_date else now.date()
    start=datetime.combine(day,daytime.min,MOSCOW);end=min(start+timedelta(days=1),now)
    if args.previous_cutoff:
        cutoff=datetime.fromisoformat(args.previous_cutoff)
        if cutoff.tzinfo is None or not start-timedelta(days=1)<=cutoff<=start:
            raise ValueError('Previous confirmed cutoff must precede this edition day')
        start=cutoff
    if end<=start:raise ValueError('Future source window')
    if any(datetime.fromisoformat(json.loads((p/'report.json').read_text(encoding='utf-8'))['window_end'])>start for p in args.history):
        raise ValueError('Published history must precede network source day')
    output=root/'artifacts/daily-network-shadow'/now.strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for run in range(1,args.runs+1):
        folder=output/str(run);folder.mkdir();state=ShadowState(folder/'immutable-shadow.sqlite3')
        bounded=BoundedStyleChat(replace(config.editor,base_url='http://127.0.0.1:11439'))
        def style(prompt):
            with IsolatedOllama(bounded.editor,folder/'ollama.log'):return bounded(prompt)
        try:
            print(f'NETWORK_SHADOW {run}: {day}; model={not args.offline}',flush=True)
            result=run_shadow(config,env,start,end,state,
                lambda *a,**k:collect_day(*a,**k,bounded=True),
                style_chat=None if args.offline else style,budget=args.source_budget,history=history,edition_day=day)
            result.update(window_start=start.isoformat(),window_end=end.isoformat(),run=run,
                actual_llm_calls=bounded.calls,model_metrics=bounded.metrics,
                source_budget_seconds=args.source_budget,seeded_source_cache=False,history_setup_seconds=round(history_seconds,3))
            (folder/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            (folder/'preview.txt').write_text(result['body'],encoding='utf-8')
            row={k:result[k] for k in ('run','elapsed_seconds','event_count','utf16','actual_llm_calls','essential_facts_checked','essential_facts_lost','distorted_events')}
            row.update(fresh_sources=len(result['sources']),available_feeds=sum(c['count']>0 for c in result['collection']['coverage']),warnings=len(result['collection']['warnings']))
            rows.append(row);print(json.dumps(row),flush=True)
        finally:state.close()
    (output/'summary.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    print('NETWORK_ARTIFACTS: '+str(output),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runs',type=int,default=3)
    parser.add_argument('--saved',type=Path,help='Saved SOURCE snapshot directory; no live collection')
    parser.add_argument('--offline',action='store_true',help='Deterministic regression replay, no model')
    parser.add_argument('--route-replay',action='store_true',help='Saved 11/12/13 October through isolated SQLite and fake receipt; never live')
    parser.add_argument('--network',action='store_true',help='Read real feeds to isolated shadow DB/payload, NEVER send')
    parser.add_argument('--network-date',help='Source Moscow date; default today')
    parser.add_argument('--previous-cutoff',help='Confirmed preceding edition SOURCE cutoff; includes unprinted late-night posts')
    parser.add_argument('--source-budget',type=int,default=90)
    parser.add_argument('--model-timeout',type=int,default=12,help='Deprecated compatibility flag; optional edit capped at 12s locally')
    parser.add_argument('--history',type=Path,action='append',default=[],help='Previously published saved snapshot, never live SQLite')
    args=parser.parse_args()
    if not 1<=args.runs<=3:parser.error('Use one to three fresh runs')
    if not 5<=args.model_timeout<=90:parser.error('Shadow timeout must be 5..90 seconds')
    root=Path(__file__).resolve().parents[1]
    saved=args.saved or root/'artifacts/daily-text-digest/20261011-205600-live'
    config=load_config(root)
    if args.network:
        if args.route_replay or args.saved:parser.error('Network mode cannot mix saved SOURCE or replay')
        if not 15<=args.source_budget<=110:parser.error('Source budget must be 15..110s')
        network_shadow(root,config,args);return
    if args.route_replay:
        if not args.offline:parser.error('Route replay requires --offline; no model or live network')
        route_replay(root,config,args.runs);return
    history_ticks=time.monotonic()
    history=[];history_evidence=[]
    for previous in args.history:
        past=json.loads((previous/'report.json').read_text(encoding='utf-8'))
        if past.get('mode')!='LIVE':raise ValueError('History must be an actual LIVE snapshot')
        published={r['source']['canonical_url'] for r in past['selected_sources']}
        original=json.loads((previous/'sources.json').read_text(encoding='utf-8'))
        historical=[IncomingItem(**dict(r,published_at=datetime.fromisoformat(r['published_at'])))
                    for r in original if r['canonical_url'] in published]
        lower,upper=map(datetime.fromisoformat,(past['window_start'],past['window_end']))
        recovered,_=build_bundles(historical,lower,upper,config.filters)
        proved,evidence=confirmed_published_history(recovered,past['selected_sources'])
        history.extend(proved);history_evidence.extend(evidence)
    history_seconds=round(time.monotonic()-history_ticks,3)
    output=root/'artifacts/daily-immutable-shadow'/datetime.now().strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True,exist_ok=True)
    results=[]
    for number in range(1,args.runs+1):
        ticks=time.monotonic(); phases={}
        raw=json.loads((saved/'sources.json').read_text(encoding='utf-8'))
        window=json.loads((saved/'report.json').read_text(encoding='utf-8'))
        start,end=map(datetime.fromisoformat,(window['window_start'],window['window_end']))
        items=[IncomingItem(**dict(r,published_at=datetime.fromisoformat(r['published_at']))) for r in raw]
        phases['saved_input_load']=round(time.monotonic()-ticks,3)
        processing=time.monotonic()
        if any(b for b in history) and any(datetime.fromisoformat(json.loads((p/'report.json').read_text(encoding='utf-8'))['window_end'])>=start for p in args.history):
            raise ValueError('History must precede the replay day')
        stage_timings={}
        bundles,audit=build_bundles(items,start,end,config.filters,history=history,timings=stage_timings)
        phases.update({k:round(v,3) for k,v in stage_timings.items()})
        phases['processing']=round(time.monotonic()-processing,3)
        # Fresh model requests every run; only saved SOURCE is reused.
        editor=OllamaTextEditor(replace(config.editor,base_url='http://127.0.0.1:11439',timeout_seconds=args.model_timeout,simple_num_predict=550,
                                       keep_alive='1m',simple_seed=100+number))
        bounded=BoundedStyleChat(editor.config);editor=bounded.editor
        system=('Ты редактор русского футбольного дайджеста. Для каждого event_id выбери вариант 0 или 1 '
                'с лучшей читаемостью и меньшими стилистическими повторами между пунктами. '
                'Оба варианта уже проверены кодом. Не переписывай текст и не меняй факты. '
                'Ответ — только JSON-массив объектов {"event_id":"...","variant":0}. '
                'Сохрани каждый ID ровно один раз, в исходном порядке; без иных полей.')
        def chat(prompt):
            return bounded(prompt)
        generation=time.monotonic()
        print(f'FRESH_SHADOW_RUN {number}: {len(bundles)} bundles',flush=True)
        if args.offline:
            choices={b.event_id:0 for b in bundles};trace=[]
        else:
            try:
                with IsolatedOllama(editor,output/f'ollama-{number}.log'):
                    from premwire.daily_fact_digest import batches
                    groups=list(batches(bundles,max_chars=2400))
                    choices={b.event_id:0 for b in bundles};trace=[]
                    if groups:
                        edited,trace=edit_batches(groups[0],chat,time.monotonic()+12,time.monotonic)
                        choices.update(edited)
            except Exception as exc:
                choices={b.event_id:0 for b in bundles}
                trace=[dict(status='OLLAMA_UNAVAILABLE_FALLBACK',issues=[type(exc).__name__],calls=0)]
        phases['model_start_edit_unload']=round(time.monotonic()-generation,3)
        validation=time.monotonic()
        body,selected=render(bundles,start.astimezone(MOSCOW).date(),choices)
        issues=[{'event_id':b.event_id,'issues':validate_edit(b,b.render(choices[b.event_id]))} for b in selected]
        issues=[i for i in issues if i['issues']]
        factual_audit=audit_digest(body,selected,choices,items)
        edition_issues=quality_issues(body,selected)
        if edition_issues or issues or factual_audit['essential_facts_lost'] or factual_audit['distorted_events'] or factual_audit['provenance_issues']:
            raise ValueError('Final shadow digest gate failed; no payload prepared')
        # Offline Telegram envelope check, not a network send or live ledger.
        envelope=prepare_payload(config.publishing.channel,body)
        json.dumps(envelope,ensure_ascii=False)
        if telegram_length(body)>4096:raise AssertionError('Telegram limit')
        phases['validation_telegram_envelope']=round(time.monotonic()-validation,3)
        basis=json.dumps([asdict(b) for b in bundles],sort_keys=True,ensure_ascii=False)
        row=dict(mode='SHADOW_ONLY',fresh=not args.offline,reused_qwen_responses=0,run=number,raw_count=len(items),
                 source_snapshot=str(saved),day=window['day'],
                 model_timeout_seconds=12,model_metrics=bounded.metrics,quality_issues=edition_issues,
                 history_snapshots=[str(p) for p in args.history],
                 phases=phases,elapsed_seconds=round(time.monotonic()-ticks,3),
                 history_preparation_seconds=history_seconds,
                 history_evidence=history_evidence,
                 llm_calls=sum(t.get('calls',0) for t in trace),trace=trace,choices=choices,
                 bundles=[asdict(b) for b in bundles],audit=audit,body=body,
                 selected_ids=[b.event_id for b in selected],event_count=len(selected),utf16=telegram_length(body),
                 fact_basis_sha256=hashlib.sha256(basis.encode()).hexdigest(),final_issues=issues,
                 **factual_audit,duplicate_events=len(selected)-len({b.event_id for b in selected}),
                 note='Collection replayed from saved SOURCE; Telegram envelope offline. Not live end-to-end latency.')
        (output/f'run-{number}.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/f'digest-{number}.txt').write_text(body,encoding='utf-8')
        results.append({k:row[k] for k in ('run','elapsed_seconds','llm_calls','event_count','utf16','fact_basis_sha256','essential_facts_lost','duplicate_events')})
        print(json.dumps(results[-1]),flush=True)
    summary=dict(mode='SHADOW_ONLY',runs=results,identical_fact_basis=len({r['fact_basis_sha256'] for r in results})==1,
                 settings_sha256=hashlib.sha256((root/'config/settings.json').read_bytes()).hexdigest())
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SHADOW_ARTIFACTS: '+str(output),flush=True)


if __name__=='__main__':main()
