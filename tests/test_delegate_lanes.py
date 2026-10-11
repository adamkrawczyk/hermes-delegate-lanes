#!/usr/bin/env python3
"""Hermetic behavioural contract for the delegate-lanes plugin.

No network, no real model calls. Exercises the REAL core seams the plugin depends on
(delegate_task credential routing, child construction, guardrail loop caps, sequential-deadline
exemption, child toolset stripping, ContextVar parent binding) and the plugin's own lane parsing,
handler and route audit. Each MUTATION check deliberately breaks one assumption and asserts the
corresponding check turns red. A suite that cannot fail proves nothing.

Interpreter: plugins run inside Hermes, so this suite needs Hermes core importable. If it is not
importable here, the suite re-executes itself under the first Hermes runtime interpreter that can
import it (override: DELEGATE_LANES_PYTHON). HERMES_CORE_DIR prepends a core checkout to sys.path,
which lets you test a CANDIDATE core before upgrading to it.

Exit 0 = contract holds. Non-zero = do NOT promote the update / do NOT trust the plugin.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_REEXEC_FLAG = "DELEGATE_LANES_REEXECED"

# Hermetic HERMES_HOME *before* any core import (config/state/log writes land in a temp dir).
_TMP_HOME = tempfile.mkdtemp(prefix="delegate-lanes-test-")
os.environ["HERMES_HOME"] = _TMP_HOME
os.environ.pop("HERMES_KANBAN_TASK", None)
# Depth 2 so a depth-1 (orchestrator) parent may spawn: it joins synchronously, which keeps the
# end-to-end route test fully in-process. Global reasoning "high" must reach every lane child.
Path(_TMP_HOME, "config.yaml").write_text(
    "delegation:\n  max_spawn_depth: 2\n  max_concurrent_children: 10\n  reasoning_effort: high\n")
if os.environ.get("HERMES_CORE_DIR"):
    sys.path.insert(0, os.environ["HERMES_CORE_DIR"])
    # Enter a candidate tree exactly like the real launcher: hermes_bootstrap relaunches under that
    # tree's managed interpreter and activates its dependency environment. Importing tools.* first
    # makes the tree relaunch mid-import WITHOUT activation (measured: base python, no ruamel).
    import hermes_bootstrap  # noqa: F401


_IMPORT_ERROR = None


def _core_importable() -> bool:
    global _IMPORT_ERROR
    try:
        import agent.tool_guardrails  # noqa: F401
        import tools.delegate_tool  # noqa: F401
        return True
    except Exception as exc:  # keep the reason: "cannot import" without it is undebuggable
        _IMPORT_ERROR = exc
        if os.environ.get("DL_DEBUG"):
            print(f"[debug] import failed under {sys.executable}: {exc!r}\n[debug] sys.path={sys.path}")
            traceback.print_exception(exc)
        return False


def _live_gateway_python() -> list:
    """The environment the RUNNING gateway maps: the core that matters right now."""
    try:
        pid = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "hermes-gateway"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        if pid and pid != "0":
            import re
            maps = Path(f"/proc/{pid}/maps").read_text(errors="replace")
            envs = sorted(set(re.findall(r"(/[^\s]*/installs/[0-9a-f]+/environments/[0-9a-f]+)/", maps)))
            return [f"{e}/venv/bin/python" for e in envs if os.path.exists(f"{e}/venv/bin/python")]
    except Exception:
        pass
    return []


def _reexec_under_runtime() -> None:
    if os.environ.get("DELEGATE_LANES_PYTHON"):
        # Explicit interpreter = strict: never fall back to some other Hermes install, which would
        # test a different core than the one asked for and still print a green result.
        cands = [os.environ["DELEGATE_LANES_PYTHON"]]
    else:
        cands = _live_gateway_python()
        cands += sorted(glob.glob(os.path.expanduser("~/.hermes/installs/*/environments/*/venv/bin/python")),
                    key=lambda p: os.path.getmtime(os.path.dirname(os.path.dirname(p))), reverse=True)
    probe = "import tools.delegate_tool, agent.tool_guardrails"
    env = dict(os.environ)
    if env.get("HERMES_CORE_DIR"):
        probe = f"import sys; sys.path.insert(0, {env['HERMES_CORE_DIR']!r}); " + probe
    for py in cands:
        if subprocess.run([py, "-c", probe], capture_output=True, env=env, check=False).returncode == 0:
            env[_REEXEC_FLAG] = "1"
            env.pop("PYTHONPATH", None)
            if os.environ.get("DL_DEBUG"):
                print(f"[debug] re-exec -> {py}")
            os.execve(py, [py, __file__, *sys.argv[1:]], env)
    print("FAIL: no interpreter can import Hermes core (set DELEGATE_LANES_PYTHON / HERMES_CORE_DIR)")
    sys.exit(2)


if not _core_importable():
    if os.environ.get(_REEXEC_FLAG):
        print(f"FAIL: re-exec'd interpreter {sys.executable} still cannot import Hermes core: {_IMPORT_ERROR!r}")
        traceback.print_exception(_IMPORT_ERROR)
        sys.exit(2)
    _reexec_under_runtime()


def _load_plugin():
    name = "delegate_lanes_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    assert spec is not None and spec.loader is not None, f"cannot load plugin from {PLUGIN_DIR}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


P = _load_plugin()
lanes_mod = sys.modules["delegate_lanes_under_test.lanes"]
audit = sys.modules["delegate_lanes_under_test.audit"]
contract = sys.modules["delegate_lanes_under_test.contract"]

RESULTS = []


def check(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


class _Patch:
    """Minimal monkeypatch: set attrs, restore on exit."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, attr, value):
        self._undo.append((obj, attr, getattr(obj, attr, _MISSING)))
        setattr(obj, attr, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for obj, attr, old in reversed(self._undo):
            if old is _MISSING:
                delattr(obj, attr)
            else:
                setattr(obj, attr, old)


_MISSING = object()


def _write_profile(home: Path, name: str, model: dict) -> None:
    from hermes_yaml import safe_dump
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(safe_dump({"model": model}))


SETTINGS = {
    "lanes": {
        "scout": {"provider": "zai", "model": "glm-5.3-flash", "use_for": "cheap recon",
                  "fallbacks": [{"provider": "anthropic", "model": "claude-haiku-5-5"}]},
        "reviewer": {"provider": "openai-codex", "model": "gpt-6.1-sol", "use_for": "adversarial review"},
        "builder": {"from_profile": "builder", "use_for": "implementation"},
    },
    "models": [{"provider": "anthropic", "model": "claude-sonnet-5-5", "use_for": "drafting"}],
}


# ----------------------------------------------------------------------------- lane parsing
@check("lanes: merge named lanes + models allowlist; from_profile imports names only")
def t_parse():
    home = Path(tempfile.mkdtemp())
    _write_profile(home, "builder", {"default": "claude-opus-5-5", "provider": "anthropic",
                                     "base_url": "https://evil.example/v1"})
    t = lanes_mod.load_lanes(SETTINGS, home)
    assert not t.errors, t.errors
    assert t.names() == ["builder", "claude-sonnet-5-5", "reviewer", "scout"], t.names()
    b = t.lanes["builder"]
    assert (b.provider, b.model, b.base_url) == ("anthropic", "claude-opus-5-5", ""), b
    assert b.source == "profile:builder"
    assert any("base_url" in w and "builder" in w for w in t.warnings), t.warnings
    assert t.lanes["scout"].allowed_routes() == (("zai", "glm-5.3-flash"), ("anthropic", "claude-haiku-5-5"))


@check("lanes: validation rejects unsafe/invalid specs")
def t_validate():
    home = Path(tempfile.mkdtemp())
    bad = {"lanes": {
        "noenv": {"provider": "custom", "model": "m", "base_url": "https://x.example/v1"},
        "overr": {"provider": "zai", "model": "m", "request_overrides": {"a": 1}},
        "Bad Name": {"provider": "zai", "model": "m"},
        "nomodel": {"provider": "zai"},
        "ghost": {"from_profile": "does-not-exist"},
        "mode": {"provider": "zai", "model": "m", "api_mode": "telepathy", "base_url": "https://x/v1", "api_key_env": "K"},
    }, "models": [{"provider": "zai", "model": "x/glm"}, {"provider": "zai", "model": "y/glm"}]}
    t = lanes_mod.load_lanes(bad, home)
    joined = "\n".join(t.errors)
    for needle in ("noenv", "overr", "Bad Name", "nomodel", "ghost", "mode", "already defined"):
        assert needle in joined, f"missing error for {needle}: {joined}"
    assert set(t.names()) == {"glm"}, t.names()


@check("lanes: credentials_cfg shape; base_url lane needs its own key")
def t_creds():
    lane = lanes_mod.Lane(name="s", provider="zai", model="glm-5.3-flash",
                          fallbacks=(("anthropic", "claude-haiku-5-5"),))
    cfg = lane.credentials_cfg({})
    assert cfg == {"provider": "zai", "model": "glm-5.3-flash",
                   "fallback_providers": [{"provider": "anthropic", "model": "claude-haiku-5-5"}]}, cfg
    assert lanes_mod.Lane(name="n", provider="zai", model="m").credentials_cfg({})["fallback_providers"] == []
    d = lanes_mod.Lane(name="d", provider="custom", model="m", base_url="https://or.example/v1", api_key_env="OR_KEY",
                       api_mode="chat_completions")
    assert d.credentials_cfg({"OR_KEY": "k1"})["api_key"] == "k1"
    try:
        d.credentials_cfg({})
        raise AssertionError("missing api_key_env must refuse")
    except lanes_mod.LaneConfigError:
        pass


@check("lanes: verify_resolvable is advisory by default, disables failing lanes only in strict mode")
def t_verify():
    t = lanes_mod.load_lanes({"lanes": {"a": {"provider": "ok", "model": "m"}, "b": {"provider": "boom", "model": "m"},
                                        "c": {"provider": "nokey", "model": "m"}}}, Path(tempfile.mkdtemp()))

    def resolver(requested, target_model):
        if requested == "boom":
            raise RuntimeError("no such provider")
        return {"api_key": "" if requested == "nokey" else "k", "base_url": "https://x"}
    import copy
    t_warn = copy.deepcopy(t)
    lanes_mod.verify_resolvable(t_warn, resolver)  # default advisory mode keeps every lane
    assert t_warn.names() == ["a", "b", "c"] and not t_warn.errors, (t_warn.names(), t_warn.errors)
    assert any("boom" in w for w in t_warn.warnings) and any("nokey" in w for w in t_warn.warnings)
    lanes_mod.verify_resolvable(t, resolver, strict=True)
    assert t.names() == ["a"], t.names()
    assert any("boom" in e for e in t.errors) and any("nokey" in e for e in t.errors)


# ----------------------------------------------------------------------------- contract
def _controller(cap):
    from agent.tool_guardrails import LoopCapConfig, ToolCallGuardrailConfig, ToolCallGuardrailController
    c = ToolCallGuardrailController(ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_subagents=cap)))
    c.reset_for_turn()
    return c


