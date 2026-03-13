import asyncio, aiohttp, json

async def test():
    payload = {
        "model": "phi3:3.8b",
        "prompt": "[INST] You are a trading analyst. Answer ONLY with JSON. Is buying BTC at 70000 coherent given RSI=55 and MACD=0.5? Respond exactly: {\"coherent\": true, \"reason\": \"short reason\", \"confidence\": 0.85} [/INST]",
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 120, "top_p": 0.9}
    }
    async with aiohttp.ClientSession() as s:
        async with s.post("http://localhost:11434/api/generate", json=payload, timeout=aiohttp.ClientTimeout(total=15.0)) as r:
            data = await r.json()
            raw = data.get("response", "")
            print("RAW:", repr(raw))
            print("PARSED:", json.loads(raw))

asyncio.run(test())
