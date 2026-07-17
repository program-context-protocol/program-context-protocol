"""UAT checks — `url_responds`, `dom_contains`, and `visual` acceptance criteria.

Honest scope: `url_responds`/`dom_contains` are deterministic, no browser
involved — `dom_contains` fetches the raw HTML response and searches it as
text, so it does NOT execute JavaScript and content only rendered
client-side (a typical SPA) won't be found even if a real browser would
show it. `visual` (check_visual) closes part of that gap with a real
headless browser via Playwright — an OPTIONAL dependency
(`pip install program-context-protocol[visual]`), never a hard requirement
of this package. It proves the page renders without crashing/timing out
and saves a screenshot for human review; it deliberately does NOT attempt
automated layout-break detection via a vision LLM — this codebase's LLM
client (llm/client.py) has no image-input plumbing, and building that
untested here would ship an unverifiable claim. Same honest-disclosure
posture as `dom_contains`'s own limitation, not overclaiming AI coverage
this doesn't have.

Same tool-wrapping shape as qa.py: never raises, degrades to a clear
failure detail instead.
"""

import re
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT_SEC = 10


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
            if screenshot_path:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_path))
            browser.close()
    except Exception as e:
        return False, f"{url} failed to render in a headless browser: {e}"

    detail = f"{url} rendered successfully in a headless browser"
    if screenshot_path:
        detail += f" -- screenshot: {screenshot_path}"
    return True, detail