def _cap_is_shared() -> bool:
    """True iff plain + lane spawns charge ONE per-turn budget (cap 3: 2 plain + 2 lane, then 1 more = blocked)."""
    c = _controller(3)
    a = c.before_call("delegate_task", {"tasks": [{"goal": "a"}, {"goal": "b"}]})
    b = c.before_call(P.TOOL, {"lane": "scout", "tasks": [{"goal": "c"}, {"goal": "d"}]})
    d = c.before_call("delegate_task", {"tasks": [{"goal": "e"}]})
    return a.allows_execution and b.allows_execution and not d.allows_execution


@check("contract: re-attachment applies and verifies (cap, deadline, guardrail sets)")
def t_contract_apply():
    report = contract.reattach(P.TOOL)
    assert len(report) == 4 and any(r.startswith("round_cap") for r in report), report
    v = contract.verify(P.TOOL)
    assert all(v.values()), v


@check("contract: per-turn spawn cap is SHARED between delegate_task and delegate_lane")
def t_cap_shared():
    contract.reattach(P.TOOL)
    assert _cap_is_shared()


@check("MUTATION: without the loop-cap re-attachment, lane spawns evade the cap (check must go red)")
def t_cap_mutation():
    import agent.tool_guardrails as g
    contract.reattach(P.TOOL)
    with _Patch() as mp:
        caps = dict(g._LOOP_CAPS)
        caps.pop(P.TOOL)
        mp.setattr(g, "_LOOP_CAPS", caps)
        assert not _cap_is_shared(), "cap check failed to detect a missing re-attachment"


