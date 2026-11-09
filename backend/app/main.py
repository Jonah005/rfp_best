"""FastAPI application factory."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import (
    admin,
    artifacts,
    assessment,
    audit,
    auth,
    bids,
    copilot,
    diagrams,
    grants,
    health,
    knowledge,
    notifications,
    release,
    tasks,
    traceability,
    writeup,
)
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, get_logger

log = get_logger("app")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Make sure the object store bucket exists before anything writes to it.

    Deliberately fail-soft. A missing bucket breaks uploads and assembly, but it
    does not break login, requirements, tasks or approvals -- so a storage
    problem is logged loudly and the rest of the API still serves, rather than
    the whole product refusing to start.
    """
    from app.services.storage import S3Storage, get_storage

    try:
        storage = get_storage()
        if isinstance(storage, S3Storage):
            storage.ensure_bucket()
            log.info("storage.bucket_ready")
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        log.error("storage.bucket_unavailable", error=str(exc))
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(json_output=settings.env != "development")

    app = FastAPI(
        title="BidReady API",
        version="0.1.0",
        description=(
            "AI-assisted Bid/RFP Operating System: governed ingestion, knowledge, "
            "tender intelligence, tasks, drafting, assembly, release, and learning."
        ),
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = uuid.uuid4()
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response

    install_error_handlers(app)

    for router in (
        auth.router,
        admin.router,
        knowledge.router,
        bids.router,
        assessment.router,
        tasks.router,
        artifacts.router,
        diagrams.router,
        writeup.router,
        release.router,
        copilot.router,
        grants.router,
        audit.router,
        notifications.router,
        traceability.router,
        health.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """Serve a statically exported frontend, when one has been built.

    This is what the single-process local runner uses: no Node at runtime, one
    origin, and therefore no CORS to configure. Mounted last so it can never
    shadow an API route. Absent a build, the API simply runs alone.
    """
    import os
    from pathlib import Path

    configured = os.environ.get("BIDREADY_UI_DIR")
    default_dir = Path(__file__).resolve().parents[2] / "frontend" / "out"
    ui_dir = Path(configured) if configured else default_dir
    if not (ui_dir / "index.html").is_file():
        return

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # Two kinds of file, two opposite caching rules, and neither was stated.
    #
    # Without a Cache-Control header a browser is free to cache heuristically,
    # and it does. That is how a deployed fix reached a browser still running
    # the previous build three separate times in one afternoon: the exported
    # HTML was cached, so it kept naming the chunk hashes from an older export
    # -- files that had already been deleted from disk. Every symptom pointed
    # at the code, because the code on disk was correct.
    #
    # The asset filenames are content hashes, so a chunk can never change under
    # its own name and may be cached forever. The HTML that names them changes
    # on every build and must be revalidated every time. "no-cache" does not
    # mean do not store: it means ask before reusing, which is exactly right
    # for a small document that decides which JavaScript the page runs.
    REVALIDATE = {"Cache-Control": "no-cache, must-revalidate"}
    IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

    def _headers(path: str) -> dict[str, str]:
        # `_next/static` filenames embed a hash of their own contents, so a file
        # there can never change under its own name. Everything else -- the HTML
        # above all -- is rewritten by each build under the same name.
        #
        # The catch-all below is registered before the /_next mount and so wins
        # for asset paths too, which is why this decision lives here rather than
        # in a StaticFiles subclass: a subclass would have been dead code, and
        # the assets would have quietly inherited the document's rules.
        return IMMUTABLE if path.startswith("_next/static/") else REVALIDATE

    @app.get("/{page:path}", include_in_schema=False)
    def ui(page: str):
        """Resolve a screen path to its exported HTML file."""
        headers = _headers(page)
        candidate = (ui_dir / page).resolve()
        if candidate.is_relative_to(ui_dir):
            if candidate.is_dir() and (candidate / "index.html").is_file():
                return FileResponse(candidate / "index.html", headers=headers)
            if candidate.is_file():
                return FileResponse(candidate, headers=headers)
            html = candidate.with_suffix(".html")
            if html.is_file() and html.is_relative_to(ui_dir):
                return FileResponse(html, headers=headers)
        # A path that resolves to nothing is a client-side route: serve the
        # shell, and never with the asset rules -- an immutable 404 shell is a
        # browser that can never recover.
        return FileResponse(ui_dir / "index.html", headers=REVALIDATE)

    app.mount("/_next", StaticFiles(directory=ui_dir / "_next"), name="next-assets")
    log.info("ui.mounted", path=str(ui_dir))


app = create_app()
