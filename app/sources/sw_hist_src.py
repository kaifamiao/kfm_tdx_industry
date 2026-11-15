"""申万个股行业分类变动历史（公开 xls/rar，不依赖 AKShare）。

来源：
- 归属历史：https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls
- 代码字典：https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClass.rar → SwClassCode_2021.xls
"""
from __future__ import annotations

import datetime as dt
import io
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .. import config, db

SOURCE = "sw_hist"
_HIST_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_RAR_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClass.rar"


def _code_to_market(code: str) -> tuple[str, str]:
    c = str(code).zfill(6)[-6:]
    if c.startswith(("5", "6", "9")):
        return "sh", c
    if c.startswith(("4", "8")):
        return "bj", c
    return "sz", c


def _pick_col(df: pd.DataFrame, *cands: str):
    cols = {str(c).strip().lower(): c for c in df.columns}
    for name in cands:
        if name.lower() in cols:
            return cols[name.lower()]
    for c in df.columns:
        cl = str(c).lower()
        for name in cands:
            if name.lower() in cl:
                return c
    return None


def _session() -> requests.Session:
    sess = requests.Session()
    sess.trust_env = False
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.swsresearch.com/",
        }
    )
    return sess


def _read_excel_bytes(content: bytes) -> pd.DataFrame:
    bio = io.BytesIO(content)
    last_err = None
    for engine in ("xlrd", "openpyxl", None):
        try:
            bio.seek(0)
            if engine:
                return pd.read_excel(bio, engine=engine)
            return pd.read_excel(bio)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"无法解析 Excel: {last_err}")


def _read_excel_path(path: Path) -> pd.DataFrame:
    last_err = None
    for engine in ("xlrd", "openpyxl", None):
        try:
            if engine:
                return pd.read_excel(path, engine=engine)
            return pd.read_excel(path)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"无法解析 {path.name}: {last_err}")


