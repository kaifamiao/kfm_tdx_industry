"""Tushare 第三轨：行业 / 概念入库。"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

from .. import config, db

SOURCE = "tushare"


def _client():
    try:
        import tushare as ts
    except ImportError as e:
        raise RuntimeError("未安装 tushare，请 pip install tushare") from e

    cfg = config.tushare_cfg()
    token = (cfg.get("token") or "").strip()
    if not token:
        raise RuntimeError("未配置 tushare.token（请写入 config.local.json）")
    pro = ts.pro_api(token)
    url = (cfg.get("http_url") or "").strip()
    if url:
        pro._DataApi__http_url = url
    return pro


def _ts_to_market_code(ts_code: str) -> tuple[str, str] | None:
    if not ts_code or "." not in ts_code:
        return None
    code, exch = ts_code.strip().upper().split(".", 1)
    m = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exch)
    if not m:
        return None
    return m, code.zfill(6)[-6:]


def _safe_call(pro, name: str, **kwargs):
    fn = getattr(pro, name, None)
    if fn is None:
        raise AttributeError(f"tushare 无接口 {name}")
    return fn(**kwargs)


def sync_from_tushare(sleep: float = 0.35) -> dict[str, Any]:
    """拉取：
    1) stock_basic.industry → 东财/通达信粗分行业名（记入 em_industry + industry 字典）
    2) index_classify(SW) → 申万行业字典
    3) 若有权限：index_member / concept_detail 成分（失败则跳过）
    """
    pro = _client()
    now = dt.datetime.now().isoformat(timespec="seconds")
    stats = {
        "stock_basic": 0,
        "industry_names": 0,
        "symbol_industry": 0,
        "sw_classify": 0,
        "blocks": 0,
        "members": 0,
        "skipped": [],
        "errors": [],
    }

    # ---- 1) 个股基础 + industry 字段 ----
    df = _safe_call(
        pro,
        "stock_basic",
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,area,industry,market,list_date",
    )
    time.sleep(sleep)
    industries: dict[str, str] = {}
    sym_rows: list[tuple] = []
    if df is not None and len(df):
        stats["stock_basic"] = int(len(df))
        for row in df.itertuples(index=False):
            ts_code = getattr(row, "ts_code", None)
            parsed = _ts_to_market_code(str(ts_code) if ts_code is not None else "")
            if not parsed:
                continue
            market, code = parsed
            ind = getattr(row, "industry", None)
            ind_name = str(ind).strip() if ind is not None and str(ind).strip() and str(ind) != "nan" else None
            if ind_name:
                industries[ind_name] = ind_name
            area = getattr(row, "area", None)
            extra = None
            if area is not None and str(area) not in ("", "nan"):
                import json
                extra = json.dumps({"area": str(area), "name": getattr(row, "name", None)}, ensure_ascii=False)
            sym_rows.append((market, code, SOURCE, None, None, ind_name, extra, now))

    # ---- 2) 申万行业分类 ----
    sw_rows: list[tuple] = []
    sw_blocks: list[tuple] = []
    for level, lv in (("L1", 1), ("L2", 2), ("L3", 3)):
        try:
            cdf = _safe_call(pro, "index_classify", level=level, src="SW2021")
            time.sleep(sleep)
        except Exception:
            try:
                cdf = _safe_call(pro, "index_classify", level=level, src="SW")
                time.sleep(sleep)
            except Exception as e:
                stats["errors"].append(f"index_classify {level}: {e}")
                continue
        if cdf is None or not len(cdf):
            continue
        cols = {c.lower(): c for c in cdf.columns}
        code_col = cols.get("index_code") or cols.get("industry_code") or list(cdf.columns)[0]
        name_col = cols.get("industry_name") or cols.get("name") or list(cdf.columns)[1]
        parent_col = cols.get("parent_code")
        for _, r in cdf.iterrows():
            code = str(r[code_col]).strip()
            name = str(r[name_col]).strip()
            parent = str(r[parent_col]).strip() if parent_col and r.get(parent_col) == r.get(parent_col) else None
            if parent in ("", "nan", "None"):
                parent = None
            sw_rows.append(("sw", code, name, parent, lv, SOURCE))
            sw_blocks.append(("sw", code, name, code, SOURCE))
        stats["sw_classify"] += int(len(cdf))

    # ---- 3) 概念列表（可选）----
    concept_blocks: list[tuple] = []
    concept_members: list[tuple] = []
    try:
        cdf = _safe_call(pro, "concept")
        time.sleep(sleep)
        if cdf is not None and len(cdf):
            cols = {c.lower(): c for c in cdf.columns}
            code_col = cols.get("code") or cols.get("id") or list(cdf.columns)[0]
            name_col = cols.get("name") or list(cdf.columns)[1]
            for _, r in cdf.iterrows():
                ccode = str(r[code_col]).strip()
                cname = str(r[name_col]).strip()
                concept_blocks.append(("gn", ccode, cname, None, SOURCE))
            stats["blocks"] += len(concept_blocks)
            # 成分：逐个拉可能很慢/耗积分，限制前 N 或跳过细节
            # 先只入库概念字典；成分按需可后续扩展
    except Exception as e:
        stats["skipped"].append(f"concept: {e}")

    # ---- 写入 ----
    ind_rows = [("em", name, name, None, 1, SOURCE) for name in sorted(industries)]
    stats["industry_names"] = len(ind_rows)
    stats["symbol_industry"] = len(sym_rows)
    stats["blocks"] = len(sw_blocks) + len(concept_blocks)

    with db.session() as conn:
        conn.execute("DELETE FROM industry WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM symbol_industry WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM block WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM block_member WHERE source=?", (SOURCE,))
        if ind_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO industry(system, code, name, parent_code, level, source) VALUES (?,?,?,?,?,?)",
                ind_rows,
            )
        if sw_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO industry(system, code, name, parent_code, level, source) VALUES (?,?,?,?,?,?)",
                sw_rows,
            )
        if sym_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_industry(market, code, source, tdx_code, sw_code, em_industry, extra_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                sym_rows,
            )
        if sw_blocks or concept_blocks:
            conn.executemany(
                "INSERT OR REPLACE INTO block(kind, code, name, index_code, source) VALUES (?,?,?,?,?)",
                sw_blocks + concept_blocks,
            )
        if concept_members:
            conn.executemany(
                "INSERT OR REPLACE INTO block_member(kind, block_code, market, code, source) VALUES (?,?,?,?,?)",
                concept_members,
            )

    return stats
