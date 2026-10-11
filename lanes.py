"""Lane configuration: parse, validate and turn operator-defined lanes into delegate_task routes.

Two sources, merged into one name -> Lane table:

  plugins.entries.delegate-lanes.settings.lanes   (named lanes, dict)
      scout:    {provider: zai, model: glm-5.3-flash, use_for: "...", fallbacks: [{provider, model}]}
      builder:  {from_profile: builder, use_for: "..."}
  plugins.entries.delegate-lanes.settings.models  (flat allowlist, list)
      - {provider: anthropic, model: claude-sonnet-5-5, use_for: "...", name: sonnet}

Every lane is an explicit (provider, model) pair the operator approved. The model-facing tool may
choose only a lane NAME from this table, never a raw model or endpoint.

Rules:
  * provider + model are required (after from_profile resolution).
  * from_profile imports model/provider NAMES only. Credentials always resolve in the active
    profile; a profile's base_url/api_mode is NOT carried over (it would pair a foreign endpoint with
    this profile's key).
  * A direct base_url REQUIRES api_key_env. delegate_task's base_url branch otherwise inherits the
    PARENT's key and sends it to the foreign endpoint.
  * request_overrides / reasoning per lane are rejected in v1 (effort is global by design).
  * fallbacks are explicit. An empty list means no fallback (fail loudly rather than switch models).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("delegate_lanes")

LANE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_LANE_KEYS = {"provider", "model", "use_for", "fallbacks", "from_profile", "base_url", "api_key_env", "api_mode"}
_MODEL_ENTRY_KEYS = _LANE_KEYS | {"name"}
_REJECTED_KEYS = {"request_overrides", "reasoning_effort", "api_key", "command", "args", "acp_command", "acp_args"}
_API_MODES = {"chat_completions", "codex_responses", "anthropic_messages"}


@dataclass(frozen=True)
class Lane:
    name: str
    provider: str
    model: str
    use_for: str = ""
    fallbacks: Tuple[Tuple[str, str], ...] = ()
    base_url: str = ""
    api_key_env: str = ""
    api_mode: str = ""
    source: str = "lanes"

    def allowed_routes(self) -> Tuple[Tuple[str, str], ...]:
        """(provider, model) pairs a child of this lane may legitimately run on."""
        return ((self.provider, self.model),) + tuple(self.fallbacks)

    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def credentials_cfg(self, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        """The per-call routing owner for delegate_task(credentials_cfg=...).

        Shape matches the `delegation` config section, which is what
        tools.delegate_tool_config._resolve_delegation_credentials reads. The fallback chain is ALWAYS
        set explicitly (possibly []), so a lane never borrows the parent's or the global chain.
        """
        cfg: Dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "fallback_providers": [{"provider": p, "model": m} for p, m in self.fallbacks],
        }
        if self.base_url:
            key = (_env_get(self.api_key_env, environ) or "").strip()
            if not key:
                raise LaneConfigError(
                    f"lane '{self.name}': api_key_env {self.api_key_env!r} is empty or unset in this process; "
                    f"refusing to call {self.base_url} (it would otherwise inherit the parent's key)."
                )
            cfg["base_url"] = self.base_url
            cfg["api_key"] = key
            if self.api_mode:
                cfg["api_mode"] = self.api_mode
        return cfg


class LaneConfigError(ValueError):
    pass


def _env_get(key: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Read a lane's api_key_env. Explicit mapping wins (tests). Otherwise the PROFILE-SCOPED reader
    hermes_cli.config.get_env_value: on the multiplexed gateway raw os.environ is process-global and
    may hold another profile's value; get_env_value is scope-checked and
    falls back to the active home's .env. os.environ only if Hermes core is not importable."""
    if environ is not None:
        return environ.get(key)
    try:
        from hermes_cli.config import get_env_value
        return get_env_value(key)
    except ImportError:
        return os.environ.get(key)


@dataclass
class LaneTable:
    lanes: Dict[str, Lane] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def names(self) -> List[str]:
        return sorted(self.lanes)


