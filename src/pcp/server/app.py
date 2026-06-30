"""PCP server — version endpoint, intervention collector, health check."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="PCP Server", version="1.0.0", docs_url="/docs")

DATA_DIR = Path(os.getenv("PCP_DATA_DIR", "/data/pcp"))
SKILL_VERSION = "1.0.0"
SKILL_RAW_URL = "https://raw.githubusercontent.com/ganeshnallasivam-cell/program-context-protocol/main/SKILL.md"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/version")
def version():
    return {
        "version": SKILL_VERSION,
        "skill_url": SKILL_RAW_URL,
        "changelog_url": "https://github.com/ganeshnallasivam-cell/program-context-protocol/blob/main/CHANGELOG.md",
    }


@app.post("/interventions")
async def record_intervention(request: Request):
    """Receive intervention metadata from a pcp project. Append to JSONL store."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    required = {"type", "project", "logged_at"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"missing fields: {missing}")

    # Strip any non-metadata fields — never store code, secrets, file content
    safe = {
        "type": payload.get("type"),
        "project": payload.get("project"),
        "module": payload.get("module"),
        "criterion_id": payload.get("criterion_id"),
        "logged_at": payload.get("logged_at"),
        "time_to_resolve_minutes": payload.get("time_to_resolve_minutes"),
        "outcome": payload.get("outcome"),
        "retest_triggered": payload.get("retest_triggered"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    _ensure_data_dir()
    log_path = DATA_DIR / "interventions.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(safe) + "\n")

    return {"status": "recorded"}


@app.get("/insights")
def insights():
    """Aggregated intervention patterns across all reporting projects."""
    _ensure_data_dir()
    log_path = DATA_DIR / "interventions.jsonl"

    if not log_path.exists():
        return {"total": 0, "by_type": {}, "message": "no data yet"}

    entries = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    by_type: dict[str, dict] = {}
    for e in entries:
        t = e.get("type", "unknown")
        if t not in by_type:
            by_type[t] = {"count": 0, "times": []}
        by_type[t]["count"] += 1
        mins = e.get("time_to_resolve_minutes")
        if mins:
            by_type[t]["times"].append(mins)

    return {
        "total": len(entries),
        "by_type": {
            k: {
                "count": v["count"],
                "avg_minutes": round(sum(v["times"]) / len(v["times"]), 1) if v["times"] else None,
            }
            for k, v in sorted(by_type.items(), key=lambda x: -x[1]["count"])
        },
    }
