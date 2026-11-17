"""给导入卡补详细解释。

用法：
    .venv/bin/python scripts/enrich_cards.py --dry-run    只看配置，不发请求
    .venv/bin/python scripts/enrich_cards.py              只补没算过的卡
    .venv/bin/python scripts/enrich_cards.py --limit 5    先试跑 5 张
    .venv/bin/python scripts/enrich_cards.py --force      全部重算
    .venv/bin/python scripts/enrich_cards.py --no-backup  只跑主模型

设计要点：
1. 密钥只从环境变量读，主模型走 OPENROUTER_API_KEY，NIM 走 NVIDIA_API_KEY，
   脚本与仓库里不出现明文。
2. 按问题与答案的哈希记进度到 data/detail_cache.json，中断可续。
3. 模型返回空正文一律算失败，绝不把空值写进卡里。
4. 按 OPENROUTER 主模型、两个免费备用、NVIDIA NIM 的顺序逐个尝试。
5. 任何参数解析失败必须停在原地，绝不带着未知参数开跑。脚本会发请求，
   --help 曾经被静默忽略导致直接开跑，这个坑踩过一次，现在用 argparse。
"""

import hashlib
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(BASE, "data", "cards.json")
CACHE = os.path.join(BASE, "data", "detail_cache.json")

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1")

# 2026-11-16 用户指定主模型与备用顺序：
# 主   nvidia/nemotron-3.5-super-vl:free
# 备一 nvidia/nemotron-3.5-lightning:free
# 备二 dots-studio/dots-3-note-preview:free
# 最后 nvidia/nemotron-3-super-120b-a12b，走 NVIDIA NIM
#
# 全部是免费层，密钥只在 .env。OpenRouter 的模型名必须带 :free，
# 少了后缀会走付费计费，代码里不做自动补全，避免误扣费。
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free")
OPENROUTER_BACKUPS = [
    m.strip() for m in os.environ.get(
        "OPENROUTER_BACKUP_MODELS",
        "nvidia/nemotron-3.5-lightning:free,"
        "dots-studio/dots-3-note-preview:free").split(",")
    if m.strip()
]

PRIMARY = {
    "name": f"openrouter/{OPENROUTER_MODEL}",
    "base": OPENROUTER_BASE,
    "key_env": "OPENROUTER_API_KEY",
    "model": OPENROUTER_MODEL,
    "max_tokens": 2000,
    "extra": None,
}

BACKUP = {
    "name": f"openrouter/{OPENROUTER_BACKUPS[0]}",
    "base": OPENROUTER_BASE,
    "key_env": "OPENROUTER_API_KEY",
    "model": OPENROUTER_BACKUPS[0],
    "max_tokens": 2000,
    "extra": None,
}

OR_BACKUPS = [BACKUP] + [
    {
        "name": f"openrouter/{m}",
        "base": OPENROUTER_BASE,
        "key_env": "OPENROUTER_API_KEY",
        "model": m,
        "max_tokens": 2000,
        "extra": None,
    }
    for m in OPENROUTER_BACKUPS[1:]
]

# NIM 的三个坑，都已实测：
# 1. 模型名是单层前缀，nvidia/... 不要再套一层。
# 2. 推理走 reasoning_content，max_tokens 给小了正文会被思考吃光。
# 3. 关掉思考，参数是 chat_template_kwargs。
NIM = {
    "name": "nim/nvidia/nemotron-3.5-super-vl-120b-a12b",
    "base": os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
    "key_env": "NVIDIA_API_KEY",
    "model": os.environ.get("NVIDIA_MODEL",
                             "nvidia/nemotron-3.5-super-vl-120b-a12b"),
    "max_tokens": 2000,
    "extra": {"chat_template_kwargs": {"enable_thinking": False,
                                       "force_nonempty_content": True}},
}

PROVIDERS = [PRIMARY] + OR_BACKUPS + [NIM]

PROMPT = """下面是一张知识卡，请补充一句补充说明。

问题：{q}
答案：{a}

要求：
1. 用中文写，1 到 3 句话，总共 40 到 90 字
2. 只说最关键的那一点判断依据，不要展开讲道理
3. 不要举例，不要场景，不要复述上面的答案
4. 宁短勿长，写到意思到位就停
5. 不用 markdown 标题、列表符号或代码块
6. 直接输出正文，不要任何开场白"""

RETRY_WAIT = 6
# ling 偶发返回空正文，实测 5 张里有 2 张，所以留一次重试就够。
# 这也是整轮请求数的上界：最多「待补卡数 × RETRY」次，不会失控。
RETRY = 2
# 用户明确要求 1 到 3 句话、不超过 100 字，所以上限卡死在 100。
# 提示词只要 40 到 90 字，留出余量让模型自己收住，超限的一律判失败重试。
MIN_LEN = 25
MAX_LEN = 100


