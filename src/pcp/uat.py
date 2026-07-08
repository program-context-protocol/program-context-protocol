"""UAT checks — `url_responds` and `dom_contains` acceptance criteria.

Honest scope: both checks here are real and deterministic, no browser
involved. `url_responds` is a plain HTTP request. `dom_contains` fetches
the raw HTML response and searches it as text — it does NOT execute
JavaScript, so content only rendered client-side (a typical SPA) will not
be found even if a real browser would show it. That gap needs actual
browser automation (Playwright, or an agent session with browser MCP
tools) — not built here, flagged in doctor.py's own "not directly
verified" note. `visual` (screenshot/visual-diff) criteria are not
implemented at all and fall through to manual trust, same as before this
module existed — nothing here silently claims visual coverage.

Same tool-wrapping shape as qa.py: never raises, degrades to a clear
failure detail instead.
"""

import re
import urllib.error
import urllib.request

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
