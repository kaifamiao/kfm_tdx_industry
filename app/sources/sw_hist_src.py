"""申万个股行业分类变动历史（公开 xls/rar，不依赖 AKShare）。

版本策略
--------
- sw2014：通达信 incon #SWHY + 历史表中仅旧版出现的代码（名称能解析则入库）
- sw2021：SwClass.rar / SwClassCode_2021.xls
- 两版字典写入 industry_dict，互不覆盖；不再用新版覆盖 industry 旧源

日期语义
--------
- start_date / end_date：行业归属生效区间（计入日起，至下一段前一日；末段 end 空=至今）
- update_time：数据源「更新日期」= 发布/修订可知日（回测应用 known_at，勿用未来信息）
- synced_at：本系统入库时间
"""
from __future__ import annotations

import datetime as dt
import io
import re
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .. import config, db
from ..parsers.tdx_files import parse_incon

SOURCE = "sw_hist"
SYSTEM = "sw"
VER_2014 = "sw2014"
VER_2021 = "sw2021"
# 申万 2021 版正式启用日（官网/说明：2021-07-30）
SW2021_EFFECTIVE = "2021-07-30"
SW2014_EFFECTIVE_FROM = "2014-01-01"

_HIST_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_RAR_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClass.rar"
# 公开整理的申万 2014 代码表（补 incon 未覆盖的旧三级码）
_SW2014_CSV_URL = (
    "https://raw.githubusercontent.com/ramwin/china-public-data/master/sw.csv"
)


def _code_to_market(code: str) -> tuple[str, str]:
    """证券代码 → (market, code)。920xxx 为北交所，不能按 9→sh。"""
    c = str(code).zfill(6)[-6:]
    if c.startswith("920") or c.startswith(("43", "83", "87")):
        return "bj", c
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


def _level_of(code: str) -> int:
    c = code.zfill(6)
    if c.endswith("0000"):
        return 1
    if c.endswith("00"):
        return 2
    return 3


def _parent_of(code: str) -> str | None:
    c = code.zfill(6)
    lv = _level_of(c)
    if lv == 1:
        return None
    if lv == 2:
        return c[:2] + "0000"
    return c[:4] + "00"


