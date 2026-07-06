import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


def make_update(user_id=1):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
    )


class BlacklistCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_supports_multiple_tags(self):
        update = make_update()
        context = SimpleNamespace(args=["add", "Tag_One", "tag_two"])

        with patch.object(
            bot, "add_to_blacklist", AsyncMock(side_effect=[True, False])
        ) as add:
            await bot.blacklist_command(update, context)

        self.assertEqual(
            [call.args for call in add.await_args_list],
            [(1, "tag_one"), (1, "tag_two")],
        )
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Добавлены", text)
        self.assertIn("Уже были", text)

    async def test_remove_deletes_tag(self):
        update = make_update()
        context = SimpleNamespace(args=["remove", "Blocked_Tag"])

        with patch.object(
            bot, "remove_from_blacklist", AsyncMock(return_value=True)
        ) as remove:
            await bot.blacklist_command(update, context)

        remove.assert_awaited_once_with(1, "blocked_tag")
        self.assertIn("Удалены", update.message.reply_text.await_args.args[0])

    async def test_invalid_arguments_show_usage(self):
        update = make_update()
        context = SimpleNamespace(args=["add"])

        await bot.blacklist_command(update, context)

        self.assertIn("Использование", update.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
