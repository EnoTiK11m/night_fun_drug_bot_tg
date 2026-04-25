import unittest

from bot import md_code, md_text


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


if __name__ == "__main__":
    unittest.main()
