# hermes-delegate-lanes

Per-lane model routing for [Hermes Agent](https://github.com/NousResearch/hermes-agent) subagents.

The plugin adds one tool, `delegate_lane`. It spawns subagents the same way `delegate_task` does,
but runs them on a provider/model ("lane") that **you** approved in config, not on the one global
`delegation.model`. The model picks a lane **name**. It never supplies a raw model, endpoint or key.

```text
delegate_lane(lane="scout",    tasks=[{goal: "survey these 40 files", context: "..."}])  -> cheap fast model
delegate_lane(lane="reviewer", tasks=[{goal: "adversarial review of PR 12", ...}])       -> strongest reasoner
```

> **Status: works, not in the official plugin catalog.** Hermes core keys several safety protections
> on the literal tool name `delegate_task`. To give `delegate_lane` the same protections, this plugin
> registers it in those private core tables at load time (see [Safety contract](#safety-contract)).
> That is what the catalog's rule 9 (no runtime overrides of core) forbids, so `hermes plugins validate`
> reports `no core override`. The plugin fails closed: if those internals move in a Hermes update, the
> tool is not registered at all. Install it from GitHub (below). The goal is a public Hermes seam that
> makes this patching unnecessary.

## Why

Stock Hermes runs every `delegate_task` child on `delegation.model`/`delegation.provider`, or on the
parent's model. Upstream deliberately does not let the calling model choose a model per task (the
`delegation-model-routing` policy): the agent should not pick its own compute budget. The open requests
for routing ([#88891](https://github.com/NousResearch/hermes-agent/issues/88891),
[#112893](https://github.com/NousResearch/hermes-agent/issues/112893)) are awaiting a decision.

This plugin is the operator-controlled shape: the operator writes the list of lanes, and the model
only picks a name from it. It lets you run a mixed fan-out (cheap scouts, one expensive reviewer, a
vision model for screenshots) from one parent, each child on the right model.

## Install

```bash
hermes plugins install adamkrawczyk/hermes-delegate-lanes --enable
```

Then add lanes (next section). Without at least one valid lane the plugin registers nothing.
Configuration changes take effect after `hermes plugins disable delegate-lanes && hermes plugins enable delegate-lanes`.
Plugin **code** updates need a fresh session, or `hermes gateway restart` for the gateway.

## Configure

`~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    delegate-lanes:
      settings:
        lanes:                       # named lanes: full control
          scout:
            provider: zai
            model: glm-5.3-flash
            use_for: cheap recon on public, non-sensitive material
            fallbacks: [{provider: anthropic, model: claude-haiku-5-5}]
          reviewer:
            provider: openai-codex
            model: gpt-6.1-sol
            use_for: adversarial review only (expensive)
          builder:
            from_profile: builder    # copies that profile's model/provider NAMES only, never its keys or base_url
            use_for: implementation
          router:                    # direct endpoint: must name its own key variable, never inherits one
            provider: custom
            model: some/model
            base_url: https://openrouter.ai/api/v1
            api_key_env: OPENROUTER_API_KEY
            api_mode: chat_completions
        models:                      # optional flat allowlist; each entry becomes a lane
          - {name: sonnet, provider: anthropic, model: claude-sonnet-5-5, use_for: drafting}
          - {provider: anthropic, model: claude-haiku-5-5}   # name defaults to the model's last path segment
        verify_providers: warn       # warn (default) | strict | off
```

The `use_for` text is shown to the model in the tool description; write it for the model.
A copy of this block is in [`examples/config.yaml`](examples/config.yaml).

Rules enforced at load (bad entries are dropped and logged, never silently repaired):

- Lane names match `^[a-z0-9][a-z0-9_-]{0,31}$` and must be unique.
- `provider` + `model` are required, unless `from_profile` supplies them.
- `base_url` requires `api_key_env`. Without one, core would send the parent's key to the foreign
  endpoint.
- `request_overrides` and unknown `api_mode` values are rejected.
- Per-lane reasoning effort is not supported. Effort stays the global `delegation.reasoning_effort`.
- `fallbacks` are explicit. An empty list means no fallback: fail loudly rather than switch models.

`verify_providers: warn` is the default because on the multiplexed gateway `register()` runs outside
any profile scope, so a load-time credential check fails for every profile-held key. The authoritative
check is per call: `delegate_task` refuses an unresolvable provider with a clear error. Use `strict`
on a single-profile CLI setup to drop failing lanes at load.

## Use

The model calls `delegate_lane(lane, tasks)`. Each task takes `goal`, `context`, and optionally
`output_schema` and `images`, the same fields as `delegate_task`. Other keys are dropped and reported
in the result.

Everything after that is core `delegate_task`, unchanged. Verified live against a running Hermes
(interactive CLI parent, haiku lane, three children in one batch):

| Behaviour | Result |
|---|---|
| Background dispatch, parallel batch | Same dispatch handle as `delegate_task` (subagent ids, live transcripts) |
| `delegate_task(action="list")` | Lane children listed with their real model |
| `delegate_task(action="steer", ...)` | Message delivered mid-run; the child acted on it |
| `delegate_task(action="stop", ...)` | Child interrupted; the batch still completed |
| Nested delegation | An orchestrator lane child's own `delegate_task` children inherit the lane's model (when `delegation.model` is unset, the default; a pinned `delegation.model` applies to them instead) |
| Completion | One consolidated message re-enters the parent |
| Cron, Kanban, one-shot `-q` | Joined synchronously by core, as with `delegate_task` |

`delegate_lane` has no control actions of its own. You list, steer and stop through
`delegate_task(action=...)`, and the dispatch result says so.

## How it works

```text
model -> delegate_lane(lane, tasks)
  parent = agent.subagent_lifecycle.get_active_subagent_parent()     # None -> tool error
  route  = lane.credentials_cfg()   # {provider, model, fallback_providers[, base_url, api_key, api_mode]}
  delegate_task(tasks, parent_agent=parent, credentials_cfg=route, background=(depth == 0))
```

`credentials_cfg` is the same per-call routing input Hermes's own review engine uses. The background
rule copies core's: background at the top level, synchronous inside an orchestrator child. Core
still falls back to synchronous when a session has no result consumer.

### Safety contract

Core keys these protections on the literal name `delegate_task`. A tool with a different name would
silently escape all of them, so at load the plugin attaches `delegate_lane` to each one:

1. `agent.tool_guardrails._LOOP_CAPS`: the same per-turn subagent counter (default 50), so plain and
   lane spawns share one budget.
2. `agent.tool_executor._SEQUENTIAL_DEADLINE_EXEMPT_TOOLS`: so a long synchronous lane batch is not
   killed by the generic 420 s tool deadline.
3. `run_agent.AIAgent._cap_delegate_task_calls`: the per-round trim of delegation calls to
   `max_concurrent_children`. Replaced with a same-semantics version that counts `delegate_task` and
   `delegate_lane` together.
4. `MUTATING_TOOL_NAMES` / `PROGRESS_RESET_TOOL_NAMES`: guardrail parity.

Each attachment is verified after it is made. If any seam is missing (an upstream refactor),
`ContractError` is raised and **the tool is not registered**: an uncapped or deadline-killed delegation
tool is worse than none. The contract is re-checked every turn. Drift is re-applied; a moved seam makes
the tool refuse calls. Disabling the plugin detaches the entries. Detachment is reference-counted,
because the gateway loads one plugin copy per profile while core tables are process-wide.

Accepted name-keyed gaps, display and telemetry only: the CLI's one-time "running in background"
notice, the TUI lifecycle card, ACP result formatting and the `first_delegation` metric recognise
only `delegate_task`.

### Route audit

A requested route is not proof of the route actually used: fallbacks, credential rotation and
provider-side swaps all happen below the plugin. So every lane child is checked when it finishes:

- `subagent_start` maps the child session to its lane. A ContextVar scope around the call means
  plain `delegate_task` children are never tracked.
- `subagent_stop` reads the child's main-loop rows in `state.db` `session_model_usage` and compares them
  with the lane's primary model plus declared fallbacks. Matching is strict: full model id and equal
  provider. A row with a blank provider cannot prove anything, so the verdict is `ok: null`
  (unverified), never a pass.
- Each verdict is appended to `~/.hermes/logs/delegate-lanes.jsonl`. A synchronous result carries
  `lane.route_audit`. A mismatch from a background child is injected into the parent's next turn
  exactly once.

The audit reports; it does not block.

### Misroute guard

With `tools.tool_search` active, Hermes defers every plugin tool behind `tool_search`/`tool_call`.
A model that calls `delegate_lane` directly by name, instead of through `tool_call`, hits core's fuzzy
tool-name repair. That repair rewrites the call to `delegate_task`, which has no `lane` parameter and
ignores it, so the children silently run on the default model. This has been observed in production.
The route audit cannot see it, because `delegate_lane` never ran.

A `pre_tool_call` hook therefore blocks any `delegate_task` call that carries a non-empty `lane`. The
block message (it becomes the tool result) names the lane, lists the available lanes, and gives the
exact `delegate_lane` and `tool_call` forms to re-issue the call. Each block is logged. Plain
`delegate_task` calls, steer/stop actions and other tools pass through untouched.

## What the plugin touches

- **Hermes core, in-process:** the four tables above, and nothing else. No files are modified.
- **Reads:** its own config section, and `~/.hermes/state.db` (`session_model_usage`, read-only) for the
  route audit.
- **Writes:** `~/.hermes/logs/delegate-lanes.jsonl`, plus WARNING lines in the agent log.
- **Network:** none of its own. Children call their providers exactly as `delegate_task` children do.
- **Credentials:** none stored. A `base_url` lane reads the key variable you name, through Hermes's
  profile-scoped env reader.
- **Telemetry:** none.

## Tests

```bash
python tests/test_delegate_lanes.py
```

The suite is hermetic: temporary `HERMES_HOME`, no network, no model calls. Its 31 checks drive
Hermes core's real seams: credential routing, child construction, loop caps, deadline exemption,
toolset stripping, ContextVar binding, and the real `pre_tool_call` dispatcher. Mutation checks prove
the suite can fail: each removes one protection (cap, exemption, round cap, route, thread context,
seam, audit retry, guard) and asserts the check goes red.

It needs a Python that can import Hermes core. By default it finds the running gateway's interpreter
or a local Hermes install. Environment variables:

- `DELEGATE_LANES_PYTHON=<python>`: use exactly this interpreter, never fall back.
- `DELEGATE_LANES_EXPECT_CORE=<dir>`: fail unless the core under test lives in that checkout.
- `HERMES_CORE_DIR=<checkout>`: test a candidate core before upgrading to it.

CI runs the suite against the oldest supported Hermes release and current `main`, on every push and
weekly, so a core change that moves a seam shows up as a red build.

Running it as a gate before Hermes updates is cheap. Exit 0 means the contract holds; non-zero means
do not trust the plugin on that core.

## Compatibility

`requires_hermes: ">=0.21.6"`. Tested against Hermes v0.21.6 and `main`. Python 3.14 (Hermes's own
dependency pins require it).

## Limits

- One lane per call. Tasks inside one call run in parallel. Separate calls (one per lane) overlap only
  in background mode. When results are joined inline (cron, Kanban, one-shot), core runs them one
  after another.
- Per-task `group` (`delegation.independent_completions`) is not passed through.
- With tool_search active, `delegate_lane` costs one `tool_describe`/`tool_call` hop, because plugin
  tools cannot opt out of deferral.
- The route audit runs after the fact and is advisory. It depends on core writing
  `session_model_usage` per child session.
- The safety contract uses private Hermes internals. Run the test suite after every Hermes update
  (see Tests).

## License

MIT
