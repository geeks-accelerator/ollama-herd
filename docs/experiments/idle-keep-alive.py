"""First request after idle, straight to Ollama vs through Ollama Herd.

For each arm: unload the model, send a request (cold), send another (warm),
wait IDLE_S, check /api/ps, send a third. Records TTFT and whether the model
was resident before the third request.
"""

import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "llama3.2:1b"
IDLE_S = int(sys.argv[3]) if len(sys.argv) > 3 else 360


def post(url, body):
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    t0 = time.perf_counter()
    ttft = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if ttft is None and line.strip():
                ttft = (time.perf_counter() - t0) * 1000
    return ttft, (time.perf_counter() - t0) * 1000


def loaded():
    with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=10) as r:
        ps = json.load(r)
    return [(m["name"], m.get("expires_at")) for m in ps["models"]]


def unload():
    post(f"{OLLAMA}/api/generate", {"model": MODEL, "keep_alive": 0, "prompt": ""})
    time.sleep(3)


def chat(base):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with the word ready."}],
        "stream": True,
        "options": {"num_predict": 8, "temperature": 0, "seed": 1},
    }
    return post(f"{base}/api/chat", body)


def arm(name, base):
    unload()
    out = {"arm": name, "url": base, "model": MODEL, "idle_s": IDLE_S}
    out["resident_before_cold"] = MODEL in [n for n, _ in loaded()]
    out["cold_ttft_ms"], _ = chat(base)
    out["warm_ttft_ms"], _ = chat(base)
    out["after_request"] = loaded()
    time.sleep(IDLE_S)
    out["after_idle"] = loaded()
    out["resident_after_idle"] = any(n == MODEL for n, _ in out["after_idle"])
    out["after_idle_ttft_ms"], _ = chat(base)
    print(json.dumps(out), flush=True)
    return out


# Usage (results: docs/experiments/idle_keep_alive_results.json):
#   python docs/experiments/idle-keep-alive.py direct qwen3-coder:30b 360
#   python docs/experiments/idle-keep-alive.py http://127.0.0.1:11435 qwen3-coder:30b 360
# Run the two arms one after the other, never at once: both use the same Ollama.
# Through Herd the model stays loaded afterwards (keep_alive -1); unload it with
#   curl localhost:11434/api/generate -d '{"model": "qwen3-coder:30b", "keep_alive": 0}'

if __name__ == "__main__":
    which = sys.argv[1]  # "direct" or a router URL
    base = OLLAMA if which == "direct" else which
    arm("direct" if which == "direct" else "herd", base)
