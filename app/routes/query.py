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
        # 北交所 920/83/87/43；其余 9 开头多为沪市 B 股等
        if code.startswith(("920", "43", "83", "87")):
            return "bj", code
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
    taxonomy_version: str | None = None,
):
    """返回个股行业归属变动历史（默认申万 sw_hist）。

    日期语义：
    - start_date / end_date：归属生效区间（空 end=至今）
    - update_time：数据源发布/修订日（回测 as-of / known_at 用此字段）
    - synced_at：本系统入库时间（不可用于回测可知性）
    - taxonomy_version：sw2014 / sw2021
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
    if taxonomy_version:
        where.append("taxonomy_version=?")
        params.append(taxonomy_version)
    cond = " AND ".join(where)
    with db.session() as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT market, code, source, system, industry_code, industry_name,
                       start_date, end_date, update_time, synced_at, taxonomy_version
                FROM symbol_industry_hist
                WHERE {cond}
                ORDER BY start_date
                """,
                params,
            ).fetchall()
        except Exception as e:
            raise HTTPException(500, f"历史表不可用，请先同步行业历史: {e}") from e
    items = []
    for r in rows:
        d = dict(r)
        d["published_at"] = d.get("update_time")  # 别名：发布/修订可知日
        d["effective_from"] = d.get("start_date")
        d["effective_to"] = d.get("end_date")
        items.append(d)
    return {
        "market": market,
        "code": code,
        "source": source,
        "system": system,
        "taxonomy_version": taxonomy_version,
        "count": len(items),
        "date_semantics": {
            "start_date / effective_from": "行业归属生效起（计入日期）",
            "end_date / effective_to": "行业归属生效止（空=至今）",
            "update_time / published_at": "数据源发布/修订日（回测 known_at）",
            "synced_at": "系统同步时间（非研究可知时点）",
        },
        "items": items,
    }


@router.get("/industry-dict", summary="版本化行业字典")
def industry_dict(
    system: str = "sw",
    version: str | None = None,
    page: int = 1,
    size: int = Query(100, le=1000),
):
    where, params = ["system=?"], [system]
    if version:
        where.append("version=?")
        params.append(version)
    cond = " AND ".join(where)
    with db.session() as conn:
        try:
            total = conn.execute(
                f"SELECT count(*) FROM industry_dict WHERE {cond}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT system, version, code, name, parent_code, level,
                       effective_from, effective_to, source, synced_at
                FROM industry_dict WHERE {cond}
                ORDER BY version, code
                LIMIT ? OFFSET ?
                """,
                params + [size, (max(page, 1) - 1) * size],
            ).fetchall()
        except Exception as e:
            raise HTTPException(500, f"industry_dict 不可用，请先同步行业历史: {e}") from e
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [dict(r) for r in rows],
    }


@router.get("/industry-history/quality", summary="行业归属历史质量检查")
def industry_history_quality(source: str = "sw_hist"):
    from ..qa import run_industry_hist_qa

    return run_industry_hist_qa(source=source)


@router.get("/industry-history/coverage", summary="缺历史归属证券分桶")
def industry_history_coverage():
    """对比 symbol_industry vs hist，给出缺失原因分桶（不回填）。"""
    from ..sources.sw_hist_src import _classify_missing_symbols

    db.init_schema()
    with db.session() as conn:
        return _classify_missing_symbols(conn)


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
