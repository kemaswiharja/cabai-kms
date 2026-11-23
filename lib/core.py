"""Shared infrastructure: config, MongoDB, LLM client, admin auth, text search."""
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache

import httpx
from fastapi import Header, HTTPException

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "cabai_kms")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Any OpenAI-compatible endpoint serving an open-weight model.
# Default: Groq (free tier) running Meta Llama 3.3 70B.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "llama-3.1-8b-instant")


# ── MongoDB ───────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_db():
    if os.getenv("CABAI_MOCK_DB") == "1":
        import mongomock
        return mongomock.MongoClient()[MONGODB_DB]
    if not MONGODB_URI:
        raise HTTPException(503, "MONGODB_URI belum diset di Environment Variables Vercel.")
    from pymongo import MongoClient
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)[MONGODB_DB]


def db_status() -> str:
    try:
        get_db().command("ping")
        return "connected"
    except Exception as e:  # noqa: BLE001
        return f"error: {str(e)[:80]}"


# ── Admin auth ────────────────────────────────────────────────────────────────
def require_admin(x_admin_password: str | None = Header(default=None)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD belum diset di Environment Variables Vercel.")
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(401, "Password admin salah.")
    return True


# ── LLM ───────────────────────────────────────────────────────────────────────
class LLMError(RuntimeError):
    pass


def llm_available() -> bool:
    return bool(LLM_API_KEY) or "localhost" in LLM_BASE_URL or os.getenv("CABAI_FAKE_LLM") == "1"


# Open-weight models tried in order when a model is unavailable for the account.
FALLBACK_MODELS = [m for m in os.getenv(
    "LLM_FALLBACK_MODELS", "llama-3.3-70b-versatile,openai/gpt-oss-120b,openai/gpt-oss-20b").split(",") if m]
_dead_models: set[str] = set()
LAST_MODEL_USED = None


def _post(model, messages, json_mode, temperature, max_tokens):
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if "gpt-oss" in model:  # reasoning models: keep thinking short so the answer fits
        body["reasoning_effort"] = "low"
        body["max_tokens"] = max_tokens + 1024
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return httpx.post(f"{LLM_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"}, json=body, timeout=45)


def llm_chat(messages, *, fast=False, json_mode=False, temperature=0.2, max_tokens=1500) -> str:
    if os.getenv("CABAI_FAKE_LLM") == "1":  # test hook
        from tests import fake_llm  # type: ignore
        return fake_llm.respond(messages, json_mode)
    if not llm_available():
        raise LLMError("LLM_API_KEY belum diset (lihat README).")
    first = LLM_FAST_MODEL if fast else LLM_MODEL
    chain = [m for m in dict.fromkeys([first, LLM_MODEL, *FALLBACK_MODELS]) if m not in _dead_models]
    last = "tidak ada model yang tersedia"
    for model in chain:
        try:
            r = _post(model, messages, json_mode, temperature, max_tokens)
        except httpx.HTTPError as e:
            raise LLMError(f"Tidak bisa menghubungi LLM: {e}") from e
        if r.status_code == 404 or "model_not_found" in r.text or "model_decommissioned" in r.text:
            _dead_models.add(model)
            last = f"{model}: {r.text[:200]}"
            continue
        if r.status_code == 400 and json_mode and "json_validate_failed" in r.text:
            r = _post(model, messages, False, temperature, max_tokens)  # retry, parse JSON ourselves
        if r.status_code >= 400:
            raise LLMError(f"LLM error {r.status_code}: {r.text[:300]}")
        global LAST_MODEL_USED
        LAST_MODEL_USED = model
        return r.json()["choices"][0]["message"]["content"] or ""
    raise LLMError(f"Semua model LLM ditolak ({last})")


def llm_json(messages, **kw) -> dict:
    kw["max_tokens"] = max(kw.get("max_tokens", 0), 400)
    raw = llm_chat(messages, json_mode=True, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
        raise LLMError(f"LLM tidak mengembalikan JSON: {raw[:200]}")


# ── Lightweight BM25 (no numpy, keeps the Vercel bundle small) ────────────────
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(
    "yang dan di ke dari untuk dengan pada adalah ini itu atau dalam juga akan oleh "
    "the of and to in is for on with as by an be are at this that from a".split()
)


def tokenize(text: str):
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


def bm25_rank(query: str, docs: list[str], k1=1.5, b=0.75):
    """Return [(index, score)] sorted best-first."""
    q = tokenize(query)
    if not q or not docs:
        return []
    toks = [tokenize(d) for d in docs]
    n = len(toks)
    avgdl = sum(len(t) for t in toks) / n or 1
    df = Counter()
    for t in toks:
        df.update(set(t))
    scores = []
    for i, t in enumerate(toks):
        tf = Counter(t)
        dl = len(t) or 1
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores.append((i, s))
    scores.sort(key=lambda x: -x[1])
    return scores


def norm_key(s: str) -> str:
    """Normalise an attribute/property name for matching."""
    s = re.sub(r"[^0-9a-zA-Z]+", " ", str(s or "")).strip().lower()
    return re.sub(r"\s+", "_", s)
