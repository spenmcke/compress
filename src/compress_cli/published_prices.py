"""Offline publisher list-price snapshots for API-equivalent input estimates.

These rates are estimates for supported model providers, not a user invoice.
Unknown model variants intentionally have no fallback price.
"""

from dataclasses import dataclass
import re


AS_OF = "2026-09-19"
ZAI_SOURCE = "https://docs.z.ai/guides/overview/pricing"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"


@dataclass(frozen=True)
class InputPrice:
    name: str
    input: float
    cached: float
    publisher: str
    source: str


PRICES = {
    "glm-5.3-flash": InputPrice("GLM 5.3 Flash", 0.15, 0.03, "Z.ai", ZAI_SOURCE),
    "glm-5.3-flashx": InputPrice("GLM 5.3 FlashX", 0.37, 0.075, "Z.ai", ZAI_SOURCE),
    "glm-5.3": InputPrice("GLM 5.3", 1.4, 0.26, "Z.ai", ZAI_SOURCE),
    "glm-5.2": InputPrice("GLM 5.2", 1.4, 0.26, "Z.ai", ZAI_SOURCE),
    "glm-4.7-flash": InputPrice("GLM 4.7 Flash", 0, 0, "Z.ai", ZAI_SOURCE),
    "gpt-6-astra": InputPrice("GPT-6 Astra", 10, 1, "OpenAI", OPENAI_SOURCE),
    "gpt-5.6-sol": InputPrice("GPT-5.6 Sol", 4, 0.4, "OpenAI", OPENAI_SOURCE),
    "gpt-5.6-terra": InputPrice("GPT-5.6 Terra", 2, 0.2, "OpenAI", OPENAI_SOURCE),
    "gpt-5.6-luna": InputPrice("GPT-5.6 Luna", 0.2, 0.02, "OpenAI", OPENAI_SOURCE),
    "gpt-5.5": InputPrice("GPT-5.5", 5, 0.5, "OpenAI", OPENAI_SOURCE),
    "gpt-5.4": InputPrice("GPT-5.4", 2.5, 0.25, "OpenAI", OPENAI_SOURCE),
    "gpt-5.4-mini": InputPrice("GPT-5.4 mini", 0.75, 0.075, "OpenAI", OPENAI_SOURCE),
    "gpt-5.2": InputPrice("GPT-5.2", 1.75, 0.175, "OpenAI", OPENAI_SOURCE),
}


def lookup(model: str) -> InputPrice | None:
    key = model.casefold()
    for prefix in ("z-ai/", "zai/", "openai/"):
        if key.startswith(prefix):
            candidate = key[len(prefix):]
            if (prefix == "openai/" and candidate.startswith("gpt-")) or (
                prefix != "openai/" and candidate.startswith("glm-")
            ):
                key = candidate
            break
    return PRICES.get(key)


def clean_model(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", value
    ) else "unknown"


def estimate(compression: dict) -> dict:
    rows = []
    unpriced = 0
    subtotal = cached_subtotal = 0.0
    groups = compression.get("by_model") or {}
    if not groups and compression.get("potential_context_tokens_saved", 0):
        groups = {"unknown": compression}
    for model, counts in groups.items():
        price = lookup(model)
        saved = counts["potential_context_tokens_saved"]
        row = {
            "model": model, "name": price.name if price else model,
            "saved_tokens_estimate": saved,
            "input_usd_per_million": price.input if price else None,
            "cached_input_usd_per_million": price.cached if price else None,
            "uncached_usd_estimate": saved * price.input / 1_000_000 if price else None,
            "cached_usd_estimate": saved * price.cached / 1_000_000 if price else None,
            "publisher": price.publisher if price else None,
            "source": price.source if price else None,
            "price_checked_on": AS_OF if price else None,
        }
        rows.append(row)
        if price:
            subtotal += row["uncached_usd_estimate"]
            cached_subtotal += row["cached_usd_estimate"]
        else:
            unpriced += saved
    return {
        "basis": "publisher_standard_input_list_price",
        "price_checked_on": AS_OF,
        "uncached_usd_estimate": None if unpriced or not rows else subtotal,
        "cached_usd_estimate": None if unpriced or not rows else cached_subtotal,
        "priced_uncached_subtotal_usd": subtotal,
        "unpriced_tokens_estimate": unpriced,
        "by_model": rows,
        "assumptions": "One input use per bridge result; standard text pricing; no long-context or service-tier premiums; compressor cost excluded. Not an invoice or net saving.",
    }
