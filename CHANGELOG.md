# Changelog

## 1.0.1

- Misroute guard: a `pre_tool_call` hook blocks `delegate_task` calls that carry a `lane` argument.
  With tool_search active, core's fuzzy tool-name repair could rewrite a direct `delegate_lane` call
  into `delegate_task`, which silently drops the lane.
- `tasks` may be given as a JSON string.
- `from_profile` reads profile config through Hermes's own YAML layer (`hermes_yaml`). PyYAML is not
  a Hermes dependency.
- Test harness: an explicit `DELEGATE_LANES_PYTHON` never falls back to another interpreter;
  `DELEGATE_LANES_EXPECT_CORE` asserts which core was tested.

## 1.0.0

- `delegate_lane` tool: operator-defined lanes (`lanes:` and a flat `models:` allowlist).
- Safety contract re-attached to the name-keyed core protections (spawn cap, per-round cap,
  deadline exemption, guardrail sets), verified every turn, fail closed.
- Route audit of every lane child against `state.db` usage.
