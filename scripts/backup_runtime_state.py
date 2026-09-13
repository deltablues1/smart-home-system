#!/usr/bin/env python3
"""Snapshot the runtime state before a deploy that changes how it is stored.

Rolling back the code is not a rollback. This round changes the Firestore
session layout — events moved out of the session document into their own — so
a deploy that goes wrong leaves data the previous version cannot read. The
`data` field it looks for is gone.

So take a copy first. This writes:

    <out>/adk_sessions/<session_id>.json    every session, both layouts
    <out>/data/                             the local job and budget ledgers
    <out>/MANIFEST.json                     git SHA, branch, host, counts

The manifest matters as much as the data: restoring a session snapshot next to
the wrong revision is how you get a second incident while cleaning up the
first.

Run it ON the machine you are about to deploy to, with that machine's .env:

    python scripts/backup_runtime_state.py
    python scripts/backup_runtime_state.py --out /home/pi/backups/pre-deploy

Restoring is deliberately not automated. Read the manifest, decide what you
actually want back, and put it back knowingly.
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COLLECTION = "adk_sessions"
EVENTS = "events"


def git(*args) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "(unavailable)"


async def dump_sessions(out_dir: Path) -> dict:
    """Every ADK session, whichever layout it is stored in."""
    from google.cloud.firestore_v1.async_client import AsyncClient

    sessions_dir = out_dir / COLLECTION
    sessions_dir.mkdir(parents=True, exist_ok=True)

    db = AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
    counts = {"sessions": 0, "legacy": 0, "current": 0, "events": 0}
    try:
        async for snapshot in db.collection(COLLECTION).stream():
            data = snapshot.to_dict() or {}
            record = {"session_id": snapshot.id, "document": _plain(data)}

            if "data" in data:
                counts["legacy"] += 1
            else:
                counts["current"] += 1
                events = []
                async for event_snap in (
                    db.collection(COLLECTION).document(snapshot.id)
                    .collection(EVENTS).stream()
                ):
                    events.append({
                        "id": event_snap.id,
                        "document": _plain(event_snap.to_dict() or {}),
                    })
                events.sort(key=lambda row: row["id"])
                record["events"] = events
                counts["events"] += len(events)

            path = sessions_dir / f"{_safe(snapshot.id)}.json"
            path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            counts["sessions"] += 1
    finally:
        db.close()
    return counts


def _plain(value):
    """Firestore hands back datetimes and refs; JSON does not take them."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def copy_local_state(out_dir: Path) -> list:
    """The local ledgers: background jobs, scheduler outcomes, budget."""
    source = ROOT / "data"
    if not source.exists():
        return []
    target = out_dir / "data"
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in sorted(source.glob("*.json")):
        shutil.copy2(path, target / path.name)
        copied.append(path.name)
    return copied


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        help="where to write (default: backups/pre-deploy-<timestamp>)",
    )
    parser.add_argument(
        "--skip-firestore", action="store_true",
        help="local files only, do not read Firestore",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else ROOT / "backups" / f"pre-deploy-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Backup -> {out_dir}")

    counts = {"sessions": 0, "legacy": 0, "current": 0, "events": 0}
    firestore_error = None
    if args.skip_firestore:
        print("  Firestore: preskočeno (--skip-firestore)")
    else:
        try:
            counts = await dump_sessions(out_dir)
            print(
                f"  Firestore: {counts['sessions']} sesija "
                f"({counts['legacy']} stari format, {counts['current']} novi), "
                f"{counts['events']} događaja"
            )
        except Exception as e:
            firestore_error = str(e)
            print(f"  Firestore: GREŠKA — {e}", file=sys.stderr)

    local = copy_local_state(out_dir)
    print(f"  data/: {len(local)} datoteka {local if local else ''}")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "python": platform.python_version(),
        "project": os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        "git": {
            "sha": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
        },
        "env": {
            key: os.getenv(key, "")
            for key in (
                "USE_PERSISTENT_ADK_SESSIONS",
                "USE_CLOUD_LOGGING",
                "USE_PLAN_EXECUTE",
                "DEPLOYMENT_PROFILE",
            )
        },
        "firestore": {"counts": counts, "error": firestore_error},
        "local_files": local,
        "note": (
            "Session layout changed in this round: events moved out of the "
            "session document into an events subcollection. A code-only "
            "rollback cannot read sessions written by the new version."
        ),
    }
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"  MANIFEST.json: {manifest['git']['branch']} @ {manifest['git']['sha'][:8]}"
          + (" (dirty)" if manifest["git"]["dirty"] else ""))
    print("\nGotovo.")
    return 2 if firestore_error else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
