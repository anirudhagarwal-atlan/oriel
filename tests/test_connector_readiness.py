import unittest
from unittest.mock import patch

from contextbase.connectors import readiness


class ConnectorReadinessTests(unittest.TestCase):
    def test_missing_runtime_and_vendor_dependencies_do_not_import_sources(self):
        original = __import__
        def guarded(name, *args, **kwargs):
            if name.split(".")[0] in {"dlt", "yaml", "requests", "sqlalchemy", "sources"}:
                raise AssertionError("Readiness must not load connectors")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded), \
             patch.object(readiness, "_installed", return_value=None), \
             patch("os.getenv", side_effect=AssertionError("No credential lookup")), \
             patch("socket.socket", side_effect=AssertionError("No source connection")):
            report = readiness.inspect()
        self.assertEqual(report["count"], 101)
        names = {entry["name"]: entry for entry in report["connectors"]}
        self.assertEqual(names["linear"]["status"], "dependencies_required")
        self.assertEqual(names["sqlite"]["status"], "dependencies_required")
        self.assertIn("simple-salesforce", {item["package"] for item in names["salesforce"]["dependencies"]})
        self.assertTrue(all(not item["live_validated"] for item in names.values()))
        self.assertTrue(all(item["verification"]["definition"] == "packaged" for item in names.values()))
        self.assertEqual(names["monday"]["verification"]["native_dlt_integration"], "not_performed")
        self.assertEqual(names["csv"]["verification"]["adapter_contract"], "local_functional")

    def test_installed_package_is_not_live_verification(self):
        with patch.object(readiness, "_installed", side_effect=lambda name: {"dlt": "1.24.0", "PyYAML": "6.0.3"}.get(name)):
            entry = readiness.inspect("linear")["connectors"][0]
        self.assertEqual(entry["status"], "prerequisites_present_connection_unverified")
        self.assertFalse(entry["configuration_and_credentials_checked"])
        self.assertFalse(entry["live_validated"])

    def test_runtime_version_and_python_requirements_are_explicit(self):
        with patch.object(readiness, "_installed", return_value="0.0.0"):
            entry = readiness.inspect("linear")["connectors"][0]
        self.assertEqual(entry["status"], "dependencies_required")
        self.assertTrue(all(item["status"] == "version_mismatch" for item in entry["dependencies"]))
        with patch.object(readiness.sys, "version_info", (3, 15, 0)), patch.object(readiness, "_installed", return_value=None):
            self.assertEqual(readiness.inspect("linear")["connectors"][0]["status"], "unsupported_python")


if __name__ == "__main__":
    unittest.main()
