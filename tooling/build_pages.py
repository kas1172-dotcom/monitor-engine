"""
Assemble the GitHub Pages site from committed client artifacts.

Scans clients/*/artifacts/ for built dashboards, copies each under
_site/<client>/, and writes a root _site/index.html that links to them (using
the monitor name + accent color from each run_output.json's site_config). This
is CI glue for the Pages deploy workflow: stdlib only, no network, and no engine
coupling beyond reading the committed artifact JSON.

Usage:
    python -m tooling.build_pages --clients-dir clients --out _site
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path
from typing import Any

_DEFAULT_ACCENT = "#0F766E"


def discover_clients(clients_dir: Path) -> list[dict[str, str]]:
    """Find every client whose dashboard has been built (artifacts/index.html
    exists). Returns {slug, name, accent} per client, sorted by slug. The name
    and accent come from the committed run_output.json so the landing page
    matches each dashboard; missing/unreadable JSON falls back to the slug."""
    found: list[dict[str, str]] = []
    for artifacts in sorted(clients_dir.glob("*/artifacts")):
        if not (artifacts / "index.html").exists():
            continue
        slug = artifacts.parent.name
        name, accent = slug, _DEFAULT_ACCENT
        run_output = artifacts / "run_output.json"
        if run_output.exists():
            try:
                site_config = json.loads(run_output.read_text(encoding="utf-8")).get("site_config", {})
                name = site_config.get("name") or slug
                accent = site_config.get("accent_color") or accent
            except (ValueError, OSError):
                pass  # fall back to slug/default — never fail the build on one bad artifact
        found.append({"slug": slug, "name": name, "accent": accent})
    return found


def render_landing(clients: list[dict[str, str]]) -> str:
    """Render the root index linking to each client dashboard. Pure; all
    interpolated values are HTML-escaped."""
    if clients:
        cards = "\n".join(
            f'    <a class="card" style="--accent: {html.escape(c["accent"])}" '
            f'href="./{html.escape(c["slug"])}/">\n'
            f'      <h2>{html.escape(c["name"])}</h2>\n'
            f'      <span>{html.escape(c["slug"])}</span>\n'
            f'    </a>'
            for c in clients
        )
    else:
        cards = '    <p class="empty">No dashboards have been built yet.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Monitors</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 3rem 1.25rem;
            max-width: 720px; margin-inline: auto; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 1.5rem; }}
    .grid {{ display: grid; gap: 1rem; }}
    .card {{ display: block; padding: 1.25rem 1.5rem; border: 1px solid #8884;
             border-radius: 12px; text-decoration: none; color: inherit;
             border-left: 5px solid var(--accent, {_DEFAULT_ACCENT}); }}
    .card:hover {{ background: #8881; }}
    .card h2 {{ font-size: 1.1rem; margin: 0 0 .25rem; }}
    .card span {{ font-size: .85rem; opacity: .6; }}
    .empty {{ opacity: .6; }}
  </style>
</head>
<body>
  <h1>Monitors</h1>
  <div class="grid">
{cards}
  </div>
</body>
</html>
"""


def build(clients_dir: Path, out_dir: Path) -> list[dict[str, str]]:
    """Copy each built client dashboard under out_dir/<slug>/ and write the root
    landing page. Returns the discovered clients."""
    clients = discover_clients(clients_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in clients:
        shutil.copytree(
            clients_dir / c["slug"] / "artifacts",
            out_dir / c["slug"],
            dirs_exist_ok=True,
        )
    (out_dir / "index.html").write_text(render_landing(clients), encoding="utf-8")
    return clients


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients-dir", type=Path, default=Path("clients"))
    parser.add_argument("--out", type=Path, default=Path("_site"))
    args = parser.parse_args()
    clients = build(args.clients_dir, args.out)
    print(f"Assembled {len(clients)} dashboard(s) into {args.out}: "
          f"{', '.join(c['slug'] for c in clients) or '(none)'}")


if __name__ == "__main__":
    main()
