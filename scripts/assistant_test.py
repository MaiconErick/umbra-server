"""End-to-end text test of the xAssistant flow, exactly as the phone will run it (minus audio):
mint a key on the Pi -> open an OpenAI Realtime session -> send what the user "said" as text ->
forward every function call to the Pi's /assistant/tool -> print what Umbra would say.

    python assistant_test.py "nossa, tá muito quente aqui, liga o ar" ["põe no 23 e desliga daqui a 5 minutos" ...]
"""
import asyncio
import json
import sys
import urllib.request

import websockets

PI = "http://192.168.1.128:8765/api/v2/assistant"


def post(path, body):
    req = urllib.request.Request(PI + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def main(lines):
    s = post("/session", {})
    url = f"wss://api.openai.com/v1/realtime?model={s['model']}"
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + s["key"]}, max_size=None) as ws:
        for line in lines:
            print(f"\n> user: {line}")
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": line}]}}))
            await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
            while True:
                ev = json.loads(await ws.recv())
                t = ev["type"]
                if t == "error":
                    print("ERROR", ev["error"])
                    return
                if t != "response.done":
                    continue
                calls = [o for o in ev["response"].get("output", []) if o.get("type") == "function_call"]
                for o in ev["response"].get("output", []):
                    if o.get("type") == "message":
                        text = "".join(c.get("text", "") or c.get("transcript", "") for c in o.get("content", []))
                        print(f"< umbra: {text}")
                if not calls:
                    break
                for c in calls:
                    out = post("/tool", {"name": c["name"], "arguments": c.get("arguments"), "session": s["session"]})["output"]
                    print(f"  [tool] {c['name']}({c.get('arguments')}) -> {json.dumps(out, ensure_ascii=False)[:300]}")
                    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": c["call_id"], "output": json.dumps(out, ensure_ascii=False)}}))
                await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1:] or ["nossa, tá muito quente aqui, liga o ar"]))
