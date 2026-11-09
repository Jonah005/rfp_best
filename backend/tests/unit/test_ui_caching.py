"""The exported UI has to say how long it may be cached, and it never did.

Three deployed fixes in one afternoon reached a browser that was still running
the previous build. Each time the evidence pointed at the code: the source on
disk was right, the built chunk contained the fix, the bundle had been unpacked,
and the page still behaved as it had that morning. The last of them was only
settled by reading the served HTML, which named two files -- a page chunk and a
stylesheet -- that had already been deleted from disk.

Nothing set `Cache-Control`, so browsers cached heuristically, and the document
that decides which JavaScript a page runs was the worst possible thing to guess
about. The asset filenames are content hashes and can be cached forever; the
HTML naming them changes every build and must be revalidated every time. Those
are opposite rules, and both of them were unstated.

The subtle half is which code path serves what. The catch-all route is
registered before the `/_next` mount, so it wins for asset paths too -- a
`StaticFiles` subclass setting the asset header would never have run, and the
assets would have silently inherited the document's rules. That is asserted
here directly, because it is invisible by inspection and passed review once.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    (tmp_path / "_next" / "static" / "chunks").mkdir(parents=True)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "index.html").write_text("<html>shell</html>")
    (tmp_path / "tasks" / "index.html").write_text("<html>tasks</html>")
    (tmp_path / "_next" / "static" / "chunks" / "page-abc123.js").write_text("console.log(1)")

    monkeypatch.setenv("BIDREADY_UI_DIR", str(tmp_path))
    from app.main import create_app

    return TestClient(create_app())


def cache_control(client: TestClient, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.headers.get("cache-control", "")


def test_a_screen_document_is_revalidated_every_time(client: TestClient):
    """This is the one that broke. The HTML names the chunk hashes, so a cached
    document pins the browser to a build that no longer exists on disk."""
    assert "no-cache" in cache_control(client, "/tasks")


def test_the_shell_is_revalidated_every_time(client: TestClient):
    assert "no-cache" in cache_control(client, "/")


def test_a_content_hashed_asset_may_be_cached_forever(client: TestClient):
    """The filename is a hash of the contents, so it can never change meaning.
    Revalidating it on every navigation is pure waste."""
    header = cache_control(client, "/_next/static/chunks/page-abc123.js")
    assert "immutable" in header
    assert "max-age=31536000" in header


def test_the_asset_rule_survives_the_catch_all_winning(client: TestClient):
    """The catch-all is registered before the /_next mount and matches asset
    paths first. If the immutable header were set only on the mount it would be
    dead code -- this asserts the header on the path a request actually takes."""
    from app.main import create_app

    app = create_app()
    routes = [getattr(route, "path", "") for route in app.routes]
    catch_all = next(i for i, path in enumerate(routes) if path == "/{page:path}")
    mount = next(i for i, path in enumerate(routes) if path == "/_next")
    assert catch_all < mount, "if the mount ever wins first, move the header with it"
    assert "immutable" in cache_control(client, "/_next/static/chunks/page-abc123.js")


def test_an_unknown_route_is_never_cached_forever(client: TestClient):
    """An unresolved path falls back to the shell. Serving that with the asset
    rules would give someone a permanently cached wrong page."""
    assert "no-cache" in cache_control(client, "/does-not-exist")
