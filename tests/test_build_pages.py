"""Tests for the GitHub Pages site assembler (pure parts + filesystem build)."""
from __future__ import annotations

import json
from pathlib import Path

from tooling.build_pages import build, discover_clients, render_landing


def _make_client(clients_dir: Path, slug: str, *, name: str | None = None,
                 accent: str | None = None, built: bool = True) -> None:
    art = clients_dir / slug / "artifacts"
    art.mkdir(parents=True)
    if built:
        (art / "index.html").write_text(f"<h1>{slug} dashboard</h1>", encoding="utf-8")
    if name or accent:
        (art / "run_output.json").write_text(
            json.dumps({"site_config": {"name": name, "accent_color": accent}}), encoding="utf-8"
        )


class TestDiscoverClients:
    def test_reads_name_and_accent_from_artifact(self, tmp_path):
        _make_client(tmp_path, "health", name="Health Monitor", accent="#112233")
        clients = discover_clients(tmp_path)
        assert clients == [{"slug": "health", "name": "Health Monitor", "accent": "#112233"}]

    def test_falls_back_to_slug_when_no_json(self, tmp_path):
        _make_client(tmp_path, "aero")  # no run_output.json
        c = discover_clients(tmp_path)[0]
        assert c["name"] == "aero" and c["accent"] == "#0F766E"

    def test_skips_unbuilt_clients(self, tmp_path):
        _make_client(tmp_path, "ready", name="Ready")
        _make_client(tmp_path, "pending", built=False)  # no index.html
        assert [c["slug"] for c in discover_clients(tmp_path)] == ["ready"]

    def test_sorted_by_slug(self, tmp_path):
        _make_client(tmp_path, "zeta", name="Z")
        _make_client(tmp_path, "alpha", name="A")
        assert [c["slug"] for c in discover_clients(tmp_path)] == ["alpha", "zeta"]

    def test_bad_json_falls_back_not_raises(self, tmp_path):
        art = tmp_path / "broken" / "artifacts"
        art.mkdir(parents=True)
        (art / "index.html").write_text("x")
        (art / "run_output.json").write_text("{not json")
        c = discover_clients(tmp_path)[0]
        assert c["name"] == "broken"


class TestRenderLanding:
    def test_links_and_escapes(self):
        out = render_landing([{"slug": "health", "name": "A & B <Monitor>", "accent": "#abc123"}])
        assert 'href="./health/"' in out
        assert "A &amp; B &lt;Monitor&gt;" in out   # escaped
        assert "#abc123" in out

    def test_empty_state(self):
        out = render_landing([])
        assert "No dashboards" in out


class TestBuild:
    def test_copies_dashboards_and_writes_index(self, tmp_path):
        clients_dir = tmp_path / "clients"
        clients_dir.mkdir()
        _make_client(clients_dir, "health", name="Health Monitor")
        out = tmp_path / "_site"

        result = build(clients_dir, out)

        assert [c["slug"] for c in result] == ["health"]
        assert (out / "index.html").exists()
        assert (out / "health" / "index.html").read_text() == "<h1>health dashboard</h1>"
        assert "Health Monitor" in (out / "index.html").read_text()
