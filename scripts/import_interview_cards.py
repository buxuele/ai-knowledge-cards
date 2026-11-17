"""把 面试题 目录下的三份问答文档转成卡片，追加进 data/cards.json。

用法：
    .venv/bin/python scripts/import_interview_cards.py --dry-run   只解析打印，不写文件
    .venv/bin/python scripts/import_interview_cards.py             解析后追加落盘

数据模型（与库内既有卡一致）：
    q      问题原文
    a      答案的首句，卡片背面的主答案，保持短
    detail 答案剩余部分，尾部接考察点等元信息。渲染层 white-space 为 pre-wrap，
           代码块的新换行靠这里保住
    cat    统一 ai，让卡片进入 /ai 路由，子类由 server.py 的关键词规则推导
    level  core / practice / basic / edge 四档之一
    source 文档名，进入来源下拉筛选
    theme  interview，标记本批

解析约定：
    1. 三份文档字段名不同，目的/考察点/考察/来源/关联简历/优秀答案/参考回答/答案
       统一成字典。英文与中文冒号都认。
    2. id 统一 intv 前缀连续编号，与库内 ai-、net-、infra- 等前缀不冲突。
    3. 只做切分不重写文字，答案内容一字不改。
"""

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(BASE, "data", "cards.json")
SRC_DIR = os.path.join(BASE, "面试题")
THEME = "interview"

# 答案首句至少这么长才作为 a 单独切出，否则并到下一句
MIN_A = 20
# a 的硬上限，超过说明整段没有句号，不切
A_CAP = 160

Q_HEAD = re.compile(r"^###\s+(\d+)\.\s*(.+?)\s*$")
FIELD = re.compile(r"^(\d+\.\s*)?(目的|考察点|考察|来源|关联简历|优秀答案|参考回答|答案)[:：]\s*(.*)$")
FENCE = re.compile(r"^\s*```")

# 三份文档的字段名不同，归一到卡片字段
ANSWER_KEYS = ("优秀答案", "参考回答", "答案")
META_ORDER = ("目的", "考察点", "考察", "来源", "关联简历")
META_LABEL = {"考察": "考察点", "目的": "目的", "考察点": "考察点",
              "来源": "来源", "关联简历": "关联简历"}


def split_answer(text):
    """把整段答案切成 a 与 detail。切点取第一个够长的句末标点。"""
    text = text.strip()
    if not text:
        return "", ""
    pos = None
    for i, ch in enumerate(text):
        if i >= A_CAP:
            break
        if ch in "。！？":
            if i + 1 >= MIN_A:
                pos = i + 1
                break
    if pos is None:
        for i, ch in enumerate(text):
            if i + 1 >= MIN_A and ch in "。！？":
                pos = i + 1
                break
    if pos is None or pos >= len(text):
        return text, ""
    return text[:pos], text[pos:].strip()


def parse(path):
    """读一份文档，返回题目列表。每题是字典，含 q 与 fields。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    items = []
    cur = None
    in_fence = False
    last_key = None
    section = ""

    for raw in lines:
        line = raw.rstrip()
        if FENCE.match(line):
            in_fence = not in_fence
            if cur is not None and last_key is not None:
                cur["fields"][last_key] = (cur["fields"].get(last_key, "") +
                                           "\n" + line).strip("\n")
            continue
        if in_fence:
            if cur is not None and last_key is not None:
                cur["fields"][last_key] = cur["fields"].get(last_key, "") + "\n" + line
            continue

        if line.startswith("## ") or (line.startswith("# ") and "梯队" in line):
            section = line.lstrip("# ").strip()
            # 换节后旧字段不再接收续行，否则结尾的补充说明会被并进最后一题的答案
            last_key = None
            continue

        m = Q_HEAD.match(line)
        if m:
            cur = {"q": m.group(2).strip(), "fields": {}, "section": section}
            items.append(cur)
            last_key = None
            continue

        if cur is None:
            continue

        m = FIELD.match(line)
        if m:
            key = m.group(2)
            cur["fields"][key] = m.group(3).strip()
            last_key = key
        elif line.strip() and last_key is not None:
            cur["fields"][last_key] = cur["fields"].get(last_key, "") + line.strip()

    return items


def level_of(section, fields):
    """按分组或来源优先级定难度档。"""
    src = fields.get("来源", "")
    m = re.search(r"P([012])", src)
    if m:
        return {"0": "core", "1": "practice", "2": "edge"}[m.group(1)]
    if "高难" in section or "核心" in section:
        return "core"
    if "进阶" in section or "软素质" in section or "视觉" in section:
        return "practice"
    if "基础" in section or "开场" in section:
        return "basic"
    return "core"


def build(items, source_name):
    out = []
    for it in items:
        fields = it["fields"]
        ans = next((fields[k] for k in ANSWER_KEYS if fields.get(k)), "")
        a, detail = split_answer(ans)
        if not a:
            a = ans.strip()
        metas = []
        for k in META_ORDER:
            if fields.get(k):
                metas.append(f"{META_LABEL[k]}：{fields[k].strip()}")
        if metas:
            tail = "\n".join(metas)
            detail = (detail + "\n\n" + tail).strip() if detail else tail
        out.append({
            "cat": "ai",
            "q": it["q"],
            "a": a,
            "detail": detail,
            "level": level_of(it["section"], fields),
            "theme": THEME,
            "source": source_name,
        })
    return out


def main(dry):
    docs = [
        ("面试题库.md", "面试题库"),
        ("可能的面试题.md", "可能的面试题"),
        ("常见面试题-top50.md", "常见面试题 top50"),
    ]
    added = []
    for fn, name in docs:
        path = os.path.join(SRC_DIR, fn)
        items = parse(path)
        cards = build(items, name)
        print(f"{fn}　解析 {len(items)} 题")
        added.extend(cards)

    with open(CARDS, encoding="utf-8") as f:
        data = json.load(f)
    existing = data["cards"]
    used = {c["id"] for c in existing}
    used_q = {c["q"].strip() for c in existing}

    dup_q = [c["q"] for c in added if c["q"].strip() in used_q]

    seq = 0
    for c in added:
        while True:
            seq += 1
            cid = f"intv-{seq:03d}"
            if cid not in used:
                used.add(cid)
                break
        c["id"] = cid
        c.update({"seen": 0, "marked": 0, "ease": 2.5, "interval": 0,
                  "reps": 0, "lapses": 0, "due": 0, "due_now": True, "due_in": 0})

    print(f"合计新增 {len(added)} 张，其中与库内问题完全重复 {len(dup_q)} 张")
    for q in dup_q:
        print(f"  重复：{q[:40]}")

    if dry:
        for c in added[:3] + added[-2:]:
            print("-" * 60)
            print("id", c["id"], "level", c["level"], "source", c["source"])
            print("q", c["q"])
            print("a", c["a"])
            print("detail", c["detail"][:400].replace("\n", "⏎"))
        return

    data["cards"] = existing + added
    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CARDS)
    print(f"已写入 {CARDS}，全库 {len(data['cards'])} 张")


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