def _cache_dir() -> Path:
    p = config.ROOT / "data" / "cache" / "swclass"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_rar(sess: requests.Session) -> Path:
    cache = _cache_dir()
    rar_path = cache / "SwClass.rar"
    if not rar_path.is_file() or rar_path.stat().st_size < 100_000:
        r = sess.get(_RAR_URL, timeout=180, verify=False)
        r.raise_for_status()
        rar_path.write_bytes(r.content)
    code_xls = cache / "SwClassCode_2021.xls"
    if not code_xls.is_file():
        try:
            subprocess.run(
                ["tar", "-xf", str(rar_path), "-C", str(cache)],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception as e:
            print(f"[sw_hist] 解压 SwClass.rar 失败: {e}")
    return rar_path


def _load_sw2021_dict(sess: requests.Session) -> dict[str, dict]:
    """{code: {name, level, parent}} from SwClassCode_2021.xls。"""
    _ensure_rar(sess)
    cache = _cache_dir()
    code_xls = cache / "SwClassCode_2021.xls"
    if not code_xls.is_file():
        found = list(cache.rglob("SwClassCode_2021.xls"))
        if not found:
            return {}
        code_xls = found[0]
    df = _read_excel_path(code_xls)
    c_code = _pick_col(df, "行业代码", "code", "industry_code")
    c_l1 = _pick_col(df, "一级行业名称", "一级行业", "l1")
    c_l2 = _pick_col(df, "二级行业名称", "二级行业", "l2")
    c_l3 = _pick_col(df, "三级行业名称", "三级行业", "l3")
    out: dict[str, dict] = {}
    if not c_code:
        return out
    for _, row in df.iterrows():
        code = _norm_ind_code(row[c_code])
        if not code:
            continue
        name = _display_name(
            row[c_l1] if c_l1 else None,
            row[c_l2] if c_l2 else None,
            row[c_l3] if c_l3 else None,
        )
        out[code] = {
            "name": name,
            "level": _level_of(code),
            "parent": _parent_of(code),
        }
    return out


def _load_sw2014_from_incon() -> dict[str, dict]:
    """通达信 incon #SWHY → 近似 SW2014 字典。"""
    path = config.path("tdx_files_dir") / "incon.dat"
    out: dict[str, dict] = {}
    for system, code, name, parent, level, _src in parse_incon(path):
        if system != "sw":
            continue
        c = _norm_ind_code(code)
        if not c:
            continue
        out[c] = {"name": name, "level": level or _level_of(c), "parent": parent or _parent_of(c)}
    return out


def _load_sw2014_from_csv(sess: requests.Session) -> dict[str, dict]:
    """公开 SW2014 代码表：仅用于补齐 incon 未覆盖的旧码，不覆盖已有名称。"""
    ref_dir = config.ROOT / "data" / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    csv_path = ref_dir / "sw2014.csv"
    if not csv_path.is_file() or csv_path.stat().st_size < 1000:
        try:
            r = sess.get(_SW2014_CSV_URL, timeout=60)
            r.raise_for_status()
            csv_path.write_bytes(r.content)
        except Exception as e:
            print(f"[sw_hist] 下载 sw2014.csv 失败: {e}")
            if not csv_path.is_file():
                return {}
    out: dict[str, dict] = {}
    try:
        # 上游为空白分隔：code name index_code
        df = pd.read_csv(csv_path, sep=r"\s+", engine="python")
    except Exception as e:
        print(f"[sw_hist] 解析 sw2014.csv 失败: {e}")
        return out
    if len(df.columns) == 1:
        # 容错：整行未分列时按空白拆
        col0 = df.columns[0]
        parts = df[col0].astype(str).str.split(r"\s+", n=2, expand=True)
        parts.columns = ["code", "name", "index_code"][: parts.shape[1]]
        df = parts
    c_code = _pick_col(df, "code", "行业代码", "industry_code")
    c_name = _pick_col(df, "name", "行业名称", "industry_name")
    if not c_code or not c_name:
        print(f"[sw_hist] sw2014.csv 列无法识别: {list(df.columns)}")
        return out
    for _, row in df.iterrows():
        code = _norm_ind_code(row[c_code])
        if not code:
            continue
        name = str(row[c_name]).strip() if row[c_name] is not None else ""
        if not name or name.lower() == "nan":
            continue
        out[code] = {"name": name, "level": _level_of(code), "parent": _parent_of(code)}
    return out


def _merge_dict(base: dict[str, dict], extra: dict[str, dict]) -> dict[str, dict]:
    """合并字典：base 优先，extra 只补缺失 code（不覆盖旧版名称）。"""
    out = dict(base)
    for code, meta in extra.items():
        if code not in out:
            out[code] = meta
        elif not out[code].get("name") and meta.get("name"):
            out[code] = {**out[code], "name": meta["name"]}
    return out


def _extract_sw2014_from_pdf() -> dict[str, dict]:
    """从 SwClassStd2021.pdf 修订对照表中尽量抠出旧版代码+名称（无 PDF 库时用字节启发式）。"""
    cache = _cache_dir()
    pdfs = list(cache.rglob("SwClassStd2021.pdf"))
    if not pdfs:
        return {}
    raw = pdfs[0].read_bytes()
    out: dict[str, dict] = {}
    # PDF 中常见：中文名 + 空白 + 6位行业码
    for enc in ("utf-8", "gbk", "latin1"):
        try:
            text = raw.decode(enc, errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"([\u4e00-\u9fffⅡⅢA-Za-z0-9（）()、/+\-]{2,20})\s*([1-9]\d{5})", text):
            name, code = m.group(1).strip(), m.group(2)
            # 过滤明显非行业的噪声
            if any(x in name for x in ("http", "www", "Page", "页")):
                continue
            if code not in out:
                out[code] = {"name": name, "level": _level_of(code), "parent": _parent_of(code)}
    return out


def _assign_taxonomy_version(
    ind: str,
    start_date: str,
    d2014: dict[str, dict],
    d2021: dict[str, dict],
) -> str:
    in14, in21 = ind in d2014, ind in d2021
    if in14 and not in21:
        return VER_2014
    if in21 and not in14:
        return VER_2021
    if in14 and in21:
        return VER_2014 if start_date < SW2021_EFFECTIVE else VER_2021
    # 字典皆无：按生效日归属版本（名称可能仍空，留给 QA）
    return VER_2014 if start_date < SW2021_EFFECTIVE else VER_2021


def _upsert_industry_dict(
    conn,
    version: str,
    items: dict[str, dict],
    source: str,
    effective_from: str | None,
    effective_to: str | None,
    synced_at: str,
):
    rows = []
    for code, meta in items.items():
        rows.append(
            (
                SYSTEM,
                version,
                code,
                meta.get("name"),
                meta.get("parent"),
                meta.get("level") or _level_of(code),
                effective_from,
                effective_to,
                source,
                synced_at,
            )
        )
    if not rows:
        return
    # 同版本整批替换该 source，避免残缺；不同 version 互不影响
    conn.execute(
        "DELETE FROM industry_dict WHERE system=? AND version=? AND source=?",
        (SYSTEM, version, source),
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO industry_dict
        (system, version, code, name, parent_code, level, effective_from, effective_to, source, synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _classify_missing_symbols(conn) -> dict[str, Any]:
    """symbol_industry 有、hist 无的证券：按原因分桶（不回填历史）。"""
    si = {
        (r["market"], r["code"])
        for r in conn.execute("SELECT DISTINCT market, code FROM symbol_industry")
    }
    hist = {
        (r["market"], r["code"])
        for r in conn.execute(
            "SELECT DISTINCT market, code FROM symbol_industry_hist WHERE source=?",
            (SOURCE,),
        )
    }
    hist_by_code = {c for _m, c in hist}
    missing = sorted(si - hist)
    buckets: dict[str, list[dict]] = {
        "market_mismatch": [],  # 同代码在 hist 其他市场
        "b_share_sh": [],
        "b_share_sz": [],
        "bj_only": [],
        "star_board": [],
        "new_or_source_gap": [],
        "other": [],
    }
    for market, code in missing:
        item = {"market": market, "code": code}
        if code in hist_by_code:
            buckets["market_mismatch"].append(item)
        elif code.startswith("900"):
            buckets["b_share_sh"].append(item)
        elif code.startswith("200"):
            buckets["b_share_sz"].append(item)
        elif market == "bj" or code.startswith(("920", "43", "83", "87")):
            buckets["bj_only"].append(item)
        elif code.startswith("688"):
            buckets["star_board"].append(item)
        elif market in ("sh", "sz") and code[0] in "036":
            buckets["new_or_source_gap"].append(item)
        else:
            buckets["other"].append(item)

    summary = {k: len(v) for k, v in buckets.items()}
    summary["total"] = len(missing)
    return {
        "summary": summary,
        "samples": {k: v[:20] for k, v in buckets.items()},
        "note": (
            "不把当前行业回填到全部历史日期；"
            "B 股/部分北交所不在申万公开历史表属预期；"
            "market_mismatch 已通过 920→bj 修正后应下降。"
        ),
    }


def sync_sw_industry_history() -> dict[str, Any]:
    """下载申万分类变动表 → symbol_industry_hist + 版本化 industry_dict。"""
    db.init_schema()
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

    d2021 = _load_sw2021_dict(sess)
    # 旧版：incon 优先，csv/pdf 只补缺口，绝不覆盖已有名称，也不写入 sw2021
    d2014 = _load_sw2014_from_incon()
    d2014 = _merge_dict(d2014, _load_sw2014_from_csv(sess))
    d2014 = _merge_dict(d2014, _extract_sw2014_from_pdf())

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
        published = None
        if c_upd is not None:
            u = pd.to_datetime(r0[c_upd], errors="coerce")
            if not pd.isna(u):
                published = u.strftime("%Y-%m-%d")
        rows_raw.append((market, code, start_s, ind, name, published))

    uniq: dict[tuple, tuple] = {}
    for market, code, start_s, ind, name, published in rows_raw:
        uniq[(market, code, start_s)] = (market, code, start_s, ind, name, published)
    ordered = sorted(uniq.values(), key=lambda x: (x[0], x[1], x[2]))

    by_sym: dict[tuple[str, str], list] = {}
    for row in ordered:
        by_sym.setdefault((row[0], row[1]), []).append(list(row))

    # 历史中出现的代码：保证进入对应版本字典（有名则写入；无名也占位，满足「能关联」）
    for market, code, start_s, ind, name, published in ordered:
        ver = _assign_taxonomy_version(ind, start_s, d2014, d2021)
        target = d2014 if ver == VER_2014 else d2021
        if ind not in target:
            target[ind] = {
                "name": name,
                "level": _level_of(ind),
                "parent": _parent_of(ind),
            }
        elif name and not target[ind].get("name"):
            target[ind]["name"] = name

    def _resolve_name(ind: str, ver: str, inline: str | None) -> str | None:
        if inline:
            return inline
        target = d2014 if ver == VER_2014 else d2021
        meta = target.get(ind) or {}
        if meta.get("name"):
            return meta["name"]
        # 同版本父级名称兜底（仍属该版字典，非跨版覆盖）
        parent = meta.get("parent") or _parent_of(ind)
        if parent and parent in target and target[parent].get("name"):
            return target[parent]["name"]
        return None

    payload = []
    unresolved = 0
    for (market, code), segs in by_sym.items():
        segs.sort(key=lambda x: x[2])
        for i, seg in enumerate(segs):
            end = None
            if i + 1 < len(segs):
                nxt = dt.datetime.strptime(segs[i + 1][2], "%Y-%m-%d").date()
                end = (nxt - dt.timedelta(days=1)).isoformat()
            _m, _c, start_s, ind, name, published = seg
            ver = _assign_taxonomy_version(ind, start_s, d2014, d2021)
            final_name = _resolve_name(ind, ver, name)
            if not final_name:
                unresolved += 1
            payload.append(
                (
                    market,
                    code,
                    SOURCE,
                    SYSTEM,
                    ind,
                    final_name,
                    start_s,
                    end,
                    published,  # update_time = 发布/修订日
                    now,
                    ver,
                )
            )

    coverage = None
    with db.session() as conn:
        # 版本化字典：旧新并存
        _upsert_industry_dict(
            conn,
            VER_2014,
            d2014,
            source="tdx_incon+sw2014_csv",
            effective_from=SW2014_EFFECTIVE_FROM,
            effective_to="2021-07-29",
            synced_at=now,
        )
        _upsert_industry_dict(
            conn,
            VER_2021,
            d2021,
            source="sw_class_2021_xls",
            effective_from=SW2021_EFFECTIVE,
            effective_to=None,
            synced_at=now,
        )

        # 兼容旧 industry 表：仅写入 sw2021 到 source=sw_hist，不碰 tdx_file
        if d2021:
            conn.execute("DELETE FROM industry WHERE system=? AND source=?", (SYSTEM, SOURCE))
            conn.executemany(
                """
                INSERT OR REPLACE INTO industry(system, code, name, parent_code, level, source)
                VALUES (?,?,?,?,?,?)
                """,
                [
                    (SYSTEM, code, meta.get("name"), meta.get("parent"), meta.get("level"), SOURCE)
                    for code, meta in d2021.items()
                ],
            )

        conn.execute("DELETE FROM symbol_industry_hist WHERE source=?", (SOURCE,))
        if payload:
            conn.executemany(
                """
                INSERT OR REPLACE INTO symbol_industry_hist
                (market, code, source, system, industry_code, industry_name,
                 start_date, end_date, update_time, synced_at, taxonomy_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
        coverage = _classify_missing_symbols(conn)

    linked = len(payload) - unresolved
    return {
        "engine": "sw_hist_xls",
        "url": _HIST_URL,
        "dict_url": _RAR_URL,
        "raw_rows": len(rows_raw),
        "hist_rows": len(payload),
        "symbols": len(by_sym),
        "dict_sw2014": len(d2014),
        "dict_sw2021": len(d2021),
        "name_resolved": linked,
        "name_unresolved": unresolved,
        "sw2021_effective": SW2021_EFFECTIVE,
        "date_semantics": {
            "start_date": "行业归属生效起（计入日期）",
            "end_date": "行业归属生效止（下一段 start-1；空=至今）",
            "update_time": "数据源发布/修订日（回测 known_as-of 用此字段，勿用 synced_at）",
            "synced_at": "本系统同步入库时间",
        },
        "coverage_gap": coverage,
    }
