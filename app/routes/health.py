from fastapi import APIRouter

from .. import __version__, config, db

router = APIRouter(prefix="/api", tags=["系统"])


@router.get("/health")
def health():
    cfg = config.get()
    tdx = config.path("tdx_files_dir")
    return {
        "ok": True,
        "version": __version__,
        "port": cfg["port"],
        "db": str(db.db_path()),
        "tdx_files_dir": str(tdx),
        "tdx_files_exist": tdx.exists(),
        "stats": db.stats(),
    }


@router.get("/stats")
def stats():
    return db.stats()
