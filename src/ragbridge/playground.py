"""Serves the playground: a few static files, under a strict Content-Security-Policy.

The page is for trying the service on your own files and seeing what
retrieval did (docs/plans/phase-6-ui.md). It is plain HTML, CSS and
JavaScript with no build step, served by this app so the browser calls
the API on its own origin - which is why no CORS middleware is needed
(decision 4).
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

PLAYGROUND_DIR = Path(__file__).parent / "static" / "playground"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
"""Everything comes from this origin. In particular ``script-src 'self'``
makes a browser refuse inline ``<script>`` blocks and ``onclick=``
attributes - so the page cannot use them, and any markup that slips into
it cannot run script either (decision 5). Checked in a real browser, not
assumed: docs/plans/phase-6-ui.md, "Where things stand".
"""

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
"""Set by the app, not by the Caddy proxy: the proxy only exists in
production, and this page mostly runs in development."""


class PlaygroundFiles(StaticFiles):
    """``StaticFiles`` that adds the security headers to every response."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(SECURITY_HEADERS)
        return response


def mount_playground(app: FastAPI) -> None:
    """Serve the playground at ``/playground``.

    Mounted after every API router, so it can never shadow an endpoint.
    ``html=True`` serves ``index.html`` for the directory itself.
    """
    app.mount("/playground", PlaygroundFiles(directory=PLAYGROUND_DIR, html=True))