@check("contract: deadline exemption is looked up at call time and includes delegate_lane")
def t_deadline():
    import inspect

    import agent.tool_executor as te
    contract.reattach(P.TOOL)
    assert P.TOOL in te._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS
    fns = [fn for _, fn in inspect.getmembers(te, inspect.isfunction)
           if fn.__module__ == te.__name__ and "in _SEQUENTIAL_DEADLINE_EXEMPT_TOOLS" in inspect.getsource(fn)]
    assert fns, "no call-time lookup of _SEQUENTIAL_DEADLINE_EXEMPT_TOOLS"
    with _Patch() as mp:
        mp.setattr(te, "_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS", frozenset({"delegate_task"}))
        assert not contract.verify(P.TOOL)["deadline_exempt"], "MUTATION not detected"


@check("contract: a moved core seam raises ContractError (fail closed)")
def t_contract_seam_moved():
    import agent.tool_guardrails as g
    with _Patch() as mp:
        mp.setattr(g, "_LOOP_CAPS", {"web_search": g._LOOP_CAPS["web_search"]})
        try:
            contract.reattach(P.TOOL)
            raise AssertionError("expected ContractError")
        except contract.ContractError:
            pass


# ----------------------------------------------------------------------------- end-to-end route
class _FakeChild:
    instances: ClassVar[list] = []

    def __init__(self, **kw):
        self.kw = kw
        self.session_id = f"child-{len(_FakeChild.instances)}"
        self.model = kw.get("model")
        self.provider = kw.get("provider")
        self.tool_progress_callback = kw.get("tool_progress_callback")
        _FakeChild.instances.append(self)

    def __getattr__(self, item):  # tolerate any other attribute core touches
        if item.startswith("__"):
            raise AttributeError(item)