def _norm_ind_code(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit():
        s = s.zfill(6)
    return s


def _display_name(l1, l2, l3) -> str | None:
    for v in (l3, l2, l1):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if s and s.lower() != "nan":
            return s
    return None


def _load_sw_code_dict(sess: requests.Session) -> dict[str, str]:
    """下载 SwClass.rar，解压 SwClassCode_2021.xls → {行业代码: 名称}。"""
    names: dict[str, str] = {}
    cache = config.ROOT / "data" / "cache" / "swclass"
    cache.mkdir(parents=True, exist_ok=True)
    rar_path = cache / "SwClass.rar"
    code_xls = cache / "SwClassCode_2021.xls"

    need_dl = not rar_path.is_file() or rar_path.stat().st_size < 100_000
    if need_dl:
        r = sess.get(_RAR_URL, timeout=180, verify=False)
        r.raise_for_status()
        rar_path.write_bytes(r.content)

    if not code_xls.is_file():
        # Windows 自带 tar 可解 rar；失败则跳过字典
        try:
            subprocess.run(
                ["tar", "-xf", str(rar_path), "-C", str(cache)],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception as e:
            # 尝试系统 tar 失败时不阻断历史写入
            print(f"[sw_hist] 解压 SwClass.rar 失败: {e}")
            return names

    if not code_xls.is_file():
        # 解压到子目录的情况
        found = list(cache.rglob("SwClassCode_2021.xls"))
        if found:
            code_xls = found[0]
        else:
            return names

    df = _read_excel_path(code_xls)
    c_code = _pick_col(df, "行业代码", "code", "industry_code")
    c_l1 = _pick_col(df, "一级行业名称", "一级行业", "l1")
    c_l2 = _pick_col(df, "二级行业名称", "二级行业", "l2")
    c_l3 = _pick_col(df, "三级行业名称", "三级行业", "l3")
    if not c_code:
        return names
    for _, row in df.iterrows():
        code = _norm_ind_code(row[c_code])
        if not code:
            continue
        name = _display_name(
            row[c_l1] if c_l1 else None,
            row[c_l2] if c_l2 else None,
            row[c_l3] if c_l3 else None,
        )
        if name:
            names[code] = name
    return names


def sync_sw_industry_history() -> dict[str, Any]:
    """下载申万分类变动表 → symbol_industry_hist，并补 end_date / 行业名。"""
    sess = _session()
    r = sess.get(_HIST_URL, timeout=120, verify=False)
    r.raise_for_status()
    df = _read_excel_bytes(r.content)
    if df is None or df.empty:
        raise RuntimeError("申万行业历史 xls 为空")

    c_sym = _pick_col(df, "股票代码", "证券代码", "symbol", "code", "代码")
    c_start = _pick_col(df, "计入日期", "开始日期", "start_date", "生效日期")
    c_ind = _pick_col(df, "行业代码", "申万行业代码", "industry_code", "三级行业代码")
    c_upd = _pick_col(df, "更新日期", "update_time", "更新时间")
    c_name = _pick_col(df, "行业名称", "三级行业名称", "industry_name", "名称")
    if not c_sym or not c_start or not c_ind:
        raise RuntimeError(f"申万表列无法识别: {list(df.columns)}")

    code_names = _load_sw_code_dict(sess)

    now = dt.datetime.now().isoformat(timespec="seconds")
    rows_raw = []
    for _, r0 in df.iterrows():
        sym = str(r0[c_sym]).strip()
        if not sym or sym.lower() == "nan":
            continue
        if "." in sym:
            sym = sym.split(".", 1)[0]
        if not sym.isdigit():
            continue
        market, code = _code_to_market(sym)
        start = pd.to_datetime(r0[c_start], errors="coerce")
        if pd.isna(start):
            continue
        start_s = start.strftime("%Y-%m-%d")
        ind = _norm_ind_code(r0[c_ind])
        if not ind:
            continue
        name = None
        if c_name is not None:
            nv = r0[c_name]
            if nv is not None and str(nv).strip() and str(nv).lower() != "nan":
                name = str(nv).strip()
        upd = None
        if c_upd is not None:
            u = pd.to_datetime(r0[c_upd], errors="coerce")
            if not pd.isna(u):
                upd = u.strftime("%Y-%m-%d")
        rows_raw.append((market, code, start_s, ind, name, upd))

    uniq: dict[tuple, tuple] = {}
    for market, code, start_s, ind, name, upd in rows_raw:
        uniq[(market, code, start_s)] = (market, code, start_s, ind, name, upd)
    ordered = sorted(uniq.values(), key=lambda x: (x[0], x[1], x[2]))

    by_sym: dict[tuple[str, str], list] = {}
    for row in ordered:
        by_sym.setdefault((row[0], row[1]), []).append(list(row))

    payload = []
    for (market, code), segs in by_sym.items():
        segs.sort(key=lambda x: x[2])
        for i, seg in enumerate(segs):
            end = None
            if i + 1 < len(segs):
                nxt = dt.datetime.strptime(segs[i + 1][2], "%Y-%m-%d").date()
                end = (nxt - dt.timedelta(days=1)).isoformat()
            _m, _c, start_s, ind, name, upd = seg
            payload.append(
                (market, code, SOURCE, "sw", ind, name, start_s, end, upd, now)
            )

    with db.session() as conn:
        conn.execute("DELETE FROM symbol_industry_hist WHERE source=?", (SOURCE,))
        name_map = {
            r["code"]: r["name"]
            for r in conn.execute("SELECT code, name FROM industry WHERE system='sw'")
        }
        name_map.update(code_names)

        # 同步写入行业字典（source=sw_hist）
        if code_names:
            ind_rows = [
                ("sw", code, name, None, _level_of(code), SOURCE)
                for code, name in code_names.items()
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO industry(system, code, name, parent_code, level, source)
                VALUES (?,?,?,?,?,?)
                """,
                ind_rows,
            )

        fixed = []
        for market, code, source, system, ind, name, start_s, end, upd, ts in payload:
            if not name:
                name = name_map.get(ind) or name_map.get(ind.lstrip("0") if ind.isdigit() else ind)
            fixed.append((market, code, source, system, ind, name, start_s, end, upd, ts))
        if fixed:
            conn.executemany(
                """
                INSERT OR REPLACE INTO symbol_industry_hist
                (market, code, source, system, industry_code, industry_name, start_date, end_date, update_time, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                fixed,
            )

    return {
        "engine": "sw_hist_xls",
        "url": _HIST_URL,
        "dict_url": _RAR_URL,
        "raw_rows": len(rows_raw),
        "hist_rows": len(payload),
        "symbols": len(by_sym),
        "industry_names": len(code_names),
    }


def _level_of(code: str) -> int:
    """申万 6 位码：xx0000=L1, xxxx00=L2, 其余 L3。"""
    c = code.zfill(6)
    if c.endswith("0000"):
        return 1
    if c.endswith("00"):
        return 2
    return 3
