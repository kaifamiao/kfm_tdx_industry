"""网络轨：新浪板块（主）+ 腾讯行情（辅）。不使用 AKShare / 东财。"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

import requests

from .. import db

SOURCE = "network"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SINA_NODES = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes"
_SINA_NODE_DATA = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_TENCENT_Q = "https://qt.gtimg.cn/q="


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"})
    return s


def _code_to_market(code: str, symbol: str | None = None) -> tuple[str, str]:
    if symbol:
        s = symbol.lower()
        if s.startswith(("sh", "sz", "bj")) and len(s) >= 8:
            return s[:2], s[2:8]
    c = str(code).zfill(6)[-6:]
    if c.startswith(("5", "6", "9")):
        return "sh", c
    if c.startswith(("4", "8")):
        return "bj", c
    return "sz", c


def _walk_leaves(node, path: list[str] | None = None, out: list[tuple[str, str, str]] | None = None):
    """收集叶子: (path, name, node_code)。叶子形如 [名称, '', 'new_xxx']。"""
    if out is None:
        out = []
    if path is None:
        path = []
    if not isinstance(node, list) or not node:
        return out
    if isinstance(node[0], str):
        name = node[0]
        cur = path + [name]
        # 叶子：第 3 项为 node 代码
        if len(node) >= 3 and isinstance(node[2], str) and node[2] and not isinstance(node[1], list):
            out.append(("/".join(cur), name, node[2]))
        for x in node[1:]:
            if isinstance(x, list):
                if x and isinstance(x[0], list):
                    for c in x:
                        _walk_leaves(c, cur, out)
                else:
                    _walk_leaves(x, cur, out)
    else:
        for x in node:
            _walk_leaves(x, path, out)
    return out


def _filter_section(leaves: list[tuple[str, str, str]], *keys: str) -> list[tuple[str, str]]:
    """返回 [(板块名, node_code), ...]。"""
    out = []
    for path, name, code in leaves:
        if any(k in path for k in keys):
            # 只要该段下的叶子，排除段名自身误匹配
            if name in keys:
                continue
            out.append((name, code))
    # 去重保序
    seen = set()
    uniq = []
    for name, code in out:
        if code in seen:
            continue
        seen.add(code)
        uniq.append((name, code))
    return uniq


def _fetch_node_members(sess: requests.Session, node: str, page_size: int = 80) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        r = sess.get(
            _SINA_NODE_DATA,
            params={
                "page": page,
                "num": page_size,
                "sort": "symbol",
                "asc": 1,
                "node": node,
                "symbol": "",
                "_s_r_a": "page",
            },
            timeout=25,
        )
        r.raise_for_status()
        text = r.text.strip()
        if not text or text == "null":
            break
        try:
            data = r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        if len(data) < page_size:
            break
        page += 1
        time.sleep(0.12)
    return rows


def _tencent_names(sess: requests.Session, symbols: list[tuple[str, str]], chunk: int = 40) -> dict[tuple[str, str], str]:
    """批量腾讯行情取名称。symbols: [(market, code), ...]。"""
    out: dict[tuple[str, str], str] = {}
    if not symbols:
        return out
    # 腾讯用独立 referer
    headers = {"User-Agent": _UA, "Referer": "https://finance.qq.com"}
    for i in range(0, len(symbols), chunk):
        part = symbols[i : i + chunk]
        q = ",".join(f"{m}{c}" for m, c in part)
        try:
            r = sess.get(_TENCENT_Q + q, headers=headers, timeout=20)
            r.raise_for_status()
            # v_sh600519="1~贵州茅台~600519~...
            for line in r.text.split(";"):
                line = line.strip()
                if not line or '="' not in line:
                    continue
                left, right = line.split('="', 1)
                payload = right.strip().strip('"')
                if not payload:
                    continue
                fields = payload.split("~")
                if len(fields) < 3:
                    continue
                name, code = fields[1], fields[2].zfill(6)[-6:]
                key = left.split("v_")[-1]  # sh600519
                if len(key) >= 8:
                    market = key[:2]
                    out[(market, code)] = name
        except Exception:
            pass
        time.sleep(0.08)
    return out


def sync_from_network(
    include_concept: bool = True,
    include_region: bool = True,
    include_sw: bool = True,
    include_tencent_names: bool = True,
    sleep: float = 0.15,
    max_concepts: int | None = 120,
) -> dict[str, Any]:
    """新浪：行业 / 申万 / 概念 / 地域 → 入库；腾讯：补股票名称到 extra_json。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    stats: dict[str, Any] = {
        "engine": "sina+tencent",
        "industry_boards": 0,
        "sw_boards": 0,
        "concept_boards": 0,
        "region_boards": 0,
        "industry_names": 0,
        "symbol_industry": 0,
        "blocks": 0,
        "block_member": 0,
        "tencent_names": 0,
        "errors": [],
    }

    sess = _session()
    raw = sess.get(_SINA_NODES, timeout=30)
    raw.raise_for_status()
    leaves = _walk_leaves(raw.json())

    sina_hy = _filter_section(leaves, "/新浪行业/")
    # 路径含「行情中心/A股/新浪行业/xxx」
    if not sina_hy:
        sina_hy = [(n, c) for p, n, c in leaves if "/新浪行业/" in p]
    sina_sw = _filter_section(leaves, "/申万行业/") if include_sw else []
    if include_sw and not sina_sw:
        sina_sw = [(n, c) for p, n, c in leaves if "/申万行业/" in p]
    # 概念：优先「概念板块」，再补「热门概念」
    sina_gn: list[tuple[str, str]] = []
    if include_concept:
        sina_gn = [(n, c) for p, n, c in leaves if "/概念板块/" in p]
        hot = [(n, c) for p, n, c in leaves if "/热门概念/" in p]
        seen = {c for _, c in sina_gn}
        for n, c in hot:
            if c not in seen:
                sina_gn.append((n, c))
                seen.add(c)
        if max_concepts is not None:
            sina_gn = sina_gn[:max_concepts]
    sina_dq = [(n, c) for p, n, c in leaves if "/地域板块/" in p] if include_region else []

    stats["industry_boards"] = len(sina_hy)
    stats["sw_boards"] = len(sina_sw)
    stats["concept_boards"] = len(sina_gn)
    stats["region_boards"] = len(sina_dq)

    if not sina_hy:
        raise RuntimeError("未解析到新浪行业节点，请检查网络或接口变更")

    ind_rows: list[tuple] = []
    block_rows: list[tuple] = []
    member_rows: list[tuple] = []
    # 主行业归属：新浪行业
    sym_sina: dict[tuple[str, str], str] = {}
    # 申万名称（新浪口径）
    sym_sw: dict[tuple[str, str], str] = {}

    def ingest(boards: list[tuple[str, str]], kind: str, system: str | None = None):
        for name, node in boards:
            block_rows.append((kind, node, name, node, SOURCE))
            if system:
                ind_rows.append((system, node, name, None, 1, SOURCE))
            try:
                members = _fetch_node_members(sess, node)
                time.sleep(sleep)
            except Exception as e:
                stats["errors"].append(f"{kind}:{name}({node}): {e}")
                continue
            for m in members:
                code = str(m.get("code") or "").strip()
                symbol = str(m.get("symbol") or "").strip()
                if not code and symbol:
                    code = symbol[-6:]
                if not code:
                    continue
                market, code6 = _code_to_market(code, symbol)
                member_rows.append((kind, node, market, code6, SOURCE))
                if kind == "hy":
                    sym_sina[(market, code6)] = name
                elif kind == "sw":
                    sym_sw[(market, code6)] = name

    ingest(sina_hy, "hy", system="sina")
    if include_sw:
        ingest(sina_sw, "sw", system="sina_sw")
    if include_concept:
        ingest(sina_gn, "gn")
    if include_region:
        ingest(sina_dq, "dq")

    # 腾讯补名称
    keys = sorted(set(sym_sina) | set(sym_sw))
    tnames = _tencent_names(sess, keys) if include_tencent_names else {}
    stats["tencent_names"] = len(tnames)

    sym_rows = []
    for market, code in sorted(set(sym_sina) | set(sym_sw)):
        sina_name = sym_sina.get((market, code))
        sw_name = sym_sw.get((market, code))
        tname = tnames.get((market, code))
        extra = {
            "sina_industry": sina_name,
            "sina_sw": sw_name,
            "tencent_name": tname,
        }
        sym_rows.append(
            (
                market,
                code,
                SOURCE,
                None,
                None,  # sw_code 新浪侧是名称不是标准码
                sina_name,  # 主展示：新浪行业
                json.dumps(extra, ensure_ascii=False),
                now,
            )
        )

    stats["industry_names"] = len(ind_rows)
    stats["symbol_industry"] = len(sym_rows)
    uniq_blocks = {(r[0], r[1], r[4]): r for r in block_rows}
    uniq_members = {(r[0], r[1], r[2], r[3], r[4]): r for r in member_rows}
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
