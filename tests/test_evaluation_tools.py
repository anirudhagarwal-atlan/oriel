"""Actual MCP transport and identical isolated post-processing for both arms."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from contextbase.evaluation_tools import ComputedTools, EvaluationToolError, StdioMCPClient
from contextbase.workspace import Workspace


class EvaluationToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="contextbase-evaluation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Workspace(Path(self.temporary.name) / "source")
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE SCHEMA fixture")
            conn.execute("CREATE TABLE fixture.charges(id VARCHAR, amount_cents INTEGER)")
            conn.execute("INSERT INTO fixture.charges VALUES ('charge_001',101),('charge_002',202)")

    def test_actual_stdio_mcp_initializes_and_preserves_errors(self):
        client = StdioMCPClient(self.workspace)
        self.assertEqual(client.command[0], sys.executable)
        with client:
            self.assertEqual({item["name"] for item in client.definitions()}, {"catalog", "context", "query", "refresh_status"})
            self.assertEqual(client.call("query", {"sql": "SELECT sum(amount_cents) AS n FROM fixture.charges"})["rows"], [{"n": 303}])
            error = client.call("query", {"sql": "DELETE FROM fixture.charges"})
            self.assertIn("error", error)
            self.assertIn("error", client.call("not_a_tool", {}))
            self.assertIn("error", client.call("query", {"sql": "SELECT 1", "unexpected": True}))
            self.assertEqual(client.call("query", {"sql": "SELECT count(*) AS n FROM fixture.charges"})["rows"], [{"n": 2}])
            self.assertTrue(all(set(item) == {"method", "tool", "elapsed_ms", "request_bytes", "response_bytes", "status"} for item in client.trace))
            self.assertIn("tool_error", [item["status"] for item in client.trace])
        self.assertIsNotNone(client.process.poll())
        with self.assertRaises(EvaluationToolError):
            client.call("catalog")

    def test_same_scratch_api_for_source_arm_without_live_api_calls(self):
        # tools/list and the context tool do not contact either simulator.
        with ComputedTools(StdioMCPClient(self.workspace, arm="rystic_source")) as tools:
            names = [item["name"] for item in tools.definitions()]
            self.assertIn("stripe_list", names)
            self.assertIn("linear_query", names)
            self.assertIn("compute", names)
            context = tools.call("context")
            self.assertEqual(context["stored_call_id"], 1)
            result = tools.call("compute", {"sql": "SELECT result_json->>'status' AS status FROM scratch.tool_results WHERE call_id=1"})
            self.assertEqual(result["result"]["rows"], [{"status": "unconfigured"}])

    def test_full_results_retained_and_json_postprocessing_is_exact(self):
        client = StdioMCPClient(self.workspace)
        with ComputedTools(client) as tools:
            scratch_path = tools.workspace.root
            result = tools.call("query", {"sql": "SELECT * FROM fixture.charges ORDER BY id"})
            self.assertEqual(result["stored_call_id"], 1)
            # Mutating a hypothetical display copy cannot mutate the retained data.
            result["result"]["rows"] = []
            calculated = tools.call("compute", {"sql": "SELECT sum((j.value->>'amount_cents')::BIGINT) AS cents FROM scratch.tool_results r, json_each(r.result_json, '$.rows') j WHERE r.call_id=1"})
            self.assertEqual(calculated["stored_call_id"], 2)
            self.assertEqual(calculated["result"]["rows"], [{"cents": 303}])
            history = tools.call("compute", {"sql": "SELECT call_id,tool_name FROM scratch.tool_results ORDER BY call_id"})
            self.assertEqual(history["result"]["rows"], [{"call_id": 1, "tool_name": "query"}, {"call_id": 2, "tool_name": "compute"}])
            self.assertGreater(tools.stored_bytes, 0)
        self.assertFalse(scratch_path.exists())
        self.assertIsNotNone(client.process.poll())

    def test_compute_cannot_read_source_database_files_or_write(self):
        private_path = Path(self.temporary.name) / "outside.json"
        private_path.write_text('{"test_marker":"must_not_be_returned"}')
        with ComputedTools(StdioMCPClient(self.workspace)) as tools:
            tools.call("catalog")
            queries = [
                "SELECT * FROM fixture.charges",
                "DELETE FROM scratch.tool_results",
                "SELECT 1; SELECT 2",
                "ATTACH 'anything' AS unrelated",
                f"SELECT * FROM read_json_auto('{private_path}')",
                "SELECT * FROM read_csv_auto('https://example.com/never-request.csv')",
                f"SELECT * FROM read_blob('{self.workspace.db_path}')",
            ]
            for sql in queries:
                with self.subTest(sql=sql):
                    output = tools.call("compute", {"sql": sql})
                    self.assertIn("error", output["result"])
                    self.assertNotIn("must_not_be_returned", json.dumps(output))
            retained = tools.call("compute", {"sql": "SELECT count(*) AS n FROM scratch.tool_results"})
            self.assertEqual(retained["result"]["rows"], [{"n": len(queries) + 1}])
        self.assertEqual(self.workspace.query("SELECT count(*) AS n FROM fixture.charges")["rows"], [{"n": 2}])

    def test_result_storage_limits_fail_without_silent_truncation(self):
        with ComputedTools(StdioMCPClient(self.workspace)) as tools:
            with patch("contextbase.evaluation_tools.MAX_RESULT_BYTES", 10):
                with self.assertRaisesRegex(EvaluationToolError, "complete result"):
                    tools.call("catalog")
            self.assertEqual(tools.stored_bytes, 0)
            tools.call("query", {"sql": "SELECT 1 AS n"})
            with patch("contextbase.evaluation_tools.MAX_TRIAL_BYTES", tools.stored_bytes):
                with self.assertRaisesRegex(EvaluationToolError, "complete result"):
                    tools.call("query", {"sql": "SELECT 2 AS n"})
            self.assertEqual(tools.workspace.query("SELECT count(*) AS n FROM scratch.tool_results")["rows"], [{"n": 1}])

    def test_compute_validates_arguments_and_max_rows(self):
        with ComputedTools(StdioMCPClient(self.workspace)) as tools:
            for arguments in ({}, {"sql": "SELECT 1", "extra": 1}, {"sql": "SELECT 1", "max_rows": 10001}, {"sql": "SELECT 1", "max_rows": True}, []):
                with self.subTest(arguments=arguments):
                    self.assertIn("error", tools.call("compute", arguments)["result"])
            output = tools.call("compute", {"sql": "SELECT * FROM range(3)", "max_rows": 2})
            self.assertTrue(output["result"]["truncated"])
            self.assertEqual(output["result"]["row_count"], 2)

    def test_invalid_arm_and_nonlocal_endpoints_fail_before_launch(self):
        with patch("contextbase.evaluation_tools.subprocess.Popen") as launch:
            for kwargs in ({"arm": "metrics"}, {"arm": "rystic_source", "stripe_url": "https://api.stripe.com"},
                           {"arm": "rystic_source", "stripe_url": "http://127.0.0.1:123/x"},
                           {"arm": "rystic_source", "stripe_url": "http://127.0.0.1:123", "linear_url": "http://127.0.0.1:123"},
                           {"arm": "sql_context", "stripe_url": "http://127.0.0.1:123"}, {"timeout": float("nan")},
                           {"arm": "rystic_source", "verification_policy": "cached"},
                           {"arm": "sql_context", "verification_policy": "evaluation_session"},
                           {"evaluation_context": "yes"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    StdioMCPClient(self.workspace, **kwargs)
            launch.assert_not_called()

    def test_session_verification_is_an_explicit_source_only_command_option(self):
        default = StdioMCPClient(self.workspace, arm="rystic_source")
        self.assertNotIn("--verification-policy", default.command)
        session = StdioMCPClient(self.workspace, arm="rystic_source", verification_policy="evaluation_session")
        at = session.command.index("--verification-policy")
        self.assertEqual(session.command[at + 1], "evaluation_session")
        for arm in ("sql_context", "rystic_source"):
            self.assertNotIn("--evaluation-context", StdioMCPClient(self.workspace, arm=arm).command)
            self.assertIn("--evaluation-context", StdioMCPClient(self.workspace, arm=arm, evaluation_context=True).command)

    def fake_process(self, code):
        original = subprocess.Popen
        def launch(*args, **kwargs):
            return original([sys.executable, "-u", "-c", code], **kwargs)
        return patch("contextbase.evaluation_tools.subprocess.Popen", side_effect=launch)

    def test_timeout_closes_child_without_exposing_stderr(self):
        with self.fake_process("import sys,time;sys.stderr.write('SECRET_TEST_MARKER');sys.stderr.flush();time.sleep(60)"):
            client = StdioMCPClient(self.workspace, timeout=0.1)
            started = time.perf_counter()
            with self.assertRaisesRegex(EvaluationToolError, "timed out") as raised:
                with client:
                    self.fail("A stalled server must not initialize.")
            self.assertLess(time.perf_counter() - started, 3)
            self.assertIsNotNone(client.process.poll())
            self.assertNotIn("SECRET_TEST_MARKER", str(raised.exception))
            self.assertNotIn("SECRET_TEST_MARKER", json.dumps(client.trace))

    def test_malformed_and_oversized_frames_close_child(self):
        scripts = ["import sys;sys.stdin.readline();print('{not JSON}');sys.stdin.read()",
                   "import sys;sys.stdin.readline();print('x'*2048);sys.stdin.read()",
                   "import sys;sys.stdin.readline();print('{\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}');sys.stdin.read()"]
        for script in scripts:
            with self.subTest(script=script), self.fake_process(script), patch("contextbase.evaluation_tools.MAX_FRAME_BYTES", 1024):
                client = StdioMCPClient(self.workspace, timeout=0.5)
                with self.assertRaises(EvaluationToolError):
                    with client:
                        self.fail("Malformed transport must not initialize.")
                self.assertIsNotNone(client.process.poll())

    def test_scratch_database_removed_when_user_code_fails(self):
        client = StdioMCPClient(self.workspace)
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with ComputedTools(client) as tools:
                path = tools.workspace.root
                raise RuntimeError("caller failed")
        self.assertFalse(path.exists())
        self.assertIsNotNone(client.process.poll())


if __name__ == "__main__":
    unittest.main()
