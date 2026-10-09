"""Explicit local browser-test server; never a real model or vendor smoke.

Only this test entry point installs the deterministic transport. The packaged
application has no HTTP parameter for injecting a transport or an answer.
"""
from __future__ import annotations

import argparse
import json

from contextbase.customer360.application import Application
from contextbase.customer360.webserver import Server
from contextbase.workspace import Workspace


class ScriptedBrowserClient:
    """Select actual scoped tool results; expected-answer oracles are never read."""
    def complete(self, system, messages, max_cost_usd):
        first = json.loads(messages[0]["content"])
        if "source_evidence" in first:
            evidence = first["source_evidence"]
            if first.get("stage") == "define":
                kinds = {binding["kind"] for binding in evidence["profile"]["bindings"]}
                if "billing" in kinds:
                    definitions = [{"name": "outstanding_invoices", "description": "Scripted example: sum remaining minor units on open invoices, separately by currency.",
                                    "kind": "billing", "operation": "sum", "field": "amount", "unit": "minor_currency",
                                    "currency_field": "currency", "filters": [{"field": "status", "operator": "eq", "value": "open"}]}]
                else:
                    definitions = [{"name": "open_pipeline", "description": "Scripted example: sum open opportunity amounts, separately by currency.",
                                    "kind": "commercial", "operation": "sum", "field": "amount", "unit": "currency",
                                    "currency_field": "currency", "filters": [{"field": "is_closed", "operator": "eq", "value": False}]}]
                action = {"definitions": definitions, "gaps": [{"code": "scripted_transport", "description": "Browser orchestration fixture; no real inference occurred."}]}
            else:
                action = {"observations": [{"connection": binding["connection"], "entity": binding["entity"], "kind": binding["kind"],
                                            "field_roles": sorted(binding["fields"]),
                                            "evidence_ids": [row["evidence_id"] for row in evidence["samples"]
                                                             if (row["connection"], row["entity"]) == (binding["connection"], binding["entity"])],
                                            "notes": "Scripted observation of mapped synthetic evidence; review remains required."}
                                           for binding in evidence["profile"]["bindings"]],
                          "gaps": [{"code": "business_review_needed", "description": "Synthetic source fields do not certify business policy."}]}
        elif len(messages) == 1:
            context = first["reviewed_context"]
            tools = [{"tool": "metric", "arguments": {"definition_name": context["definitions"][0]["name"]}}]
            row = next((row for row in context["evidence_index"] if "text" in row["fields"]), None)
            if row:
                tools.append({"tool": "read_evidence", "arguments": {"evidence_id": row["evidence_id"], "field": "text"}})
                if "updates" in row["fields"]:
                    tools.append({"tool": "read_evidence", "arguments": {"evidence_id": row["evidence_id"], "field": "updates"}})
            action = {"tools": tools}
        else:
            metrics, quotes = [], []
            for message in messages[1:]:
                value = json.loads(message["content"])
                if value.get("type") != "tool":
                    continue
                result = value["result"]
                if value["tool"] == "metric":
                    metrics.append({"definition_name": result["definition"]["name"], "result_id": result["result_id"]})
                elif value["tool"] == "read_evidence":
                    quotes.append({key: result[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")})
            action = {"answer": {"metrics": metrics, "quotes": quotes}}
        return {"response": json.dumps(action), "cost_usd": 0.0, "is_error": False,
                "model_usage": {"scripted-browser-transport": {}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--synthetic-fixtures", action="store_true", required=True)
    args = parser.parse_args()
    app = Application(Workspace(args.workspace), model_client_factory=ScriptedBrowserClient)
    server = None
    try:
        server = Server(app, args.port)
        print(json.dumps({"origin": server.origin, "model_origin": app.model_origin, "actual_model_calls": 0}), flush=True)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        app.close()


if __name__ == "__main__":
    main()
