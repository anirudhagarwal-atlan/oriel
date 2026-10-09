"""Stdlib harness contracts only; these tests do not execute or verify dlt."""
from importlib import metadata, util
import ast
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = util.spec_from_file_location(name, ROOT / "examples/connectors" / filename)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verification = load("api_verification", "verify_apis.py")
worker = load("api_fixture_worker", "api_fixture_worker.py")
QUERY = "query($cursor: String) { issues(first: 100, after: $cursor) { nodes { id } pageInfo { hasNextPage endCursor } } }"


def request(fixture, protocol="rest", *, cursor=None, token=True, query=QUERY, method=None):
    if protocol == "rest":
        url = fixture.origin + "/rest/contacts" + ("?starting_after=" + cursor if cursor is not None else "")
        headers = {"Intercom-Version": "2.10"}
        body = None
    else:
        url = fixture.origin + "/graphql"
        headers = {"Content-Type": "application/json"}
        body = json.dumps({"query": query, "variables": {"cursor": cursor} if cursor else {}}).encode()
    if token:
        headers["Authorization"] = ("Bearer " if protocol == "rest" else "") + verification.SYNTHETIC_TOKEN
    req = Request(url, body, headers, method=method)
    # Never consult proxies or use ambient auth in this independent fixture test.
    with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
        return json.load(response)


class ConnectorAPIPreflightTests(unittest.TestCase):
    def test_missing_runtime_refuses_before_directory_or_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "uncreated"
            with patch.object(verification.metadata, "version", side_effect=metadata.PackageNotFoundError("dlt")), \
                    patch.object(verification, "FixtureServer") as server:
                with self.assertRaisesRegex(ValueError, "approved connector runtime"):
                    verification.verify(output)
                server.assert_not_called()
            self.assertFalse(output.exists())

    def test_wrong_runtime_version_refuses_before_directory_or_server(self):
        versions = {"dlt": "1.23.0", "PyYAML": "6.0.3", "duckdb": "1.4.5", "requests": "test-version"}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "uncreated"
            with patch.object(verification.metadata, "version", side_effect=versions.__getitem__), \
                    patch.object(verification, "FixtureServer") as server:
                with self.assertRaisesRegex(ValueError, "reviewed connector runtime"):
                    verification.verify(output)
                server.assert_not_called()
            self.assertFalse(output.exists())

    def test_preflight_only_reads_metadata_and_records_versions(self):
        versions = {"dlt": "1.24.0", "PyYAML": "6.0.3", "duckdb": "1.4.5", "requests": "test-version"}
        with patch.object(verification.metadata, "version", side_effect=versions.__getitem__):
            self.assertEqual(verification.prerequisites(), versions)

    def test_independent_oracle_qualifies_accounts_once(self):
        self.assertEqual(verification.expected(verification.CONTACTS_INITIAL, verification.ISSUES_INITIAL), 10)
        self.assertEqual(verification.expected(verification.CONTACTS_REFRESHED, verification.ISSUES_REFRESHED), 15)
        duplicate_issue = [("other_issue", "contact_001", 1, "started")]
        self.assertEqual(verification.expected(verification.CONTACTS_INITIAL, verification.ISSUES_INITIAL + duplicate_issue), 10)
        self.assertEqual(verification.expected(verification.CONTACTS_INITIAL, [("other", "contact_003", 1, "started")]), 0)
        self.assertEqual(verification.expected(verification.CONTACTS_INITIAL, [("other", "contact_001", 1, "completed")]), 0)

    def test_row_oracle_proves_removals_additions_and_changes(self):
        before = verification.expected_rows(verification.CONTACTS_INITIAL, verification.ISSUES_INITIAL)
        after = verification.expected_rows(verification.CONTACTS_REFRESHED, verification.ISSUES_REFRESHED)
        self.assertEqual([row["id"] for row in before["contacts"]], ["contact_001", "contact_002", "contact_003"])
        self.assertEqual([row["id"] for row in after["contacts"]], ["contact_001", "contact_002", "contact_004"])
        self.assertEqual(after["contacts"][0]["seats"], 4)
        self.assertNotIn("issue_005", {row["id"] for row in after["issues"]})
        self.assertIn("issue_006", {row["id"] for row in after["issues"]})

    def test_source_list_uses_cli_sources_envelope_and_one_named_connection(self):
        entry = {"name": "crm", "last_sync": {"status": "failed"}, "last_snapshot": {"snapshot_sha256": "synthetic"}}
        self.assertEqual(verification._listed_connection({"sources": [entry]}, "crm"), entry)
        for invalid in ([entry], {}, {"sources": []}, {"sources": [entry, entry]}):
            with self.subTest(value=invalid), self.assertRaises(RuntimeError):
                verification._listed_connection(invalid, "crm")


