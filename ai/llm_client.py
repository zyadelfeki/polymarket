"""
Local LLM client — Phi-3 Mini 3.8B via ollama (localhost:11434).
Hardware: i3-8100, 16 GB DDR4, GTX 1050 Ti 4 GB VRAM.
Hard timeout: 15 seconds. Failure = None, never raises.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional, Union

import aiohttp

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "phi3:3.8b"
TIMEOUT_SECONDS = 15.0


async def llm_query(
    prompt: str, *, expect_json: bool = True
) -> Optional[Union[dict, str]]:
    """
    Send prompt to local Phi-3. Returns parsed dict if expect_json=True,
    raw string if False, None on any failure or timeout.
    """
    payload: dict = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,   # low temp = deterministic, factual
            "num_predict": 120,   # cap output tokens — we only need short JSON
            "top_p": 0.9,
        },
    }
    if expect_json:
        payload["format"] = "json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                raw: str = data.get("response", "")
                if not expect_json:
                    return raw.strip()
                # Normalise fenced code blocks that some models emit
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1].strip()
                    if raw.startswith("json"):
                        raw = raw[4:].strip()
                return json.loads(raw)
    except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError, Exception):
        return None


async def health_check() -> bool:
    """Returns True if ollama is reachable and Phi-3 responds."""
    result = await llm_query('{"status": "ok"}', expect_json=True)
    return result is not None
