"""Graficos SVG inline para el reporte, sin dependencias JS.

Se generan en el server como SVG para que se vean identicos en el navegador, en
la descarga HTML (self-contained) y en el PDF (Playwright), sin depender de una
libreria externa ni del timing de render de un <canvas>.

Cada funcion devuelve un string <svg ...> listo para inyectar con `| safe`.
"""
from __future__ import annotations

import math
from html import escape
from typing import Mapping, Sequence

# Paleta para las series por canal (coherente con la del reporte).
PALETTE = ["#00C2CB", "#00B87A", "#F59E0B", "#6366F1", "#EC4899", "#9333EA"]
_PRIMARY = "#0D1B2A"
_GRID = "#e5edf5"
_AXIS = "#9fb0c0"


def money_short(v: float) -> str:
    """1234567 -> '$1,2M'; 8500 -> '$9K'; usa coma decimal (locale CL)."""
    v = float(v or 0)
    a = abs(v)
    if a >= 1_000_000:
        s = f"${v / 1_000_000:.1f}M"
    elif a >= 1_000:
        s = f"${v / 1_000:.0f}K"
    else:
        s = f"${v:.0f}"
    return s.replace(".", ",")


def _nice_ceil(x: float) -> float:
    """Redondea hacia arriba a 1/2/5 * 10^k para un tope de eje 'lindo'."""
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    base = 10 ** exp
    f = x / base
    nice = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nice * base


def _ticks(ymax: float, n: int = 4) -> list[float]:
    return [ymax * i / n for i in range(n + 1)]


def bar_chart_svg(
    labels: Sequence[str],
    values: Sequence[float],
    width: int = 1080,
    height: int = 200,
    color: str = "#00C2CB",
) -> str:
    """Barras verticales (venta total por mes) con eje Y, grilla y etiquetas."""
    n = max(len(labels), 1)
    padL, padR, padT, padB = 78, 24, 26, 40
    plotW = width - padL - padR
    plotH = height - padT - padB
    vmax = max(values) if values else 0
    ymax = _nice_ceil(vmax if vmax > 0 else 1)
    bw = (plotW / n) * 0.55

    def cx(i: int) -> float:
        return padL + plotW * (i + 0.5) / n

    def yv(v: float) -> float:
        return padT + plotH * (1 - v / ymax)

    p: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Lato, sans-serif" width="100%" role="img">'
    ]
    for t in _ticks(ymax):
        gy = yv(t)
        p.append(f'<line x1="{padL}" y1="{gy:.1f}" x2="{width - padR}" y2="{gy:.1f}" stroke="{_GRID}" stroke-width="1"/>')
        p.append(f'<text x="{padL - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#999">{money_short(t)}</text>')
    for i, v in enumerate(values):
        h = plotH * (v / ymax) if ymax else 0
        x = cx(i) - bw / 2
        y = padT + plotH - h
        p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3" fill="{color}"/>')
        if v > 0:
            p.append(f'<text x="{cx(i):.1f}" y="{y - 5:.1f}" text-anchor="middle" font-size="10.5" font-weight="700" fill="{_PRIMARY}">{money_short(v)}</text>')
        p.append(f'<text x="{cx(i):.1f}" y="{height - padB + 18:.1f}" text-anchor="middle" font-size="11.5" fill="#555">{escape(str(labels[i]))}</text>')
    p.append(f'<line x1="{padL}" y1="{padT + plotH:.1f}" x2="{width - padR}" y2="{padT + plotH:.1f}" stroke="{_AXIS}" stroke-width="1.5"/>')
    p.append("</svg>")
    return "".join(p)


def line_chart_svg(
    labels: Sequence[str],
    series: Mapping[str, Sequence[float]],
    width: int = 1080,
    height: int = 220,
) -> str:
    """Multi-linea (una serie por canal) con leyenda, grilla y marcadores."""
    n = max(len(labels), 1)
    padL, padR, padT, padB = 78, 24, 48, 40  # padT extra para la leyenda
    plotW = width - padL - padR
    plotH = height - padT - padB
    all_vals = [v for s in series.values() for v in s]
    vmax = max(all_vals) if all_vals else 0
    ymax = _nice_ceil(vmax if vmax > 0 else 1)

    def cx(i: int) -> float:
        return padL + plotW * (i + 0.5) / n

    def yv(v: float) -> float:
        return padT + plotH * (1 - v / ymax)

    p: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Lato, sans-serif" width="100%" role="img">'
    ]
    for t in _ticks(ymax):
        gy = yv(t)
        p.append(f'<line x1="{padL}" y1="{gy:.1f}" x2="{width - padR}" y2="{gy:.1f}" stroke="{_GRID}" stroke-width="1"/>')
        p.append(f'<text x="{padL - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#999">{money_short(t)}</text>')
    # leyenda
    lx = padL
    for idx, name in enumerate(series.keys()):
        col = PALETTE[idx % len(PALETTE)]
        p.append(f'<rect x="{lx:.0f}" y="12" width="12" height="12" rx="2" fill="{col}"/>')
        p.append(f'<text x="{lx + 17:.0f}" y="22" font-size="12" fill="#444">{escape(str(name))}</text>')
        lx += 34 + len(str(name)) * 7.0
    # lineas + marcadores
    for idx, (name, vals) in enumerate(series.items()):
        col = PALETTE[idx % len(PALETTE)]
        pts = " ".join(f"{cx(i):.1f},{yv(v):.1f}" for i, v in enumerate(vals))
        p.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(vals):
            p.append(f'<circle cx="{cx(i):.1f}" cy="{yv(v):.1f}" r="3.2" fill="{col}"/>')
    for i, lab in enumerate(labels):
        p.append(f'<text x="{cx(i):.1f}" y="{height - padB + 18:.1f}" text-anchor="middle" font-size="11.5" fill="#555">{escape(str(lab))}</text>')
    p.append(f'<line x1="{padL}" y1="{padT + plotH:.1f}" x2="{width - padR}" y2="{padT + plotH:.1f}" stroke="{_AXIS}" stroke-width="1.5"/>')
    p.append("</svg>")
    return "".join(p)