def make_client(provider):
    """按 provider 建客户端。密钥缺失时返回 None，由调用方决定是否跳过。"""
    key = os.environ.get(provider["key_env"])
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(base_url=provider["base"], api_key=key,
                  timeout=120.0, max_retries=0)


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def card_key(c):
    """问题与答案的哈希。改了内容就会重新算，不需要手动清理缓存"""
    return hashlib.sha256((c["q"] + "\u0001" + c["a"]).encode()).hexdigest()[:16]


def is_placeholder(c):
    """detail 是来源占位才算待补。已有真解释的一律跳过"""
    d = (c.get("detail") or "").strip()
    return d.startswith("来源") or not d


def clean(text):
    """清理模型输出。

    实测 nemotron 会把英文思考链和自检写进 content，例如
    Here's a thinking process: ... Count: ~210 words. Good. Within 200-300.
    这些必须整段丢掉，否则会存进去变成 4000 字垃圾。
    判断依据是内容特征而不是模型名，换个模型也拦得住。

    NVIDIA NIM 文档另说明 Nemotron 可能用 <think></think> 内联标签输出推理，
    这类必须整段删除而不是走逐行过滤，未闭合的残段也要丢掉。
    """
    t = text.strip()
    for ch in ("**", "##", "###"):
        t = t.replace(ch, "")

    # 成对的 <think></think> 与未闭合的残段
    while True:
        a = t.lower().find("<think>")
        if a < 0:
            break
        b = t.lower().find("</think>", a)
        t = t[:a] + (t[b + len("</think>"):] if b >= 0 else "")
    a = t.lower().find("<think>")
    if a >= 0:
        t = t[:a]
    for tag in ("</think>", "</think>", "<reasoning>", "</reasoning>"):
        t = t.replace(tag, "")

    # 思考链与自检的开头特征。命中就只取最后一个中文段落块
    markers = (
        "Here's a thinking process",
        "Here's a thinking",
        "A thinking process",
        "Let me analyze",
        "Let me think",
    )
    idx = None
    for m in markers:
        p = t.find(m)
        if p >= 0:
            idx = p if idx is None else min(idx, p)
    if idx is not None:
        # 思考链之后是真正的正文，取第一个出现中文成句的位置
        rest = t[idx:]
        for m in ("### Answer", "Answer:", "答案：", "**Answer"):
            q = rest.find(m)
            if q >= 0:
                rest = rest[q + len(m):]
                break
        t = rest

    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    # 去掉纯英文的思考行与自检行
    lines = [ln for ln in lines if not is_english_line(ln)]
    out = "\n".join(lines).strip()

    # 末尾的自检句，例如 Count: ~210 words. Good. Within 200-300.
    for tail in ("Count:", "Word count:", "Let me know"):
        p = out.rfind(tail)
        if p > 0:
            out = out[:p].strip()
    return out


def is_english_line(line):
    """整行几乎没有中文就判为英文思考行"""
    cn = sum(1 for ch in line if "\u4e00" <= ch <= "\u9fff")
    return cn < 4 and len(line) > 12


def ask(client, provider, q, a):
    """发一次请求。provider 的 extra 非空时透传给 API。

    nemotron 的推理在 reasoning_content 字段，正文空的时候要能看到那个字段，
    否则排查时只能看到「正文 0 字」，判断不出是预算不够还是模型不收敛。
    """
    kwargs = {}
    if provider.get("extra"):
        kwargs["extra_body"] = provider["extra"]
    r = client.chat.completions.create(
        model=provider["model"],
        messages=[{"role": "user", "content": PROMPT.format(q=q, a=a)}],
        max_tokens=provider["max_tokens"],
        **kwargs,
    )
    m = r.choices[0].message
    text = m.content or ""
    if not text:
        reason = getattr(m, "reasoning_content", None) or ""
        if reason:
            print(f"    正文为空，推理字段有 {len(reason)} 字，"
                  f"finish_reason={r.choices[0].finish_reason}")
    return text


def try_one(client, provider, c, label, tries):
    """在一个 provider 上跑完重试。合格即返回正文，不合格返回空串。

    报错只打类型不重试换模型，拿到原文交给人判断，避免拿钱做探测。
    """
    for attempt in range(tries):
        try:
            text = clean(ask(client, provider, c["q"], c["a"]))
        except Exception as e:
            print(f"  {label} {c['id']} 第{attempt + 1}次请求失败：{type(e).__name__}"
                  f" {str(e)[:90]}")
            time.sleep(RETRY_WAIT)
            continue
        if MIN_LEN <= len(text) <= MAX_LEN:
            return text
        print(f"  {label} {c['id']} 第{attempt + 1}次正文 {len(text)} 字不合格")
        time.sleep(RETRY_WAIT)
    return ""


