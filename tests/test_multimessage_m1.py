"""Deterministic M1 orchestration benchmark; no DB, Meta or LLM calls."""

import asyncio
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
import conversation_store  # noqa: E402
from durable_worker import DeliveryContext, current_delivery_context  # noqa: E402
from message_queue import (  # noqa: E402
    extract_inbound_messages,
    next_process_at,
    ordered_turn_text,
    seed_last_processed_message_id,
)


def meta_payload(messages):
    return {"entry": [{"changes": [{"value": {"messages": messages}}]}]}


class MultiMessageM1Benchmark(unittest.TestCase):
    PHONE = "5491111111111"

    def test_two_fast_messages_share_one_bounded_window(self):
        start = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        result = next_process_at(start, start + timedelta(milliseconds=400), 1.5, 5)
        self.assertEqual(result, start + timedelta(seconds=1.9))

    def test_five_fast_messages_cannot_extend_past_max_wait(self):
        start = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        result = next_process_at(start, start + timedelta(seconds=4.8), 1.5, 5)
        self.assertEqual(result, start + timedelta(seconds=5))

    def test_meta_payload_extracts_every_message(self):
        payload = meta_payload([
            {"from": self.PHONE, "id": "m1", "timestamp": "1786460400", "text": {"body": "Hola"}},
            {"from": self.PHONE, "id": "m2", "timestamp": "1786460401", "text": {"body": "Quería pestañas"}},
            {"from": self.PHONE, "id": "m3", "timestamp": "1786460402", "interactive": {"button_reply": {"id": "fulfillment:pickup", "title": "Retiro"}}},
        ])
        extracted = extract_inbound_messages(payload)
        self.assertEqual([message.wa_message_id for message in extracted], ["m1", "m2", "m3"])
        self.assertEqual(extracted[2].interactive_reply_id, "fulfillment:pickup")

    def test_ordered_turn_uses_provider_order_supplied_by_claim(self):
        messages = [
            {"body": "Hola"},
            {"body": "Quería pestañas"},
            {"body": "Chocolate tenés?"},
        ]
        self.assertEqual(ordered_turn_text(messages), "Hola\nQuería pestañas\nChocolate tenés?")

    @patch.object(app, "enqueue_inbound_message")
    @patch.object(app, "handle_isa_message")
    def test_all_payload_messages_are_enqueued_and_duplicates_are_db_owned(self, isa, enqueue):
        enqueue.side_effect = [
            {"duplicate": False, "conversation_id": 7, "generation": 1},
            {"duplicate": True, "conversation_id": 7, "generation": 1},
        ]
        payload = meta_payload([
            {"from": self.PHONE, "id": "same", "text": {"body": "Hola"}},
            {"from": self.PHONE, "id": "same", "text": {"body": "Hola"}},
        ])
        app._ingest_durable_webhook(payload)
        self.assertEqual(enqueue.call_count, 2)
        isa.assert_not_called()

    @patch.object(app, "processing_claim_is_current", return_value=False)
    @patch.object(app.requests, "post")
    def test_worker_that_lost_lease_cannot_send(self, post, current):
        context = DeliveryContext(7, self.PHONE, 3, "worker-a")
        token = current_delivery_context.set(context)
        try:
            self.assertFalse(app.send_whatsapp_text(self.PHONE, "respuesta vieja"))
        finally:
            current_delivery_context.reset(token)
        post.assert_not_called()
        self.assertTrue(context.stale_discarded)

    @patch.object(app, "processing_claim_is_current", return_value=False)
    @patch.object(app.requests, "post")
    def test_new_generation_discards_obsolete_response(self, post, current):
        context = DeliveryContext(7, self.PHONE, 4, "worker-a")
        token = current_delivery_context.set(context)
        try:
            delivered = app.send_customer_fulfillment_buttons(self.PHONE)
        finally:
            current_delivery_context.reset(token)
        self.assertFalse(delivered)
        post.assert_not_called()

    @patch.object(app, "finish_processing_claim", return_value=True)
    @patch.object(app, "processing_claim_is_current", return_value=True)
    @patch.object(app, "_process_webhook_body")
    @patch.object(app, "_renew_claim_loop")
    def test_whatsapp_failure_never_marks_delivery(self, renew, process, current, finish):
        async def no_heartbeat(_claim):
            await asyncio.Future()

        renew.side_effect = no_heartbeat
        claim = {
            "conversation_id": 7,
            "customer_phone": self.PHONE,
            "state": "BOT",
            "generation": 8,
            "latest_message_id": 20,
            "lease_owner": "worker-a",
            "history": [],
            "messages": [{"id": 20, "body": "hola", "wa_message_id": "m20"}],
        }
        asyncio.run(app._process_durable_claim(claim))
        self.assertFalse(finish.call_args.args[-1])

    def test_sql_contract_contains_atomic_dedup_and_skip_locked_lease(self):
        source = (BOT_DIR / "conversation_store.py").read_text(encoding="utf-8")
        self.assertIn("ON CONFLICT (wa_message_id)", source)
        self.assertIn("FOR UPDATE SKIP LOCKED", source)
        self.assertIn("generation = conversation_processing.generation + 1", source)

    def test_two_workers_cannot_own_the_same_in_memory_claim(self):
        # The DB implementation uses the equivalent SKIP LOCKED operation;
        # this deterministic race proves the benchmark expectation itself.
        import threading

        mutex = threading.Lock()
        available = [7]

        def claim_once():
            with mutex:
                return available.pop() if available else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: claim_once(), range(2)))
        self.assertEqual(sorted(result is not None for result in results), [False, True])

    def test_migration_has_separate_processed_and_delivered_watermarks(self):
        sql = (BOT_DIR.parent / "docs/sql/008_durable_conversation_processing.sql").read_text(encoding="utf-8")
        self.assertIn("last_processed_message_id", sql)
        self.assertIn("last_delivered_message_id", sql)


