"""麦蕊智数（mairui.club）第四轨：行业/概念树 + 成分。

文档：https://www.mairui.club/hsdata
鉴权：URL 路径中的 licence（写入 config.local.json，勿提交 Git）
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any
from urllib.parse import quote

import requests

from .. import config, db

SOURCE = "mairui"

# type2 → (kind, system)
_TYPE2_MAP = {
    0: ("sw", "sw"),          # 申万行业
    1: ("sw", "sw2"),         # 申万二级
    2: ("gn", "hot_gn"),      # 热门概念
    3: ("gn", "gn"),          # 概念板块
    4: ("dq", "dq"),          # 地域
    5: ("csrc", "csrc"),      # 证监会行业
}


def _cfg() -> dict:
    return dict(config.get().get("mairui") or {})


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "kfm_tdx_industry/1.0", "Accept": "application/json"})
    return s


def _base() -> str:
    return (_cfg().get("base_url") or "https://api.mairuiapi.com").rstrip("/")


def _licence() -> str:
    lic = (_cfg().get("licence") or _cfg().get("token") or "").strip()
    if not lic:
        raise RuntimeError("未配置 mairui.licence（请写入 config.local.json）")
    return lic


def _get(sess: requests.Session, path: str, timeout: float = 30) -> Any:
    url = f"{_base()}{path}"
    r = sess.get(url, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"麦蕊限流 429: {r.text[:200]}")
    r.raise_for_status()
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"麦蕊返回非 JSON: {r.text[:200]}") from e


def _parse_dm(dm: str, jys: str | None = None) -> tuple[str, str] | None:
    dm = (dm or "").strip()
    if not dm:
        return None
    if "." in dm:
        code, exch = dm.split(".", 1)
        m = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exch.upper())
        if m:
            return m, code.zfill(6)[-6:]
    if jys:
        m = {"SH": "sh", "SZ": "sz", "BJ": "bj", "sh": "sh", "sz": "sz", "bj": "bj"}.get(str(jys))
        if m:
            return m, dm.zfill(6)[-6:]
    code = dm.zfill(6)[-6:]
    if code.startswith(("5", "6", "9")):
        return "sh", code
    if code.startswith(("4", "8")):
        return "bj", code
    return "sz", code


def sync_from_mairui(
    include_concept: bool = True,
    include_region: bool = True,
    include_sw: bool = True,
    include_csrc: bool = True,
    max_concepts: int | None = 150,
    sleep: float = 0.12,
) -> dict[str, Any]:
    """拉取 hszg/list 树，对叶子调用 hszg/gg 取成分并入库。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    lic = _licence()
    sess = _session()
    stats: dict[str, Any] = {
        "engine": "mairui",
        "tree_nodes": 0,
        "leaves": 0,
        "sw_leaves": 0,
        "concept_leaves": 0,
        "region_leaves": 0,
        "csrc_leaves": 0,
        "industry_names": 0,
        "symbol_industry": 0,
        "blocks": 0,
        "block_member": 0,
        "errors": [],
    }

    tree = _get(sess, f"/hszg/list/{quote(lic, safe='')}")
    if not isinstance(tree, list):
        raise RuntimeError(f"行业树格式异常: {type(tree)}")
    stats["tree_nodes"] = len(tree)

    want_type2: set[int] = set()
    if include_sw:
        want_type2 |= {0, 1}
    if include_concept:
        want_type2 |= {2, 3}
    if include_region:
        want_type2 |= {4}
    if include_csrc:
        want_type2 |= {5}

    def _as_int(v, default: int = -1) -> int:
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    leaves = []
    for n in tree:
        # 注意 type1/type2 可能为 0，不能用 `v or default`（0 会被当成假值）
        if _as_int(n.get("isleaf"), 0) != 1:
            continue
        if _as_int(n.get("type1"), -1) != 0:  # 只要 A 股
            continue
        t2 = _as_int(n.get("type2"), -1)
        if t2 not in want_type2:
            continue
        code = str(n.get("code") or "").strip()
        name = str(n.get("name") or "").strip()
        if not code or not name:
            continue
        leaves.append((t2, code, name, n.get("pcode"), n.get("pname"), n.get("level")))

    # 概念限量（热门+概念合并后截断）
    if max_concepts is not None:
        gn = [x for x in leaves if x[0] in (2, 3)]
        other = [x for x in leaves if x[0] not in (2, 3)]
        leaves = other + gn[:max_concepts]

    stats["leaves"] = len(leaves)
    stats["sw_leaves"] = sum(1 for x in leaves if x[0] in (0, 1))
    stats["concept_leaves"] = sum(1 for x in leaves if x[0] in (2, 3))
    stats["region_leaves"] = sum(1 for x in leaves if x[0] == 4)
    stats["csrc_leaves"] = sum(1 for x in leaves if x[0] == 5)

    ind_rows: list[tuple] = []
    block_rows: list[tuple] = []
    member_rows: list[tuple] = []
    sym_sw: dict[tuple[str, str], tuple[str, str]] = {}  # -> (sw_code, sw_name)
    sym_main: dict[tuple[str, str], str] = {}  # display industry name

    for t2, code, name, pcode, pname, level in leaves:
        kind, system = _TYPE2_MAP[t2]
        short = name.split("-")[-1] if "-" in name else name
        ind_rows.append((system, code, short, str(pcode) if pcode else None, int(level or 1), SOURCE))
        block_rows.append((kind, code, short, code, SOURCE))
        try:
            members = _get(sess, f"/hszg/gg/{quote(code, safe='')}/{quote(lic, safe='')}")
            time.sleep(sleep)
        except Exception as e:
            stats["errors"].append(f"{code}: {e}")
            continue
        if not isinstance(members, list):
            continue
        for m in members:
            parsed = _parse_dm(str(m.get("dm") or ""), m.get("jys"))
            if not parsed:
                continue
            market, code6 = parsed
            # 只要像股票代码
            if not code6.isdigit():
                continue
            member_rows.append((kind, code, market, code6, SOURCE))
            if t2 in (0, 1):
                # 优先更深的申万（二级覆盖一级）
                prev = sym_sw.get((market, code6))
                if prev is None or t2 == 1:
                    sym_sw[(market, code6)] = (code, short)
                    sym_main[(market, code6)] = short
            elif (market, code6) not in sym_main and t2 == 5:
                sym_main[(market, code6)] = short

    now_iso = now
    sym_rows = []
    for key in sorted(set(sym_main) | set(sym_sw)):
        market, code6 = key
        sw_code, sw_name = sym_sw.get(key, (None, None))
        main = sym_main.get(key) or sw_name
        extra = {"mairui_sw_code": sw_code, "mairui_sw_name": sw_name, "mairui_industry": main}
        sym_rows.append(
            (
                market,
                code6,
                SOURCE,
                None,
                sw_code,
                main,
                json.dumps(extra, ensure_ascii=False),
                now_iso,
            )
        )

    uniq_blocks = {(r[0], r[1], r[4]): r for r in block_rows}
    uniq_members = {(r[0], r[1], r[2], r[3], r[4]): r for r in member_rows}
    stats["industry_names"] = len(ind_rows)
    stats["symbol_industry"] = len(sym_rows)
    stats["blocks"] = len(uniq_blocks)
    stats["block_member"] = len(uniq_members)

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
        if sym_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_industry(market, code, source, tdx_code, sw_code, em_industry, extra_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                sym_rows,
            )
        if uniq_blocks:
            conn.executemany(
                "INSERT OR REPLACE INTO block(kind, code, name, index_code, source) VALUES (?,?,?,?,?)",
                list(uniq_blocks.values()),
            )
        if uniq_members:
            conn.executemany(
                "INSERT OR REPLACE INTO block_member(kind, block_code, market, code, source) VALUES (?,?,?,?,?)",
                list(uniq_members.values()),
            )

    return stats
