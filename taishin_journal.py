"""Durable intent identity and pre-submit journal. No broker I/O."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone

from taishin_adapter import MappingError, broker_tag, integer

STATES = {"RESERVED", "SUBMITTING", "WORKING", "PARTIAL_FILLED", "FILLED",
          "CANCEL_PENDING", "CANCELLED", "REJECTED", "STATUS_UNKNOWN", "NOT_SUBMITTED"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED"}

class IntentJournal:
    def __init__(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(target, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS intents (
            intent_id TEXT PRIMARY KEY, broker_tag TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            state TEXT NOT NULL, order_no TEXT NOT NULL DEFAULT '',
            filled_shares INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")

    def close(self):
        with self.lock:
            self.db.close()

    def get(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM intents WHERE intent_id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def reserve(self, order):
        payload = json.dumps(asdict(order), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        identity, tag = order.intent_id, broker_tag(order.intent_id)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.get(identity)
                if old:
                    if old["payload_sha256"] != digest or old["payload"] != payload:
                        raise MappingError("INTENT_REUSED_WITH_DIFFERENT_ORDER")
                    self.db.execute("COMMIT")
                    return dict(created=False, record=old)
                if self.db.execute("SELECT 1 FROM intents WHERE broker_tag=?", (tag,)).fetchone():
                    raise MappingError("BROKER_TAG_COLLISION")
                self.db.execute(
                    "INSERT INTO intents(intent_id,broker_tag,payload,payload_sha256,state,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (identity, tag, payload, digest, "RESERVED", datetime.now(timezone.utc).isoformat()))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return dict(created=True, record=self.get(identity))

    def begin_submission(self, identity):
        """Call and commit BEFORE crossing a broker transport boundary."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.get(identity)
                if not row or row["state"] != "RESERVED":
                    raise MappingError("INTENT_ALREADY_STARTED_OR_UNKNOWN")
                self.db.execute("UPDATE intents SET state='SUBMITTING',updated_at=? WHERE intent_id=?",
                                (datetime.now(timezone.utc).isoformat(), identity))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return self.get(identity)

    def mark_unknown(self, identity):
        return self.record_state(identity, state="STATUS_UNKNOWN")

    def record_state(self, identity, *, state, order_no="", filled_shares=None,
                     account=None, trade_date=None):
        if state not in STATES - {"RESERVED", "SUBMITTING"}:
            raise MappingError("INVALID_JOURNAL_STATE")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.get(identity)
                if row is None:
                    raise MappingError("UNREGISTERED_INTENT")
                order = json.loads(row["payload"])
                if account is not None and account != order["account"]:
                    raise MappingError("JOURNAL_ACCOUNT_MISMATCH")
                if trade_date is not None and trade_date != order["trade_date"]:
                    raise MappingError("JOURNAL_DATE_MISMATCH")
                if state in {"WORKING", "PARTIAL_FILLED", "FILLED", "CANCELLED", "REJECTED"}:
                    if account is None or trade_date is None:
                        raise MappingError("BROKER_IDENTITY_EVIDENCE_REQUIRED")
                filled = row["filled_shares"] if filled_shares is None else integer(
                    filled_shares, "filled_shares")
                if not row["filled_shares"] <= filled <= order["quantity_shares"]:
                    raise MappingError("FILL_REGRESSION_OR_OVERFILL")
                if state == "FILLED" and filled != order["quantity_shares"]:
                    raise MappingError("FULL_FILL_WITHOUT_FULL_QUANTITY")
                if state == "PARTIAL_FILLED" and not 0 < filled < order["quantity_shares"]:
                    raise MappingError("INVALID_PARTIAL_FILL")
                number = order_no or row["order_no"]
                if row["order_no"] and number != row["order_no"]:
                    raise MappingError("BROKER_ORDER_NUMBER_CHANGED")
                if state not in {"STATUS_UNKNOWN", "NOT_SUBMITTED", "REJECTED"} and not number:
                    raise MappingError("BROKER_ORDER_NUMBER_REQUIRED")
                if row["state"] == "RESERVED" and state != "NOT_SUBMITTED":
                    raise MappingError("SUBMISSION_BOUNDARY_NOT_RECORDED")
                if row["state"] != "RESERVED" and state == "NOT_SUBMITTED":
                    raise MappingError("CANNOT_DECLARE_NOT_SUBMITTED_AFTER_BOUNDARY")
                if row["state"] in TERMINAL:
                    if state != row["state"] or filled != row["filled_shares"] or number != row["order_no"]:
                        raise MappingError("TERMINAL_STATE_CONFLICT_RECONCILE_REQUIRED")
                if row["state"] == "PARTIAL_FILLED" and state == "WORKING":
                    raise MappingError("PARTIAL_FILL_CANNOT_REGRESS")
                if state == "REJECTED" and filled:
                    raise MappingError("REJECTION_WITH_CONFIRMED_FILL")
                self.db.execute("UPDATE intents SET state=?,order_no=?,filled_shares=?,updated_at=? "
                                "WHERE intent_id=?", (state, number, filled,
                                datetime.now(timezone.utc).isoformat(), identity))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return self.get(identity)

    def resolve_tag(self, tag):
        with self.lock:
            row = self.db.execute("SELECT intent_id FROM intents WHERE broker_tag=?", (tag,)).fetchone()
            return row["intent_id"] if row else None

    def needs_reconciliation(self):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM intents WHERE state NOT IN ('RESERVED','FILLED','CANCELLED',"
                "'REJECTED','NOT_SUBMITTED') ORDER BY updated_at")]
