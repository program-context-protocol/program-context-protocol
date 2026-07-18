"""UAT checks — `url_responds`, `dom_contains`, and `visual` acceptance criteria,
plus two advisory checks build.py runs on top of a rendered screenshot:
`check_axe` (deterministic a11y scan) and `check_visual_quality` (checklist-
anchored VLM judge).

Honest scope: `url_responds`/`dom_contains` are deterministic, no browser
involved — `dom_contains` fetches the raw HTML response and searches it as
text, so it does NOT execute JavaScript and content only rendered
client-side (a typical SPA) won't be found even if a real browser would
show it. `visual` (check_visual) closes part of that gap with a real
headless browser via Playwright — an OPTIONAL dependency
(`pip install program-context-protocol[visual]`), never a hard requirement
of this package. It proves the page renders without crashing/timing out
and saves a screenshot for human review.

**Updated 2026-07-18** — layout-break detection via a vision LLM is now
built (`check_visual_quality`), closing the gap this docstring used to name
as out of scope. What changed: `llm/client.py` gained image-input plumbing
(`call_with_image`/`call_json_with_image`, via `claude -p`'s
`--input-format stream-json` multimodal message shape). Deliberately
**checklist-anchored, not a freeform "does this look good" prompt** —
research (ArtifactsBench, 2026) found a checklist-anchored VLM judge hits
~94% human-correlation vs. ~21% for a bare Nielsen-heuristics-style review;
the checklist is what does the work, not the model. Same advisory,
never-a-hard-block posture as `check_visual`'s baseline-diff note below —
a screen scoring poorly on the checklist is a review signal, not proof the
screen is wrong.

Reconnaissance-then-action pattern (wait for `networkidle` before reading
DOM state) is a reference-pattern borrowed from Anthropic's own
`webapp-testing` skill (anthropics/skills) after a real prior-art miss
found post-hoc: `check_visual` shipped without checking for it first,
initially screenshotting right after `goto()` with no settle-wait — on a
slow-loading SPA that can capture a blank/loading state and still report
"rendered successfully," exactly the failure mode this check exists to
catch. `webapp-testing` itself is a full agentic skill (server lifecycle,
selector discovery, browser console logs) meant for an interactive Claude
session — not adopted wholesale, since `check_visual` runs inside `pcp
scan`'s deterministic, zero-LLM evaluation loop and turning that into an
agent invocation per UI criterion would break Token Discipline. Only the
underlying technique was reused; `pcp build`'s coding agent is pointed at
the real skill separately for its own in-build UI verification.

Same tool-wrapping shape as qa.py: never raises, degrades to a clear
failure detail instead.
"""

import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT_SEC = 10
TIMEOUT_AXE = 120

# Checklist-anchored, per the research finding above -- deliberately generic
# and small rather than exhaustive, same "advisory signal, not proof" posture
# as check_visual's baseline diff. A criterion's own design_justification
# (design_system.md tokens, jtbd_framing) is NOT folded in here as additional
# checklist items -- that field is a self-report the agent fills in, judging
# a screen against the agent's own claims about itself would be circular.
DEFAULT_VISUAL_CHECKLIST = [
    "layout is not visibly broken (no overlapping elements, no obvious clipping/overflow)",
    "text is legible (adequate contrast against its background, not truncated where it shouldn't be)",
    "primary action or focal element is visually prominent and easy to locate",
    "spacing/alignment reads as intentional, not haphazard",
]


def check_url_responds(url: str) -> tuple[bool, str]:
    """Pass if the URL returns a 2xx/3xx status. No content is inspected."""
    if not url:
        return False, "no url configured for url_responds check"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            ok = 200 <= resp.status < 400
            return ok, f"{url} responded {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"{url} responded {e.code}"
    except Exception as e:
        return False, f"{url} did not respond: {e}"


def check_dom_contains(url: str, selector: str) -> tuple[bool, str]:
    """Pass if `selector` (plain text, or a regex if it fails to match literally)
    appears in the URL's raw HTML response. Static content only — see module
    docstring for the JS-rendering limitation."""
    if not url:
        return False, "no url configured for dom_contains check"
    if not selector:
        return False, "no selector/text configured for dom_contains check"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = resp.read().decode(errors="replace")
    except Exception as e:
        return False, f"{url} did not respond: {e}"

    if selector in body:
        return True, f"'{selector}' found in {url} (static HTML)"
    try:
        if re.search(selector, body):
            return True, f"pattern '{selector}' matched in {url} (static HTML)"
    except re.error:
        pass
    return False, f"'{selector}' not found in {url}'s static HTML (JS-rendered content won't show here)"


def check_visual(url: str, screenshot_path: Path | None = None) -> tuple[bool | None, str]:
    """Loads `url` in a real headless browser (Playwright/Chromium) and
    captures a screenshot. Returns (None, detail) — not (False, detail) —
    when playwright isn't installed: this means "could not check", not
    "failed". Callers must preserve whatever status a criterion already had
    rather than downgrading it on a missing optional dependency, the same
    posture a manual/visual criterion without this check already gets in
    scan.py."""
    if not url:
        return False, "no url configured for visual check"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, (
            "playwright not installed -- visual check skipped, not failed. "
            "Install with: pip install program-context-protocol[visual] && playwright install chromium"
        )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=TIMEOUT_SEC * 1000)
            # Reconnaissance-then-action pattern (reference: anthropics/skills'
            # webapp-testing) -- goto() alone returns once the initial HTML
            # response lands, before a typical SPA's JS has actually rendered
            # content. Without this wait, a screenshot on a slow-loading SPA
            # can capture a blank/loading state and still report "rendered
            # successfully" -- exactly the failure mode this check exists to
            # catch (dom_contains's own JS-rendering gap). networkidle waits
            # for in-flight requests to settle before the check reads DOM state.
            page.wait_for_load_state("networkidle", timeout=TIMEOUT_SEC * 1000)
            if screenshot_path:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_path), full_page=True)
            browser.close()
    except Exception as e:
        return False, f"{url} failed to render in a headless browser: {e}"

    detail = f"{url} rendered successfully in a headless browser"
    if screenshot_path:
        detail += f" -- screenshot: {screenshot_path}"
        detail += _baseline_note(screenshot_path)
    return True, detail


