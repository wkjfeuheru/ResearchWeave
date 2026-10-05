# Contributing

OpenHarness is an investment research Web product. Keep session isolation, traceable evidence and existing user data intact. New functionality must use the shared research runtime; do not reintroduce terminal coding flows, orchestration, messaging platforms or old cross-session memory.

## Development

```bash
uv sync --extra dev --extra web
cd frontend/web
npm ci
npm run build
cd ../..
uv run oh web
```

Python modules live under `src/openharness`; browser code lives in `frontend/web`. The runtime is transport independent, while the Web adapter owns requests, approvals and cancellation. Research schema and on-disk identity formats must stay compatible.

## Checks

```bash
uv run ruff check src tests scripts
uv run pytest -q
cd frontend/web
npm run build
npx playwright install chromium
npm run test:e2e
```

Use deterministic models for protocol and cancellation tests. Real model validation is separate: run `tests/test_web/real_research_eval.py --help`, use a configured profile and disposable workspace, and inspect evidence provenance, calculations and restart recovery. Never embed credentials in tests or reports.

Build a wheel with `uv build --wheel` after building Web assets, then install the wheel with its `[web]` extra in a clean environment. Plugins retain skills, tools, MCP and generic hooks; retired manifest contributions must be diagnosed and ignored.
