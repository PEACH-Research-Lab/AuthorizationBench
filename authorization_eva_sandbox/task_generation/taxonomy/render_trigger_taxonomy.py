"""Render trigger_manifestation.json (the single source of truth for the
situational trigger taxonomy) into a human-readable HTML page.

Usage:
    python task_generation/taxonomy/render_trigger_taxonomy.py

Writes trigger-taxonomy.html next to this script. Re-run after any edit to
trigger_manifestation.json. This replaces the retired hand-maintained
docs/situational-trigger-taxonomy.md so the doc can never drift from the data.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "trigger_manifestation.json"
TARGET = HERE / "trigger-taxonomy.html"

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; max-width: 920px;
       margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.55; }
h1 { border-bottom: 2px solid #ddd; padding-bottom: .4rem; }
h2 { margin-top: 2.2rem; color: #333; }
.trigger { border: 1px solid #ddd; border-radius: 8px; padding: .5rem 1.2rem; margin: .6rem 0; }
.trigger > summary { cursor: pointer; font-size: 1.05rem; font-weight: 600; padding: .35rem 0;
                     list-style-position: outside; }
.trigger[open] > summary { border-bottom: 1px solid #eee; margin-bottom: .5rem; }
.oneline { color: #666; font-weight: 400; font-size: .88rem; margin-left: .4rem; }
table.index td, table.index th { border: 1px solid #ddd; padding: .3rem .55rem;
                                 text-align: left; font-size: .92rem; }
table.index { border-collapse: collapse; width: 100%; margin: .8rem 0 1.4rem; }
.letter { display: inline-block; background: #333; color: #fff; border-radius: 4px;
          padding: 0 .5rem; margin-right: .5rem; font-family: monospace; }
dt { font-weight: 600; margin-top: .6rem; }
dd { margin-left: 0; color: #444; }
ul { margin: .3rem 0; padding-left: 1.3rem; }
code { background: #f4f4f4; padding: .1rem .3rem; border-radius: 3px; font-size: .92em; }
.meta { color: #777; font-size: .85rem; }
.intro p { color: #444; }
"""


def _e(text: object) -> str:
    return html.escape(str(text or ""))


def _render_trigger(letter: str, c: dict) -> str:
    mech = str(c.get("mechanism") or "")
    oneline = mech.split(". ")[0][:110]
    parts = [f'<details class="trigger" id="trigger-{letter}">']
    parts.append(
        f'<summary><span class="letter">{letter}</span>{_e(c.get("title"))}'
        f'<span class="oneline">{_e(oneline)}</span></summary>'
    )
    parts.append(f"<p>{_e(mech)}</p>")
    forbidden = c.get("forbidden") or []
    if forbidden:
        parts.append('<p class="meta"><b>Forbidden (leak if agent-visible):</b></p><ul>'
                     + "".join(f"<li>{_e(f)}</li>" for f in forbidden) + "</ul>")
    note = c.get("pipeline_note")
    if note:
        parts.append('<p class="meta" style="color:#888;font-size:0.85em">'
                     f'<b>Pipeline note (never shown to LLMs):</b> {_e(note)}</p>')
    channels = c.get("typical_channels") or []
    if channels:
        parts.append(
            '<p class="meta">Typically involves: '
            + ", ".join(f"<code>{_e(ch)}</code>" for ch in channels)
            + " (suggestion only — never exclusive; the scenario decides where "
            "the pressure lives)</p>"
        )
    aliases = c.get("aliases") or []
    if aliases:
        parts.append(
            '<p class="meta">Accepted aliases (seed phrases): '
            + "; ".join(_e(a) for a in aliases)
            + "</p>"
        )
    parts.append("</details>")
    return "\n".join(parts)


def main() -> int:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    triggers = data["triggers"]
    doc = data.get("taxonomy_doc") or {}
    themes = doc.get("themes") or {}
    theme_order = doc.get("theme_order") or sorted(
        {c.get("theme", "") for c in triggers.values()}
    )

    out = ["<!DOCTYPE html>", "<html lang='en'><head><meta charset='utf-8'>"]
    out.append(f"<title>{_e(doc.get('title', 'Trigger Taxonomy'))}</title>")
    out.append(f"<style>{_CSS}</style></head><body>")
    out.append(f"<h1>{_e(doc.get('title', 'Trigger Taxonomy'))}</h1>")
    out.append('<p class="meta">Generated from '
               "<code>task_generation/taxonomy/trigger_manifestation.json</code> "
               "by <code>render_trigger_taxonomy.py</code> — do not edit by hand.</p>")
    out.append('<div class="intro">')
    for para in doc.get("intro", []):
        out.append(f"<p>{_e(para)}</p>")
    out.append("</div>")

    # Full index: letter | title | theme — for a clean at-a-glance review.
    out.append("<h2>总览索引</h2>")
    out.append('<table class="index"><tr><th>#</th><th>Trigger</th><th>类别（theme）</th></tr>')
    for letter in sorted(triggers):
        c = triggers[letter]
        theme = themes.get(c.get("theme", ""), {})
        out.append(
            f'<tr><td><code>{letter}</code></td>'
            f'<td><a href="#trigger-{letter}">{_e(c.get("title"))}</a></td>'
            f'<td>{_e(theme.get("title", c.get("theme", "")))}</td></tr>'
        )
    out.append("</table>")

    for theme_key in theme_order:
        theme = themes.get(theme_key) or {}
        members = [k for k, c in sorted(triggers.items()) if c.get("theme") == theme_key]
        if not members:
            continue
        out.append(f"<h2>{_e(theme.get('title', theme_key))}</h2>")
        if theme.get("description"):
            out.append(f"<p>{_e(theme['description'])}</p>")
        for letter in members:
            out.append(_render_trigger(letter, triggers[letter]))

    out.append("</body></html>")
    TARGET.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
