"""Read JSONL trace and rotated siblings. Never imports the bot or opens SQLite."""
import argparse
from collections import deque
import json
from pathlib import Path
import sys


def read_events(path, *, trace=None, flow=None, user=None, event=None, last=100):
    paths = [p for p in path.parent.glob(path.name + '.*') if p.name.removeprefix(path.name + '.').isdigit()]
    paths.sort(key=lambda p: int(p.name.removeprefix(path.name + '.')), reverse=True)
    paths.append(path)
    selected = deque(maxlen=max(1, min(last, 10000)))
    invalid = 0
    for filename in paths:
        if not filename.exists():
            continue
        with filename.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError('not an object')
                except (ValueError, TypeError):
                    invalid += 1
                    continue
                if trace and item.get('trace_id') != trace: continue
                if flow and item.get('flow') != flow: continue
                if user and item.get('user_id_hash') != user: continue
                if event and item.get('event') != event: continue
                selected.append(item)
    return sorted(selected, key=lambda item: item.get('ts', '')), invalid


def summary(events):
    traces = {}
    for item in events:
        traces.setdefault(item.get('trace_id', '?'), []).append(item)
    lines = []
    for trace_id, items in traces.items():
        first = items[0]
        lines.append(f"{first.get('flow', '?').upper()} {trace_id}")
        lines.append('query: ' + str(first.get('query', '')))
        for item in items:
            event = item.get('event', '')
            if event == 'filter.summary':
                fields = ('pid', 'received', 'invalid_id', 'used_in_snapshot', 'dedup', 'blacklist', 'rating', 'media_type', 'resolution', 'orientation', 'accepted')
                lines.append('page: ' + ' '.join(f'{key}={item[key]}' for key in fields if key in item))
            elif event in ('post.selected', 'search.selected', 'subscription.decision', 'media.quality.selected'):
                lines.append(event + ': ' + json.dumps({k: v for k, v in item.items() if k in {'post_id', 'pid', 'decision', 'reason', 'selected_source'}}, ensure_ascii=False))
            elif event.endswith('.finish'):
                lines.append(f"outcome: {item.get('outcome')} duration_ms={item.get('duration_ms')} api_requests={item.get('api_requests', '?')}")
            elif event.startswith('telegram.send.'):
                lines.append(event)
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, default=Path(__file__).resolve().parent.parent / 'logs' / 'app.observability.logic_trace.jsonl')
    for name in ('trace', 'flow', 'user', 'event'):
        parser.add_argument('--' + name)
    parser.add_argument('--last', type=int)
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    if args.last is not None and not 1 <= args.last <= 10000:
        parser.error('--last must be between 1 and 10000')
    events, invalid = read_events(args.file, trace=args.trace, flow=args.flow, user=args.user,
        event=args.event, last=args.last or (10000 if args.trace else 100))
    if args.summary:
        print(summary(events))
    else:
        for item in events:
            fields = {k: v for k, v in item.items() if k not in {'ts', 'trace_id', 'flow', 'event'}}
            print(f"{item.get('ts', '?')} {item.get('trace_id', '?')} {item.get('event', '?')} " + json.dumps(fields, ensure_ascii=False))
    if invalid:
        print(f'Skipped invalid JSON lines: {invalid}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