def _fake_parent(depth=1):
    return SimpleNamespace(
        model="claude-opus-5-5", provider="anthropic", base_url="https://api.anthropic.com", api_key="parent-key",
        api_mode="anthropic_messages", session_id="parent-sess", _delegate_depth=depth, enabled_toolsets=["delegation", "file"],
        disabled_toolsets=[], reasoning_config={"effort": "high"}, request_overrides={}, max_tokens=8000,
        _fallback_chain=[], _credential_pool=None, platform="cli", _session_db=None, prefill_messages=None,
        _print_fn=None, acp_command=None, acp_args=[], requested_provider="anthropic", capabilities={},
        _current_turn_id="turn-1", _subagent_id=None, _client_kwargs={}, client=None, tool_progress_callback=None,
        valid_tool_names=set(), tools=[],
    )


def _run_lane(lane_name, table, parent, credentials_override=None, resolver=None):
    """Drive the REAL handler -> delegate_task -> credential resolution -> child construction,
    with only the model-calling child replaced and hooks routed to the plugin."""
    import hermes_cli.runtime_provider as rp
    import run_agent
    import tools.delegate_tool as dt
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_cli import lifecycle

    def fake_resolve(requested=None, target_model=None, **_):
        return {"provider": requested, "api_key": f"key-{requested}", "base_url": f"https://{requested}.example/v1",
                "api_mode": "chat_completions", "request_overrides": {}, "model": target_model}

    def fake_run_single_child(task_index, goal, child=None, parent_agent=None, **_):
        assert child is not None and parent_agent is not None
        lifecycle.invoke_hook("subagent_stop", parent_session_id=parent_agent.session_id,
                              child_session_id=child.session_id, child_status="completed")
        return {"task_index": task_index, "status": "completed", "summary": f"done {goal}",
                "model": child.model, "duration_seconds": 0.0, "api_calls": 1}

    def fake_invoke_hook(name, **kw):
        if name == "subagent_start":
            P._on_subagent_start(**kw)
        elif name == "subagent_stop":
            P._on_subagent_stop(**kw)
        return []

    _FakeChild.instances.clear()
    with _Patch() as mp:
        mp.setattr(run_agent, "AIAgent", _FakeChild)
        mp.setattr(rp, "resolve_runtime_provider", resolver or fake_resolve)
        mp.setattr(dt, "_open_child_session_db", lambda parent_agent: None)
        mp.setattr(dt, "_run_single_child", fake_run_single_child)
        mp.setattr(lifecycle, "invoke_hook", fake_invoke_hook)
        if credentials_override is not None:
            mp.setattr(lanes_mod.Lane, "credentials_cfg", lambda self, environ=None: credentials_override)
        P._STATE["table"] = table
        P._STATE["contract_ok"] = True
        with bind_subagent_parent(parent):
            out = json.loads(P.handle({"lane": lane_name, "tasks": [{"goal": "survey module alpha for the test"}, {"goal": "survey module beta for the test", "acp_command": "rm"}]}))
    return out, list(_FakeChild.instances)


def _table():
    home = Path(tempfile.mkdtemp())
    _write_profile(home, "builder", {"default": "claude-opus-5-5", "provider": "anthropic"})
    return lanes_mod.load_lanes(SETTINGS, home)


def _children_on(children, provider, model):
    return bool(children) and all(c.kw.get("provider") == provider and c.kw.get("model") == model for c in children)


@check("route: real delegate_task builds every child on the lane's provider/model (not parent, not pin)")
def t_route_e2e():
    contract.reattach(P.TOOL)
    parent = _fake_parent(depth=1)  # depth>0 -> synchronous join, fully hermetic
    out, kids = _run_lane("scout", _table(), parent)
    assert len(kids) == 2, (out, len(kids))
    assert _children_on(kids, "zai", "glm-5.3-flash"), [(c.kw.get("provider"), c.kw.get("model")) for c in kids]
    assert all(c.kw.get("base_url") == "https://zai.example/v1" for c in kids)
    fb = kids[0].kw.get("fallback_model") or []
    assert [(f.get("provider"), f.get("model")) for f in fb] == [("anthropic", "claude-haiku-5-5")], fb
    rc = kids[0].kw.get("reasoning_config")
    assert rc and "high" in json.dumps(rc).lower(), f"global delegation.reasoning_effort=high did not reach the child: {rc}"
    assert parent.model == "claude-opus-5-5" and parent.provider == "anthropic", "parent route mutated"
    assert out["lane"]["name"] == "scout" and out["lane"]["dropped_task_keys"] == ["acp_command"], out["lane"]
    assert len(out["lane"]["child_sessions"]) == 2, out["lane"]


@check("MUTATION: route replaced by global pin/inherit is detected (children land on the parent route)")
def t_route_mutation():
    contract.reattach(P.TOOL)
    _, kids = _run_lane("scout", _table(), _fake_parent(depth=1), credentials_override={})
    assert kids and not _children_on(kids, "zai", "glm-5.3-flash"), "route check could not detect a lost route"


# ----------------------------------------------------------------------------- handler semantics
def _capture_delegate(mp):
    import tools.delegate_tool as dt
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return json.dumps({"status": "dispatched"})
    mp.setattr(dt, "delegate_task", fake)
    return seen


