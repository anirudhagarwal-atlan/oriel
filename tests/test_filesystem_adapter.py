"""Controlled file/client contracts only; no dlt runtime or external services."""
import base64
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb

from contextbase.connectors import filesystem_adapter as adapter


class FileItem(dict):
    def __init__(self, path, raw, *, prefix="s3://example-bucket/data/", **fields):
        super().__init__(file_url=prefix + path, relative_path=path,
                         size_in_bytes=len(raw), modification_date="2026-10-09T00:00:00Z", **fields)
        self.raw, self.opens = raw, []

    def open(self, **kwargs):
        self.opens.append(kwargs)
        return io.BytesIO(self.raw)


class Resource:
    def __init__(self, data, name, write_disposition):
        self.data, self.name, self.write_disposition = data, name, write_disposition
        self.selected = True

    def __iter__(self):
        return iter(self.data)


class FakeDlt:
    def __init__(self):
        self.resources = []

    def resource(self, data, **kwargs):
        result = Resource(data, **kwargs)
        self.resources.append(result)
        return result

    def source(self, **kwargs):
        def decorate(factory):
            def build():
                # A controlled naming contract, not execution of the dlt wheel.
                aliases = {"orderID": "order_id", "ContextbaseFileUrl": "contextbase_file_url",
                           "a+b": "axb", "a*b": "axb"}
                normalize = lambda name: aliases.get(name, re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))
                schema = SimpleNamespace(naming=SimpleNamespace(normalize_identifier=normalize))
                return SimpleNamespace(name=kwargs["name"], resources={row.name: row for row in factory()}, schema=schema)
            return build
        return decorate


