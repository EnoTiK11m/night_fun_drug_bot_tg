"""Domain implementation. Dependencies are supplied by the public facade."""

import asyncio

# Ref-counted locks cover selection, send and acknowledgement for one user's
# subscriptions. The single-instance guard already excludes a second bot process.
_delivery_locks = {}
CLAIM_RENEWAL_INTERVAL_SECONDS = 60.0


class _SubscriptionClaimLease:
    """Keep one worker's lease alive; fail closed if renewal loses ownership."""

    def __init__(self, runtime, user_id, query, token, *, sleep=None):
        self.runtime = runtime
        self.user_id, self.query, self.token = user_id, query, token
        self.sleep = sleep or asyncio.sleep
        self.lost = False
        self.stopped = False
        self.worker_started = False
        self.task = None

    def trace(self, event, *, level="normal", **fields):
        self.runtime.trace_event(
            event, level=level, user_id_hash=self.runtime.safe_hash(self.user_id, 'u_'),
            query=self.query, query_hash=self.runtime.safe_hash(self.query, 'q_'),
            claim_token_hash=self.runtime.safe_hash(self.token),
            lease_minutes=self.runtime.SUBSCRIPTION_CLAIM_MINUTES, **fields,
        )

    async def _renew(self, worker):
        while not self.stopped:
            await self.sleep(CLAIM_RENEWAL_INTERVAL_SECONDS)
            if self.stopped:
                return
            try:
                renewed = await self.runtime.renew_subscription_claim(
                    self.user_id, self.query, self.token
                )
            except Exception as exc:
                self.runtime.logger.warning(
                    "Subscription claim renewal failed user=%s query=%r type=%s",
                    self.user_id, self.query, type(exc).__name__,
                )
                renewed = False
            if self.stopped:
                return
            if renewed:
                self.trace("claim.renewed", level="verbose", result=True,
                           lease_remaining_seconds=self.runtime.SUBSCRIPTION_CLAIM_MINUTES * 60)
            else:
                self.lost = True
                self.trace("claim.renewal_failed", result=False)
                self.runtime.logger.warning(
                    "Subscription claim lost during active flow user=%s query=%r",
                    self.user_id, self.query,
                )
                worker.cancel()
                return

    async def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.trace("claim.renewal_stopped", level="verbose", result=not self.lost)

    async def run(self, coroutine):
        worker = asyncio.create_task(coroutine, name="subscription-claim-worker")
        self.task = asyncio.create_task(self._renew(worker), name="subscription-claim-renewal")
        try:
            result = await worker
            # A simultaneous external cancellation must still reach the caller,
            # even if the worker consumed our internal claim-loss cancellation.
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            return result
        finally:
            await self.runtime._cancellation_safe_db_call(self.stop())
            if not self.worker_started:
                await self.runtime._cancellation_safe_db_call(
                    self.runtime.release_subscription_claim(self.user_id, self.query, self.token)
                )


