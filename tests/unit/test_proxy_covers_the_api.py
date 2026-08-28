"""The browser reaches the API through a Next proxy, not through port 8000.

`NEXT_PUBLIC_API_BASE=/api/backend` routes every browser request through
`app/api/backend/[...path]/route.ts`, which forwards to FastAPI. That file
exports one handler per HTTP verb, and Next returns **405** for any verb with no
export — regardless of what the backend supports.

This is a gap no API-level test can see. A `PUT` endpoint verified against
127.0.0.1:8000 passes every assertion and is still dead in the product, because
the browser's request never leaves Next. That is exactly what happened to
`PUT /lecturer/courses/{id}/voice`: fully tested, fully working, and the
dropdown in the UI silently did nothing.

CORS has the same shape of hole one layer further out, for any deployment that
talks to the API directly rather than through the proxy.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PROXY = BACKEND.parent / "merakiai-frontend" / "app" / "api" / "backend" / "[...path]" / "route.ts"

# Verbs the proxy is not expected to forward. HEAD is served by GET, and
# OPTIONS is answered by Next itself for same-origin requests.
_NOT_FORWARDED = {"HEAD", "OPTIONS"}


def _api_methods() -> set[str]:
    """Every HTTP verb the FastAPI app actually serves."""
    import app.main
    from fastapi.routing import APIRoute

    methods: set[str] = set()
    for route in app.main.app.routes:
        if isinstance(route, APIRoute):
            methods |= set(route.methods)
    return methods - _NOT_FORWARDED


def _proxy_methods() -> set[str]:
    source = PROXY.read_text(encoding="utf-8")
    return set(re.findall(r"export async function ([A-Z]+)\s*\(", source))


@pytest.mark.skipif(not PROXY.exists(), reason="frontend not present")
class TestEveryVerbSurvivesTheProxy:
    def test_the_proxy_forwards_every_verb_the_api_serves(self):
        missing = _api_methods() - _proxy_methods()
        assert not missing, (
            f"the API serves {sorted(missing)} but the Next proxy exports no handler "
            f"for them, so the browser gets 405 while direct calls to port 8000 "
            f"succeed. Add `export async function <VERB>` to {PROXY.name}."
        )

    def test_the_forwarder_itself_is_method_agnostic(self):
        """The handlers are one-liners onto forward(); if that ever stops being
        true, adding a verb becomes more than adding an export."""
        source = PROXY.read_text(encoding="utf-8")
        for verb in _proxy_methods():
            body = source.split(f"export async function {verb}(", 1)[1].split("}", 1)[0]
            assert "forward(request" in body, f"{verb} does not delegate to forward()"


class TestCorsAllowsWhatTheApiServes:
    """A deployment pointing the browser straight at the API (no proxy) needs
    the preflight to allow the same verbs."""

    def test_allowed_methods_cover_the_api(self):
        import app.main

        cors = next(
            m for m in app.main.app.user_middleware
            if "CORSMiddleware" in str(m.cls)
        )
        allowed = set(cors.kwargs["allow_methods"])
        if "*" in allowed:
            return
        missing = _api_methods() - allowed
        assert not missing, (
            f"the API serves {sorted(missing)} but CORS does not allow them; a "
            "browser calling the API directly would fail preflight."
        )
