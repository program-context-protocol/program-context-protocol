"""PCP server — version endpoint, intervention collector, health check."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

app = FastAPI(title="PCP Server", version="1.0.0", docs_url="/docs")

DATA_DIR = Path(os.getenv("PCP_DATA_DIR", "/data/pcp"))
SKILL_VERSION = "1.0.0"

# Origin repo is private — the install doc and wheel are self-hosted here
# instead of pulled from raw.githubusercontent.com / PyPI. See SKILL.md.
_REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_MD_PATH = Path(os.getenv("SKILL_MD_PATH", "/app/SKILL.md"))
if not SKILL_MD_PATH.exists():
    SKILL_MD_PATH = _REPO_ROOT / "SKILL.md"
WHEEL_DIR = Path(os.getenv("WHEEL_DIR", "/app/dist"))
if not WHEEL_DIR.exists():
    WHEEL_DIR = _REPO_ROOT / "dist"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/version")
def version(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "version": SKILL_VERSION,
        "skill_url": f"{base}/skill",
        "wheel_url": f"{base}/download/pcp-latest.whl",
    }


@app.get("/skill", response_class=PlainTextResponse)
def skill(request: Request):
    """Self-install doc an LLM fetches to bootstrap PCP into a project."""
    if not SKILL_MD_PATH.exists():
        raise HTTPException(status_code=404, detail="SKILL.md not found")
    base = str(request.base_url).rstrip("/")
    return SKILL_MD_PATH.read_text().replace("{BASE_URL}", base)


@app.get("/download/pcp-latest.whl")
def download_wheel():
    if not WHEEL_DIR.exists():
        raise HTTPException(status_code=404, detail="no build artifacts")
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    if not wheels:
        raise HTTPException(status_code=404, detail="no wheel built")
    # Real wheel filename, not a "-latest" alias — pip validates the
    # name-version-pytag-abitag-platformtag format and rejects anything else.
    return FileResponse(
        wheels[-1],
        filename=wheels[-1].name,
        media_type="application/octet-stream",
    )


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