@check("handler: background at depth 0, synchronous at depth>0; unknown lane / no parent refused")
def t_handler_modes():
    from agent.subagent_lifecycle import bind_subagent_parent
    P._STATE["table"] = _table()
    P._STATE["contract_ok"] = True
    with _Patch() as mp:
        seen = _capture_delegate(mp)
        with bind_subagent_parent(_fake_parent(depth=0)):
            P.handle({"lane": "reviewer", "tasks": [{"goal": "x"}]})
        assert seen["background"] is True and seen["credentials_cfg"]["model"] == "gpt-6.1-sol", seen
        with bind_subagent_parent(_fake_parent(depth=1)):
            P.handle({"lane": "reviewer", "tasks": [{"goal": "x"}]})
        assert seen["background"] is False, seen
        with bind_subagent_parent(_fake_parent(depth=0)):
            assert "Unknown lane" in P.handle({"lane": "nope", "tasks": [{"goal": "x"}]})
            assert "non-empty" in P.handle({"lane": "scout", "tasks": []})
        assert "No active Hermes parent" in P.handle({"lane": "scout", "tasks": [{"goal": "x"}]})


@check("binding: parent visible through propagate_context_to_thread; MUTATION plain thread loses it")
def t_binding_threads():
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.thread_context import propagate_context_to_thread
    P._STATE["table"] = _table()
    P._STATE["contract_ok"] = True
    out = {}
    with _Patch() as mp:
        _capture_delegate(mp)
        with bind_subagent_parent(_fake_parent(depth=0)):
            th = threading.Thread(target=propagate_context_to_thread(
                lambda: out.__setitem__("ok", P.handle({"lane": "scout", "tasks": [{"goal": "x"}]}))))
            th.start()
            th.join()
            th2 = threading.Thread(target=lambda: out.__setitem__("bare", P.handle({"lane": "scout", "tasks": [{"goal": "x"}]})))
            th2.start()
            th2.join()
    assert "dispatched" in out["ok"], out["ok"]
    assert "No active Hermes parent" in out["bare"], "MUTATION not detected: a context-less thread still found a parent"


# ----------------------------------------------------------------------------- toolset exposure
@check("exposure: leaf children never see delegate_lane; orchestrators do")
def t_leaf_strip():
    import model_tools
    from tools.delegate_tool_toolsets import _resolve_child_toolsets
    from tools.registry import registry
    contract.reattach(P.TOOL)
    table = _table()
    P._STATE["table"] = table
    registry.register(name=P.TOOL, toolset="delegation", schema=P._schema(table), handler=P.handle,
                      check_fn=lambda: True)
    try:
        parent = _fake_parent(depth=0)
        names = {}
        for role in ("leaf", "orchestrator"):
            en, dis = _resolve_child_toolsets(parent, None, role)
            defs = model_tools.get_tool_definitions(enabled_toolsets=en, disabled_toolsets=dis, quiet_mode=True,
                                                    skip_tool_search_assembly=True)
            names[role] = {d.get("function", d).get("name") for d in defs}
        assert P.TOOL not in names["leaf"], sorted(names["leaf"])
        assert P.TOOL in names["orchestrator"], sorted(names["orchestrator"])
        out = registry.dispatch(P.TOOL, {"lane": "nope", "tasks": [{"goal": "x"}]}, task_id="t", session_id="s")
        assert "Unknown lane" in str(out), out
    finally:
        try:
            registry.deregister(P.TOOL)
        except Exception:
            pass


# ----------------------------------------------------------------------------- route audit
def _usage_db(rows):
    db = Path(tempfile.mkdtemp()) / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, "
                "billing_base_url TEXT, billing_mode TEXT, task TEXT, api_call_count INTEGER)")
    con.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


@check("audit: primary + declared fallback pass; foreign model flagged; aux rows ignored; no rows = unverified")
def t_audit():
    lane = _table().lanes["scout"]
    db = _usage_db([
        ("ok1", "glm-5.3-flash", "zai", "https://api.z.ai/api/coding/paas/v4/", "", "", 3),
        ("ok1", "qwen3.8", "qwen38-local", "http://x", "", "title_generation", 1),
        ("fb", "claude-haiku-5-5", "anthropic", "https://api.anthropic.com", "", "", 2),
        ("bad", "claude-opus-5-5", "anthropic", "https://api.anthropic.com", "", "", 5),
    ])
    assert audit.audit_session(lane, "ok1", db)["ok"] is True
    assert audit.audit_session(lane, "fb", db)["ok"] is True
    bad = audit.audit_session(lane, "bad", db)
    assert bad["ok"] is False and bad["mismatched"] == ["anthropic/claude-opus-5-5"], bad
    assert audit.audit_session(lane, "none", db)["ok"] is None


