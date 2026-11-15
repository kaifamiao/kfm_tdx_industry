"""SQLite 连接与建表。"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS industry (
    system TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    parent_code TEXT,
    level INTEGER,
    source TEXT NOT NULL DEFAULT 'tdx_file',
    PRIMARY KEY (system, code, source)
);

-- 版本化行业字典：旧版/新版并存，互不覆盖
CREATE TABLE IF NOT EXISTS industry_dict (
    system TEXT NOT NULL,
    version TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    parent_code TEXT,
    level INTEGER,
    effective_from TEXT,
    effective_to TEXT,
    source TEXT NOT NULL,
    synced_at TEXT,
    PRIMARY KEY (system, version, code)
);
CREATE INDEX IF NOT EXISTS idx_industry_dict_code
    ON industry_dict (system, code, version);

CREATE TABLE IF NOT EXISTS symbol_industry (
    market TEXT NOT NULL,
    code TEXT NOT NULL,
    source TEXT NOT NULL,
    tdx_code TEXT,
    sw_code TEXT,
    em_industry TEXT,
    extra_json TEXT,
    updated_at TEXT,
    PRIMARY KEY (market, code, source)
);

CREATE TABLE IF NOT EXISTS block (
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    index_code TEXT,
    source TEXT NOT NULL DEFAULT 'tdx_file',
    PRIMARY KEY (kind, code, source)
);

CREATE TABLE IF NOT EXISTS block_member (
    kind TEXT NOT NULL,
    block_code TEXT NOT NULL,
    market TEXT NOT NULL,
    code TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'tdx_file',
    PRIMARY KEY (kind, block_code, market, code, source)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    ok INTEGER,
    message TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS symbol_industry_hist (
    market TEXT NOT NULL,
    code TEXT NOT NULL,
    source TEXT NOT NULL,
    system TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    industry_name TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    update_time TEXT,
    synced_at TEXT,
    taxonomy_version TEXT,
    PRIMARY KEY (market, code, source, system, start_date)
);
CREATE INDEX IF NOT EXISTS idx_sym_ind_hist_code
    ON symbol_industry_hist (market, code, start_date);
"""


def db_path() -> Path:
    return config.path("db_path")


def seed_db_path() -> Path:
    return config.ROOT / "data" / "industry.seed.db"


def ensure_seed_db() -> bool:
    """若运行库不存在且有种子快照，则复制为 industry.db。"""
    path = db_path()
    if path.is_file() and path.stat().st_size > 0:
        return False
    seed = seed_db_path()
    if not seed.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, path)
    return True


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection):
    """增量迁移：为已有库补列/表。"""
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(symbol_industry_hist)")}
    if cols and "taxonomy_version" not in cols:
        conn.execute("ALTER TABLE symbol_industry_hist ADD COLUMN taxonomy_version TEXT")
    # 列就绪后再建依赖索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_ind_hist_ver "
        "ON symbol_industry_hist (taxonomy_version, industry_code)"
    )


def init_schema():
    with session() as conn:
        _migrate(conn)


def stats() -> dict:
    with session() as conn:
        def count(table: str, where: str = "1=1") -> int:
            try:
                return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]
            except Exception:
                return 0

        last = conn.execute(
            "SELECT track, started_at, finished_at, ok, message FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        dict_by_ver = {
            r["version"]: r["c"]
            for r in conn.execute(
                "SELECT version, count(*) c FROM industry_dict WHERE system='sw' GROUP BY version"
            )
        }
        return {
            "db_path": str(db_path()),
            "industry": count("industry"),
            "industry_dict": count("industry_dict"),
            "industry_dict_by_version": dict_by_ver,
            "symbol_industry": count("symbol_industry"),
            "symbol_industry_hist": count("symbol_industry_hist"),
            "block": count("block"),
            "block_member": count("block_member"),
            "sync_log": count("sync_log"),
            "last_sync": dict(last) if last else None,
        }
