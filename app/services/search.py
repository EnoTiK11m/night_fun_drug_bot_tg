"""Bounded page selection with SQLite progress and per-subscription history."""
import asyncio
import hashlib
import json
import random
import time
from contextlib import asynccontextmanager

import app.storage.database as database
from app.integrations.rule34.client import APITemporaryError
from app.services.media_preferences import normalize_feature_settings, post_matches_preferences
from app.observability.logic_trace import trace_event, traced_flow, enabled, current_trace, annotate, outcome, add_timing
from app.services.media_preferences import media_kind

PAGE_SIZE = 250
SEARCH_REQUEST_BUDGET = 12
SUBSCRIPTION_REQUEST_BUDGET = 4
SEARCH_DEADLINE_SECONDS = 60
SUBSCRIPTION_DEADLINE_SECONDS = 90


class SearchBudgetExceeded(APITemporaryError):
    """Progress is saved, but this pass cannot establish archive exhaustion."""


class SearchBusy(Exception):
    pass


class SearchAdmission:
    def __init__(self, capacity=8, concurrency=2):
        self.capacity = capacity
        self.semaphore = asyncio.Semaphore(concurrency)
        self.users = set()
        self.tasks = set()

    @asynccontextmanager
    async def hold(self, user):
        if user in self.users or len(self.users) >= self.capacity:
            raise SearchBusy()
        self.users.add(user)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            queued_at = time.monotonic() if enabled('verbose') else 0
            async with asyncio.timeout(90):
                async with self.semaphore:
                    trace_event('search.queue.admitted', level='verbose', queue_wait_ms=(time.monotonic() - queued_at) * 1000 if queued_at else 0)
                    yield
        finally:
            self.users.discard(user)
            self.tasks.discard(task)

    async def stop(self):
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)


