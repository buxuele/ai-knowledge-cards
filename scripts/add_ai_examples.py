"""给 AI 技术里缺少例子的拆书/面试卡补一个具体例子。

只补 detail 为空的卡，不改问题与答案。模型走 NIM，关思考，免费。
用法：
    .venv/bin/python scripts/add_ai_examples.py
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
CARDS = os.path.join(BASE, "data", "cards.json")
BATCH = 8
LIMIT = 0
WORKERS = 3
RETRY = 2

PROMPT = """你是知识卡编辑。为下面每张卡写一个具体例子，严格只输出 JSON 数组。

每张卡输出 {"id": "原id", "example": "例如……"}。
要求：
1. 每条 20 到 60 字，必须以“例如”开头。
2. 用日常或工程场景，写清谁做什么、结果如何。
3. 不重复答案，不引入新的数字、公司、人名，不写目的、考察点或来源。
4. 原 id 一个字不改，数组顺序与输入一致。

卡片：
{cards}
"""


def load():
    with open(CARDS, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CARDS)


def make_client():
    import server
    mod, providers = server.llm_modules()
    nim = next((p for p in providers if p["name"].startswith("nim/")), None)
    if not nim:
        sys.exit("没有可用的 NIM provider")
    return mod, nim, mod.make_client(nim)


def ask(client, nim, rows):
    payload = [{"id": c["id"], "q": c["q"], "a": c["a"]} for c in rows]
    import server
    r = client.chat.completions.create(
        model=nim["model"],
        messages=[{"role": "user",
                   "content": PROMPT.replace("{cards}", json.dumps(payload, ensure_ascii=False))}],
        max_tokens=2000,
        extra_body=nim.get("extra") or {},
    )
    text = r.choices[0].message.content or ""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        arr = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in arr:
        cid, ex = item.get("id"), (item.get("example") or "").strip()
        if cid and ex.startswith("例如") and 8 <= len(ex) <= 100:
            out[cid] = ex.replace("\n", "")
    return out


def main():
    data = load()
    cards = [c for c in data["cards"]
             if c["cat"] == "ai" and "例如" not in (c.get("detail") or "")]
    if LIMIT:
        cards = cards[:LIMIT]
    print(f"待补例子 {len(cards)} 张")
    if not cards:
        return
    mod, nim, client = make_client()
    batches = [cards[i:i + BATCH] for i in range(0, len(cards), BATCH)]
    ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(ask, client, nim, b): b for b in batches}
        for fut in as_completed(futs):
            rows = futs[fut]
            got = {}
            for _ in range(RETRY + 1):
                try:
                    got = fut.result()
                except Exception as e:
                    print("批次失败", type(e).__name__, str(e)[:80])
                    got = {}
                if len(got) == len(rows):
                    break
            for c in rows:
                ex_text = got.get(c["id"])
                if ex_text:
                    old = (c.get("detail") or "").strip()
                    c["detail"] = (old + "\n" + ex_text) if old else ex_text
                    ok += 1
            print(f"完成 {ok}/{len(cards)}")
    save(data)
    print(f"补上 {ok} 张，剩余 {len(cards) - ok} 张")


if __name__ == "__main__":
    main()
