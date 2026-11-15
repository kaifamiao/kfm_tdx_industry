"""解析 data/tdx_files 中的通达信行业/板块文件并写入 SQLite。"""
from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

from .. import db

SOURCE = "tdx_file"
MARKET_MAP = {"0": "sz", "1": "sh", "2": "bj"}


def _read_text(path: Path) -> list[str]:
    raw = path.read_bytes()
    for enc in ("gbk", "gb2312", "utf-8"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="ignore").splitlines()


def parse_incon(path: Path) -> list[tuple]:
    """返回 (system, code, name, parent_code, level, source)。"""
    if not path.is_file():
        return []
    system = None
    rows: list[tuple] = []
    for line in _read_text(path):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and s != "######":
            tag = s.lstrip("#").upper()
            if tag in ("TDXNHY", "SWHY", "ZJHHY"):
                system = {"TDXNHY": "tdx", "SWHY": "sw", "ZJHHY": "csrc"}[tag]
            else:
                system = None
            continue
        if s == "######":
            system = None
            continue
        if not system or "|" not in s:
            continue
        code, name = s.split("|", 1)
        code, name = code.strip(), name.strip()
        if not code:
            continue
        parent = None
        level = 1
        if system == "tdx":
            # T01 / T0101 / T010101
            if len(code) >= 7:
                level, parent = 3, code[:5]
            elif len(code) >= 5:
                level, parent = 2, code[:3]
            else:
                level, parent = 1, None
        elif system == "sw":
            if code.endswith("00") and not code.endswith("0000"):
                level, parent = 2, code[:2] + "0000" if len(code) >= 6 else None
            elif not code.endswith("00"):
                level, parent = 3, code[:4] + "00" if len(code) >= 6 else None
            else:
                level, parent = 1, None
        elif system == "csrc":
            level = 1 if len(code) == 1 else (2 if len(code) <= 3 else 3)
            parent = code[:1] if level > 1 else None
        rows.append((system, code, name, parent, level, SOURCE))
    return rows


def parse_tdxhy(path: Path) -> list[tuple]:
    """返回 (market, code, source, tdx_code, sw_code, em, extra, updated_at)。"""
    if not path.is_file():
        return []
    now = dt.datetime.now().isoformat(timespec="seconds")
    out = []
    for line in _read_text(path):
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) < 3:
            continue
        mkt = MARKET_MAP.get(parts[0], parts[0])
        code = parts[1].zfill(6)[-6:]
        tdx_code = parts[2] or None
        sw_code = parts[3] if len(parts) > 3 and parts[3] else None
        if not code.isdigit():
            continue
        out.append((mkt, code, SOURCE, tdx_code, sw_code, None, None, now))
    return out


def parse_tdxzs(path: Path) -> tuple[list[tuple], dict[str, list[str]]]:
    """板块列表 + 行业码→板块码映射（用于成分反查）。

    返回:
      blocks: (kind, code, name, index_code, source)
      hy_map: industry_code -> [block_code, ...]
    """
    if not path.is_file():
        return [], {}
    kind_map = {
        "2": "hy",
        "3": "dq",
        "4": "gn",
        "5": "fg",
        "6": "zs",
        "8": "sw",
        "12": "yjhy",
    }
    blocks: list[tuple] = []
    hy_map: dict[str, list[str]] = {}
    for line in _read_text(path):
        parts = [p.strip() for p in line.strip().split("|")]
        if len(parts) < 3:
            continue
        name, index_code, typ = parts[0], parts[1], parts[2]
        ref = parts[5] if len(parts) > 5 else ""
        kind = kind_map.get(typ, f"t{typ}")
        block_code = ref or index_code or name
        blocks.append((kind, block_code, name, index_code or None, SOURCE))
        if kind in ("hy", "sw", "yjhy") and ref:
            hy_map.setdefault(ref, []).append(block_code)
            # 二级码前缀匹配辅助
            if len(ref) >= 5:
                hy_map.setdefault(ref[:5], []).append(block_code)
    return blocks, hy_map


