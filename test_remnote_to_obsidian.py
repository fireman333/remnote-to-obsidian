#!/usr/bin/env python3
"""Regression tests for remnote_to_obsidian.

Zero dependencies — run with:

    python3 -m unittest test_remnote_to_obsidian -v

Every test here pins a defect that silently lost data in v1.0.0.
"""

import os
import shutil
import tempfile
import unittest

import remnote_to_obsidian as r2o


def convert(lines, rel_path="Note.md", index=None, options=None):
    """Run the phase-1 pipeline over in-memory lines."""
    stats = r2o.Stats()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        _, out = r2o.process_file(path, rel_path, index or {}, stats, options)
    return out, stats


class CodeFenceProtection(unittest.TestCase):
    """v1.0.0 rewrote markdown syntax inside fenced code blocks."""

    def test_code_block_contents_are_untouched(self):
        out, _ = convert([
            "- Example",
            "    - ```python",
            '    - label = "^^not a highlight^^"',
            '    - link = "[text](path.md)"',
            '    - tag = "#[[SomeTag]]"',
            "    - ```",
        ])
        body = "\n".join(out)
        self.assertIn('"^^not a highlight^^"', body)
        self.assertIn('"[text](path.md)"', body)
        self.assertIn('"#[[SomeTag]]"', body)
        self.assertNotIn("==not a highlight==", body)

    def test_conversion_resumes_after_the_closing_fence(self):
        out, _ = convert([
            "- ```",
            "- inside ^^kept^^",
            "- ```",
            "- outside ^^converted^^",
        ])
        body = "\n".join(out)
        self.assertIn("inside ^^kept^^", body)
        self.assertIn("outside ==converted==", body)


class FlashcardHandling(unittest.TestCase):
    """v1.0.0 deleted two marker styles and ignored the other three."""

    CARDS = [
        "- What is the first-line drug for T2DM? >> Metformin",
        "- HbA1c target is {{7%}} for most adults",
        "- Insulin::a hormone from beta cells",
        "- Numbered card >>3.",
        "- List card >>>",
    ]

    def test_preserve_is_the_default_and_loses_nothing(self):
        out, _ = convert(self.CARDS)
        body = "\n".join(out)
        for marker in (">> Metformin", "{{7%}}", "Insulin::a hormone",
                       ">>3.", ">>>"):
            self.assertIn(marker, body)

    def test_anki_mode_converts_rather_than_deletes(self):
        out, stats = convert(self.CARDS,
                             options=r2o.Options(flashcards="anki"))
        body = "\n".join(out)
        self.assertIn("{{c1::7%}}", body)
        self.assertIn("T2DM? :: Metformin", body)
        self.assertIn("Numbered card #card", body)
        self.assertIn("List card #card", body)
        self.assertGreater(stats.flashcards_converted, 0)

    def test_anki_mode_leaves_numbered_clozes_alone(self):
        out, _ = convert(["- already {{c2::numbered}}"],
                         options=r2o.Options(flashcards="anki"))
        self.assertIn("{{c2::numbered}}", "\n".join(out))
        self.assertNotIn("c1::", "\n".join(out))

    def test_strip_mode_reproduces_legacy_behaviour(self):
        out, stats = convert(self.CARDS,
                             options=r2o.Options(flashcards="strip"))
        body = "\n".join(out)
        self.assertNotIn(">>>", body)
        self.assertNotIn(">>3.", body)
        self.assertEqual(stats.flashcard_markers_removed, 2)


class PortalHandling(unittest.TestCase):
    """v1.0.0 deleted Portal blocks leaving no trace they existed."""

    PORTAL = [
        "- Topic",
        "    - Portal ---------------------",
        "        - mirrored content",
        "    - after portal",
    ]

    def test_mark_mode_leaves_an_auditable_callout(self):
        out, _ = convert(self.PORTAL)
        body = "\n".join(out)
        self.assertIn("RemNote Portal removed", body)
        self.assertNotIn("mirrored content", body)
        self.assertIn("after portal", body)

    def test_remove_mode_leaves_nothing(self):
        out, _ = convert(self.PORTAL, options=r2o.Options(portals="remove"))
        body = "\n".join(out)
        self.assertNotIn("Portal", body)
        self.assertIn("after portal", body)


