# OpenHarness

OpenHarness is a local investment research Web workspace. Each conversation owns its task context, research state, evidence pool and auditable reasoning summaries. Sources carry immutable snapshots, timestamps and stable IDs; answers freeze the versions they cite.

[中文说明](README.zh-CN.md) · [Web workspace](docs/web-workspace.md) · [Contributing](CONTRIBUTING.md)

## Install and start

Python 3.10 or newer is required.

```bash
pip install 'openharness-ai[web]'
oh setup
oh web
```

Open the local address printed by `oh web`. The server accepts local hosts and checks browser origins. Credentials stay on the backend and are redacted from browser output.

For a source checkout, build the frontend with Node.js 20 or newer:

```bash
uv sync --extra dev --extra web
cd frontend/web
npm ci
npm run build
cd ../..
uv run oh web
```

The installation scripts support PyPI and source installs. `oh`, `openharness` and `openh` share the same CLI; on PowerShell use `openh web` or `oh.exe web` to avoid the built-in `oh` alias.

## Research workflow

Complex questions produce a concise task outline and committed progress. Simple questions need no forced plan. Research tools read and search documents, fetch webpages, query configured MCP services, execute calculations and create files, notebooks or images. File edits and Shell operations retain permission and sandbox checks.

The research memory tool maintains versioned objectives, plans, evidence, conclusions and auditable method summaries. A successful tool call does not verify a claim. Search summaries and user accounts remain pending verification until supported by explicit records. Corrections preserve history and mark dependent conclusions for review.

Use **Stop** to cancel generation, or **Interrupt and modify** to save a change request, finish cancellation and replan. Refreshing or restarting restores committed state and historical citations; it does not automatically resume a model run. Conversations do not inherit other conversations' research.

## Models, credentials and extensions

Existing Anthropic/OpenAI compatible APIs and Codex, Claude and Copilot subscription authentication are retained. Add API configurations in the Web model page or use the CLI. Subscription profiles can be selected and tested in Web; their authentication is managed by CLI:

```bash
oh auth login
oh auth codex-login
oh auth claude-login
oh auth copilot-login
oh auth status
oh provider list
oh provider use PROFILE
```

Financial data comes from your configured services and retrieved sources; OpenHarness does not add a built-in live quote feed or trading service.

```bash
oh mcp list
oh mcp add --help
oh plugin list
oh plugin install PATH
oh config show
oh config set research_memory.enabled false
```

Plugins can contribute skills, tools, MCP services and generic hooks. Installed skills and plugin discovery directories remain compatible, including `.agents/skills` and `.claude/skills`. Legacy plugin agent and terminal command declarations are accepted but ignored, with diagnostics in `oh plugin list`.

Web search defaults to configured investment research sources, ordered by official disclosures/statistics/policy, industry professionals, then financial media. Broader web discovery requires an explicit scope. The former bundled `skill-creator` is now supplied by the toggleable `skill-authoring` plugin with layered resources and a reusable scaffold. See [research search and skill plugins](docs/research-search-and-skills.md) for configuration and layouts.

Research memory defaults to enabled with a 6,000-token injection budget. Disabling it never activates an older memory system. Old configuration fields are ignored; legacy memory context budgets migrate only when no top-level or active-model budget is set. Existing user data is not automatically removed.

The personal assistant product, messaging integrations, coding orchestration, terminal UI, scheduling and old long-term/vector memory are retired. Only the research Web runtime and administrative CLI remain.

## Validation

```bash
uv run pytest -q
uv run ruff check src tests scripts
cd frontend/web
npm run build
npx playwright install chromium
npm run test:e2e
```

Real model verification is separate from deterministic tests. See `tests/test_web/real_research_eval.py --help` for the two-turn Web evaluation with source collection, calculations, citations, browser checks and restart recovery. Run it with an existing configured profile in a disposable workspace.

[Cleanup scope and validation report](docs/research-product-cleanup.md)
