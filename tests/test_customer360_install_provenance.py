"""Local Git provenance contracts; no installs, providers, or remote Git calls."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_customer360_install as install


class InstallGitProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init", "-q", "-b", "main")
        files = {
            "contextbase/public.py": b"PUBLIC_SYNTHETIC_VALUE = 1\n",
            "uv.lock": b"# synthetic lock for provenance unit checks only\n",
            ".gitignore": b".oriel/\ncontextbase/ignored.py\n",
            install.HELPER_FILE: Path(install.__file__).read_bytes(),
            install.WORKFLOW_FILE: b"# synthetic workflow; never executed\n",
        }
        for name, raw in files.items():
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.commit()
        self.public = {name: self.repository / name for name in ("contextbase/public.py", "uv.lock")}

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments, input=None):
        # commit-tree creates test objects directly and runs no commit hooks.
        # Identities apply only to this temporary synthetic repository command.
        result = subprocess.run(["git", "-c", "user.name=Synthetic Test", "-c",
                                 "user.email=synthetic@example.invalid", "-C", str(self.repository), *arguments],
                                input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=10)
        return result.stdout

    def commit(self, *, parent=None):
        self.git("add", "--all")
        tree = self.git("write-tree").decode().strip()
        arguments = ["commit-tree", tree, "-m", "Synthetic local provenance fixture"]
        if parent:
            arguments += ["-p", parent]
        commit = self.git(*arguments).decode().strip()
        self.git("update-ref", "refs/heads/main", commit)
        return commit

    def test_committed_receipt_pins_head_tree_every_public_blob_and_helpers(self):
        provenance = install._capture_git_provenance(self.repository, self.public)
        self.assertFalse(provenance["verified"])
        self.assertEqual(provenance["head_commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(provenance["tree_oid"], self.git("rev-parse", "HEAD^{tree}").decode().strip())
        self.assertEqual(set(provenance["files"]), set(self.public) | {install.HELPER_FILE, install.WORKFLOW_FILE})
        for name, record in provenance["files"].items():
            self.assertEqual(record["blob_oid"], self.git("rev-parse", "HEAD:" + name).decode().strip())
            self.assertEqual(record["sha256"], hashlib.sha256((self.repository / name).read_bytes()).hexdigest())
        # Ignored runtime files are neither copied nor evidence of source dirt.
        ignored = self.repository / ".oriel" / "verification.json"
        ignored.parent.mkdir()
        ignored.write_text('{"synthetic":true}')
        receipt = {"source_kind": "clean_git_commit_snapshot", "git_provenance": provenance}
        install._finish_git_provenance(self.repository, self.public, receipt)
        self.assertTrue(receipt["git_provenance"]["verified"])
        self.assertTrue(receipt["git_provenance"]["final_clean"])
        self.assertTrue(receipt["git_provenance"]["head_tree_unchanged"])
        self.assertFalse(receipt["git_provenance"]["remote_publication_verified"])

    def test_plain_copy_and_repository_subdirectory_are_not_git_toplevel(self):
        copy = self.root / "plain-copy"
        copy.mkdir()
        with self.assertRaisesRegex(ValueError, "Git provenance"):
            install._capture_git_provenance(copy, {})
        with self.assertRaisesRegex(ValueError, "Git top-level"):
            install._capture_git_provenance(self.repository / "contextbase", {})

    def test_dirty_tracked_staged_and_nonignored_untracked_files_refuse(self):
        path = self.public["contextbase/public.py"]
        original = path.read_bytes()
        path.write_bytes(original + b"# local edit\n")
        with self.assertRaisesRegex(ValueError, "clean Git"):
            install._capture_git_provenance(self.repository, self.public)
        self.git("add", "contextbase/public.py")
        with self.assertRaisesRegex(ValueError, "clean Git"):
            install._capture_git_provenance(self.repository, self.public)
        path.write_bytes(original)
        self.git("add", "contextbase/public.py")
        (self.repository / "untracked.txt").write_text("synthetic nonignored file")
        with self.assertRaisesRegex(ValueError, "clean Git"):
            install._capture_git_provenance(self.repository, self.public)

    def test_status_clean_index_hint_cannot_hide_changed_public_bytes(self):
        # An index hint in this test-only repository demonstrates why clean
        # porcelain output alone cannot establish committed source identity.
        self.git("update-index", "--assume-unchanged", "contextbase/public.py")
        self.public["contextbase/public.py"].write_bytes(b"PUBLIC_SYNTHETIC_VALUE = 2\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        with self.assertRaisesRegex(ValueError, "differs from its committed Git blob"):
            install._capture_git_provenance(self.repository, self.public)

    def test_ignored_uncommitted_public_input_is_not_part_of_commit(self):
        ignored = self.repository / "contextbase/ignored.py"
        ignored.write_bytes(b"# public-looking but not committed\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        with self.assertRaisesRegex(ValueError, "not a regular committed Git blob"):
            install._capture_git_provenance(self.repository, {**self.public, "contextbase/ignored.py": ignored})

    def test_changed_running_helper_and_copied_workflow_refuse(self):
        provenance = install._capture_git_provenance(self.repository, self.public)
        with self.assertRaisesRegex(ValueError, "pinned Git blob"):
            install._match_committed_bytes(install.WORKFLOW_FILE, b"# different workflow", provenance)
        (self.repository / install.HELPER_FILE).write_bytes(b"# different committed verifier\n")
        self.commit(parent=provenance["head_commit"])
        with self.assertRaisesRegex(ValueError, "pinned Git blob"):
            install._capture_git_provenance(self.repository, self.public)

    def test_same_tree_new_head_at_end_refuses_success(self):
        original = install._capture_git_provenance(self.repository, self.public)
        self.commit(parent=original["head_commit"])
        receipt = {"git_provenance": deepcopy(original)}
        with self.assertRaisesRegex(ValueError, "HEAD, tree"):
            install._finish_git_provenance(self.repository, self.public, receipt)
        self.assertFalse(receipt["git_provenance"]["verified"])

    def test_different_clean_committed_tree_at_end_refuses_success(self):
        original = install._capture_git_provenance(self.repository, self.public)
        self.public["contextbase/public.py"].write_bytes(b"PUBLIC_SYNTHETIC_VALUE = 2\n")
        self.commit(parent=original["head_commit"])
        receipt = {"git_provenance": deepcopy(original)}
        with self.assertRaisesRegex(ValueError, "HEAD, tree"):
            install._finish_git_provenance(self.repository, self.public, receipt)

    def test_default_failure_receipt_does_not_claim_git_checkout_provenance(self):
        output = self.root / "failed-install"
        with patch.object(install.shutil, "which", return_value=None):
            with self.assertRaises(install.InstallVerificationError):
                install.verify_install(self.repository, output)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["source_kind"], "isolated_working_tree_snapshot")
        self.assertEqual(receipt["git_provenance"], {"required": False, "verified": False})
        self.assertTrue(any("not checked" in text for text in receipt["limitations"]))
        self.assertEqual(receipt["commands"], [])

    def test_required_git_preflight_refuses_before_any_install_command(self):
        self.public["contextbase/public.py"].write_bytes(b"# uncommitted source\n")
        output = self.root / "refused-git-install"
        with patch.object(install.shutil, "which", return_value="uv"), \
                patch.object(install.metadata, "version", return_value="84.0.0"), \
                patch.object(install, "_public_package", return_value=dict(self.public)), \
                patch.object(install, "ROOT_FILES", ("uv.lock",)), \
                patch.object(install, "_command") as command:
            with self.assertRaises(install.InstallVerificationError):
                install.verify_install(self.repository, output, require_clean_git=True)
        command.assert_not_called()
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["source_kind"], "clean_git_commit_snapshot")
        self.assertEqual(receipt["git_provenance"], {"required": True, "verified": False})
        self.assertIn("clean Git", receipt["error"])
        self.assertEqual(receipt["commands"], [])

    def test_git_location_override_refuses_without_using_other_index(self):
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.root / "different-index")}):
            with self.assertRaisesRegex(ValueError, "overrides"):
                install._capture_git_provenance(self.repository, self.public)


if __name__ == "__main__":
    unittest.main()
