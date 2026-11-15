from fastapi import APIRouter, HTTPException, Query

from .. import db

router = APIRouter(prefix="/api", tags=["查询"])


def parse_symbol(symbol: str) -> tuple[str, str]:
    s = symbol.strip().lower().replace(" ", "")
    if "." in s:
        code, m = s.split(".", 1)
        m = {"sh": "sh", "sz": "sz", "bj": "bj", "ss": "sh"}.get(m, m)
        return m, code.zfill(6)[-6:]
    if s.startswith(("sh", "sz", "bj")) and len(s) >= 8:
        return s[:2], s[2:8]
    if s.isdigit() and len(s) <= 6:
        code = s.zfill(6)
        if code.startswith(("5", "6", "9")):
            return "sh", code
        if code.startswith(("4", "8")):
            return "bj", code
        return "sz", code
    raise HTTPException(400, f"无法解析证券代码: {symbol}")


@router.get("/symbol/{symbol}")
def symbol_industry(symbol: str, source: str | None = None):
    market, code = parse_symbol(symbol)
    with db.session() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM symbol_industry WHERE market=? AND code=? AND source=?",
                (market, code, source),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM symbol_industry WHERE market=? AND code=? ORDER BY source",
                (market, code),
            ).fetchall()
        items = [dict(r) for r in rows]
        tdx_codes = {it["tdx_code"] for it in items if it.get("tdx_code")}
        sw_codes = {it["sw_code"] for it in items if it.get("sw_code")}
        names = {"tdx": {}, "sw": {}}
        if tdx_codes:
            q = ",".join("?" * len(tdx_codes))
            for r in conn.execute(
                f"SELECT code, name FROM industry WHERE system='tdx' AND code IN ({q})",
                tuple(tdx_codes),
            ):
                names["tdx"][r["code"]] = r["name"]
        if sw_codes:
            q = ",".join("?" * len(sw_codes))
            for r in conn.execute(
                f"SELECT code, name FROM industry WHERE system='sw' AND code IN ({q})",
                tuple(sw_codes),
            ):
                names["sw"][r["code"]] = r["name"]
    return {"market": market, "code": code, "items": items, "names": names}


@router.get("/symbol/{symbol}/industry-history", summary="股票行业归属历史")
def symbol_industry_history(
    symbol: str,
    source: str | None = None,
    system: str | None = "sw",
):
    """返回个股行业归属变动历史（默认申万 sw_hist）。

    字段：industry_code / industry_name / start_date / end_date（空表示至今）。
    """
    market, code = parse_symbol(symbol)
    where = ["market=?", "code=?"]
    params: list = [market, code]
    if source:
        where.append("source=?")
        params.append(source)
    if system:
        where.append("system=?")
        params.append(system)
    cond = " AND ".join(where)
    with db.session() as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT market, code, source, system, industry_code, industry_name,
                       start_date, end_date, update_time, synced_at
                FROM symbol_industry_hist
                WHERE {cond}
                ORDER BY start_date
                """,
                params,
            ).fetchall()
        except Exception as e:
            raise HTTPException(500, f"历史表不可用，请先同步行业历史: {e}") from e
    items = [dict(r) for r in rows]
    return {
        "market": market,
        "code": code,
        "source": source,
        "system": system,
        "count": len(items),
        "items": items,
    }


@router.get("/industries")
def industries(system: str | None = None, source: str | None = None,
               page: int = 1, size: int = Query(50, le=500)):
    where, params = ["1=1"], []
    if system:
        where.append("system=?")
        params.append(system)
    if source:
        where.append("source=?")
        params.append(source)
    cond = " AND ".join(where)
    with db.session() as conn:
        total = conn.execute(f"SELECT count(*) FROM industry WHERE {cond}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM industry WHERE {cond} ORDER BY system, code LIMIT ? OFFSET ?",
            params + [size, (max(page, 1) - 1) * size],
        ).fetchall()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@router.get("/blocks")
def blocks(kind: str | None = None, source: str | None = None,
           page: int = 1, size: int = Query(50, le=500)):
    where, params = ["1=1"], []
    if kind:
        where.append("kind=?")
        params.append(kind)
    if source:
        where.append("source=?")
        params.append(source)
    cond = " AND ".join(where)
    with db.session() as conn:
        total = conn.execute(f"SELECT count(*) FROM block WHERE {cond}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM block WHERE {cond} ORDER BY kind, code LIMIT ? OFFSET ?",
            params + [size, (max(page, 1) - 1) * size],
        ).fetchall()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@router.get("/blocks/{kind}/{code}/members")
def block_members(kind: str, code: str, source: str | None = None,
                  page: int = 1, size: int = Query(100, le=2000)):
    where = ["kind=?", "block_code=?"]
    params: list = [kind, code]
    if source:
        where.append("source=?")
        params.append(source)
    cond = " AND ".join(where)
    with db.session() as conn:
        total = conn.execute(f"SELECT count(*) FROM block_member WHERE {cond}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM block_member WHERE {cond} ORDER BY market, code LIMIT ? OFFSET ?",
            params + [size, (max(page, 1) - 1) * size],
        ).fetchall()
    return {"kind": kind, "code": code, "total": total, "page": page, "size": size,
            "items": [dict(r) for r in rows]}
