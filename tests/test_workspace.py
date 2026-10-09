import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import duckdb

from contextbase.mcp import dispatch, serve
from contextbase.tools import Toolkit
from contextbase.workspace import Workspace, init_demo


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = init_demo(self.root / "demo", accounts=12)

    def test_agent_read_boundary(self):
        secret = self.root / "private.csv"
        secret.write_text("value\nsynthetic_private_fixture\n")
        denied = [
            "SELECT 1; DELETE FROM crm.accounts",
            "/* comment */ UPDATE crm.accounts SET health='changed'",
            "COPY crm.accounts TO '/tmp/contextbase-not-created.csv'",
            f"SELECT * FROM read_csv_auto('{secret}')",
            "SELECT * FROM read_csv_auto('https://example.invalid/data.csv')",
            "INSTALL httpfs",
            "SET enable_external_access=true",
            "ATTACH ':memory:' AS elsewhere",
        ]
        for sql in denied:
            with self.subTest(sql=sql), self.assertRaises((ValueError, duckdb.Error)):
                self.w.query(sql)
        self.assertEqual(self.w.query("WITH n AS (SELECT 3 AS value) SELECT value FROM n")["rows"], [{"value": 3}])
        self.assertEqual(self.w.query("SELECT DISTINCT health FROM crm.accounts")["rows"], [{"health": "unreviewed"}])

    def test_truncation_and_identifiers(self):
        result = self.w.query("SELECT * FROM crm.accounts ORDER BY account_id", max_rows=2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["rows"]), 2)
        with self.assertRaises(ValueError):
            self.w.query("SELECT 1 AS repeated, 2 AS repeated")
        with self.assertRaises(ValueError):
            self.w.query("SELECT 1", max_rows=True)
        with self.assertRaises(ValueError):
            self.w.transform("unsafe; DROP SCHEMA crm", "SELECT 1")

    def test_file_import_formats_and_explicit_replace(self):
        for suffix, content in [("csv", "id,amount\n1,12\n2,24\n"),
                                ("json", '[{"id":1,"amount":12},{"id":2,"amount":24}]'),
                                ("jsonl", '{"id":1,"amount":12}\n{"id":2,"amount":24}\n')]:
            source = self.root / ("source." + suffix)
            source.write_text(content)
            receipt = self.w.ingest(source, "imported", suffix)
            self.assertEqual(receipt["rows"], 2)
            self.assertEqual(receipt["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(self.w.query(f"SELECT SUM(amount) AS total FROM imported.{suffix}")["rows"], [{"total": 36}])
            with self.assertRaises(duckdb.Error):
                self.w.ingest(source, "imported", suffix)
            self.assertEqual(self.w.ingest(source, "imported", suffix, replace=True)["rows"], 2)
        self.assertFalse(self.w.is_synthetic())

    def test_dinobase_bridge_does_not_modify_source_and_restricts_views(self):
        source = self.root / "upstream.duckdb"
        allowed = self.root / "parquet"
        allowed.mkdir()
        parquet = allowed / "events.parquet"
        outside = self.root / "unapproved.csv"
        outside.write_text("private_value\nsynthetic_private_fixture\n")
        with duckdb.connect(str(source)) as conn:
            conn.execute("CREATE SCHEMA crm")
            conn.execute("CREATE TABLE crm.contacts AS SELECT 'example_1' AS id")
            conn.execute(f"COPY (SELECT 8 AS amount) TO '{parquet}' (FORMAT PARQUET)")
            conn.execute(f"CREATE VIEW crm.events AS SELECT * FROM read_parquet('{parquet}')")
            conn.execute(f"CREATE VIEW crm.disallowed AS SELECT * FROM read_csv_auto('{outside}')")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        receipt = self.w.import_dinobase(source, ["crm.contacts", "crm.events"], parquet_root=allowed)
        self.assertFalse(receipt["upstream_modified"])
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(self.w.query("SELECT amount FROM crm.events")["rows"], [{"amount": 8}])
        with self.assertRaises(duckdb.Error):
            self.w.import_dinobase(source, ["crm.disallowed"], parquet_root=allowed)
        self.assertNotIn("disallowed", [t["table"] for t in self.w.catalog()])

    def test_change_preview_approval_staleness_and_replay(self):
        proposal = self.w.plan_change("crm.accounts", "account_id", "customer_003", {"health": "review"})
        self.assertEqual(proposal["before"]["health"], "unreviewed")
        with self.assertRaises(ValueError):
            self.w.apply_change(proposal["plan_id"], "wrong_hash")
        self.w.apply_change(proposal["plan_id"], proposal["approval_sha256"])
        self.assertEqual(self.w.query("SELECT health FROM crm.accounts WHERE account_id='customer_003'")["rows"], [{"health": "review"}])
        with self.assertRaises(ValueError):
            self.w.apply_change(proposal["plan_id"], proposal["approval_sha256"])
        stale = self.w.plan_change("crm.accounts", "account_id", "customer_003", {"health": "old_plan"})
        new = self.w.plan_change("crm.accounts", "account_id", "customer_003", {"health": "newer"})
        self.w.apply_change(new["plan_id"], new["approval_sha256"])
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            self.w.apply_change(stale["plan_id"], stale["approval_sha256"])
        with self.assertRaises(ValueError):
            self.w.plan_change("crm.accounts", "health", "unreviewed", {"segment": "all"})

    def test_transform_is_local_and_new_table_only(self):
        receipt = self.w.transform("segments", "SELECT segment,COUNT(*) AS accounts FROM crm.accounts GROUP BY segment")
        self.assertEqual(receipt["rows"], 3)
        self.assertEqual(self.w.query("SELECT SUM(accounts) AS total FROM models.segments")["rows"], [{"total": 12}])
        with self.assertRaises(duckdb.Error):
            self.w.transform("segments", "SELECT 1")
        with self.assertRaises(ValueError):
            self.w.transform("bad", "SELECT 1; DELETE FROM crm.accounts")

    def test_source_tools_paginate_without_duplicates_and_filter_before_page(self):
        toolkit = Toolkit(self.w, "per_source")
        rows, offset = [], 0
        while True:
            page = toolkit.call("list_records", {"table": "crm.accounts", "columns": ["account_id"],
                                                "filters": {"segment": "startup"}, "offset": offset, "limit": 2})
            rows.extend(page["rows"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r["account_id"] for r in rows}), 4)
        with self.assertRaises(ValueError):
            toolkit.call("query", {"sql": "SELECT 1"})

    def test_source_headers_may_have_spaces_and_dlt_prefixes(self):
        source = self.root / "dlt.csv"
        source.write_text('id,_dlt_id,Account Name\n1,abc,Example account\n')
        self.w.ingest(source, "crm", "exports")
        result = Toolkit(self.w, "per_source").call("list_records", {
            "table": "crm.exports", "columns": ["id", "Account Name"], "filters": {"_dlt_id": "abc"}})
        self.assertEqual(result["rows"], [{"id": 1, "Account Name": "Example account"}])

    def test_reviewed_changes_preserve_exact_imported_headers(self):
        headers = [
            ("Account ID", "Health Status"),
            ("_dlt_id", "_health"),
            ('Account "ID"; DROP TABLE crm.accounts; --',
             'Health "Status"; DROP TABLE crm.accounts; --'),
        ]
        changed_value = "reviewed'; DELETE FROM crm.accounts; --"
        for index, (key, field) in enumerate(headers):
            with self.subTest(key=key, field=field):
                source = self.root / f"headers_{index}.csv"
                with source.open("w", newline="") as handle:
                    csv.writer(handle).writerows([
                        [key, field], ["customer_001", "unreviewed"], ["customer_002", "unchanged"],
                    ])
                original = source.read_bytes()
                table = f"imported.headers_{index}"
                self.w.ingest(source, "imported", f"headers_{index}")
                plan = self.w.plan_change(table, key, "customer_001", {field: changed_value})
                self.assertEqual(plan["before"], {key: "customer_001", field: "unreviewed"})
                self.assertEqual(plan["after"], {key: "customer_001", field: changed_value})
                applied = self.w.apply_change(plan["plan_id"], plan["approval_sha256"])
                self.assertEqual(applied["scope"], "local_only")
                self.assertEqual(self.w.query(f"SELECT * FROM {table} ORDER BY 1")["rows"], [
                    {key: "customer_001", field: changed_value}, {key: "customer_002", field: "unchanged"},
                ])
                self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.w.query("SELECT COUNT(*) AS n FROM crm.accounts")["rows"], [{"n": 12}])

    def test_reviewed_changes_require_exact_observed_columns(self):
        source = self.root / "change-columns.csv"
        source.write_text("Account ID,Health Status\ncustomer_001,unreviewed\n")
        self.w.ingest(source, "imported", "change_columns")
        invalid = [
            ("Missing Key", {"Health Status": "reviewed"}, "Unknown lookup column"),
            ("account id", {"Health Status": "reviewed"}, "Unknown lookup column"),
            ("Account ID", {"Missing Column": "reviewed"}, "Unknown changed column"),
            ("Account ID", {"health status": "reviewed"}, "Unknown changed column"),
            ("Account ID", {'x"; DROP TABLE crm.accounts; --': "reviewed"}, "Unknown changed column"),
            ("Account ID", {"Account ID": "customer_002"}, "without modifying the lookup key"),
        ]
        for key, changes, message in invalid:
            with self.subTest(key=key, changes=changes), self.assertRaisesRegex(ValueError, message):
                self.w.plan_change("imported.change_columns", key, "customer_001", changes)
        with self.assertRaises(ValueError):
            self.w.plan_change("imported.change_columns; DROP TABLE crm.accounts", "Account ID", "customer_001",
                               {"Health Status": "reviewed"})
        self.assertEqual(self.w.query("SELECT COUNT(*) AS n FROM _contextbase.changes")["rows"], [{"n": 0}])
        self.assertEqual(self.w.query("SELECT * FROM imported.change_columns")["rows"], [
            {"Account ID": "customer_001", "Health Status": "unreviewed"},
        ])

    def test_imported_header_change_refuses_stale_or_ambiguous_refresh(self):
        refreshes = [
            ("changed", "Account ID,Health Status\ncustomer_001,refreshed\n", "changed since preview"),
            ("duplicate", "Account ID,Health Status\ncustomer_001,unreviewed\ncustomer_001,unreviewed\n",
             "changed since preview"),
            ("renamed", "Account ID,Renamed Status\ncustomer_001,unreviewed\n", "Unknown changed column"),
        ]
        for name, refreshed_csv, message in refreshes:
            with self.subTest(refresh=name):
                source = self.root / (name + ".csv")
                source.write_text("Account ID,Health Status\ncustomer_001,unreviewed\n")
                table = "imported." + name
                self.w.ingest(source, "imported", name)
                plan = self.w.plan_change(table, "Account ID", "customer_001", {"Health Status": "reviewed"})
                with self.assertRaisesRegex(ValueError, "exact preview SHA-256"):
                    self.w.apply_change(plan["plan_id"], "0" * 64)
                source.write_text(refreshed_csv)
                self.w.ingest(source, "imported", name, replace=True)
                before = self.w.query(f"SELECT * FROM {table}")["rows"]
                with self.assertRaisesRegex(ValueError, message):
                    self.w.apply_change(plan["plan_id"], plan["approval_sha256"])
                self.assertEqual(self.w.query(f"SELECT * FROM {table}")["rows"], before)
                if name == "duplicate":
                    with self.assertRaisesRegex(ValueError, "exactly one existing row"):
                        self.w.plan_change(table, "Account ID", "customer_001", {"Health Status": "reviewed"})

    def test_decimal_change_preview_preserves_exact_stale_check(self):
        with self.w._connect(writable=True) as conn:
            conn.execute("CREATE TABLE crm.precise(id INTEGER, amount DECIMAL(38,2), note VARCHAR)")
            conn.execute("INSERT INTO crm.precise VALUES (1,9007199254740992.00,'original')")
        plan = self.w.plan_change("crm.precise", "id", 1, {"note": "reviewed"})
        self.assertEqual(plan["before"]["amount"], "9007199254740992.00")
        with self.w._connect(writable=True) as conn:
            conn.execute("UPDATE crm.precise SET amount=9007199254740993.00")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            self.w.apply_change(plan["plan_id"], plan["approval_sha256"])
        result = self.w.query("SELECT amount FROM crm.precise")
        self.assertEqual(result["rows"], [{"amount": "9007199254740993.00"}])
        self.assertEqual(result["decimal_columns"], {"amount": "DECIMAL(38,2)"})
        from contextbase.context import propose_metric, approve_metric
        proposal = propose_metric(self.w, "exact_amount", "SELECT amount FROM crm.precise", "Exact amount",
                                  grain="One row", rules=["Use the complete amount"], owner="example_owner")
        with self.w._connect(writable=True) as conn:
            conn.execute("UPDATE crm.precise SET amount=9007199254740992.00")
        with self.assertRaisesRegex(ValueError, "witnessed metric result changed"):
            approve_metric(self.w, "exact_amount", proposal["approval_sha256"], "example_reviewer")

    def test_synthetic_consent_is_revoked_after_edits_and_context_changes(self):
        self.assertTrue(self.w.is_synthetic())
        plan = self.w.plan_change("crm.accounts", "account_id", "customer_003", {"name": "operator text"})
        self.assertFalse(self.w.is_synthetic())
        self.w.apply_change(plan["plan_id"], plan["approval_sha256"])
        self.assertFalse(self.w.is_synthetic())
        fresh = init_demo(self.root / "fresh", accounts=4)
        with fresh._connect(writable=True) as conn:
            conn.execute("UPDATE crm.accounts SET name='operator text' WHERE account_id='customer_001'")
        self.assertFalse(fresh.is_synthetic())
        pristine = init_demo(self.root / "pristine", accounts=4)
        path = pristine.root / "context" / "approved.json"
        context = json.loads(path.read_text())
        context["rules"].append("operator-specific text")
        path.write_text(json.dumps(context))
        self.assertFalse(pristine.is_synthetic())

    def test_stdio_mcp_and_direct_tools_return_equal_answers(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "metric", "arguments": {"name": "active_arr"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "query", "arguments": {"sql": "DELETE FROM crm.accounts"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "contextbase", "--workspace", str(self.w.root), "serve"],
                                input="\n".join(json.dumps(r) for r in requests) + "\n", text=True,
                                capture_output=True, timeout=15, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        from_mcp = json.loads(responses[2]["result"]["content"][0]["text"])
        direct = Toolkit(self.w).call("metric", {"name": "active_arr"})
        self.assertEqual(from_mcp["rows"], direct["rows"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertNotIn("changes", [t["name"] for t in responses[1]["result"]["tools"]])

    def test_demo_is_reproducible_and_never_overwrites(self):
        other = init_demo(self.root / "other", accounts=12)
        self.assertEqual(self.w.query("SELECT * FROM crm.opportunities ORDER BY opportunity_id")["rows"],
                         other.query("SELECT * FROM crm.opportunities ORDER BY opportunity_id")["rows"])
        with self.assertRaises(ValueError):
            init_demo(self.w.root)


if __name__ == "__main__":
    unittest.main()
