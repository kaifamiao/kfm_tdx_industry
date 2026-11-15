"""运行配置。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
LOCAL_CONFIG_PATH = ROOT / "config.local.json"

_DEFAULTS = {
    "host": "0.0.0.0",
    "port": 13520,
    "db_path": "data/industry.db",
    "tdx_files_dir": "data/tdx_files",
    "tdx_install_hq_cache": "",
    "tushare": {
        "token": "",
        "http_url": "https://api.tushare.pro",
    },
    "mairui": {
        "licence": "",
        "base_url": "https://api.mairuiapi.com",
    },
}

_cfg: dict | None = None


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    global _cfg
    data = dict(_DEFAULTS)
    if CONFIG_PATH.exists():
        data = _deep_merge(data, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if LOCAL_CONFIG_PATH.exists():
        data = _deep_merge(data, json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")))
    _cfg = data
    return data


def get() -> dict:
    return _cfg if _cfg is not None else load()


def path(key: str) -> Path:
    p = Path(get()[key])
    return p if p.is_absolute() else ROOT / p


def tushare_cfg() -> dict:
    return dict(get().get("tushare") or {})


def mairui_cfg() -> dict:
    return dict(get().get("mairui") or {})
