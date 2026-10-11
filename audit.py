"""Route audit: prove which provider/model a lane child ACTUALLY ran on.

The live manifest records the REQUESTED route; result entries carry only the final model. The ground
truth is `state.db.session_model_usage`, written per API call: (session_id, model,
billing_provider, billing_base_url, task, ...).

Flow:
  1. The handler sets CURRENT_LANE (ContextVar) around its delegate_task() call. Child construction
     runs inside that call; `subagent_start` fires there (or on a hook worker with a copied context)
     and records child_session_id -> lane.
  2. `subagent_stop` (caller thread, after the child ran) audits that child. Its main-loop usage rows
     (task == '') must match the lane's allowed routes: primary + declared fallbacks.
  3. Each verdict is one JSONL row (~/.hermes/logs/delegate-lanes.jsonl). A mismatch also logs a
     WARNING and queues for the owning parent session (bounded); the parent's next turn
     (pre_llm_call) or next delegate_lane result surfaces it exactly once. An UNVERIFIED verdict
     (no usage rows even after retries, or rows without a provider) logs a WARNING too: it is never
     silently treated as a pass.

Matching is strict: full model id (case-insensitive; a leading "<provider>/" is
tolerated), and the observed billing_provider must equal the lane provider. A row with an empty
provider cannot prove the route, so it makes the verdict unverified (None), not ok.

v1 is non-blocking by design. Note: middleware that raises before next_call FAILS OPEN, so a
blocking variant must short-circuit by returning an error, never by raising.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("delegate_lanes")

CURRENT_LANE: contextvars.ContextVar = contextvars.ContextVar("delegate_lanes_current_lane", default=None)

_LOCK = threading.Lock()
_CHILD_LANE: dict[str, dict[str, Any]] = {}      # child_session_id -> {lane, subagent_id, parent_session_id, ts}
_PENDING: dict[str, list[dict[str, Any]]] = {}    # parent_session_id -> unseen mismatch rows
_RECENT: list[dict[str, Any]] = []                # bounded ring of recent verdicts (for sync annotation)
_MAX_TRACKED = 2000
_MAX_PENDING_PARENTS = 500     # parents that never take another turn must not leak forever
_MAX_PENDING_PER_PARENT = 20
_USAGE_RETRY_DELAYS = (0.5, 1.0)  # rows come from an async token writer; children flush at persist,
                                  # but a no-rows read is retried before calling the child unverified


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def log_path() -> Path:
    return _home() / "logs" / "delegate-lanes.jsonl"


def _norm(x: str) -> str:
    return (x or "").strip().lower()


def _model_eq(allowed_provider: str, allowed_model: str, observed_model: str) -> bool:
    am, om, ap = _norm(allowed_model), _norm(observed_model), _norm(allowed_provider)
    return bool(om) and (om in (am, f"{ap}/{am}") or am == f"{ap}/{om}")


def route_status(allowed: tuple[tuple[str, str], ...], provider: str, model: str) -> bool | None:
    """True = provably on an allowed route; False = provably not; None = cannot tell (no provider)."""
    observed_provider = _norm(provider)
    model_ok = [ap for ap, am in allowed if _model_eq(ap, am, model)]
    if not observed_provider:
        return None if model_ok else False
    return any(_norm(ap) == observed_provider for ap in model_ok)


def route_matches(allowed: tuple[tuple[str, str], ...], provider: str, model: str) -> bool:
    return route_status(allowed, provider, model) is True


def usage_rows(session_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    db = db_path or (_home() / "state.db")
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        cur = con.execute(
            "SELECT model, billing_provider, billing_base_url, api_call_count FROM session_model_usage "
            "WHERE session_id = ? AND (task = '' OR task IS NULL)", (session_id,))
        return [{"model": r[0], "provider": r[1], "base_url": r[2], "api_calls": r[3]} for r in cur.fetchall()]
    finally:
        con.close()


def audit_session(lane, session_id: str, db_path: Path | None = None, retry: bool = False) -> dict[str, Any]:
    """Verdict for one child session: ok True / False / None (unverified: no main-loop usage rows, or
    a row whose provider is blank). retry=True re-reads after short delays when no rows exist yet."""
    rows = usage_rows(session_id, db_path)
    if retry:
        for delay in _USAGE_RETRY_DELAYS:
            if rows:
                break
            time.sleep(delay)
            rows = usage_rows(session_id, db_path)
    allowed = lane.allowed_routes()
    status = [route_status(allowed, r["provider"], r["model"]) for r in rows]
    bad = [r for r, st in zip(rows, status, strict=True) if st is False]
    if bad:
        ok: bool | None = False
    elif not rows or any(st is None for st in status):
        ok = None
    else:
        ok = True
    return {
        "lane": lane.name,
        "requested": lane.label(),
        "allowed": [f"{p}/{m}" for p, m in allowed],
        "actual": [f"{r['provider'] or '?'}/{r['model']} x{r['api_calls']}" for r in rows],
        "mismatched": [f"{r['provider'] or '?'}/{r['model']}" for r in bad],
        "ok": ok,
        "child_session_id": session_id,
    }


def _append_jsonl(row: dict[str, Any]) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("delegate-lanes: audit log write failed", exc_info=True)


class CallScope:
    """Per delegate_lane call. A mutable list survives copy_context() by reference, so children
    started by hook workers still land here."""

    __slots__ = ("children", "lane")

    def __init__(self, lane) -> None:
        self.lane = lane
        self.children: list[str] = []


def record_child_start(child_session_id: str | None, child_subagent_id: str | None,
                       parent_session_id: str | None) -> bool:
    scope = CURRENT_LANE.get()
    if scope is None or not child_session_id:
        return False
    lane = scope.lane
    scope.children.append(child_session_id)
    with _LOCK:
        if len(_CHILD_LANE) >= _MAX_TRACKED:
            for k in sorted(_CHILD_LANE, key=lambda k: _CHILD_LANE[k]["ts"])[: _MAX_TRACKED // 4]:
                _CHILD_LANE.pop(k, None)
        _CHILD_LANE[child_session_id] = {"lane": lane, "subagent_id": child_subagent_id,
                                         "parent_session_id": parent_session_id, "ts": time.time()}
    return True


def audit_child_stop(child_session_id: str | None, child_status: str | None = None,
                     db_path: Path | None = None) -> dict[str, Any] | None:
    if not child_session_id:
        return None
    with _LOCK:
        meta = _CHILD_LANE.pop(child_session_id, None)
    if meta is None:
        return None  # not a lane child
    verdict = audit_session(meta["lane"], child_session_id, db_path, retry=True)
    verdict.update({"ts": time.time(), "subagent_id": meta["subagent_id"],
                    "parent_session_id": meta["parent_session_id"], "child_status": child_status})
    _append_jsonl(verdict)
    with _LOCK:
        _RECENT.append(verdict)
        del _RECENT[:-200]
        if verdict["ok"] is False:
            _queue_pending(meta["parent_session_id"] or "", verdict)
    if verdict["ok"] is False:
        logger.warning("delegate-lanes ROUTE MISMATCH lane=%s requested=%s actual=%s child=%s",
                       verdict["lane"], verdict["requested"], verdict["actual"], child_session_id)
    elif verdict["ok"] is None:
        logger.warning("delegate-lanes route UNVERIFIED lane=%s child=%s rows=%s (no provable usage rows; "
                       "treat the child's route as unknown)", verdict["lane"], child_session_id, verdict["actual"])
    return verdict


def _queue_pending(parent: str, verdict: dict[str, Any]) -> None:
    """Caller holds _LOCK. Bounded per parent and in parent count (oldest parents evicted first)."""
    if parent not in _PENDING and len(_PENDING) >= _MAX_PENDING_PARENTS:
        oldest = sorted(_PENDING, key=lambda k: _PENDING[k][0].get("ts", 0) if _PENDING[k] else 0)
        for k in oldest[: max(1, _MAX_PENDING_PARENTS // 4)]:
            _PENDING.pop(k, None)
    rows = _PENDING.setdefault(parent, [])
    rows.append(verdict)
    del rows[:-_MAX_PENDING_PER_PARENT]


def take_pending(parent_session_id: str | None) -> list[dict[str, Any]]:
    with _LOCK:
        return _PENDING.pop(parent_session_id or "", [])


def recent_for(child_session_ids: list[str]) -> list[dict[str, Any]]:
    want = set(child_session_ids)
    with _LOCK:
        return [v for v in _RECENT if v.get("child_session_id") in want]


def _reset_for_tests() -> None:
    with _LOCK:
        _CHILD_LANE.clear()
        _PENDING.clear()
        _RECENT.clear()
