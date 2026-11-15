"""kfm_tdx_industry FastAPI 入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db
from .routes import health, query, sync

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.load()
    db.ensure_seed_db()
    db.init_schema()
    tdx_dir = config.path("tdx_files_dir")
    (tdx_dir / "hq_cache").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="kfm_tdx_industry",
    version=__version__,
    description="A股行业/概念/风格/地区板块：通达信文件 + 网络双轨获取（SQLite）",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(sync.router)
app.include_router(query.router)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    page = STATIC_DIR / "index.html"
    if page.is_file():
        return FileResponse(page)
    return {"ok": True, "version": __version__, "docs": "/docs"}
