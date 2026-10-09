"""Real local publications and reviewed passage contracts; no model/vendor calls."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.connectors import service
from contextbase.customer360 import answers, contexts, profiles, snapshots
from contextbase.customer360.contracts import PROFILE_SPEC, sha256
from contextbase.customer360.identity import create_customer, propose_links, approve_links
from contextbase.workspace import Workspace


def passage(*, path="/body", source="field", encoding="rfc3339"):
    time = {"source": source, "path": "/at", "encoding": encoding, "meaning": "passage_created"}
    if source != "item":
        time["field"] = "event"
    if source == "attachment":
        time["attachment"] = "call"
    return {"name": "message", "field": "text", "text_path": path, "time": time}


def row(identifier, body, time="2026-01-01T00:00:00Z", *, customer="party-a", lookup=None):
    return [identifier, customer, json.dumps({"body": body}), json.dumps({"at": time}), lookup or "meta-" + identifier]


class PassageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = Workspace(self.root / "workspace")
        self.a = create_customer(self.w, "synthetic-a")["id"]
        self.b = create_customer(self.w, "synthetic-b")["id"]
        self.rows = [row("event-a", "First observed passage.")]
        self.metadata = None

    @staticmethod
    def write(path, header, rows):
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    def build(self, *, mapping=None, metadata=None, unmapped_binding=False):
        self.mapping = copy.deepcopy(mapping or passage())
        self.metadata = metadata
        self.write(self.root / "events.csv", ["id", "customer", "body", "metadata", "lookup"], self.rows)
        service.add(self.w, "csv", name="events", path=self.root / "events.csv")
        service.sync(self.w, "events", bootstrap_context=False)
        binding = {"connection": "events", "entity": "event", "table": "events.records", "kind": "conversation",
                   "record_id": "id", "customer_ref": {"connection": "events", "entity": "party", "column": "customer"},
                   "fields": {"text": "body", "event": "metadata"}, "passages": [self.mapping]}
        self.sources = ["events"]
        if metadata is not None:
            self.write(self.root / "metadata.csv", ["id", "customer", "time"], metadata)
            service.add(self.w, "csv", name="metadata", path=self.root / "metadata.csv")
            service.sync(self.w, "metadata", bootstrap_context=False)
            self.sources.append("metadata")
            binding["attachments"] = [{"name": "call", "connection": "metadata", "entity": "call",
                "table": "metadata.records", "record_id": "id", "local_key": "lookup", "remote_key": "id",
                "customer_ref": {"connection": "events", "entity": "party", "column": "customer"},
                "fields": {"event": "time"}}]
        links = [{"connection": "events", "entity": "party", "source_id": key, "customer_id": customer,
                  "basis": "Explicit synthetic source identifier"} for key, customer in (("party-a", self.a), ("party-b", self.b))]
        proposed = propose_links(self.w, links)
        approve_links(self.w, proposed["proposal_id"], proposed["approval_sha256"], "synthetic-reviewer")
        bindings = [binding]
        if unmapped_binding:
            service.add(self.w, "csv", name="unmapped", path=self.root / "events.csv")
            service.sync(self.w, "unmapped", bootstrap_context=False)
            self.sources.append("unmapped")
            other = copy.deepcopy(binding)
            other.pop("passages")
            other["connection"], other["table"] = "unmapped", "unmapped.records"
            other["entity"] = "unmapped_event"
            bindings.append(other)
        self.profile = {"spec_version": PROFILE_SPEC, "name": "passage_review", "description": "Synthetic reviewed passages", "bindings": bindings}
        self.snapshot = snapshots.capture_snapshot(self.w, self.sources)["snapshot_id"]
        candidate = profiles.propose_profile(self.w, self.profile, self.snapshot)
        profiles.approve_profile(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")
        profiles.materialize(self.w, "passage_review", self.snapshot)
        rules = [{"name": "observed_records", "description": "Count attributed synthetic records", "kind": "conversation",
                  "operation": "count", "field": None, "filters": [], "unit": "records", "currency_field": None}]
        candidate = contexts.propose_context(self.w, "passage_review", self.snapshot, rules)
        contexts.approve_context(self.w, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-reviewer")

    def refresh(self):
        self.write(self.root / "events.csv", ["id", "customer", "body", "metadata", "lookup"], self.rows)
        service.sync(self.w, "events", bootstrap_context=False)
        if self.metadata is not None:
            self.write(self.root / "metadata.csv", ["id", "customer", "time"], self.metadata)
            service.sync(self.w, "metadata", bootstrap_context=False)
        self.snapshot = snapshots.capture_snapshot(self.w, self.sources)["snapshot_id"]
        profiles.materialize(self.w, "passage_review", self.snapshot)

    def latest(self, customer=None, **kwargs):
        return answers.latest_evidence(self.w, "passage_review", self.snapshot, customer or self.a, "conversation", **kwargs)

    def read(self, quote, **kwargs):
        args = {key: quote[key] for key in ("field", "passage_name", "json_pointer", "start", "end")}
        return answers.read_evidence(self.w, "passage_review", self.snapshot, self.a, quote["evidence_id"], **{**args, **kwargs})

    @staticmethod
    def selection(latest):
        keys = ("evidence_id", "field", "start", "end", "text", "quote_id", "passage_name", "json_pointer")
        return {"metrics": [], "quotes": [{key: q[key] for key in keys} for q in latest["quotes"]],
                "latest": [{key: latest[key] for key in ("kind", "limit", "selection_id")}]}

    def validate(self, answer, customer=None):
        return answers.validate_answer(self.w, "passage_review", self.snapshot, customer or self.a, answer)

    def test_complete_customer_projection_not_bounded_index_and_exact_subseconds(self):
        self.rows = [row("event-" + str(i), "Earlier synthetic passage " + str(i), "2026-01-01T00:00:00.000000001Z") for i in range(65)]
        self.rows.append(row("event-latest", "Latest synthetic passage.", "2025-12-31T19:00:00.000000002-05:00"))
        self.rows.append(row("other", "Other customer's later passage.", "2027-01-01T00:00:00Z", customer="party-b"))
        self.build()
        result = self.latest()
        self.assertEqual(result["status"], "latest_unique")
        self.assertEqual([q["text"] for q in result["quotes"]], ["Latest synthetic passage."])
        self.assertTrue(result["latest_time"]["unix_seconds"].endswith(".000000002"))
        self.assertTrue(result["coverage"]["complete"])
        answer = self.validate(self.selection(result))
        self.assertEqual(answer["temporal_selections"], [result])
        self.assertEqual(answer["answer_id"], sha256({k: v for k, v in answer.items() if k != "answer_id"}))

    def test_ties_are_explicit_and_all_tied_quotes_required(self):
        self.rows = [row("event-a", "First tied passage.", "2026-01-01T00:00:00.25Z"),
                     row("event-b", "Second tied passage.", "2025-12-31T19:00:00.250-05:00")]
        self.build()
        result = self.latest(limit=2)
        self.assertEqual(result["status"], "latest_tied")
        self.assertEqual(len(result["quotes"]), 2)
        self.assertIn("are tied", self.validate(self.selection(result))["rendered"])
        selected = self.selection(result)
        selected["quotes"].pop()
        with self.assertRaises(ValueError): self.validate(selected)
        limited = self.latest(limit=1)
        self.assertEqual(limited["status"], "latest_ties_truncated")
        self.assertTrue(limited["coverage"]["truncated"])
        self.assertEqual(limited["quotes"], [])
        selected = self.selection(result)
        selected["latest"] = [{key: limited[key] for key in ("kind", "limit", "selection_id")}]
        with self.assertRaises(ValueError): self.validate(selected)

    def test_unknown_time_blocks_latest_despite_other_known_evidence(self):
        self.rows.append(row("missing-time", "A passage without an event time.", None))
        self.build()
        result = self.latest()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["quotes"], [])

    def assert_concrete_time_pointer(self, source):
        mapping = passage(source=source, encoding="unix_seconds")
        mapping["time"]["path"] = "/*"
        self.rows[0][3] = "[1768305600]"
        metadata = [["meta-event-a", "party-a", "[1768305600]"]] if source == "attachment" else None
        self.build(mapping=mapping, metadata=metadata)
        result = self.latest()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["quotes"], [])

        self.rows[0][3] = '{"*":1768305600}'
        if self.metadata is not None:
            self.metadata[0][2] = '{"*":1768305600}'
        self.refresh()
        result = self.latest()
        self.assertEqual(result["status"], "latest_unique")
        quote = result["quotes"][0]
        self.assertEqual(quote["time"]["unix_seconds"], "1768305600")
        self.assertEqual(quote["time_lineage"]["path"], "/*")
        self.assertEqual(self.read(quote), quote)
        self.assertEqual(self.validate(self.selection(result))["quotes"], [quote])

    def test_field_time_pointer_preserves_literal_star_without_array_expansion(self):
        self.assert_concrete_time_pointer("field")

    def test_attachment_time_pointer_preserves_literal_star_without_array_expansion(self):
        self.assert_concrete_time_pointer("attachment")

    def test_unmapped_kind_binding_blocks_latest(self):
        self.build(unmapped_binding=True)
        result = self.latest()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("passage_mapping_missing", [x["reason"] for x in result["coverage"]["issues"]])

    def test_nested_exact_escaped_leaf_and_item_timestamp(self):
        content = 'A quoted "update" with a backslash \\ and Unicode ☃.\nSecond line.'
        self.rows[0][2] = json.dumps({"parts": [{"body/text~value": "Older passage.", "at": "42.000000001"},
                                               {"body/text~value": content, "at": "42.000000002"}]})
        self.build(mapping=passage(path="/parts/*/body~1text~0value", source="item", encoding="unix_seconds"))
        result = self.latest()
        quote = result["quotes"][0]
        self.assertEqual(quote["text"], content)
        self.assertEqual(quote["json_pointer"], "/parts/1/body~1text~0value")
        self.assertEqual(quote["time"]["unix_seconds"], "42.000000002")
        self.assertEqual(quote["field_sha256"], sha256(self.rows[0][2]))
        self.assertEqual(self.read(quote), quote)
        shorter = self.read(quote, start=2, end=12)
        self.assertEqual(shorter["text"], content[2:12])
        self.assertEqual(shorter["field_sha256"], quote["field_sha256"])
        self.assertTrue(shorter["is_partial_quote"])
        self.assertEqual(self.validate(self.selection(result))["quotes"], [quote])

    def test_empty_arrays_are_no_passages_but_missing_paths_are_unknown(self):
        self.rows[0][2] = '{"parts":[]}'
        self.build(mapping=passage(path="/parts/*/body", source="item", encoding="unix_seconds"))
        self.assertEqual(self.latest()["status"], "no_passages")
        self.rows[0][2] = '{"other":[]}'
        self.refresh()
        self.assertEqual(self.latest()["status"], "unknown")

    def test_quote_locator_tamper_and_cross_customer_scope_refused(self):
        self.rows.append(row("other", "Other synthetic account.", customer="party-b"))
        self.build()
        result = self.latest()
        for field, value in (("json_pointer", "/missing"), ("passage_name", "unreviewed"), ("text", "Fabricated passage.")):
            selected = self.selection(result)
            selected["quotes"][0][field] = value
            with self.assertRaises(ValueError): self.validate(selected)
        selected = self.selection(result)
        selected["quotes"][0]["time"] = {"unix_seconds": "9999999999"}
        with self.assertRaises(ValueError): self.validate(selected)
        with self.assertRaises(ValueError): self.validate(self.selection(result), customer=self.b)
        selected = self.selection(result)
        selected["latest"][0]["limit"] = 4
        with self.assertRaises(ValueError): self.validate(selected)

    def test_changed_date_invalidates_selection_without_rewriting_passage(self):
        self.build()
        old = self.latest()
        self.rows[0][3] = json.dumps({"at": "2026-01-02T00:00:00Z"})
        self.refresh()
        new = self.latest()
        self.assertEqual(old["quotes"][0]["text"], new["quotes"][0]["text"])
        self.assertNotEqual(old["quotes"][0]["quote_id"], new["quotes"][0]["quote_id"])
        self.assertNotEqual(old["selection_id"], new["selection_id"])
        with self.assertRaises(ValueError): self.validate(self.selection(old))

    def test_attachment_time_lineage_and_changed_date(self):
        self.build(mapping=passage(source="attachment"), metadata=[["meta-event-a", "party-a", '{"at":"2026-01-01T00:00:00Z"}']])
        old = self.latest()
        quote = old["quotes"][0]
        self.assertEqual(quote["time_lineage"]["attachment_lineage"]["source_id"], "meta-event-a")
        self.assertRegex(quote["time_lineage"]["attachment_lineage"]["source_row_sha256"], r"^[a-f0-9]{64}$")
        self.metadata[0][2] = '{"at":"2026-01-02T00:00:00Z"}'
        self.refresh()
        new = self.latest()
        self.assertNotEqual(old["selection_id"], new["selection_id"])
        self.assertNotEqual(quote["time_lineage"], new["quotes"][0]["time_lineage"])

    def test_unmatched_attachment_does_not_assert_latest(self):
        self.build(mapping=passage(source="attachment"), metadata=[["unrelated", "party-a", '{"at":"2026-01-01T00:00:00Z"}']])
        self.assertEqual(self.latest()["status"], "unknown")

    def test_candidate_coverage_and_tie_output_bounds_fail_closed(self):
        self.rows = [row("tied-" + str(i), "Same-time synthetic passage " + str(i)) for i in range(70)]
        self.build()
        limited = self.latest(limit=3)
        self.assertEqual(limited["status"], "latest_ties_truncated")
        self.assertEqual(limited["ties_count"], 70)
        self.assertEqual(len(limited["ties"]), 3)
        self.assertEqual(limited["quotes"], [])
        # Exercise the configured internal receipt bound with real published
        # records; lowering a bound can only refuse, never certify a result.
        with patch.object(answers, "MAX_PASSAGE_PROOF_BYTES", 100):
            bounded = self.latest()
        self.assertEqual(bounded["status"], "unknown")
        self.assertFalse(bounded["coverage"]["complete"])
        self.assertEqual(bounded["quotes"], [])
        for item in self.rows:
            item[3] = '{"at":null}'
        self.refresh()
        unknown = self.latest()
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(len(unknown["coverage"]["issues"]), 64)
        self.assertTrue(unknown["coverage"]["issues_truncated"])

    def test_incomplete_nested_candidates_block_other_known_leaf(self):
        self.rows[0][2] = '{"parts":[{"body":"Known passage.","at":42},{"at":43}]}'
        self.build(mapping=passage(path="/parts/*/body", source="item", encoding="unix_seconds"))
        result = self.latest()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["quotes"], [])
        self.assertIn("incomplete_passage_extraction", [issue["reason"] for issue in result["coverage"]["issues"]])

    def test_legacy_raw_quote_shape_unchanged(self):
        self.build()
        latest = self.latest()
        quote = latest["quotes"][0]
        raw = answers.read_evidence(self.w, "passage_review", self.snapshot, self.a, quote["evidence_id"], field="text")
        self.assertEqual(raw["text"], self.rows[0][2])
        self.assertNotIn("passage_name", raw)
        selection = {"metrics": [], "quotes": [{key: raw[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")}]}
        self.assertNotIn("temporal_selections", self.validate(selection))
        with self.assertRaises(ValueError):
            answers.read_evidence(self.w, "passage_review", self.snapshot, self.a, quote["evidence_id"], passage_name="message")


if __name__ == "__main__":
    unittest.main()
