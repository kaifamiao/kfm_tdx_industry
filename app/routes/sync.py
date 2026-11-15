"""同步与通达信文件导入。"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import config, db
from ..parsers import sync_from_tdx_dir

router = APIRouter(prefix="/api", tags=["同步"])


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _log(track: str, ok: bool, message: str, stats: dict | None = None):
    with db.session() as conn:
        conn.execute(
            "INSERT INTO sync_log(track, started_at, finished_at, ok, message, stats_json) VALUES (?,?,?,?,?,?)",
            (track, _now(), _now(), 1 if ok else 0, message, json.dumps(stats or {}, ensure_ascii=False)),
        )


def _copy_from_install(src_path: Path) -> list[str]:
    dest = config.path("tdx_files_dir")
    dest_hq = dest / "hq_cache"
    dest_hq.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    # 安装根 / hq_cache 上级
    roots = []
    if src_path.is_dir():
        roots = [src_path, src_path.parent, src_path.parent.parent]
    candidates = [r / "incon.dat" for r in roots] + [Path(r) / "incon.dat" for r in []]
    # 也支持直接传安装根 D:\new_tdx_mock
    for c in [
        src_path / "incon.dat",
        src_path.parent / "incon.dat",
        src_path.parent.parent / "incon.dat",
    ]:
        if c.is_file():
            shutil.copy2(c, dest / "incon.dat")
            copied.append(c.name)
            break

    names = ["tdxhy.cfg", "tdxzs.cfg", "tdxzs3.cfg", "block_gn.dat", "block_fg.dat", "block_zs.dat", "block.dat"]
    # 若传入安装根，则用 T0002/hq_cache
    bases = []
    if (src_path / "T0002" / "hq_cache").is_dir():
        bases.append(src_path / "T0002" / "hq_cache")
    if src_path.name.lower() == "hq_cache" or (src_path / "tdxhy.cfg").is_file():
        bases.append(src_path)
    if (src_path / "hq_cache").is_dir():
        bases.append(src_path / "hq_cache")
    if not bases and src_path.is_dir():
        bases.append(src_path)

    seen = set()
    for base in bases:
        for name in names:
            f = base / name
            key = name
            if key in seen or not f.is_file():
                continue
            shutil.copy2(f, dest_hq / name)
            copied.append(name)
            seen.add(key)
    return copied


@router.post("/tdx/import")
def import_tdx_files(src: str | None = None):
    """从本机通达信目录拷贝到 data/tdx_files。

    src 可传安装根（如 D:\\new_tdx_mock）或 hq_cache 路径。
    """
    cfg = config.get()
    raw = src or cfg.get("tdx_install_hq_cache") or ""
    # 若配置的是 hq_cache，也允许只用安装根
    src_path = Path(raw) if raw else None
    if src_path is None or not src_path.exists():
        # 尝试从 hq_cache 配置推安装根
        raise HTTPException(400, "请提供 src（如 D:\\\\new_tdx_mock）或在 config.json 填写 tdx_install_hq_cache")

    copied = _copy_from_install(src_path)
    if not copied:
        # 再试父目录（配置指向 hq_cache 时）
        if src_path.name.lower() == "hq_cache":
            copied = _copy_from_install(src_path.parent.parent)
    if not copied:
        raise HTTPException(404, f"未在 {src_path} 找到可拷贝的通达信文件")
    _log("tdx_import", True, f"copied {len(copied)}", {"files": copied})
    return {"ok": True, "copied": copied, "dest": str(config.path("tdx_files_dir"))}


@router.post("/sync/tdx")
def sync_tdx():
    tdx = config.path("tdx_files_dir")
    hy = tdx / "hq_cache" / "tdxhy.cfg"
    incon = tdx / "incon.dat"
    missing = [p.name for p in (incon, hy) if not p.exists()]
    if missing:
        msg = f"缺少文件: {', '.join(missing)}，请先放入 data/tdx_files 或调用 /api/tdx/import"
        _log("tdx", False, msg)
        raise HTTPException(400, msg)
    try:
        stats = sync_from_tdx_dir(tdx)
    except Exception as e:
        _log("tdx", False, str(e))
        raise HTTPException(500, f"解析失败: {e}") from e
    msg = f"通达信同步完成：行业字典 {stats['industry']} / 个股 {stats['symbol_industry']} / 板块 {stats['block']} / 成分 {stats['block_member']}"
    _log("tdx", True, msg, stats)
    return {"ok": True, "message": msg, "stats": stats, "db": db.stats()}


@router.post("/sync/network")
def sync_network(
    include_concept: bool = True,
    include_region: bool = True,
    include_sw: bool = True,
    max_concepts: int | None = 120,
):
    """网络轨：新浪板块（行业/申万/概念/地域）+ 腾讯行情补名称。"""
    from ..sources import sync_from_network

    try:
        stats = sync_from_network(
            include_concept=include_concept,
            include_region=include_region,
            include_sw=include_sw,
            max_concepts=max_concepts,
        )
    except Exception as e:
        _log("network", False, str(e))
        raise HTTPException(500, f"网络轨同步失败: {e}") from e
    msg = (
        f"网络同步完成（新浪+腾讯）：行业 {stats.get('industry_boards', 0)}"
        f" / 申万 {stats.get('sw_boards', 0)}"
        f" / 概念 {stats.get('concept_boards', 0)}"
        f" / 个股 {stats.get('symbol_industry', 0)}"
        f" / 成分 {stats.get('block_member', 0)}"
    )
    if stats.get("errors"):
        msg += f"（部分失败 {len(stats['errors'])}）"
    _log("network", True, msg, stats)
    return {"ok": True, "message": msg, "stats": stats, "db": db.stats()}


@router.post("/sync/tushare")
def sync_tushare():
    """Tushare：stock_basic.industry + 申万分类等。"""
    from ..sources import sync_from_tushare

    try:
        stats = sync_from_tushare()
    except Exception as e:
        _log("tushare", False, str(e))
        raise HTTPException(500, f"Tushare 同步失败: {e}") from e
    msg = (
        f"Tushare 同步完成：个股 {stats.get('symbol_industry', 0)}"
        f" / 行业名 {stats.get('industry_names', 0)}"
        f" / 申万分类 {stats.get('sw_classify', 0)}"
        f" / 板块 {stats.get('blocks', 0)}"
    )
    _log("tushare", True, msg, stats)
    return {"ok": True, "message": msg, "stats": stats, "db": db.stats()}


@router.post("/sync/industry-history", summary="同步申万行业归属历史")
def sync_industry_history():
    """下载申万公开 xls，写入 symbol_industry_hist（source=sw_hist）。"""
    from ..sources import sync_sw_industry_history

    try:
        stats = sync_sw_industry_history()
    except Exception as e:
        _log("sw_hist", False, str(e))
        raise HTTPException(500, f"行业历史同步失败: {e}") from e
    msg = (
        f"申万行业历史同步完成：{stats.get('hist_rows', 0)} 条"
        f" / {stats.get('symbols', 0)} 只股票"
    )
    _log("sw_hist", True, msg, stats)
    return {"ok": True, "message": msg, "stats": stats, "db": db.stats()}


@router.post("/sync/mairui")
def sync_mairui(
    include_concept: bool = True,
    include_region: bool = True,
    include_sw: bool = True,
    include_csrc: bool = True,
    max_concepts: int | None = 150,
):
    """麦蕊智数：hszg 行业/概念树 + 成分（需 config.local.json 中 mairui.licence）。"""
    from ..sources import sync_from_mairui

    try:
        stats = sync_from_mairui(
            include_concept=include_concept,
            include_region=include_region,
            include_sw=include_sw,
            include_csrc=include_csrc,
            max_concepts=max_concepts,
        )
    except Exception as e:
        _log("mairui", False, str(e))
        raise HTTPException(500, f"麦蕊同步失败: {e}") from e
    msg = (
        f"麦蕊同步完成：叶子 {stats.get('leaves', 0)}"
        f" / 个股 {stats.get('symbol_industry', 0)}"
        f" / 板块 {stats.get('blocks', 0)}"
        f" / 成分 {stats.get('block_member', 0)}"
    )
    if stats.get("errors"):
        msg += f"（部分失败 {len(stats['errors'])}）"
    _log("mairui", True, msg, stats)
    return {"ok": True, "message": msg, "stats": stats, "db": db.stats()}


@router.post("/sync/all")
def sync_all():
    results = {}
    for key, fn in (
        ("tdx", sync_tdx),
        ("tushare", sync_tushare),
        ("network", sync_network),
        ("mairui", sync_mairui),
    ):
        try:
            results[key] = fn()
        except HTTPException as e:
            results[key] = {"ok": False, "detail": e.detail}
    ok = any(isinstance(v, dict) and v.get("ok") for v in results.values())
    _log(
        "all",
        ok,
        "multi-track sync attempted",
        {k: (v.get("ok") if isinstance(v, dict) else False) for k, v in results.items()},
    )
    return {"ok": ok, "results": results}
