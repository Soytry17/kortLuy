"""Chart generation for Telegram bot reports.

Uses the non-interactive Agg backend so it works on headless servers.
All functions return a BytesIO PNG buffer ready to pass to reply_photo().
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # must be set before importing pyplot

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from io import BytesIO

# ── Palette ───────────────────────────────────────────────────────────────────
_BG = "#1e1e2e"
_PANEL = "#16213e"
_INCOME = "#4ade80"   # soft green
_EXPENSE = "#f87171"  # soft red
_NET_POS = "#60a5fa"  # blue for positive net
_NET_NEG = "#fb923c"  # orange for negative net
_TEXT = "#e2e8f0"
_GRID = "#2d3748"
_LABEL = "#94a3b8"


def _fmt_money(cents: int, currency: str = "USD") -> str:
    v = abs(cents) / 100
    if currency == "USD":
        return f"${v:,.0f}" if v >= 10 else f"${v:,.2f}"
    return f"{v:,.0f}"


def _bar_labels(ax: plt.Axes, bars, currency: str) -> None:
    for bar in bars:
        h = bar.get_height()
        if h <= 0:
            continue
        ax.annotate(
            _fmt_money(int(h * 100), currency),
            xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            color=_TEXT,
            fontsize=8.5,
            fontweight="bold",
        )


def _style_ax(ax: plt.Axes, title: str, currency: str) -> None:
    ax.set_facecolor(_PANEL)
    ax.set_title(title, color=_TEXT, fontsize=13, fontweight="bold", pad=12)
    ax.tick_params(axis="x", colors=_LABEL, labelsize=9)
    ax.tick_params(axis="y", colors=_LABEL, labelsize=8)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: _fmt_money(int(v * 100), currency))
    )
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color(_GRID)
    ax.yaxis.grid(True, color=_GRID, linewidth=0.6, linestyle="--", alpha=0.7)
    ax.set_axisbelow(True)



def build_goal_chart(
    *,
    goals: list[dict],  # each: {name, current_cents, target_cents}
    currency: str = "USD",
) -> BytesIO:
    """Horizontal progress bar chart for savings goals."""
    if not goals:
        goals = [{"name": "No goals", "current_cents": 0, "target_cents": 1}]

    n = len(goals)
    fig_h = max(2.5, 0.7 * n + 1.5)
    fig, ax = plt.subplots(figsize=(9, fig_h), facecolor=_BG)
    fig.subplots_adjust(left=0.22, right=0.88, top=0.88, bottom=0.1)

    names = [g["name"] for g in goals]
    pcts = [
        min(g["current_cents"] / g["target_cents"] * 100, 100) if g["target_cents"] else 0
        for g in goals
    ]
    colors = [_INCOME if p >= 100 else _NET_POS for p in pcts]

    y = np.arange(n)
    ax.barh(y, [100] * n, color=_GRID, alpha=0.4, zorder=2, height=0.55)
    bars = ax.barh(y, pcts, color=colors, alpha=0.9, zorder=3, height=0.55)

    ax.set_facecolor(_PANEL)
    ax.set_xlim(0, 110)
    ax.set_yticks(y)
    ax.set_yticklabels(names, color=_TEXT, fontsize=10)
    ax.tick_params(axis="x", colors=_LABEL, labelsize=8)
    ax.set_xlabel("Progress (%)", color=_LABEL, fontsize=9)
    ax.set_title("Savings Goals", color=_TEXT, fontsize=13, fontweight="bold", pad=10)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color(_GRID)

    for bar, g in zip(bars, goals):
        cur = _fmt_money(g["current_cents"], currency)
        tgt = _fmt_money(g["target_cents"], currency)
        pct = min(int(g["current_cents"] / g["target_cents"] * 100), 100) if g["target_cents"] else 0
        label = f"  {cur} / {tgt}  ({pct}%)"
        ax.text(
            bar.get_width() + 1,
            bar.get_y() + bar.get_height() / 2,
            label,
            va="center",
            color=_TEXT,
            fontsize=8.5,
        )

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=_BG)
    buf.seek(0)
    plt.close(fig)
    return buf
