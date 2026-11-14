#!/usr/bin/env python3
"""Ship the alert service's lifecycle log to Databricks.

    alerts/data/events.jsonl --(this script)--> safestep.lifecycle_events (Delta)

This is the ONLY link between the alert system and analytics. The alert
service never imports Databricks, so a Databricks outage cannot delay an
alert, and this process can be stopped, restarted or moved to another
machine without touching the alerts side.

Safe to re-run: a byte-offset checkpoint skips rows already shipped, and the
MERGE on row_id ignores any row that did land twice (e.g. a crash between the
insert and the checkpoint write).

    python ingest.py             # keep running, ship new rows every few seconds
    python ingest.py --once      # ship what is there, then exit
    python ingest.py --dry-run   # print the SQL, touch nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import math
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ingest")
HERE = Path(__file__).resolve().parent

LOG_PATH = Path(os.getenv("SAFESTEP_EVENT_LOG", HERE.parent / "alerts" / "data" / "events.jsonl"))
CHECKPOINT = Path(os.getenv("SAFESTEP_CHECKPOINT", HERE / ".ingest_offset"))
TABLE = os.getenv("SAFESTEP_TABLE", "safestep.lifecycle_events")
BATCH = int(os.getenv("INGEST_BATCH", "10"))
POLL_S = float(os.getenv("INGEST_POLL_S", "5"))

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){1,2}", TABLE):
    raise SystemExit(f"bad SAFESTEP_TABLE: {TABLE!r}")

COLUMNS = [
    ("row_id", "STRING"), ("logged_at", "TIMESTAMP"), ("lifecycle", "STRING"),
    ("incident_id", "STRING"), ("source_event_id", "STRING"), ("device_id", "STRING"),
    ("event_type", "STRING"), ("status", "STRING"), ("room", "STRING"), ("zone", "STRING"),
    ("camera_id", "STRING"), ("confidence", "DOUBLE"), ("person", "STRING"),
    ("details", "STRING"), ("detected_at", "TIMESTAMP"), ("priority", "INT"),
    ("escalations", "INT"), ("acked_by", "STRING"), ("person_detected", "BOOLEAN"),
    ("immobility_seconds", "DOUBLE"), ("actor", "STRING"), ("outcome", "STRING"),
    ("raw", "STRING"),
]


# Native SQL parameters are limited to 255 per statement.
MAX_BATCH = 255 // len(COLUMNS)
if not 1 <= BATCH <= MAX_BATCH:
    raise SystemExit(f"INGEST_BATCH must be between 1 and {MAX_BATCH}")
if not math.isfinite(POLL_S) or POLL_S <= 0:
    raise SystemExit("INGEST_POLL_S must be a positive finite number")


# ---- parsing -----------------------------------------------------------------
def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _num(v, cast):
    try:
        return None if v is None else cast(v)
    except (TypeError, ValueError):
        return None


def _str(v):
    return None if v is None else str(v)


def row_to_record(line: bytes) -> dict | None:
    try:
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise ValueError("Unreadable event log row; repair the source before retrying") from e
    return {
        "row_id": hashlib.sha1(line).hexdigest(),
        "logged_at": _ts(row.get("logged_at")),
        "lifecycle": _str(row.get("lifecycle")),
        "incident_id": _str(row.get("incident_id")),
        "source_event_id": _str(row.get("source_event_id")),
        "device_id": _str(row.get("device_id")),
        "event_type": _str(row.get("type")),
        "status": _str(row.get("status")),
        "room": _str(row.get("room")),
        "zone": _str(row.get("zone")),
        "camera_id": _str(row.get("camera_id")),
        "confidence": _num(row.get("confidence"), float),
        "person": _str(row.get("person")),
        "details": _str(row.get("details")),
        "detected_at": _ts(row.get("detected_at")),
        "priority": _num(row.get("priority"), int),
        "escalations": _num(row.get("escalations"), int),
        "acked_by": _str(row.get("acked_by")),
        "person_detected": row.get("person_detected") if isinstance(row.get("person_detected"), bool) else None,
        "immobility_seconds": _num(row.get("immobility_seconds"), float),
        "actor": _str(row.get("by")),
        "outcome": _str(row.get("outcome")),
        "raw": json.dumps(row, separators=(",", ":")),
    }


def build_merge(records: list[dict]) -> tuple[str, dict]:
    records = list({r["row_id"]: r for r in records}.values())
    if not records or len(records) > MAX_BATCH:
        raise ValueError(f"MERGE needs 1 to {MAX_BATCH} unique records")
    selects, params = [], {}
    for i, rec in enumerate(records):
        cols = []
        for name, typ in COLUMNS:
            cols.append(f"CAST(:{name}{i} AS {typ}) AS {name}")
            params[f"{name}{i}"] = rec[name]
        selects.append("SELECT " + ", ".join(cols))
    sql_text = (f"MERGE INTO {TABLE} t USING ({' UNION ALL '.join(selects)}) s "
                f"ON t.row_id = s.row_id WHEN NOT MATCHED THEN INSERT *")
    return sql_text, params


# ---- reading + checkpoint ------------------------------------------------------
def _identity() -> dict:
    stat = LOG_PATH.stat()
    return {"path": str(LOG_PATH.resolve()), "device": stat.st_dev, "inode": stat.st_ino,
            "table": TABLE}


def _load_offset() -> int:
    try:
        checkpoint = json.loads(CHECKPOINT.read_text())
    except FileNotFoundError:
        return 0
    except ValueError:
        log.warning("Unreadable checkpoint; replaying log (MERGE is idempotent)")
        return 0
    # Legacy integer checkpoints cannot identify the source or destination.
    if not isinstance(checkpoint, dict):
        return 0
    identity = _identity()
    if any(checkpoint.get(k) != v for k, v in identity.items()):
        log.warning("Log identity or destination changed; replaying from zero")
        return 0
    offset = checkpoint.get("offset", 0)
    if not isinstance(offset, int) or offset < 0 or offset > LOG_PATH.stat().st_size:
        return 0
    return offset


def _save_offset(n: int, identity: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps({**identity, "offset": n}))
    tmp.replace(CHECKPOINT)


def read_new(offset: int):
    """Stream complete lines, retaining a partially written line for the next poll."""
    with LOG_PATH.open("rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                break
            yield line[:-1], f.tell()


# ---- shipping ----------------------------------------------------------------
def _connect():
    from databricks import sql  # imported lazily so --dry-run needs no install
    host = os.getenv("DATABRICKS_SERVER_HOSTNAME") or os.getenv("DATABRICKS_HOST", "")
    host = urlparse(host).netloc if "://" in host else host
    if not host or not os.getenv("DATABRICKS_HTTP_PATH") or not os.getenv("DATABRICKS_TOKEN"):
        raise ValueError("Set DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN")
    return sql.connect(
        server_hostname=host,
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def run_once(connect=_connect, dry_run: bool = False) -> int:
    if not LOG_PATH.exists():
        log.info("Waiting for event log: %s", LOG_PATH)
        return 0
    identity = _identity()
    pending = iter(read_new(_load_offset()))
    shipped, conn = 0, None
    try:
        from itertools import islice
        while True:
            chunk = list(islice(pending, BATCH))
            if not chunk:
                break
            records = [r for r in (row_to_record(line) for line, _ in chunk if line.strip()) if r]
            if records:
                sql_text, params = build_merge(records)
                if dry_run:
                    print(sql_text[:400] + " ...\n", f"({len(records)} rows, {len(params)} params)\n")
                else:
                    conn = conn or connect()
                    with conn.cursor() as cur:
                        cur.execute(sql_text, params)
                shipped += len(records)
            if not dry_run:
                _save_offset(chunk[-1][1], identity)  # only after the batch succeeded
    finally:
        if conn is not None:
            conn.close()
    return shipped


def initialize(connect=_connect, dry_run=False):
    """Create the destination schema, Delta table and incident view."""
    namespace = TABLE.rsplit(".", 1)[0]
    source = (HERE / "sql" / "schema.sql").read_text()
    source = source.replace("safestep.lifecycle_events", TABLE)
    source = source.replace("safestep.incidents", namespace + ".incidents")
    source = source.replace("CREATE SCHEMA IF NOT EXISTS safestep", "CREATE SCHEMA IF NOT EXISTS " + namespace)
    source = "\n".join(line.split("--", 1)[0] for line in source.splitlines())
    statements = [stmt.strip() for stmt in source.split(";") if stmt.strip()]
    if dry_run:
        print(";\n\n".join(statements) + ";")
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="create schema, table and incident view, then exit")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.init:
        initialize(dry_run=args.dry_run)
        return
    while True:
        try:
            n = run_once(dry_run=args.dry_run)
            if n:
                log.info("shipped %d row(s) to %s", n, TABLE)
        except Exception as e:  # Databricks down, token expired...: retry, nothing is lost
            log.warning("ship failed, will retry: %s", e)
            if args.once:
                raise
        if args.once or args.dry_run:
            break
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()