class TemplateDirs(unittest.TestCase):
    """v1.0.0 hard-coded "Dz" so every other template folder was missed."""

    def test_default_still_matches_dz(self):
        self.assertTrue(r2o.is_template_path("../Dz/Summary.md", "Notes",
                                             ("Dz",)))

    def test_additional_dirs_are_honoured(self):
        self.assertFalse(r2o.is_template_path("../Sx/Steps.md", "Notes",
                                              ("Dz",)))
        self.assertTrue(r2o.is_template_path("../Sx/Steps.md", "Notes",
                                             ("Dz", "Sx")))


class ImageFilenames(unittest.TestCase):
    """v1.0.0 kept only the last URL segment, so same-named images collided."""

    URLS = [
        "https://remnote-user-data.s3.amazonaws.com/u/a/image.png",
        "https://remnote-user-data.s3.amazonaws.com/u/b/image.png",
        "https://remnote-user-data.s3.amazonaws.com/u/c/image.png",
    ]

    def test_distinct_urls_get_distinct_filenames(self):
        names = [r2o.url_to_local_filename(u) for u in self.URLS]
        self.assertEqual(len(set(names)), len(self.URLS))

    def test_same_url_is_stable_across_runs(self):
        self.assertEqual(r2o.url_to_local_filename(self.URLS[0]),
                         r2o.url_to_local_filename(self.URLS[0]))

    def test_extension_is_preserved(self):
        self.assertTrue(r2o.url_to_local_filename(self.URLS[0])
                        .endswith(".png"))


class AliasExtraction(unittest.TestCase):
    """v1.0.0 only recognised ordinals 1.-9. and ignored bullet lists."""

    def test_double_digit_ordinals_are_collected(self):
        lines = ["- Note", "    - [Aliases]()"] + [
            f"        {i}. alias{i}" for i in range(1, 12)
        ]
        self.assertEqual(len(r2o.extract_aliases(lines)), 11)

    def test_bullet_aliases_are_collected(self):
        lines = ["- Note", "    - [Aliases]()",
                 "        - first", "        - second"]
        self.assertEqual(r2o.extract_aliases(lines), ["first", "second"])


class FrontmatterParsing(unittest.TestCase):
    """v1.0.0 treated every '- "..."' line in frontmatter as an alias."""

    def _parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return r2o.parse_frontmatter(path)

    def test_aliases_are_read(self):
        fm = self._parse('---\ntitle: "T"\naliases:\n  - "a"\n  - "b"\n---\n')
        self.assertEqual(fm["aliases"], ["a", "b"])

    def test_other_list_keys_are_not_mistaken_for_aliases(self):
        fm = self._parse('---\ntitle: "T"\ntags:\n  - "x"\n  - "y"\n---\n')
        self.assertNotIn("aliases", fm)


class DedupPreservesParentOnlyContent(unittest.TestCase):
    """v1.0.0 rebuilt the parent from the folder listing, destroying any
    content the parent Rem held that no child file contained."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "Cardiology"))
        with open(os.path.join(self.tmp, "Cardiology.md"), "w",
                  encoding="utf-8") as f:
            f.write("---\ntitle: \"Cardiology\"\n---\n\n"
                    "- Cardiology\n"
                    "    - MY OWN SUMMARY — exists only in the parent\n"
                    "    - Heart failure\n"
                    "        - Reduced cardiac output\n")
        with open(os.path.join(self.tmp, "Cardiology", "Heart failure.md"),
                  "w", encoding="utf-8") as f:
            f.write("---\ntitle: \"Heart failure\"\n---\n\n"
                    "- Heart failure\n"
                    "    - Reduced cardiac output\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_parent_only_content_survives_dedup(self):
        stats = r2o.Stats()
        r2o.phase3_dedup(self.tmp, stats)
        with open(os.path.join(self.tmp, "Cardiology.md"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("MY OWN SUMMARY", body)
        self.assertIn("## Notes", body)
        self.assertGreater(stats.dedup_lines_preserved, 0)

    def test_aggregated_child_content_is_still_removed(self):
        r2o.phase3_dedup(self.tmp, r2o.Stats())
        with open(os.path.join(self.tmp, "Cardiology.md"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("Reduced cardiac output", body)
        self.assertIn("[[Cardiology/Heart failure|Heart failure]]", body)


class HighlightConversion(unittest.TestCase):
    def test_double_caret_becomes_obsidian_highlight(self):
        stats = r2o.Stats()
        self.assertEqual(r2o.convert_highlights("a ^^b^^ c", stats),
                         "a ==b== c")

    def test_single_caret_superscript_is_left_alone(self):
        stats = r2o.Stats()
        self.assertEqual(r2o.convert_highlights("Ca^2+^ level", stats),
                         "Ca^2+^ level")


if __name__ == "__main__":
    unittest.main()
