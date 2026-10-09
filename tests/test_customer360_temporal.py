"""Pure source-time primitives, with no connector, workspace or model calls."""
from decimal import Decimal, localcontext
import json
import unittest

from contextbase.customer360 import temporal


class PointerTests(unittest.TestCase):
    def test_rfc6901_escaped_empty_numeric_and_literal_star_keys(self):
        value = {"a/b": {"~key": [None, {"": "exact", "*": "literal"}]}}
        self.assertEqual(temporal.validate_pointer("/a~1b/~0key/1/"), "/a~1b/~0key/1/")
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/1/"), (True, "exact"))
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/1/*"), (True, "literal"))
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/0"), (True, None))
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/2"), (False, None))
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/01"), (False, None))
        self.assertEqual(temporal.resolve_pointer(value, "/a~1b/~0key/-"), (False, None))
        self.assertEqual(temporal.resolve_pointer(value, ""), (True, value))
        self.assertEqual(temporal.resolve_pointer({"01": "object-key"}, "/01"), (True, "object-key"))
        # Traversal of ordinary decoded JSON does not reject an unrelated
        # finite float; timestamp interpretation itself still refuses floats.
        self.assertEqual(temporal.resolve_pointer({"duration": 1.5, "time": "exact"}, "/time"), (True, "exact"))

    def test_invalid_pointer_contracts_are_rejected(self):
        for path in (None, True, 1, [], {}, "a/b", "#/a", "/~", "/~2", "/a~01/~3",
                     "/" + "x" * 1024, "/" + "é" * 600, "/\ud800", "/x" * 17):
            with self.subTest(path=repr(path)), self.assertRaisesRegex(ValueError, "invalid_temporal_pointer"):
                temporal.validate_pointer(path)
        with self.assertRaises(ValueError):
            temporal.validate_pointer("/x", wildcard=1)

    def test_nested_array_wildcards_return_concrete_paths_and_exact_parents(self):
        source = {"groups": [{"items": [{"body": "one", "created": 1}, {"body": "two", "created": 2}]},
                             {"items": [{"body": "three", "created": 3}]}]}
        result = temporal.select_leaves(json.dumps(source), "/groups/*/items/*/body")
        self.assertTrue(result["complete"])
        self.assertIsNone(result["reason"])
        self.assertEqual([row["path"] for row in result["matches"]],
                         ["/groups/0/items/0/body", "/groups/0/items/1/body", "/groups/1/items/0/body"])
        self.assertEqual([row["value"] for row in result["matches"]], ["one", "two", "three"])
        self.assertEqual([row["parent"]["created"] for row in result["matches"]], [1, 2, 3])
        for row in result["matches"]:
            self.assertEqual(temporal.resolve_pointer(source, row["path"]), (True, row["value"]))

    def test_empty_is_complete_but_missing_or_wrong_type_is_incomplete(self):
        cases = [('{"items":[]}', True, None), ('{}', False, "missing_path"),
                 ('{"items":null}', False, "path_type_mismatch"),
                 ('{"items":{"*":{"body":"not an array"}}}', False, "path_type_mismatch")]
        for raw, complete, reason in cases:
            with self.subTest(raw=raw):
                result = temporal.select_leaves(raw, "/items/*/body")
                self.assertEqual(result, {"matches": [], "complete": complete, "reason": reason})
        result = temporal.select_leaves('{"items":[{"body":"known"},{}]}', "/items/*/body")
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "missing_path")
        self.assertEqual([row["value"] for row in result["matches"]], ["known"])

    def test_selected_containers_are_not_silently_treated_as_passages(self):
        result = temporal.select_leaves('{"items":[{"body":"one"}]}', "/items/*")
        self.assertEqual(result, {"matches": [], "complete": False, "reason": "non_scalar_leaf"})
        self.assertEqual(temporal.resolve_pointer({"items": [{"body": "one"}]}, "/items/0"),
                         (True, {"body": "one"}))

    def test_empty_path_selects_raw_text_without_json_interpretation(self):
        for raw in ("", "plain passage", "{invalid JSON", '[{"body":"one"}]'):
            with self.subTest(raw=raw):
                self.assertEqual(temporal.select_leaves(raw, ""),
                                 {"matches": [{"path": "", "value": raw, "parent": None}],
                                  "complete": True, "reason": None})

    def test_escaped_paths_null_and_fractional_numbers_preserve_values(self):
        raw = '{"a/b":{"~key":null,"fraction":1700000000.123456789}}'
        null = temporal.select_leaves(raw, "/a~1b/~0key")
        self.assertTrue(null["complete"])
        self.assertIsNone(null["matches"][0]["value"])
        result = temporal.select_leaves(raw, "/a~1b/fraction")
        self.assertEqual(result["matches"][0]["value"], Decimal("1700000000.123456789"))
        self.assertEqual(temporal.parse_timestamp(result["matches"][0]["value"], "unix_seconds")["unix_seconds"],
                         "1700000000.123456789")

    def test_malformed_duplicate_nonfinite_and_nontext_inputs_fail_closed(self):
        for raw in ("{", "null trailing", '{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}',
                    '{"x":-Infinity}', "\ufeff{}", '{"x":01}', '{"x":1e999999999999999999999}'):
            with self.subTest(raw=raw):
                result = temporal.select_leaves(raw, "/x")
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"], "invalid_json")
                self.assertEqual(result["matches"], [])
        for raw in (None, True, 1, {}, "\ud800"):
            self.assertEqual(temporal.select_leaves(raw, "/x")["reason"], "invalid_text")

    def test_depth_nodes_input_and_repeated_parent_output_are_bounded(self):
        self.assertEqual(temporal.select_leaves("[" * 18 + "0" + "]" * 18, "/0")["reason"], "depth_limit")
        self.assertEqual(temporal.select_leaves('{"x":[' + ','.join('0' for _ in range(10_000)) + ']}', "/x/*")["reason"], "node_limit")
        self.assertEqual(temporal.select_leaves("a" * 64_001, "")["reason"], "input_limit")
        self.assertEqual(temporal.select_leaves("😀" * 16_001, "")["reason"], "input_limit")
        result = temporal.select_leaves(json.dumps({"values": list(range(1500))}), "/values/*")
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "output_limit")
        self.assertGreater(len(result["matches"]), 0)
        self.assertLess(len(result["matches"]), 1500)
        self.assertLessEqual(len(temporal._encoded(result)), temporal.MAX_OUTPUT_BYTES)
        # Oversized or invalid decoded documents are never traversed as known.
        self.assertEqual(temporal.resolve_pointer({"x": "a" * 64_000}, "/x"), (False, None))
        self.assertEqual(temporal.resolve_pointer({"x": float("nan")}, "/x"), (False, None))
        cyclic = []
        cyclic.append(cyclic)
        self.assertEqual(temporal.resolve_pointer(cyclic, "/0"), (False, None))

    def test_brackets_inside_text_do_not_change_json_depth(self):
        raw = json.dumps({"x": "[" * 100 + '\\"' + "]" * 100})
        result = temporal.select_leaves(raw, "/x")
        self.assertTrue(result["complete"])


