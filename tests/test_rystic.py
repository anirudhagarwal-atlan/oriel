"""Local transport boundaries and source-backed refresh correctness; no model calls."""

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from contextbase.rystic import (
    RysticClient, RysticPair, collect_snapshot, linear_issues, local_url,
    save_receipt, snapshot_info, stripe_pages, sync_snapshot,
)
from contextbase.rystic_recipe import NAMESPACE
from contextbase.workspace import Workspace


@contextmanager
def local_server(responses):
    """Serve only ephemeral loopback routes and record requests without logging."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append({"method": self.command, "path": self.path, "body": body,
                             "content_type": self.headers.get("Content-Type")})
            status, headers, value = responses.get(urlsplit(self.path).path, (404, {}, {}))
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class PageClient:
    def __init__(self, pages):
        self.pages = deepcopy(pages)
        self.calls = []

    def request(self, method, path, query=None):
        self.calls.append((method, path, deepcopy(query)))
        return self.pages.pop(0)

    def graphql(self, query, variables=None):
        self.calls.append((query, deepcopy(variables)))
        return self.pages.pop(0)


def issue_page(rows, *, more=False, cursor=None):
    return {"issues": {"nodes": rows, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}


class SourceStripe:
    def __init__(self, pair):
        self.pair = pair
        self.calls = []

    def request(self, method, path, query=None):
        self.calls.append((method, path, deepcopy(query)))
        if self.pair.fail_stripe:
            raise ValueError("Synthetic source fetch failure")
        assert method == "GET"
        resource = path.rsplit("/", 1)[1]
        return {"data": deepcopy(getattr(self.pair, resource)), "has_more": False}


class SourceLinear:
    def __init__(self, pair):
        self.pair = pair
        self.calls = []

    def graphql(self, query, variables=None):
        self.calls.append((query, deepcopy(variables)))
        if self.pair.fail_linear:
            raise ValueError("Synthetic source fetch failure")
        return issue_page(deepcopy(self.pair.issues))


class SnapshotPair:
    """Schema-faithful source fixture; records are intentionally not normalized."""
    def __init__(self):
        self.customers = [
            {"id": "cus_002", "name": "Example Company 002", "metadata": {
                "contextbase_fixture": NAMESPACE, "account_id": "customer_002", "is_internal": "true"}},
            {"id": "cus_001", "name": "Example Company 001", "metadata": {
                "contextbase_fixture": NAMESPACE, "account_id": "customer_001", "is_internal": "false"}},
            {"id": "cus_unrelated", "name": "Unrelated Synthetic Company", "metadata": {}},
        ]
        self.charges = [
            {"id": "ch_002", "customer": "cus_002", "amount": 7000, "amount_refunded": 0,
             "currency": "eur", "paid": True, "captured": True, "status": "succeeded",
             "disputed": False, "created": 1791504000},
            {"id": "ch_001", "customer": "cus_001", "amount": 10001, "amount_refunded": 3333,
             "currency": "USD", "paid": True, "captured": True, "status": "succeeded",
             "disputed": False, "created": 1791504000},
            {"id": "ch_unrelated", "customer": "cus_unrelated", "amount": 100,
             "amount_refunded": 0, "currency": "usd", "paid": True, "captured": True,
             "status": "succeeded", "disputed": False, "created": 1791504000},
        ]
        self.issues = [
            {"id": "issue_001", "identifier": "SYN-1", "title": "Synthetic investigation",
             "description": f"contextbase_account_id=customer_001\ncontextbase_fixture={NAMESPACE}",
             "priority": 1, "state": {"id": "state_1", "name": "Started", "type": "started"},
             "assignee": {"id": "user_1", "name": "Synthetic Operator"},
             "updatedAt": "2026-10-09T00:00:00Z"},
            {"id": "issue_unrelated", "description": "Unrelated synthetic issue"},
        ]
        self.fail_stripe = self.fail_linear = False
        self.verifications = 0
        self.stripe = SourceStripe(self)
        self.linear = SourceLinear(self)

    def verify(self):
        self.verifications += 1
        return {"stripe": {"product": "stripe-twin", "version": "0.0.8"},
                "linear": {"product": "linear-twin", "version": "0.0.1"}}


class RysticTransportTests(unittest.TestCase):
    def test_only_numeric_loopback_roots_with_explicit_ports_are_allowed(self):
        self.assertEqual(local_url("http://127.0.0.1:8877/"), "http://127.0.0.1:8877")
        self.assertEqual(local_url("http://[::1]:8877"), "http://[::1]:8877")
        for value in ("https://127.0.0.1:8877", "http://localhost:8877", "http://127.0.0.2:8877",
                      "http://example.invalid:8877", "http://127.0.0.1", "http://127.0.0.1:0",
                      "http://127.0.0.1:65536", "http://127.0.0.1:8877/path",
                      "http://127.0.0.1:8877?x=1", "http://127.0.0.1:8877#fragment",
                      "http://synthetic:placeholder@127.0.0.1:8877", "file:///tmp/synthetic", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RysticClient(value)
        with self.assertRaisesRegex(ValueError, "separate"):
            RysticPair("http://127.0.0.1:8877", "http://127.0.0.1:8877/")

    def test_shell_proxies_are_bypassed_and_redirects_are_never_followed(self):
        with local_server({"/leak": (200, {}, {"unexpected": True})}) as (proxy_url, proxy_requests):
            responses = {"/v1/customers": (200, {}, {"data": [], "has_more": False}),
                         "/v1/charges": (302, {"Location": proxy_url + "/leak"}, {})}
            with local_server(responses) as (url, requests), patch.dict("os.environ", {
                "http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
                "https_proxy": proxy_url, "HTTPS_PROXY": proxy_url,
                "all_proxy": proxy_url, "ALL_PROXY": proxy_url,
                "no_proxy": "", "NO_PROXY": "",
            }):
                client = RysticClient(url)
                self.assertEqual(client.request("GET", "/v1/customers"), {"data": [], "has_more": False})
                with self.assertRaisesRegex(ValueError, "redirects are refused"):
                    client.request("GET", "/v1/charges")
                self.assertEqual([request["path"] for request in requests], ["/v1/customers", "/v1/charges"])
                self.assertEqual(proxy_requests, [])
                self.assertEqual(len(client.trace), 2)

    def test_pinned_product_and_version_are_verified_before_source_reads(self):
        stripe = {"product": "stripe-twin", "version": "0.0.8", "model_hash": "synthetic_hash",
                  "unexpected_private_field": "synthetic_omitted_value"}
        linear = {"product": "linear-twin", "version": "0.0.1"}
        stripe_routes = {"/_rystic/version": (200, {}, stripe)}
        linear_routes = {"/_rystic/version": (200, {}, linear)}
        with local_server(stripe_routes) as (stripe_url, stripe_requests), \
                local_server(linear_routes) as (linear_url, linear_requests):
            pair = RysticPair(stripe_url, linear_url)
            identities = pair.verify()
            self.assertNotIn("unexpected_private_field", identities["stripe"])
            for name, value in (("version", "0.0.9"), ("product", "other-twin")):
                with self.subTest(field=name):
                    stripe_routes["/_rystic/version"] = (200, {}, {**stripe, name: value})
                    with self.assertRaisesRegex(ValueError, "pinned stripe-twin 0.0.8"):
                        pair.verify()
            stripe_routes["/_rystic/version"] = (200, {}, stripe)
            linear_routes["/_rystic/version"] = (200, {}, {**linear, "version": "0.0.2"})
            with self.assertRaisesRegex(ValueError, "pinned linear-twin 0.0.1"):
                pair.verify()
            self.assertTrue(all(request["path"] == "/_rystic/version" for request in stripe_requests + linear_requests))

    def test_paths_control_writes_and_request_bodies_are_bounded(self):
        routes = {"/v1/customers": (200, {}, {"id": "cus_synthetic"}),
                  "/graphql": (200, {}, {"data": {"synthetic": True}})}
        with local_server(routes) as (url, requests):
            client = RysticClient(url)
            for method, path in (("DELETE", "/v1/customers"), ("GET", "//example.invalid/path"),
                                 ("GET", "/v1/../outside"), ("POST", "/_rystic/version"),
                                 ("POST", "/_rystic/reset"), ("GET", "/v1/customers?redirect=1")):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    client.request(method, path)
            with self.assertRaises(ValueError):
                client.request("GET", "/v1/customers", form={"name": "Synthetic"})
            with self.assertRaises(ValueError):
                client.request("POST", "/v1/customers", form={}, json_body={})
            self.assertEqual(requests, [])
            client.request("POST", "/v1/customers", form={"name": "Synthetic & Example"})
            self.assertEqual(requests[-1]["body"], b"name=Synthetic+%26+Example")
            self.assertEqual(client.graphql("query Synthetic { synthetic }", {"count": 1}), {"synthetic": True})
            self.assertEqual(json.loads(requests[-1]["body"])["variables"], {"count": 1})
            self.assertEqual(set(client.trace[0]), {"method", "path", "status", "request_body_bytes",
                                                   "response_bytes", "elapsed_seconds"})

    def test_invalid_oversized_and_error_responses_do_not_leak_response_body(self):
        routes = {"/v1/customers": (200, {}, b'{"x":1,"x":2}')}
        with local_server(routes) as (url, requests):
            client = RysticClient(url)
            with self.assertRaisesRegex(ValueError, "duplicate JSON"):
                client.request("GET", "/v1/customers")
            routes["/v1/customers"] = (200, {}, b'{"x":NaN}')
            with self.assertRaisesRegex(ValueError, "non-finite JSON"):
                client.request("GET", "/v1/customers")
            routes["/v1/customers"] = (200, {}, {"long": "x" * 100})
            with patch("contextbase.rystic.MAX_RESPONSE_BYTES", 16), self.assertRaisesRegex(ValueError, "bounded size"):
                client.request("GET", "/v1/customers")
            routes["/v1/customers"] = (500, {}, {"detail": "synthetic_body_should_not_escape"})
            with self.assertRaisesRegex(ValueError, "HTTP 500") as error:
                client.request("GET", "/v1/customers")
            self.assertNotIn("synthetic_body_should_not_escape", str(error.exception))
            routes["/graphql"] = (200, {}, {"data": {}, "errors": [{"message": "synthetic_body_should_not_escape"}]})
            with self.assertRaisesRegex(ValueError, "GraphQL operation failed") as error:
                client.graphql("query Synthetic { synthetic }")
            self.assertNotIn("synthetic_body_should_not_escape", str(error.exception))


class RysticPaginationTests(unittest.TestCase):
    def test_stripe_advances_cursor_and_preserves_original_query(self):
        client = PageClient([{"data": [{"id": "cus_1"}], "has_more": True},
                             {"data": [{"id": "cus_2"}], "has_more": False}])
        self.assertEqual(stripe_pages(client, "customers", query={"email": "synthetic@example.invalid"}),
                         [{"id": "cus_1"}, {"id": "cus_2"}])
        self.assertEqual(client.calls[1][2], {"email": "synthetic@example.invalid", "limit": 100, "starting_after": "cus_1"})

    def test_stripe_rejects_duplicates_nonadvance_and_incomplete_envelopes(self):
        cases = [
            [{"data": [{"id": "cus_1"}, {"id": "cus_1"}], "has_more": False}],
            [{"data": [{"id": "cus_1"}], "has_more": True}, {"data": [{"id": "cus_1"}], "has_more": False}],
            [{"data": [], "has_more": True}], [{"data": [], "has_more": "false"}],
            [{"data": [{}], "has_more": False}], [{"data": []}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                stripe_pages(PageClient(pages), "customers")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            stripe_pages(PageClient([]), "unapproved")
        pages = [{"data": [{"id": "cus_1"}], "has_more": True}]
        with patch("contextbase.rystic.MAX_PAGES", 1), self.assertRaisesRegex(ValueError, "page budget"):
            stripe_pages(PageClient(pages), "customers")

    def test_linear_advances_cursor_and_rejects_duplicates_or_nonadvance(self):
        client = PageClient([issue_page([{"id": "issue_1"}], more=True, cursor="next"),
                             issue_page([{"id": "issue_2"}])])
        self.assertEqual(linear_issues(client), [{"id": "issue_1"}, {"id": "issue_2"}])
        self.assertEqual(client.calls[1][1], {"after": "next"})
        self.assertIn("includeArchived: true", client.calls[0][0])
        cases = [
            [issue_page([{"id": "issue_1"}, {"id": "issue_1"}])],
            [issue_page([{"id": "issue_1"}], more=True, cursor="next"), issue_page([{"id": "issue_1"}])],
            [issue_page([], more=True, cursor="next")],
            [issue_page([{"id": "issue_1"}], more=True)],
            [issue_page([{"id": "issue_1"}], more=True, cursor="next"),
             issue_page([{"id": "issue_2"}], more=True, cursor="next")],
            [{"issues": {"nodes": [], "pageInfo": {"hasNextPage": "false"}}}],
            [issue_page([{}])],
        ]
        for pages in cases:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                linear_issues(PageClient(pages))
        with patch("contextbase.rystic.MAX_PAGES", 1), self.assertRaisesRegex(ValueError, "page budget"):
            linear_issues(PageClient([issue_page([{"id": "issue_1"}], more=True, cursor="next")]))


class RysticSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.pair = SnapshotPair()

    def saved_state(self):
        with self.workspace._connect() as conn:
            tables = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                      for name in ("stripe.customers", "stripe.charges", "linear.issues")}
            snapshots = conn.execute("SELECT * FROM _contextbase.rystic_snapshots ORDER BY sequence").fetchall()
            imports = conn.execute("SELECT * FROM _contextbase.imports ORDER BY imported_at, source, target").fetchall()
        return tables, snapshots, imports

    def test_collection_filters_cohort_normalizes_and_checks_explicit_identity(self):
        tables, provenance = collect_snapshot(self.pair, NAMESPACE)
        self.assertEqual([row["customer_id"] for row in tables["stripe.customers"]], ["cus_001", "cus_002"])
        self.assertEqual(len(tables["stripe.charges"]), 2)
        self.assertEqual(tables["stripe.charges"][0]["amount_refunded_cents"], 3333)
        self.assertEqual(tables["stripe.charges"][0]["currency"], "usd")
        self.assertEqual(tables["stripe.charges"][0]["created_at"], "2026-10-09T00:00:00+00:00")
        self.assertEqual(len(tables["linear.issues"]), 1)
        self.assertEqual(tables["linear.issues"][0]["owner_name"], "Synthetic Operator")
        self.assertTrue(provenance["synthetic"])
        self.assertEqual(provenance["namespace"], NAMESPACE)
        self.assertEqual(self.pair.verifications, 1)
        self.assertIn("sequential API reads", provenance["consistency"])

    def test_refresh_detects_actual_data_change_and_preserves_other_local_models(self):
        self.assertIsNone(snapshot_info(self.workspace))
        first = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertTrue(first["data_changed"])
        self.assertIsNone(first["previous_snapshot_id"])
        self.assertEqual(first["rows"], {"stripe.customers": 2, "stripe.charges": 2, "linear.issues": 1})
        self.workspace.transform("synthetic_model", "SELECT 42 AS answer")
        # A different upstream ordering must not change snapshot content identity.
        self.pair.customers.reverse()
        self.pair.charges.reverse()
        second = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertFalse(second["data_changed"])
        self.assertEqual(first["data_sha256"], second["data_sha256"])
        self.assertEqual(second["previous_snapshot_id"], first["snapshot_id"])
        charge = next(row for row in self.pair.charges if row["id"] == "ch_001")
        charge["amount_refunded"] += 1
        third = sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertTrue(third["data_changed"])
        self.assertNotEqual(third["data_sha256"], second["data_sha256"])
        self.assertEqual(third["previous_snapshot_id"], second["snapshot_id"])
        self.assertEqual(snapshot_info(self.workspace), third)
        self.assertEqual(self.workspace.query("SELECT answer FROM models.synthetic_model")["rows"], [{"answer": 42}])
        self.assertFalse(self.workspace.is_synthetic())

    def test_failed_source_fetch_preserves_all_rows_and_receipts(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.saved_state()
        self.pair.charges[1]["amount_refunded"] += 1
        for source in ("fail_stripe", "fail_linear"):
            with self.subTest(source=source):
                setattr(self.pair, source, True)
                with self.assertRaisesRegex(ValueError, "could not be verified"):
                    sync_snapshot(self.workspace, self.pair, NAMESPACE)
                self.assertEqual(self.saved_state(), before)
                from contextbase.refresh_state import refresh_status
                self.assertEqual(refresh_status(self.workspace)["latest_attempt"]["outcome"], "unverified")
                with self.assertRaisesRegex(ValueError, "latest required refresh"):
                    self.workspace.query("SELECT * FROM stripe.customers")
                setattr(self.pair, source, False)

    def test_failed_transaction_rolls_back_replaced_source_tables_and_metadata(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.saved_state()
        self.pair.charges[1]["amount_refunded"] += 1
        with patch.object(self.workspace, "_metadata", side_effect=ValueError("Synthetic metadata failure")):
            with self.assertRaisesRegex(ValueError, "metadata failure"):
                sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertEqual(self.saved_state(), before)

    def test_locally_edited_source_rows_are_preserved_and_refresh_is_refused(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE stripe.customers SET name=? WHERE customer_id=?", ["Locally Edited Synthetic Name", "cus_001"])
        before = self.saved_state()
        with self.assertRaisesRegex(ValueError, "Local source rows changed"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertEqual(self.saved_state(), before)

    def test_unmanaged_table_collisions_are_not_overwritten(self):
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE SCHEMA stripe")
            conn.execute("CREATE TABLE stripe.customers (local_marker VARCHAR)")
            conn.execute("INSERT INTO stripe.customers VALUES ('synthetic_local_row')")
        with self.assertRaisesRegex(ValueError, "not managed"):
            sync_snapshot(self.workspace, self.pair, NAMESPACE)
        self.assertEqual(self.workspace.query("SELECT * FROM stripe.customers")["rows"], [{"local_marker": "synthetic_local_row"}])
        self.assertIsNone(snapshot_info(self.workspace))
        self.assertEqual(len(self.workspace.catalog()), 1)

    def test_ambiguous_invalid_and_orphan_mapping_cannot_replace_snapshot(self):
        sync_snapshot(self.workspace, self.pair, NAMESPACE)
        before = self.saved_state()
        bad_pairs = []
        duplicate = SnapshotPair()
        duplicate.customers[0]["metadata"]["account_id"] = "customer_001"
        bad_pairs.append(duplicate)
        internal = SnapshotPair()
        del internal.customers[0]["metadata"]["is_internal"]
        bad_pairs.append(internal)
        orphan = SnapshotPair()
        orphan.issues[0]["description"] = f"contextbase_account_id=customer_missing\ncontextbase_fixture={NAMESPACE}"
        bad_pairs.append(orphan)
        ambiguous = SnapshotPair()
        ambiguous.issues[0]["description"] += "\ncontextbase_account_id=customer_002"
        bad_pairs.append(ambiguous)
        over_refund = SnapshotPair()
        over_refund.charges[1]["amount_refunded"] = 20000
        bad_pairs.append(over_refund)
        for index, pair in enumerate(bad_pairs):
            with self.subTest(case=index), self.assertRaises(ValueError):
                sync_snapshot(self.workspace, pair, NAMESPACE)
            self.assertEqual(self.saved_state(), before)

    def test_receipts_are_exclusive_and_namespace_validation_precedes_reads(self):
        with self.assertRaisesRegex(ValueError, "Invalid synthetic fixture namespace"):
            collect_snapshot(self.pair, "../invalid")
        self.assertEqual(self.pair.verifications, 0)
        path = self.root / "receipts" / "synthetic.json"
        save_receipt(path, {"synthetic": True})
        self.assertEqual(json.loads(path.read_text()), {"synthetic": True})
        with self.assertRaisesRegex(ValueError, "already exists"):
            save_receipt(path, {"synthetic": False})
        self.assertEqual(json.loads(path.read_text()), {"synthetic": True})


if __name__ == "__main__":
    unittest.main()
