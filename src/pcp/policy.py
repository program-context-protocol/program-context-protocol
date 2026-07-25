"""Decision logic layer (Tier 2, Phase B) — consolidates the scattered rule
mechanisms already in PCP (ci_rules.yaml deterministic checks, gate.py/
architect-review LLM judgment, coupling.py thresholds) behind one queryable,
stateless policy engine: OPA (Open Policy Agent).

One-shot CLI eval only — no opa-python-client dependency, no running OPA
server. Same shutil.which-gate + subprocess.run tool-wrapping shape as
audit.py/doctor.py, consistent with PCP's existing CLI-first/no-new-services
posture (this is a deliberate choice over the opa-python-client + REST-server
approach: that would add a persistent service PCP has to manage, for no
benefit over a one-shot eval at the point a decision is actually needed).

Human-authored policies live in .pcp/policies/*.rego — same "human authorizes,
tooling reads" posture as ci_rules.yaml.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

OPA_TIMEOUT_SEC = 30


def opa_available() -> bool:
    return shutil.which("opa") is not None


def get_policies_dir(pcp_dir: Path) -> Path:
    return pcp_dir / "policies"


def evaluate(pcp_dir: Path, query: str, input_dict: dict) -> dict:
    """Runs `opa eval` against every .rego file in .pcp/policies/ with
    input_dict as OPA's "input" document. Returns:
    - {"available": False} if the opa binary isn't installed
    - {"available": True, "value": <result>, "undefined": False} on a
      defined result
    - {"available": True, "value": None, "undefined": True} if the query
      path has no matching rule (OPA's own "undefined" case — happens when
      a policy simply hasn't been written yet for this query, not an error)
    - {"available": True, "error": "..."} if opa itself fails (bad Rego,
      timeout, non-zero exit) — never raises, matches every other
      optional-tool wrapper in this codebase.
    """
    if not opa_available():
        return {"available": False}

    policies_dir = get_policies_dir(pcp_dir)
    if not policies_dir.is_dir() or not any(policies_dir.glob("*.rego")):
        return {"available": True, "value": None, "undefined": True}

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(input_dict, f)
        input_path = f.name

    try:
        result = subprocess.run(
            ["opa", "eval", "-d", str(policies_dir), "-i", input_path,
             "--format", "json", query],
            capture_output=True, text=True, timeout=OPA_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "error": f"opa eval timed out after {OPA_TIMEOUT_SEC}s"}
    finally:
        Path(input_path).unlink(missing_ok=True)

    if result.returncode != 0:
        return {"available": True, "error": result.stderr.strip() or "opa eval failed"}

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"available": True, "error": "opa returned non-JSON output"}

    expressions = parsed.get("result", [{}])[0].get("expressions", []) if parsed.get("result") else []
    if not expressions:
        return {"available": True, "value": None, "undefined": True}

    return {"available": True, "value": expressions[0].get("value"), "undefined": False}
