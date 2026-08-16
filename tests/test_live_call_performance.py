"""Performance changes to the live path, pinned as behaviour.

Three changes, none of which may alter a single reply:

  1. the Tiendanube credential is cached in-process instead of being re-read
     from Postgres on every request (measured at ~500ms per read),
  2. HTTP requests reuse one pooled session instead of a new TCP+TLS
     handshake each time,
  3. independent lookups are issued together instead of one after another.

What these tests defend is the "none of which may alter a reply" half. Speed
is measured outside; correctness is measured here -- ordering, error handling,
and the one case where caching could genuinely change behaviour (a rotated
credential) must all survive.
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
import tiendanube_credentials as credentials  # noqa: E402
import tiendanube_tools as tools  # noqa: E402


class ParallelFetchTests(unittest.TestCase):
    """Scheduling only: same inputs, same outputs, same order."""

    def test_results_come_back_in_input_order_not_completion_order(self):
        # The slowest item is first, so a naive "append as they finish" would
        # reorder them -- and the caller renders candidates in order.
        def fetch(item):
            time.sleep(0.05 if item == "a" else 0.0)
            return item.upper()

        self.assertEqual(
            app._fetch_in_parallel(fetch, ["a", "b", "c", "d"], "probando"),
            ["A", "B", "C", "D"],
        )

    def test_one_failure_becomes_none_and_never_sinks_the_others(self):
        def fetch(item):
            if item == "b":
                raise RuntimeError("tiendanube caída")
            return item.upper()

        self.assertEqual(
            app._fetch_in_parallel(fetch, ["a", "b", "c"], "probando"),
            ["A", None, "C"],
        )

    def test_a_failure_is_logged_with_the_callers_own_message(self):
        import io
        from contextlib import redirect_stdout

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._fetch_in_parallel(
                lambda item: (_ for _ in ()).throw(ValueError("x")),
                ["a", "b"],
                "verificando candidata Tiendanube",
            )
        self.assertIn("ERROR verificando candidata Tiendanube", stream.getvalue())
        self.assertIn("ValueError", stream.getvalue())

    def test_a_single_item_runs_inline_without_a_thread(self):
        seen = []

        def fetch(item):
            seen.append(threading.current_thread().name)
            return item

        app._fetch_in_parallel(fetch, ["only"], "probando")
        self.assertEqual(seen, [threading.current_thread().name])

    def test_an_empty_list_does_nothing_at_all(self):
        called = []
        self.assertEqual(
            app._fetch_in_parallel(lambda item: called.append(item), [], "probando"), [])
        self.assertEqual(called, [])

    def test_every_item_is_fetched_exactly_once(self):
        seen = []
        lock = threading.Lock()

        def fetch(item):
            with lock:
                seen.append(item)
            return item

        items = list(range(9))
        app._fetch_in_parallel(fetch, items, "probando")
        self.assertEqual(sorted(seen), items)


class CredentialCacheTests(unittest.TestCase):
    def setUp(self):
        credentials.invalidate_tiendanube_configuration()
        self.addCleanup(credentials.invalidate_tiendanube_configuration)

    def test_the_database_is_read_once_and_then_reused(self):
        reads = []

        def fake_authorized():
            reads.append(1)
            return {"store_id": "1", "access_token": "tok"}

        with patch.object(credentials, "_authorized_credential", fake_authorized):
            for _ in range(5):
                configuration = credentials.get_tiendanube_configuration()
        self.assertEqual(len(reads), 1)
        self.assertEqual(configuration["store_id"], "1")
        self.assertEqual(configuration["access_token"], "tok")

    def test_callers_never_share_a_mutable_dict(self):
        with patch.object(
            credentials, "_authorized_credential",
            lambda: {"store_id": "1", "access_token": "tok"},
        ):
            first = credentials.get_tiendanube_configuration()
            first["access_token"] = "manoseado"
            second = credentials.get_tiendanube_configuration()
        self.assertEqual(second["access_token"], "tok")

    def test_invalidating_forces_a_fresh_read(self):
        tokens = iter(["viejo", "nuevo"])

        def fake_authorized():
            return {"store_id": "1", "access_token": next(tokens)}

        with patch.object(credentials, "_authorized_credential", fake_authorized):
            self.assertEqual(
                credentials.get_tiendanube_configuration()["access_token"], "viejo")
            self.assertEqual(
                credentials.get_tiendanube_configuration()["access_token"], "viejo")
            credentials.invalidate_tiendanube_configuration()
            self.assertEqual(
                credentials.get_tiendanube_configuration()["access_token"], "nuevo")

    def test_the_cache_expires_on_its_own(self):
        reads = []

        def fake_authorized():
            reads.append(1)
            return {"store_id": "1", "access_token": "tok"}

        with patch.object(credentials, "_authorized_credential", fake_authorized), \
                patch.object(credentials, "_CREDENTIAL_CACHE_SECONDS", 0.0):
            credentials.get_tiendanube_configuration()
            credentials.get_tiendanube_configuration()
        self.assertEqual(len(reads), 2)

    def test_the_user_agent_still_follows_the_environment_not_the_cache(self):
        with patch.object(
            credentials, "_authorized_credential",
            lambda: {"store_id": "1", "access_token": "tok"},
        ):
            with patch.dict(os.environ, {"TIENDANUBE_USER_AGENT": "primero"}):
                self.assertEqual(
                    credentials.get_tiendanube_configuration()["user_agent"], "primero")
            with patch.dict(os.environ, {"TIENDANUBE_USER_AGENT": "segundo"}):
                self.assertEqual(
                    credentials.get_tiendanube_configuration()["user_agent"], "segundo")


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))


class RotatedCredentialTests(unittest.TestCase):
    """The one case where caching could change behaviour, closed off."""

    def setUp(self):
        credentials.invalidate_tiendanube_configuration()
        self.addCleanup(credentials.invalidate_tiendanube_configuration)

    def test_a_rejected_cached_credential_is_refreshed_and_the_call_succeeds(self):
        tokens = iter(["rotado", "vigente"])
        reads = []

        def fake_configuration():
            token = next(tokens)
            reads.append(token)
            return {"store_id": "1", "access_token": token, "user_agent": "x"}

        def fake_get(url, headers=None, params=None, timeout=None):
            if "rotado" in headers["Authentication"]:
                return Response(401)
            return Response(200, {"ok": True})

        with patch.object(tools, "get_tiendanube_configuration", fake_configuration), \
                patch.object(tools._SESSION, "get", fake_get):
            payload = tools._get("/products/1")

        # The stale credential produced one 401, was dropped, and the retry
        # went out with the real one -- the caller never sees the difference.
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(reads, ["rotado", "vigente"])

    def test_a_genuinely_invalid_credential_still_raises(self):
        # Caching must not turn a real auth failure into an infinite retry or
        # a silent empty answer.
        with patch.object(
            tools, "get_tiendanube_configuration",
            lambda: {"store_id": "1", "access_token": "malo", "user_agent": "x"},
        ), patch.object(tools._SESSION, "get", lambda *a, **k: Response(401)):
            with self.assertRaises(RuntimeError):
                tools._get("/products/1")

    def test_a_normal_response_costs_exactly_one_request(self):
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(url)
            return Response(200, {"ok": True})

        with patch.object(
            tools, "get_tiendanube_configuration",
            lambda: {"store_id": "1", "access_token": "tok", "user_agent": "x"},
        ), patch.object(tools._SESSION, "get", fake_get):
            tools._get("/products/1")
        self.assertEqual(len(calls), 1)


class SessionReuseTests(unittest.TestCase):
    def test_requests_go_through_one_pooled_session(self):
        # Connection reuse is the whole point; a module-level session is what
        # provides it. If this ever reverts to requests.get, every call pays a
        # fresh TCP+TLS handshake again (~120ms each, measured).
        import requests

        self.assertIsInstance(tools._SESSION, requests.Session)
        adapter = tools._SESSION.get_adapter("https://api.tiendanube.com/")
        self.assertGreaterEqual(adapter._pool_maxsize, 4)


if __name__ == "__main__":
    unittest.main()