def parse_block_dat(path: Path, kind: str) -> tuple[list[tuple], list[tuple]]:
    """解析 block_gn/fg/zs.dat。返回 blocks, members。"""
    if not path.is_file():
        return [], []
    data = path.read_bytes()
    if len(data) < 386:
        return [], []
    try:
        count = struct.unpack_from("<h", data, 384)[0]
    except struct.error:
        return [], []
    blocks, members = [], []
    blk = data[386:]
    size = 2813
    for i in range(max(0, count)):
        chunk = blk[i * size : (i + 1) * size]
        if len(chunk) < 13:
            break
        name = chunk[:9].split(b"\x00", 1)[0].decode("gbk", errors="ignore").strip()
        try:
            num = struct.unpack_from("<h", chunk, 9)[0]
        except struct.error:
            continue
        codes_raw = chunk[13 : 13 + 7 * max(0, num)].decode("gbk", errors="ignore")
        codes = [c for c in codes_raw.split("\x00") if c and c.strip()]
        code_key = name or f"{kind}_{i}"
        blocks.append((kind, code_key, name, None, SOURCE))
        for c in codes:
            c = c.strip()
            if len(c) < 6:
                continue
            # 有的格式带市场前缀数字
            if len(c) == 7 and c[0] in "012":
                mkt = MARKET_MAP.get(c[0], "sz")
                sym = c[1:7]
            else:
                sym = c[-6:]
                mkt = "sh" if sym.startswith(("5", "6", "9")) else ("bj" if sym.startswith(("4", "8")) else "sz")
            members.append((kind, code_key, mkt, sym, SOURCE))
    return blocks, members


def sync_from_tdx_dir(tdx_dir: Path) -> dict:
    tdx_dir = Path(tdx_dir)
    hq = tdx_dir / "hq_cache"
    incon_rows = parse_incon(tdx_dir / "incon.dat")
    hy_rows = parse_tdxhy(hq / "tdxhy.cfg")

    blocks: list[tuple] = []
    hy_map: dict[str, list[str]] = {}
    for name in ("tdxzs.cfg", "tdxzs3.cfg"):
        b, m = parse_tdxzs(hq / name)
        blocks.extend(b)
        for k, v in m.items():
            hy_map.setdefault(k, []).extend(v)

    members: list[tuple] = []
    # 行业成分：按 tdxhy 的 tdx_code 匹配 tdxzs
    seen_mem = set()
    for market, code, _src, tdx_code, _sw, *_ in hy_rows:
        if not tdx_code:
            continue
        keys = [tdx_code]
        if len(tdx_code) >= 5:
            keys.append(tdx_code[:5])
        if len(tdx_code) >= 3:
            keys.append(tdx_code[:3])
        for k in keys:
            for bc in hy_map.get(k, []):
                key = ("hy", bc, market, code)
                if key in seen_mem:
                    continue
                seen_mem.add(key)
                members.append(("hy", bc, market, code, SOURCE))

    for kind, fname in (("gn", "block_gn.dat"), ("fg", "block_fg.dat"), ("zs", "block_zs.dat")):
        b, m = parse_block_dat(hq / fname, kind)
        blocks.extend(b)
        members.extend(m)

    # 去重 blocks
    uniq_blocks = {}
    for row in blocks:
        uniq_blocks[(row[0], row[1], row[4])] = row
    blocks = list(uniq_blocks.values())

    with db.session() as conn:
        conn.execute("DELETE FROM industry WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM symbol_industry WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM block WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM block_member WHERE source=?", (SOURCE,))
        if incon_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO industry(system, code, name, parent_code, level, source) VALUES (?,?,?,?,?,?)",
                incon_rows,
            )
        if hy_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO symbol_industry(market, code, source, tdx_code, sw_code, em_industry, extra_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                hy_rows,
            )
        if blocks:
            conn.executemany(
                "INSERT OR REPLACE INTO block(kind, code, name, index_code, source) VALUES (?,?,?,?,?)",
                blocks,
            )
        if members:
            conn.executemany(
                "INSERT OR REPLACE INTO block_member(kind, block_code, market, code, source) VALUES (?,?,?,?,?)",
                members,
            )

    return {
        "industry": len(incon_rows),
        "symbol_industry": len(hy_rows),
        "block": len(blocks),
        "block_member": len(members),
        "files": {
            "incon": (tdx_dir / "incon.dat").is_file(),
            "tdxhy": (hq / "tdxhy.cfg").is_file(),
            "tdxzs": (hq / "tdxzs.cfg").is_file() or (hq / "tdxzs3.cfg").is_file(),
            "block_gn": (hq / "block_gn.dat").is_file(),
            "block_fg": (hq / "block_fg.dat").is_file(),
            "block_zs": (hq / "block_zs.dat").is_file(),
        },
    }
