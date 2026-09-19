"""Tests for the playground page: serving, headers, and rules the page must keep.

``enable_playground`` is read when the app is built, so the on/off tests
set the environment and rebuild it, as ``test_api_docs.py`` does. The page
itself needs no database, so none of these use ``app_with_database``.

The last group are *guards*: the JavaScript is not run in CI (decision 9,
docs/plans/phase-6-ui.md), but the two rules that keep the page safe can be
checked as plain text. They exist so an ordinary-looking edit cannot
quietly undo the Content-Security-Policy or the ``textContent`` rule.
"""

import re
from collections.abc import Iterator
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from ragbridge.config import get_settings
from ragbridge.main import create_app
from ragbridge.playground import PLAYGROUND_DIR

PAGE_FILES = ("index.html", "playground.css", "playground.js")


@pytest.fixture
def rebuilt_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Let a test set settings in the environment and build a fresh app."""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def client(rebuilt_app: pytest.MonkeyPatch) -> TestClient:
    rebuilt_app.delenv("ENABLE_PLAYGROUND", raising=False)
    return TestClient(create_app())


def test_the_page_is_served_by_default(client: TestClient) -> None:
    response = client.get("/playground/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ragbridge playground" in response.text


def test_the_directory_without_a_trailing_slash_redirects_to_the_page(client: TestClient) -> None:
    response = client.get("/playground", follow_redirects=False)

    assert response.status_code in (307, 308)
    assert response.headers["location"].endswith("/playground/")


def test_every_file_carries_the_security_headers(client: TestClient) -> None:
    for name in PAGE_FILES:
        response = client.get(f"/playground/{name}")

        assert response.status_code == 200, name
        policy = response.headers["content-security-policy"]
        assert "script-src 'self'" in policy, name
        assert "default-src 'self'" in policy, name
        assert "frame-ancestors 'none'" in policy, name
        assert response.headers["x-content-type-options"] == "nosniff", name
        assert response.headers["referrer-policy"] == "no-referrer", name


def test_the_policy_allows_nothing_from_outside_this_origin(client: TestClient) -> None:
    policy = client.get("/playground/").headers["content-security-policy"]

    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "http:" not in policy and "https:" not in policy
    assert "*" not in policy


def test_the_static_files_have_the_right_content_types(client: TestClient) -> None:
    assert client.get("/playground/playground.css").headers["content-type"].startswith("text/css")
    js_type = client.get("/playground/playground.js").headers["content-type"]
    assert "javascript" in js_type


def test_the_page_cannot_be_used_to_read_files_outside_its_folder(client: TestClient) -> None:
    for path in ("/playground/%2e%2e%2f%2e%2e%2f.env", "/playground/../../../.env"):
        assert client.get(path).status_code == 404, path


def test_no_cors_headers_are_added(client: TestClient) -> None:
    """Same origin needs no CORS (decision 4): the service must not start sending it."""
    response = client.get("/playground/", headers={"Origin": "http://evil.example"})

    assert "access-control-allow-origin" not in response.headers


def test_the_api_is_unaffected_by_the_playground(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/playground/health").status_code == 404


def test_the_page_is_absent_when_disabled(rebuilt_app: pytest.MonkeyPatch) -> None:
    rebuilt_app.setenv("ENABLE_PLAYGROUND", "false")
    client = TestClient(create_app())

    for path in ("/playground", "/playground/", "/playground/playground.js"):
        assert client.get(path, follow_redirects=False).status_code == 404, path
    assert client.get("/health").status_code == 200


# --- guards ------------------------------------------------------------------


class _Scan(HTMLParser):
    """Collects what the CSP would silently block or that would break the rules."""

    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts: list[str] = []
        self.event_handlers: list[str] = []
        self.style_uses: list[str] = []
        self.references: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script":
            self._in_script = "src" not in attributes
            if "src" not in attributes:
                self.inline_scripts.append("<script> without src")
        if tag == "style":
            self.style_uses.append("<style> element")
        for name, value in attrs:
            if name.startswith("on"):
                self.event_handlers.append(f"<{tag} {name}=...>")
            if name == "style":
                self.style_uses.append(f"<{tag} style=...>")
            if name in ("src", "href") and value and not value.startswith(("data:", "#")):
                self.references.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip())


def _scan_page() -> _Scan:
    scan = _Scan()
    scan.feed((PLAYGROUND_DIR / "index.html").read_text())
    return scan


def test_every_file_the_page_references_exists() -> None:
    references = _scan_page().references

    assert references, "the page should reference its own CSS and JavaScript"
    for reference in references:
        assert not reference.startswith(("http:", "https:", "//")), f"external: {reference}"
        assert (PLAYGROUND_DIR / reference).is_file(), reference


def test_the_page_has_no_inline_script_or_event_handler() -> None:
    """The policy blocks these, so they would fail silently in a browser (decision 5)."""
    scan = _scan_page()

    assert scan.inline_scripts == []
    assert scan.event_handlers == []


def test_the_page_has_no_inline_styles() -> None:
    """``style-src 'self'`` blocks ``<style>`` and ``style=`` too."""
    assert _scan_page().style_uses == []


FORBIDDEN_IN_JAVASCRIPT = (
    r"\.innerHTML\b",
    r"\.outerHTML\b",
    r"\binsertAdjacentHTML\b",
    r"\bdocument\.write",
    r"\beval\s*\(",
    r"\bnew\s+Function\b",
    r"\bsrcdoc\b",
)


def _code_only(source: str) -> str:
    """Drop block comments and whole-line ``//`` comments, so a comment that
    *explains* a rule does not trip the check for it.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.lstrip().startswith("//")
    )


def test_the_javascript_never_builds_markup_from_text() -> None:
    """Document text and model answers are untrusted (decision 6): they may only
    be shown with ``textContent``, which cannot create an element.
    """
    code = _code_only((PLAYGROUND_DIR / "playground.js").read_text())

    for pattern in FORBIDDEN_IN_JAVASCRIPT:
        assert re.search(pattern, code) is None, pattern


def test_the_guard_notices_a_forbidden_call() -> None:
    """A guard that never fails proves nothing: check that it can."""
    code = _code_only("element.innerHTML = answer;\n// only a comment about innerHTML\n")

    assert re.search(FORBIDDEN_IN_JAVASCRIPT[0], code) is not None
    assert "only a comment" not in code
