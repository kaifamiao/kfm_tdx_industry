"""申万行业归属历史质量检查。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .. import db
from ..sources.sw_hist_src import SOURCE, SYSTEM


def run_industry_hist_qa(source: str = SOURCE) -> dict[str, Any]:
    db.init_schema()
    with db.session() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT market, code, industry_code, industry_name,
                       start_date, end_date, update_time, taxonomy_version
                FROM symbol_industry_hist
                WHERE source=?
                ORDER BY market, code, start_date
                """,
                (source,),
            )
        ]
        dict_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT version, code, name FROM industry_dict WHERE system=?",
                (SYSTEM,),
            )
        ]

    dict_set = {(r["version"], r["code"]) for r in dict_rows}
    dict_by_ver: dict[str, set[str]] = defaultdict(set)
    for r in dict_rows:
        dict_by_ver[r["version"]].add(r["code"])

    total = len(rows)
    by_ver: dict[str, int] = defaultdict(int)
    linked = 0
    unresolved_name = 0
    missing_dict = 0
    missing_dict_codes: dict[str, int] = defaultdict(int)

    # 按证券检查区间
    by_sym: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_sym[(r["market"], r["code"])].append(r)
        ver = r.get("taxonomy_version") or ""
        by_ver[ver or "(null)"] += 1
        if r.get("industry_name"):
            linked += 1
        else:
            unresolved_name += 1
        key = (ver, r["industry_code"])
        if ver and key not in dict_set:
            missing_dict += 1
            missing_dict_codes[f"{ver}:{r['industry_code']}"] += 1
        elif not ver:
            missing_dict += 1

    overlaps = []
    gaps = []
    multi_open = []
    for (market, code), segs in by_sym.items():
        segs = sorted(segs, key=lambda x: x["start_date"] or "")
        open_n = sum(1 for s in segs if not s.get("end_date"))
        if open_n > 1:
            multi_open.append({"market": market, "code": code, "open_segments": open_n})
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            ae, bs = a.get("end_date"), b.get("start_date")
            if ae and bs and ae >= bs:
                overlaps.append(
                    {
                        "market": market,
                        "code": code,
                        "a": {"start": a["start_date"], "end": ae, "ind": a["industry_code"]},
                        "b": {"start": bs, "end": b.get("end_date"), "ind": b["industry_code"]},
                    }
                )
            if ae and bs:
                # 缺口：end+1 < next start
                try:
                    from datetime import datetime, timedelta

                    end_d = datetime.strptime(ae, "%Y-%m-%d").date()
                    start_d = datetime.strptime(bs, "%Y-%m-%d").date()
                    if end_d + timedelta(days=1) < start_d:
                        gaps.append(
                            {
                                "market": market,
                                "code": code,
                                "gap_from": (end_d + timedelta(days=1)).isoformat(),
                                "gap_to": (start_d - timedelta(days=1)).isoformat(),
                            }
                        )
                except ValueError:
                    pass

    # 重复主键风险：同 market/code/start 多行（理论上 PK 防住）
    dup_key = defaultdict(int)
    for r in rows:
        dup_key[(r["market"], r["code"], r["start_date"])] += 1
    dups = [
        {"market": m, "code": c, "start_date": s, "n": n}
        for (m, c, s), n in dup_key.items()
        if n > 1
    ]

    link_rate = round(linked / total, 4) if total else 0.0
    dict_rate = round((total - missing_dict) / total, 4) if total else 0.0

    return {
        "source": source,
        "total_rows": total,
        "symbols": len(by_sym),
        "by_taxonomy_version": dict(by_ver),
        "dict_size_by_version": {k: len(v) for k, v in dict_by_ver.items()},
        "name_resolved": linked,
        "name_unresolved": unresolved_name,
        "name_link_rate": link_rate,
        "dict_linked_rows": total - missing_dict,
        "dict_missing_rows": missing_dict,
        "dict_link_rate": dict_rate,
        "top_missing_dict_codes": sorted(
            ({"key": k, "rows": v} for k, v in missing_dict_codes.items()),
            key=lambda x: -x["rows"],
        )[:30],
        "duplicate_start_dates": {"count": len(dups), "samples": dups[:20]},
        "interval_overlaps": {"count": len(overlaps), "samples": overlaps[:20]},
        "interval_gaps": {"count": len(gaps), "samples": gaps[:20]},
        "multi_open_segments": {"count": len(multi_open), "samples": multi_open[:20]},
        "pass": (
            missing_dict == 0
            and len(dups) == 0
            and len(overlaps) == 0
            and len(multi_open) == 0
        ),
    }