class ProgressiveSearch:
    def __init__(self, api, request_budget=None):
        self.api = api
        self.request_budget = request_budget
        self.cache_page = None

    async def _save(self, kind, user, query, state):
        async with database.connect_db() as db:
            # A deleted subscription must never resurrect progress.
            if kind == 'subscription':
                exists = await (await db.execute('SELECT 1 FROM subscriptions WHERE user_id=? AND query=?', (user, query))).fetchone()
                if not exists:
                    return
            await db.execute('''INSERT INTO query_progress
                (kind,user_id,query,signature,pid,high_water,page_json,used_json)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind,user_id,query) DO UPDATE SET
                signature=excluded.signature,pid=excluded.pid,high_water=excluded.high_water,
                page_json=excluded.page_json,used_json=excluded.used_json,updated_at=CURRENT_TIMESTAMP''',
                (kind, user, query, state['signature'], state['pid'], state['high_water'], json.dumps(state['page']), json.dumps(state['used'])))
            if kind == 'search':
                pruned = await db.execute('''DELETE FROM query_progress WHERE kind='search' AND user_id=? AND query IN
                    (SELECT query FROM query_progress WHERE kind='search' AND user_id=? ORDER BY updated_at DESC,query LIMIT -1 OFFSET 200)''', (user, user))
                if pruned.rowcount:
                    trace_event('progress.expire', level='normal', kind=kind, reason='query_retention_limit', expired_count=pruned.rowcount)
            await db.commit()
            trace_event('progress.save', level='normal', kind=kind, pid=state['pid'], high_water=state['high_water'], page_size=len(state['page']), used_count=len(state['used']), signature=state['signature'])
            trace_event('db.progress.save', level='normal', acknowledged=True)
            trace_event(kind + '.progress.saved', level='normal', pid=state['pid'])

    async def _seen(self, user, query, posts, subscription):
        ids = [int(p['id']) for p in posts]
        async with database.connect_db() as db:
            if subscription:
                cursor = await db.execute('''SELECT post_id FROM subscription_delivery_history
                    WHERE user_id=? AND query=? AND post_id IN (SELECT value FROM json_each(?))''', (user, query, json.dumps(ids)))
            else:
                cursor = await db.execute('''SELECT post_id FROM sent_posts
                    WHERE user_id=? AND post_id IN (SELECT value FROM json_each(?))''', (user, json.dumps(ids)))
            return {row[0] for row in await cursor.fetchall()}

    @traced_flow(lambda b: 'subscription' if b.get('subscription') else 'search', user_arg='user', query_arg='query')
    async def select(self, user, query, blacklist, settings, *, subscription=False, excluded=None):
        kind = 'subscription' if subscription else 'search'
        settings = normalize_feature_settings(settings)
        filters = {k: settings.get(k) for k in ('rating_filter', 'media_type', 'orientation', 'min_width', 'min_height')}
        blocked = {str(t).lower().lstrip('-') for t in blacklist}
        signature = hashlib.sha256(json.dumps([sorted(blocked), filters], sort_keys=True).encode()).hexdigest()
        async with database.connect_db() as db:
            row = await (await db.execute('SELECT signature,pid,high_water,page_json,used_json FROM query_progress WHERE kind=? AND user_id=? AND query=?', (kind, user, query))).fetchone()
        state = {'signature': signature, 'pid': 0, 'high_water': 0, 'page': [], 'used': []}
        if row:
            state['high_water'] = row[2]
            if row[0] == signature:
                state.update(pid=row[1], page=json.loads(row[3]), used=json.loads(row[4]))
            else:
                trace_event('progress.reset', level='normal', reason='filter_signature_changed', from_pid=row[1], to_pid=0)
        trace_event('progress.load', level='normal', kind=kind, found=bool(row), pid=state['pid'], high_water=state['high_water'], page_size=len(state['page']), used_count=len(state['used']), signature=signature)
        trace_event('db.progress.load', level='normal', found=bool(row))
        trace_event(kind + '.progress.loaded', level='normal', pid=state['pid'], used_count=len(state['used']))
        ctx = current_trace()
        is_more = bool(ctx and ctx.fields.get('is_more'))
        if is_more:
            trace_event('search.more.progress', level='normal', pid_before=state['pid'], used_count=len(state['used']))
        if enabled('verbose'):
            trace_event('progress.snapshot', level='verbose', page_ids=[p.get('id') for p in state['page'][:20]], used_ids=state['used'][:20])
        budget = self.request_budget or (SUBSCRIPTION_REQUEST_BUDGET if subscription else SEARCH_REQUEST_BUDGET)
        deadline = time.monotonic() + (SUBSCRIPTION_DEADLINE_SECONDS if subscription else SEARCH_DEADLINE_SECONDS)
        calls = 0
        normalization = {}
        trace_event('budget.start', level='normal', max_requests=budget, used_requests=0, remaining_requests=budget, deadline_remaining_ms=(deadline - time.monotonic()) * 1000)

        async def fetch(pid):
            nonlocal calls
            remaining = deadline - time.monotonic()
            if calls >= budget or remaining <= 0:
                await self._save(kind, user, query, state)
                reason = 'request_budget_exhausted' if calls >= budget else 'deadline'
                outcome('budget_exhausted' if calls >= budget else 'deadline')
                trace_event('budget.exhausted', reason=reason, max_requests=budget, used_requests=calls, remaining_requests=max(0, budget-calls), deadline_remaining_ms=max(0, remaining)*1000)
                trace_event(kind + ('.budget.exhausted' if calls >= budget else '.deadline.exceeded'), pid=pid, reason=reason)
                if subscription:
                    trace_event('subscription.decision', decision='defer', reason=reason)
                raise SearchBudgetExceeded('Search pass budget reached; continue from saved cursor')
            calls += 1
            api_started = time.monotonic() if enabled() else 0
            annotate(pages_checked=calls, api_requests=calls, budget_remaining=budget-calls)
            trace_event('budget.consume', level='normal', max_requests=budget, used_requests=calls, remaining_requests=budget-calls, deadline_remaining_ms=remaining*1000)
            trace_event(kind + '.page.request', level='normal', pid=pid, limit=PAGE_SIZE, budget_remaining=budget-calls)
            try:
                async with asyncio.timeout(remaining):
                    if subscription and hasattr(self.api, 'search_subscription_cache'):
                        posts = await self.api.search_subscription_cache(query, blacklist, limit=PAGE_SIZE, pid=pid, timeout=min(remaining, 30))
                    else:
                        posts = await self.api.search(query, blacklist, limit=PAGE_SIZE, pid=pid,
                            timeout=min(remaining, 30), request_kind='background' if subscription else 'interactive')
            except TimeoutError as exc:
                await self._save(kind, user, query, state)
                outcome('deadline')
                trace_event(kind + '.deadline.exceeded', pid=pid)
                raise SearchBudgetExceeded('Search deadline reached') from exc
            finally:
                if api_started:
                    add_timing('api', (time.monotonic()-api_started)*1000)
            if subscription and posts and self.cache_page:
                trace_event('cache.update', level='verbose', pid=pid, received=len(posts), source='api_page')
                await self.cache_page(user, query, posts)
            clean = []
            for p in posts or []:
                if not isinstance(p, dict):
                    continue
                try:
                    post_id = int(p['id'])
                except (KeyError, TypeError, ValueError):
                    continue
                if post_id > 0:
                    clean.append(dict(p, id=post_id))
            normalization[id(clean)] = {'received': len(posts or []), 'invalid_id': len(posts or []) - len(clean)}
            trace_event(kind + '.page.result', level='normal', pid=pid, received=len(posts or []), valid_ids=len(clean))
            if not posts:
                trace_event(kind + '.page.empty', level='normal', pid=pid)
            return clean

        async def eligible(posts, *, stage='archive', pid=None, used=()):
            seen = await self._seen(user, query, posts, subscription)
            seen |= set(excluded or ()) if not subscription else set()
            filtered_input = [p for p in posts if p['id'] not in used]
            filter_started = time.monotonic() if enabled() else 0
            accepted = [p for p in filtered_input if p.get('file_url') and p['id'] not in seen
                and not blocked.intersection(str(p.get('tags', '')).lower().split())
                and post_matches_preferences(p, settings)]
            if filter_started:
                add_timing('filter', (time.monotonic()-filter_started)*1000)
            if enabled('normal'):
                counts = dict.fromkeys(('invalid_id','used_in_snapshot','missing_url','dedup','blacklist','rating','media_type','resolution','orientation'), 0)
                samples = []
                for p in posts:
                    reason = None
                    if p['id'] in used: reason = 'used_in_snapshot'
                    elif not p.get('file_url'): reason = 'missing_url'
                    elif p['id'] in seen: reason = 'dedup'
                    elif blocked.intersection(str(p.get('tags', '')).lower().split()): reason = 'blacklist'
                    elif not post_matches_preferences(p, settings):
                        if settings.get('rating_filter') != 'all' and p.get('rating') != settings.get('rating_filter'): reason = 'rating'
                        elif settings.get('media_type') != 'all' and media_kind(p) != settings.get('media_type'): reason = 'media_type'
                        elif not post_matches_preferences(p, dict(settings, orientation='any')): reason = 'resolution'
                        else: reason = 'orientation'
                    if reason:
                        counts[reason] += 1
                        if enabled('verbose') and len(samples) < 20:
                            samples.append({'post_id': p['id'], 'reason': reason})
                norm = normalization.pop(id(posts), {}) if stage == 'archive' else {}
                counts['invalid_id'] = norm.get('invalid_id', 0)
                fields = dict(counts, received=len(posts)+counts['invalid_id'], accepted=len(accepted), remaining=len(accepted), already_sent=counts['dedup'], pid=pid, stage=stage)
                trace_event('filter.summary', level='normal', **fields)
                trace_event('subscription.' + stage + '.filtered' if subscription else 'search.page.filtered', level='normal', **fields)
                if samples:
                    trace_event('filter.rejections', level='verbose', rejected_samples=samples)
            return accepted

        def selected(post, candidates, mode, pid):
            if not enabled():
                return
            annotate(post_id=post['id'])
            trace_event('post.selected', post_id=post['id'], pid=pid, candidate_count=len(candidates), selection_mode=mode, rating=post.get('rating'), media_kind=media_kind(post), width=post.get('width'), height=post.get('height'), score=post.get('score'))
            trace_event('subscription.' + ('fresh' if mode == 'new' else 'archive') + '.selected' if subscription else 'search.selected', post_id=post['id'], pid=pid)
            if is_more:
                trace_event('search.more.selected', post_id=post['id'], pid=pid)

        if subscription:
            trace_event('subscription.fresh.start', level='normal', pid=0, watermark_before=state['high_water'])
            fresh = await fetch(0)
            new_posts = [p for p in fresh if p['id'] > state['high_water']]
            trace_event('subscription.fresh.result', level='normal', pid=0, watermark_before=state['high_water'], highest_received_id=max((p['id'] for p in fresh), default=0), new_ids_count=len(new_posts), received=len(fresh))
            if state['high_water']:
                new = await eligible(new_posts, stage='fresh', pid=0)
                if new:
                    # Lowest new ID first: no new candidate is skipped by advancing the watermark.
                    result = min(new, key=lambda p: p['id'])
                    await self._save(kind, user, query, state)
                    trace_event('subscription.decision', decision='fresh', matching_new_ids=len(new))
                    selected(result, new, 'new', 0)
                    return result
            else:
                state['high_water'] = max((p['id'] for p in fresh), default=0)
            if state['pid'] == 0 and not state['page']:
                state['page'] = fresh
            trace_event('subscription.decision', decision='archive', reason='no_fresh_candidates')
            trace_event('subscription.archive.start', level='normal', archive_pid_before=state['pid'])

        while True:
            if state['page'] and is_more:
                trace_event('search.more.page_reused', level='normal', pid=state['pid'], used_count=len(state['used']))
            if not state['page']:
                state['page'] = await fetch(state['pid'])
            if not state['page']:
                await self._save(kind, user, query, state)
                outcome('empty')
                trace_event('subscription.empty.real' if subscription else 'search.exhausted', reason='archive_exhausted', pid=state['pid'])
                return None
            trace_event('subscription.archive.page' if subscription else 'search.page.snapshot', level='normal', pid=state['pid'], page_candidates=len(state['page']), used_in_snapshot=len(state['used']))
            candidates = await eligible(state['page'], pid=state['pid'], used=state['used'])
            if candidates:
                await self._save(kind, user, query, state)
                result = random.choice(candidates)
                selected(result, candidates, 'archive' if subscription else 'random', state['pid'])
                return result
            trace_event('subscription.archive.advance' if subscription else 'search.page.advance', level='normal', from_pid=state['pid'], to_pid=state['pid']+1, reason='page_exhausted', archive_page_exhausted=True)
            if is_more:
                trace_event('search.more.page_advanced', level='normal', from_pid=state['pid'], to_pid=state['pid']+1, reason='page_exhausted')
            state.update(pid=state['pid'] + 1, page=[], used=[])
            await self._save(kind, user, query, state)

    async def delivered(self, user, query, post, *, subscription=False):
        kind = 'subscription' if subscription else 'search'
        async with database.connect_db() as db:
            await db.execute('BEGIN IMMEDIATE')
            row = await (await db.execute('SELECT used_json,high_water FROM query_progress WHERE kind=? AND user_id=? AND query=?', (kind, user, query))).fetchone()
            if row:
                used = json.loads(row[0])
                post_id = int(post['id'])
                if post_id not in used:
                    used.append(post_id)
                await db.execute('UPDATE query_progress SET used_json=?,high_water=? WHERE kind=? AND user_id=? AND query=?', (json.dumps(used[-PAGE_SIZE:]), max(row[1], post_id), kind, user, query))
                if subscription:
                    inserted = await db.execute('INSERT OR IGNORE INTO subscription_delivery_history(user_id,query,post_id) SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM subscriptions WHERE user_id=? AND query=?)', (user, query, post_id, user, query))
            await db.commit()
            trace_event('db.history.duplicate' if row and subscription and inserted.rowcount == 0 else 'db.history.insert' if row else 'db.history.skipped', level='normal', kind=kind, post_id=post.get('id'), acknowledged=bool(row))
            if subscription and row:
                trace_event('subscription.history.saved', level='normal', post_id=post.get('id'), watermark_after=max(row[1], int(post['id'])))