@check("audit: start/stop hooks correlate children to the calling lane; mismatch queued for the parent")
def t_audit_flow():
    audit._reset_for_tests()
    lane = _table().lanes["scout"]
    db = _usage_db([("c-bad", "claude-opus-5-5", "anthropic", "", "", "", 1)])
    scope = audit.CallScope(lane)
    tok = audit.CURRENT_LANE.set(scope)
    try:
        assert audit.record_child_start("c-bad", "sa-1", "parent-x")
    finally:
        audit.CURRENT_LANE.reset(tok)
    assert not audit.record_child_start("c-plain", "sa-2", "parent-x"), "a non-lane child must not be tracked"
    assert scope.children == ["c-bad"]
    v = audit.audit_child_stop("c-bad", "completed", db)
    assert v["ok"] is False
    assert audit.audit_child_stop("c-plain", "completed", db) is None
    pend = audit.take_pending("parent-x")
    assert len(pend) == 1 and pend[0]["child_session_id"] == "c-bad"
    assert audit.take_pending("parent-x") == []


@check("audit: a queued background mismatch is injected into the parent's next turn exactly once")
def t_pre_llm_notice():
    audit._reset_for_tests()
    lane = _table().lanes["scout"]
    db = _usage_db([("c-bg", "claude-opus-5-5", "anthropic", "", "", "", 1)])
    scope = audit.CallScope(lane)
    tok = audit.CURRENT_LANE.set(scope)
    try:
        audit.record_child_start("c-bg", "sa-9", "parent-bg")
    finally:
        audit.CURRENT_LANE.reset(tok)
    audit.audit_child_stop("c-bg", "completed", db)
    assert P._on_pre_llm_call(session_id="someone-else") is None, "leaked into another session"
    got = P._on_pre_llm_call(session_id="parent-bg")
    assert got and "ROUTE MISMATCH" in got["context"] and "anthropic/claude-opus-5-5" in got["context"], got
    assert P._on_pre_llm_call(session_id="parent-bg") is None, "notice repeated"
    # MUTATION: if the stop hook never queues, the parent must hear nothing (the check above is what proves delivery)
    audit._reset_for_tests()
    assert P._on_pre_llm_call(session_id="parent-bg") is None



# ----------------------------------------------------------------------------- hardening checks
def _calls(*names):
    return [SimpleNamespace(function=SimpleNamespace(name=n)) for n in names]


def _round_cap_counts_lanes() -> bool:
    """max_concurrent_children=10 (hermetic config): 6 plain + 6 lane + 1 other -> 10 delegation kept."""
    import run_agent
    out = run_agent.AIAgent._cap_delegate_task_calls(_calls(*(["delegate_task"] * 6 + [P.TOOL] * 6 + ["read_file"])))
    kept = sum(1 for c in out if c.function.name in ("delegate_task", P.TOOL))
    return kept == 10 and out[-1].function.name == "read_file"


@check("contract: per-ROUND call cap counts delegate_task + delegate_lane together")
def t_round_cap():
    contract.reattach(P.TOOL)
    assert _round_cap_counts_lanes()


@check("MUTATION: core's original round cap (name-keyed) lets lane calls through (check must go red)")
def t_round_cap_mutation():
    import run_agent
    contract.reattach(P.TOOL)
    cur = run_agent.AIAgent.__dict__["_cap_delegate_task_calls"].__func__
    with _Patch() as mp:
        mp.setattr(run_agent.AIAgent, "_cap_delegate_task_calls", staticmethod(getattr(cur, contract._CAP_ORIG_ATTR)))
        assert not _round_cap_counts_lanes(), "round-cap check failed to detect the name-keyed original"


@check("contract: detach is refcounted per owner and restores core exactly; reattach is idempotent")
def t_detach_refcount():
    import run_agent
    contract.reattach(P.TOOL, "A")
    contract.reattach(P.TOOL, "B")
    contract.reattach(P.TOOL, "B")
    try:
        assert contract.detach(P.TOOL, "A") is False and all(contract.verify(P.TOOL).values())
        for owner in ("default", P.OWNER):
            contract.detach(P.TOOL, owner)
        assert contract.detach(P.TOOL, "B") is True
        v = contract.verify(P.TOOL)
        assert not any(v.values()), v
        fn = run_agent.AIAgent.__dict__["_cap_delegate_task_calls"].__func__
        assert not hasattr(fn, contract._CAP_NAMES_ATTR), "original round cap not restored"
    finally:
        contract.reattach(P.TOOL)


@check("contract: per-turn drift check re-applies; a moved seam makes the handler refuse")
def t_drift():
    import agent.tool_guardrails as g
    from agent.subagent_lifecycle import bind_subagent_parent
    contract.reattach(P.TOOL)
    P._STATE["table"] = _table()
    g._LOOP_CAPS.pop(P.TOOL)
    assert P._ensure_contract() is True and contract.verify(P.TOOL)["loop_cap_shared"], "drift not healed"
    with _Patch() as mp:
        mp.setattr(g, "_LOOP_CAPS", {"web_search": g._LOOP_CAPS["web_search"]})
        assert P._ensure_contract() is False
        with bind_subagent_parent(_fake_parent(depth=0)):
            assert "disabled" in P.handle({"lane": "scout", "tasks": [{"goal": "survey the thing for test"}]})
    contract.reattach(P.TOOL)
    assert P._ensure_contract() is True


