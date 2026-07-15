import os
import shutil
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import database
from tag_translation import TagTranslationService


class TagTranslationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = f"test_translations_{uuid.uuid4().hex}"
        os.makedirs(self.tempdir)
        self.old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tempdir, "test.db")
        await database.init_db()
        self.service = TagTranslationService()

    async def asyncTearDown(self):
        await self.service.close()
        database.DB_PATH = self.old_db_path
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def test_translates_missing_tags_and_reuses_database_cache(self):
        async def fake_translate(tag):
            return {"blue_hair": "голубые волосы", "artist_name": ""}[tag]

        with patch.object(
            self.service, "_translate_one", AsyncMock(side_effect=fake_translate)
        ) as translate_one:
            first = await self.service.translate_tags(["blue_hair", "artist_name"])
            second = await self.service.translate_tags(["blue_hair", "artist_name"])

        self.assertEqual(first, {"blue_hair": "голубые волосы"})
        self.assertEqual(second, first)
        self.assertEqual(translate_one.await_count, 2)

    async def test_failed_translation_is_deferred_instead_of_retried_immediately(self):
        with patch.object(
            self.service, "_translate_one", AsyncMock(return_value=None)
        ) as translate_one:
            self.assertEqual(await self.service.translate_tags(["unknown_tag"]), {})
            self.assertEqual(await self.service.translate_tags(["unknown_tag"]), {})

        translate_one.assert_awaited_once_with("unknown_tag")


if __name__ == "__main__":
    unittest.main()
