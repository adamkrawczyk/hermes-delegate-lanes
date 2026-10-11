"""Re-attach the execution contract that Hermes core keys on the literal tool NAME `delegate_task`.

Reusing delegate_task() preserves its internals, not the contract around
its name. Each entry below is a name-keyed seam in core that `delegate_lane` would otherwise silently
bypass:

  S1 agent.tool_guardrails._LOOP_CAPS["delegate_task"]       per-turn subagent cap (default 50).
     The lane gets the SAME spec tuple, so it charges the SAME controller counter
     (`_turn_subagent_count`): plain + lane spawns share one budget per turn.
  S2 agent.tool_executor._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS    exemption from the generic 420 s
     sequential tool deadline. Synchronous lane batches (Kanban workers, cron, one-shot,
     orchestrator children, async-pool fallback) would otherwise time out and orphan children.
  S3 run_agent.AIAgent._cap_delegate_task_calls              per-ROUND truncation of delegation calls
     to max_concurrent_children, applied before dispatch (agent/turn_tool_round.py). It counts the
     literal name, so lane calls would escape it. Replaced by a
     same-semantics version that counts {delegate_task, delegate_lane} together.
  G  agent.tool_guardrails.MUTATING_TOOL_NAMES / PROGRESS_RESET_TOOL_NAMES   guardrail parity.

Known, accepted name-keyed tail (display/telemetry only, no execution semantics): CLI one-time
"running in background" notice, TUI lifecycle card, ACP result formatting, `first_delegation` metric.

All re-attachments are runtime registrations, not file edits. reattach() verifies each took effect;
ANY missing seam raises ContractError and the plugin must NOT expose the tool (fail closed: an
uncapped, deadline-killed delegation tool is worse than no tool). Seam detection by source substring
is deliberately brittle: an upstream refactor disables the tool rather than silently degrading it.

Core modules are process singletons, but the multiplexed gateway loads one plugin module copy per
profile. Ownership is therefore reference-counted ON THE CORE MODULE (`_OWNERS_ATTR`), so one copy
unloading never strips the contract from another that is still live.
"""
from __future__ import annotations

import inspect
import logging

logger = logging.getLogger("delegate_lanes")

SOURCE_TOOL = "delegate_task"
_OWNERS_ATTR = "_delegate_lanes_owners"     # set on agent.tool_guardrails: {(tool, owner), ...}
_CAP_NAMES_ATTR = "_delegate_lanes_names"   # set on the replacement round-cap function
_CAP_ORIG_ATTR = "_delegate_lanes_original"


class ContractError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- S1
def _reattach_loop_cap(tool: str) -> str:
    import agent.tool_guardrails as g

    caps = getattr(g, "_LOOP_CAPS", None)
    if not isinstance(caps, dict) or SOURCE_TOOL not in caps:
        raise ContractError("agent.tool_guardrails._LOOP_CAPS['delegate_task'] not found")
    caps[tool] = caps[SOURCE_TOOL]
    src = inspect.getsource(g.ToolCallGuardrailController._check_loop_cap)
    if "_LOOP_CAPS" not in src or "_subagent_spawn_count" not in src:
        raise ContractError("_check_loop_cap no longer reads _LOOP_CAPS/_subagent_spawn_count")
    return f"loop_cap: {tool} shares {caps[SOURCE_TOOL][1]}"


# ----------------------------------------------------------------------------- S2
def _reattach_deadline_exemption(tool: str) -> str:
    import agent.tool_executor as te

    exempt = getattr(te, "_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS", None)
    if not isinstance(exempt, (set, frozenset)) or SOURCE_TOOL not in exempt:
        raise ContractError("agent.tool_executor._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS missing or lacks delegate_task")
    te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS = frozenset(exempt) | {tool}
    # The exemption only works if the lookup happens at CALL time via the module global.
    found = [name for name, fn in inspect.getmembers(te, inspect.isfunction)
             if fn.__module__ == te.__name__ and "in _SEQUENTIAL_DEADLINE_EXEMPT_TOOLS" in inspect.getsource(fn)]
    if not found:
        raise ContractError("no function in agent.tool_executor reads _SEQUENTIAL_DEADLINE_EXEMPT_TOOLS at call time")
    return f"deadline_exempt: via {','.join(found)}"


# ----------------------------------------------------------------------------- S3
def _round_cap_function():
    import run_agent

    raw = run_agent.AIAgent.__dict__.get("_cap_delegate_task_calls")
    if raw is None:
        raise ContractError("run_agent.AIAgent._cap_delegate_task_calls not found")
    return run_agent.AIAgent, (raw.__func__ if isinstance(raw, staticmethod) else raw)


def _make_round_cap(original, names: frozenset):
    def _cap_delegate_task_calls(tool_calls: list) -> list:
        """delegate-lanes replacement: core semantics, but delegate_task AND delegate_lane calls share
        the per-round max_concurrent_children budget (first come, first kept; others always kept)."""
        from tools.delegate_tool import _get_max_concurrent_children
        max_children = _get_max_concurrent_children()
        counted = getattr(_cap_delegate_task_calls, _CAP_NAMES_ATTR)
        count = sum(1 for tc in tool_calls if tc.function.name in counted)
        if count <= max_children:
            return tool_calls
        kept, out = 0, []
        for tc in tool_calls:
            if tc.function.name in counted:
                if kept >= max_children:
                    continue
                kept += 1
            out.append(tc)
        logger.warning("Truncated %d excess delegation call(s) (%s) to enforce max_concurrent_children=%d",
                       count - max_children, ",".join(sorted(counted)), max_children)
        return out

    setattr(_cap_delegate_task_calls, _CAP_NAMES_ATTR, names)
    setattr(_cap_delegate_task_calls, _CAP_ORIG_ATTR, original)
    return _cap_delegate_task_calls


