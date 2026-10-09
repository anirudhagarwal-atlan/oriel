"""Measured stdio MCP access and equal scratch computation for evaluations.

The scratch database contains only results already returned during this trial.
It cannot open the source snapshot, provider APIs, files, or other databases.
No model, provider credentials, additional packages, or shell is used here.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

from .rystic import local_url
from .tools import definition
from .workspace import Workspace

MAX_REQUEST_BYTES = 1_048_576
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_TRIAL_BYTES = 16 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"


class EvaluationToolError(RuntimeError):
    """A bounded transport or scratch-capacity failure; count it as a failure."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = item
        return result

    def invalid(_):
        raise ValueError("Non-finite JSON.")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


class StdioMCPClient:
    """Launch this package's actual CLI and exchange bounded JSON-RPC frames.

    ``trace`` contains sizes and durations, never payloads or child stderr.
    The client is sequential and intended for one evaluation trial at a time.
    Transport failures close the child immediately; tool errors are returned as
    decoded JSON so the agent can recover within the same trial.
    """

    def __init__(self, workspace: Workspace | str | Path, arm="sql_context", *,
                 stripe_url=None, linear_url=None, timeout=20.0, verification_policy="per_call", evaluation_context=False):
        if arm not in {"sql_context", "rystic_source"}:
            raise ValueError("Evaluation arm must be sql_context or rystic_source.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("MCP timeout must be greater than zero and at most 120 seconds.")
        if arm != "rystic_source" and (stripe_url is not None or linear_url is not None):
            raise ValueError("Simulator URLs are only valid for rystic_source.")
        if verification_policy not in ("per_call", "evaluation_session"):
            raise ValueError("verification_policy must be per_call or evaluation_session.")
        if arm != "rystic_source" and verification_policy != "per_call":
            raise ValueError("Evaluation session verification is only valid for rystic_source.")
        if type(evaluation_context) is not bool:
            raise ValueError("evaluation_context must be a boolean.")
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.arm, self.timeout = arm, float(timeout)
        self.command = [sys.executable, "-m", "contextbase", "--workspace", str(self.workspace.root), "serve", "--arm", arm]
        if evaluation_context:
            self.command.append("--evaluation-context")
        urls = []
        if arm == "rystic_source":
            if verification_policy != "per_call":
                self.command.extend(["--verification-policy", verification_policy])
            for option, url, default in (("--stripe-url", stripe_url, "http://127.0.0.1:18981"),
                                         ("--linear-url", linear_url, "http://127.0.0.1:18982")):
                value = local_url(default if url is None else url)
                urls.append(value)
                self.command.extend([option, value])
            if urls[0] == urls[1]:
                raise ValueError("Select separate Stripe and Linear simulator instances.")
        self.trace = []
        self.process = None
        self._definitions = None
        self._next_id = 1
        self._buffer = bytearray()

    def __enter__(self):
        if self.process is not None:
            raise EvaluationToolError("MCP client has already been started.")
        try:
            # The package location keeps source checkouts and installed wheels
            # importable without adding the user's workspace to Python's path.
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0,
                                            cwd=str(Path(__file__).resolve().parent.parent))
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                   "clientInfo": {"name": "contextbase-evaluation", "version": "1"}})
            if not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION:
                raise EvaluationToolError("MCP initialization returned an unsupported protocol.")
            self._request("notifications/initialized", {}, notification=True)
            listing = self._request("tools/list", {})
            definitions = listing.get("tools") if isinstance(listing, dict) else None
            if not isinstance(definitions, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                                                       or not isinstance(item.get("inputSchema"), dict) for item in definitions):
                raise EvaluationToolError("MCP returned an invalid tool list.")
            names = [item["name"] for item in definitions]
            if len(names) != len(set(names)) or "compute" in names:
                raise EvaluationToolError("MCP returned conflicting tool names.")
            self._definitions = definitions
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()

    def definitions(self):
        if self._definitions is None or self.process is None or self.process.poll() is not None:
            raise EvaluationToolError("MCP client is not running.")
        return copy.deepcopy(self._definitions)

    def _request(self, method, params, *, notification=False):
        started = time.perf_counter()
        received = 0
        status = "failed"
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            request["id"] = self._next_id
            self._next_id += 1
        try:
            raw = (_json(request) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("Tool arguments must be finite JSON values.") from None
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("MCP request exceeded the one MiB frame limit.")
        try:
            if self.process is None or self.process.poll() is not None:
                raise EvaluationToolError("MCP subprocess is not running.")
            deadline = started + self.timeout
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                offset = 0
                while offset < len(raw):
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0 or not selector.select(remaining):
                        raise EvaluationToolError("MCP request timed out.")
                    offset += os.write(self.process.stdin.fileno(), raw[offset:])
                selector.unregister(self.process.stdin)
                if notification:
                    status = "ok"
                    return None
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while b"\n" not in self._buffer:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0 or not selector.select(remaining):
                        raise EvaluationToolError("MCP response timed out.")
                    chunk = os.read(self.process.stdout.fileno(), min(65536, MAX_FRAME_BYTES + 1 - len(self._buffer)))
                    if not chunk:
                        raise EvaluationToolError("MCP subprocess closed before returning a response.")
                    self._buffer.extend(chunk)
                    received += len(chunk)
                    if len(self._buffer) > MAX_FRAME_BYTES:
                        raise EvaluationToolError("MCP response exceeded the bounded frame limit.")
                line, _, remainder = self._buffer.partition(b"\n")
                self._buffer = bytearray(remainder)
                received = len(line) + 1
            response = _decode(line)
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != request["id"]:
                raise EvaluationToolError("MCP returned a malformed or mismatched response.")
            if "error" in response or "result" not in response:
                raise EvaluationToolError("MCP returned a protocol error.")
            status = "tool_error" if isinstance(response["result"], dict) and response["result"].get("isError") else "ok"
            return response["result"]
        except EvaluationToolError:
            self.close()
            raise
        except (OSError, ValueError, TypeError):
            self.close()
            raise EvaluationToolError("MCP transport returned an invalid response or became unavailable.") from None
        finally:
            self.trace.append({"method": method, "tool": params.get("name") if method == "tools/call" else None,
                               "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                               "request_bytes": len(raw), "response_bytes": received, "status": status})

    def call(self, name, arguments=None):
        self.definitions()  # Check process liveness without skipping server validation.
        result = self._request("tools/call", {"name": name, "arguments": {} if arguments is None else arguments})
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "text":
            self.close()
            raise EvaluationToolError("MCP tool returned unsupported content.")
        try:
            return _decode(content[0]["text"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            self.close()
            raise EvaluationToolError("MCP tool returned invalid JSON content.") from None


COMPUTE_DEFINITION = definition(
    "compute",
    "Run one read-only DuckDB SELECT over this trial's previously returned tool results. "
    "Both evaluation arms have this same calculator. The only stored data is "
    "scratch.tool_results(call_id INTEGER, tool_name VARCHAR, result_json JSON). "
    "result_json is the original tool result, without its stored_call_id wrapper. "
    "For example: SELECT j.value->>'id' AS id FROM scratch.tool_results r, "
    "json_each(r.result_json, '$.data') j WHERE r.call_id=1. "
    "Use the actual result's JSON structure. Results remain available even when the "
    "model's tool-result display is truncated. No files, network, source snapshot, "
    "or write statements are accessible. Compute results are also stored.",
    {"sql": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["sql"])


class ComputedTools:
    """Own an MCP client and temporary, per-trial scratch result database."""

    def __init__(self, base_client):
        self.base_client = base_client
        self.workspace = None
        self._temporary = None
        self._next_call_id = 1
        self.stored_bytes = 0
        self.compute_trace = []

    @property
    def trace(self):
        return [*self.base_client.trace, *self.compute_trace]

    def __enter__(self):
        if self._temporary is not None:
            raise EvaluationToolError("Scratch tools have already been started.")
        self._temporary = tempfile.TemporaryDirectory(prefix="contextbase-evaluation-")
        self.workspace = Workspace(self._temporary.name)
        try:
            with self.workspace._connect(writable=True) as conn:
                conn.execute("CREATE SCHEMA scratch")
                conn.execute("CREATE TABLE scratch.tool_results(call_id INTEGER PRIMARY KEY, tool_name VARCHAR, result_json JSON)")
            self.base_client.__enter__()
            return self
        except Exception:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.base_client.__exit__(exc_type, exc, traceback)
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self.workspace = None

    def definitions(self):
        return [*self.base_client.definitions(), copy.deepcopy(COMPUTE_DEFINITION)]

    def _store(self, name, result):
        if self.workspace is None:
            raise EvaluationToolError("Scratch tools are not running.")
        try:
            payload = _json(result)
        except (TypeError, ValueError):
            raise EvaluationToolError("Tool result is not finite JSON and cannot be retained.") from None
        size = len(payload.encode("utf-8"))
        if size > MAX_RESULT_BYTES:
            raise EvaluationToolError("Tool result exceeded the two MiB scratch limit; trial cannot retain the complete result.")
        if self.stored_bytes + size > MAX_TRIAL_BYTES:
            raise EvaluationToolError("Trial exceeded the sixteen MiB scratch limit; trial cannot retain another complete result.")
        call_id = self._next_call_id
        with self.workspace._connect(writable=True) as conn:
            conn.execute("INSERT INTO scratch.tool_results VALUES (?, ?, ?)", [call_id, name, payload])
        self._next_call_id += 1
        self.stored_bytes += size
        return {"stored_call_id": call_id, "result": result}

    def call(self, name, arguments=None):
        if self.workspace is None:
            raise EvaluationToolError("Scratch tools are not running.")
        if name != "compute":
            result = self.base_client.call(name, arguments)
            return self._store(name, result)
        started = time.perf_counter()
        status = "ok"
        arguments = {} if arguments is None else arguments
        try:
            if not isinstance(arguments, dict) or set(arguments) - {"sql", "max_rows"} or "sql" not in arguments:
                raise ValueError("compute requires sql and accepts only an optional max_rows.")
            result = self.workspace.query(**arguments)
        except ValueError as exc:
            status, result = "tool_error", {"error": str(exc)}
        except Exception:
            status, result = "tool_error", {"error": "Computation failed. Use one SELECT over this trial's scratch results; external access is disabled."}
        wrapped = self._store(name, result)
        self.compute_trace.append({"method": "compute", "tool": "compute", "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                                   "request_bytes": len(_json(arguments).encode("utf-8")),
                                   "response_bytes": len(_json(wrapped).encode("utf-8")), "status": status})
        return wrapped
