"""Self-contained HTML rendering of a scorecard (one file, inline CSS and an inline SVG chart)."""

import html
import re
from collections.abc import Sequence

from evalharness.metrics import CurvePoint
from evalharness.report import Scorecard, to_markdown

STYLE = (
    "body{font:15px/1.5 system-ui,sans-serif;max-width:920px;margin:2rem auto;padding:0 1rem;"
    "color:#1a1a1a;background:#fff}"
    "table{border-collapse:collapse;margin:.6rem 0;font-size:13px}"
    "th,td{border:1px solid #ccc;padding:3px 8px;text-align:right}"
    "th:first-child,td:first-child{text-align:left}"
    ".banner{background:#fff3cd;border:2px solid #d39e00;padding:.7rem 1rem;font-weight:600}"
    "@media(prefers-color-scheme:dark){body{background:#161616;color:#e8e8e8}"
    "th,td{border-color:#444}.banner{background:#4d3b00;color:#fff}}"
)
_STRONG = re.compile(r"\*\*(.+?)\*\*")
_EMPH = re.compile(r"(?<!\*)\*(?!\*)(.+?)\*")
_CODE = re.compile(r"`(.+?)`")


def inline(text: str) -> str:
    """Escape, then apply the few inline Markdown forms the scorecard uses."""
    out = html.escape(text)
    out = _STRONG.sub(r"<strong>\1</strong>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return _EMPH.sub(r"<em>\1</em>", out)


def svg_curve(curve: Sequence[CurvePoint], chosen: CurvePoint | None) -> str:
    w, h, pad = 560, 280, 36

    def px(threshold: float) -> float:
        return pad + threshold * (w - 2 * pad)

    def py(value: float) -> float:
        return h - pad - value * (h - 2 * pad)

    def line(values: Sequence[tuple[float, float | None]], colour: str) -> str:
        pts = " ".join(f"{px(t):.1f},{py(v):.1f}" for t, v in values if v is not None)
        return f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{pts}"/>'

    grid = "".join(
        f'<line x1="{pad}" x2="{w - pad}" y1="{py(g):.1f}" y2="{py(g):.1f}" stroke="#ddd"/>'
        f'<text x="4" y="{py(g) + 4:.1f}" font-size="10">{g:.2f}</text>'
        for g in (0, 0.25, 0.5, 0.75, 1)
    )
    marker = ""
    if chosen:
        x = px(chosen.threshold)
        marker = (
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{pad}" y2="{h - pad}" '
            'stroke="#555" stroke-dasharray="4 3"/>'
            f'<text x="{x + 4:.1f}" y="{pad + 10}" font-size="11">'
            f"threshold {chosen.threshold:.2f}</text>"
        )
    legend = (
        f'<text x="{w - 170}" y="{h - 8}" font-size="11">'
        '<tspan fill="#0a7">━ precision</tspan>  <tspan fill="#36c">━ recall</tspan></text>'
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" width="100%" style="max-width:{w}px" '
        'aria-label="Precision and recall versus auto-resolve threshold">'
        f"{grid}{line([(p.threshold, p.precision) for p in curve], '#0a7')}"
        f"{line([(p.threshold, p.recall) for p in curve], '#36c')}{marker}{legend}"
        f'<text x="{pad}" y="{h - 8}" font-size="11">threshold →</text></svg>'
    )


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    data = [r for r in rows if set(r.replace("|", "").strip()) - {"-", " "}]
    head, body = data[0], data[1:]
    th = "".join(f"<th>{inline(c)}</th>" for c in _cells(head))
    trs = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in _cells(r)) + "</tr>" for r in body
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def to_html(card: Scorecard) -> str:
    parts: list[str] = []
    pending: list[str] = []
    in_list = False

    def close() -> None:
        nonlocal in_list
        if pending:
            parts.append(_table(pending))
            pending.clear()
        if in_list:
            parts.append("</ul>")
            in_list = False

    for line in to_markdown(card).splitlines():
        if line.startswith("|"):
            pending.append(line)
            continue
        close()
        if line.startswith("# "):
            parts.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            parts.append(f"<h2>{inline(line[3:])}</h2>")
            if "vs. threshold" in line:
                parts.append(svg_curve(card.curve, card.chosen))
        elif line.startswith("> "):
            parts.append(f'<p class="banner">{inline(line[2:])}</p>')
        elif line.startswith("- "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip():
            parts.append(f"<p>{inline(line)}</p>")
    close()
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>SettleAgent evaluation scorecard</title>"
        f"<style>{STYLE}</style><body>{''.join(parts)}</body></html>"
    )