async def process_one_subscription(runtime, app, subscription):
    user = subscription[0]
    entry = _delivery_locks.setdefault(user, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            return await _process_one_subscription(runtime, app, subscription)
    finally:
        entry[1] -= 1
        if not entry[1]:
            del _delivery_locks[user]


async def _process_one_subscription(runtime, app, subscription):
    """Process one due subscription after atomically claiming it."""
    user_id, query, interval, empty_count = subscription
    runtime.trace_event("subscription.claim.request", level="normal")
    processing_token = await runtime.claim_due_subscription(user_id, query)
    runtime.trace_event("subscription.claim.success" if processing_token else "subscription.claim.failed", level="normal", claim_token_hash=runtime.safe_hash(processing_token) if processing_token else None)
    if not processing_token:
        return False

    if not runtime.is_recipient_allowed(user_id, user_id, "private"):
        runtime.logger.info(
            "Skipping subscription after access revocation user=%s query=%r",
            user_id,
            query,
        )
        await runtime.release_subscription_claim(user_id, query, processing_token)
        return False

    lease = _SubscriptionClaimLease(runtime, user_id, query, processing_token)
    return await lease.run(_process_claimed_subscription(
        runtime, app, subscription, processing_token, lease
    ))


async def _process_claimed_subscription(runtime, app, subscription, processing_token, lease):
    lease.worker_started = True
    user_id, query, interval, empty_count = subscription
    result = None
    caption = ""
    claim_completed = False
    delivery_started = False
    try:
        runtime.logger.info("Отправляем подписку пользователю %s: %s", user_id, query)

        blacklist = await runtime.get_user_blacklist(user_id)
        settings = await runtime.get_user_settings(user_id)
        subscription_options = await runtime.get_subscription_options(user_id, query)
        runtime.trace_event("subscription.options.loaded", level="normal", options=subscription_options)
        blacklist |= set(str(subscription_options.get("extra_blacklist", "")).split())
        settings.update({
            key: value for key, value in subscription_options.items()
            if key in {"rating_filter", "media_type", "orientation", "min_width", "min_height", "quality_mode"}
        })
        settings = runtime.normalize_feature_settings(settings)
        excluded_post_ids = set()
        result = await runtime.get_subscription_cached_image(
            user_id, query, blacklist, excluded_post_ids, settings
        )
        runtime.reset_upstream_failure_streak()

        if result:
            await runtime.remember_and_cache_post(result)
            post_id = result.get("id", 0)
            if subscription_options.get("digest_mode") == "digest":
                queued = await runtime.enqueue_subscription_digest(user_id, query, result)
                await lease.stop()
                updated = await runtime.update_subscription_time(user_id, query, processing_token)
                claim_completed = bool(updated)
                if updated and post_id:
                    await runtime.search_service.delivered(user_id, query, result, subscription=True, user_delivery=False)
                    await runtime.mark_post_sent(user_id, int(post_id))
                runtime.runtime_metrics.increment("subscription_digest_queued", int(queued))
                return bool(updated)
            if not await runtime.is_subscription_claim_active(
                user_id, query, processing_token
            ):
                runtime.logger.info(
                    "Subscription changed before delivery user=%s query=%r",
                    user_id,
                    query,
                )
                return False
            keyboard = runtime.get_subscription_image_keyboard(
                post_id,
                query,
                runtime.should_show_tags_button(settings),
                side_effect_callback=runtime.subscription_callback_issuer_for(user_id),
            )

            caption = ""
            if settings.get("show_caption", True):
                caption = await runtime.build_caption(settings, result, query, True)

            delivery_started = True

            async def subscription_delivery_allowed():
                allowed = runtime.is_recipient_allowed(user_id, user_id, "private")
                valid = await runtime.is_subscription_claim_active(user_id, query, processing_token) if allowed and not lease.lost else False
                ctx = runtime.current_trace()
                runtime.trace_event("subscription.before_send.revalidate", level="normal", recipient_allowed=allowed, claim_valid=valid, active=ctx.fields.get('claim_active') if ctx and allowed else None, paused=ctx.fields.get('claim_paused') if ctx and allowed else None)
                if not valid:
                    runtime.trace_event("telegram.send.skipped", reason="claim_invalid_or_recipient_denied")
                return allowed and valid

            runtime.trace_event("subscription.delivery.start", post_id=post_id)
            delivered = await runtime.send_post_media_to_chat(
                app.bot,
                user_id,
                result,
                caption,
                keyboard,
                settings=settings,
                before_send=subscription_delivery_allowed,
            )
            if lease.lost:
                runtime.trace_outcome("stale")
                return False
            runtime.trace_event("subscription.delivery.success" if delivered else "subscription.delivery.failed", post_id=post_id)
            runtime.trace_outcome("success" if delivered else "telegram_error", post_id=post_id)
            if delivered:
                runtime.runtime_metrics.increment("subscription_delivered")
                if post_id:
                    await runtime._cancellation_safe_db_call(
                        runtime.search_service.delivered(user_id, query, result, subscription=True)
                    )
                # Stop before our own terminal UPDATE clears the token: a
                # concurrent renewal must not mistake completion for claim loss.
                await lease.stop()
                updated = await runtime.update_subscription_time(user_id, query, processing_token)
                claim_completed = bool(updated)
                if updated and post_id:
                    await runtime.mark_post_sent(user_id, int(post_id))
                    await runtime.clear_delivery_failure_for_post(user_id, int(post_id))
                elif not updated:
                    runtime.logger.warning(
                        "Subscription claim expired before schedule update for user=%s query=%r",
                        user_id,
                        query,
                    )
            else:
                runtime.runtime_metrics.increment("subscription_failed")
                runtime.revoke_unsent_keyboard_callbacks(keyboard)
                await runtime.save_delivery_failure(user_id, result, caption)
            return bool(delivered)

        await lease.stop()
        empty_count, backoff_minutes, should_notify = await runtime.mark_subscription_empty(
            user_id, query, processing_token
        )
        runtime.trace_event("subscription.empty", reason="fresh_empty_and_archive_exhausted")
        if backoff_minutes > 0:
            runtime.trace_event("subscription.decision", decision="backoff", reason="fresh_empty_and_archive_exhausted")
            runtime.trace_event("subscription.backoff.applied", empty_count=empty_count, backoff_minutes=backoff_minutes)
            runtime.trace_event("subscription.backoff", empty_count=empty_count, backoff_minutes=backoff_minutes)
            runtime.trace_outcome("empty")
        else:
            runtime.trace_event('subscription.decision', decision='skip', reason='claim_lost_before_backoff')
            runtime.trace_outcome('stale')
        claim_completed = backoff_minutes > 0
        runtime.logger.info(
            "No new post for subscription user=%s query=%r; empty_count=%s backoff=%s",
            user_id,
            query,
            empty_count,
            backoff_minutes,
        )
        if should_notify:
            async def subscription_notice_allowed():
                return runtime.is_recipient_allowed(user_id, user_id, "private")

            await runtime.send_text_to_chat(
                app.bot,
                user_id,
                before_send=subscription_notice_allowed,
                text=(
                    f"🕒 По подписке `{runtime.md_code(query)}` пока нет новых постов.\n\n"
                    f"Я продолжу проверять ее реже: следующая проверка примерно через {backoff_minutes} мин. "
                    "Когда появится новый пост, подписка вернется к обычному интервалу."
                ),
                parse_mode="Markdown",
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
            )
        return False

    except runtime.asyncio.CancelledError:
        await runtime._cancellation_safe_db_call(lease.stop())
        if lease.lost:
            runtime.trace_outcome("stale")
            return False
        if delivery_started:
            claim_completed = await runtime._cancellation_safe_db_call(
                runtime.defer_subscription_after_transient_failure(
                    user_id, query, processing_token, backoff_seconds=1800
                )
            )
        raise
    except runtime.TimedOut as trace_exc:
        await lease.stop()
        runtime.trace_outcome("telegram_error")
        runtime.trace_error(trace_exc, stage="subscription.delivery", event="subscription.error")
        claim_completed = await runtime.defer_subscription_after_transient_failure(
            user_id, query, processing_token, backoff_seconds=1800
        )
        runtime.logger.warning(
            "Ambiguous subscription timeout deferred user=%s query=%r",
            user_id,
            query,
        )
        return False
    except runtime.NetworkError as exc:
        await lease.stop()
        runtime.trace_outcome("telegram_error")
        runtime.trace_error(exc, stage="subscription.delivery", event="subscription.error")
        if isinstance(exc, runtime.BadRequest):
            if result:
                await runtime.save_delivery_failure(
                    user_id, result, caption, error=f"BadRequest: {exc}"
                )
            claim_completed = await runtime.defer_subscription_after_transient_failure(
                user_id, query, processing_token
            )
            return False
        claim_completed = await runtime.defer_subscription_after_transient_failure(
            user_id, query, processing_token, backoff_seconds=1800
        )
        runtime.logger.warning(
            "Ambiguous subscription network error deferred user=%s query=%r: %s",
            user_id,
            query,
            exc,
        )
        return False
    except runtime.SearchBudgetExceeded:
        await lease.stop()
        trace_ctx = runtime.current_trace()
        deadline_hit = bool(trace_ctx and trace_ctx.outcome == 'deadline')
        runtime.trace_event('subscription.defer.deadline' if deadline_hit else 'subscription.defer.budget', reason='deadline' if deadline_hit else 'request_budget_exhausted')
        claim_completed = await runtime.defer_subscription_after_transient_failure(user_id, query, processing_token)
        return False
    except runtime.APITemporaryError as e:
        await lease.stop()
        runtime.trace_outcome("api_error")
        runtime.trace_event("subscription.defer.api_error", reason="api_error")
        runtime.trace_error(e, stage="rule34")
        await runtime.note_upstream_failure(app, str(e))
        runtime.logger.warning(
            "Temporary Rule34 API error for subscription user=%s query=%r: %s",
            user_id,
            query,
            e,
        )
        claim_completed = await runtime.defer_subscription_after_transient_failure(
            user_id, query, processing_token
        )
        return False
    except Exception as exc:
        await lease.stop()
        runtime.trace_outcome("error")
        runtime.trace_error(exc, stage="subscription", event="subscription.error")
        if result:
            await runtime.save_delivery_failure(
                user_id, result, caption, error=f"{type(exc).__name__}: {exc}"
            )
        runtime.logger.exception("Subscription processing error for user %s", user_id)
        claim_completed = await runtime.defer_subscription_after_transient_failure(
            user_id, query, processing_token
        )
        return False
    finally:
        await runtime._cancellation_safe_db_call(lease.stop())
        if not claim_completed:
            await runtime._cancellation_safe_db_call(
                runtime.release_subscription_claim(user_id, query, processing_token)
            )


async def get_subscription_cached_image(runtime, user_id, query, blacklist, excluded_post_ids, settings):
    try:
        return await runtime.search_service.select(user_id, query, blacklist, settings or {}, subscription=True)
    except runtime.SearchBudgetExceeded:
        raise
    except runtime.APITemporaryError as trace_exc:
        runtime.trace_outcome("api_error")
        runtime.trace_error(trace_exc, stage="rule34")
        cached, _ = await runtime.get_subscription_cache(user_id, query)
        seen = await runtime.search_service._seen(user_id, query, cached, True)
        blocked = {str(tag).lower().lstrip('-') for tag in blacklist}
        available = [p for p in cached if p.get('file_url') and p.get('id') not in seen | excluded_post_ids
            and not blocked.intersection(str(p.get('tags', '')).lower().split())
            and runtime.post_matches_preferences(p, runtime.normalize_feature_settings(settings or {}))]
        if available:
            result = runtime.random.choice(available)
            runtime.trace_event('cache.decision', level='verbose', decision='fallback', reason='api_error', candidate_count=len(available), post_id=result.get('id'))
            runtime.trace_event('post.selected', post_id=result.get('id'), selection_mode='cache', candidate_count=len(available))
            return result
        raise


async def process_subscriptions(runtime, app):
    """Фоновая задача для обработки подписок"""
    runtime.logger.info("Запущена фоновая задача для подписок")
    semaphore = runtime.asyncio.Semaphore(runtime.SUBSCRIPTION_CONCURRENCY)

    async def guarded_user(subscriptions):
        async with semaphore:
            sent_this_pass = 0
            for subscription in subscriptions[:runtime.SUBSCRIPTION_MAX_POSTS_PER_USER_PASS]:
                sent_this_pass += bool(await runtime.process_one_subscription(app, subscription))
            runtime.logger.info(
                "Subscription pass user=%s sent=%s due=%s",
                subscriptions[0][0],
                sent_this_pass,
                len(subscriptions),
            )

    while True:
        try:
            await runtime.release_stale_subscription_claims()
            due_subs = await runtime.get_due_subscriptions()
            subscriptions_by_user = {}
            for subscription in due_subs:
                subscriptions_by_user.setdefault(subscription[0], []).append(subscription)
            pending_users = iter(subscriptions_by_user.values())
            async def worker():
                for subscriptions in pending_users:
                    await guarded_user(subscriptions)
            await runtime.asyncio.gather(*(worker() for _ in range(min(runtime.SUBSCRIPTION_CONCURRENCY, len(subscriptions_by_user)))))
            for digest_user_id in await runtime.get_due_digest_users():
                if not runtime.is_recipient_allowed(
                    digest_user_id, digest_user_id, "private"
                ):
                    runtime.logger.info(
                        "Skipping digest after access revocation user=%s",
                        digest_user_id,
                    )
                    continue
                claim_token, digest_posts = await runtime.claim_subscription_digest(
                    digest_user_id, 10
                )
                if not claim_token:
                    continue
                claim_open = True
                locks: list[runtime.DigestSubscriptionLockHandle] = []
                lease: runtime.DigestClaimLease | None = None
                try:
                    locks = await runtime.acquire_digest_subscription_locks(
                        digest_user_id, digest_posts
                    )
                    active_keys = await runtime.get_subscription_digest_claim_keys(
                        digest_user_id, claim_token
                    )
                    digest_posts = [
                        post for post in digest_posts
                        if runtime.digest_item_key(post) in active_keys
                    ]
                    lease = runtime.DigestClaimLease(digest_user_id, claim_token)
                    if not digest_posts or not await lease.start():
                        await runtime.cancellation_safe_digest_finish(
                            digest_user_id, claim_token, []
                        )
                        claim_open = False
                        continue
                    delivery = await runtime.send_digest_to_chat(
                        app.bot, digest_user_id, digest_posts, lease=lease
                    )
                    if delivery.ambiguous_ids:
                        await runtime.cancellation_safe_digest_finish(
                            digest_user_id,
                            claim_token,
                            delivery.delivered_ids,
                            delivery.ambiguous_ids,
                        )
                    else:
                        await runtime.cancellation_safe_digest_finish(
                            digest_user_id, claim_token, delivery.delivered_ids
                        )
                    claim_open = False
                    runtime.logger.info(
                        "Scheduled digest result user=%s delivered=%s failed=%s ambiguous=%s",
                        digest_user_id,
                        len(delivery.delivered_ids),
                        len(delivery.failed_ids),
                        len(delivery.ambiguous_ids),
                    )
                except runtime.DigestDeliveryCancelled as exc:
                    await runtime.cancellation_safe_digest_finish(
                        digest_user_id,
                        claim_token,
                        exc.result.delivered_ids,
                        exc.result.ambiguous_ids,
                    )
                    claim_open = False
                    raise
                finally:
                    if lease is not None:
                        await lease.stop()
                    if claim_open:
                        await runtime.cancellation_safe_digest_release(
                            digest_user_id, claim_token
                        )
                    runtime.release_digest_subscription_locks(locks)
            runtime.logger.info(
                "Subscription pass complete users=%s due=%s",
                len(subscriptions_by_user),
                len(due_subs),
            )
            await runtime.asyncio.sleep(runtime.SUBSCRIPTION_CHECK_INTERVAL_SECONDS)

        except Exception:
            runtime.logger.exception("Ошибка в фоновой задаче подписок")
            await runtime.asyncio.sleep(runtime.SUBSCRIPTION_CHECK_INTERVAL_SECONDS)