class FilesystemAdapterTests(unittest.TestCase):
    def setUp(self):
        self.credentials = {"bucket_url": "s3://example-bucket/data/",
                            "aws_access_key_id": "synthetic-access", "aws_secret_access_key": "synthetic-secret"}
        self.params = {"tables": {"orders": {"file_glob": "*.csv", "format": "csv"}}}
        self.client_calls, self.list_calls, self.closed = [], [], []
        self.dlt = FakeDlt()

    def build(self, files, *, params=None, resources=None, credentials=None, source_type="s3"):
        def client(kind, values, options):
            self.client_calls.append((kind, values, options))
            return SimpleNamespace(close=lambda: self.closed.append(kind))

        def listing(**kwargs):
            self.list_calls.append(kwargs)
            return files(kwargs) if callable(files) else files

        return adapter.build_filesystem_source(source_type, credentials or self.credentials, resources,
                                               params if params is not None else self.params,
                                               filesystem_factory=listing, dlt_module=self.dlt,
                                               filesystem_client_factory=client)

    def rows(self, source, name="orders"):
        return [row for batch in source.resources[name] for row in batch]

    def test_csv_contents_multiple_files_chunks_and_exact_provenance(self):
        first = FileItem("a.csv", b"order_id,amount\nsynthetic_1,10\nsynthetic_2,20\n")
        second = FileItem("b.csv", b"order_id,amount\nsynthetic_3,30\n")
        params = {**self.params, "chunk_size": 2, "region_name": "eu-west-1"}
        source = self.build([[first], [second]], params=params)
        self.assertEqual(self.client_calls, [])
        self.assertEqual(self.list_calls, [])
        chunks = list(source.resources["orders"])
        self.assertEqual([len(batch) for batch in chunks], [2, 1])
        rows = [row for batch in chunks for row in batch]
        self.assertEqual([row["amount"] for row in rows], ["10", "20", "30"])
        self.assertEqual([row["contextbase_file_row_number"] for row in rows], [1, 2, 1])
        self.assertEqual(rows[0]["contextbase_file_sha256"], hashlib.sha256(first.raw).hexdigest())
        self.assertEqual(rows[-1]["contextbase_file_url"], second["file_url"])
        self.assertEqual(first.opens, [{"mode": "rb", "compression": "disable"}])
        self.assertEqual(self.closed, ["s3"])
        self.assertIs(self.list_calls[0]["extract_content"], False)
        self.assertEqual(self.list_calls[0]["kwargs"], {})
        self.assertEqual(source.resources["orders"].write_disposition, "replace")

    def test_selecting_one_table_never_lists_other_table_and_reads_jsonl_values(self):
        params = {"tables": {"events": {"file_glob": "events/**/*.jsonl", "format": "jsonl"},
                             "unused": {"file_glob": "private/*.csv", "format": "csv"}}}
        item = FileItem("events/day/a.jsonl", b'{"id":1,"ok":true,"payload":{"count":2}}\n\n{"id":2,"ok":false}\n')
        source = self.build([item], params=params, resources=["events"])
        self.assertEqual(set(source.resources), {"events"})
        rows = self.rows(source, "events")
        self.assertIs(rows[0]["ok"], True)
        self.assertEqual(rows[0]["payload"], {"count": 2})
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([call["file_glob"] for call in self.list_calls], ["events/**/*.jsonl"])

    def test_parquet_contents_use_real_duckdb_over_only_downloaded_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.parquet"
            with duckdb.connect() as connection:
                connection.execute("COPY (SELECT * FROM (VALUES (1, 2.5), (2, 4.25)) AS t(id, amount)) TO ? (FORMAT PARQUET)", [str(path)])
            item = FileItem("values.parquet", path.read_bytes())
        source = self.build([item], params={"tables": {"values": {"file_glob": "*.parquet", "format": "parquet"}}, "chunk_size": 1})
        rows = self.rows(source, "values")
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([float(row["amount"]) for row in rows], [2.5, 4.25])
        self.assertEqual(rows[0]["contextbase_file_sha256"], hashlib.sha256(item.raw).hexdigest())

    def test_missing_ambient_credentials_fail_before_import_or_client(self):
        for values in ({}, {"bucket_url": self.credentials["bucket_url"]}, {**self.credentials, "profile": "default"}):
            with self.subTest(values=list(values)), patch.object(adapter.importlib, "import_module", side_effect=AssertionError("No imports")):
                with self.assertRaises(ValueError):
                    adapter.build_filesystem_source("s3", values, params=self.params)

    def test_invalid_config_and_selection_fail_before_any_factory(self):
        bad = [None, {}, {"file_glob": "*.csv"}, {"tables": {}},
               {"tables": {"orders": {"file_glob": "../*.csv", "format": "csv"}}},
               {"tables": {"orders": {"file_glob": "*.csv", "format": "xml"}}},
               {"tables": {"Orders": {"file_glob": "*.csv", "format": "csv"}}},
               {**self.params, "client_kwargs": {"verify": False}}, {**self.params, "max_files": True},
               {**self.params, "max_file_bytes": 129 * 1024 * 1024}, {**self.params, "chunk_size": 0}]
        for params in bad:
            with self.subTest(params=params), patch.object(adapter.importlib, "import_module", side_effect=AssertionError("No imports")):
                with self.assertRaises(ValueError):
                    adapter.build_filesystem_source("s3", self.credentials, params=params)
        for selected in ([], ["missing"], ["orders", "orders"], "orders"):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.build([], resources=selected)
        self.assertEqual(self.client_calls, [])
        self.assertEqual(self.list_calls, [])

    def test_url_credentials_traversal_wrong_protocol_and_port_are_rejected(self):
        urls = ["s3://synthetic-user:synthetic-password@example-bucket/data/", "s3://example-bucket/data/?token=synthetic",
                "s3://example-bucket/%2e%2e/data/", "https://example.invalid/data/", "s3://example-bucket:9000/data/"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.build([], credentials={**self.credentials, "bucket_url": url})
        self.assertEqual(self.client_calls, [])

    def test_listing_cannot_read_unselected_or_external_files(self):
        items = [FileItem("a.csv", b"id\n1\n", prefix="s3://other-bucket/data/"),
                 FileItem("nested/a.csv", b"id\n1\n"), FileItem("a.jsonl", b'{"id":1}\n')]
        for item in items:
            with self.subTest(file=item["file_url"]), self.assertRaises(ValueError):
                self.rows(self.build([item]))
            self.assertEqual(item.opens, [])

    def test_duplicate_missing_empty_and_changed_files_refuse_completion(self):
        item = FileItem("a.csv", b"id\n1\n")
        for items in ([item, item], [], [FileItem("empty.csv", b"id\n")]):
            with self.subTest(files=len(items)), self.assertRaises(ValueError):
                self.rows(self.build(items))
        changed = FileItem("changed.csv", b"id\n1\n")
        changed["size_in_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "changed"):
            self.rows(self.build([changed]))

    def test_size_and_shared_file_count_caps_include_all_selected_tables(self):
        item = FileItem("a.csv", b"id\n1\n")
        with self.assertRaisesRegex(ValueError, "max_file_bytes"):
            self.rows(self.build([item], params={**self.params, "max_file_bytes": 2}))
        self.assertEqual(item.opens, [])
        item.pop("size_in_bytes")
        with self.assertRaisesRegex(ValueError, "max_file_bytes"):
            self.rows(self.build([item], params={**self.params, "max_file_bytes": 2}))
        params = {"tables": {"one": {"file_glob": "*.csv", "format": "csv"},
                             "two": {"file_glob": "*.csv", "format": "csv"}}, "max_files": 1}
        source = self.build([FileItem("a.csv", b"id\n1\n")], params=params)
        self.assertEqual(len(self.rows(source, "one")), 1)
        with self.assertRaisesRegex(ValueError, "max_files"):
            self.rows(source, "two")

    def test_gzip_is_bounded_and_hashes_original_compressed_content(self):
        item = FileItem("a.csv.gz", gzip.compress(b"id\n1\n"), encoding="gzip")
        params = {"tables": {"orders": {"file_glob": "*.csv.gz", "format": "csv"}}}
        rows = self.rows(self.build([item], params=params))
        self.assertEqual(rows[0]["id"], "1")
        self.assertEqual(rows[0]["contextbase_file_sha256"], hashlib.sha256(item.raw).hexdigest())
        bomb = FileItem("bomb.csv.gz", gzip.compress(b"id\n" + b"1\n" * 10000), encoding="gzip")
        with self.assertRaisesRegex(ValueError, "Decompressed"):
            self.rows(self.build([bomb], params={**params, "max_file_bytes": 1000}))

    def test_malformed_rows_and_provenance_collisions_refuse_data_loss(self):
        csv_cases = [b"id,id\n1,2\n", b"id,ID\n1,2\n", b"id,amount\n1\n", b"contextbase_file_url\nsynthetic\n",
                     b"Contextbase File Url\nsynthetic\n", b"id\n1,2\n", b"orderID,order_id\n1,2\n",
                     b"ContextbaseFileUrl\nsynthetic\n", b"a+b,a*b\n1,2\n"]
        for raw in csv_cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.rows(self.build([FileItem("a.csv", raw)]))
        json_cases = [b'{"id":1,"id":2}\n', b'{"id":NaN}\n', b'[1,2]\n', b'{}\n', b'{"id":Infinity}\n',
                      b'{"nested":{"orderID":1,"order_id":2}}\n']
        params = {"tables": {"orders": {"file_glob": "*.jsonl", "format": "jsonl"}}}
        for raw in json_cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.rows(self.build([FileItem("a.jsonl", raw)], params=params))

    def test_explicit_gcs_json_and_catalog_metadata_no_file_fallback(self):
        token = {"type": "service_account", "project_id": "synthetic-project", "private_key": "synthetic-key",
                 "client_email": "synthetic@example.invalid", "token_uri": "https://oauth2.googleapis.com/token"}
        credentials = {"bucket_url": "gs://example-bucket/data/", "credentials": json.dumps(token)}
        source = self.build([FileItem("a.csv", b"id\n1\n", prefix=credentials["bucket_url"])], source_type="gcs", credentials=credentials)
        self.rows(source)
        self.assertEqual(self.client_calls[0][1]["credentials"], token)
        self.assertEqual(json.loads(credentials["credentials"]), token)
        for value in ("/synthetic/service-account.json", "default", "null", json.dumps({**token, "token_uri": "https://example.invalid/token"})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build([], source_type="gcs", credentials={**credentials, "credentials": value})
        field = next(row for row in adapter.FILESYSTEM_CREDENTIAL_SPECS["gcs"] if row["name"] == "credentials")
        self.assertIs(field["secret"], True)
        self.assertEqual(field["env_var"], "GCS_SERVICE_ACCOUNT_JSON")

    def test_native_client_options_never_select_ambient_authentication(self):
        calls = []
        fsspec = SimpleNamespace(filesystem=lambda *args, **kwargs: calls.append((args, kwargs)) or object())
        options = adapter.validate_filesystem_parameters(self.params, source_type="s3")
        with patch.object(adapter.importlib, "import_module", return_value=fsspec):
            adapter._filesystem_client("s3", self.credentials, options)
            adapter._filesystem_client("gcs", {"credentials": {"project_id": "synthetic-project"}}, options)
            adapter._filesystem_client("azure", {"azure_storage_account_name": "syntheticaccount", "azure_storage_account_key": "synthetic-key"}, options)
        self.assertEqual(calls[0][1]["key"], "synthetic-access")
        self.assertEqual(calls[0][1]["secret"], "synthetic-secret")
        self.assertFalse(calls[0][1]["anon"])
        self.assertTrue(calls[0][1]["use_ssl"])
        self.assertEqual(calls[0][1]["client_kwargs"], {"region_name": "us-east-1"})
        self.assertEqual(calls[1][1]["token"], {"project_id": "synthetic-project"})
        self.assertEqual(calls[1][1]["access"], "read_only")
        self.assertFalse(calls[2][1]["anon"])
        for _, kwargs in calls:
            self.assertTrue(kwargs["skip_instance_cache"])
            self.assertFalse(kwargs["use_listings_cache"])

    def test_sftp_pin_checked_before_auth_and_no_agent_or_key_discovery(self):
        key = b"synthetic-public-host-key"
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        calls = []

        class Client:
            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, host, **kwargs):
                self.policy.missing_host_key(self, host, SimpleNamespace(asbytes=lambda: key))
                calls.append((host, kwargs))

            def open_sftp(self):
                return SimpleNamespace(close=lambda: None)

            def close(self):
                pass

        class Base:
            def __init__(self, host, skip_instance_cache, **kwargs):
                self.host, self.ssh_kwargs = host, kwargs
                self._connect()

        fake_paramiko = SimpleNamespace(MissingHostKeyPolicy=object, SSHClient=Client)
        fake_fsspec = SimpleNamespace(get_filesystem_class=lambda name: Base)
        values = {"bucket_url": "sftp://example.invalid:2222/data/", "sftp_username": "synthetic-user", "sftp_password": "synthetic-password"}
        params = {**self.params, "sftp_host_key_sha256": fingerprint}
        options = adapter.validate_filesystem_parameters(params, source_type="sftp")
        with patch.object(adapter.importlib, "import_module", side_effect=lambda name: {"fsspec": fake_fsspec, "paramiko": fake_paramiko}[name]):
            client = adapter._filesystem_client("sftp", values, options)
            client.close()
            with self.assertRaisesRegex(ValueError, "host key"):
                adapter._filesystem_client("sftp", values, {**options, "sftp_host_key_sha256": "SHA256:" + "A" * 43})
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1]["allow_agent"])
        self.assertFalse(calls[0][1]["look_for_keys"])
        self.assertEqual(calls[0][1]["port"], 2222)
        with self.assertRaisesRegex(ValueError, "trust decision"):
            self.build([], source_type="sftp", credentials=values)

    def test_sftp_listing_port_omission_matches_bound_client_and_retains_endpoint(self):
        values = {"bucket_url": "sftp://example.invalid:2222/data/", "sftp_username": "synthetic-user",
                  "sftp_password": "synthetic-password"}
        params = {**self.params, "sftp_host_key_sha256": "SHA256:" + "A" * 43}
        # Pinned dlt 1.24's _make_sftp_url emits hostname + path, without port.
        item = FileItem("a.csv", b"id\n1\n", prefix="sftp://example.invalid/data/")
        source = self.build([item], source_type="sftp", credentials=values, params=params)
        row = self.rows(source)[0]
        self.assertEqual(row["contextbase_file_url"], "sftp://example.invalid/data/a.csv")
        self.assertEqual(row["contextbase_source_bucket_url"], values["bucket_url"])
        for prefix in ("sftp://example.invalid:22/data/", "sftp://other.invalid/data/"):
            wrong = FileItem("a.csv", b"id\n1\n", prefix=prefix)
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "outside"):
                self.rows(self.build([wrong], source_type="sftp", credentials=values, params=params))
            self.assertEqual(wrong.opens, [])

    def test_literal_spaces_in_selected_object_paths_are_materialized(self):
        credentials = {**self.credentials, "bucket_url": "s3://example-bucket/my folder/"}
        params = {"tables": {"orders": {"file_glob": "my file.csv", "format": "csv"}}}
        item = FileItem("my file.csv", b"id\n1\n", prefix=credentials["bucket_url"])
        row = self.rows(self.build([item], credentials=credentials, params=params))[0]
        self.assertEqual(row["contextbase_file_url"], "s3://example-bucket/my folder/my file.csv")
        with self.assertRaises(ValueError):
            self.build([], credentials={**credentials, "bucket_url": "s3://example bucket/my folder/"})

    def test_actual_source_naming_contract_is_required_before_client_creation(self):
        class MissingNaming(FakeDlt):
            def source(self, **kwargs):
                return lambda factory: lambda: SimpleNamespace(resources={row.name: row for row in factory()})

        self.dlt = MissingNaming()
        with self.assertRaisesRegex(ValueError, "naming normalizer"):
            self.build([])
        self.assertEqual(self.client_calls, [])


if __name__ == "__main__":
    unittest.main()