def enrich(limit=None, force=False, use_backup=True):
    data = read_json(CARDS, None)
    if data is None:
        sys.exit(f"读不到 {CARDS}")
    cards = data["cards"]
    cache = read_json(CACHE, {})

    # --force 只重算导入卡，也就是带 source 的那批。
    # 不限定的话 142 张手写卡也会被模型覆盖掉，那些是照笔记整理的，比模型写的准。
    # 已经符合长度标准的跳过，这样重跑只补还没改的那部分，不重复花钱
    todo = [c for c in cards
            if (c.get("source") and force and len(c.get("detail") or "") > MAX_LEN)
            or (is_placeholder(c) and cache.get(card_key(c)) is None)]
    total = len(todo)
    if limit is not None:
        todo = todo[:limit]

    # 主模型失败才逐个启备用，请求数上界按 provider 数量相乘
    providers = [PRIMARY] if not use_backup else PROVIDERS
    upper = len(todo) * RETRY * len(providers)
    print(f"卡片总数 {len(cards)}，待补 {total}，本次跑 {len(todo)}")
    print(f"模型顺序 {' → '.join(p['name'] for p in providers)}")
    print(f"最多 {upper} 次请求")
    if not todo:
        print("没有待补的卡，跑完了")
        return

    clients = []
    for p in providers:
        cl = make_client(p)
        if cl is None:
            print(f"缺少环境变量 {p['key_env']}，跳过 {p['name']}")
            continue
        clients.append((p, cl))
    if not clients:
        sys.exit("所有 provider 的密钥都没配置，无法运行")

    ok = fail = backup_hit = 0

    for i, c in enumerate(todo, 1):
        text = ""
        used = ""
        for n, (p, cl) in enumerate(clients):
            label = f"[{i}/{len(todo)}] {p['name']}"
            text = try_one(cl, p, c, label, RETRY)
            if text:
                used = p["name"]
                if n > 0:
                    backup_hit += 1
                break
        if not text:
            fail += 1
            print(f"  [{i}/{len(todo)}] {c['id']} 所有模型都不合格，记为失败")
            continue

        c["detail"] = text
        ok += 1
        tag = "（备用）" if used != PRIMARY["name"] else ""
        print(f"  [{i}/{len(todo)}] {c['id']} {len(text)} 字{tag}　{c['q'][:24]}")
        write_json(CARDS, data)
        cache[card_key(c)] = len(text)
        write_json(CACHE, cache)

    print(f"\n成功 {ok}，失败 {fail}，其中备用模型接手 {backup_hit} 张")
    if fail:
        print("失败的卡重跑本脚本即可，只补没成功的")


if __name__ == "__main__":
    # 必须先解析参数再动手。以前没有 argparse，--help 会被静默忽略，
    # 脚本直接开始跑付费请求，这个坑踩过一次。
    import argparse

    ap = argparse.ArgumentParser(description="给导入卡补详细解释")
    ap.add_argument("--limit", type=int, help="本次最多处理几张，用于先试跑")
    ap.add_argument("--force", action="store_true", help="已算过的卡也重算")
    ap.add_argument("--no-backup", action="store_true",
                    help="只跑主模型。用于单独排查主模型失败原因，"
                         "不用备用模型掩盖问题")
    ap.add_argument("--primary-model", help="覆盖主模型名")
    ap.add_argument("--primary-max-tokens", type=int, help="覆盖主模型 max_tokens")
    ap.add_argument("--backup-model", help="覆盖备用模型名")
    ap.add_argument("--backup-max-tokens", type=int, help="覆盖备用模型 max_tokens")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将用的模型与请求数上界，不发任何请求")
    args = ap.parse_args()

    if args.primary_model:
        PRIMARY["model"] = args.primary_model
    if args.primary_max_tokens:
        PRIMARY["max_tokens"] = args.primary_max_tokens
    if args.backup_model:
        BACKUP["model"] = args.backup_model
    if args.backup_max_tokens:
        BACKUP["max_tokens"] = args.backup_max_tokens

    if args.dry_run:
        todo = [c for c in read_json(CARDS, {"cards": []})["cards"]
                if (c.get("source") and args.force
                    and len(c.get("detail") or "") > MAX_LEN)
                or is_placeholder(c)]
        if args.limit is not None:
            todo = todo[:args.limit]
        providers = [PRIMARY] if args.no_backup else PROVIDERS
        print(f"待补 {len(todo)} 张　干跑，不发请求")
        for p in providers:
            print(f"{p['name']}　模型={p['model']}　max_tokens={p['max_tokens']}　"
                  f"密钥已配置={bool(os.environ.get(p['key_env']))}")
        print(f"请求数上界 {len(todo) * RETRY * len(providers)} 次")
        sys.exit(0)

    enrich(limit=args.limit, force=args.force,
           use_backup=not args.no_backup)