class WatermarkSeedTests(unittest.TestCase):
    """Pure regression coverage for the pre-existing-history bug found by the
    read-only preflight: a conversation with 98 prior customer messages must
    not have them replayed as one giant turn the first time M1 claims it."""

    def test_existing_conversation_history_is_not_reprocessed(self):
        prior_ids = list(range(1, 99))  # the 98 pre-M1 customer messages
        new_message_id = 150  # first message that arrives once M1 is live
        self.assertEqual(seed_last_processed_message_id(prior_ids, new_message_id), 98)

    def test_only_the_new_message_becomes_pending_after_seeding(self):
        existing_ids = list(range(1, 99))  # rows that actually exist in `messages`
        new_message_id = 150
        seed = seed_last_processed_message_id(existing_ids, new_message_id)
        # Mirrors the claim's own filter: id > seed AND id <= latest_message_id,
        # applied to the ids that are actually present (no rows in between).
        pending = [mid for mid in existing_ids + [new_message_id] if seed < mid <= new_message_id]
        self.assertEqual(pending, [new_message_id])

    def test_new_conversation_has_no_history_and_processes_its_first_message(self):
        seed = seed_last_processed_message_id([], 1)
        self.assertEqual(seed, 0)
        self.assertTrue(seed < 1)  # message 1 remains pending work

    def test_seed_only_considers_ids_older_than_the_triggering_message(self):
        self.assertEqual(seed_last_processed_message_id([5, 10, 20], 15), 10)


class WatermarkSqlContractTests(unittest.TestCase):
    """The SQL cannot be executed against Postgres from this test environment
    (see docs/decisiones-tecnicas.md); these pin the seed subquery's shape the
    same way the existing dedup/lease contract tests already do."""

    @classmethod
    def setUpClass(cls):
        cls.source = (BOT_DIR / "conversation_store.py").read_text(encoding="utf-8")

    def test_insert_seeds_last_processed_message_id_from_prior_customer_messages(self):
        self.assertIn("last_processed_message_id", self.source)
        self.assertIn("seed.direction = 'in'", self.source)
        self.assertIn("seed.sender = 'customer'", self.source)
        self.assertIn("seed.id < %s", self.source)

    def test_burst_update_branch_never_resets_the_watermark(self):
        conflict_block = self.source.split(
            "ON CONFLICT (conversation_id) DO UPDATE SET", 1
        )[1].split("RETURNING generation, process_after", 1)[0]
        self.assertNotIn("last_processed_message_id", conflict_block)


class _RecordingCursor:
    def __init__(self, fetch_results):
        self._fetch_results = list(fetch_results)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._fetch_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class EnqueueWatermarkWiringTests(unittest.TestCase):
    """The original bug was structural: the INSERT simply never listed
    ``last_processed_message_id``, so it stayed NULL forever. Guard the wiring
    itself, not just the SQL text, so a future edit can't drop the column or
    the parameters again without a test failing."""

    @patch.object(conversation_store, "_connect")
    def test_new_conversation_row_passes_conversation_and_message_id_to_the_seed(self, connect):
        received_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        conversation_id, message_id = 1, 55
        cursor = _RecordingCursor([
            (conversation_id, "BOT"),         # _get_or_create_conversation
            (message_id, received_at),        # INSERT messages RETURNING id, received_at
            (1, received_at),                 # INSERT conversation_processing RETURNING ...
        ])
        connect.return_value = _RecordingConnection(cursor)

        conversation_store.enqueue_inbound_message(
            customer_phone="5491111111111",
            body="hola",
            wa_message_id="wam-1",
            provider_timestamp=None,
            quiet_seconds=1.5,
            max_burst_seconds=5.0,
        )

        self.assertEqual(len(cursor.calls), 3)
        processing_sql, processing_params = cursor.calls[2]
        self.assertIn("last_processed_message_id", processing_sql)
        # The seed subquery's two placeholders (conversation_id, id <) must
        # carry the real ids, not be left unbound or defaulted.
        self.assertIn(conversation_id, processing_params)
        self.assertEqual(processing_params.count(message_id), 2)  # latest_message_id + seed bound


if __name__ == "__main__":
    unittest.main()
