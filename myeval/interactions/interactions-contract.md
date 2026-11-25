# Interaction and local state contract v1

This package supplies reusable user-turn drivers and local file state. It does not call a product, manage provider credentials, grade conversation quality, or provide a security sandbox.

## Turn driver

`TurnDriver(policy, context, episode_id, provider=None)` accepts a strict policy dictionary. Required identity: `id`, `version`, `mode` (`script`, `rules`, or `model`), `goal`, `visible_facts`, `forbidden_knowledge`, and `max_turns`. Unknown policy fields are rejected. No task case, future transcript, evaluation rubric, holdout criterion, filesystem handle, or product configuration is passed to a provider.

Call `next_user()` once, deliver its `text` to the real product, then call `next_user(actual_assistant_text)` after each actual delivery. A stopped result has `text=null` and a structured `stop_reason`. `finish(reason)` closes the episode on product failure or external completion; `snapshot()` returns a JSON-serializable visible transcript and driver state.

- `script` reads only the next registered user line. It is a scripted control, not a model user.
- `rules` chooses a named branch using the latest actual assistant response and a finite list of literal `contains_any` predicates. Selection order and default branch are explicit. Rule text is not supplied to a model.
- `model` requires an explicit `provider(request, control)` callback. Request is a whitelist: policy identity, goal, visible facts, prohibited knowledge categories, already-visible transcript, current turn, and generation limits. Provider returns `text`, optional `stop`, public `stop_reason`, `model`, and numeric `usage`; hidden reasoning and extra fields are neither retained nor emitted.

The callback must honor `control.check_cancelled()` and `control.remaining_seconds`, request `control.max_output_tokens`, and state whether the transport can enforce that limit upstream. `control.on_cancel(bounded_cleanup)` registers local-process cleanup before the driver returns on cancellation; the handler must be bounded. The driver bounds local waiting, call count, visible history size, output size, and observed token consumption; returned output above the requested token limit is rejected. Token totals do not establish a hard monetary limit; unknown usage stops further simulator requests. Local cancellation/timeouts do not prove provider computation or billing stopped. Provider callbacks execute in-process and must be trusted not to read external data; the whitelist is an information contract, not an OS sandbox.

Every emitted event includes episode/turn, policy ID/version/hash, mode, actual visible input/output, and terminal reason. Simulation is explicitly labelled `scripted`, `rule_based`, or `model_simulated`; never report simulated-user outcomes as human feedback. Cancelled/failed episodes retain prior delivered turns. Goal completion from simulator output is a simulator decision, not independent task verification.

## Included Codex CLI provider

`CodexSimulatorProvider(trace_root, model="gpt-6-sol", effort="low")` is a public callable provider. It reuses the installed CLI's saved login without reading credentials itself. Each invocation uses a fresh temporary cwd, no user/project config or rules, ephemeral sessions, read-only mode, disabled shell/web/apps/plugins/skills/memory/MCP/computer/browser/subagents, and a replacement simulator instruction file. Incoming requests must match the driver's exact field allowlist. Raw JSONL is parsed only in memory; reasoning entries are dropped before persistence. Tool events reject the output. Requested model/effort and unconfirmed effective fields remain distinct.

The installed CLI exposes no verified upstream output-token cap in this implementation. That limit is supplied in the prompt and verified against returned numeric usage; this is explicitly reported as `upstream_limit_unconfirmed`. Call count, wall time, response bytes and observed cumulative tokens remain bounded. Each call keeps `request.json`, `prompt.txt`, `boundary.txt`, `schema.json`, safe `events.jsonl`, `result.json`, `receipt.json`, `invocation.json`, and `process.json` under `trace_root/<run_id>/live/`; no credentials or raw reasoning stream are stored. The driver includes safe provider receipts in its snapshot and events, including cancellation receipts.

Adapter terminal mapping: `cancelled` is cancellation, `deadline` is timeout, `provider_error:*`, `invalid_provider_*`, `provider_output_budget_exceeded` and `observed_token_budget_exceeded` are unsuccessful simulation outcomes. `max_turns`, `script_exhausted` or `simulator_completed` indicate why simulation stopped; none independently proves the tested product achieved the task. For export, declare the provider's safe evidence files explicitly through the adapter/core artifact contract rather than assuming arbitrary subdirectories are included.

## Local state and final artifacts

`EpisodeWorkspace(root)` creates or reopens an owned directory with a marker. It exposes `write/read`, `snapshot(label)`, `reset(snapshot_id)` and `verify(expectations)`. Files live under a managed `live/` directory; immutable snapshot objects and reset history remain alongside it. Absolute paths, traversal, symbolic links, and non-regular files are rejected. Snapshot/reset uses actual byte reads and hashes. Reset preserves the previous live directory in reset history.

Final artifact expectations support existence, SHA-256, UTF-8 exact text, and JSON Pointer value equality. Results report file hash, byte size, and rule outcome; these are objective file checks, not semantic product acceptance. State must be created under an isolated trial directory. The wrapper does not prevent another process from changing files and is not a container sandbox.

## Integration seam

Existing `RunContext` already supplies `emit`, `check_cancelled`, and `timeout_seconds`; no core changes are required. A product adapter owns the real product call between `next_user` calls and maps final product state into `EpisodeWorkspace` only when the files actually belong to that isolated execution. The adapter must preserve failure and cancellation, and must not pass a full case or a hidden rubric into provider callbacks. Core/API should surface mode, policy/version, limits, stop reason, usage unknowns and state verification separately from evaluator results.
