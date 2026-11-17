"""把 面试题/进阶题库-01 到 -10 的 200 道题导入成知识卡。

用法：
    .venv/bin/python scripts/import_advanced_cards.py

幂等：已存在的 adv- 编号跳过。题目按文档顺序编号，答案取「答：」后的文字。
"""

import glob
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(BASE, "data", "cards.json")
QDIR = os.path.join(BASE, "面试题")

FILES = sorted(glob.glob(os.path.join(QDIR, "进阶题库-*.md")))
HEAD = re.compile(r"^##\s+(\d+)\.\s*(.+?)\s*$")
ANS = re.compile(r"^答[:：]\s*(.*)$")


def parse_file(path):
    title = ""
    items = []
    cur = None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        m = HEAD.match(line)
        if m:
            if cur and cur["a"]:
                items.append(cur)
            cur = {"q": m.group(2).strip(), "a": ""}
            continue
        if cur is not None:
            a = ANS.match(line)
            if a:
                cur["a"] = a.group(1).strip()
    if cur and cur["a"]:
        items.append(cur)
    return title, items


def main():
    with open(CARDS, encoding="utf-8") as f:
        data = json.load(f)
    have = {c["id"] for c in data["cards"]}
    haveq = {c["q"] for c in data["cards"]}
    added = []
    n = 0
    for path in FILES:
        title, items = parse_file(path)
        if len(items) != 20:
            raise SystemExit(f"{os.path.basename(path)} 解析到 {len(items)} 题，预期 20")
        for i, item in enumerate(items, 1):
            n += 1
            cid = f"adv-{n:03d}"
            if cid in have or item["q"] in haveq:
                continue
            data["cards"].append({
                "id": cid,
                "cat": "ai",
                "q": item["q"],
                "a": item["a"],
                "detail": "",
                "level": "core",
                "source": title or os.path.basename(path)[:-3],
                "theme": "interview-advanced",
            })
            have.add(cid)
            haveq.add(item["q"])
            added.append(cid)

    if "ai" not in {c["id"] for c in data["categories"]}:
        data["categories"].insert(0, {
            "id": "ai", "name": "AI 技术",
            "summary": "Agent 架构、记忆系统、MCP 与 LSP、AI 编程方法论",
        })

    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CARDS)
    print(f"解析 {n} 题，新增 {len(added)} 张，当前共 {len(data['cards'])} 张")


if __name__ == "__main__":
    main()
