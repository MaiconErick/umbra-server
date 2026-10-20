"""Record what Umbra sounds like on different realtime models/voices (text in -> audio out) as WAV files."""
import asyncio
import base64
import json
import sys
import urllib.request
import wave
from pathlib import Path

import websockets

PI = "http://192.168.1.128:8765/api/v2/assistant"
OUT = Path(__file__).parent / "voice_samples"
LINES = [
    "Umbra, acabei de tropeçar no tapete e derramei o café todinho na minha camisa branca",
    "conta uma piada bem ruim aí",
]


def post(path):
    req = urllib.request.Request(PI + path, b"{}", {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def record(model, voice):
    s = post(f"/session?model={model}&voice={voice}")
    pcm, texts = bytearray(), []
    url = f"wss://api.openai.com/v1/realtime?model={s['model']}"
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + s["key"]}, max_size=None) as ws:
        for line in LINES:
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": line}]}}))
            await ws.send(json.dumps({"type": "response.create"}))
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 60))
                t = ev["type"]
                if t == "error":
                    raise RuntimeError(ev["error"])
                if t in ("response.output_audio.delta", "response.audio.delta"):
                    pcm += base64.b64decode(ev["delta"])
                elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                    texts.append(ev.get("transcript", ""))
                elif t == "response.done":
                    pcm += b"\0\0" * 24000 * 1  # 1 s gap between answers
                    break
    OUT.mkdir(exist_ok=True)
    f = OUT / f"umbra_{model}_{voice}.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(bytes(pcm))
    print(f"{f.name}: {len(pcm) / 48000:.1f} s | " + " || ".join(texts), flush=True)
    return s["session"]


async def main(pairs):
    sids = []
    for pair in pairs:
        model, voice = pair.split(":")
        try:
            sids.append(await record(model, voice))
        except Exception as e:  # noqa: BLE001
            print(f"{model}:{voice} FAILED: {e}", flush=True)
    print("SESSIONS", " ".join(sids))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1:]))
