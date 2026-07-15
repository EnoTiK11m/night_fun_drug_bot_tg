import unittest

from bot import build_full_tags_messages, md_code, md_text


class MarkdownEscapingTests(unittest.TestCase):
    def test_md_code_preserves_tags_inside_code_spans(self):
        escaped = md_code("user_zvth2853 girls&#039;_frontline _exilium")

        self.assertEqual(escaped, "user_zvth2853 girls'_frontline _exilium")
        self.assertNotIn(r"\_", escaped)
        self.assertNotIn("&#039;", escaped)

    def test_md_code_removes_backticks_that_break_code_spans(self):
        escaped = md_code("bad`tag")

        self.assertEqual(escaped, "bad'tag")

    def test_md_code_preserves_regular_text(self):
        self.assertEqual(md_code("simple tag"), "simple tag")

    def test_md_text_escapes_legacy_markdown_control_chars(self):
        escaped = md_text("_ * [ ] ( ) ~ ` > # + - = | { } . !")

        self.assertIn(r"\_", escaped)
        self.assertIn(r"\*", escaped)
        self.assertIn(r"\[", escaped)
        self.assertIn(r"\`", escaped)

    def test_md_text_decodes_html_entities_before_escaping(self):
        self.assertEqual(md_text("girls&#039;_frontline"), r"girls'\_frontline")

    def test_full_tags_message_splits_tags_line_by_line(self):
        messages = build_full_tags_messages(
            {"id": 123, "tags": "tag_one girls&#039;_frontline bad`tag"},
            {"tag_one": "первый тег"},
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("• `tag_one`", messages[0])
        self.assertIn("первый тег", messages[0])
        self.assertIn("• `girls'_frontline`", messages[0])
        self.assertIn("• `bad'tag`", messages[0])


if __name__ == "__main__":
    unittest.main()
