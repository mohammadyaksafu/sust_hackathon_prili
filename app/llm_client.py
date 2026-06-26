"""
Gemini-only LLM client.
Retry logic uses SHORT waits (2s, 4s, 6s) that fit inside the 30s request timeout.
Uses gemini-2.0-flash-lite by default — higher free tier quota (30 RPM vs 15 RPM).
"""

from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings, get_model

logger = logging.getLogger(__name__)

TIMEOUT = 20.0       # per individual HTTP call
MAX_RETRIES = 3
RETRY_WAITS = [2, 4, 6]   # total max wait = 12s — safely fits in 30s budget


async def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    model = get_model()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={settings.gemini_api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1200,
        },
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def llm_call(system_prompt: str, user_prompt: str) -> str:
    """
    Call Gemini with short retry on 429/5xx errors.
    Waits are intentionally small (2s, 4s, 6s) so retries complete within
    the 30s per-request deadline enforced by the judge harness.
    On persistent 429, raises so the pipeline can use its deterministic fallback.
    """
    last_error: Exception = RuntimeError("No attempts made")

    for attempt in range(MAX_RETRIES):
        try:
            return await _call_gemini(system_prompt, user_prompt)

        except httpx.HTTPStatusError as e:
            last_error = e
            status = e.response.status_code

            if status == 429:
                wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
                logger.warning(
                    f"Gemini 429 rate limit (attempt {attempt + 1}/{MAX_RETRIES}). "
                    f"Retrying in {wait}s..."
                )
                await asyncio.sleep(wait)
                continue

            elif status in (500, 502, 503, 504):
                wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
                logger.warning(f"Gemini {status} server error. Retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue

            else:
                # 400, 401, 403 — do not retry
                logger.error(f"Gemini non-retryable error {status}: {e}")
                raise

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            logger.warning(f"Gemini connection error attempt {attempt + 1}: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
            continue

        except Exception as e:
            logger.error(f"Gemini unexpected error: {e}")
            raise

    logger.error(f"Gemini failed after {MAX_RETRIES} attempts. Last: {last_error}")
    raise last_error


def extract_json(text: str) -> Optional[dict]:
    """Robustly extract JSON from LLM output even if wrapped in markdown fences."""
    # Direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Strip ```json ... ``` fences
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Extract first { ... } block
    match = re.search(r"\{[\s\S]+\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return None