"""Deterministic stand-in for an LLM endpoint. NOT a real model.

Same input -> same output, on every machine and Python version (uses SHA-256,
never ``hash()``). It has a slightly larger vocabulary than the rule based
fallback so the two disagree on some texts, like a real model would.

Use it in-process via ``mock_transport()`` (tests, seed) or run it as a local
HTTP service:

    uvicorn ai.mock_model:app --port 8100
    FAL_LLM_URL=http://127.0.0.1:8100/v1/classify
"""
from __future__ import annotations

import hashlib
import json

import httpx
from fastapi import FastAPI

from ai.rules import KEYWORDS, matches
from app.reference import VENDORS, default_cost_center

EXTRA_VOCAB: dict[str, tuple[str, ...]] = {
    "IT_SOFTWARE": ("antivirus", "backup storage", "crm", "vcpu", "seats"),
    "IT_HARDWARE": ("headset", "keyboard", "mice", "network switch", "notebook"),
    "CONSULTING": ("analytics project", "controller", "milestone", "process analysis", "tax"),
    "MARKETING": ("social media", "sponsoring", "booth", "print run", "ad"),
    "TRAVEL": ("taxi", "nights", "offsite"),
    "OFFICE_SUPPLIES": ("folders", "desk organizer", "copy paper"),
    "FACILITIES": ("pallets", "air conditioning", "night shifts", "disposal"),
    "TRAINING": ("participants", "first aid", "language course", "leadership"),
}
VOCAB = {cat: KEYWORDS[cat] + EXTRA_VOCAB[cat] for cat in KEYWORDS}
_VENDOR_HINT = {name.lower(): cat for name, cat, _ in VENDORS.values() if cat}


def _jitter(text: str) -> float:
    """0.00 .. 0.04, stable per text."""
    return (int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 5) / 100


def predict(text: str, vendor: str | None = None) -> dict:
    lowered = text.lower()
    scores = {cat: sum(1 for w in words if matches(lowered, w)) for cat, words in VOCAB.items()}
    hint = _VENDOR_HINT.get((vendor or "").lower())
    if hint:
        scores[hint] += 1
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    (category, top), (_, second) = ranked[0], ranked[1]
    if top == 0:
        # a real LLM always answers something - with low confidence
        category = "CONSULTING"
        confidence = 0.35 + _jitter(text)
        rationale = "No clear signal in the text; guessed a generic service category."
    else:
        confidence = min(0.98, 0.62 + 0.12 * min(top, 3) - 0.25 * (second / top) + _jitter(text))
        rationale = f"Text and vendor point to {category} ({top} signals, runner-up {second})."
    return {
        "category": category,
        "cost_center": default_cost_center(category),
        "confidence": round(confidence, 2),
        "rationale": rationale,
    }


def _handle(request: httpx.Request) -> httpx.Response:
    try:
        body = json.loads(request.content or b"{}")
        text = str(body["text"])
    except (ValueError, KeyError):
        return httpx.Response(400, json={"error": "expected JSON with 'text'"})
    return httpx.Response(200, json=predict(text, body.get("vendor")))


def mock_transport() -> httpx.MockTransport:
    """In-process transport: no network, no server needed."""
    return httpx.MockTransport(_handle)


MOCK_URL = "http://mock-llm.local/v1/classify"

app = FastAPI(title="Mock LLM classifier (deterministic, not a real model)")


@app.post("/v1/classify")
def classify_endpoint(body: dict) -> dict:
    return predict(str(body.get("text", "")), body.get("vendor"))
