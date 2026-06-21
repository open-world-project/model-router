# model-router

Automatic cost-aware model routing for Hermes Agent.

`model-router` classifies each turn, picks the cheapest tier that should work, and escalates only when the task needs more reasoning.

## What It Does

- automatic per-turn routing,
- manual pinning with `/t1` to `/t5` and `/auto`,
- per-profile configuration via `model_router.yaml`,
- automatic validation and repair of the small required Hermes CLI integrations,
- plugin-registered slash commands on current Hermes builds,
- synchronized routing docs in `skill_routing.md` and `SOUL.md`.

## Five-Tier Contract

The plugin is built around **exactly 5 tiers** and expects those five slots to exist at all times.

- You can change the model, label, emoji, and reasoning mode of each slot.
- You should not change the number of tiers.
- `/t1` to `/t5` always map to those five configured slots.

Default mapping:

These are bootstrap defaults only. Once `model_router.yaml` exists, the active profile config becomes the only source of truth for routing behavior.

| Tier | Default model | Reasoning | Purpose |
|---|---|---|---|
| T1 | `qwen/qwen3.5-flash-02-23` | — | triage, acks, cheap helper work |
| T2 | `deepseek/deepseek-v4-flash` | — | default daily-driver |
| T3 | `minimax/minimax-m2.7` | — | creating, basic coding, review, synthesis |
| T4 | `deepseek/deepseek-v4-pro` | — | planning, architecture, complex multi-step design |
| T5 | `anthropic/claude-sonnet-4-6` | `medium` | high-stakes reasoning |

## Installation

Clone the plugin into the canonical Hermes plugin directory:

```bash
mkdir -p ~/.hermes/plugins
cd ~/.hermes/plugins
git clone https://github.com/open-world-project/model-router model-router
cd model-router
```

Install for the default profile and all named profiles:

```bash
bash install.sh
```

Install only selected targets:

```bash
bash install.sh default
bash install.sh seo coder trading
```

Restart Hermes after installation.

`install.sh` is safe to run repeatedly. It repairs and validates the required Hermes integration points, refreshes the global `hermes` launcher, ensures the plugin is enabled, normalizes `model_router.yaml`, syncs `auxiliary.triage_specifier`, and regenerates `skill_routing.md` plus the managed routing block in `SOUL.md`. On current Hermes, `/t1` to `/t5` and `/auto` are registered through the plugin slash-command API; the remaining CLI patch points cover status-bar display, single-query live-agent binding, and inline `/model` handling.

If a local `hermes-webui` checkout is detected, `install.sh` also patches its API and static UI so model-router prepares each WebUI turn before agent startup, owns the live agent during streaming, and exposes native tier controls without the noisy "CLI-only command" chat messages.

Before rewriting managed Hermes core files or the launcher, it creates timestamped backups.

**Important:** If you move or rename the plugin directory after installation, re-run `bash install.sh` from the new location to re-sync the canonical plugin path and all profile symlinks.

## Startup Validation

When launched through `hermes` or profile launchers like `seo`, `coder`, and `trading`, the global launcher validates model-router requirements on every start.

If `model-router` is enabled for the target profile and validation fails, it asks:

```text
Hermes core files validation failed. Plugin model-router won't work without it. Do you want to repair files? [y/N]
```

If you answer `y`, it runs the installer for the current target, re-validates the result, and continues only if validation passes. In a non-interactive terminal it prints a warning instead of blocking for input.

## Configuration

Source of truth:

- default profile: `~/.hermes/model_router.yaml`
- named profile: `~/.hermes/profiles/<name>/model_router.yaml`

This file is created automatically by the installer if it does not exist, and normalized on later runs.
If you delete it, `install.sh` generates a fresh one from the built-in defaults.

The installer syncs this config into:

- `config.yaml` -> `auxiliary.triage_specifier`
- `skill_routing.md` -> human-readable routing reference
- `SOUL.md` -> managed routing guidance block

`classifier.api_key_env` stores the **name** of the Hermes environment variable that holds the classifier API key. The actual secret belongs in `~/.hermes/.env` or the process environment, never in `model_router.yaml`. The installer migrates a legacy non-empty `classifier.api_key` into the configured env var when possible, then removes the YAML field.

## Customizing Models

You can customize the five tier slots, but the router still expects all five tiers to exist.

1. Edit the active profile's `model_router.yaml`.
2. Change `classifier.model` if you want a different classifier.
3. Change `tiers.1` to `tiers.5` as needed.
4. Re-run `bash install.sh <target>`.
5. Restart Hermes.

You only need to override the fields you want to change. Missing fields fall back to defaults.

If you edit `~/.hermes/model_router.yaml`, use `bash install.sh default`. If you edit a profile file like `~/.hermes/profiles/seo/model_router.yaml`, use `bash install.sh seo`.

Practical rule:

- If you change only tier `model`, `label`, `emoji`, or `reasoning`, a Hermes restart is enough for runtime behavior, because the plugin loads `model_router.yaml` directly.
- Re-running `install.sh` is still recommended after any config change if you want everything else to stay in sync too: `auxiliary.triage_specifier`, `skill_routing.md`, `SOUL.md`, and startup-managed files.
- If you change anything under `classifier`, run `install.sh` and restart Hermes.

Example:

