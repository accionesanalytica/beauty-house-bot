"""The product lexicon must come from the repository, not from a developer.

It used to be derived from data/catalog.json, a 5.9 MB dump that .gitignore
excludes as a locally generated artifact. It therefore existed on exactly one
machine. CI and production both built an EMPTY lexicon, and an empty lexicon
does not fail safe: it switches OFF the "customer named a product" blocker, so
"Foxy Cat eye?" looked answerable from a policy document. Nothing reported it.

Two properties are pinned here:

  * the lexicon ships with the code, so local, CI and production read the same
    words from the same file;
  * if it is ever missing or empty, nothing is classified knowledge_only and
    the failure is loud. A blind guard must never look like a passed check.
"""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = REPO_ROOT / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
from routing_policy import (  # noqa: E402
    DATA_CATALOG,
    DATA_KNOWLEDGE_ONLY,
    classify_turn_data_requirement,
)

LEXICON_PATH = REPO_ROOT / "data" / "product_lexicon.txt"


class LexiconShipsWithTheCodeTests(unittest.TestCase):
    def test_the_lexicon_file_exists_in_the_working_tree(self):
        self.assertTrue(
            LEXICON_PATH.exists(),
            "falta data/product_lexicon.txt; regenerar con "
            "`python api/build_product_lexicon.py`",
        )

    def test_the_lexicon_is_tracked_by_git(self):
        """The actual defect: the old source was gitignored.

        A file present locally proves nothing about CI -- only git does.
        """
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "data/product_lexicon.txt"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(
            tracked.returncode, 0,
            "data/product_lexicon.txt no está trackeado por git: CI y "
            "producción se quedarían sin léxico.\n" + tracked.stderr,
        )

    def test_the_lexicon_is_not_excluded_by_gitignore(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "data/product_lexicon.txt"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(ignored.returncode, 0, "gitignore excluye el léxico")

    def test_it_carries_enough_real_names_to_be_useful(self):
        lexicon = app.product_lexicon()
        self.assertGreater(len(lexicon), 100)
        for name in ("isabel", "foxy", "taylor", "twiggy"):
            self.assertIn(name, lexicon)

    def test_generic_words_never_made_it_in(self):
        # A polluted lexicon over-blocks and quietly kills knowledge_only.
        lexicon = app.product_lexicon()
        for generic in ("shoow", "tools", "beauty", "todo", "natural", "negro"):
            self.assertNotIn(generic, lexicon)

    def test_comments_and_blank_lines_are_not_treated_as_words(self):
        lexicon = app.product_lexicon()
        self.assertFalse([word for word in lexicon if word.startswith("#") or not word])


class _ReloadedLexicon:
    """Force one reload of the module-level lexicon and restore it after."""

    def __enter__(self):
        self._cache = app._product_lexicon_cache
        self._status = app._product_lexicon_status
        app._product_lexicon_cache = None
        app._product_lexicon_status = "unloaded"
        return self

    def __exit__(self, *exc):
        app._product_lexicon_cache = self._cache
        app._product_lexicon_status = self._status
        return False


class MissingLexiconIsLoudAndClosedTests(unittest.TestCase):
    """A missing catalog must never degrade into a silently disabled guard."""

    def _load_from(self, path):
        stream = io.StringIO()
        with _ReloadedLexicon(), patch.object(app, "_PRODUCT_LEXICON_PATH", path):
            with redirect_stdout(stream):
                lexicon = app.product_lexicon()
                available = app.product_lexicon_available()
            status = app._product_lexicon_status
        return lexicon, available, status, stream.getvalue()

    def test_a_missing_file_is_reported_not_swallowed(self):
        lexicon, available, status, output = self._load_from(
            REPO_ROOT / "data" / "no-existe.txt")
        self.assertEqual(lexicon, frozenset())
        self.assertFalse(available)
        self.assertEqual(status, "missing")
        self.assertIn("[FredCatalog]", output)
        self.assertIn("status=missing", output)
        self.assertIn("ERROR CRÍTICO", output)

    def test_an_empty_file_counts_as_unavailable_not_as_no_matches(self):
        empty = REPO_ROOT / "data" / ".lexicon-vacio-test.txt"
        empty.write_text("# solo comentarios\n", encoding="utf-8")
        self.addCleanup(empty.unlink)
        _, available, status, output = self._load_from(empty)
        self.assertFalse(available)
        self.assertEqual(status, "empty")
        self.assertIn("ERROR CRÍTICO", output)

    def test_a_healthy_load_reports_ok_and_stays_quiet(self):
        lexicon, available, status, output = self._load_from(LEXICON_PATH)
        self.assertTrue(available)
        self.assertEqual(status, "ok")
        self.assertGreater(len(lexicon), 100)
        self.assertIn("status=ok", output)
        self.assertNotIn("ERROR CRÍTICO", output)


class BlindGuardNeverAllowsKnowledgeOnlyTests(unittest.TestCase):
    def test_an_unavailable_lexicon_blocks_knowledge_only_entirely(self):
        # Without the lexicon the "named a product" check is blind, so "no
        # blocker fired" proves nothing. Keep spending instead of concluding.
        verdict = classify_turn_data_requirement(
            "¿Cuál es el horario?",
            governing_topic="pickups_showroom",
            knowledge_context="- [politicas / showroom] Texto aprobado.",
            product_lexicon=frozenset(),
            product_lexicon_available=False,
        )
        self.assertEqual(verdict["data_required"], DATA_CATALOG)
        self.assertEqual(verdict["reason"], "product_lexicon_unavailable")

    def test_the_same_turn_is_knowledge_only_once_the_lexicon_is_there(self):
        verdict = classify_turn_data_requirement(
            "¿Cuál es el horario?",
            governing_topic="pickups_showroom",
            knowledge_context="- [politicas / showroom] Texto aprobado.",
            product_lexicon=app.product_lexicon(),
            product_lexicon_available=True,
        )
        self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_the_reason_is_visible_in_the_routing_log(self):
        # Whoever reads the logs has to be able to see the guard is off.
        import re

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_routing(
                {"intent": "unknown", "data_required": "catalog",
                 "reason": "product_lexicon_unavailable"},
                live_calls={"count": 0},
            )
        line = stream.getvalue()
        self.assertIn("reason=product_lexicon_unavailable", line)
        self.assertTrue(re.search(r"\[FredRouting\]", line))

    def test_the_named_product_blocker_works_with_the_shipped_lexicon(self):
        # The exact case CI caught: a real product name must never be
        # answerable from a document.
        verdict = classify_turn_data_requirement(
            "Foxy Cat eye?",
            governing_topic="commercial_operations",
            knowledge_context="- [politicas / x] Texto aprobado.",
            product_lexicon=app.product_lexicon(),
            product_lexicon_available=True,
        )
        self.assertNotEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)
        self.assertEqual(verdict["intent"], "product_named")


if __name__ == "__main__":
    unittest.main()
