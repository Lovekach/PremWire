"""Persistence and platform helpers for the existing daily text newsroom."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .config import load_config
from .env import load_env
from .models import IncomingItem
from .process_lock import ProcessLock
from .storage import Storage


def source_key(item):
    from .daily_quality import QUALITY_REVISION
    # An edit invalidates the rewrite. URLs alone are not immutable source facts.
    return hashlib.sha256((QUALITY_REVISION + json.dumps(asdict(item), sort_keys=True, default=str,
                                    ensure_ascii=False)).encode()).hexdigest()


def confirmed_published_history(bundles, published_stories):
    """Opt-in immutable shadow history, based on ACTUALLY printed facts.

    A selected SOURCE is not proof every detail was published. Legacy free-form
    editions have no fact ledger: accept exact approved wording or a narrow,
    explicit points/rounds relation, otherwise keep the evidence unresolved.
    New immutable editions should persist their validated fact signatures.
    This function is not called by the current live daily route.
    """
    from .daily_quality import clubs
    history=[];review=[]
    numeral_forms={'шесть':'6','шести':'6','шестью':'6','сорок':'40','сорока':'40'}
    for bundle in bundles:
        texts=[row['text'] for row in published_stories
               if row['source']['canonical_url'] in bundle.source_refs]
        verified=[]
        for fact in bundle.verified_facts:
            proven=any(wording in text for text in texts for wording in fact.formulations)
            for text in texts:
                normalized=text.casefold()
                for word,value in numeral_forms.items():
                    normalized=re.sub(r'\b'+word+r'\b',value,normalized)
                if fact.key=='points' and len(fact.values)==3:
                    club,points,games=fact.values
                    if club in clubs(text) and re.search(
                        rf'набрал\w*\s+{re.escape(points)}\s+очк\w*\s+в\s+(?:первых\s+)?{re.escape(games)}\s+(?:матч\w*|тур\w*)',normalized):
                        proven=True
                if fact.key=='record' and len(fact.values)==1:
                    if set(bundle.participants)&clubs(text) and re.search(
                        rf'худш\w*\s+старт.*?{re.escape(fact.values[0])}\s+лет',normalized):
                        proven=True
            if proven:verified.append(fact)
            review.append(dict(event_id=bundle.event_id,fact=fact.key,published_evidence=proven))
        if verified:
            keys={f.key for f in verified}
            history.append(replace(bundle,verified_facts=tuple(verified),
                                   essential_facts=tuple(k for k in bundle.essential_facts if k in keys)))
    return history,review


def merge_source_context(winner, group):
    """Retain explicit supplementary terms, never earlier status/score rumours."""
    fields = (r'fee|million|млн|миллион|сумм\w*|стоимост\w*',
              r'contract.{0,30}(?:until|through)|контракт.{0,30}(?:до\s+20|лет|год)',
              r'loan.{0,40}(?:option|obligation)|аренд.{0,40}(?:опци|выкуп)')
    details = []
    for pattern in fields:
        if re.search(pattern, winner.text, re.I):
            continue
        for previous in sorted(group, key=lambda i: i.published_at, reverse=True):
            if previous is winner:
                continue
            for sentence in re.split(r'(?<=[.!?])\s+|\n+', previous.text):
                if (re.search(pattern, sentence, re.I) and re.search(r'\d|option|obligation|опци|выкуп', sentence, re.I)
                    and not re.search(r'interest|talks|bid|offer|agreed|official|interested|соглас|переговор|интерес|предлож|подпис|\d\s*[-:]\s*\d', sentence, re.I)):
                    details.append(sentence.strip())
                    break
            else:
                continue
            break
    if details:
        return replace(winner, text=winner.text + '\n' + '\n'.join(dict.fromkeys(details)))
    return winner


class DailyState:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS daily_sources (
                source_id TEXT, external_id TEXT, payload TEXT NOT NULL,
                included_day TEXT, PRIMARY KEY(source_id,external_id));
            CREATE TABLE IF NOT EXISTS daily_rewrites (
                source_hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_editions (
                day TEXT PRIMARY KEY, full_body TEXT NOT NULL,
                artifact TEXT NOT NULL, members TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_deliveries (
                day TEXT, platform TEXT, state TEXT NOT NULL, remote_id TEXT,
                PRIMARY KEY(day,platform));
        ''')
        self.db.commit()

    def accumulate(self, items):
        from datetime import timezone
        # Collection time is observation time, never SOURCE/event time. Preserve
        # the first known collection; legacy payloads remain readable unchanged.
        observed = datetime.now(timezone.utc).isoformat()
        stamped = []
        for item in items:
            row = self.db.execute('SELECT payload FROM daily_sources WHERE source_id=? AND external_id=?',
                                  (item.source_id, item.external_id)).fetchone()
            old = json.loads(row[0]) if row else {}
            stamped.append(replace(item, source_collected_at=observed,
                                   first_seen_at=old.get('first_seen_at') or item.first_seen_at or observed,
                                   last_seen_at=observed))
        with self.db:
            self.db.executemany('INSERT INTO daily_sources(source_id,external_id,payload) VALUES(?,?,?) '
                'ON CONFLICT(source_id,external_id) DO UPDATE SET payload=excluded.payload',
                [(i.source_id, i.external_id, json.dumps(asdict(i), ensure_ascii=False, default=str)) for i in stamped])

    def items(self):
        result = []
        for (payload,) in self.db.execute('SELECT payload FROM daily_sources WHERE included_day IS NULL'):
            value = json.loads(payload)
            value['published_at'] = datetime.fromisoformat(value['published_at'])
            result.append(IncomingItem(**value))
        return result

    def cached(self, item):
        from .daily_text_digest import DigestStory, digest_factual_issues
        row = self.db.execute('SELECT payload FROM daily_rewrites WHERE source_hash=?', (source_key(item),)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        # Cached text goes through today's factual safeguards too.
        if digest_factual_issues(item.text, value['text']):
            return None
        return DigestStory(item, value['text'], value['section'], value['importance'], value['priority'])

    def all_cached(self, items):
        return all(self.cached(item) is not None for item in items)

    def save_story(self, story):
        value = asdict(story)
        value.pop('source')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO daily_rewrites VALUES(?,?)',
                            (source_key(story.source), json.dumps(value, ensure_ascii=False)))

    def freeze(self, day, full_body, artifact, stories, items, *, exact_members=False):
        members = ([(i.source_id, i.external_id) for i in items] if exact_members
                   else self.members(stories, items))
        with self.db:
            previous = self.db.execute('SELECT full_body,members FROM daily_editions WHERE day=?',
                                       (day.isoformat(),)).fetchone()
            if previous:
                if previous != (full_body, json.dumps(members)):
                    raise sqlite3.IntegrityError('Frozen edition cannot be silently replaced')
                return
            self.db.execute('INSERT INTO daily_editions VALUES(?,?,?,?)',
                            (day.isoformat(), full_body, str(artifact), json.dumps(members)))

    @staticmethod
    def members(stories, items):
        from .daily_text_digest import same_event
        return [(i.source_id, i.external_id) for i in items
                if any((i.source_id, i.external_id) == (s.source.source_id, s.source.external_id)
                       or same_event(i, s.source) for s in stories)]

    def mark_included(self, day, stories, items, *, exact_members=False):
        with self.db:
            self.db.executemany('UPDATE daily_sources SET included_day=? WHERE source_id=? AND external_id=?',
                [(day.isoformat(), *key) for key in
                 ([(i.source_id, i.external_id) for i in items] if exact_members
                  else self.members(stories, items))])

    def delivery_claim(self, day, platform):
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO daily_deliveries VALUES(?,?,?,NULL)',
                                    (day.isoformat(), platform, 'SENDING'))
            return cursor.rowcount == 1

    def delivery_result(self, day, platform, state, remote=None):
        with self.db:
            self.db.execute('UPDATE daily_deliveries SET state=?,remote_id=? WHERE day=? AND platform=?',
                            (state, remote, day.isoformat(), platform))


def accumulated_items(state, config, start, end):
    from .daily_text_digest import cached_items, suppression_matches, suppression_texts
    suppressed = suppression_texts(config)
    # Journal contains the most recent scan, including evening backfill edits.
    combined = {(i.source_id, i.external_id): i for i in cached_items(config)}
    combined.update({(i.source_id, i.external_id): i for i in state.items()})
    included = set(state.db.execute('SELECT source_id,external_id FROM daily_sources WHERE included_day IS NOT NULL'))
    return [i for key, i in combined.items() if key not in included and start <= i.published_at <= end
            and not any(suppression_matches(i, old) for old in suppressed)]


def cached_rewrite(state, editor, items, deadline, rewrite):
    stories, rejected = [], []
    trace = []
    if editor is not None:
        editor.daily_trace = []
    for item in items:
        previous = state.cached(item)
        if previous:
            stories.append(previous)
            trace.append({'url':item.canonical_url, 'cache_hit':True, 'issues':[]})
            continue
        if editor is None or time.monotonic() >= deadline:
            rejected.append({'url': item.canonical_url, 'reason': 'EDITORIAL_BUDGET_EXPIRED'})
            continue
        fresh, failures = rewrite(editor, [item], deadline)
        trace.extend(dict(entry, url=item.canonical_url) for entry in editor.daily_trace)
        rejected.extend(failures)
        for story in fresh:
            state.save_story(story)
            stories.append(story)
    if editor is not None:
        editor.daily_trace = trace
    return stories, rejected


def adapt_telegram(stories, day, render):
    # Retain complete, source-checked sentences. Never cut a condition halfway.
    body, selected = render(stories, day)
    compact = []
    for story in stories:
        sentences = re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«])', story.text)
        first = sentences[0]
        from .daily_text_digest import digest_factual_issues
        text = first if not digest_factual_issues(story.source.text, first) else story.text
        compact.append(replace(story, text=text))
    short_body, short_selected = render(compact, day)
    # Prefer rich paragraphs when they cover the same number of events.
    return (short_body, short_selected) if len(short_selected) > len(selected) else (body, selected)


def collect_light(root):
    from .text_production import ingest_poll
    from .daily_text_digest import daily_mode, owner_paused
    if not daily_mode(root):
        raise RuntimeError('DAILY_MODE_REQUIRED')
    lock = ProcessLock(root / 'data/daily-collection.lock')
    if not lock.acquire():
        return 0
    state = None
    try:
        config = load_config(root)
        if not config.production.enabled:
            return 0
        storage = Storage(config.storage.database)
        storage.initialise()
        if owner_paused(config):
            print('Daily collection paused by owner', flush=True)
            return 0
        state = DailyState(root / 'data/daily-text-digest.sqlite3')
        env = load_env(root / '.env')
        # Two source polls retain the configured 30-second cadence in finite
        # minute jobs. Slow feeds may make Task Scheduler skip an overlap.
        for poll in range(2):
            started = time.monotonic()
            outcome = ingest_poll(config, env, storage, observer=state.accumulate)
            print(f'Daily collection: collected={outcome.collected}, new={outcome.new}, '
                  f'updated={outcome.updated}, warnings={len(outcome.warnings)}; no LLM/no publication', flush=True)
            if poll == 0:
                time.sleep(max(0, config.collection.poll_interval_seconds - (time.monotonic() - started)))
        return 0
    finally:
        if state:
            state.db.close()
        lock.release()


def x_digest(stories, day, limit):
    from .daily_text_digest import digest_factual_issues
    title = f'Итоги дня {day:%d.%m}\n'
    parts = []
    for story in sorted(stories, key=lambda s: (-s.priority, -s.importance)):
        first = re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z«])', story.text)[0]
        if digest_factual_issues(story.source.text, first):
            first = story.text
        candidate = title + '\n'.join([*parts, '• ' + first])
        # Twitter's weighted limit is conservatively bounded here; Russian and
        # Latin BMP characters count as one, astral characters as two.
        if len(candidate.encode('utf-16-le')) // 2 <= limit:
            parts.append('• ' + first)
    return title + '\n'.join(parts) if parts else ''


def publish_secondary(state, config, day, full_body, stories):
    from .daily_text_digest import MOSCOW, publication_window, owner_paused
    result = {}
    for platform in ('X', 'Dzen'):
        enabled = config.x_publishing.enabled if platform == 'X' else config.dzen_digest.enabled
        if not enabled:
            result[platform] = 'DISABLED'
            continue
        if not publication_window(datetime.now(MOSCOW)) or owner_paused(config):
            result[platform] = 'WINDOW_EXPIRED_OR_PAUSED'
            continue
        body = x_digest(stories, day, config.x_publishing.max_chars) if platform == 'X' else full_body
        if not body:
            result[platform] = 'NO_COMPLETE_STORY_FITS'
            continue
        if not state.delivery_claim(day, platform):
            result[platform] = 'ALREADY_HANDLED_OR_AMBIGUOUS'
            continue
        try:
            if platform == 'X':
                from .x_browser import XBrowserSession
                with XBrowserSession(config.x_publishing.profile_directory,
                                     headless=config.x_publishing.headless) as browser:
                    remote = browser.post(body)
            else:
                from .dzen_digest_service import _default_browser_factory
                from .dzen_publisher import DzenArticleDraft, publish_or_reconcile_dzen_article
                from .dzen_lifecycle import DZEN_LOCK_PATH
                lock = ProcessLock(DZEN_LOCK_PATH)
                if not lock.acquire():
                    raise RuntimeError('DZEN_BUSY')
                try:
                    with _default_browser_factory(config)() as browser:
                        published = publish_or_reconcile_dzen_article(browser,
                            DzenArticleDraft(day, f'Итоги футбольного дня — {day:%d.%m.%Y}', body))
                        remote = published.article_url
                finally:
                    lock.release()
            state.delivery_result(day, platform, 'PUBLISHED', str(remote))
            result[platform] = 'PUBLISHED'
            print(f'{platform} published', flush=True)
        except Exception as exc:
            # An uncertain send must never trigger a second post. No raw
            # browser/request exceptions (cookies/tokens/URLs) in logs.
            state.delivery_result(day, platform, 'FAILED_OR_AMBIGUOUS', type(exc).__name__)
            result[platform] = type(exc).__name__
            print(f'{platform} not confirmed: {type(exc).__name__}', flush=True)
    return result
