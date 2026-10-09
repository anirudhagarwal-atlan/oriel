"""Bounded local-operator discovery before and between explicit reviews.

Source inspection validates committed data under the source locks. Workspace
and pending-review lists are discovery views, never permission, freshness or
approval grants. No function initializes metadata, fetches upstream data,
reads credential values, calls a model or approves a proposal.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json
import math
import uuid

from ..context import _identifier
from ..connectors import service
from ..model import strict_json_loads
from ..workspace import digest
from . import contexts, identity, profiles, snapshots
from .contracts import canonical, name, sha256, validate_profile

MAX_TABLES = 64
MAX_COLUMNS = 128
MAX_CELL_BYTES = 2048
MAX_INSPECTION_BYTES = 256_000
MAX_LIST_BYTES = 2_000_000
MAX_DISCOVERY_ITEMS = 100
_PENDING_SCOPE = "stored_pending_proposals_not_approval_authority"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _bounded(value, maximum=MAX_LIST_BYTES):
    if len(_json(value).encode()) > maximum:
        raise ValueError("Onboarding result exceeds its bounded size; select a smaller source or review scope.")
    return value


def _parse(value):
    if not isinstance(value, str) or len(value.encode()) > 4_000_000:
        raise ValueError("Stored onboarding metadata exceeds its supported size.")
    result = strict_json_loads(value)
    if not isinstance(result, dict):
        raise ValueError("Stored onboarding metadata must be an object.")
    return result


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog=current_database() "
                        "AND table_schema='_contextbase' AND table_name=?", [table]).fetchone() is not None


def _display(value):
    """Exact display values; their original SQL type is retained in columns."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite source values cannot be inspected.")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite source values cannot be inspected.")
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_display(item) for item in value]
    if isinstance(value, dict):
        if len({str(key) for key in value}) != len(value):
            raise ValueError("Source map keys cannot be represented unambiguously.")
        return {str(key): _display(item) for key, item in value.items()}
    raise ValueError("The source value has no supported exact display representation.")


def _samples(conn, table, count):
    columns = table["columns"]
    if len(columns) > MAX_COLUMNS:
        raise ValueError("Inspect at most 128 columns per source table.")
    if not count:
        return []
    selections = []
    for column in columns:
        field = _identifier(column["name"])
        if column["type"] in {"TIMESTAMP_NS", "INTERVAL"}:
            field = "CAST(" + field + " AS VARCHAR)"
        # Do not transfer an oversized field to Python just to omit it. The
        # database applies the cell guard before returning selected values.
        small = f"({field} IS NULL OR octet_length(encode(to_json({field}))) <= {MAX_CELL_BYTES})"
        selections.extend([f"CASE WHEN {small} THEN {field} ELSE NULL END", f"NOT {small}"])
    schema, physical = table["table"].split(".", 1)
    rows = conn.execute(f"SELECT {','.join(selections)} FROM {_identifier(schema)}.{_identifier(physical)} LIMIT ?", [count]).fetchall()
    samples = []
    for row in rows:
        values, omitted = {}, []
        for index, column in enumerate(columns):
            value, too_large = row[index * 2:index * 2 + 2]
            if not too_large:
                value = _display(value)
            if too_large or len(_json(value).encode()) > MAX_CELL_BYTES:
                omitted.append({"field": column["name"], "reason": "cell_size_limit"})
            else:
                values[column["name"]] = value
        samples.append({"values": values, "omitted_fields": omitted})
    return samples


