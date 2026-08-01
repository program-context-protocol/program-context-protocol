"""Per-vendor harness implementations.

One file per harness (claude.py, agy.py, ...), each implementing the same
contract client.py's call()/call_json() dispatch against: a `_call_<name>`
function taking (system, user, model=None, pcp_dir=None, command=...,
return_meta=False) and returning text, or (text, meta) when return_meta is
True, raising RuntimeError on a CLI-level failure. See client.py's own
module docstring (SUPPORTED_HARNESSES section) for how a new harness plugs
in and what this seam does and does not cover -- it's the judge/generation
call path only, not `pcp build`'s own coding-agent loop (still Claude-Code-
specific, lives in commands/build.py, not abstracted here).
"""
