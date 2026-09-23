"""Server-side chart geometry. The strict CSP forbids inline styles, so bar lengths are CSS width
classes (w-0..w-100) and the trend chart is an SVG whose shapes are computed here.

Layout follows the page direction (RTL): bars grow from the right, and time runs right -> left
(oldest month on the right, newest on the left)."""
from __future__ import annotations

import math

from . import jalali, money

# Sized for a phone (CSS caps it at ~560px wide), so 11-unit text stays ~11px where it's read.
W, H = 400, 220
PAD_TOP, PAD_BOTTOM = 12, 40  # bottom band holds the month + year labels
PAD_AXIS, PAD_END = 60, 4     # y-axis labels sit on the right (the start side in RTL)
BAR, GAP, RADIUS = 12, 2, 4   # thin columns, 2px surface gap, 4px rounded data end
MONTHS = 6                    # six full month names fit; the table view below lists 12


def to_unit(rial: int, unit: str) -> float:
    return rial / 10 if unit == "toman" else float(rial)


def compact(value: float) -> str:
    """Axis/tooltip shorthand in Persian: 12500000 -> '۱۲٫۵ میلیون'."""
    v = abs(value)
    for size, word in ((1e9, "میلیارد"), (1e6, "میلیون"), (1e3, "هزار")):
        if v >= size:
            x = v / size
            s = f"{x:.1f}".rstrip("0").rstrip(".") if x < 100 else f"{x:.0f}"
            return money.fa_digits(s.replace(".", "٫")) + " " + word
    return money.fa_digits(f"{v:.0f}")


def nice_ticks(peak: float, count: int = 4) -> list[float]:
    """0 plus `count` clean gridline values (1/2/2.5/5 x 10^k) covering peak."""
    if peak <= 0:
        return [0.0, 1.0]
    raw = peak / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [step * i for i in range(count + 1)]


def width_class(part: float, whole: float) -> str:
    if whole <= 0 or part <= 0:
        return "w-0"
    return f"w-{max(1, min(100, round(100 * part / whole)))}"


def column_path(x: float, y_top: float, y_base: float, w: float) -> str:
    """Column with a rounded data end (top) and a square baseline."""
    h = y_base - y_top
    r = min(RADIUS, h, w / 2)
    return (f"M{x:.1f},{y_base:.1f}V{y_top + r:.1f}Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top + r:.1f}V{y_base:.1f}Z")


def trend_chart(rows: list[dict], unit: str, selected: tuple[int, int]) -> dict:
    """rows: oldest first, each {jy, jm, income, expense} in rial."""
    peak = max([0.0] + [to_unit(max(r["income"], r["expense"]), unit) for r in rows])
    ticks = nice_ticks(peak)
    top = ticks[-1]
    plot_w = W - PAD_AXIS - PAD_END
    plot_h = H - PAD_TOP - PAD_BOTTOM
    base = PAD_TOP + plot_h
    slot = plot_w / max(1, len(rows))

    def y(v: float) -> float:
        return base - plot_h * (v / top)

    grid = [{"y": round(y(t), 1), "label": compact(t)} for t in ticks]
    months = []
    for i, r in enumerate(rows):
        right = W - PAD_AXIS - i * slot          # slot i spans [right - slot, right]
        center = right - slot / 2
        pair_w = 2 * BAR + GAP
        x_income = center + pair_w / 2 - BAR     # income on the right (first in RTL reading order)
        x_expense = x_income - GAP - BAR
        inc, exp = to_unit(r["income"], unit), to_unit(r["expense"], unit)
        months.append({
            "key": f"{r['jy']:04d}-{r['jm']:02d}",
            "label": jalali.MONTHS[r["jm"] - 1],
            "title": money.fa_digits(jalali.month_title(r["jy"], r["jm"])),
            "x": round(right - slot, 1), "w": round(slot, 1), "cx": round(center, 1),
            "income_path": column_path(x_income, y(inc), base, BAR) if inc > 0 else "",
            "expense_path": column_path(x_expense, y(exp), base, BAR) if exp > 0 else "",
            "income": money.format_amount(r["income"], unit), "expense": money.format_amount(r["expense"], unit),
            "selected": (r["jy"], r["jm"]) == selected,
            "jy_label": money.fa_digits(r["jy"]) if r["jm"] == 1 or i == 0 else "",
        })
    return {"w": W, "h": H, "grid": grid, "months": months, "base": base, "axis_x": W - PAD_AXIS + 6,
            "plot_left": PAD_END, "plot_right": W - PAD_AXIS, "label_y": base + 18, "year_y": base + 33,
            "top": PAD_TOP, "plot_h": plot_h, "empty": peak == 0}