def inspect_sources(workspace, snapshot_id, *, sample_rows=3):
    """Inspect bounded committed schema/values without requiring a profile."""
    if type(sample_rows) is not int or not 0 <= sample_rows <= 5:
        raise ValueError("Inspect zero to five sample rows per source table.")
    manifest = snapshots.load_snapshot(workspace, snapshot_id)
    with snapshots._locks(workspace, manifest["coverage"]["connections"]), workspace._connect() as conn:
        conn.begin()
        try:
            manifest = snapshots._validate_current(workspace, conn, snapshots._load(conn, snapshot_id))
            if sum(len(source["tables"]) for source in manifest["sources"]) > MAX_TABLES:
                raise ValueError("Inspect at most 64 selected source tables per snapshot.")
            sources = []
            for source in manifest["sources"]:
                tables = []
                for table in source["tables"]:
                    rows = _samples(conn, table, sample_rows)
                    tables.append({**table, "samples": rows, "sampled_rows": len(rows),
                                   "samples_truncated": len(rows) < table["rows"]})
                    _bounded({"sources": sources, "next_tables": tables}, MAX_INSPECTION_BYTES)
                sources.append({"name": source["name"], "source_type": source["source_type"],
                                "receipt_sha256": source["receipt_sha256"], "tables": tables})
            value = {"spec_version": "oriel.customer360.source-inspection/v1", "scope": "committed_source_inspection",
                     "snapshot_id": snapshot_id, "manifest_sha256": manifest["manifest_sha256"],
                     "source_observed_at": {s["name"]: s["receipt"]["finished_at"] for s in manifest["sources"]},
                     "sources": sources, "upstream_completeness": manifest["coverage"]["upstream_completeness"],
                     "sampling": "bounded_first_rows_not_a_representativeness_claim",
                     "bounds": {"sample_rows_per_table": sample_rows, "max_tables": MAX_TABLES,
                                "max_columns_per_table": MAX_COLUMNS, "max_cell_bytes": MAX_CELL_BYTES,
                                "max_total_bytes": MAX_INSPECTION_BYTES}}
            _bounded(value, MAX_INSPECTION_BYTES)
            conn.commit()
            return value
        except Exception:
            conn.rollback()
            raise


def _head(conn):
    return identity.identity_head(conn) if _exists(conn, "customer360_identity_state") else None


def _approved_profile(conn, approval, profile_name=None):
    if not _exists(conn, "customer360_profile_approvals"):
        raise ValueError("Stored proposal has no retained approved source mapping.")
    rows = conn.execute("SELECT name,version,proposal_id,payload FROM _contextbase.customer360_profile_approvals "
                        "WHERE json_extract_string(payload,'$.approval_sha256')=? LIMIT 2", [approval]).fetchall()
    if len(rows) != 1:
        raise ValueError("Stored source mapping approval is missing or ambiguous.")
    stored_name, version, proposal_id, raw = rows[0]
    value = _parse(raw)
    candidate = value["candidate"]
    if (value.get("approval_sha256") != approval or sha256({k: v for k, v in value.items() if k != "approval_sha256"}) != approval
            or sha256(candidate) != proposal_id or value.get("proposal_id") != proposal_id
            or candidate["profile"]["name"] != stored_name or candidate["version"] != version
            or (profile_name is not None and stored_name != profile_name)):
        raise ValueError("Stored source mapping approval failed its consistency checks.")
    validate_profile(candidate["profile"])
    return value