class TimestampTests(unittest.TestCase):
    def assertKnown(self, value, encoding, seconds, utc):
        self.assertEqual(temporal.parse_timestamp(value, encoding),
                         {"status": "known", "unix_seconds": seconds, "utc": utc, "reason": None})

    def test_rfc3339_offsets_and_nine_digits_are_exact(self):
        self.assertKnown("1970-01-01T01:00:00.123456789+01:00", "rfc3339", "0.123456789",
                         "1970-01-01T00:00:00.123456789Z")
        self.assertKnown("1969-12-31T23:30:00-00:30", "rfc3339", "0", "1970-01-01T00:00:00Z")
        self.assertKnown("1969-12-31T23:59:59.999999999Z", "rfc3339", "-0.000000001",
                         "1969-12-31T23:59:59.999999999Z")
        self.assertKnown("2000-02-29T00:00:00.120000000Z", "rfc3339", "951782400.12",
                         "2000-02-29T00:00:00.12Z")

    def test_unix_integer_decimal_and_string_never_use_binary_float(self):
        for value in (0, Decimal("0"), "0", Decimal("-0E+999999")):
            self.assertKnown(value, "unix_seconds", "0", "1970-01-01T00:00:00Z")
        self.assertKnown("-0.1", "unix_seconds", "-0.1", "1969-12-31T23:59:59.9Z")
        self.assertKnown(Decimal("1700000000.123456789"), "unix_seconds", "1700000000.123456789",
                         "2023-11-14T22:13:20.123456789Z")
        self.assertKnown(Decimal("1E+2"), "unix_seconds", "100", "1970-01-01T00:01:40Z")
        with localcontext() as context:
            context.prec = 3
            self.assertKnown(Decimal("253402300799.999999999"), "unix_seconds", "253402300799.999999999",
                             "9999-12-31T23:59:59.999999999Z")

    def test_missing_wrong_types_and_unsupported_encodings(self):
        for encoding in ("rfc3339", "unix_seconds"):
            self.assertEqual(temporal.parse_timestamp(None, encoding)["reason"], "missing")
            for value in (True, False, 1.0, float("nan"), {}, []):
                with self.subTest(value=repr(value), encoding=encoding):
                    result = temporal.parse_timestamp(value, encoding)
                    self.assertEqual(result["status"], "unknown")
                    self.assertIsNone(result["unix_seconds"])
                    self.assertIsNone(result["utc"])
        for encoding in ("milliseconds", "ISO8601", None, [], 1):
            with self.assertRaisesRegex(ValueError, "unsupported_timestamp_encoding"):
                temporal.parse_timestamp(0, encoding)

    def test_invalid_dates_unknown_timezone_and_leap_seconds(self):
        for value in ("2025-02-29T12:00:00Z", "2024-13-01T00:00:00Z", "2024-01-32T00:00:00Z",
                      "2024-01-01T24:00:00Z", "2024-01-01T00:60:00Z", "2016-12-31T23:59:60Z",
                      "2024-01-01T00:00:00", "2024-01-01", "2024-01-01 00:00:00Z",
                      "2024-01-01T00:00:00+24:00", "2024-01-01T00:00:00+01:60",
                      "2024-01-01T00:00:00.1234567890Z", "2024-01-01T00:00:00+0000",
                      "2024-01-01t00:00:00z", " 2024-01-01T00:00:00Z", "0000-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(temporal.parse_timestamp(value, "rfc3339")["status"], "unknown")
        self.assertEqual(temporal.parse_timestamp("2024-01-01T00:00:00-00:00", "rfc3339")["reason"], "unknown_offset")

    def test_large_epoch_boundaries_and_subsecond_precision(self):
        self.assertKnown(-62135596800, "unix_seconds", "-62135596800", "0001-01-01T00:00:00Z")
        self.assertKnown("253402300799.999999999", "unix_seconds", "253402300799.999999999",
                         "9999-12-31T23:59:59.999999999Z")
        for value in (10 ** 1000, -62135596801, "-62135596800.000000001", 253402300800,
                      Decimal("1E+99999"), Decimal("-1E+99999")):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(temporal.parse_timestamp(value, "unix_seconds")["reason"], "out_of_range")
        for value in ("0.0000000001", "1.0000000000", "NaN", "Infinity", "1e3", "+1", "01", " 1", "1 "):
            self.assertEqual(temporal.parse_timestamp(value, "unix_seconds")["status"], "unknown")
        self.assertEqual(temporal.parse_timestamp(Decimal("0.0000000001"), "unix_seconds")["reason"], "unsupported_precision")
        for value in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")):
            self.assertEqual(temporal.parse_timestamp(value, "unix_seconds")["status"], "unknown")
        self.assertEqual(temporal.parse_timestamp("0001-01-01T00:00:00+01:00", "rfc3339")["reason"], "out_of_range")
        self.assertEqual(temporal.parse_timestamp("9999-12-31T23:59:59-01:00", "rfc3339")["reason"], "out_of_range")


if __name__ == "__main__":
    unittest.main()
