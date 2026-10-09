"""Preflight/oracle tests only; actual dlt execution is a separate explicit check."""
from importlib import metadata, util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "examples/connectors/verify_sqlite.py"
spec = util.spec_from_file_location("connector_verification", path)
verification = util.module_from_spec(spec)
spec.loader.exec_module(verification)


class ConnectorVerificationPreflightTests(unittest.TestCase):
    def test_missing_runtime_refuses_before_creating_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-fixture"
            with patch.object(verification.metadata, "version", side_effect=metadata.PackageNotFoundError("dlt")):
                with self.assertRaisesRegex(ValueError, "approved connector runtime"):
                    verification.verify(output)
            self.assertFalse(output.exists())

    def test_oracle_checks_membership_and_changed_source_values(self):
        self.assertEqual(verification.expected(verification.INITIAL), 10)
        self.assertEqual(verification.expected(verification.REFRESHED), 4)
        self.assertEqual(verification.expected([("synthetic", "inactive", 99999)]), 0)


if __name__ == "__main__":
    unittest.main()