def _candidate(conn, kind, row, head):
    identifier, raw = row[:2]
    value = _parse(raw)
    if kind == "links":
        proof, status = row[2:]
        identity._uuid(identifier)
        if (value.get("spec") != "contextbase.customer360-identity-proposal/v1"
                or value.get("proposal_id") != identifier or digest(value) != proof
                or value.get("status") != status or status not in {"proposed", "conflict"}
                or head is None or value.get("workspace_id") != head["workspace_id"]):
            raise ValueError("Stored identity proposal failed its consistency checks.")
        links = identity._links(value["links"])
        for link in links:
            if not conn.execute("SELECT 1 FROM _contextbase.customer360_customers WHERE id=?", [link["customer_id"]]).fetchone():
                raise ValueError("Pending link references a customer outside this workspace.")
        expected = [[i, link["connection"], link["entity"], link["source_id"], link["customer_id"]] for i, link in enumerate(links)]
        actual = [list(r) for r in conn.execute("SELECT ordinal,connection,entity,source_id,customer_id FROM "
                                               "_contextbase.customer360_link_candidates WHERE proposal_id=? ORDER BY ordinal", [identifier]).fetchall()]
        if actual != expected:
            raise ValueError("Pending identity proposal and candidate index disagree.")
        return {**value, "approval_sha256": proof}
    if sha256(value) != identifier or head is None or value["identity"]["workspace_id"] != head["workspace_id"]:
        raise ValueError("Stored pending proposal failed its consistency checks.")
    snapshots._load(conn, value["snapshot_id"])
    if kind == "profile":
        if value.get("spec_version") != "oriel.customer360.mapping-proposal/v1":
            raise ValueError("Unknown source mapping proposal format.")
        validate_profile(value["profile"])
    else:
        if value.get("spec_version") != contexts.SPEC or sha256(value["definitions"]) != value["definition_sha256"]:
            raise ValueError("Stored context proposal failed its consistency checks.")
        approved = _approved_profile(conn, value["profile_approval"], value["profile"])
        contexts.validate_definitions(value["definitions"], approved)
        contexts._gaps(value["gaps"])
    return {"proposal_id": identifier, "approval_sha256": identifier, "status": "candidate", **value}


def pending_proposals(workspace, kind="links", *, limit=20):
    """Return exact unapproved proposals, including stale/conflicting candidates.

    Pending does not mean approvable: each explicit approval API rechecks its
    own current source/identity/context prerequisites atomically.
    """
    if kind not in {"links", "profile", "context"} or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Select links, profile or context and a limit between one and 100.")
    result = {"kind": kind, "scope": _PENDING_SCOPE, "proposals": [], "truncated": False}
    if not workspace.db_path.exists():
        return result
    queries = {
        "links": ("customer360_link_proposals", "SELECT p.proposal_id,p.payload,p.approval_sha256,p.status FROM _contextbase.customer360_link_proposals p "
                  "WHERE NOT EXISTS (SELECT 1 FROM _contextbase.customer360_link_reviews r WHERE r.proposal_id=p.proposal_id)"),
        "profile": ("customer360_profile_candidates", "SELECT p.proposal_id,p.payload FROM _contextbase.customer360_profile_candidates p "
                    "WHERE NOT EXISTS (SELECT 1 FROM _contextbase.customer360_profile_approvals r WHERE r.proposal_id=p.proposal_id)"),
        "context": ("customer360_context_candidates", "SELECT p.proposal_id,p.payload FROM _contextbase.customer360_context_candidates p "
                    "WHERE NOT EXISTS (SELECT 1 FROM _contextbase.customer360_context_approvals r WHERE json_extract_string(r.payload,'$.proposal_id')=p.proposal_id)"),
    }
    with workspace._connect() as conn:
        conn.begin()
        table, query = queries[kind]
        if not _exists(conn, table):
            return result
        rows = conn.execute(query + " ORDER BY json_extract_string(p.payload,'$.created_at') DESC,p.proposal_id DESC LIMIT ?", [limit + 1]).fetchall()
        head = _head(conn)
        result["truncated"] = len(rows) > limit
        for row in rows[:limit]:
            try:
                candidate = _candidate(conn, kind, row, head)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("Pending proposal integrity check failed; inspect the local review history.") from exc
            trial = {**result, "proposals": [*result["proposals"], candidate]}
            if len(_json(trial).encode()) > MAX_LIST_BYTES:
                if not result["proposals"]:
                    raise ValueError("Pending proposal exceeds the bounded display size; prepare a smaller proposal.")
                result["truncated"] = True
                break
            result["proposals"].append(candidate)
    return result