def _slug(model: str) -> str:
    base = model.rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", base).strip("-")[:32] or "lane"


def _read_profile_model(hermes_home: Path, profile: str) -> Tuple[str, str, Dict[str, Any]]:
    try:  # Hermes's own YAML layer (ruamel); PyYAML is NOT a Hermes dependency
        from hermes_yaml import safe_load
    except ImportError:  # pragma: no cover - very old cores only
        from yaml import safe_load

    path = hermes_home / "profiles" / profile / "config.yaml"
    if not path.is_file():
        raise LaneConfigError(f"from_profile '{profile}': {path} not found")
    data = safe_load(path.read_text(encoding="utf-8")) or {}
    model = data.get("model") or {}
    if isinstance(model, str):
        return "", model, {}
    if not isinstance(model, dict):
        raise LaneConfigError(f"from_profile '{profile}': model section is not a mapping")
    return str(model.get("provider") or "").strip(), str(model.get("default") or "").strip(), model


def _parse_fallbacks(name: str, raw: Any, table: LaneTable) -> Tuple[Tuple[str, str], ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        table.errors.append(f"lane '{name}': fallbacks must be a list of {{provider, model}}")
        return ()
    out = []
    for i, fb in enumerate(raw):
        if not isinstance(fb, dict) or not str(fb.get("provider") or "").strip() or not str(fb.get("model") or "").strip():
            table.errors.append(f"lane '{name}': fallbacks[{i}] needs non-empty provider and model")
            continue
        out.append((str(fb["provider"]).strip(), str(fb["model"]).strip()))
    return tuple(out)


def _build_lane(name: str, spec: Mapping[str, Any], source: str, hermes_home: Path, table: LaneTable,
                allowed_keys: set) -> Optional[Lane]:
    if not LANE_NAME_RE.match(name):
        table.errors.append(f"lane name {name!r} must match {LANE_NAME_RE.pattern}")
        return None
    bad = sorted(set(spec) & _REJECTED_KEYS)
    if bad:
        table.errors.append(f"lane '{name}': keys {bad} are not allowed (v1: route = provider/model/fallbacks only; "
                            f"secrets via api_key_env)")
        return None
    unknown = sorted(set(spec) - allowed_keys)
    if unknown:
        table.warnings.append(f"lane '{name}': ignoring unknown keys {unknown}")

    provider = str(spec.get("provider") or "").strip()
    model = str(spec.get("model") or "").strip()
    profile = str(spec.get("from_profile") or "").strip()
    if profile:
        try:
            p_provider, p_model, p_section = _read_profile_model(hermes_home, profile)
        except LaneConfigError as exc:
            table.errors.append(f"lane '{name}': {exc}")
            return None
        provider = provider or p_provider
        model = model or p_model
        if p_section.get("base_url") and not spec.get("base_url"):
            table.warnings.append(
                f"lane '{name}': profile '{profile}' pins base_url {p_section.get('base_url')!r}; only provider/model "
                f"names are imported. The endpoint is resolved for provider '{provider}' in THIS profile.")
        source = f"profile:{profile}"
    if not provider or not model:
        table.errors.append(f"lane '{name}': provider and model are required (got provider={provider!r}, model={model!r})")
        return None

    base_url = str(spec.get("base_url") or "").strip()
    api_key_env = str(spec.get("api_key_env") or "").strip()
    api_mode = str(spec.get("api_mode") or "").strip().lower()
    if base_url and not api_key_env:
        table.errors.append(f"lane '{name}': base_url requires api_key_env (never inherit the parent's key "
                            f"for a foreign endpoint)")
        return None
    if base_url and not base_url.startswith(("https://", "http://")):
        table.errors.append(f"lane '{name}': base_url must be http(s)")
        return None
    if api_mode and api_mode not in _API_MODES:
        table.errors.append(f"lane '{name}': api_mode must be one of {sorted(_API_MODES)}")
        return None
    if api_mode and not base_url:
        table.warnings.append(f"lane '{name}': api_mode is only honoured with base_url; provider '{provider}' "
                              f"resolves its own wire")
        api_mode = ""

    return Lane(
        name=name, provider=provider, model=model, use_for=str(spec.get("use_for") or "").strip(),
        fallbacks=_parse_fallbacks(name, spec.get("fallbacks"), table),
        base_url=base_url, api_key_env=api_key_env, api_mode=api_mode, source=source,
    )


def load_lanes(settings: Mapping[str, Any] | None, hermes_home: Path) -> LaneTable:
    """Merge `lanes` (dict) and `models` (list) into one validated table. Never raises."""
    table = LaneTable()
    settings = settings or {}

    raw_lanes = settings.get("lanes") or {}
    if not isinstance(raw_lanes, dict):
        table.errors.append("settings.lanes must be a mapping of name -> lane")
        raw_lanes = {}
    for name, spec in raw_lanes.items():
        if not isinstance(spec, dict):
            table.errors.append(f"lane '{name}': must be a mapping")
            continue
        lane = _build_lane(str(name), spec, "lanes", hermes_home, table, _LANE_KEYS)
        if lane:
            table.lanes[lane.name] = lane

    raw_models = settings.get("models") or []
    if not isinstance(raw_models, list):
        table.errors.append("settings.models must be a list of {provider, model, name?, use_for?}")
        raw_models = []
    for i, spec in enumerate(raw_models):
        if not isinstance(spec, dict):
            table.errors.append(f"models[{i}]: must be a mapping")
            continue
        name = str(spec.get("name") or _slug(str(spec.get("model") or ""))).strip()
        if name in table.lanes:
            table.errors.append(f"models[{i}]: lane name {name!r} already defined (set an explicit unique `name`)")
            continue
        lane = _build_lane(name, spec, "models", hermes_home, table, _MODEL_ENTRY_KEYS)
        if lane:
            table.lanes[lane.name] = lane
    return table


def verify_resolvable(table: LaneTable, resolver=None, strict: bool = False) -> None:
    """Startup check that every provider-routed lane resolves to a credential bundle.

    Default (strict=False) is ADVISORY: a failure is a warning and the lane stays. Reason, measured
    On the multiplexed gateway a plugin's register() runs OUTSIDE any profile scope, so
    resolve_runtime_provider cannot read profile credentials (core says "profile-scoping bug on the
    multiplexed gateway") and every anthropic/openai-codex lane falsely failed. The authoritative
    check happens at CALL time inside the profile-scoped turn, where delegate_task refuses an
    unresolvable provider loudly. strict=True disables failing lanes (use in single-profile CLI setups).
    """
    if resolver is None:
        from hermes_cli.runtime_provider import resolve_runtime_provider as resolver  # type: ignore

    def _fail(name: str, why: str) -> None:
        if strict:
            table.errors.append(f"lane '{name}': {why}; lane disabled (verify_providers=strict)")
            del table.lanes[name]
        else:
            table.warnings.append(f"lane '{name}': {why} at load time; kept (verified again per call)")

    for name in list(table.lanes):
        lane = table.lanes[name]
        if lane.base_url:
            if not (_env_get(lane.api_key_env) or "").strip():
                _fail(name, f"api_key_env {lane.api_key_env!r} unset")
            continue
        try:
            rt = resolver(requested=lane.provider, target_model=lane.model)
        except Exception as exc:
            _fail(name, f"provider '{lane.provider}' did not resolve: {str(exc)[:200]}")
            continue
        if not (rt or {}).get("api_key") and not (rt or {}).get("command"):
            _fail(name, f"provider '{lane.provider}' resolved without credentials")


def describe(table: LaneTable) -> str:
    lines = []
    for name in table.names():
        lane = table.lanes[name]
        use = f" — use for: {lane.use_for}" if lane.use_for else ""
        fb = (" (fallbacks: " + ", ".join(f"{p}/{m}" for p, m in lane.fallbacks) + ")") if lane.fallbacks else ""
        lines.append(f"- {name}: {lane.label()}{fb}{use}")
    return "\n".join(lines)