class ConnectorAPIFixtureTests(unittest.TestCase):
    def test_rest_cursor_pages_exhaust_exact_fixture(self):
        with verification.FixtureServer() as fixture:
            first = request(fixture)
            second = request(fixture, cursor=first["pages"]["next"]["starting_after"])
            self.assertEqual([row["id"] for row in first["data"] + second["data"]],
                             [row[0] for row in verification.CONTACTS_INITIAL])
            self.assertIsNone(second["pages"]["next"])
            self.assertEqual([item["offset"] for item in fixture.requests], [0, 2])

    def test_graphql_relay_three_pages_and_refresh(self):
        with verification.FixtureServer() as fixture:
            ids, cursor = [], None
            while True:
                page = request(fixture, "graphql", cursor=cursor)["data"]["issues"]
                ids.extend(row["id"] for row in page["nodes"])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                cursor = page["pageInfo"]["endCursor"]
            self.assertEqual(ids, [row[0] for row in verification.ISSUES_INITIAL])
            self.assertEqual([item["offset"] for item in fixture.requests], [0, 2, 4])
            fixture.set_state("refreshed")
            second = request(fixture, "graphql", cursor="offset_2")["data"]["issues"]
            self.assertFalse(second["pageInfo"]["hasNextPage"])
            self.assertEqual(second["nodes"][-1]["id"], "issue_006")

    def test_each_protocol_exposes_partial_page_then_hard_failure(self):
        with verification.FixtureServer() as fixture:
            for protocol in ("rest", "graphql"):
                fixture.set_state("refreshed", failure=protocol)
                first = request(fixture, protocol)
                if protocol == "rest":
                    self.assertEqual(first["data"][0]["custom_attributes"]["seats"], 99999)
                else:
                    self.assertEqual(first["data"]["issues"]["nodes"][0]["description"], "unpublished_partial_account")
                with self.assertRaises(HTTPError) as failed:
                    request(fixture, protocol, cursor="offset_2")
                self.assertEqual(failed.exception.code, 503)
                failed.exception.close()

    def test_auth_missing_or_mutation_is_rejected(self):
        with verification.FixtureServer() as fixture:
            for protocol in ("rest", "graphql"):
                with self.assertRaises(HTTPError) as failed:
                    request(fixture, protocol, token=False)
                self.assertEqual(failed.exception.code, 401)
                failed.exception.close()
            with self.assertRaises(HTTPError) as failed:
                request(fixture, "graphql", query="mutation { issueDelete(id: \"synthetic\") { success } }")
            self.assertEqual(failed.exception.code, 400)
            failed.exception.close()

    def test_unknown_cursor_or_write_is_rejected(self):
        with verification.FixtureServer() as fixture:
            for kwargs in ({"cursor": "offset_999"}, {"method": "DELETE"}):
                with self.assertRaises(HTTPError) as failed:
                    request(fixture, **kwargs)
                self.assertEqual(failed.exception.code, 400)
                failed.exception.close()


class ConnectorAPIGuardTests(unittest.TestCase):
    def test_launcher_uses_interpreter_package_without_checkout_path_injection(self):
        tree = ast.parse((ROOT / "examples/connectors/api_fixture_worker.py").read_text())
        path_accesses = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                         and node.attr == "path" and isinstance(node.value, ast.Name)
                         and node.value.id == "sys"]
        self.assertEqual(path_accesses, [], "Wheel verification must not override the interpreter's package search path.")

    def test_only_exact_numeric_fixture_origin_is_accepted(self):
        self.assertEqual(worker.fixture_origin("http://127.0.0.1:12345"), 12345)
        for origin in ("https://127.0.0.1:12345", "http://localhost:12345", "http://127.0.0.2:12345",
                       "http://example.invalid:12345", "http://127.0.0.1", "http://127.0.0.1:12345/",
                       "http://synthetic@127.0.0.1:12345", "http://127.0.0.1:12345?query=yes"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                worker.fixture_origin(origin)

    def test_address_and_http_guards_reject_different_destinations_and_auth(self):
        self.assertTrue(worker.allowed_address(("127.0.0.1", 12345), 12345))
        for address in (("127.0.0.1", 1), ("localhost", 12345), ("::1", 12345, 0, 0), "/tmp/fixture.sock"):
            self.assertFalse(worker.allowed_address(address, 12345))
        origin = "http://127.0.0.1:12345"
        headers = {"Authorization": "Bearer " + worker.SYNTHETIC_TOKEN}
        worker.check_http(origin + "/rest/contacts?starting_after=offset_2", "GET", headers, origin)
        for url, method, auth in (("http://example.invalid/rest/contacts", "GET", headers),
                                  (origin + "/rest/contacts", "POST", headers),
                                  (origin + "/rest/contacts", "GET", {}),
                                  (origin + "/unselected", "GET", headers)):
            with self.assertRaises(ValueError):
                worker.check_http(url, method, auth, origin)

    def test_child_audit_guard_blocks_external_dns_before_resolution(self):
        code = """
import importlib.util, json, socket, sys
spec = importlib.util.spec_from_file_location('fixture_worker', sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
counts = worker.install_network_guard('http://127.0.0.1:12345')
try:
    socket.getaddrinfo('example.invalid', 443)
except RuntimeError:
    pass
else:
    raise AssertionError('external lookup escaped the fixture guard')
print(json.dumps(counts))
"""
        completed = subprocess.run([sys.executable, "-c", code, str(ROOT / "examples/connectors/api_fixture_worker.py")],
                                   capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(completed.stdout), {"allowed_connects": 0, "blocked_network_attempts": 1})

    def test_child_guard_permits_only_the_running_fixture_connection(self):
        code = """
import importlib.util, json, sys
from urllib.request import Request, ProxyHandler, build_opener
spec = importlib.util.spec_from_file_location('fixture_worker', sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
origin = sys.argv[2]
counts = worker.install_network_guard(origin)
request = Request(origin + '/rest/contacts', headers={
    'Authorization': 'Bearer ' + worker.SYNTHETIC_TOKEN, 'Intercom-Version': '2.10'})
with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
    assert len(json.load(response)['data']) == 2
print(json.dumps(counts))
"""
        with verification.FixtureServer() as fixture:
            completed = subprocess.run([sys.executable, "-c", code,
                                        str(ROOT / "examples/connectors/api_fixture_worker.py"), fixture.origin],
                                       capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(completed.stdout), {"allowed_connects": 1, "blocked_network_attempts": 0})


if __name__ == "__main__":
    unittest.main()
