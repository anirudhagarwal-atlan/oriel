"""Local, retained customer briefs and explicit current-versus-saved comparisons.

Saving computes the brief itself inside one validated source transaction; callers
cannot submit an answer to archive as trusted evidence. Historical reads never
pretend to be current answers. Hashes check local consistency, not authorship or
protection from a workspace owner rewriting every receipt and table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

from . import answers, snapshots
from .contracts import canonical, name, sha256, text
from .identity import identity_head
from .profiles import _check_receipt_integrity

MAX_REVIEW_BYTES = 5_000_000
SPEC = "oriel.customer360.saved-review/v1"


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS _contextbase.customer360_saved_reviews "
                 "(review_id VARCHAR PRIMARY KEY,profile VARCHAR,customer_id VARCHAR,created_at VARCHAR,payload VARCHAR)")


def _exists(conn):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog=current_database() "
                             "AND table_schema='_contextbase' AND table_name='customer360_saved_reviews'").fetchone())


def save_review(workspace, profile_name, snapshot_id, customer_id, *, fields=("text",), quote_limit=5):
    """Save an independently computed brief and its complete scoped record index."""
    fields, quote_limit = answers._fields(fields), answers._limit(quote_limit)
    with answers._scope(workspace, profile_name, snapshot_id, customer_id) as scope:
        brief = answers._brief(scope, fields, quote_limit)
        value = {"spec_version": SPEC, **answers._basis(scope),
                 "created_at": datetime.now(timezone.utc).isoformat(),
                 "status": "retained_review_not_current_answer", "brief": brief,
                 "records": scope["payload"]["records"],
                 "options": {"fields": fields, "quote_limit": quote_limit},
                 "retention_scope": "private_local_operator_workspace"}
        if len(canonical(value).encode()) > MAX_REVIEW_BYTES:
            raise ValueError("Customer review exceeds the five-megabyte retention limit; reduce selected source scope.")
        value["review_id"] = sha256(value)
        _ensure(scope["conn"])
        scope["conn"].execute("INSERT INTO _contextbase.customer360_saved_reviews VALUES (?,?,?,?,?)",
                               [value["review_id"], profile_name, customer_id, value["created_at"], canonical(value)])
    return value


def _load(conn, review_id):
    answers._hash(review_id)
    if not _exists(conn):
        raise ValueError("No saved customer review in this workspace.")
    row = conn.execute("SELECT payload,profile,customer_id,created_at FROM _contextbase.customer360_saved_reviews WHERE review_id=?", [review_id]).fetchone()
    if row is None:
        raise ValueError("No saved customer review in this workspace.")
    try:
        value = json.loads(row[0])
        if (value["review_id"] != review_id or value["spec_version"] != SPEC
                or sha256({k: v for k, v in value.items() if k != "review_id"}) != review_id):
            raise ValueError()
        if (value["profile"], value["customer_id"], value["created_at"]) != row[1:]:
            raise ValueError()
        head = identity_head(conn)
        if value["workspace_id"] != head["workspace_id"]:
            raise ValueError()
        if not conn.execute("SELECT 1 FROM _contextbase.customer360_customers WHERE id=?", [value["customer_id"]]).fetchone():
            raise ValueError()
        brief = value["brief"]
        if sha256({k: v for k, v in brief.items() if k != "answer_id"}) != brief["answer_id"]:
            raise ValueError()
        for key in ("workspace_id", "profile", "customer_id", "snapshot_id", "snapshot_manifest_sha256",
                    "profile_approval", "materialization_id", "materialization_receipt_sha256",
                    "context_id", "definition_sha256", "identity_sha256"):
            if value[key] != brief[key]:
                raise ValueError()
        saved = conn.execute("SELECT payload FROM _contextbase.customer360_materializations WHERE materialization_id=?",
                             [value["materialization_id"]]).fetchone()
        if saved is None:
            raise ValueError()
        receipt = json.loads(saved[0])
        _check_receipt_integrity(receipt)
        if (receipt["receipt_sha256"] != value["materialization_receipt_sha256"]
                or receipt["snapshot_id"] != value["snapshot_id"]
                or receipt["profile_approval"] != value["profile_approval"]
                or receipt["profile"] != value["profile"]
                or receipt["identity"]["workspace_id"] != value["workspace_id"]
                or receipt["identity"]["sha256"] != value["identity_sha256"]):
            raise ValueError()
        history = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM _contextbase.customer360_record_history WHERE materialization_id=? ORDER BY record_key",
            [value["materialization_id"]]).fetchall()]
        if sha256(history) != receipt["records_sha256"]:
            raise ValueError()
        keys = ("record_key", "evidence_id", "kind", "connection", "entity", "source_id", "fields")
        records = [{key: record[key] for key in (*keys, *(("attachments",) if "attachments" in record else ()))} for record in history
                   if record["customer_id"] == value["customer_id"]]
        if canonical(records) != canonical(value["records"]):
            raise ValueError()
        # Verify immutable approvals and the historical manifest, without asking
        # historical source publications to pass today's freshness gate.
        manifest = snapshots._load(conn, value["snapshot_id"])
        if manifest["manifest_sha256"] != value["snapshot_manifest_sha256"]:
            raise ValueError()
        profile_rows = conn.execute("SELECT payload FROM _contextbase.customer360_profile_approvals WHERE name=?",
                                    [value["profile"]]).fetchall()
        approved = next((item for item in (json.loads(r[0]) for r in profile_rows)
                         if item.get("approval_sha256") == value["profile_approval"]), None)
        if (approved is None
                or sha256({k: v for k, v in approved.items() if k != "approval_sha256"}) != value["profile_approval"]):
            raise ValueError()
        context_row = conn.execute("SELECT payload FROM _contextbase.customer360_context_approvals WHERE context_id=?",
                                   [value["context_id"]]).fetchone()
        context = json.loads(context_row[0]) if context_row else None
        if (context is None or context.get("context_id") != value["context_id"]
                or sha256({k: v for k, v in context.items() if k != "context_id"}) != value["context_id"]
                or context["definition_sha256"] != value["definition_sha256"]
                or sha256(context["definitions"]) != value["definition_sha256"]
                or context["profile_approval"] != value["profile_approval"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Saved customer review or retained evidence failed its consistency checks.") from None
    return value


def load_review(workspace, review_id):
    """Read retained evidence even after refresh/failure; never bypass current gates."""
    with workspace._connect() as conn:
        conn.begin()
        return _load(conn, review_id)


def list_reviews(workspace, profile_name, customer_id, *, limit=20):
    name(profile_name)
    text(customer_id, "customer ID", maximum=128)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("List between 1 and 100 saved reviews.")
    with workspace._connect() as conn:
        conn.begin()
        if not _exists(conn):
            return []
        ids = [r[0] for r in conn.execute("SELECT review_id FROM _contextbase.customer360_saved_reviews "
                                         "WHERE profile=? AND customer_id=? ORDER BY created_at DESC,review_id DESC LIMIT ?",
                                         [profile_name, customer_id, limit]).fetchall()]
        results = []
        for review_id in ids:
            value = _load(conn, review_id)
            if value["profile"] != profile_name or value["customer_id"] != customer_id:
                raise ValueError("Saved review index disagrees with its evidence scope.")
            results.append({key: value[key] for key in ("review_id", "profile", "customer_id", "created_at",
                                                       "snapshot_id", "context_id", "status")})
        return results


def _metric_changes(before, after, comparable):
    def index(brief):
        values = {}
        for metric in brief["metrics"]:
            result = metric["result"]
            for row in result["values"]:
                values[(result["definition_name"], result["unit"], row["currency"])] = str(row["value"])
        return values
    old, new = index(before), index(after)
    changes = []
    for key in sorted(set(old) | set(new), key=canonical):
        left, right = old.get(key), new.get(key)
        if left == right:
            continue
        delta = None
        if comparable and left is not None and right is not None:
            # Source sums are exact decimals; never round their difference via float.
            with localcontext() as context:
                context.prec = max(100, len(left) + len(right) + 10)
                delta = str(Decimal(right) - Decimal(left))
        changes.append({"definition_name": key[0], "unit": key[1], "currency": key[2],
                        "before": left, "after": right, "delta": delta})
    return changes


def compare_review(workspace, review_id, current_snapshot_id):
    """Compare a saved brief to newly validated current facts for the same scope.

