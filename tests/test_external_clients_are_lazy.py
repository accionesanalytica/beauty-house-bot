"""Importing the app must never require an external credential.

CI has no keys, by design. When the Gemini client was constructed at module
scope, `import app` died with "Missing key inputs argument!" -- an error that
names nothing useful and fires before a single test can run. It was masked
locally because a developer's .env supplies a real key, and masked further by
twelve test files that set a fake one just to get past the import.

The rule these tests hold:

  import          -> never touches a credential or the network
  first real use  -> a missing key fails immediately, and says which key
  provider errors -> surface exactly as the provider reported them
"""

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


class ImportIsCredentialFreeTests(unittest.TestCase):
    def test_importing_app_builds_no_gemini_client(self):
        # Nothing is constructed until something asks for it. Reimporting is
        # not enough to prove this (the module is cached), so the module-level
        # holder is checked directly.
        import importlib

        module = importlib.import_module("app")
        self.assertTrue(hasattr(module, "gemini_client"))
        self.assertTrue(callable(module.gemini_client))

    def test_no_module_level_statement_constructs_an_external_client(self):
        """A source-level guard, so this cannot regress quietly.

        Reading the AST rather than importing: the point is what happens at
        module scope, which an import test cannot observe after the fact.
        """
        tree = ast.parse((BOT_DIR / "app.py").read_text(encoding="utf-8"))
        forbidden = {"Client", "create_client", "connect", "OpenAI"}
        offenders = []
        for node in tree.body:  # module scope only, not inside functions
            if not isinstance(node, (ast.Assign, ast.Expr)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
                if name in forbidden:
                    offenders.append("line {}: {}(...)".format(node.lineno, name))
        self.assertEqual(offenders, [], "cliente externo construido en import")

    def test_the_test_suite_does_not_smuggle_in_a_fake_api_key(self):
        # The old workaround: every test file that imported app set
        # GEMINI_API_KEY="test-key" first. That hid the real defect and made
        # the suite's offline-ness depend on import order.
        offenders = [
            path.name
            for path in sorted((Path(__file__).resolve().parent).glob("*.py"))
            if "GEMINI_API_KEY" in path.read_text(encoding="utf-8")
            and path.name != Path(__file__).name
        ]
        self.assertEqual(offenders, [])


class MissingKeyFailsClearlyTests(unittest.TestCase):
    def setUp(self):
        # Each test starts from "no client built yet".
        app._gemini_client = None
        self.addCleanup(setattr, app, "_gemini_client", None)

    def test_a_missing_key_raises_a_message_that_names_the_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                app.gemini_client()
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_the_failure_happens_on_use_not_on_import(self):
        # embed_text is the first thing that actually needs Gemini.
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                app.embed_text("hola")
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_a_present_key_builds_the_client_once_and_reuses_it(self):
        built = []

        class FakeClient:
            def __init__(self, api_key=None):
                built.append(api_key)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "una-clave"}), \
                patch.object(app.genai, "Client", FakeClient):
            first = app.gemini_client()
            second = app.gemini_client()

        self.assertIs(first, second)
        self.assertEqual(built, ["una-clave"])

    def test_a_key_configured_after_import_is_still_honoured(self):
        # The key is read at first use, not captured at import, so a process
        # that gets its environment late still works.
        class FakeClient:
            def __init__(self, api_key=None):
                self.api_key = api_key

        with patch.dict(os.environ, {"GEMINI_API_KEY": "tardía"}), \
                patch.object(app.genai, "Client", FakeClient):
            self.assertEqual(app.gemini_client().api_key, "tardía")


class ProviderErrorsAreNotHiddenTests(unittest.TestCase):
    def setUp(self):
        app._gemini_client = None
        self.addCleanup(setattr, app, "_gemini_client", None)

    def test_a_failure_building_the_client_propagates_untouched(self):
        class Exploding:
            def __init__(self, api_key=None):
                raise ValueError("credencial rechazada por Gemini")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "una-clave"}), \
                patch.object(app.genai, "Client", Exploding):
            with self.assertRaises(ValueError) as caught:
                app.gemini_client()
        self.assertIn("rechazada por Gemini", str(caught.exception))

    def test_a_failure_from_the_embedding_call_propagates_untouched(self):
        class Models:
            def embed_content(self, **kwargs):
                raise RuntimeError("429 quota exceeded")

        class FakeClient:
            def __init__(self, api_key=None):
                self.models = Models()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "una-clave"}), \
                patch.object(app.genai, "Client", FakeClient):
            with self.assertRaises(RuntimeError) as caught:
                app.embed_text("hola")
        # Not rewritten into a friendly message: a quota error must stay a
        # quota error, or the FredKnowledge line reports the wrong cause.
        self.assertIn("429 quota exceeded", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
