"""Rule based fallback classifier: keyword scoring plus a vendor hint.

Deliberately simple and transparent. It is used when the LLM is not
configured, times out, errors, or answers with something invalid.
"""
from __future__ import annotations

import re

from app.reference import VENDORS, default_cost_center

KEYWORDS: dict[str, tuple[str, ...]] = {
    "IT_SOFTWARE": ("licence", "license", "saas", "subscription", "hosting", "cloud", "software"),
    "IT_HARDWARE": ("laptop", "monitor", "hardware", "docking", "server"),
    "CONSULTING": ("consulting", "audit", "advisory", "consultant"),
    "MARKETING": ("campaign", "advertising", "brochure", "flyer", "trade fair", "marketing"),
    "TRAVEL": ("flight", "hotel", "train", "travel", "rental car"),
    "OFFICE_SUPPLIES": ("paper", "toner", "office supplies", "pens"),
    "FACILITIES": ("cleaning", "security service", "freight", "waste", "maintenance"),
    "TRAINING": ("training", "seminar", "workshop", "course"),
}

_VENDOR_HINT = {name.lower(): cat for name, cat, _ in VENDORS.values() if cat}


def matches(text_lower: str, keyword: str) -> bool:
    """Whole-word match that also accepts a plural 's'/'es' ('train' does not hit 'training')."""
    return re.search(r"\b" + re.escape(keyword) + r"(?:s|es)?\b", text_lower) is not None


def score(text: str, vendor_name: str | None = None) -> dict[str, float]:
    lowered = text.lower()
    scores = {cat: float(sum(1 for kw in kws if matches(lowered, kw))) for cat, kws in KEYWORDS.items()}
    hint = _VENDOR_HINT.get((vendor_name or "").lower())
    if hint:
        scores[hint] += 1.0
    return scores


def classify(text: str, vendor_name: str | None = None) -> tuple[str | None, str | None, float, str]:
    """Return ``(category, cost_center, confidence, rationale)``.

    confidence = min(0.9, 0.45 + 0.15 * top) * top / total
      one keyword, nothing else  -> 0.60 (goes to a human at threshold 0.75)
      two signals, no competitor -> 0.75
      competing categories       -> scaled down by their share
    """
    scores = score(text, vendor_name)
    total = sum(scores.values())
    if total == 0:
        return None, None, 0.0, "no keyword matched"
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    category, top = ranked[0]
    confidence = round(min(0.9, 0.45 + 0.15 * top) * top / total, 2)
    matched = [kw for kw in KEYWORDS[category] if matches(text.lower(), kw)]
    return category, default_cost_center(category), confidence, f"rules matched {matched or ['vendor hint']}"