def _source_statuses(workspace):
    values = service.configured(workspace)["sources"]
    if len(values) > MAX_DISCOVERY_ITEMS:
        raise ValueError("Workspace source discovery exceeds 100 configured sources.")
    result = []
    for value in values:
        last, committed = value.get("last_sync"), value.get("last_snapshot")
        result.append({"name": value["name"], "source_type": value["source_type"],
                       "status": last.get("status") if isinstance(last, dict) else "configured",
                       "configuration_changed_since_sync": value["configuration_changed_since_sync"],
                       "last_attempt": {key: last.get(key) for key in ("attempt", "status", "started_at", "finished_at")} if isinstance(last, dict) else None,
                       "last_publication": {key: committed.get(key) for key in ("attempt", "finished_at", "snapshot_sha256", "tables")} if isinstance(committed, dict) else None})
    return result


def workspace_state(workspace):
    """Bounded read-only discovery; currentness is checked by action APIs later."""
    result = {"spec_version": "oriel.customer360.workspace-state/v1", "exists": workspace.db_path.is_file(),
              "workspace_id": None, "customers": [], "profiles": [], "sources": _source_statuses(workspace),
              "truncated": {"customers": False, "profiles": False},
              "authority": "discovery_only_revalidate_before_action"}
    if not result["exists"]:
        return _bounded(result)
    with workspace._connect() as conn:
        conn.begin()
        head = _head(conn)
        result["workspace_id"] = head["workspace_id"] if head else None
        if _exists(conn, "customer360_customers"):
            rows = conn.execute("SELECT id,label FROM _contextbase.customer360_customers ORDER BY label,id LIMIT ?", [MAX_DISCOVERY_ITEMS + 1]).fetchall()
            result["truncated"]["customers"] = len(rows) > MAX_DISCOVERY_ITEMS
            result["customers"] = [{"id": identifier, "label": label} for identifier, label in rows[:MAX_DISCOVERY_ITEMS]]
        if _exists(conn, "customer360_profile_approvals"):
            rows = conn.execute("SELECT name FROM _contextbase.customer360_profile_approvals GROUP BY name ORDER BY name LIMIT ?", [MAX_DISCOVERY_ITEMS + 1]).fetchall()
            result["truncated"]["profiles"] = len(rows) > MAX_DISCOVERY_ITEMS
            for (profile_name,) in rows[:MAX_DISCOVERY_ITEMS]:
                name(profile_name)
                approved = profiles._latest(conn, profile_name)
                approved = _approved_profile(conn, approved["approval_sha256"], profile_name)
                item = {"name": profile_name, "approval_sha256": approved["approval_sha256"],
                        "version": approved["candidate"]["version"],
                        "capabilities": sorted({b["kind"] for b in approved["candidate"]["profile"]["bindings"]}),
                        "materialization": None, "context": None}
                if _exists(conn, "customer360_projection_heads"):
                    found = conn.execute("SELECT materialization_id FROM _contextbase.customer360_projection_heads WHERE profile=?", [profile_name]).fetchone()
                    if found:
                        stored = conn.execute("SELECT payload FROM _contextbase.customer360_materializations WHERE materialization_id=?", [found[0]]).fetchone()
                        if stored is None:
                            raise ValueError("Discovered profile is missing its materialization receipt.")
                        receipt = _parse(stored[0])
                        profiles._check_receipt_integrity(receipt)
                        if receipt["materialization_id"] != found[0] or receipt["profile"] != profile_name:
                            raise ValueError("Discovered profile and materialization receipt disagree.")
                        item["materialization"] = {key: receipt[key] for key in ("materialization_id", "snapshot_id", "profile_approval", "created_at", "row_count")}
                        item["materialization"]["matches_current_profile"] = receipt["profile_approval"] == approved["approval_sha256"]
                        item["materialization"]["matches_current_identity"] = bool(head and receipt["identity"] == head)
                current = contexts._latest(conn, profile_name)
                if current:
                    if current["profile"] != profile_name or sha256(current["definitions"]) != current["definition_sha256"]:
                        raise ValueError("Discovered business context failed its consistency checks.")
                    item["context"] = {key: current[key] for key in ("context_id", "definition_sha256", "version", "profile_approval", "snapshot_id")}
                    item["context"]["matches_current_profile"] = current["profile_approval"] == approved["approval_sha256"]
                result["profiles"].append(item)
    return _bounded(result)
