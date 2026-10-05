"""Domain implementation. Dependencies are supplied by the public facade."""

async def button_handler(runtime, update, context):
    """Обработчик нажатий кнопок"""
    pass
    query = update.callback_query
    user_id = query.from_user.id
    raw_data = query.data
    runtime.trace_event("callback.received", level="normal", action=raw_data.split("_", 1)[0], callback_size=len(raw_data))
    data = runtime.resolved_callback_data(raw_data)
    deferred_answer = data.startswith((
        "later_add_",
        "tag_block_",
        "bl_quick_",
        "gallery_bulk_fav_",
    ))
    reservation = await runtime.reserve_one_shot_callback_result(user_id, raw_data)
    if reservation != "accepted":
        runtime.trace_outcome("stale")
        entry = runtime.issued_one_shot_callbacks.get(raw_data)
        runtime.trace_event("callback.owner_rejected" if entry and entry.owner_id != user_id else "callback.stale_generation", reason=reservation)
        already_processed = reservation == "duplicate"
        if deferred_answer:
            await runtime.safe_query_answer(
                query,
                "Кнопка уже обработана" if already_processed else "Кнопка устарела",
            )
        else:
            await runtime.safe_query_answer(query)
            await query.message.reply_text(
                "Эта кнопка уже была обработана."
                if already_processed
                else "Эта кнопка устарела."
            )
        return
    if not await runtime.begin_one_shot_processing(user_id, raw_data):
        if deferred_answer:
            await runtime.safe_query_answer(query, "Кнопка устарела")
        else:
            await runtime.safe_query_answer(query)
            await query.message.reply_text("Эта кнопка устарела.")
        return
    reserved_entry = runtime.issued_one_shot_callbacks.get(raw_data)
    callback_flow_scoped = (
        reserved_entry.flow_scoped if reserved_entry is not None else True
    )
    async with runtime.guarded_user_state(user_id):
        callback_generation = runtime.temporary_user_state.generation(user_id)
        if callback_generation == 0:
            callback_generation = runtime.temporary_user_state.begin_flow(user_id)
    callback_issuer = runtime.side_effect_callback_for(user_id, callback_generation)

    async def begin_callback_flow(state: str, **related_state):
        return await runtime.begin_user_flow(
            user_id,
            state,
            expected_generation=callback_generation,
            **related_state,
        )
    if not deferred_answer:
        await runtime.safe_query_answer(query)

    async with runtime.guarded_user_state(user_id):
        callback_is_current = (
            runtime.temporary_user_state.generation(user_id) == callback_generation
        )
    if callback_flow_scoped and not callback_is_current:
        runtime.stale_flow_results_discarded += 1
        if deferred_answer:
            await runtime.safe_query_answer(query, "Кнопка устарела")
        else:
            await query.message.reply_text("Эта кнопка устарела.")
        return

    data = runtime.resolved_callback_data(raw_data)

    if data == "cancel_input":
        await runtime.invalidate_user_flow(user_id)
        await query.edit_message_text(
            "Действие отменено.\n\n" + await runtime.build_main_menu_text(user_id),
            reply_markup=await runtime.get_user_main_keyboard(user_id),
        )

    elif data.startswith("context_help_"):
        section = data.replace("context_help_", "", 1)
        help_texts = {
            "start": (
                "📖 *Как пользоваться*\n\n"
                "1. Откройте поиск и отправьте теги через пробел.\n"
                "2. Сохраняйте понравившиеся посты в библиотеку.\n"
                "3. Создайте подписку, чтобы получать новые посты автоматически."
            ),
            "search": (
                "Главная → Поиск → Помощь\n\n"
                "Обычный поиск находит один пост, подборка формирует альбом, "
                "а конструктор помогает собрать запрос с исключениями."
            ),
            "library": (
                "Главная → Библиотека → Помощь\n\n"
                "Избранное можно распределять по коллекциям, снабжать заметками "
                "и сохранять в список «На потом»."
            ),
            "subscriptions": (
                "Главная → Подписки → Помощь\n\n"
                "Подписка периодически проверяет сохранённый запрос. Её можно "
                "приостановить отдельно или временно остановить все подписки."
            ),
            "blacklist": (
                "Главная → Чёрный список → Помощь\n\n"
                "Добавленные теги исключаются из поиска, подборок и случайных постов. "
                "Временные теги удаляются автоматически после истечения срока."
            ),
            "settings": (
                "Главная → Настройки → Помощь\n\n"
                "Здесь настраиваются подписи, спойлеры, размер подборок, качество "
                "медиа и сложность интерфейса."
            ),
        }
        help_back_callbacks = {
            "search": "search_hub",
            "library": "library",
            "subscriptions": "subscriptions",
            "blacklist": "blacklist",
            "settings": "settings",
        }
        await query.edit_message_text(
            help_texts.get(section, "ℹ️ Справка для этого раздела недоступна."),
            reply_markup=runtime.InlineKeyboardMarkup([
                [runtime.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=help_back_callbacks.get(section, "back"),
                )]
            ]),
            parse_mode="Markdown",
        )

    elif data == "my_data":
        await query.edit_message_text(
            "Главная → Мои данные\n\n"
            "Здесь находятся статистика, сведения о хранилище и экспорт данных.",
            reply_markup=runtime.get_data_keyboard(),
        )

    elif data == "search":
        if await begin_callback_flow("waiting_search") is None:
            return
        await query.edit_message_text(
            "🔍 Введите теги для поиска (через пробел):\n\n"
            "Примеры:\n"
            "• `anime girl`\n"
            "• `2girls blonde_hair`\n"
            "• `solo male`\n\n"
            "💡 Используй `_` для тегов из нескольких слов",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("search_hub"),
        )

    elif data == "search_hub":
        await query.edit_message_text(
            "Главная → Поиск\n\nВыберите способ поиска.",
            reply_markup=runtime.get_search_hub_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "library":
        total = await runtime.count_favorites(user_id)
        later_count = (await runtime.get_user_storage_stats(user_id)).get("read_later", 0)
        await query.edit_message_text(
            f"Главная → Библиотека\n\nИзбранное: `{total}`\nНа потом: `{later_count}`",
            reply_markup=runtime.get_library_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "random":
        runtime.schedule_background_task(
            context,
            runtime.send_random_image(
                query.message,
                user_id,
                expected_generation=callback_generation,
            ),
        )

    elif data == "more":
        saved = await runtime.get_user_query(user_id)
        if saved and saved[0]:
            runtime.schedule_background_task(
                context,
                runtime.send_image(
                    query.message,
                    user_id,
                    saved[0],
                    edit=False,
                    is_more=True,
                    expected_generation=callback_generation,
                ),
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!", reply_markup=runtime.get_main_keyboard()
            )

    elif data.startswith("post_more_"):
        post_id_text = data.replace("post_more_", "", 1)
        if post_id_text.isdigit():
            settings = await runtime.get_user_settings(user_id)
            await query.edit_message_reply_markup(
                reply_markup=runtime.get_post_more_keyboard(
                    int(post_id_text), runtime.should_show_tags_button(settings)
                )
            )

    elif data.startswith("post_compact_"):
        post_id_text = data.replace("post_compact_", "", 1)
        if post_id_text.isdigit():
            settings = await runtime.get_user_settings(user_id)
            await query.edit_message_reply_markup(
                reply_markup=runtime.get_image_keyboard(
                    int(post_id_text),
                    show_tags_button=runtime.should_show_tags_button(settings),
                    side_effect_callback=callback_issuer,
                )
            )
    elif data == "blacklist":
        await query.edit_message_text(
            "Главная → Чёрный список\n\n"
            "Теги из этого списка исключаются из результатов поиска.",
            reply_markup=runtime.get_blacklist_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "subscriptions":
        await query.edit_message_text(
            await runtime.build_subscriptions_menu_text(user_id),
            reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data == "history":
        await runtime.show_history(query.message, user_id, edit=True)

    elif data == "favorites":
        await runtime.show_favorites(query.message, user_id, edit=True)

    elif data == "gallery":
        if await begin_callback_flow("waiting_gallery") is None:
            return
        await query.edit_message_text(
            "🖼 Введите теги для галереи. Для случайной подборки отправьте `random`.",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("search_hub"),
        )

    elif data.startswith("gallery_next_"):
        payload = await runtime.get_callback_payload("gallery_next", data)
        try:
            params = runtime.json.loads(payload or "{}")
            page = int(params.get("page", 0))
            tags = str(params.get("tags", ""))
        except (ValueError, TypeError, runtime.json.JSONDecodeError):
            await query.message.reply_text("❌ Подборка устарела. Запустите галерею заново.")
            return
        runtime.schedule_background_task(
            context,
            runtime.send_search_gallery(
                query.message,
                user_id,
                tags,
                page,
                expected_generation=callback_generation,
            ),
        )

    elif data == "search_builder":
        if await begin_callback_flow(
            "waiting_builder_include",
            builder=(runtime.search_builders, {}),
        ) is None:
            return
        await query.message.reply_text(
            "🧩 *Конструктор поиска*\n\nВведите обязательные теги через пробел.",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("search_hub"),
        )

    elif data == "presets":
        await runtime.show_search_presets(query.message, user_id, issuer=callback_issuer)

    elif data == "preset_save_current":
        saved = await runtime.get_user_query(user_id)
        if not saved or not saved[0]:
            await query.message.reply_text("Сначала выполните поиск.")
        else:
            if await begin_callback_flow(
                "waiting_preset_name",
                preset=(runtime.pending_preset_queries, saved[0]),
            ) is None:
                return
            await query.message.reply_text(
                "Введите название сохранённого запроса (до 40 символов):",
                reply_markup=runtime.get_cancel_keyboard("search_hub"),
            )

    elif data.startswith("preset_run_"):
        value = data.replace("preset_run_", "", 1)
        preset = await runtime.get_search_preset(user_id, int(value)) if value.isdigit() else None
        if not preset:
            await query.message.reply_text("Сохранённый запрос не найден.")
        else:
            await runtime.save_user_settings(user_id, preset["settings"])
            runtime.schedule_background_task(
                context,
                runtime.send_search_gallery(
                    query.message,
                    user_id,
                    preset["query"],
                    expected_generation=callback_generation,
                ),
            )

    elif data.startswith("preset_del_"):
        value = data.replace("preset_del_", "", 1)
        if value.isdigit():
            await runtime.delete_search_preset(user_id, int(value))
        await runtime.show_search_presets(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("preset_from_"):
        preset_query = await runtime.get_callback_payload("preset_from", data)
        if preset_query:
            if await begin_callback_flow(
                "waiting_preset_name",
                preset=(runtime.pending_preset_queries, preset_query),
            ) is None:
                return
            await query.message.reply_text(
                "Введите название сохранённого запроса:",
                reply_markup=runtime.get_cancel_keyboard("search_hub"),
            )

    elif data.startswith("builder_run_"):
        built_query = await runtime.get_callback_payload("builder_run", data)
        if built_query:
            runtime.schedule_background_task(
                context,
                runtime.send_search_gallery(
                    query.message,
                    user_id,
                    built_query,
                    expected_generation=callback_generation,
                ),
            )

    elif data == "recommendations":
        runtime.schedule_background_task(
            context,
            runtime.send_recommendations(
                query.message,
                user_id,
                expected_generation=callback_generation,
            ),
        )

    elif data.startswith("rec_hide_"):
        tag = await runtime.get_callback_payload("rec_hide", data)
        if tag:
            def exclude_recommendation_tag(settings):
                excluded = set(
                    str(settings.get("recommendation_excluded_tags", "")).split()
                )
                excluded.add(tag)
                return {
                    "recommendation_excluded_tags": " ".join(
                        sorted(excluded)[:100]
                    )
                }

            await runtime.mutate_user_settings(user_id, exclude_recommendation_tag)
            await query.message.reply_text(f"🚫 `{runtime.md_code(tag)}` исключён из рекомендаций.", parse_mode="Markdown")

    elif data.startswith("similar_"):
        value = data.replace("similar_", "", 1)
        post = await runtime.get_known_post(int(value)) if value.isdigit() else None
        if not post:
            await query.message.reply_text("Не удалось получить теги поста.")
        else:
            similar_tags = runtime.similar_query_from_post(post)
            if similar_tags:
                runtime.schedule_background_task(
                    context,
                    runtime.send_search_gallery(
                        query.message,
                        user_id,
                        similar_tags,
                        expected_generation=callback_generation,
                    ),
                )
            else:
                await query.message.reply_text("Недостаточно характерных тегов для похожей подборки.")

    elif data.startswith("tag_search_"):
        tag = await runtime.get_callback_payload("tag_search", data)
        if tag:
            runtime.schedule_background_task(
                context,
                runtime.send_search_gallery(
                    query.message,
                    user_id,
                    tag,
                    expected_generation=callback_generation,
                ),
            )

    elif data.startswith("tag_block_"):
        tag = await runtime.get_callback_payload("tag_block", data)
        if tag:
            added = await runtime.add_to_blacklist(user_id, tag)
            await runtime.safe_query_answer(
                query,
                "Добавлено в чёрный список" if added else "Тег уже в чёрном списке",
            )
        else:
            await runtime.safe_query_answer(query, "Кнопка устарела")

    elif data.startswith("later_add_"):
        value = data.replace("later_add_", "", 1)
        post = await runtime.get_known_post(int(value)) if value.isdigit() else None
        settings = runtime.normalize_feature_settings(await runtime.get_user_settings(user_id))
        added = bool(post) and await runtime.add_read_later(
            user_id, post, settings.get("read_later_days", 30)
        )
        await runtime.safe_query_answer(
            query,
            "Добавлено в «На потом»" if added else "Уже сохранено или недоступно",
        )

    elif data == "later_list":
        await runtime.show_read_later(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("later_open_"):
        value = data.replace("later_open_", "", 1)
        posts = await runtime.get_read_later(user_id, 100)
        post = next((item for item in posts if str(item.get("id")) == value), None)
        if post:
            settings = await runtime.get_user_settings(user_id)
            await runtime.send_post_media(
                query.message,
                post,
                keyboard=runtime.get_subscription_image_keyboard(
                    post.get("id", 0),
                    side_effect_callback=callback_issuer,
                ),
                settings=settings,
            )
        else:
            await query.message.reply_text("Пост больше не находится в списке.")

    elif data.startswith("later_del_"):
        value = data.replace("later_del_", "", 1)
        if value.isdigit():
            await runtime.remove_read_later(user_id, int(value))
        await runtime.show_read_later(query.message, user_id, issuer=callback_issuer)

    elif data == "storage":
        await runtime.show_storage(query.message, user_id)

    elif data == "storage_cleanup_90":
        await query.message.reply_text(
            "Удалить историю и служебные записи старше 90 дней?",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "🧹 Удалить",
                    callback_data=callback_issuer.side_effect(
                        "storage_cleanup_90_do"
                    ),
                ),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="storage"),
            ]]),
        )

    elif data == "storage_cleanup_90_do":
        removed = await runtime.cleanup_user_storage(user_id, 90)
        await query.message.reply_text(
            "🧹 Удалено старых записей: " + str(sum(removed.values()))
        )
        await runtime.show_storage(query.message, user_id)

    elif data == "storage_empty_collections":
        await query.message.reply_text(
            "Удалить все пустые коллекции?",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        "storage_empty_collections_do"
                    ),
                ),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="storage"),
            ]]),
        )

    elif data == "storage_empty_collections_do":
        removed = await runtime.cleanup_empty_collections(user_id)
        await query.message.reply_text(f"🗑 Удалено пустых коллекций: {removed}.")
        await runtime.show_storage(query.message, user_id)

    elif data.startswith("gallery_bulk_fav_"):
        raw_ids = await runtime.get_callback_payload("gallery_bulk_fav", data) or ""
        added = 0
        for value in raw_ids.split(",")[:10]:
            post = await runtime.get_known_post(int(value)) if value.isdigit() else None
            if post and await runtime.add_favorite(user_id, post):
                added += 1
        await runtime.safe_query_answer(query, f"Добавлено в избранное: {added}")

    elif data.startswith("gallery_collection_"):
        raw_ids = await runtime.get_callback_payload("gallery_collection", data) or ""
        bulk_post_ids = tuple(
            int(value) for value in raw_ids.split(",") if value.isdigit()
        )[:10]
        canonical_ids = ",".join(str(post_id) for post_id in bulk_post_ids)
        collections = await runtime.get_favorite_collections(user_id)
        if not await runtime.commit_flow_if_current(
            user_id, callback_generation, lambda: None
        ):
            return
        rows = [[runtime.InlineKeyboardButton(
            f"🗂 {item['name'][:28]}",
            callback_data=callback_issuer.side_effect(
                f"gallery_col_add:{item['id']}:{canonical_ids}"
            ),
        )] for item in collections]
        rows.append([runtime.InlineKeyboardButton(
            "➕ Новая коллекция",
            callback_data=callback_issuer.side_effect(
                f"gallery_col_new:{canonical_ids}"
            ),
        )])
        await query.message.reply_text(
            "Выберите коллекцию для всей подборки:",
            reply_markup=runtime.InlineKeyboardMarkup(rows),
        )

    elif data.startswith("gallery_col_add:"):
        _action, value, raw_ids = data.split(":", 2)
        collection_id = int(value) if value.isdigit() else 0
        added = 0
        bulk_post_ids = tuple(
            int(post_id) for post_id in raw_ids.split(",") if post_id.isdigit()
        )[:10]
        for post_id in bulk_post_ids:
            post = await runtime.get_known_post(post_id)
            if post:
                await runtime.add_favorite(user_id, post)
                if await runtime.add_favorite_to_collection(user_id, collection_id, post_id):
                    added += 1
        await query.message.reply_text(f"🗂 Добавлено в коллекцию: {added}.")

    elif data.startswith("gallery_col_new:"):
        raw_ids = data.split(":", 1)[1]
        bulk_post_ids = tuple(
            int(post_id) for post_id in raw_ids.split(",") if post_id.isdigit()
        )[:10]
        if await begin_callback_flow(
            "waiting_bulk_collection_name",
            bulk=(runtime.pending_bulk_posts, bulk_post_ids),
        ) is None:
            return
        await query.message.reply_text(
            "Введите название новой коллекции для этой подборки:",
            reply_markup=runtime.get_cancel_keyboard("library"),
        )

    elif data == "settings_spoiler":
        values = ["off", "explicit", "all"]
        settings = await runtime.mutate_user_settings(
            user_id,
            lambda current: {
                "spoiler_mode": values[
                    (values.index(current["spoiler_mode"]) + 1) % len(values)
                ]
            },
        )
        labels = {"off": "выключены", "explicit": "только explicit", "all": "для всех медиа"}
        await query.message.reply_text(
            f"🙈 Спойлеры: {labels[settings['spoiler_mode']]}",
            reply_markup=await runtime.get_user_settings_keyboard(user_id),
        )

    elif data == "sub_digest_send":
        claim_token, posts = await runtime.claim_subscription_digest(user_id, 10)
        if not claim_token:
            await query.message.reply_text("📨 Дайджест пока пуст.")
        else:
            claim_open = True
            locks: list[runtime.DigestSubscriptionLockHandle] = []
            lease: runtime.DigestClaimLease | None = None
            try:
                locks = await runtime.acquire_digest_subscription_locks(user_id, posts)
                active_keys = await runtime.get_subscription_digest_claim_keys(
                    user_id, claim_token
                )
                posts = [post for post in posts if runtime.digest_item_key(post) in active_keys]
                lease = runtime.DigestClaimLease(user_id, claim_token)
                if not posts or not await lease.start():
                    await runtime.cancellation_safe_digest_finish(user_id, claim_token, [])
                    claim_open = False
                    await query.message.reply_text("📨 Дайджест пока пуст.")
                    return
                delivery = await runtime.send_digest_posts(
                    query.message, user_id, posts, lease=lease
                )
                if delivery.ambiguous_ids:
                    await runtime.cancellation_safe_digest_finish(
                        user_id,
                        claim_token,
                        delivery.delivered_ids,
                        delivery.ambiguous_ids,
                    )
                else:
                    await runtime.cancellation_safe_digest_finish(
                        user_id, claim_token, delivery.delivered_ids
                    )
                claim_open = False
                total = len(posts)
                delivered_count = len(delivery.delivered_ids)
                runtime.logger.info(
                    "Manual digest result user=%s delivered=%s failed=%s ambiguous=%s",
                    user_id,
                    delivered_count,
                    len(delivery.failed_ids),
                    len(delivery.ambiguous_ids),
                )
                if delivered_count < total:
                    await query.message.reply_text(
                        f"⚠️ Доставлено {delivered_count} из {total} постов. "
                        "Остальные будут повторены позже."
                    )
            except runtime.DigestDeliveryCancelled as exc:
                await runtime.cancellation_safe_digest_finish(
                    user_id,
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
                    await runtime.cancellation_safe_digest_release(user_id, claim_token)
                runtime.release_digest_subscription_locks(locks)

    elif data.startswith("sub_options_"):
        sub_query = await runtime.get_callback_payload("sub_options", data)
        if sub_query:
            await runtime.show_subscription_options(query.message, user_id, sub_query)

    elif data.startswith(runtime.SUBSCRIPTION_OPTION_CALLBACK_PREFIXES):
        parsed_option = runtime.parse_subscription_option_callback(data)
        if parsed_option is None:
            await query.message.reply_text("Настройки подписки устарели.")
            return
        action, token = parsed_option
        sub_query = await runtime.get_callback_payload_by_token("sub_options", token)
        if not sub_query:
            await query.message.reply_text("Настройки подписки устарели.")
            return
        if action == "blacklist":
            if await begin_callback_flow(
                "waiting_subscription_blacklist",
                subscription=(runtime.pending_subscription_options, sub_query),
            ) is None:
                return
            await query.message.reply_text(
                "Введите дополнительные теги чёрного списка через пробел или `-` для сброса.",
                reply_markup=runtime.get_cancel_keyboard("subscriptions"),
            )
            return
        fields = {"rating": ("rating_filter",), "type": ("media_type",), "orientation": ("orientation",), "resolution": ("min_width", "min_height"), "quality": ("quality_mode",), "digest": ("digest_mode",)}
        async with runtime.user_operation_gate.hold(user_id):
            options = runtime.normalize_feature_settings(await runtime.get_subscription_options(user_id, sub_query))
            if action == "rating":
                values = ["all", "s", "q", "e"]
                current = options.get("rating_filter", "all")
                options["rating_filter"] = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
            elif action == "type":
                values = ["all", "images", "animations", "videos"]
                current = options.get("media_type", "all")
                options["media_type"] = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
            elif action == "orientation":
                values = ["any", "portrait", "landscape", "square"]
                current = options.get("orientation", "any")
                options["orientation"] = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
            elif action == "resolution":
                values = [(0, 0), (1280, 720), (1920, 1080), (2560, 1440)]
                current = (int(options.get("min_width", 0)), int(options.get("min_height", 0)))
                choice = values[(values.index(current) + 1) % len(values)] if current in values else values[0] if current in values else values[0]
                options["min_width"], options["min_height"] = choice
            elif action == "quality":
                values = ["auto", "preview", "sample", "original"]
                current = options.get("quality_mode", "auto")
                options["quality_mode"] = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
            else:
                options["digest_mode"] = "instant" if options.get("digest_mode") == "digest" else "digest"
            await runtime.update_subscription_options(user_id, sub_query, {key: options[key] for key in fields[action]})
        await runtime.show_subscription_options(query.message, user_id, sub_query)

    elif data == "stats":
        await runtime.show_user_stats(query.message, user_id)

    elif data == "stats_clear_confirm":
        await query.message.reply_text(
            "Очистить историю поиска и отметки просмотренных постов? Избранное и настройки сохранятся.",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "✅ Очистить",
                    callback_data=callback_issuer.side_effect("stats_clear_do"),
                ),
                runtime.InlineKeyboardButton("Отмена", callback_data="stats"),
            ]]),
        )

    elif data == "stats_clear_do":
        await runtime.clear_user_activity_stats(user_id)
        await query.message.reply_text("✅ Персональная статистика очищена.")

    elif data == "fav_collections":
        await runtime.show_collections(query.message, user_id)

    elif data == "col_create":
        if await begin_callback_flow("waiting_collection_create") is None:
            return
        await query.message.reply_text(
            "Введите название коллекции (до 40 символов):",
            reply_markup=runtime.get_cancel_keyboard("fav_collections"),
        )

    elif data.startswith("col_open_"):
        value = data.replace("col_open_", "", 1)
        if value.isdigit():
            await runtime.show_collection(
                query.message, user_id, int(value), issuer=callback_issuer
            )

    elif data.startswith("col_page_"):
        parts = data.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            await runtime.show_collection(
                query.message,
                user_id,
                int(parts[2]),
                int(parts[3]),
                issuer=callback_issuer,
            )

    elif data.startswith("col_delete_") and not data.startswith("col_delete_do_"):
        value = data.replace("col_delete_", "", 1)
        if value.isdigit():
            await query.message.reply_text(
                "Удалить коллекцию? Посты останутся в общем избранном.",
                reply_markup=runtime.InlineKeyboardMarkup([[
                    runtime.InlineKeyboardButton(
                        "🗑 Удалить",
                        callback_data=callback_issuer.side_effect(
                            f"col_delete_do_{value}"
                        ),
                    ),
                    runtime.InlineKeyboardButton("❌ Отмена", callback_data="fav_collections"),
                ]]),
            )

    elif data.startswith("col_delete_do_"):
        value = data.replace("col_delete_do_", "", 1)
        if value.isdigit():
            await runtime.delete_favorite_collection(user_id, int(value))
            await runtime.show_collections(query.message, user_id)

    elif data.startswith("col_rename_"):
        value = data.replace("col_rename_", "", 1)
        if value.isdigit():
            if await begin_callback_flow(
                f"waiting_collection_rename_{value}"
            ) is None:
                return
            await query.message.reply_text(
                "Введите новое название коллекции:",
                reply_markup=runtime.get_cancel_keyboard("fav_collections"),
            )

    elif data.startswith("fav_col_pick_"):
        value = data.replace("fav_col_pick_", "", 1)
        if value.isdigit():
            await runtime.show_collection_picker(
                query.message, user_id, int(value), issuer=callback_issuer
            )

    elif data.startswith("col_add_"):
        parts = data.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            added = await runtime.add_favorite_to_collection(user_id, int(parts[2]), int(parts[3]))
            await query.message.reply_text(
                "✅ Добавлено в коллекцию." if added else "ℹ️ Пост уже в коллекции или не найден."
            )

    elif data.startswith("col_remove_"):
        parts = data.split("_")
        if len(parts) == 5 and all(part.isdigit() for part in parts[2:]):
            collection_id, post_id, index = map(int, parts[2:])
            await runtime.remove_favorite_from_collection(user_id, collection_id, post_id)
            await runtime.show_collection(
                query.message,
                user_id,
                collection_id,
                index,
                issuer=callback_issuer,
            )

    elif data.startswith("col_export_"):
        value = data.replace("col_export_", "", 1)
        if value.isdigit():
            await runtime.enqueue_collection_zip_export(query.message, user_id, int(value))

    elif data.startswith("zip_cancel_"):
        job_id = data.replace("zip_cancel_", "", 1)
        cancelled = bool(runtime.zip_export_manager) and await runtime.zip_export_manager.cancel_for_user(
            user_id, job_id
        )
        if not cancelled:
            await query.message.reply_text("ℹ️ ZIP-экспорт уже завершён или не найден.")

    elif data.startswith("fav_note_"):
        value = data.replace("fav_note_", "", 1)
        if value.isdigit():
            note_generation = await begin_callback_flow(
                f"waiting_favorite_note_{value}"
            )
            if note_generation is None:
                return
            current = await runtime.get_favorite_note(user_id, int(value))
            if not await runtime.commit_flow_if_current(
                user_id, note_generation, lambda: None
            ):
                return
            await query.message.reply_text(
                "Введите заметку до 1000 символов. Отправьте `-`, чтобы удалить."
                + (f"\n\nСейчас: {current}" if current else ""),
                reply_markup=runtime.get_cancel_keyboard("library"),
            )

    elif data == "fav_gallery":
        await runtime.send_favorites_gallery(query.message, user_id, issuer=callback_issuer)

    elif data == "fav_list":
        await runtime.show_favorites_list(query.message, user_id, edit=False, page=0)

    elif data == "fav_find":
        if await begin_callback_flow("waiting_fav_tag") is None:
            return
        await query.edit_message_text(
            "🔎 Введите теги или слова из заметки для поиска в избранном:\n\n"
            "Пример: `blonde_hair wallpaper`",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("library"),
        )

    elif data == "fav_export":
        await runtime.enqueue_favorites_zip_export(query.message, user_id)

    elif data.startswith("fav_list_page_"):
        page_text = data.replace("fav_list_page_", "", 1)
        if not page_text.isdigit():
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        await runtime.show_favorites_list(query.message, user_id, edit=True, page=int(page_text))

    elif data.startswith("fav_tag_page_"):
        payload = await runtime.get_callback_payload("fav_tag_page", data)
        if not payload or "\n" not in payload:
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        tag_filter, page_text = payload.split("\n", 1)
        if not page_text.isdigit():
            await query.message.reply_text("Не удалось открыть страницу избранного.")
            return
        await runtime.show_favorites_list(
            query.message,
            user_id,
            edit=True,
            page=int(page_text),
            tag_filter=tag_filter,
        )

    elif data == "noop":
        return

    elif data.startswith("post_original_"):
        post_id_text = data.replace("post_original_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return
        post = await runtime.get_known_post(int(post_id_text))
        if not post or not post.get("file_url"):
            post = await runtime.api.get_post_by_id(int(post_id_text))
            if post:
                await runtime.remember_and_cache_post(post)
        if not post:
            await query.message.reply_text("❌ Оригинал недоступен.")
            return
        settings = await runtime.get_user_settings(user_id)
        settings["quality_mode"] = "original"
        await runtime.send_post_media(
            query.message,
            post,
            keyboard=runtime.get_image_keyboard(
                int(post_id_text),
                show_tags_button=runtime.should_show_tags_button(settings),
                side_effect_callback=callback_issuer,
            ),
            settings=settings,
        )

    elif data == "post_tags_noop":
        return

    elif data.startswith("post_tags_page_"):
        payload = data.replace("post_tags_page_", "", 1)
        try:
            post_id_text, page_text = payload.rsplit("_", 1)
            post_id, page = int(post_id_text), int(page_text)
        except (TypeError, ValueError):
            await query.message.reply_text("❌ Не удалось открыть страницу тегов.")
            return
        await runtime.send_full_post_tags(
            query.message,
            post_id,
            user_id,
            callback_issuer,
            page=page,
            edit=True,
        )

    elif data.startswith("post_tags_"):
        post_id_text = data.replace("post_tags_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return
        await runtime.send_full_post_tags(
            query.message, int(post_id_text), user_id, callback_issuer
        )

    elif data == "settings":
        settings = await runtime.get_user_settings(user_id)
        caption_enabled = (
            "✅ Включено" if settings.get(
                "show_caption", True) else "❌ Выключено"
        )

        await query.edit_message_text(
            "Главная → Настройки\n\n"
            f"Подписи к постам: {caption_enabled}\n\n"
            "Здесь можно настроить внешний вид постов, подборки и качество медиа.",
            reply_markup=runtime.get_settings_keyboard(runtime.normalize_feature_settings(settings)),
            parse_mode="Markdown",
        )

    elif data == "settings_caption":
        settings = await runtime.get_user_settings(user_id)
        text = runtime.build_caption_settings_text(settings)
        keyboard = await runtime.get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception as e:
            runtime.logger.error(f"Error in settings_caption: {e}")
            await query.message.reply_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )

    elif data == "settings_gallery":
        settings = runtime.normalize_feature_settings(await runtime.get_user_settings(user_id))
        await query.edit_message_text(
            runtime.gallery_settings_text(settings),
            reply_markup=runtime.get_gallery_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "settings_quality":
        settings = runtime.normalize_feature_settings(await runtime.get_user_settings(user_id))
        await query.edit_message_text(
            runtime.quality_settings_text(settings),
            reply_markup=runtime.get_quality_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data.startswith("gallery_cycle_") or data.startswith("gallery_size_"):
        def mutate_gallery(current):
            if data == "gallery_cycle_sort":
                values = ["random", "new", "popular"]
                return {"gallery_sort": values[(values.index(current["gallery_sort"]) + 1) % len(values)]}
            if data == "gallery_cycle_rating":
                values = ["all", "s", "q", "e"]
                return {"rating_filter": values[(values.index(current["rating_filter"]) + 1) % len(values)]}
            if data == "gallery_cycle_type":
                values = ["all", "images", "animations", "videos"]
                return {"media_type": values[(values.index(current["media_type"]) + 1) % len(values)]}
            if data == "gallery_cycle_orientation":
                values = ["any", "portrait", "landscape", "square"]
                return {"orientation": values[(values.index(current["orientation"]) + 1) % len(values)]}
            if data == "gallery_size_down":
                return {"gallery_size": max(2, current["gallery_size"] - 1)}
            return {"gallery_size": min(10, current["gallery_size"] + 1)}

        settings = await runtime.mutate_user_settings(user_id, mutate_gallery)
        await query.edit_message_text(
            runtime.gallery_settings_text(settings),
            reply_markup=runtime.get_gallery_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "gallery_resolution":
        if await begin_callback_flow("waiting_gallery_resolution") is None:
            return
        await query.edit_message_text(
            "Введите минимальное разрешение как `ширинаxвысота`, например `1920x1080`. Для сброса: `0x0`.",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("settings_gallery"),
        )

    elif data == "quality_cycle_mode" or data.startswith("quality_max_"):
        def mutate_quality(current):
            if data == "quality_cycle_mode":
                values = ["auto", "preview", "sample", "original"]
                return {"quality_mode": values[(values.index(current["quality_mode"]) + 1) % len(values)]}
            if data == "quality_max_down":
                return {"max_file_mb": max(1, current["max_file_mb"] - 1)}
            return {"max_file_mb": min(50, current["max_file_mb"] + 1)}

        settings = await runtime.mutate_user_settings(user_id, mutate_quality)
        await query.edit_message_text(
            runtime.quality_settings_text(settings),
            reply_markup=runtime.get_quality_settings_keyboard(settings),
            parse_mode="Markdown",
        )

    elif data == "settings_reset":
        await query.edit_message_text(
            "Сбросить все настройки к значениям по умолчанию?\n\n"
            "Библиотека, подписки и чёрный список не будут удалены.",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "✅ Сбросить",
                    callback_data=callback_issuer.side_effect("settings_reset_do"),
                ),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="settings"),
            ]]),
        )

    elif data == "settings_reset_do":
        await runtime.save_user_settings(user_id, runtime.DEFAULT_USER_SETTINGS)
        await query.edit_message_text(
            "✅ Настройки сброшены к значениям по умолчанию!",
            reply_markup=await runtime.get_user_settings_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data == "settings_interface_mode":
        settings = await runtime.mutate_user_settings(
            user_id,
            lambda current: {
                "interface_mode": (
                    "advanced"
                    if current["interface_mode"] == "simple"
                    else "simple"
                )
            },
        )
        label = "расширенный" if settings["interface_mode"] == "advanced" else "простой"
        await query.edit_message_text(
            f"🧭 Режим интерфейса: {label}.\n\n"
            "Нижняя клавиатура обновлена. Расширенный режим показывает быстрый "
            "доступ к подборкам, подпискам и разделу данных.",
            reply_markup=runtime.get_settings_keyboard(settings),
        )
        await query.message.reply_text(
            "Основные кнопки обновлены.",
            reply_markup=runtime.get_persistent_keyboard(settings["interface_mode"]),
        )

    elif data == "settings_pause_subscriptions":
        if await begin_callback_flow("waiting_pause_subscriptions") is None:
            return
        await query.edit_message_text(
            "⏸ На сколько остановить все активные подписки?\n\n"
            "Можно написать в минутах или коротко: `30`, `2ч`, `1д`.\n"
            "Максимум: 7 дней.",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("subscriptions"),
        )

    elif data == "settings_resume_subscriptions":
        resumed_count = await runtime.resume_all_active_subscriptions(user_id)
        await query.edit_message_text(
            "▶️ Подписки возобновлены.\n\n"
            f"Активных подписок: {resumed_count}.",
            reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
        )

    elif data.startswith("toggle_"):
        setting_name = data.replace("toggle_", "")
        if setting_name not in runtime.DEFAULT_CAPTION_SETTINGS:
            await query.message.reply_text("Настройка устарела.")
            return

        def mutate_caption(current):
            current_value = bool(current.get(setting_name, True))
            patch = {setting_name: not current_value}
            if setting_name == "show_caption" and current_value:
                patch.update({
                    "show_search_query": False,
                    "show_subscription_label": False,
                    "show_id": False,
                    "show_score": False,
                    "show_rating": False,
                    "show_tags": False,
                    "show_tags_button": False,
                })
            elif setting_name == "show_caption" and not current_value:
                patch.update({
                    "show_id": True,
                    "show_tags": True,
                    "show_tags_button": True,
                })
            return patch

        settings = await runtime.mutate_user_settings(user_id, mutate_caption)

        text = runtime.build_caption_settings_text(settings)
        keyboard = await runtime.get_caption_settings_keyboard(user_id)

        try:
            await query.edit_message_text(
                text=text, reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception as e:
            runtime.logger.error(f"Error updating toggle: {e}")

    elif data == "sub_add_current":
        saved = await runtime.get_user_query(user_id)
        if saved and saved[0]:
            if await begin_callback_flow(
                f"waiting_sub_interval_{saved[0]}"
            ) is None:
                return
            await query.edit_message_text(
                f"🔔 Подписка на: `{runtime.md_code(saved[0])}`\n\n"
                "Введите интервал: 30 сек, 1 мин, 2 мин, 5 мин, 10 мин, 30 мин, 1 ч. Число без единицы — минуты (до 120).",
                parse_mode="Markdown",
                reply_markup=runtime.get_cancel_keyboard("subscriptions"),
            )
        else:
            await query.message.reply_text(
                "❌ Сначала выполните поиск!",
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
            )

    elif data == "sub_add_new":
        if await begin_callback_flow("waiting_sub_new") is None:
            return
        await query.edit_message_text(
            "🔔 Введите теги для подписки (через пробел):\n\n" "Пример: `anime girl`",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("subscriptions"),
        )

    elif data == "sub_list":
        subscriptions = await runtime.get_all_user_subscriptions(user_id)
        if subscriptions:
            subs_list = []
            for sub_query, interval, is_active, empty_count, next_check_at in subscriptions:
                if not is_active:
                    status = "⏸ остановлена"
                elif empty_count:
                    status = f"🕒 ожидает новые посты, пустых проверок: {empty_count}"
                else:
                    status = "✅ активна"
                subs_list.append(
                    f"• `{runtime.md_code(sub_query)}` - каждые {runtime.format_subscription_interval(interval)}, {status}"
                )

            text = "📋 *Ваши подписки:*\n\n" + "\n".join(subs_list)
        else:
            text = "📋 У вас пока нет подписок."

        for index, chunk in enumerate(runtime.split_markdown_lines(text)):
            send = query.edit_message_text if index == 0 else query.message.reply_text
            await send(chunk, reply_markup=await runtime.get_user_subscriptions_keyboard(user_id), parse_mode="Markdown")

    elif data == "sub_manage":
        subscriptions = await runtime.get_all_user_subscriptions(user_id)
        if not subscriptions:
            await query.edit_message_text(
                "❌ У вас нет подписок.",
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
            )
            return

        keyboard = []
        for sub_query, interval, is_active, empty_count, next_check_at in subscriptions:
            wait_marker = " 🕒" if is_active and empty_count else ""
            status_icon = "✅" if is_active else "⏸"
            toggle_action = "Приостановить" if is_active else "Возобновить"
            keyboard.extend(
                [
                    [
                        runtime.InlineKeyboardButton(
                            f"{status_icon}{wait_marker} {sub_query[:32]}",
                            callback_data="noop",
                        )
                    ],
                    [
                        runtime.InlineKeyboardButton(
                            f"{toggle_action}",
                            callback_data=callback_issuer.payload(
                                "sub_toggle", sub_query
                            ),
                        ),
                        runtime.InlineKeyboardButton(
                            f"⏱ {runtime.format_subscription_interval(interval)}",
                            callback_data=runtime.store_callback_payload("sub_interval", sub_query),
                        ),
                    ],
                    [
                        runtime.InlineKeyboardButton(
                            "🖼 Посты",
                            callback_data=runtime.store_callback_payload("sub_posts", sub_query),
                        ),
                        runtime.InlineKeyboardButton(
                            "🎛 Фильтры",
                            callback_data=runtime.store_callback_payload("sub_options", sub_query),
                        ),
                        runtime.InlineKeyboardButton(
                            "🗑 Удалить",
                            callback_data=runtime.store_callback_payload("sub_remove", sub_query),
                        ),
                    ],
                ]
            )

        keyboard.append(
            [runtime.InlineKeyboardButton("⬅️ К подпискам", callback_data="subscriptions")]
        )

        await query.edit_message_text(
            "⚙️ *Управление подписками*\n\n🕒 значит, что тег временно исчерпан: бот проверяет его реже и вернется к обычному интервалу, когда появится новый пост.",
            reply_markup=runtime.InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data.startswith("sub_interval_"):
        sub_query = await runtime.get_callback_payload("sub_interval", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        if await begin_callback_flow(
            f"waiting_sub_interval_update_{sub_query}"
        ) is None:
            return
        await query.edit_message_text(
            f"⏱ Новый интервал для `{runtime.md_code(sub_query)}`\n\n"
            "Введите интервал: 30 сек, 1 мин, 2 мин, 5 мин, 10 мин, 30 мин, 1 ч. Число без единицы — минуты (до 120).",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("subscriptions"),
        )

    elif data.startswith("sub_posts_"):
        token = data.replace("sub_posts_", "", 1)
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        await runtime.show_subscription_posts_menu(query.message, user_id, sub_query, token)

    elif data.startswith("sub_list_posts_"):
        token = data.replace("sub_list_posts_", "", 1)
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await runtime.show_subscription_posts_menu(
            query.message, user_id, sub_query, token, edit=False
        )

    elif data.startswith("sub_one_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[-1].isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        token = parts[2]
        index = int(parts[3])
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await runtime.send_subscription_post_by_index(
            query.message, user_id, sub_query, index, issuer=callback_issuer
        )

    elif data.startswith("sub_all_"):
        token = data.replace("sub_all_", "", 1)
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await runtime.send_subscription_gallery(
            query.message, user_id, sub_query, token, issuer=callback_issuer
        )

    elif data.startswith("sub_page_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[-1].isdigit():
            await query.message.reply_text("❌ Не удалось открыть пост.")
            return

        token = parts[2]
        index = int(parts[3])
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await runtime.edit_subscription_gallery(
            query, user_id, sub_query, token, index, issuer=callback_issuer
        )

    elif data.startswith("sub_post_del_"):
        parts = data.split("_")
        if len(parts) < 5 or not parts[-1].isdigit() or not parts[-2].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return

        token = parts[3]
        post_id = int(parts[4])
        index = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        sub_query = await runtime.get_callback_payload_by_token("sub_posts", token)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти подписку. Откройте список заново."
            )
            return

        await runtime.remove_subscription_post(user_id, sub_query, post_id)
        await runtime.remove_favorite(user_id, post_id)
        await runtime.edit_subscription_gallery(
            query, user_id, sub_query, token, index, issuer=callback_issuer
        )

    elif data.startswith("sub_toggle_"):
        sub_query = await runtime.get_callback_payload("sub_toggle", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        toggle_result = await runtime.toggle_subscription(user_id, sub_query)
        if toggle_result.status == "not_found":
            await query.edit_message_text(
                "❌ Подписка не найдена.", parse_mode="Markdown"
            )
        elif toggle_result.status in {"active_limit_reached", "total_limit_reached"}:
            await query.edit_message_text(
                (
                    "❌ Сначала удалите лишние подписки до общего лимита."
                    if toggle_result.status == "total_limit_reached"
                    else "❌ Сначала приостановите или удалите одну из активных подписок."
                ),
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )
        else:
            state_text = "запущена" if toggle_result.is_active else "остановлена"
            await query.edit_message_text(
                f"✅ Подписка `{runtime.md_code(sub_query)}` {state_text}.",
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )

    elif data.startswith("sub_create_"):
        payload = await runtime.get_callback_payload("sub_create", data)
        try:
            preview = runtime.json.loads(payload or "{}")
            sub_query = str(preview["query"]).strip()
            interval = int(preview["interval_seconds"]) if "interval_seconds" in preview else runtime.parse_subscription_interval(str(preview["interval"]))
            if interval < runtime.SUBSCRIPTION_MIN_INTERVAL_SECONDS:
                raise ValueError('Interval below minimum')
        except (KeyError, TypeError, ValueError, runtime.json.JSONDecodeError):
            await query.edit_message_text("❌ Предпросмотр устарел. Создайте подписку заново.")
            return
        result = await runtime.add_subscription(user_id, sub_query, interval_seconds=interval)
        await query.edit_message_text(
            await runtime.build_subscription_added_text(sub_query, interval, user_id)
            if result
            else runtime.build_subscription_create_error(result),
            reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
            parse_mode="Markdown",
        )

    elif data.startswith("subscribe_"):
        # Подписка из клавиатуры под изображением
        sub_query = await runtime.get_callback_payload("subscribe", data)
        if not sub_query:
            await query.message.reply_text(
                "❌ Не удалось найти запрос для подписки. Попробуйте выполнить поиск заново.",
                parse_mode="Markdown",
            )
            return

        preview_text, preview_keyboard = runtime.get_subscription_preview(
            sub_query, 10, user_id, issuer=callback_issuer
        )
        await query.message.reply_text(
            preview_text,
            reply_markup=preview_keyboard,
            parse_mode="Markdown",
        )

    elif data.startswith("sub_remove_") and not data.startswith("sub_remove_do_"):
        # Удаление подписки
        sub_query = await runtime.get_callback_payload("sub_remove", data)
        if not sub_query:
            await query.edit_message_text(
                "❌ Не удалось найти подписку для удаления. Откройте список подписок заново.",
                parse_mode="Markdown",
            )
            return

        confirm_callback = callback_issuer.payload("sub_remove_do", sub_query)
        await query.edit_message_text(
            f"Удалить подписку `{runtime.md_code(sub_query)}`?\n\n"
            "Сохранённые посты этой подписки также будут удалены.",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton("🗑 Удалить", callback_data=confirm_callback),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="subscriptions"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("sub_remove_do_"):
        sub_query = await runtime.get_callback_payload("sub_remove_do", data)
        if not sub_query:
            await query.edit_message_text("❌ Подтверждение устарело.")
            return
        async with runtime.digest_subscription_lock(user_id, sub_query):
            success = await runtime.remove_subscription(user_id, sub_query)

        if success:
            await query.edit_message_text(
                f"✅ Подписка на `{runtime.md_code(sub_query)}` удалена.",
                reply_markup=await runtime.get_user_subscriptions_keyboard(user_id),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "Подписка не найдена.", parse_mode="Markdown"
            )
    elif data.startswith("fav_remove_") and not data.startswith("fav_remove_do_"):
        payload_parts = data.replace("fav_remove_", "", 1).split("_")
        post_id_text = payload_parts[0]
        page = int(payload_parts[1]) if len(payload_parts) > 1 and payload_parts[1].isdigit() else 0
        if not post_id_text.isdigit():
            await query.message.reply_text("Не удалось определить пост.")
            return

        await query.message.reply_text(
            f"Удалить пост `{runtime.md_code(post_id_text)}` из избранного?",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        f"fav_remove_do_{post_id_text}_{page}"
                    ),
                ),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="fav_list"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("fav_remove_do_"):
        payload_parts = data.replace("fav_remove_do_", "", 1).split("_")
        post_id_text = payload_parts[0]
        page = int(payload_parts[1]) if len(payload_parts) > 1 and payload_parts[1].isdigit() else 0
        if not post_id_text.isdigit():
            await query.message.reply_text("Не удалось определить пост.")
            return

        removed = await runtime.remove_favorite(user_id, int(post_id_text))
        if removed:
            await runtime.show_favorites_list(query.message, user_id, edit=True, page=page)
        else:
            await query.message.reply_text("Пост не найден в избранном.")

    elif data.startswith("fav_open_"):
        post_id_text = data.replace("fav_open_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        post = await runtime.get_favorite(user_id, int(post_id_text))
        if not post:
            await query.message.reply_text("❌ Пост не найден в избранном.")
            return

        settings = await runtime.get_user_settings(user_id)
        caption = runtime.build_favorites_gallery_caption(post, 0, 1)
        await runtime.send_post_media(
            query.message,
            post,
            caption,
            runtime.get_image_keyboard(
                post["id"],
                show_tags_button=runtime.should_show_tags_button(settings),
                side_effect_callback=callback_issuer,
            ),
            settings=settings,
        )

    elif data == "fav_all":
        await runtime.send_favorites_gallery(query.message, user_id, issuer=callback_issuer)

    elif data.startswith("fav_page_"):
        page_text = data.replace("fav_page_", "", 1)
        if not page_text.isdigit():
            await query.message.reply_text("❌ Не удалось открыть страницу.")
            return

        await runtime.send_favorites_gallery(
            query.message, user_id, int(page_text), issuer=callback_issuer
        )

    elif data.startswith("fav_del_") and not data.startswith("fav_del_do_"):
        parts = data.split("_")
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return

        post_id = int(parts[2])
        index = int(parts[3])
        await query.message.reply_text(
            f"Удалить пост `{post_id}` из избранного?",
            reply_markup=runtime.InlineKeyboardMarkup([[
                runtime.InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=callback_issuer.side_effect(
                        f"fav_del_do_{post_id}_{index}"
                    ),
                ),
                runtime.InlineKeyboardButton("❌ Отмена", callback_data="fav_gallery"),
            ]]),
            parse_mode="Markdown",
        )

    elif data.startswith("fav_del_do_"):
        parts = data.split("_")
        if len(parts) < 5 or not parts[3].isdigit() or not parts[4].isdigit():
            await query.message.reply_text("❌ Не удалось удалить пост.")
            return
        post_id = int(parts[3])
        index = int(parts[4])
        await runtime.remove_favorite(user_id, post_id)
        await runtime.edit_favorites_gallery(query, user_id, index, issuer=callback_issuer)

    elif data.startswith("sub_fav_"):
        payload = data.replace("sub_fav_", "", 1)
        sub_query = ""
        if payload.isdigit():
            post_id_text = payload
        else:
            legacy_payload = await runtime.get_callback_payload("sub_fav", data)
            if not legacy_payload or "\n" not in legacy_payload:
                await query.message.reply_text("❌ Не удалось определить пост подписки.")
                return
            post_id_text, sub_query = legacy_payload.split("\n", 1)

        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост подписки.")
            return

        post_id = int(post_id_text)
        post = await runtime.get_known_post(post_id) or runtime.minimal_post(post_id)
        if not post.get("file_url"):
            runtime.logger.warning(
                "Saving subscription favorite without cached media user=%s post=%s",
                user_id,
                post_id,
            )

        await runtime.add_favorite(user_id, post)
        sub_queries = [sub_query] if sub_query else await runtime.get_subscription_queries_for_post(user_id, post_id)
        for known_sub_query in sub_queries:
            await runtime.add_subscription_post(user_id, known_sub_query, post)

        if sub_queries:
            await query.message.reply_text(
                f"⭐ Пост `{runtime.md_code(post_id)}` добавлен в избранное подписки.",
                reply_markup=runtime.InlineKeyboardMarkup([[
                    runtime.InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{runtime.md_code(post_id)}` добавлен в избранное.",
                reply_markup=runtime.InlineKeyboardMarkup([[
                    runtime.InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
            runtime.logger.warning(
                "Subscription favorite saved without matching subscription user=%s post=%s",
                user_id,
                post_id,
            )

    elif data.startswith("fav_"):
        post_id_text = data.replace("fav_", "", 1)
        if not post_id_text.isdigit():
            await query.message.reply_text("❌ Не удалось определить пост.")
            return

        post_id = int(post_id_text)
        post = await runtime.get_known_post(post_id) or runtime.minimal_post(post_id)
        if not post.get("file_url"):
            runtime.logger.warning(
                "Saving favorite without cached media user=%s post=%s",
                user_id,
                post_id,
            )

        added = await runtime.add_favorite(user_id, post)
        if added:
            await query.message.reply_text(
                f"⭐ Пост `{runtime.md_code(post_id)}` добавлен в избранное.",
                reply_markup=runtime.InlineKeyboardMarkup([[
                    runtime.InlineKeyboardButton(
                        "🗂 В коллекцию",
                        callback_data=callback_issuer.side_effect(
                            f"fav_col_pick_{post_id}"
                        ),
                    )
                ]]),
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text(
                f"⭐ Пост `{runtime.md_code(post_id)}` уже есть в избранном.",
                parse_mode="Markdown",
            )

    elif data.startswith("hist_"):
        history_query = await runtime.get_callback_payload("hist", data)
        if not history_query:
            await query.message.reply_text(
                "❌ Не удалось найти запрос. Откройте историю заново."
            )
            return
        runtime.schedule_background_task(
            context,
            runtime.send_image(
                query.message,
                user_id,
                history_query,
                expected_generation=callback_generation,
            ),
        )

    elif data == "bl_add":
        if await begin_callback_flow("waiting_bl_add") is None:
            return
        await query.edit_message_text(
            "➕ Введите тег для добавления в чёрный список:\n\n"
            "💡 Можно ввести несколько тегов через пробел",
            reply_markup=runtime.get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_remove":
        remove_generation = await begin_callback_flow("waiting_bl_remove")
        if remove_generation is None:
            return
        blacklist = await runtime.get_user_blacklist(user_id)
        if not await runtime.commit_flow_if_current(
            user_id, remove_generation, lambda: None
        ):
            return
        if blacklist:
            ordered_tags = sorted(blacklist)
            visible_tags = ordered_tags[:60]
            translations = await runtime.tag_translation_service.translate_tags(visible_tags)
            tags_list = ", ".join(
                f"`{runtime.md_code(tag)}`"
                + (f" — {runtime.md_text(translations[tag])}" if translations.get(tag) else "")
                for tag in visible_tags
            )
            if len(ordered_tags) > len(visible_tags):
                tags_list += f"\n\n…и ещё {len(ordered_tags) - len(visible_tags)}. Полный список доступен через «Показать»."
            text = f"➖ Введите тег для удаления:\n\nВаши теги: {tags_list}"
        else:
            text = "➖ Ваш чёрный список пуст"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_show":
        entries = await runtime.get_blacklist_entries(user_id)
        if entries:
            translations = await runtime.tag_translation_service.translate_tags(
                [item["tag"] for item in entries], immediate_limit=50
            )
            pages = []
            current = "📋 *Ваш чёрный список:*\n\n"
            for item in entries:
                translation = translations.get(item["tag"], "")
                line = f"• `{runtime.md_code(item['tag'])}`"
                if translation:
                    line += f" — {runtime.md_text(translation)}"
                if item["expires_at"]:
                    line += f" — до {runtime.md_text(item['expires_at'])}"
                line += "\n"
                if len(current) + len(line) > 3900:
                    pages.append(current.rstrip())
                    current = line
                else:
                    current += line
            if current.strip():
                pages.append(current.rstrip())
        else:
            pages = ["📋 Ваш чёрный список пуст"]

        await query.edit_message_text(
            pages[0],
            reply_markup=runtime.get_blacklist_keyboard() if len(pages) == 1 else None,
            parse_mode="Markdown",
        )
        for index, page in enumerate(pages[1:], start=1):
            await query.message.reply_text(
                page,
                reply_markup=runtime.get_blacklist_keyboard() if index == len(pages) - 1 else None,
                parse_mode="Markdown",
            )

    elif data == "bl_temp":
        if await begin_callback_flow("waiting_bl_temp") is None:
            return
        await query.edit_message_text(
            "Введите тег и срок: `tag 2ч`, `tag 1д` или `tag 30` (минуты).",
            parse_mode="Markdown",
            reply_markup=runtime.get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_import":
        if await begin_callback_flow("waiting_bl_import") is None:
            return
        await query.edit_message_text(
            "Отправьте список тегов через пробел, запятую или с новой строки. "
            "Импорт заменит текущий чёрный список.",
            reply_markup=runtime.get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_export":
        entries = await runtime.get_blacklist_entries(user_id)
        content = "\n".join(item["tag"] for item in entries).encode("utf-8")
        document = runtime.io.BytesIO(content)
        document.name = f"blacklist_{user_id}.txt"
        await query.message.reply_document(
            document=document,
            filename=document.name,
            caption=f"Чёрный список: {len(entries)} тегов",
        )

    elif data == "bl_suggest":
        if await begin_callback_flow("waiting_bl_suggest") is None:
            return
        await query.edit_message_text(
            "Введите тег, для которого найти похожие варианты:",
            reply_markup=runtime.get_cancel_keyboard("blacklist"),
        )

    elif data == "bl_presets":
        rows = []
        for preset, tags in runtime.BLACKLIST_PRESETS.items():
            rows.append([
                runtime.InlineKeyboardButton(
                    f"➕ {preset} ({len(tags)})",
                    callback_data=callback_issuer.side_effect(
                        f"bl_preset_add_{preset}"
                    ),
                ),
                runtime.InlineKeyboardButton(
                    "➖",
                    callback_data=callback_issuer.side_effect(
                        f"bl_preset_del_{preset}"
                    ),
                ),
            ])
        rows.append([runtime.InlineKeyboardButton("◀️ Назад", callback_data="blacklist")])
        await query.edit_message_text(
            "🧰 *Готовые наборы чёрного списка*\n\n"
            "Добавление набора не удаляет ваши собственные теги.",
            reply_markup=runtime.InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )

    elif data.startswith("bl_preset_add_"):
        preset = data.replace("bl_preset_add_", "", 1)
        changed = await runtime.apply_blacklist_preset(user_id, preset)
        await query.message.reply_text(f"✅ Добавлено тегов: {changed}.")

    elif data.startswith("bl_preset_del_"):
        preset = data.replace("bl_preset_del_", "", 1)
        changed = await runtime.remove_blacklist_preset(user_id, preset)
        await query.message.reply_text(f"✅ Удалено тегов набора: {changed}.")

    elif data.startswith("bl_quick_"):
        tag = await runtime.get_callback_payload("bl_quick", data)
        if tag:
            added = await runtime.add_to_blacklist(user_id, tag)
            await runtime.safe_query_answer(
                query,
                "Тег добавлен" if added else "Тег уже в чёрном списке",
            )
        else:
            await runtime.safe_query_answer(query, "Кнопка устарела")

    elif data == "back":
        await runtime.invalidate_user_flow(user_id)
        await query.edit_message_text(
            await runtime.build_main_menu_text(user_id),
            reply_markup=await runtime.get_user_main_keyboard(user_id),
        )

    elif data == "help":
        settings = runtime.normalize_feature_settings(await runtime.get_user_settings(user_id))
        await query.edit_message_text(
            "Главная → Помощь\n\n"
            "*Быстрый старт*\n"
            "1. Откройте «🔎 Поиск».\n"
            "2. Отправьте теги через пробел.\n"
            "3. Сохраняйте понравившиеся посты в библиотеку или создавайте подписки.\n\n"
            "*Основные разделы*\n"
            "• *Поиск* — один пост, случайный результат, подборка, конструктор запроса, "
            "история и сохранённые запросы.\n"
            "• *Библиотека* — избранное, коллекции, заметки, рекомендации и список «На потом».\n"
            "• *Подписки* — автоматическая проверка запросов по расписанию. Перед созданием "
            "бот показывает запрос и интервал для подтверждения.\n"
            "• *Чёрный список* — исключает нежелательные теги из поиска, подборок и случайных постов.\n"
            "• *Настройки* — подписи, спойлеры, размер подборок, качество медиа и режим интерфейса.\n"
            "• *Мои данные* — статистика, хранилище и экспорт; доступен в расширенном режиме.\n\n"
            "*Как вводить теги*\n"
            "Разделяйте теги пробелами, а слова внутри одного тега соединяйте `_`. "
            "Чтобы исключить тег, поставьте перед ним `-`.\n"
            "Пример: `blue_hair 1girl -comic`\n\n"
            "*Управление интерфейсом*\n"
            "В простом режиме показаны только основные кнопки. Расширенный режим включает "
            "быстрый доступ к подборкам, подпискам и данным. Переключение находится в настройках.\n"
            "Во время любого ввода используйте кнопку «❌ Отмена» или команду `/cancel`.\n\n"
            "*Команды*\n"
            "`/start` — обновить меню и открыть быстрый старт\n"
            "`/search <теги>` — найти пост\n"
            "`/random` — случайный пост\n"
            "`/gallery <теги>` — создать подборку\n"
            "`/favorites`, `/collections`, `/later` — разделы библиотеки\n"
            "`/subscriptions` — подписки\n"
            "`/presets` — сохранённые запросы\n"
            "`/blacklist` — чёрный список\n"
            "`/settings` — настройки\n"
            "`/stats`, `/storage` — данные пользователя\n"
            "`/tags <запрос>` — подобрать теги\n"
            "`/id <номер>` — открыть пост по ID\n"
            "`/cancel` — отменить текущее действие\n\n"
            "⚠️ Бот предназначен только для пользователей 18+.",
            reply_markup=runtime.get_help_keyboard(settings.get("interface_mode", "simple")),
            parse_mode="Markdown",
        )
