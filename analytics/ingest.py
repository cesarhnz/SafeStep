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
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ingest")
HERE = Path(__file__).resolve().parent

LOG_PATH = Path(os.getenv("SAFESTEP_EVENT_LOG", HERE.parent / "alerts" / "data" / "events.jsonl"))
CHECKPOINT = Path(os.getenv("SAFESTEP_CHECKPOINT", HERE / ".ingest_offset"))
TABLE = os.getenv("SAFESTEP_TABLE", "safestep.lifecycle_events")
BATCH = int(os.getenv("INGEST_BATCH", "50"))  # 23 params per row; keep batches modest
POLL_S = float(os.getenv("INGEST_POLL_S", "5"))

if not re.fullmatch(r"\w+(\.\w+){0,2}", TABLE):
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
        log.warning("skipping unreadable line (%s): %.80r", e, line)
        return None
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
def _load_offset() -> int:
    try:
        return int(CHECKPOINT.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_offset(n: int) -> None:
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(str(n))
    tmp.replace(CHECKPOINT)


def read_new(offset: int) -> list[tuple[bytes, int]]:
    """Complete lines after `offset` as (line, offset_after_line). A line the
    alert service is still writing (no trailing newline yet) is left for next time."""
    if not LOG_PATH.exists():
        return []
    if offset > LOG_PATH.stat().st_size:
        log.warning("event log is smaller than the checkpoint; restarting from 0")
        offset = 0
    with LOG_PATH.open("rb") as f:
        f.seek(offset)
        buf = f.read()
    out, i = [], 0
    while True:
        j = buf.find(b"\n", i)
        if j < 0:
            break
        out.append((buf[i:j], offset + j + 1))
        i = j + 1
    return out


# ---- shipping ----------------------------------------------------------------
def _connect():
    from databricks import sql  # imported lazily so --dry-run needs no install
    return sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def run_once(connect=_connect, dry_run: bool = False) -> int:
    pending = read_new(_load_offset())
    shipped, conn = 0, None
    try:
        for start in range(0, len(pending), BATCH):
            chunk = pending[start:start + BATCH]
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
                _save_offset(chunk[-1][1])  # only after the batch succeeded
    finally:
        if conn is not None:
            conn.close()
    return shipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
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