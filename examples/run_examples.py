"""Run the two assignment test inputs against a running server and save the responses.

    uvicorn app.main:app --port 8000      # in one terminal
    python examples/run_examples.py       # in another (add 'standard' or 'complex' to run one)
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.main import EXAMPLES  # noqa: E402

BASE = "http://localhost:8000"
OUT = Path(__file__).resolve().parent

names = sys.argv[1:] or list(EXAMPLES)
for name in names:
    print(f"\n=== {name}: {EXAMPLES[name]['summary']} ===")
    r = httpx.post(f"{BASE}/agent", json=EXAMPLES[name]["value"], timeout=600)
    r.raise_for_status()
    data = r.json()
    (OUT / f"{name}_response.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    doc = httpx.get(data["document"]["download_url"], timeout=60)
    (OUT / f"{name}_{data['document']['filename']}").write_bytes(doc.content)

    print(data["message"])
    print("\nTODO list:")
    for t in data["todo"]:
        print(f"  [{t['status']:>8}] {t['id']:<3} {t['action']:<17} {t['title']}")
    print("\nReflection:", " → ".join(f"{r['score']}/10" for r in data["reflection"]))
    print("Saved:", OUT / f"{name}_{data['document']['filename']}")
