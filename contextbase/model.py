"""A bounded, tool-free Claude CLI adapter for synthetic benchmark trials.

The CLI's configured model is used. Tool execution stays in our Python harness;
the model receives only supplied strings and returns a JSON action.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import time


def finite_number(value, *, positive=False) -> bool:
    """Booleans, strings, NaN and infinity are not monetary/time quantities."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def reported_cost(response: dict) -> float | None:
    value = response.get("cost_usd")
    return float(value) if finite_number(value) else None


def strict_json_loads(text: str):
    def constant(value):
        raise ValueError("Non-finite JSON numbers are not allowed")

    def floating(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON numbers are not allowed")
        return number

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON keys are not allowed")
            result[key] = value
        return result

    return json.loads(text, parse_constant=constant, parse_float=floating, object_pairs_hook=pairs)


def parse_action(text: str, *, allow_text_answer=False) -> dict:
    """Parse an object, allowing only a surrounding Markdown code fence."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 32_000:
        raise ValueError("Model response must be text of at most 32 KB")
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        opening, separator, body = value.partition("\n")
        if not separator or opening not in ("```", "```json"):
            raise ValueError("Expected a JSON code fence")
        value = body.rsplit("```", 1)[0].strip()
    result = strict_json_loads(value)
    if not isinstance(result, dict):
        raise ValueError("Expected one JSON object")
    if set(result) == {"answer"}:
        if allow_text_answer and isinstance(result["answer"], str):
            if not result["answer"].strip() or len(result["answer"].encode("utf-8")) > 16_000:
                raise ValueError("Final answer must be nonempty text of at most 16 KB")
            return result
        if (type(result["answer"]) not in (int, float)
                or not finite_number(abs(result["answer"]))):
            raise ValueError("Final answer must be a JSON number")
        return result
    if set(result) == {"tool", "arguments"}:
        result = {"tools": [result]}
    if set(result) != {"tools"} or not isinstance(result["tools"], list):
        raise ValueError("Expected answer or tools action")
    if not 1 <= len(result["tools"]) <= 4:
        raise ValueError("A turn must contain 1–4 tool calls")
    for action in result["tools"]:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"}:
            raise ValueError("Invalid tool action")
        if not isinstance(action["tool"], str) or not isinstance(action["arguments"], dict):
            raise ValueError("Invalid tool name or arguments")
    return result


class ClaudeCLI:
    """Run one independent CLI inference without native tools or MCP servers."""

    def __init__(self, executable: str | None = None, timeout_seconds: float = 90):
        if not finite_number(timeout_seconds, positive=True) or timeout_seconds > 3600:
            raise ValueError("Model timeout must be finite and between zero and 3600 seconds")
        self.executable = executable or shutil.which("claude")
        if not self.executable:
            raise RuntimeError("Claude CLI is not installed or is not on PATH")
        self.timeout_seconds = timeout_seconds

    def complete(self, system: str, messages: list[dict], max_cost_usd: float = 0.25) -> dict:
        if not finite_number(max_cost_usd, positive=True):
            raise ValueError("Model budget must be a positive finite number")
        prompt = json.dumps(messages, ensure_ascii=False, allow_nan=False)
        if len(prompt.encode()) + len(system.encode()) > 150_000:
            raise ValueError("Benchmark prompt exceeds the 150 KB limit")
        args = [
            self.executable, "-p", "--restricted", "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-session-persistence",
            "--output-format", "json", "--system-prompt", system,
            "--max-budget-usd", str(max_cost_usd),
        ]
        started = time.perf_counter()
        # A fresh empty directory prevents accidental project context loading.
        try:
            with tempfile.TemporaryDirectory(prefix="contextbase-model-") as workdir:
                completed = subprocess.run(
                    args, input=prompt, text=True, capture_output=True,
                    cwd=workdir, timeout=self.timeout_seconds, check=False,
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"is_error": True, "error_type": type(exc).__name__,
                    "elapsed_seconds": time.perf_counter() - started,
                    "cost_usd": None, "cost_known": False, "spend_unknown": True}
        elapsed = time.perf_counter() - started
        failure = {"is_error": True, "exit_code": completed.returncode,
                   "elapsed_seconds": elapsed, "cost_usd": None,
                   "cost_known": False, "spend_unknown": True}
        if len(completed.stdout.encode()) > 2_000_000:
            return {**failure, "error_type": "response_size_limit"}
        try:
            native = strict_json_loads(completed.stdout)
        except (ValueError, RecursionError):
            return {**failure, "error_type": "invalid_cli_json"}
        if not isinstance(native, dict):
            return {**failure, "error_type": "invalid_cli_result"}
        raw = native.get("result", "")
        cost = reported_cost({"cost_usd": native.get("total_cost_usd")})
        result = {
            "elapsed_seconds": elapsed,
            "cost_usd": cost, "cost_known": cost is not None, "spend_unknown": cost is None,
            "usage": native.get("usage", {}),
            "model_usage": native.get("modelUsage", {}),
            "duration_ms": native.get("duration_ms"),
            "duration_api_ms": native.get("duration_api_ms"),
            "num_turns": native.get("num_turns"),
            "subtype": native.get("subtype"),
            "is_error": bool(native.get("is_error")) or completed.returncode != 0,
        }
        if completed.returncode:
            # Preserve structured cost even on failure; stderr may contain
            # credentials or local paths and is never copied to the result.
            result["exit_code"] = completed.returncode
            result["error_type"] = "cli_exit"
        if not isinstance(raw, str) or len(raw.encode()) > 32_000:
            result["parse_error"] = "Model answer exceeds the 32 KB limit or is not text"
            return result
        result["response"] = raw
        try:
            result["action"] = parse_action(raw)
        except (ValueError, RecursionError) as exc:
            result["parse_error"] = str(exc)[:300]
        return result
