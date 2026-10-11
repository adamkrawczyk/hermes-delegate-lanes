"""delegate-lanes: per-lane provider/model routing for Hermes subagents. Zero core edits.

Mechanism:
  parent = agent.subagent_lifecycle.get_active_subagent_parent()   # bound by every agent turn
  delegate_task(tasks, parent_agent=parent, credentials_cfg=<lane route>, background=depth==0)
credentials_cfg is the same per-call routing owner /review uses (agent/review_engine.py). The model
chooses only a lane NAME from the operator's table, never a raw provider/model/endpoint.

Contract: core keys several protections on the literal name `delegate_task`. contract.reattach()
re-attaches them (shared per-turn spawn cap, per-round call cap, 420 s deadline exemption, guardrail
sets) or the tool is NOT registered. They are re-verified every turn (drift = re-apply, or refuse
calls). Proof of route: audit.py checks each lane child's real usage in state.db.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List

from . import audit, contract
from .lanes import LaneConfigError, LaneTable, describe, load_lanes, verify_resolvable

logger = logging.getLogger("delegate_lanes")

TOOL = "delegate_lane"
ALLOWED_TASK_KEYS = ("goal", "context", "output_schema", "images")
# One module copy per gateway profile scope; OWNER identifies this copy in the core-side refcount.
OWNER = f"delegate-lanes:{uuid.uuid4().hex[:12]}"
_STATE: Dict[str, Any] = {"table": None, "contract": [], "contract_ok": False}


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False)


def _clean_tasks(tasks: Any):
    if isinstance(tasks, str):  # models often stringify the array (core's delegate_task tolerates it too)
        try:
            tasks = json.loads(tasks)
        except (TypeError, ValueError):
            pass
    if not isinstance(tasks, list) or not tasks:
        return None, None, "tasks must be a non-empty array of {goal, context?, output_schema?, images?}"
    clean: List[Dict[str, Any]] = []
    dropped: set = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict) or not str(t.get("goal") or "").strip():
            return None, None, f"tasks[{i}] needs a non-empty string goal"
        dropped |= set(t) - set(ALLOWED_TASK_KEYS)
        clean.append({k: t[k] for k in ALLOWED_TASK_KEYS if k in t})
    return clean, sorted(dropped), None


def handle(args: Dict[str, Any], **_kw: Any) -> str:
    table: LaneTable = _STATE["table"]
    if table is None or not table.lanes:
        return _err("delegate_lane has no configured lanes on this profile.")
    if not _STATE.get("contract_ok"):
        return _err("delegate_lane is disabled: its safety contract with Hermes core could not be (re)applied "
                    "(see agent.log 'delegate-lanes'). Use delegate_task.")
    lane_name = str(args.get("lane") or "").strip()
    lane = table.lanes.get(lane_name)
    if lane is None:
        return _err(f"Unknown lane {lane_name!r}.", available_lanes=table.names())
    clean, dropped, problem = _clean_tasks(args.get("tasks"))
    if problem:
        return _err(problem)

    from agent.subagent_lifecycle import get_active_subagent_parent

    parent = get_active_subagent_parent()
    if parent is None:
        return _err("No active Hermes parent agent is bound to this call; delegate_lane only works inside an agent turn.")
    try:
        route = lane.credentials_cfg()
    except LaneConfigError as exc:
        return _err(str(exc))
    depth = int(getattr(parent, "_delegate_depth", 0) or 0)

    from tools.delegate_tool import delegate_task

    scope = audit.CallScope(lane)
    token = audit.CURRENT_LANE.set(scope)
    try:
        # Mirrors run_agent._dispatch_delegate_task: top level runs in the background, an
        # orchestrator child (depth > 0) joins in-turn. delegate_task itself falls back to
        # synchronous on sessions without async delivery (Kanban, cron, one-shot) or a full pool.
        raw = delegate_task(tasks=clean, parent_agent=parent, credentials_cfg=route, background=(depth == 0))
    finally:
        audit.CURRENT_LANE.reset(token)

    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(out, dict):
        return raw
    info: Dict[str, Any] = {
        "name": lane.name,
        "route": lane.label(),
        "fallbacks": [f"{p}/{m}" for p, m in lane.fallbacks],
        "child_sessions": list(scope.children),
    }
    if dropped:
        info["dropped_task_keys"] = dropped
    verdicts = audit.recent_for(scope.children)
    if verdicts:
        info["route_audit"] = verdicts
    elif scope.children:
        info["route_audit"] = [{"pending": True, "note": (
            "each child is verified on completion against state.db usage; a mismatch is injected into "
            "your next turn and logged to ~/.hermes/logs/delegate-lanes.jsonl")}]
    pending = audit.take_pending(getattr(parent, "session_id", None))
    if pending:
        info["ROUTE_MISMATCH_since_last_call"] = pending
    out["lane"] = info
    return json.dumps(out, ensure_ascii=False)


def _schema(table: LaneTable) -> Dict[str, Any]:
    return {
        "name": TOOL,
        "description": (
            "Spawn subagents exactly like delegate_task, but route them to an operator-approved model lane "
            "instead of the default delegation model. Pick the lane whose purpose fits the work:\n"
            f"{describe(table)}\n"
            "Use plain delegate_task when no lane fits. One lane per call: put every task for a lane in ONE call "
            "(tasks inside a call run in parallel). Separate calls (e.g. one per lane) overlap only when they "
            "return background handles; when results come back inline they run one after another. Same rules as delegate_task: children know nothing of this conversation (pass full context); "
            "at top level results arrive between your turns (finish independent work, then end your turn); "
            "summaries are self-reports. The result's `lane.route_audit` shows the provider/model each child "
            "actually used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lane": {"type": "string", "enum": table.names(), "description": "Lane name from the list above."},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "description": "One entry per subagent; all run in parallel on this lane (limit = "
                                   "delegation.max_concurrent_children).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "Specific, self-contained objective."},
                            "context": {"type": "string", "description": "Everything this child needs: paths, errors, constraints, output language."},
                            "output_schema": {"type": "object", "description": "Optional JSON Schema the child's final answer must satisfy."},
                            "images": {"type": "array", "items": {"type": "string"}, "description": "Optional image paths/URLs the child must see (max 8)."},
                        },
                        "required": ["goal"],
                    },
                },
            },
            "required": ["lane", "tasks"],
        },
    }


def _check() -> bool:
    try:
        from tools.delegate_tool import check_delegate_requirements
        return bool(check_delegate_requirements())
    except Exception:
        return False


def _on_subagent_start(**kw: Any) -> None:
    try:
        audit.record_child_start(kw.get("child_session_id"), kw.get("child_subagent_id"), kw.get("parent_session_id"))
    except Exception:
        logger.debug("delegate-lanes: subagent_start record failed", exc_info=True)


def _on_subagent_stop(**kw: Any) -> None:
    try:
        audit.audit_child_stop(kw.get("child_session_id"), kw.get("child_status"))
    except Exception:
        logger.warning("delegate-lanes: route audit failed for child %s", kw.get("child_session_id"), exc_info=True)


def _mismatch_notice(pending) -> str:
    lines = ["[delegate-lanes ROUTE MISMATCH] A lane child did NOT run on its lane's provider/model (verified "
             "against state.db usage). Treat its result as coming from the observed model, and tell the user if "
             "the route mattered (cost, privacy, review independence):"]
    for v in pending:
        lines.append(f"- lane {v.get('lane')}: requested {v.get('requested')}, observed {', '.join(v.get('actual') or []) or '?'}"
                     f" (child {v.get('child_session_id')})")
    return "\n".join(lines)


def _ensure_contract() -> bool:
    """Cheap per-turn drift check. Drift (something reset a core table) -> re-apply; a moved seam ->
    handler refuses calls until a reload. Never raises."""
    if _STATE.get("table") is None:
        return False
    try:
        if all(contract.verify(TOOL).values()):
            _STATE["contract_ok"] = True
            return True
        logger.error("delegate-lanes: core contract drifted (%s); re-applying", contract.verify(TOOL))
        _STATE["contract"] = contract.reattach(TOOL, OWNER)
        _STATE["contract_ok"] = all(contract.verify(TOOL).values())
    except Exception as exc:
        _STATE["contract_ok"] = False
        logger.error("delegate-lanes: contract re-apply failed (%s); delegate_lane calls refused", exc)
    return bool(_STATE["contract_ok"])


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_kw: Any):
    """Fail closed when a lane request reaches plain delegate_task.

    delegate_task has no `lane` parameter and ignores unknown keys, so such a call would run every child on
    the default delegation route while the caller believes it chose a lane. It happens in the field: with
    tool_search active delegate_lane is a DEFERRED tool, and a model that calls it by name gets core's fuzzy
    name-repair (agent/turn_tool_validation.py -> repair_tool_call) rewriting it to delegate_task. The route
    audit cannot see that (delegate_lane never ran), so the veto has to happen here, before dispatch."""
    if tool_name != "delegate_task" or not isinstance(args, dict):
        return None
    lane = args.get("lane")
    if lane is None or (isinstance(lane, str) and not lane.strip()):
        return None
    table = _STATE.get("table")
    names = table.names() if table is not None else []
    lane_s = str(lane).strip()
    audit._append_jsonl({"ts": time.time(), "event": "lane_on_delegate_task_blocked",
                         "lane": lane_s, "session_id": session_id or None})
    logger.warning("delegate-lanes: blocked delegate_task carrying lane=%r (session %s); the model must use %s",
                   lane_s, session_id or "?", TOOL)
    parts = [f"BLOCKED (delegate-lanes): delegate_task has no `lane` parameter. It would have ignored "
             f"lane={lane_s!r} and run every child on the default delegation model."]
    if not _STATE.get("contract_ok"):
        parts.append(f"{TOOL} is disabled on this host right now (safety contract). Call delegate_task again "
                     "WITHOUT `lane` to use the default route on purpose.")
        return {"action": "block", "message": " ".join(parts)}
    if lane_s not in names:
        parts.append(f"Unknown lane {lane_s!r}.")
    parts.append(
        f"Re-issue the same tasks with the {TOOL} tool: {TOOL}(lane=<lane>, tasks=[{{goal, context}}, ...]). "
        f"If {TOOL} is not in your tool list it is deferred; call it through the bridge: "
        f'tool_call(calls=[{{"name": "{TOOL}", "arguments": {{"lane": "<lane>", "tasks": [...]}}}}]). '
        f"Available lanes: {', '.join(names) or '(none)'}. To use the default route on purpose, call "
        "delegate_task again without `lane`.")
    return {"action": "block", "message": " ".join(parts)}


def _on_unload() -> None:
    try:
        removed = contract.detach(TOOL, OWNER)
        logger.info("delegate-lanes unloaded (%s)", "core contract removed" if removed else
                    "core contract kept: another profile copy is still live")
    except Exception:
        logger.warning("delegate-lanes: detach failed", exc_info=True)
    _STATE.update(table=None, contract=[], contract_ok=False)


def _on_pre_llm_call(**kw: Any):
    """Per turn: re-verify the core contract, then surface background-child route mismatches on the
    parent's NEXT turn (core formats the async completion itself, so the plugin cannot annotate it).
    Clean routes inject nothing."""
    _ensure_contract()
    try:
        pending = audit.take_pending(kw.get("session_id"))
        if pending:
            return {"context": _mismatch_notice(pending)}
    except Exception:
        logger.warning("delegate-lanes: mismatch notice failed", exc_info=True)
    return None


def build_table(settings: Dict[str, Any], verify: str = "warn", resolver=None) -> LaneTable:
    """verify: "warn" (default; advisory, lanes kept), "strict" (failing lanes disabled), "off"."""
    table = load_lanes(settings, audit._home())
    mode = {True: "warn", False: "off"}.get(verify, str(verify or "warn").strip().lower())  # type: ignore[arg-type]
    if mode in ("warn", "strict"):
        try:
            verify_resolvable(table, resolver, strict=(mode == "strict"))
        except Exception as exc:  # resolver import failure etc.: keep lanes, say so
            table.warnings.append(f"provider verification unavailable: {exc}")
    return table


def register(ctx) -> None:
    settings = {
        "lanes": ctx.get_config("lanes", default={}) or {},
        "models": ctx.get_config("models", default=[]) or [],
    }
    table = build_table(settings, verify=ctx.get_config("verify_providers", default="warn"))
    for e in table.errors:
        logger.error("delegate-lanes config: %s", e)
    scoping = [w for w in table.warnings if "profile-scoping" in w]
    for w in table.warnings:
        if w not in scoping:
            logger.warning("delegate-lanes config: %s", w)
    if scoping:  # known false positive: gateway register() runs outside profile scope (see lanes.verify_resolvable)
        logger.info("delegate-lanes: load-time provider check skipped for %d lane(s) on the multiplexed gateway "
                    "(no profile scope at plugin load); delegate_task verifies credentials per call", len(scoping))
    if not table.lanes:
        logger.warning("delegate-lanes: no valid lanes configured (plugins.entries.delegate-lanes.settings); "
                       "delegate_lane NOT registered")
        return
    try:
        report = contract.reattach(TOOL, OWNER)
    except contract.ContractError as exc:
        logger.error("delegate-lanes: core contract seam moved (%s); refusing to register delegate_lane "
                     "(an uncapped/deadline-killed delegation tool is worse than none)", exc)
        return
    _STATE["table"] = table
    _STATE["contract"] = report
    _STATE["contract_ok"] = True
    ctx.register_tool(name=TOOL, toolset="delegation", schema=_schema(table), handler=handle, check_fn=_check,
                      description="Delegate to an operator-approved model lane", emoji="🛣️")
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(_on_unload)
    logger.info("delegate-lanes loaded: lanes=%s | %s", table.names(), " | ".join(report))