@check("audit: strict matching (full model id + provider); blank provider = unverified, not ok")
def t_strict_match():
    allowed = (("openrouter", "meta-llama/llama-4"),)
    assert audit.route_status(allowed, "openrouter", "meta-llama/llama-4") is True
    assert audit.route_status(allowed, "openrouter", "openrouter/meta-llama/llama-4") is True
    assert audit.route_status(allowed, "evil", "x/llama-4") is False, "endpoint-blind last-segment match"
    assert audit.route_status(allowed, "openrouter", "x/llama-4") is False
    assert audit.route_status(allowed, "", "meta-llama/llama-4") is None
    lane = _table().lanes["scout"]
    db = _usage_db([("blank", "glm-5.3-flash", "", "", "", "", 1)])
    assert audit.audit_session(lane, "blank", db)["ok"] is None


@check("audit: a no-rows read is retried before a child is called unverified")
def t_audit_retry():
    lane = _table().lanes["scout"]
    calls = {"n": 0}

    def flaky(session_id, db_path=None):
        calls["n"] += 1
        return [] if calls["n"] < 2 else [{"model": "glm-5.3-flash", "provider": "zai", "base_url": "", "api_calls": 1}]
    with _Patch() as mp:
        mp.setattr(audit, "usage_rows", flaky)
        mp.setattr(audit, "_USAGE_RETRY_DELAYS", (0, 0))
        assert audit.audit_session(lane, "late", retry=True)["ok"] is True
        calls["n"] = 0
        assert audit.audit_session(lane, "late", retry=False)["ok"] is None  # MUTATION: no retry -> unverified


@check("audit: pending mismatch queue is bounded (parents and per parent)")
def t_pending_bounded():
    audit._reset_for_tests()
    with _Patch() as mp:
        mp.setattr(audit, "_MAX_PENDING_PARENTS", 8)
        mp.setattr(audit, "_MAX_PENDING_PER_PARENT", 3)
        with audit._LOCK:
            for i in range(40):
                audit._queue_pending(f"p{i}", {"ts": i, "ok": False})
            for i in range(10):
                audit._queue_pending("hot", {"ts": 100 + i, "ok": False})
        assert len(audit._PENDING) <= 8, len(audit._PENDING)
        assert len(audit._PENDING["hot"]) == 3
    audit._reset_for_tests()


@check("lanes: base_url key is read through the profile-scoped reader, not raw os.environ")
def t_scoped_env():
    import hermes_cli.config as hc
    lane = lanes_mod.Lane(name="d", provider="custom", model="m", base_url="https://or.example/v1",
                          api_key_env="DL_TEST_KEY", api_mode="chat_completions")
    with _Patch() as mp:
        mp.setattr(hc, "get_env_value", lambda k: "scoped-key" if k == "DL_TEST_KEY" else None)
        os.environ["DL_TEST_KEY"] = "global-other-profile-key"
        try:
            assert lane.credentials_cfg()["api_key"] == "scoped-key"
        finally:
            os.environ.pop("DL_TEST_KEY", None)


@check("route: unresolvable lane provider -> loud error, no child built (real delegate_task path)")
def t_unresolvable_provider():
    contract.reattach(P.TOOL)

    def no_creds(requested=None, target_model=None, **_):
        raise RuntimeError(f"no credentials for provider {requested}")
    out, kids = _run_lane("scout", _table(), _fake_parent(depth=1), resolver=no_creds)
    assert not kids, f"{len(kids)} child(ren) built despite unresolvable provider"
    assert "error" in json.dumps(out).lower(), out

# ----------------------------------------------------------------------------- registration
class _FakeCtx:
    def __init__(self, settings):
        self.settings, self.tools, self.hooks = settings, [], []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kw):
        self.tools.append(kw)

    def register_hook(self, name, cb):
        self.hooks.append(name)


# ----------------------------------------------------------------------------- misroute guard
# Observed in production: with tool_search active, delegate_lane is a DEFERRED tool. A model that called it
# by name got core's fuzzy name-repair (turn_tool_validation -> repair_tool_call) to rewrite the call into
# delegate_task, which silently ignored `lane` and ran the children on the parent's model. The route audit
# never saw it, because delegate_lane never ran.
def _dispatch_with(hooks, name, args):
    """Core's real pre_tool_call path (_dispatch_pre_tool_call_hooks -> lifecycle -> PluginManager.invoke_hook)
    with a fresh manager whose only registered callbacks are `hooks`."""
    import hermes_cli.plugins as hp
    mgr = hp.PluginManager()
    mgr._hooks = {"pre_tool_call": list(hooks)}
    with _Patch() as mp:
        mp.setattr(hp, "_delivery_manager", lambda: mgr)
        return hp._dispatch_pre_tool_call_hooks(name, args, session_id="s-guard", tool_call_id="c-1")


