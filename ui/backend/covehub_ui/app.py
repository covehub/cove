from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .indexer import (
    CoveHubIndexer,
    ObjectNotFoundError,
    UiSettings,
    UnsafeObjectPathError,
)


def create_app(settings: UiSettings | None = None) -> FastAPI:
    settings = settings or UiSettings.from_env()
    indexer = CoveHubIndexer(settings)
    app = FastAPI(title="CoveHub Public Browser", version="0.1.0")
    app.state.indexer = indexer

    @app.get("/ui-api/healthz")
    def healthz() -> dict[str, object]:
        return {
            "ok": settings.data_root.is_dir(),
            "data_root": str(settings.data_root),
        }

    @app.get("/ui-api/summary")
    def summary() -> dict[str, object]:
        return indexer.summary()

    @app.get("/ui-api/objects")
    def objects(
        kind: str | None = None,
        q: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, object]:
        return indexer.list_objects(kind=kind, query=q, limit=limit)

    @app.get("/ui-api/objects/{hub_path:path}")
    def object_detail(hub_path: str) -> dict[str, object]:
        try:
            return indexer.object_detail(hub_path)
        except ObjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail="object not found") from exc
        except UnsafeObjectPathError as exc:
            raise HTTPException(status_code=400, detail="invalid CoveHub object path") from exc

    @app.get("/ui-api/glossary")
    def glossary() -> dict[str, object]:
        return indexer.glossary()

    docs_root = Path(
        os.getenv(
            "COVE_UI_DOCS_ROOT",
            str(Path(__file__).resolve().parents[3] / "docs"),
        )
    )
    if docs_root.is_dir():
        app.mount("/docs", StaticFiles(directory=docs_root), name="docs")

    static_root = Path(
        os.getenv(
            "COVE_UI_STATIC_ROOT",
            str(Path(__file__).resolve().parents[2] / "static"),
        )
    )
    if static_root.is_dir():
        assets_root = static_root / "assets"
        if assets_root.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_root), name="assets")

        @app.get("/{path:path}")
        def spa(path: str = "") -> FileResponse:
            return FileResponse(static_root / "index.html")

    return app


app = create_app()
