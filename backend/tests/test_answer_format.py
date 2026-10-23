"""Pure parser checks; can also run with unittest without a database."""
import json
import unittest

from app.modules.ai.answer_format import answer_document, plain_text


def nodes(document):
    yield document
    for child in document.get("content", []):
        yield from nodes(child)


class AnswerFormatTests(unittest.TestCase):
    def document(self, text):
        document = answer_document(text)
        self.assertIsNotNone(document)
        return document

    def test_headings_and_inline_marks_remain_editable_nodes(self):
        document = self.document("# Title\n\n**bold** *italic* ~~removed~~ `literal`")
        heading, paragraph = document["content"]
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["attrs"], {"level": 1})
        marked = [node["marks"][0]["type"] for node in paragraph["content"] if "marks" in node]
        self.assertEqual(marked, ["bold", "italic", "strike", "code"])
        self.assertEqual(plain_text(document), "Title\nbold italic removed literal")

    def test_nested_code_excludes_surrounding_marks_without_losing_other_text(self):
        for markdown in ["**a `b` c**", "***a `b` c***", "~~a `b` c~~",
                         "[a `b` c](https://example.test/path)"]:
            with self.subTest(markdown=markdown):
                document = self.document(markdown)
                content = document["content"][0]["content"]
                self.assertEqual(plain_text(document), "a b c")
                self.assertEqual(content[1], {"type": "text", "text": "b", "marks": [{"type": "code"}]})
                self.assertTrue(content[0].get("marks"))
                self.assertEqual(content[0]["marks"], content[2]["marks"])
                if markdown.startswith("["):
                    self.assertEqual(content[0]["marks"], [{"type": "link", "attrs": {
                        "href": "https://example.test/path"}}])

    def test_duplicate_bold_marks_are_normalized(self):
        for markdown in ["****x****", "__**x**__", "**__x__**"]:
            with self.subTest(markdown=markdown):
                document = self.document(markdown)
                self.assertEqual(document["content"][0]["content"],
                                 [{"type": "text", "text": "x", "marks": [{"type": "bold"}]}])

    def test_ordered_nested_lists_quote_and_rules(self):
        document = self.document("3. third\n4. fourth\n   - nested\n\n> quoted\n\n---")
        ordered, quote, rule = document["content"]
        self.assertEqual(ordered["type"], "orderedList")
        self.assertEqual(ordered["attrs"]["start"], 3)
        self.assertTrue(any(node["type"] == "bulletList" for node in nodes(ordered)))
        self.assertEqual(quote["type"], "blockquote")
        self.assertEqual(rule["type"], "horizontalRule")
        self.assertIn("nested", plain_text(document))

    def test_line_breaks_and_empty_answer_are_valid(self):
        document = self.document("first  \nsecond\nthird")
        self.assertEqual(plain_text(document), "first\nsecond\nthird")
        self.assertEqual(sum(node["type"] == "hardBreak" for node in nodes(document)), 2)
        self.assertEqual(self.document(""), {"type": "doc", "content": [{"type": "paragraph"}]})

    def test_all_four_math_delimiters(self):
        for text, kind, latex in [
            (r"before $x^2$ after", "inlineMath", "x^2"),
            (r"before \(\alpha + 1\) after", "inlineMath", r"\alpha + 1"),
            ("$$\nx+y\n$$", "blockMath", "x+y"),
            ("\\[\nx+y\n\\]", "blockMath", "x+y"),
        ]:
            with self.subTest(text=text):
                document = self.document(text)
                math = [node for node in nodes(document) if node["type"] == kind]
                self.assertEqual(len(math), 1)
                self.assertEqual(math[0]["attrs"], {"latex": latex})
                self.assertIn(latex, plain_text(document))

    def test_display_math_preserves_adjacent_text_and_equation_number(self):
        for delimiter in [("$$", "$$"), (r"\[", r"\]")]:
            with self.subTest(delimiter=delimiter):
                document = self.document("before " + delimiter[0] + "x" + delimiter[1] + " (2) after")
                self.assertEqual([node["type"] for node in document["content"]],
                                 ["paragraph", "blockMath", "paragraph"])
                self.assertEqual(plain_text(document), "before \nx\n (2) after")

    def test_numbered_formula_does_not_swallow_the_next_formula(self):
        for markdown in ["$$x$$ (1)\n$$y$$", "$$\nx\n$$ (1)\n$$y$$",
                         "$$x$$ suffix $$y$$"]:
            with self.subTest(markdown=markdown):
                document = self.document(markdown)
                self.assertEqual([node["attrs"]["latex"] for node in nodes(document)
                                  if node["type"] == "blockMath"], ["x", "y"])
                self.assertIn("(1)" if "(1)" in markdown else "suffix", plain_text(document))

    def test_standalone_multiline_dollar_formula_preserves_blank_lines(self):
        document = self.document("$$\nx\n\ny\n$$\n\nafter")
        self.assertEqual(document["content"][0], {"type": "blockMath", "attrs": {"latex": "x\n\ny"}})
        self.assertEqual(plain_text(document["content"][1]), "after")

    def test_multiline_formula_inside_quote_excludes_quote_markers(self):
        document = self.document("> $$\n> x+y\n> $$")
        self.assertEqual([node["attrs"]["latex"] for node in nodes(document)
                          if node["type"] == "blockMath"], ["x+y"])

    def test_display_math_inside_list_still_starts_with_paragraph(self):
        item = self.document("- $$x$$")["content"][0]["content"][0]
        self.assertEqual([node["type"] for node in item["content"]], ["paragraph", "blockMath"])

    def test_code_preserves_math_markup_html_and_citations_as_text(self):
        code = r"$x$ \(y\) [[p:2]] <script>alert(1)</script>" + "\n"
        document = self.document("```python\n" + code + "```\n\n`$z$ [[p:3]]`")
        block = document["content"][0]
        self.assertEqual(block["type"], "codeBlock")
        self.assertEqual(block["attrs"], {"language": "python"})
        self.assertEqual(plain_text(block), code)
        self.assertFalse(any(node["type"] in {"inlineMath", "blockMath"} for node in nodes(document)))
        inline = document["content"][1]["content"][0]
        self.assertEqual(inline["marks"], [{"type": "code"}])
        self.assertEqual(inline["text"], "$z$ [[p:3]]")

    def test_citations_are_not_rewritten_in_any_context(self):
        document = self.document("**[[p:2]]** `[[p:3]]` $[[p:4]]$")
        self.assertEqual(plain_text(document), "[[p:2]] [[p:3]] [[p:4]]")
        self.assertEqual([node["attrs"]["latex"] for node in nodes(document) if node["type"] == "inlineMath"],
                         ["[[p:4]]"])

    def test_only_explicit_http_links_can_have_link_marks(self):
        document = self.document("[safe](https://example.test/a) [http](http://example.test) "
                                 "[ftp](ftp://example.test) [mail](mailto:a@example.test) "
                                 "[relative](/notes) [bad](javascript:alert(1)) https://example.test")
        links = [mark["attrs"]["href"] for node in nodes(document) for mark in node.get("marks", [])
                 if mark["type"] == "link"]
        self.assertEqual(links, ["https://example.test/a", "http://example.test"])
        self.assertIn("javascript:alert(1)", plain_text(document))

    def test_html_and_images_never_produce_executable_nodes(self):
        document = self.document('<script>alert(1)</script> <img src="https://remote.test/x">\n\n'
                                 '![diagram alt](https://remote.test/secret.png "image title")')
        self.assertFalse(any(node["type"] in {"image", "html", "iframe"} for node in nodes(document)))
        self.assertIn("<script>alert(1)</script>", plain_text(document))
        self.assertIn("diagram alt", plain_text(document))
        self.assertNotIn("secret.png", json.dumps(document))

    def test_currency_escapes_and_unclosed_math_remain_readable(self):
        document = self.document(r"\$2 and $3 and $ unfinished; \(also unfinished")
        self.assertFalse(any(node["type"] in {"inlineMath", "blockMath"} for node in nodes(document)))
        self.assertIn("$2 and $3 and $ unfinished", plain_text(document))
        self.assertIn("also unfinished", plain_text(document))

    def test_oversized_formula_is_literal_with_delimiters(self):
        latex = "x" * 8_193
        for opening, closing in [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")]:
            with self.subTest(opening=opening):
                original = opening + latex + closing
                document = self.document(original)
                self.assertEqual(plain_text(document), original)
                self.assertFalse(any(node["type"] in {"inlineMath", "blockMath"} for node in nodes(document)))
                self.assertEqual(document["content"][0]["type"], "paragraph")
                self.assertEqual(document["content"][0]["content"][0]["marks"], [{"type": "code"}])

    def test_oversized_formula_citation_remains_protected_literal_code(self):
        for opening, closing in [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")]:
            with self.subTest(opening=opening):
                original = opening + "x" * 8_193 + "[[p:1]]" + closing
                document = self.document(original)
                text = document["content"][0]["content"][0]
                self.assertEqual(text, {"type": "text", "text": original, "marks": [{"type": "code"}]})

    def test_complexity_falls_back_without_a_partial_document(self):
        for text in ["a" * 250_001, '"' * 125_000, "**x** " * 2_100,
                     "[" * 20_000, "> " * 30 + "deep answer"]:
            with self.subTest(length=len(text)):
                self.assertIsNone(answer_document(text))

    def test_invalid_unicode_does_not_break_response_serialization(self):
        self.assertIsNone(answer_document("invalid \ud800"))


if __name__ == "__main__":
    unittest.main()
