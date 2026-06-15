# monitor-engine

## Project shape

`monitor_engine` is an installable Python package. On top of it sits a thin per-client deployment layer (a separate directory / repo, not part of this package). The engine knows nothing about any specific client or industry.

## Non-negotiable rules

### 1. Zero client-specific or industry-specific strings in the engine package

If you find yourself writing an industry word (e.g. "healthcare", "fintech", "retail", a company name, a product name) anywhere inside `monitor_engine/`, stop. Move it to the deployment config layer instead.

Engine code must be generic: monitors, checks, thresholds, alerts — not patients, transactions, or SKUs.

### 2. Static site output only — no backend, no database

Output is a static site (HTML + JSON). A GitHub Actions cron job runs the pipeline and commits the JSON artifacts back to the repo. There is no server, no database, no API endpoint to maintain.

### 3. Single source of truth for the data schema

The data schema lives in exactly one Python module (`monitor_engine/schema.py` or equivalent) using Pydantic models. Everything else — data generation, validation, frontend contract docs — must derive from that module. Do not define schema shapes in multiple places.

### 4. API keys from environment variables only

No secrets in code, config files, or committed files. All API keys and credentials come from environment variables. Document required env vars in README; never hardcode or default them to real values.

### 5. Boring, dependency-light solutions

Prefer the standard library and already-present dependencies. Before introducing any new third-party package, stop and flag it for approval with a one-line justification. Do not add a dependency to solve a problem the stdlib can handle.

### 6. Minimal feature implementation

When asked for a feature, build the smallest version that satisfies the request. At the end of your response, list deferred ideas (things intentionally left out) so they can be considered separately. Do not implement speculative or "nice to have" extensions.

## Workflow reminders

- The deployment layer configures the engine via config (YAML/TOML/env), not by subclassing or monkey-patching engine internals.
- GitHub Actions is the only scheduler. Do not add cron logic inside the engine itself.
- JSON artifacts committed by CI are the contract between the pipeline and the frontend. Schema changes must be backward-compatible or versioned.
