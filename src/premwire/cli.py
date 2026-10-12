"""Public, finite CLI. No autostart, browser login or resident worker commands."""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

from .config import load_config

def main() -> int:
    parser = argparse.ArgumentParser(description="PremWire text-only development CLI")
    parser.add_argument('--root', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Inspect safe configuration; no credentials/network reads')
    commands.add_parser('demo', help='Offline synthetic-source selection and deduplication')
    daily = commands.add_parser('daily-digest', help='Finite SHADOW batch; no publication')
    daily.add_argument('--shadow-input', type=Path)
    daily.add_argument('--shadow-date')
    args = parser.parse_args()
    root = args.root.resolve()
    config = load_config(root)
    if args.command == 'status':
        print(json.dumps({'dry_run': config.publishing.dry_run,
                          'production_enabled': config.production.enabled,
                          'mode': config.production.mode,
                          'enabled_sources': sum(s.enabled for s in config.sources)}, indent=2))
        return 0
    if args.command == 'demo':
        from datetime import datetime
        from .daily_text_digest import MOSCOW, collapse, filter_items
        from .models import IncomingItem
        now = datetime.now(MOSCOW)
        story = 'Liverpool have officially signed Example Player from Everton.'
        inputs = [IncomingItem('example', str(i), story,
                              f'https://example.test/story/{i}', now) for i in (1, 2)]
        accepted, rejected = filter_items(inputs, now.replace(hour=0, minute=0, second=0, microsecond=0), now, (), config.filters)
        selected, conflicts = collapse(accepted)
        print(json.dumps({'synthetic_example': True, 'inputs': len(inputs),
                          'selected': len(selected), 'rejected': len(rejected),
                          'conflicts': len(conflicts), 'live_publications': 0}, indent=2))
        return 0
    # Explicitly opt-in invocation only; imports do not start a model or collector.
    # Public CLI has no --confirm: reviewed operators may integrate the library
    # sender separately, after provisioning their own credentials and controls.
    for variable, field in [('OLLAMA_BASE_URL', 'base_url'), ('OLLAMA_MODEL', 'model')]:
        value = os.environ.get(variable, '').strip()
        if value:
            config = replace(config, editor=replace(config.editor, **{field: value}))
    from .daily_text_digest import run_daily
    try:
        return run_daily(root, confirm=False, config_override=config,
                         shadow_input=str(args.shadow_input) if args.shadow_input else None,
                         shadow_date=args.shadow_date)
    except Exception as exc:
        print('SHADOW_FAILED: ' + type(exc).__name__)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
