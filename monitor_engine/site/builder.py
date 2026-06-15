"""
Static site builder.

Writes two files to output_dir:
  index.html       — self-contained HTML with CSS and JS inlined
  run_output.json  — RunOutput artifact with site_config embedded
"""
from __future__ import annotations

from pathlib import Path

from monitor_engine.models import (
    ClientConfig,
    DeepAnalysisSectionInfo,
    EditionInfo,
    RunOutput,
    SiteConfig,
)

_ASSETS = Path(__file__).parent / "_assets"
_TEMPLATE = Path(__file__).parent / "_template" / "index.html"

_STYLE_MARKER = "/* STYLE_PLACEHOLDER */"
_SCRIPT_MARKER = "/* SCRIPT_PLACEHOLDER */"
_DATA_URL_MARKER = "DATA_FILENAME_PLACEHOLDER"


def build_site(
    run_output: RunOutput,
    config: ClientConfig,
    output_dir: Path,
    *,
    data_filename: str = "run_output.json",
) -> None:
    """
    Produce a self-contained static site in *output_dir*.

    The site_config is derived from *config* and embedded inside the JSON
    artifact so the frontend never needs a separate config file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    deep_sections = None
    if config.deep_analysis is not None:
        deep_sections = [
            DeepAnalysisSectionInfo(id=s.id, label=s.label, kind=s.kind)
            for s in config.deep_analysis.sections
        ]

    site_cfg = SiteConfig(
        name=config.branding.name,
        accent_color=config.branding.accent_color,
        editions=[
            EditionInfo(id=ed.id, label=ed.label, categories=ed.categories)
            for ed in config.editions
        ],
        deep_analysis_sections=deep_sections,
    )
    enriched = run_output.model_copy(update={"site_config": site_cfg})

    (output_dir / data_filename).write_text(
        enriched.model_dump_json(indent=2), encoding="utf-8"
    )

    css = (_ASSETS / "style.css").read_text(encoding="utf-8")
    js = (_ASSETS / "app.js").read_text(encoding="utf-8")
    html = (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace(_STYLE_MARKER, css, 1)
        .replace(_SCRIPT_MARKER, js, 1)
        .replace(_DATA_URL_MARKER, data_filename, 1)
    )

    (output_dir / "index.html").write_text(html, encoding="utf-8")