def _baseline_note(screenshot_path: Path) -> str:
    """Baseline comparison (Chromatic reference pattern, 2026-07-17, build
    plan 3.5) — closes part of check_visual's own stated 'not visual
    regression testing' gap. First successful capture becomes the baseline
    (`<name>_baseline.png`); later captures are compared by content hash.
    HONEST SCOPE: hash inequality means "pixels changed since the accepted
    baseline", not "layout broke" — a changed screenshot is a review signal,
    never a failure. Accept a new baseline by deleting the old one."""
    import hashlib
    baseline = screenshot_path.with_name(screenshot_path.stem + "_baseline.png")
    try:
        current = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
        if not baseline.exists():
            baseline.write_bytes(screenshot_path.read_bytes())
            return " -- baseline established (first capture)"
        if hashlib.sha256(baseline.read_bytes()).hexdigest() == current:
            return " -- matches accepted baseline"
        return (f" -- CHANGED vs accepted baseline ({baseline.name}); review the two "
                "screenshots and delete the baseline to accept the new look")
    except OSError as e:
        return f" -- baseline comparison skipped: {e}"


def check_axe(url: str) -> tuple[bool | None, str]:
    """Deterministic WCAG a11y scan via `@axe-core/cli` (npx, auto-installed
    on first run same as the Context7 MCP entry doctor.py already scaffolds
    via npx -- no new hard dependency added to this package). Returns
    (None, detail) when npx isn't on PATH -- "could not check", same
    could-not-check-vs-failed distinction check_visual already makes for a
    missing optional dependency. --exit makes the CLI process exit 1 if any
    rule fails; --stdout silences everything but the results/errors so the
    tail of stdout is a usable detail message on failure."""
    if not url:
        return False, "no url configured for axe a11y check"
    if not shutil.which("npx"):
        return None, "npx not found -- axe-core a11y scan skipped, not failed"
    try:
        result = subprocess.run(
            ["npx", "--yes", "@axe-core/cli", url, "--exit", "--stdout"],
            capture_output=True, text=True, timeout=TIMEOUT_AXE,
        )
    except subprocess.TimeoutExpired:
        return False, f"axe-core scan of {url} timed out after {TIMEOUT_AXE}s"
    except Exception as e:
        return False, f"axe-core scan of {url} failed to run: {e}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, f"axe-core: no violations found at {url}"
    return False, f"axe-core found violation(s) at {url}:\n{output[-3000:]}"


def check_visual_quality(
    screenshot_path: Path,
    checklist: list[str] | None = None,
    reference_image_path: Path | None = None,
    model: str | None = None,
    pcp_dir: Path | None = None,
) -> tuple[bool | None, str, list[dict]]:
    """Checklist-anchored VLM judge over a screenshot check_visual already
    captured. Returns (None, detail, []) if the screenshot doesn't exist
    (nothing to judge -- same could-not-check posture as a missing optional
    dependency elsewhere in this module) or if the judge call itself errors
    (advisory check, never let an LLM-call failure read as a verdict).

    reference_image_path, if given, is attached as a second inline image so
    the judge can compare against it -- research finding: screenshot +
    reference beats either alone as grounding context. Comparison is
    layout/structure, not pixel-perfect match; the prompt says so explicitly
    so the judge doesn't fail a screen for a legitimate content difference.
    """
    if not screenshot_path or not screenshot_path.exists():
        return None, "no screenshot available to judge (check_visual must run first)", []

    from pcp.llm import client as llm

    items = checklist or DEFAULT_VISUAL_CHECKLIST
    checklist_text = "\n".join(f"- {item}" for item in items)
    system = (
        "You judge a rendered UI screenshot against a fixed checklist. For EACH "
        "checklist item, decide pass/fail and give a one-sentence reason grounded "
        "in what you actually see in the image -- never invent a defect that isn't "
        "visible. If a reference image is also attached, use it only to judge "
        "layout/structure similarity, not pixel-perfect match; a legitimate content "
        "difference (different copy, different data) is not a failure."
    )
    user = (
        f"Checklist:\n{checklist_text}\n\n"
        "Respond with JSON: "
        '{"items": [{"item": "<checklist item text>", "passed": true|false, "reason": "..."}], '
        '"overall_passed": true|false}. overall_passed is true only if every item passed.'
    )
    if reference_image_path and reference_image_path.exists():
        user += "\n\nA reference image is attached second, after the rendered screenshot, for comparison."

    image_paths = [screenshot_path]
    if reference_image_path and reference_image_path.exists():
        image_paths.append(reference_image_path)

    try:
        verdict = llm.call_json_with_images(
            system, user, image_paths,
            model=model or llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="uat.check_visual_quality",
        )
    except Exception as e:
        return None, f"visual-quality judge call failed: {e}", []

    checked_items = verdict.get("items", [])
    overall = bool(verdict.get("overall_passed", all(i.get("passed") for i in checked_items)))
    failed = [i for i in checked_items if not i.get("passed")]
    if not overall and failed:
        detail = "; ".join(f"{i.get('item', '?')}: {i.get('reason', '')}" for i in failed)
    else:
        detail = "all checklist items passed"
    return overall, detail, checked_items
