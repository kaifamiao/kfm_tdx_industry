"""后台同步任务：支持进度查询，刷新页面不中断服务端执行。"""
from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Any, Callable

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_active_id: str | None = None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def get_active_job() -> dict[str, Any] | None:
    with _lock:
        if _active_id and _active_id in _jobs:
            j = _jobs[_active_id]
            if j.get("status") in ("queued", "running"):
                return dict(j)
        # 最近完成的也返回，便于刷新后展示结果
        if _active_id and _active_id in _jobs:
            return dict(_jobs[_active_id])
        return None


def _set_job(job_id: str, **kwargs):
    with _lock:
        j = _jobs.setdefault(job_id, {})
        j.update(kwargs)
        j["updated_at"] = _now()


def start_sync_all_job(steps: list[tuple[str, str, Callable[[], Any]]]) -> dict[str, Any]:
    """steps: (key, label, fn) — fn 返回可序列化结果或抛 HTTPException/Exception。"""
    global _active_id
    with _lock:
        if _active_id and _active_id in _jobs:
            cur = _jobs[_active_id]
            if cur.get("status") in ("queued", "running"):
                return dict(cur)
        job_id = uuid.uuid4().hex[:12]
        _active_id = job_id
        _jobs[job_id] = {
            "id": job_id,
            "kind": "sync_all",
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "started_at": None,
            "finished_at": None,
            "total": len(steps),
            "done": 0,
            "percent": 0,
            "current": None,
            "current_label": None,
            "steps": [
                {"key": k, "label": lab, "status": "pending", "message": None, "ok": None}
                for k, lab, _ in steps
            ],
            "message": "排队中",
            "ok": None,
            "results": {},
        }

    def _run():
        _set_job(job_id, status="running", started_at=_now(), message="同步进行中")
        results: dict[str, Any] = {}
        for i, (key, label, fn) in enumerate(steps):
            with _lock:
                st = _jobs[job_id]["steps"]
                st[i]["status"] = "running"
            _set_job(
                job_id,
                current=key,
                current_label=label,
                done=i,
                percent=int(100 * i / max(len(steps), 1)),
                message=f"{label}…",
            )
            try:
                out = fn()
                ok = bool(isinstance(out, dict) and out.get("ok", True))
                # 精简结果，避免巨型 payload
                slim = {
                    "ok": ok,
                    "message": (out.get("message") if isinstance(out, dict) else None),
                }
                if isinstance(out, dict) and out.get("detail"):
                    slim["detail"] = out["detail"]
                results[key] = slim
                with _lock:
                    st = _jobs[job_id]["steps"]
                    st[i]["status"] = "done" if ok else "error"
                    st[i]["ok"] = ok
                    st[i]["message"] = slim.get("message") or slim.get("detail")
            except Exception as e:
                detail = getattr(e, "detail", None) or str(e)
                results[key] = {"ok": False, "detail": detail}
                with _lock:
                    st = _jobs[job_id]["steps"]
                    st[i]["status"] = "error"
                    st[i]["ok"] = False
                    st[i]["message"] = str(detail)
            _set_job(
                job_id,
                done=i + 1,
                percent=int(100 * (i + 1) / max(len(steps), 1)),
                results=dict(results),
            )

        any_ok = any(isinstance(v, dict) and v.get("ok") for v in results.values())
        _set_job(
            job_id,
            status="done",
            finished_at=_now(),
            current=None,
            current_label=None,
            percent=100,
            ok=any_ok,
            message="全部同步完成" if any_ok else "全部同步结束（有失败）",
            results=results,
        )

    t = threading.Thread(target=_run, name=f"sync-all-{job_id}", daemon=True)
    t.start()
    return get_job(job_id) or {"id": job_id}
