"""Dependency-free terminal presentation for savings reports."""

from __future__ import annotations

import textwrap
import unicodedata

from . import published_prices


def money(value: float | None) -> str:
    if value is None:
        return "Unavailable"
    if 0 < value < 0.000001:
        return "<$0.000001"
    return f"${value:,.6f}" if value < 1 else f"${value:,.4f}"


def _safe(value: object) -> str:
    return "".join(char if not unicodedata.category(char).startswith("C") else " " for char in str(value))


def render(summary: dict, totals: dict, *, details: bool = False,
           color: bool = False, width: int = 80, unicode: bool = True) -> str:
    rows: list[tuple[str, str]] = []

    def add(text: str = "", style: str = "") -> None:
        rows.append((_safe(text), style))

    def add_impact(percent: float, label: str, saved: int,
                   comparison: str, compressed_results: int) -> None:
        add(f"{percent:.1f}% {label}", "success")
        filled = min(20, round(percent / 5))
        bar = ("█" if unicode else "#") * filled + ("░" if unicode else ".") * (20 - filled)
        add(f"{bar}  {saved:,} tokens removed (est.)", "success")
        add(comparison)
        add(f"{compressed_results} compressed results | this run only")

    def add_no_compression(detail: str) -> None:
        add("NO TOOL OUTPUTS COMPRESSED", "muted")
        add(detail)

    def add_price_note() -> None:
        add("List-price estimate; actual billing and cache usage may differ.", "muted")

    add("COMPRESS SAVINGS  /  CODEX", "title")
    models = summary.get("models") or summary.get("by_model", {})
    add("Model: " + (", ".join(models) or "Not recorded"))
    add()
    if totals["usage_complete"]:
        percent = totals["token_reduction_percent"]
        compressed_results = int(summary.get("compressed_results", 0))
        comparison = (f"With compress {totals['actual_tokens']:,} | "
                      f"Without compress ~{totals['without_compress_tokens_estimate']:,}")
        if compressed_results:
            add_impact(percent, "ESTIMATED SESSION TOKEN SAVINGS",
                       totals["avoided_tokens_estimate"], comparison,
                       compressed_results)
        else:
            add_no_compression(comparison)
        add()
        add("ESTIMATED API INPUT SAVINGS", "title")
        add(money(totals["avoided_cost_usd_estimate"]), "success")
        add(f"With compress {money(totals['actual_cost_usd'])} | "
            f"Without compress ~{money(totals['without_compress_cost_usd_estimate'])}")
        add_price_note()
    else:
        add(f"{totals['observed_avoided_tokens_estimate']:,} TOKENS REMOVED (EST.)", "success")
        add("Response usage was not recorded, so percentage and cost cannot be calculated.",
            "warning")
    if details:
        add()
        add("TOKEN DETAILS", "title")
        if not totals["usage_complete"]:
            add(f"Usage coverage: {totals['usage_responses']}/{totals['responses']} responses.")
        add("Savings = removed / (recorded input + output + removed).")
        add("Both sums cover the same responses. Cached input counts once.")
        add("Assumes unchanged model outputs and execution steps.")
        for label, buckets in (("Actual", summary.get("actual") or {}),
                               ("Avoided (est.)", summary.get("avoided") or {})):
            add(label + ": " + ", ".join(f"{value:,} {key.replace('_', ' ')}" for key, value in buckets.items()))
        add("Pricing basis: standard short-context rates; tier/region premiums excluded.")
        add("Price source: " + published_prices.OPENAI_SOURCE, "muted")
        add("Price snapshot: " + str(summary.get("pricing_as_of", "unknown")), "muted")
    if not details:
        if not totals["usage_complete"]:
            add("Use --details for usage diagnostics.", "muted")
        else:
            add("Use --details for pricing sources and accounting notes.", "muted")

    outer = max(24, min(88, width))
    inner = outer - 4
    styles = {"title": "1;36", "success": "1;32", "warning": "33", "muted": "2"}
    top, bottom, side, rule = ("╭╮", "╰╯", "│", "─") if unicode else ("++", "++", "|", "-")
    lines = [top[0] + rule * (outer - 2) + top[1]]
    for text, style in rows:
        for line in textwrap.wrap(text, width=inner, break_on_hyphens=False) or [""]:
            padded = line.ljust(inner)
            if color and style in styles:
                padded = f"\x1b[{styles[style]}m{padded}\x1b[0m"
            lines.append(f"{side} {padded} {side}")
    lines.append(bottom[0] + rule * (outer - 2) + bottom[1])
    return "\n".join(lines)
