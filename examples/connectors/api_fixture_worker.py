"""Fixture-only CLI/worker launcher; never use with real connector credentials.

This example redirects exactly two packaged source endpoints in the extraction
child. The production CLI, worker, dlt adapters, pipeline and publisher remain
unchanged. It adds no production endpoint-override option. Its Python network
guard is a test constraint, not an operating-system sandbox.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit


SYNTHETIC_TOKEN = "contextbase-synthetic-fixture-only"


def fixture_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or value != f"http://127.0.0.1:{parsed.port}"):
        raise ValueError("The fixture origin must be an explicit 127.0.0.1 HTTP port.")
    return parsed.port


def allowed_address(address, port):
    return (isinstance(address, tuple) and len(address) == 2
            and address[0] == "127.0.0.1" and type(address[1]) is int
            and address[1] == port)


def check_http(url, method, headers, origin):
    fixture_origin(origin)
    parsed = urlsplit(url)
    if (f"{parsed.scheme}://{parsed.netloc}" != origin or parsed.fragment
            or (method, parsed.path) not in {("GET", "/rest/contacts"), ("POST", "/graphql")}
            or headers.get("Authorization") != ("Bearer " if parsed.path.startswith("/rest/") else "") + SYNTHETIC_TOKEN):
        raise ValueError("Only the declared synthetic fixture requests are allowed.")


def install_network_guard(origin):
    """Install before importing dlt; reject DNS and sockets outside one port."""
    port = fixture_origin(origin)
    counts = {"allowed_connects": 0, "blocked_network_attempts": 0}

    def audit(event, args):
        if event == "socket.getaddrinfo":
            allowed = allowed_address((args[0], args[1]), port)
        elif event == "socket.connect":
            allowed = args[0].family == socket.AF_INET and allowed_address(args[1], port)
            if allowed:
                counts["allowed_connects"] += 1
        elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
            allowed = False
        else:
            return
        if not allowed:
            counts["blocked_network_attempts"] += 1
            raise RuntimeError("The fixture worker refused an undeclared network destination.")

    sys.addaudithook(audit)
    return counts


def install_worker_override(origin):
    from contextbase.connectors import worker
    original_runtime = worker._runtime

    def runtime(root):
        dlt, duckdb, get_source = original_runtime(root)
        from contextbase.connectors.upstream import sources, yaml_source
        import requests

        load = yaml_source.load_yaml_config
        lookup = sources.get_source_entry
        send = requests.sessions.Session.send
        request = requests.sessions.Session.request

        def load_fixture(name):
            config = copy.deepcopy(load(name))
            if name == "intercom":
                config["client"]["base_url"] = origin + "/rest/"
            return config

        def lookup_fixture(name):
            entry = copy.deepcopy(lookup(name))
            if name == "linear":
                entry.graphql_config["endpoint"] = origin + "/graphql"
            return entry

        def guarded_request(session, *args, **kwargs):
            # Prevent ambient proxies and requests' ~/.netrc authentication.
            session.trust_env = False
            return request(session, *args, **kwargs)

        def guarded_send(session, prepared, **kwargs):
            check_http(prepared.url, prepared.method, prepared.headers, origin)
            kwargs["proxies"] = {}
            return send(session, prepared, **kwargs)

        yaml_source.load_yaml_config = load_fixture
        sources.load_yaml_config = load_fixture
        sources.get_source_entry = lookup_fixture
        requests.sessions.Session.request = guarded_request
        requests.sessions.Session.send = guarded_send

        def restricted_source(source_type, credentials, resource_names=None, params=None):
            expected = {"intercom": ("token", ["contacts"]), "linear": ("api_key", ["issues"])}
            if source_type not in expected:
                raise ValueError("Only the two declared fixture connectors are supported.")
            field, resources = expected[source_type]
            if credentials != {field: SYNTHETIC_TOKEN} or resource_names != resources or params:
                raise ValueError("The fixture worker requires its exact synthetic configuration.")
            return get_source(source_type, credentials, resource_names=resource_names, params=params)

        return dlt, duckdb, restricted_source

    worker._runtime = runtime
    return worker


def _private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-origin", required=True)
    parser.add_argument("--fixture-worker", action="store_true")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    fixture_origin(args.fixture_origin)
    counts = install_network_guard(args.fixture_origin)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if args.fixture_worker:
        request_parser = argparse.ArgumentParser()
        request_parser.add_argument("--request", required=True)
        request_parser.add_argument("--result", required=True)
        paths = request_parser.parse_args(arguments)
        try:
            return install_worker_override(args.fixture_origin).main(arguments)
        finally:
            _private_json(str(paths.result) + ".network.json", {
                "spec": "contextbase.synthetic-api-network-guard/v1",
                "fixture_origin": args.fixture_origin, **counts,
                "constraint": "Python audit and requests guards; not an OS sandbox",
            })

    from contextbase.connectors import service
    from contextbase import cli
    original_run = subprocess.run

    def launch(command, *positional, **kwargs):
        prefix = [sys.executable, "-m", "contextbase.connectors.worker"]
        if not isinstance(command, list) or command[:3] != prefix:
            raise RuntimeError("The fixture launcher only supports the production extraction worker.")
        command = [sys.executable, str(Path(__file__).resolve()), "--fixture-origin", args.fixture_origin,
                   "--fixture-worker", "--", *command[3:]]
        return original_run(command, *positional, **kwargs)

    service.subprocess.run = launch
    sys.argv = ["contextbase", *arguments]
    cli.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