Missing result groups are not zero. Record aliases are additions/removals, not
guessed continuity. Mapping, identity or definition changes suppress arithmetic
deltas rather than attributing them solely to changed source facts.
"""
    saved = load_review(workspace, review_id)
    with answers._scope(workspace, saved["profile"], current_snapshot_id, saved["customer_id"]) as scope:
        # Recheck inside the transaction that supplies current evidence.
        saved = _load(scope["conn"], review_id)
        current = answers._brief(scope, saved["options"]["fields"], saved["options"]["quote_limit"])
        changed_basis = [key for key in ("definition_sha256", "profile_approval", "identity_sha256")
                         if saved[key] != current[key]]
        previous = {r["record_key"]: r for r in saved["records"]}
        now = {r["record_key"]: r for r in scope["payload"]["records"]}
        record_changes = {"added": [], "removed": [], "changed": []}
        for key in sorted(set(previous) | set(now)):
            before, after = previous.get(key), now.get(key)
            if before is None:
                record_changes["added"].append(after)
            elif after is None:
                record_changes["removed"].append(before)
            elif before["evidence_id"] != after["evidence_id"]:
                record_changes["changed"].append({"record_key": key, "before": before, "after": after})
        value = {"spec_version": "oriel.customer360.review-comparison/v1", **answers._basis(scope),
                 "saved_review_id": review_id, "previous_snapshot_id": saved["snapshot_id"],
                 "previous_context_id": saved["context_id"], "current_brief": current,
                 "status": "review_basis_changed" if changed_basis else "same_reviewed_basis",
                 "changed_basis": changed_basis,
                 "metric_changes": _metric_changes(saved["brief"], current, not changed_basis),
                 "record_changes": record_changes,
                 "scope": "observed_local_records_not_a_claim_of_upstream_event_completeness",
                 "record_identity_policy": "exact_source_record_keys; aliases_are_added_and_removed"}
        return {**value, "comparison_id": sha256(value)}