```yaml
classifier:
  provider: openrouter
  model: openai/gpt-4.1-mini
  base_url: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
  timeout: 30
  extra_body:
    enable_caching: true

tiers:
  1:
    model: openai/gpt-4.1-mini
  2:
    label: T2 Maverick
    model: meta-llama/llama-4-maverick
  3:
    model: deepseek/deepseek-v4-pro
  4:
    model: anthropic/claude-sonnet-4.5
    reasoning: low
  5:
    model: anthropic/claude-sonnet-4.5
    reasoning: medium
integrations:
  hermes_webui_dir: ""
```

## Runtime Notes

- Every turn is classified before the main LLM call. On classifier failure, the router falls back to `T2`.
- You can hint tiers in user text with `T1` to `T5`, `tier1` to `tier5`, or `tier 1` to `tier 5`.
- `/model`, `/t1` to `/t5`, and `/auto` work at session scope.
- The active tier is shown in the CLI status bar.
- In `hermes-webui`, the composer model dropdown auto-detects model-router and renders native `Auto`, `T1` to `T5` rows showing the configured target models for the active profile.

## Hermes WebUI Integration

- Auto-detects a `hermes-webui` checkout in common locations like `~/.hermes/hermes-webui`, `~/GitHub/hermes-webui`, `~/code/hermes-webui`, and similar.
- If `model_router.yaml` contains `integrations.hermes_webui_dir`, that path is checked first and wins over env vars and auto-detect when it points to a real checkout.
- Patches `api/routes.py`, `api/streaming.py`, `static/commands.js`, `static/ui.js`, and `static/style.css` only when a real `hermes-webui` repo is found.
- Adds local WebUI command handlers for `/t1` to `/t5` and `/auto`, so those commands work quietly instead of falling through to the generic WebUI "CLI-only" warning.
- Adds a `Model Router` section inside the composer model dropdown with native rows for `Auto` and `T1` to `T5`, showing the configured model slug and reasoning level for the active profile.
- Makes WebUI ask model-router for the turn model before spawning the stream, so WebUI defaults no longer override routed tiers during actual API calls.
- If your WebUI checkout lives somewhere unusual, point the installer at it with `HERMES_WEBUI_DIR=/path/to/hermes-webui bash install.sh`.
- Example config override:
```yaml
integrations:
  hermes_webui_dir: /absolute/path/to/hermes-webui
```

## Auto-Escalation & De-escalation

**Escalation:**
- When a tool call fails, the error counter increments.
- After 2 consecutive errors, the router automatically bumps up one tier (T1→T2→T3→T4, max T4).
- The error counter resets after each escalation, so another 2 errors on the new tier will escalate again.
- Escalations continue until T4 is reached (T5 is never auto-reached, only manually or by hint).
- Escalations are **not** limited per turn — every 2 errors can trigger another bump within the same loop.
- Tool errors do **not** interrupt the agent, they are just tracked to identify when a model is struggling.

**De-escalation:**
- Once the turn completes (final response with no pending tool calls), the tier automatically drops back to the base tier (the one assigned at the start of the turn).
- This keeps costs low for the next turn while preserving the escalation capability if problems reoccur.

**Goal:** Get cheap work done cheaply, but always escalate when needed to make sure the job gets done right.

## Manual Pinning

- `/t1` to `/t5` pins the session to one of the five configured tier slots.
- `/model` also acts as a manual override for the current session.
- After using `/model` or `/t1` to `/t5`, automatic routing is paused for that session.
- Auto-routing starts working again only after `/auto` or after starting a fresh Hermes session.
- This means the router will not auto-choose a model again until you explicitly unpin or re-run Hermes.

## Validation Model

Validation is block-aware, not marker-based. Startup validation checks for the complete managed blocks in `commands.py`, `cli.py`, and the current Hermes agent setup mixin (`hermes_cli/cli_agent_setup_mixin.py`). If even one internal line is missing, validation fails and repair restores the full block.

## Provider Support

- The default config uses OpenRouter, and the plugin is documented and tested around that setup.
- The runtime classifier uses Hermes `auxiliary.triage_specifier`, so provider settings come from `model_router.yaml`, then get synced into `config.yaml` by `install.sh`.
- The classifier credential is referenced by env-var name via `classifier.api_key_env`; the installer syncs that into `auxiliary.triage_specifier.api_key` as `${ENV_NAME}` so Hermes resolves the secret from `~/.hermes/.env` or the process environment at runtime.
- That means the plugin is not hardcoded to OpenRouter only, but non-OpenRouter providers depend on Hermes supporting them correctly through `triage_specifier`.

## Verify

```bash
hermes plugins list
profile_name plugins list
```

```bash
cat ~/.hermes/model_router.yaml
cat ~/.hermes/skill_routing.md
cat ~/.hermes/SOUL.md
```

## Troubleshooting

- Plugin not loading: run `bash install.sh`, then check `hermes plugins list` and whether `model-router` is present in the active `config.yaml`.
- Everything falls back to `T2`: confirm the env var named by `classifier.api_key_env` exists (default `OPENROUTER_API_KEY`), `auxiliary.triage_specifier` is present, and `model_router.yaml` is valid YAML.
- `/t1` to `/t5` use the wrong models: edit the correct `model_router.yaml`, re-run `install.sh`, and restart Hermes.
- Hermes update changed patched core files: just launch Hermes and let startup validation repair it, or run `bash install.sh` manually if you want to refresh it before launch.
- Re-run safety: repeated `bash install.sh` runs are supported and only rewrite managed files when they drift or are damaged.

## License

This project is licensed under the `MIT` License. See `LICENSE`.

## Support

If this plugin saved you time or helped you ship something useful, you can support the work here:

https://buymeacoffee.com/jakubmisiak