def _reattach_round_cap(tool: str) -> str:
    import agent.turn_tool_round as ttr

    cls, fn = _round_cap_function()
    if "_cap_delegate_task_calls" not in inspect.getsource(ttr):
        raise ContractError("agent.turn_tool_round no longer calls _cap_delegate_task_calls")
    names = getattr(fn, _CAP_NAMES_ATTR, None)
    if names is not None:  # already replaced (another lane tool / profile copy): widen the name set
        setattr(fn, _CAP_NAMES_ATTR, frozenset(names) | {tool})
        return f"round_cap: {tool} shares max_concurrent_children (already wrapped)"
    src = inspect.getsource(fn)
    if 'tc.function.name == "delegate_task"' not in src or "_get_max_concurrent_children" not in src:
        raise ContractError("_cap_delegate_task_calls changed shape; refusing to replace it")
    cls._cap_delegate_task_calls = staticmethod(_make_round_cap(fn, frozenset({SOURCE_TOOL, tool})))
    return f"round_cap: {tool} shares max_concurrent_children"


# ----------------------------------------------------------------------------- G
def _reattach_guardrail_sets(tool: str) -> str:
    import agent.tool_guardrails as g

    done = []
    for attr in ("MUTATING_TOOL_NAMES", "PROGRESS_RESET_TOOL_NAMES"):
        cur = getattr(g, attr, None)
        if not isinstance(cur, (set, frozenset)) or SOURCE_TOOL not in cur:
            raise ContractError(f"agent.tool_guardrails.{attr} missing or lacks delegate_task")
        setattr(g, attr, frozenset(cur) | {tool})
        done.append(attr)
    return "guardrail_sets: " + ",".join(done)


# ----------------------------------------------------------------------------- public
def reattach(tool: str, owner: str = "default") -> list[str]:
    """Apply every re-attachment (idempotent); raise ContractError on the first missing seam."""
    import agent.tool_guardrails as g

    report = [_reattach_loop_cap(tool), _reattach_deadline_exemption(tool), _reattach_round_cap(tool),
              _reattach_guardrail_sets(tool)]
    owners = getattr(g, _OWNERS_ATTR, None)
    if not isinstance(owners, set):
        owners = set()
        setattr(g, _OWNERS_ATTR, owners)
    owners.add((tool, owner))
    return report


def detach(tool: str, owner: str = "default") -> bool:
    """Undo reattach() for this owner. Core entries are removed only when NO owner of `tool` remains
    (another profile's plugin copy may still be live). Returns True when entries were removed."""
    import agent.tool_executor as te
    import agent.tool_guardrails as g

    owners = getattr(g, _OWNERS_ATTR, None)
    if isinstance(owners, set):
        owners.discard((tool, owner))
        if any(t == tool for t, _ in owners):
            return False
    caps = getattr(g, "_LOOP_CAPS", None)
    if isinstance(caps, dict):
        caps.pop(tool, None)
    exempt = getattr(te, "_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS", None)
    if isinstance(exempt, (set, frozenset)):
        te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS = frozenset(exempt) - {tool}
    for attr in ("MUTATING_TOOL_NAMES", "PROGRESS_RESET_TOOL_NAMES"):
        cur = getattr(g, attr, None)
        if isinstance(cur, (set, frozenset)):
            setattr(g, attr, frozenset(cur) - {tool})
    try:
        cls, fn = _round_cap_function()
        names = getattr(fn, _CAP_NAMES_ATTR, None)
        if names is not None:
            rest = frozenset(names) - {tool}
            if rest <= {SOURCE_TOOL}:
                cls._cap_delegate_task_calls = staticmethod(getattr(fn, _CAP_ORIG_ATTR))
            else:
                setattr(fn, _CAP_NAMES_ATTR, rest)
    except ContractError:
        pass
    return True


def verify(tool: str) -> dict[str, bool]:
    """Read-only check that every re-attachment is currently in effect (cheap: run every turn)."""
    import agent.tool_executor as te
    import agent.tool_guardrails as g

    caps = getattr(g, "_LOOP_CAPS", {})
    try:
        _, fn = _round_cap_function()
        round_cap = tool in (getattr(fn, _CAP_NAMES_ATTR, None) or ())
    except ContractError:
        round_cap = False
    return {
        "loop_cap_shared": caps.get(tool) is not None and caps.get(tool) == caps.get(SOURCE_TOOL),
        "deadline_exempt": tool in getattr(te, "_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS", ()),
        "round_cap_shared": round_cap,
        "mutating": tool in getattr(g, "MUTATING_TOOL_NAMES", ()),
        "progress_reset": tool in getattr(g, "PROGRESS_RESET_TOOL_NAMES", ()),
    }