@check("guard: a lane request rewritten to delegate_task is BLOCKED via core's real hook path, not silently misrouted")
def t_guard_blocks_lane_on_delegate_task():
    from agent.agent_runtime_helpers import repair_tool_call
    P._STATE["table"] = _table()
    # Premise (informational): core's repair still maps the deferred name onto delegate_task.
    repaired = repair_tool_call(SimpleNamespace(valid_tool_names={"delegate_task", "terminal", "tool_call"}), P.TOOL)
    if repaired != "delegate_task":
        print(f"    note: core no longer repairs {P.TOOL!r} -> delegate_task (got {repaired!r}); guard stays as defence")
    args = {"lane": "scout", "tasks": '[{"goal": "survey"}]'}
    block, _ = _dispatch_with([P._on_pre_tool_call], "delegate_task", args)
    assert block and "delegate_lane" in block and "tool_call" in block and "scout" in block, block
    # MUTATION: without the guard, core lets the lane-carrying delegate_task through (the field bug).
    block0, _ = _dispatch_with([], "delegate_task", args)
    assert block0 is None, "premise broken: core itself now blocks; re-evaluate the guard"
    # Unknown lane is named as such; available lanes listed.
    blk, _ = _dispatch_with([P._on_pre_tool_call], "delegate_task", {"lane": "nope", "tasks": []})
    assert blk and "Unknown lane" in blk and "scout" in blk, blk


@check("guard: never touches legitimate calls (plain delegate_task, steer, other tools, empty lane)")
def t_guard_passthrough():
    P._STATE["table"] = _table()
    for name, args in [("delegate_task", {"tasks": [{"goal": "x"}]}),
                       ("delegate_task", {"action": "steer", "subagent_id": "sa-1", "message": "m"}),
                       ("delegate_task", {"lane": "", "goal": "x"}),
                       ("delegate_task", {"lane": None, "goal": "x"}),
                       ("terminal", {"lane": "scout", "command": "true"}),
                       (P.TOOL, {"lane": "scout", "tasks": [{"goal": "x"}]})]:
        block, mod = _dispatch_with([P._on_pre_tool_call], name, args)
        assert block is None and mod is None, (name, args, block)


@check("guard: a blocked misroute is logged to delegate-lanes.jsonl")
def t_guard_logged():
    P._STATE["table"] = _table()
    before = audit.log_path().read_text().count("lane_on_delegate_task") if audit.log_path().exists() else 0
    P._on_pre_tool_call(tool_name="delegate_task", args={"lane": "scout", "goal": "g"}, session_id="s-log")
    after = audit.log_path().read_text().count("lane_on_delegate_task")
    assert after == before + 1, (before, after)


@check("handler: tasks given as a JSON string (as models emit for delegate_task) are accepted")
def t_tasks_json_string():
    clean, dropped, problem = P._clean_tasks('[{"goal": "a", "context": "c", "acp_command": "x"}]')
    assert problem is None and clean == [{"goal": "a", "context": "c"}] and dropped == ["acp_command"], (clean, dropped, problem)
    _, _, problem2 = P._clean_tasks("not json")
    assert problem2, "garbage string accepted"


@check("register: fails closed on a moved seam and on an empty lane table")
def t_register_fail_closed():
    ctx = _FakeCtx({"lanes": {}, "models": [], "verify_providers": "off"})
    P.register(ctx)
    assert ctx.tools == [], "registered with no lanes"
    with _Patch() as mp:
        def boom(tool, owner="default"):
            raise contract.ContractError("seam moved")
        mp.setattr(contract, "reattach", boom)
        ctx2 = _FakeCtx({"lanes": {"scout": SETTINGS["lanes"]["scout"]}, "verify_providers": "off"})
        P.register(ctx2)
        assert ctx2.tools == [], "registered despite a broken contract"
    ctx3 = _FakeCtx({"lanes": {"scout": SETTINGS["lanes"]["scout"]}, "verify_providers": "off"})
    P.register(ctx3)
    assert [t["name"] for t in ctx3.tools] == [P.TOOL] and ctx3.tools[0]["toolset"] == "delegation"
    assert set(ctx3.hooks) == {"subagent_start", "subagent_stop", "pre_llm_call", "pre_tool_call"}
    assert P._STATE["contract_ok"] is True


def main() -> int:
    import tools.delegate_tool as dt
    print(f"core under test: {dt.__file__}")
    print(f"interpreter:     {sys.executable}")
    expect = os.environ.get("DELEGATE_LANES_EXPECT_CORE")
    if expect and not os.path.realpath(dt.__file__).startswith(os.path.realpath(expect) + os.sep):
        print(f"FAIL: core under test is not under DELEGATE_LANES_EXPECT_CORE={expect}")
        return 2
    failed = 0
    for name, fn in RESULTS:
        try:
            fn()
            print(f"  [OK ] {name}")
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
