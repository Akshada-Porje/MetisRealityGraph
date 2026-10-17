"""Minimal Gemini REST client (stdlib only) with on-disk response cache.

Uses the public generateContent REST endpoint. Model and key come from the environment:
  GEMINI_API_KEY, GEMINI_MODEL. We do not set temperature unless METIS_TEMPERATURE is set,
because some Gemini 3 guidance recommends leaving it at the default; the cache plus
repeated samples (not temperature 0) is how we get reproducible numbers.
Cannot be exercised in the offline build sandbox: verify with `python -m metis.smoke`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """Daily (or otherwise non-retryable) quota hit: waiting a minute will not help."""


_sleep = time.sleep   # patched in tests


def retry_delay_seconds(body: str) -> float | None:
    """Server-suggested wait from a Google API error body (RetryInfo.retryDelay, e.g. '34s')."""
    try:
        for d in json.loads(body).get("error", {}).get("details", []):
            if str(d.get("@type", "")).endswith("RetryInfo") and d.get("retryDelay"):
                return float(str(d["retryDelay"]).rstrip("s"))
    except (ValueError, AttributeError, TypeError):
        pass
    m = re.search(r"retry in ([0-9.]+)s", body)
    return float(m.group(1)) if m else None


@dataclass
class LLMResult:
    text: str
    parsed: dict | None
    prompt_tokens: int
    output_tokens: int
    latency_s: float
    cached: bool = False


def parse_json_object(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    try:
        obj = json.loads(t.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 cache_dir: str | None = ".cache/llm", timeout: float = 90.0):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        if not self.model:
            raise LLMError("GEMINI_MODEL is not set (pick one from models.list)")
        self.timeout = timeout
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)

    def _key(self, system: str, user: str, repeat: int) -> str:
        blob = json.dumps([self.model, system, user, repeat], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def generate_json(self, system: str, user: str, repeat: int = 0) -> LLMResult:
        ck = self._key(system, user, repeat)
        if self.cache and (self.cache / f"{ck}.json").exists():
            d = json.loads((self.cache / f"{ck}.json").read_text())
            return LLMResult(d["text"], parse_json_object(d["text"]), d["pt"], d["ot"], d["lat"], True)

        gen_cfg: dict = {"responseMimeType": "application/json"}
        if os.environ.get("METIS_TEMPERATURE"):
            gen_cfg["temperature"] = float(os.environ["METIS_TEMPERATURE"])
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        url = os.environ.get("METIS_ENDPOINT", ENDPOINT).format(model=self.model)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        last = ""
        for attempt in range(6):
            t0 = time.time()
            wait = 2 ** attempt * 3
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                text_body = e.read().decode(errors="replace")
                last = f"HTTP {e.code}: {text_body[:400]}"
                if e.code == 429:
                    if "PerDay" in text_body or "per day" in text_body.lower():
                        raise QuotaExhausted(last) from e
                    wait = (retry_delay_seconds(text_body) or wait) + 1
                elif e.code not in (500, 502, 503, 504):
                    raise LLMError(last) from e
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"network: {e}"
            _sleep(min(wait, 95))
        else:
            raise LLMError(f"Gemini call failed after retries: {last}")
        lat = time.time() - t0

        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from e
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        u = data.get("usageMetadata", {})
        pt, ot = int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
        if self.cache:
            (self.cache / f"{ck}.json").write_text(json.dumps({"text": text, "pt": pt, "ot": ot, "lat": lat}))
        return LLMResult(text, parse_json_object(text), pt, ot, lat)
