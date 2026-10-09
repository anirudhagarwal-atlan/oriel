#!/usr/bin/env python3
"""Reproduce Customer360 installation and synthetic application jobs, offline.

Copies allowlisted public implementation/package metadata to an isolated source
snapshot, not a Git checkout. Uses the supplied lock, approved cached distributions, and existing pinned
setuptools. Missing offline dependencies fail; this helper never fetches them.
Retained evidence is private. No provider, vendor, or comparative study runs.

Python API: verify_install(repository, output, *, command_timeout=180,
                           require_clean_git=False) -> dict.
Opt-in --require-clean-git pins and verifies an actual clean local Git commit,
including the helper and workflow. This does not verify a remote publication.
Use an output outside the repository or inside an ignored directory.
The output directory must not already exist. Failure retains receipt.json and
raises InstallVerificationError with its path. Browser DOM proof is separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile


ROOT_FILES = ("README.md", "pyproject.toml", "uv.lock", "LICENSE", "MANIFEST.in")
ASSETS = ("contextbase/customer360/web/index.html", "contextbase/customer360/web/app.js",
          "contextbase/customer360/web/style.css")
STATIC_SUFFIXES = {".json", ".yaml", ".yml", ".txt", ".md", ".html", ".css", ".js"}
STATIC_ROOTS = ("contextbase/data/", "contextbase/connectors/",
                "contextbase/customer360/sample_data/", "contextbase/customer360/web/")
PRIVATE_DIRECTORIES = {"private", "workspace", "workspaces", "credentials", "secrets", "auth",
                       "__pycache__", "node_modules"}
HELPER_FILE = "scripts/check_customer360_install.py"
WORKFLOW_FILE = "tests/customer360/installed_workflow.py"


def _eligible_name(name, *, implementation_python=False):
    lowered = name.lower()
    if lowered.startswith(".") or lowered in PRIVATE_DIRECTORIES or lowered.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    # Public SDK implementation files such as credentials.py are code; this is
    # never permission to discover credential stores or private data directories.
    if implementation_python and Path(name).suffix == ".py":
        return True
    return not re.search(r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?)(?:[._-]|$)", lowered)


class InstallVerificationError(RuntimeError):
    def __init__(self, message, receipt_path):
        super().__init__(message)
        self.receipt_path = Path(receipt_path)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _write(path, value):
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.chmod(path, 0o600)


def _git(repository, *arguments):
    """Local object/index reads only; never invoke a remote or expose stderr."""
    if any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")):
        raise ValueError("Git location/index overrides are not accepted for committed-source proof.")
    result = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs", "-C", str(repository), *arguments],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if result.returncode:
        raise ValueError("Local Git provenance inspection failed; require a readable committed repository.")
    return result.stdout


def _git_blob(raw, object_format):
    return hashlib.new(object_format, b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def _match_committed_bytes(relative, raw, provenance):
    record = provenance["files"].get(relative)
    if record is None or _hash(raw) != record["sha256"] or _git_blob(raw, provenance["object_format"]) != record["blob_oid"]:
        raise ValueError("Public verification bytes differ from the pinned Git blob: " + relative)


def _capture_git_provenance(repository, public):
    """Validate allowlisted source bytes against immutable objects at clean HEAD.

    Git status alone is insufficient: ignored public files, index flags or
    filters can hide a byte mismatch. Compare raw Git blob hashes independently.
    Only approved public paths are read; other tree entries supply names/hashes.
    """
    toplevel = Path(os.fsdecode(_git(repository, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    if toplevel != repository or toplevel.resolve() != repository:
        raise ValueError("Require the actual Git top-level, not a subdirectory or plain source copy.")
    if _git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):
        raise ValueError("Committed-source verification requires a clean Git index and worktree, including nonignored untracked files.")
    commit = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    tree = _git(repository, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip()
    object_format = _git(repository, "rev-parse", "--show-object-format").decode("ascii").strip()
    if object_format not in {"sha1", "sha256"} or any(not re.fullmatch(r"[0-9a-f]{" + str(40 if object_format == "sha1" else 64) + "}", value) for value in (commit, tree)):
        raise ValueError("Unsupported Git object identity.")
    tracked = {}
    for entry in _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata_raw, path = entry.split(b"\t", 1)
        mode, kind, oid = metadata_raw.decode("ascii").split()
        tracked[os.fsdecode(path)] = (mode, kind, oid)
    required = {**public, HELPER_FILE: repository / HELPER_FILE, WORKFLOW_FILE: repository / WORKFLOW_FILE}
    records = {}
    for relative, source in sorted(required.items()):
        if source != repository / relative or source.resolve() != source or source.is_symlink() or not source.is_file():
            raise ValueError("Committed public inputs must be direct regular files: " + relative)
        mode, kind, oid = tracked.get(relative, (None, None, None))
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("Public verification input is not a regular committed Git blob: " + relative)
        raw = source.read_bytes()
        if _git_blob(raw, object_format) != oid:
            raise ValueError("Public verification input differs from its committed Git blob: " + relative)
        records[relative] = {"mode": mode, "blob_oid": oid, "sha256": _hash(raw)}
    provenance = {"spec": "oriel.customer360.git-source-provenance/v1", "required": True, "verified": False,
                  "repository_toplevel": str(repository), "head_commit": commit, "tree_oid": tree,
                  "object_format": object_format, "initial_clean": True, "files": records,
                  "replacement_objects_used": False, "remote_publication_verified": False}
    # Invoking a helper from another directory is allowed only if its bytes
    # match this exact committed helper, never merely a similarly named file.
    _match_committed_bytes(HELPER_FILE, Path(__file__).read_bytes(), provenance)
    return provenance


def _finish_git_provenance(repository, public, receipt):
    original = receipt["git_provenance"]
    current = _capture_git_provenance(repository, public)
    if current != original:
        raise ValueError("Git HEAD, tree or committed public inputs changed during verification.")
    original.update(verified=True, final_clean=True, head_tree_unchanged=True,
                    final_head_commit=current["head_commit"], final_tree_oid=current["tree_oid"])


def _public_package(repository):
    package = repository / "contextbase"
    if package.is_symlink() or not package.is_dir():
        raise ValueError("Require the actual public implementation directory.")
    found = {}
    for current, directories, files in os.walk(package, followlinks=False):
        directories[:] = sorted(name for name in directories if _eligible_name(name) and not (Path(current) / name).is_symlink())
        for filename in sorted(files):
            if not _eligible_name(filename, implementation_python=True):
                continue
            path = Path(current) / filename
            relative = path.relative_to(repository).as_posix()
            if path.is_symlink():
                raise ValueError("Public package copy refuses symbolic links.")
            if not path.is_file():
                continue
            if path.suffix == ".py" or (relative.startswith(STATIC_ROOTS) and (
                    path.suffix in STATIC_SUFFIXES or path.name in {"LICENSE", "NOTICE"})):
                found[relative] = path
    if not found:
        raise ValueError("No public implementation files found.")
    for name in ASSETS:
        if name not in found:
            raise ValueError("Require all three application assets.")
    sample = [name for name in found if name.startswith("contextbase/customer360/sample_data/") and name.endswith(".json")]
    if len(sample) != 25:
        raise ValueError("Require exactly 25 packaged sample JSON files.")
    if any("oracle" in Path(name).name.lower() or "expected" in Path(name).name.lower() for name in found):
        raise ValueError("Oracle/expected data must not be packaged with the application.")
    return found


def _command(arguments, cwd, output, stage, timeout, receipt):
    log = output / (stage + ".log")
    started = time.monotonic()
    record = {"stage": stage, "arguments": [str(value) for value in arguments],
              "cwd": str(cwd), "log": log.name}
    receipt["commands"].append(record)
    with log.open("xb") as handle:
        os.chmod(log, 0o600)
        process = subprocess.Popen([str(value) for value in arguments], cwd=cwd,
                                   stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            record.update(status="timed_out", elapsed_seconds=round(time.monotonic() - started, 6),
                          log_sha256=_hash(log.read_bytes()))
            raise RuntimeError("Bounded verification command timed out: " + stage) from None
    record.update(returncode=returncode, status="passed" if returncode == 0 else "failed",
                  elapsed_seconds=round(time.monotonic() - started, 6), log_sha256=_hash(log.read_bytes()))
    if returncode:
        raise RuntimeError("Verification command failed: " + stage + " (exit " + str(returncode) + "); inspect its private log.")


def _installed_cli_http(python, executable, cwd, output, expected_assets, timeout):
    log = output / "installed-cli-app.log"
    command = [str(python), "-I", str(executable), "--workspace", str(output / "cli-workspace"), "customer", "app", "--port", "0"]
    with log.open("xb") as handle:
        os.chmod(log, 0o600)
        process = subprocess.Popen(command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + min(timeout, 45)
            origin = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Installed application CLI exited before loopback startup.")
                # The CLI prints only its local address and scope notice here.
                content = log.read_text(errors="replace")[:4096]
                match = re.search(r"^Oriel Customer 360: (http://127\.0\.0\.1:([1-9][0-9]{0,4}))$", content, re.MULTILINE)
                if match:
                    origin, port = match.group(1), int(match.group(2))
                    if not 1 <= port <= 65535:
                        raise RuntimeError("Invalid application loopback port.")
                    break
                time.sleep(0.05)
            if not origin:
                raise RuntimeError("Installed CLI did not announce a loopback address.")
            def get(path, cookie=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
                    response = conn.getresponse()
                    raw = response.read(2_000_000)
                    if response.status != 200:
                        raise RuntimeError("Installed app HTTP route failed: " + path)
                    return dict(response.getheaders()), raw
                finally:
                    conn.close()
            headers, home = get("/")
            cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
            if not cookie:
                raise RuntimeError("Installed app did not create a local session.")
            if _hash(home) != expected_assets["contextbase/customer360/web/index.html"]:
                raise RuntimeError("Installed app did not serve the packaged HTML.")
            responses = {"/": _hash(home)}
            for path, filename in (("/app.js", "app.js"), ("/style.css", "style.css")):
                _, raw = get(path, cookie)
                if _hash(raw) != expected_assets["contextbase/customer360/web/" + filename]:
                    raise RuntimeError("Installed UI asset differs from the checked package.")
                responses[path] = _hash(raw)
            _, raw = get("/api/session", cookie)
            session = json.loads(raw)
            if not isinstance(session.get("csrf"), str) or not session["csrf"]:
                raise RuntimeError("Installed app session is unavailable.")
            # Session/cookie secrets never enter the retained receipt.
            return {"status": "passed", "arguments": command, "isolated_python": True,
                    "numeric_loopback": True, "asset_sha256": responses,
                    "session_established": True, "csrf_retained": False,
                    "model_origin": session.get("model_origin"), "source_calls": 0, "model_calls": 0}
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)


def verify_install(repository, output, *, command_timeout=180, require_clean_git=False):
    """Offline isolated source snapshot + installed wheel; optional Git proof."""
    repository, output = Path(repository).absolute(), Path(output).absolute()
    if repository.resolve() != repository or output.parent.resolve() != output.parent:
        raise ValueError("Use direct local paths without symbolic-link parents.")
    if type(command_timeout) is not int or not 30 <= command_timeout <= 600:
        raise ValueError("Use a command timeout between 30 and 600 seconds.")
    if type(require_clean_git) is not bool:
        raise ValueError("require_clean_git must be a boolean.")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    old_umask = os.umask(0o077)
    receipt_path = output / "receipt.json"
    receipt = {"spec": "oriel.customer360.clean-install/v1", "status": "running", "synthetic": True,
               "started_at": datetime.now(timezone.utc).isoformat(), "model_calls": 0, "vendor_calls": 0,
               "network_dependency_fetches": 0, "offline_required": True, "commands": [],
               "source_kind": "clean_git_commit_snapshot" if require_clean_git else "isolated_working_tree_snapshot",
               "git_provenance": {"required": require_clean_git, "verified": False},
               "claim_eligible": False, "limitations": ["This verifies packaged synthetic workflows, not vendor authentication or actual model behavior.",
                                                     "Installed Application jobs and CLI HTTP assets are checked; browser DOM proof is separate.",
                                                     "A copied source snapshot is not itself a Git checkout or proof of remote publication."]}
    if not require_clean_git:
        receipt["limitations"].append("Git cleanliness and committed-source provenance were not checked; source files may include local changes.")
    try:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("The existing uv executable is required; no installer is run.")
        if metadata.version("setuptools") != "84.0.0":
            raise RuntimeError("Run this helper with the existing approved setuptools 84.0.0 environment.")
        public = _public_package(repository)
        checkout = output / "source-snapshot"
        checkout.mkdir(mode=0o700)
        copied = {}
        for name in ROOT_FILES:
            source = repository / name
            if source.is_symlink() or not source.is_file():
                raise ValueError("Missing allowlisted public file: " + name)
            public[name] = source
        if require_clean_git:
            receipt["git_provenance"] = _capture_git_provenance(repository, public)
        for relative, source in public.items():
            target = checkout / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            raw = source.read_bytes()
            if require_clean_git:
                _match_committed_bytes(relative, raw, receipt["git_provenance"])
            target.write_bytes(raw)
            os.chmod(target, 0o600)
            copied[relative] = _hash(raw)
        package_manifest = {key: value for key, value in copied.items() if key.startswith("contextbase/")}
        _write(output / "package-files.json", package_manifest)
        receipt["public_file_count"] = len(copied)
        receipt["copied_code_sha256"] = _hash(_canonical(copied).encode())
        receipt["lock_sha256"] = copied["uv.lock"]
        receipt["source_files"] = copied
        receipt["helper_sha256"] = _hash(Path(__file__).read_bytes())
        _write(receipt_path, receipt)
        python = Path(sys.executable).absolute()
        _command([uv, "sync", "--frozen", "--offline", "--extra", "connectors", "--no-python-downloads", "--python", python],
                 checkout, output, "isolated-snapshot-sync", command_timeout, receipt)
        _command([checkout / ".venv" / "bin" / "oriel", "--help"], checkout, output,
                 "documented-entrypoint", command_timeout, receipt)
        if _hash((checkout / "uv.lock").read_bytes()) != receipt["lock_sha256"]:
            raise RuntimeError("Frozen installation changed the copied lock.")
        wheel_dir = output / "wheels"
        wheel_dir.mkdir(mode=0o700)
        build = "from setuptools.build_meta import build_wheel; build_wheel(" + repr(str(wheel_dir)) + ")"
        _command([python, "-I", "-c", build], checkout, output, "build-wheel",
                 command_timeout, receipt)
        wheels = list(wheel_dir.glob("oriel-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Require exactly one built Oriel wheel.")
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            for relative, expected in package_manifest.items():
                if relative not in names or _hash(archive.read(relative)) != expected:
                    raise RuntimeError("Wheel omitted or altered public package file: " + relative)
            if any("oracle" in Path(name).name.lower() or "expected" in Path(name).name.lower() for name in names):
                raise RuntimeError("Wheel unexpectedly includes oracle/expected material.")
            licenses = [name for name in names if ".dist-info/licenses/" in name and name.endswith("/LICENSE")]
            if len(licenses) != 1 or _hash(archive.read(licenses[0])) != copied["LICENSE"]:
                raise RuntimeError("Wheel did not preserve the project's license.")
        receipt["wheel"] = {"name": wheel.name, "sha256": _hash(wheel.read_bytes()),
                            "package_files_verified": len(package_manifest),
                            "ui_assets_verified": len(ASSETS), "sample_json_verified": 25,
                            "oracle_packaged": False, "project_license_verified": True}
        requirements = output / "locked-requirements.txt"
        _command([uv, "export", "--frozen", "--offline", "--extra", "connectors", "--no-emit-project",
                  "--no-header", "--format", "requirements.txt", "--output-file", requirements],
                 checkout, output, "export-locked-dependencies", command_timeout, receipt)
        if "--hash=sha256:" not in requirements.read_text():
            raise RuntimeError("Locked export omitted artifact hashes.")
        outside = output / "outside-checkout"
        outside.mkdir(mode=0o700)
        installed = output / "installed-venv"
        _command([uv, "venv", "--offline", "--no-python-downloads", "--python", python, installed],
                 outside, output, "create-installed-venv", command_timeout, receipt)
        installed_python = installed / "bin" / "python"
        _command([uv, "pip", "install", "--offline", "--no-python-downloads", "--no-build", "--no-deps",
                  "--require-hashes", "--python", installed_python, "-r", requirements],
                 outside, output, "install-locked-dependencies", command_timeout, receipt)
        wheel_requirement = output / "wheel-requirement.txt"
        wheel_requirement.write_text("oriel @ " + wheel.as_uri() + " --hash=sha256:" + receipt["wheel"]["sha256"] + "\n")
        os.chmod(wheel_requirement, 0o600)
        _command([uv, "pip", "install", "--offline", "--no-deps", "--require-hashes", "--python", installed_python,
                  "-r", wheel_requirement], outside, output, "install-wheel", command_timeout, receipt)
        workflow_source = repository / WORKFLOW_FILE
        if workflow_source.is_symlink() or not workflow_source.is_file():
            raise RuntimeError("Missing the dedicated installed-workflow acceptance helper.")
        workflow = outside / "installed_workflow.py"
        workflow_raw = workflow_source.read_bytes()
        if require_clean_git:
            _match_committed_bytes(WORKFLOW_FILE, workflow_raw, receipt["git_provenance"])
        workflow.write_bytes(workflow_raw)
        os.chmod(workflow, 0o600)
        receipt["workflow_sha256"] = _hash(workflow.read_bytes())
        workflow_output = output / "installed-workflow"
        _command([installed_python, "-I", workflow, "--output", workflow_output,
                  "--package-manifest", output / "package-files.json", "--excluded-checkout", checkout],
                 outside, output, "installed-workflow", command_timeout, receipt)
        result = json.loads((workflow_output / "receipt.json").read_text())
        if result.get("status") != "passed" or len(result.get("profiles", [])) != 2:
            raise RuntimeError("Installed workflows did not produce complete passing evidence.")
        receipt["installed_workflow"] = result
        receipt["installed_cli_http"] = _installed_cli_http(installed_python, installed / "bin" / "oriel",
                                                           outside, output, package_manifest, command_timeout)
        current_public = _public_package(repository)
        current_public.update({name: repository / name for name in ROOT_FILES})
        current_hashes = {name: _hash(path.read_bytes()) for name, path in current_public.items()}
        if current_hashes != copied:
            raise RuntimeError("Public source or lock changed during this verification; rerun against a stable snapshot.")
        if require_clean_git:
            _finish_git_provenance(repository, current_public, receipt)
        receipt.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat())
        _write(receipt_path, receipt)
        return receipt
    except Exception as error:
        receipt.update(status="failed", error_type=type(error).__name__,
                       error=str(error)[:1000], finished_at=datetime.now(timezone.utc).isoformat())
        _write(receipt_path, receipt)
        raise InstallVerificationError("Clean installation verification failed; inspect " + str(receipt_path), receipt_path) from error
    finally:
        os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--command-timeout", type=int, default=180)
    parser.add_argument("--require-clean-git", action="store_true", help="Require clean local Git HEAD and committed bytes for every public input and verification helper.")
    args = parser.parse_args()
    try:
        result = verify_install(args.repository, args.output, command_timeout=args.command_timeout,
                                require_clean_git=args.require_clean_git)
        print(json.dumps({"status": result["status"], "receipt": str(Path(args.output).absolute() / "receipt.json"),
                          "profiles": len(result["installed_workflow"]["profiles"]), "model_calls": 0, "vendor_calls": 0,
                          "source_kind": result["source_kind"], "git_verified": result["git_provenance"]["verified"]}))
    except InstallVerificationError as error:
        print(json.dumps({"status": "failed", "receipt": str(error.receipt_path)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
