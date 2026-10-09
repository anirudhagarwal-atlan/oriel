"""HTTP dispatch guards with fake openers; no sockets or native runtimes."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from contextbase.rystic import RysticClient, linear_issues, stripe_pages
from contextbase.study_binding import _hash
from contextbase.study_budget import BudgetError
from contextbase.study_runtime import NativeStudyRuntime
import test_study_runtime as runtime_fixtures


class Response:
    status = 200

    def __init__(self, value):
        self.raw = json.dumps(value).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, limit):
        return self.raw[:limit]


class Opener:
    def __init__(self, *values, error=None):
        self.responses = [Response(value) for value in values]
        self.calls = []
        self.error = error

    def open(self, request, timeout):
        self.calls.append({"request": request, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.responses[len(self.calls) - 1]


class RysticRequestGuardTests(unittest.TestCase):
    def client(self, *values, error=None):
        opener = Opener(*values, error=error)
        with patch("contextbase.rystic.build_opener", return_value=opener):
            client = RysticClient("http://127.0.0.1:18981")
        self.assertIsNone(client.request_guard)
        return client, opener

    def test_default_behavior_and_trace_shape_stay_unchanged(self):
        client, opener = self.client({"data": []})
        self.assertEqual(client.request("GET", "/v1/customers"), {"data": []})
        self.assertEqual(opener.calls[0]["timeout"], 15)
        self.assertTrue(opener.responses[0].closed)
        self.assertEqual(set(client.trace[0]), {"method", "path", "status", "request_body_bytes",
                                               "response_bytes", "elapsed_seconds"})

    def test_each_dispatch_rechecks_remaining_time_and_clamps_socket_timeout(self):
        client, opener = self.client({}, {}, {})
        remaining = iter((30, 2.5, .001))
        checks = []

        def guard():
            checks.append(len(opener.calls))
            return next(remaining)

        client.request_guard = guard
        for _ in range(3):
            client.request("GET", "/v1/customers")
        self.assertEqual(checks, [0, 1, 2])
        self.assertEqual([call["timeout"] for call in opener.calls], [15, 2.5, .001])
        self.assertEqual(len(client.trace), 3)

    def test_guard_refusal_has_no_opener_call_or_dispatched_trace(self):
        client, opener = self.client({})
        failure = BudgetError("study_deadline")

        def refuse():
            raise failure

        client.request_guard = refuse
        with self.assertRaises(BudgetError) as caught:
            client.request("POST", "/v1/customers", form={"metadata[account_id]": "synthetic-account"})
        self.assertIs(caught.exception, failure)
        self.assertEqual(opener.calls, [])
        self.assertEqual(client.trace, [])

    def test_invalid_guard_values_cannot_dispatch(self):
        for value in (None, 0, -1, True, False, "1", float("nan"), float("inf"), 10**400):
            with self.subTest(value=type(value).__name__):
                client, opener = self.client({})
                client.request_guard = lambda: value
                with self.assertRaisesRegex(ValueError, "positive finite"):
                    client.request("GET", "/v1/customers")
                self.assertEqual(opener.calls, [])
                self.assertEqual(client.trace, [])
        client, opener = self.client({})
        client.request_guard = "not a function"
        with self.assertRaisesRegex(ValueError, "callable"):
            client.request("GET", "/v1/customers")
        self.assertEqual(opener.calls, [])

    def test_guard_timeout_is_not_reported_as_a_dispatched_network_failure(self):
        client, opener = self.client({})

        def refuse():
            raise TimeoutError("Authored pre-dispatch refusal")

        client.request_guard = refuse
        with self.assertRaises(TimeoutError):
            client.request("GET", "/v1/customers")
        self.assertEqual(opener.calls, [])
        self.assertEqual(client.trace, [])

    def test_dispatched_timeout_still_has_one_actual_request_trace(self):
        client, opener = self.client(error=TimeoutError("Authored socket timeout"))
        client.request_guard = lambda: .2
        with self.assertRaisesRegex(ValueError, "timed out"):
            client.request("GET", "/v1/customers")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0]["timeout"], .2)
        self.assertEqual(len(client.trace), 1)
        self.assertIsNone(client.trace[0]["status"])

    def test_stripe_pagination_stops_before_next_page_after_expiry(self):
        client, opener = self.client({"data": [{"id": "customer-synthetic-1"}], "has_more": True},
                                     {"data": [{"id": "customer-synthetic-2"}], "has_more": False})
        remaining = iter((1., 0.))
        client.request_guard = lambda: next(remaining)
        with self.assertRaisesRegex(ValueError, "positive finite"):
            stripe_pages(client, "customers")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(client.trace), 1)
        self.assertIn("limit=100", opener.calls[0]["request"].full_url)

    def test_linear_pagination_stops_before_next_graphql_dispatch(self):
        client, opener = self.client({"data": {"issues": {"nodes": [{"id": "issue-synthetic-1"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-synthetic-1"}}}})

        def guard():
            if opener.calls:
                raise BudgetError("operation_duration_exceeded")
            return 2

        client.request_guard = guard
        with self.assertRaisesRegex(BudgetError, "operation_duration_exceeded"):
            linear_issues(client)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0]["request"].get_method(), "POST")
        self.assertEqual(len(client.trace), 1)


class RuntimeRequestGuardTests(unittest.TestCase):
    setUpClass = classmethod(runtime_fixtures.StudyRuntimeTests.setUpClass.__func__)
    setUp = runtime_fixtures.StudyRuntimeTests.setUp
    rehash_protocol = runtime_fixtures.StudyRuntimeTests.rehash_protocol
    pair_factory = runtime_fixtures.StudyRuntimeTests.pair_factory
    construct = runtime_fixtures.StudyRuntimeTests.construct

    def test_active_guard_is_installed_on_later_constructed_clients(self):
        calls = []

        def guard():
            calls.append("checked")
            return 1

        self.runtime.set_request_guard(guard)
        self.runtime.setup_source("batch_001")
        self.assertIs(self.pair.stripe.request_guard, guard)
        self.assertIs(self.pair.linear.request_guard, guard)
        self.assertEqual(calls, [], "Installing a guard must not invoke it or read sources")
        self.assertEqual((self.pair.verifications, self.pair.reads, self.pair.writes), (0, [], []))

    def test_update_and_clear_forward_to_every_existing_pair(self):
        protocol, assignments = deepcopy(self.protocol), deepcopy(self.assignments)
        second = deepcopy(protocol["intents"][0])
        second.update(batch_id="batch_002")
        second["provisioning"]["environment_id"] = "synthetic-pair-002"
        protocol["intents"].append(second)
        protocol["batches"].append({**deepcopy(protocol["batches"][0]), "id": "batch_002"})
        protocol["protocol_sha256"] = _hash({key: value for key, value in protocol.items() if key != "protocol_sha256"})
        assignments["batch_002"] = {"environment_id": "synthetic-pair-002",
                                    "stripe_url": "http://127.0.0.1:18983", "linear_url": "http://127.0.0.1:18984"}
        second_pair = SimpleNamespace(stripe=SimpleNamespace(url=assignments["batch_002"]["stripe_url"]),
                                      linear=SimpleNamespace(url=assignments["batch_002"]["linear_url"]))
        runtime = NativeStudyRuntime(protocol, self.approved, assignments, self.root / "two-pair-runtime",
            pair_factory=lambda stripe, linear: self.pair if stripe == self.pair.stripe.url else second_pair,
            client_factory=runtime_fixtures.ConfigurationClient)
        initial, replacement = (lambda: 3), (lambda: 1)
        runtime.set_request_guard(initial)
        runtime.setup_source("batch_001")
        runtime.set_request_guard(replacement)
        runtime.setup_source("batch_002")
        for pair in (self.pair, second_pair):
            self.assertIs(pair.stripe.request_guard, replacement)
            self.assertIs(pair.linear.request_guard, replacement)
        runtime.set_request_guard(None)
        for pair in (self.pair, second_pair):
            self.assertIsNone(pair.stripe.request_guard)
            self.assertIsNone(pair.linear.request_guard)

    def test_invalid_replacement_does_not_change_installed_guard(self):
        guard = lambda: 1
        self.runtime.set_request_guard(guard)
        self.runtime.setup_source("batch_001")
        with self.assertRaisesRegex(ValueError, "callable"):
            self.runtime.set_request_guard(0)
        self.assertIs(self.pair.stripe.request_guard, guard)
        self.assertIs(self.pair.linear.request_guard, guard)


if __name__ == "__main__":
    unittest.main()
