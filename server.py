import atexit
import datetime
import json
import os
import re
import signal
import sqlite3
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("FLASH_PORT", 8899))
HOST = "127.0.0.1"
CARDS_FILE = "data/cards.json"
ARCHIVE_DIR = "data/archived"
# 撤销清空发的是一整份快照行数组，64 KB 只装得下约 580 行。
# 库里当前 373 张卡全答过已接近上限，卡库再涨撤销就会先被体积挡掉。
# 4 MB 与 MAX_ROWS 20000 行（约 2.4 MB）匹配，两个上限不再互相打架
MAX_BODY = 4 * 1024 * 1024
EASE_MIN = 1.3
EASE_MAX = 2.8
MAX_INTERVAL = 180
MAX_PAGE_SIZE = 200
# 撤销清空的快照行上限。正常量级是已复习过的卡数，几千行足够，
# 但必须是有限值，否则一个巨大的 body 能直接读进内存
MAX_ROWS = 20000
ROW_KEYS = ("card_id", "seen", "marked", "ease", "interval", "reps", "lapses", "due")

# 删除原因由服务端按删除那一刻的排程状态自动推断，用户不需要选。
# seen 为 0 说明一眼就删，多半内容不相关；lapses 多说明内容难或写得差；
# reps 累积说明确实已掌握；都没复习过又没失败倾向，判为浅显。
REASONS = {
    "known": "已掌握，内容太简单",
    "trivial": "内容浅显，没复习价值",
    "wrong": "卡片本身有问题，答案不准",
    "duplicate": "与其他卡片重复",
    "unrelated": "没看就删，内容可能不相关",
    "other": "其他原因",
}


_llm_lock = threading.Lock()
_llm_ready = None

# 解释用非阻塞闸门。旧版用 _llm_lock 把整条链路锁 75 秒，第二次点击排在锁外，
# 而它的预算时钟在等锁之前就开始走，拿到锁时预算已尽，只能回一句
# 「所有模型都不可用或超时」。模型明明是好的，这句提示会把人带错方向。
# 闸门满就直接说明在解释另一张卡，不排队也就不饿死。
_explain_gate = threading.Semaphore(1)
# 同一张卡重复点解释时直接复用上次结果，默认只留最近 200 条
_explain_cache = {}


def load_env():
    """从 .env 补齐密钥。

    just_run.sh 不加载 .env，服务端拿不到密钥。密钥只留在 .env 里不外泄，
    这里读进进程环境供 openai SDK 用。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def llm_modules():
    """复用 scripts/enrich_cards.py 里的 provider 配置与清理逻辑。

    不在这里复制一份，配置改一处两边同步。脚本不是包，用文件路径动态加载。

    锁只保护初始化。旧版在锁外判一次空、锁内再判一次的双检写法没必要，
    这里干脆只锁初始化那一下，请求本身不持锁。
    """
    global _llm_ready
    if _llm_ready is None:
        with _llm_lock:
            if _llm_ready is None:
                load_env()
                try:
                    import importlib.util
                    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "scripts", "enrich_cards.py")
                    spec = importlib.util.spec_from_file_location("enrich_cards", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    _llm_ready = (mod, mod.PROVIDERS)
                except Exception:
                    _llm_ready = (None, [])
    return _llm_ready


# 解释的总时间预算。最坏情况不加限制是 2 个 provider × 2 次重试 × 120 秒
# 超时，等于 480 秒，用户看到的就是按钮一直转。超预算立刻降级。
EXPLAIN_BUDGET = 75
# 单个 provider 最多等 30 秒。免费模型常见 10 到 25 秒，45 秒会让一个挂住的
# 主模型吃掉大半预算，用户等到 80 秒才看到备用模型的结果。
EXPLAIN_TIMEOUT = 30
# 模型实际输出常见 70 到 100 字。门槛太高会把有效解释判成失败，
# 进而白白走完后面所有备用模型，单次点击就要多等几十秒。
MIN_EXPLAIN = 60

# 深入解释的提示词。原版要求「200 到 400 字、用自然段」，实测出 586 字一大坨：
# 字数上限被无视，「自然段」被理解成「不用 markdown」，连一个换行都没有，
# 例子埋在中段。改成给三段各自派任务，每段写明字数与开头词。
# 再往后加了说人话与必须举例两条，因为原版满篇术语，例子讲成了抽象道理。
EXPLAIN_PROMPT = """下面是一张知识卡，请解释。严格照下面的三段结构写。

问题：{q}
答案：{a}

三段。段与段之间必须换行，绝对不要写成一整段。
第一段：一句话给出核心结论，不超过 40 字。
第二段：用「例如」开头，讲一个具体场景，不超过 60 字。
第三段：用「否则」开头，说清不这么做会出什么问题，不超过 40 字。

说人话的要求：
用日常词，讲给完全不懂这一行的人听。句子要短，一句话说一件事。
第二段必须落到具体的人、事或数字，比如谁在做什么、几个、多久，
不许写成「这样做能提升效率」这类没有画面的道理。
术语第一次出现时，后面跟一句大白话解释它。

硬要求：全文不超过 180 字；不用标题、列表符号、代码块；直接输出正文，不要开场白。"""


def _ensure_breaks(text):
    """模型偶尔仍把三段连成一句。按分段标记补换行，只改排版不改内容。

    实测五次里有一次零换行，提示词管不住这种随机性，靠这里兜底。
    换行后前端 white-space: pre-wrap 会原样显示。
    """
    if text.count("\n") >= 2:
        return text
    for mark in ("例如", "否则"):
        i = text.find(mark)
        if i > 0 and text[i - 1] != "\n":
            text = text[:i] + "\n" + text[i:]
    return text


def llm_model_list():
    """给设置面板返回可选模型，不返回任何密钥。"""
    mod, providers = llm_modules()
    if mod is None:
        return []
    return [{"id": p["name"], "name": p["name"], "model": p["model"]}
            for p in providers]


def explain_card(q, a, model=None):
    """按「免费优先、付费兜底」逐个 provider 尝试，全失败就让用户自己拿去问。

    全部失败时返回 ok 为 false 与一段现成的问题文本，前端复制到剪贴板。
    这样模型全挂时用户不至于卡住，至少还能问别的模型。

    闸门是非阻塞的。同时只放一个解释在飞，防止连点和翻卡期间把免费层速率打满，
    但第二个请求立刻得到答复，不会排 75 秒队。
    """
    q = (q or "").strip()
    a = (a or "").strip()
    if not q:
        raise BadBody("缺少问题")

    cache_key = (q, a, model if isinstance(model, str) else "")
    if cache_key in _explain_cache:
        return _explain_cache[cache_key]

    mod, providers = llm_modules()
    if model and isinstance(model, str):
        # 用户选中的模型排第一，失败时仍按原顺序兜底，避免一次选择把解释打死
        providers = [p for p in providers if p["name"] == model] + \
                    [p for p in providers if p["name"] != model]
    elif providers:
        # 自动模式优先走 NIM，实测 1 到 3 秒返回；OpenRouter 免费模型常要 10 秒以上
        providers = [p for p in providers if p["name"].startswith("nim/")] + \
                    [p for p in providers if not p["name"].startswith("nim/")]
    if mod is None or not providers:
        return {"ok": False, "reason": "llm 模块加载失败",
                "fallback": f"{q}\n{a}\n\n请深入解释这个知识点为什么这样设计。"}

    if not _explain_gate.acquire(blocking=False):
        return {"ok": False, "reason": "正在解释另一张卡，等它出来再点",
                "fallback": f"{q}\n{a}\n\n请深入解释这个知识点为什么这样设计。"}

    deadline = time.time() + EXPLAIN_BUDGET
    last_err = ""
    try:
        for p in providers:
            left = deadline - time.time()
            if left <= 5:
                break
            if not os.environ.get(p["key_env"]):
                continue
            client = mod.make_client(p)
            if client is None:
                continue
            # 每个 provider 只试一次。限流时重试只会让情况更糟，
            # 而且会把等待时间拉长到用户以为界面死了
            client.timeout = min(EXPLAIN_TIMEOUT, left)
            try:
                kwargs = {"extra_body": p["extra"]} if p.get("extra") else {}
                r = client.chat.completions.create(
                    model=p["model"],
                    messages=[{"role": "user",
                               "content": EXPLAIN_PROMPT.format(q=q, a=a)}],
                    max_tokens=p["max_tokens"], **kwargs)
                text = mod.clean(r.choices[0].message.content or "")
            except Exception as e:
                print(f"解释失败 {p['name']} {type(e).__name__} {str(e)[:80]}")
                if "401" in str(e) or "Unauthorized" in str(e):
                    last_err = "密钥失效或没有权限"
                elif "429" in str(e):
                    last_err = "模型限流"
                else:
                    last_err = type(e).__name__
                continue
            if len(text) >= MIN_EXPLAIN:
                result = {"ok": True, "text": _ensure_breaks(text),
                          "model": p["name"]}
                _explain_cache[cache_key] = result
                while len(_explain_cache) > 200:
                    _explain_cache.pop(next(iter(_explain_cache)))
                return result
            last_err = "模型返回正文过短"
    finally:
        _explain_gate.release()

    why = f"所有模型都不可用或超时，最后错误：{last_err}" if last_err \
        else "所有模型都不可用或超时"
    return {"ok": False, "reason": why,
            "fallback": f"{q}\n{a}\n\n请深入解释这个知识点为什么这样设计。"}


# AI 的细分类。规则按主体判定：取问题里关键词首次出现的位置，靠前者胜出。
#
# 不用手工给每张卡打标签。三条理由：165 张手工标必然漏；卡库增删后标签会失效；
# 以后新增的卡没人记得补标签。规则由问题文本推导，加卡自动归位。
#
# 关键词里刻意不放「工具」这类泛词。实测加上之后，「AI 工具越强程序员越不能只会描述
# 需求」会被「工具」抢走归到工具类，而它讲的是协作方法论。
AI_SUB_RULES = [
    # 体系总览放最前，讲概念之间的关系。归属按 id 前缀 rel- 判定，
    # 不参与关键词匹配，避免「记忆」「工具」这类词把关系卡抢走。
    ("relations", "体系总览", []),
    ("memory", "记忆与上下文",
     ["记忆", "记得", "上下文", "MemGPT", "Mem0", "HippoRAG",
      "遗忘", "巩固", "笔记", "会话", "信息增益"]),
    ("rag", "检索与 RAG",
     ["RAG", "检索", "向量", "召回", "查询改写", "FTS5", "全文",
      "分词", "BM25", "trigram", "倒排"]),
    ("tool", "工具与 MCP",
     ["MCP", "Tool", "沙箱", "渐进式披露", "Resource",
      "schema", "幂等", "驱动", "函数调用", "openclaw", "Skill"]),
    ("security", "安全与权限",
     ["注入", "权限", "审计", "租户", "审批", "护栏",
      "敏感", "脱敏", "预算", "限制", "不可信", "外网"]),
    ("eval", "评估与可靠性",
     ["评估", "评测", "测试", "回归", "观测", "轨迹", "指标",
      "漂移", "重试", "熔断", "背压", "降级", "灰度", "并发",
      "检查点", "重放", "队列", "版本", "离线和在线"]),
    ("craft", "工程与协作",
     ["AI 编程", "程序员", "验收", "技术方案", "怎么训练"]),
    ("agent", "智能体基础",
     ["规划器", "路由器", "工作流", "多智能体", "子智能体",
      "停止", "提问", "Harness", "in the loop"]),
]

TOP_CATS = {sid: name for sid, name, _ in AI_SUB_RULES}
AI_SUB_ORDER = [sid for sid, _, _ in AI_SUB_RULES]


def sub_of(card):
    """AI 卡归到哪个子类。按问题里关键词首次出现的位置判定。"""
    if card.get("cat") != "ai":
        return ""
    if (card.get("id") or "").startswith("rel-"):
        return "relations"
    q = card.get("q") or ""
    best, best_pos = AI_SUB_ORDER[-1], float("inf")
    for sid, _, kws in AI_SUB_RULES:
        for kw in kws:
            p = q.find(kw)
            if 0 <= p < best_pos:
                best, best_pos = sid, p
    return best


CAT_NAMES = {"ai": "AI 技术", "net": "网络", "db": "数据库", "misc": "杂项",
             "java": "Java 基础", "spring": "Spring 框架", "vue": "Vue 前端",
             "redis": "Redis 与缓存", "micro": "微服务", "dist": "分布式与部署",
             "books": "Agent设计模式"}


def sidebar_tree(cards, categories=None):
    """侧边栏结构与张数。AI 下挂子类，其余分类是平级。"""
    sub_counts = {}
    top_counts = {}
    for c in cards:
        top_counts[c["cat"]] = top_counts.get(c["cat"], 0) + 1
        s = sub_of(c)
        if s:
            sub_counts[s] = sub_counts.get(s, 0) + 1

    groups = [{"id": "ai", "name": CAT_NAMES["ai"],
               "count": top_counts.get("ai", 0),
               "children": [{"id": s, "name": TOP_CATS[s],
                             "count": sub_counts.get(s, 0)}
                            for s in AI_SUB_ORDER if sub_counts.get(s, 0)]}]
    # 有分类表时按分类表顺序，避免卡库插入顺序把新分类排到最后。
    # 没有分类表时退回卡库首次出现顺序，兼容直接调用 sidebar_tree 的测试。
    order = [c["id"] for c in categories] if categories else \
        list(dict.fromkeys(c["cat"] for c in cards))
    for cat in order:
        if cat == "ai" or cat not in CAT_NAMES:
            continue
        if not top_counts.get(cat, 0):
            continue
        groups.append({"id": cat, "name": CAT_NAMES[cat],
                       "count": top_counts.get(cat, 0), "children": []})
    return groups


def is_spa_route(path):
    """判断是否该返回页面而不是 404。

    必须用白名单。对任意无扩展名路径回落会让不存在的地址拿到 200，
    页面报「找不到卡片」而不是 404，排查时会被带偏。

    根路径也算，它由这里统一处理，静态分支不再单独认。
    """
    parts = [x for x in path.split("/") if x]
    if not parts:
        return True
    if len(parts) == 1:
        # all 是全部卡片的路由。首页让给了 AI 技术，全部必须另有一个地址，
        # 否则根路径要同时表示两者
        return parts[0] in CAT_NAMES or parts[0] in ("all", "recycle")
    if len(parts) == 2 and parts[0] == "ai":
        return parts[1] in AI_SUB_ORDER
    return False


def infer_reason(state):
    """按排程状态推断删除原因，让用户不必为归档这件事做选择"""
    if not state.get("seen"):
        return "unrelated"
    if state.get("reps", 0) >= 3 and state.get("lapses", 0) == 0:
        return "known"
    if state.get("lapses", 0) >= 3:
        return "wrong"
    return "trivial"

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web", "dist")
DB_PATH = os.path.join(BASE, "data", "progress.db")
ARCHIVE = os.path.join(BASE, ARCHIVE_DIR)
# RLock 可重入。archive_card 内部加锁，而调用方也可能持锁，Lock 会死锁
_lock = threading.RLock()
DAY = 86400.0

_web_types = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    # manifest 的 MIME 错了浏览器会直接拒绝解析
    ".webmanifest": "application/manifest+json",
}

_cards_cache = {"mtime": None, "data": None}


def load_cards():
    """按文件修改时间缓存，卡片数据量小但每个请求都会读，接口测试会放大这个开销"""
    path = os.path.join(BASE, CARDS_FILE)
    stamp = os.stat(path).st_mtime_ns
    if _cards_cache["mtime"] != stamp:
        with open(path, encoding="utf-8") as f:
            _cards_cache["data"] = json.load(f)
        _cards_cache["mtime"] = stamp
    return _cards_cache["data"]


def atomic_write_json(path, obj):
    """先写临时文件再替换。直接覆盖原文件，写一半进程崩了就是题库损坏。

    os.replace 在同一文件系统内是原子操作，读者要么看到旧文件要么看到新文件，
    不会看到写了一半的内容。临时文件与目标同目录，保证同一文件系统。
    """
    path = os.path.abspath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix="." + os.path.basename(path) + "-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_cards(data):
    """题库落盘并让缓存失效。所有写 cards.json 的路径都必须走这里。"""
    atomic_write_json(os.path.join(BASE, CARDS_FILE), data)
    _cards_cache["mtime"] = None


def card_ids():
    return {c["id"] for c in load_cards()["cards"]}


def archive_card(card_id, reason=None):
    """把删掉的卡片连同删除时的排程状态写入 data/archived/。

    存 SM-2 状态是为了让后续分析能回答「为什么删」：
    reps 高说明已掌握，lapses 多说明内容难或写得差，
    seen 为 0 说明用户看一眼就删了，多半是内容不相关。
    """
    with _lock:
        return _archive_locked(card_id, reason)


def _archive_record(card, state, reason):
    """写一条归档文件。卡与删除时的排程状态一起落盘。"""
    if reason not in REASONS:
        reason = infer_reason(state)
    os.makedirs(ARCHIVE, exist_ok=True)
    record = {
        "id": card["id"],
        "cat": card["cat"],
        "cat_name": CAT_NAMES.get(card["cat"], card["cat"]),
        "level": card.get("level", ""),
        "q": card["q"],
        "a": card["a"],
        "detail": card.get("detail", ""),
        "reason": reason,
        "reason_text": REASONS[reason],
        "archived_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "state_at_archive": state,
        # 存整张原卡。只存 q 与 a 的话，恢复后 source、theme、author
        # 这些溯源字段会全丢，导入卡的来源行就没了
        "card": card,
    }
    atomic_write_json(os.path.join(ARCHIVE, f"{card['id']}.json"), record)
    return record


def _archive_locked(card_id, reason=None):
    card = next((c for c in load_cards()["cards"] if c["id"] == card_id), None)
    if not card:
        raise KeyError(card_id)

    con = db()
    row = con.execute(
        "SELECT seen, marked, ease, interval, reps, lapses, due FROM state WHERE card_id=?",
        (card_id,),
    ).fetchone()
    con.close()
    state = dict(zip(("seen", "marked", "ease", "interval", "reps", "lapses", "due"), row)) if row \
        else dict(seen=0, marked=0, ease=2.5, interval=0.0, reps=0, lapses=0, due=0.0)

    # 顺序有讲究：先落归档文件，再原子替换题库，最后才删排程行。
    # 反过来的话题库换成功、归档没写成，这张卡就真丢了
    record = _archive_record(card, state, reason)

    # 同步移除主卡片库与排程，否则前端缓存会让已删卡继续出现
    data = load_cards()
    data["cards"] = [c for c in data["cards"] if c["id"] != card_id]
    # 最后一卡被删光的分类直接移除，侧边栏不再留一个 0 张的空标签
    used = {c["cat"] for c in data["cards"]}
    data["categories"] = [c for c in data["categories"] if c["id"] in used]
    save_cards(data)

    con = db()
    con.execute("DELETE FROM state WHERE card_id=?", (card_id,))
    con.commit()
    con.close()
    return record


def unarchive_card(card_id):
    """撤销删除：把归档文件里的卡与删除时的排程状态写回主库。

    旧版归档文件没有 card 字段，按 id、cat、level、q、a、detail 重建一张，
    至少不丢题面，只是溯源字段会缺。
    """
    with _lock:
        path = os.path.join(ARCHIVE, f"{card_id}.json")
        if not os.path.isfile(path):
            raise BadBody("找不到这张卡的归档记录")
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)

        data = load_cards()
        if any(c["id"] == card_id for c in data["cards"]):
            raise BadBody("这张卡已经回到库里了")

        card = rec.get("card")
        if not isinstance(card, dict):
            card = {k: rec.get(k, "") for k in
                    ("id", "cat", "level", "q", "a", "detail")}
        card = dict(card)
        card["id"] = card_id

        # 放回原分类的末尾，而不是整库末尾。否则恢复后这个分类会在
        # 侧边栏挪到最后，和原来的位置对不上
        cat = card.get("cat") or "misc"
        at = max((i for i, c in enumerate(data["cards"]) if c["cat"] == cat),
                 default=len(data["cards"]) - 1) + 1
        data["cards"].insert(at, card)
        # 这个分类如果之前被删空过，恢复时补回来，否则侧边栏找不到它
        if cat not in {c["id"] for c in data["categories"]}:
            data["categories"].append({
                "id": cat,
                "name": CAT_NAMES.get(cat, cat),
                "summary": "",
            })
        save_cards(data)

        state = rec.get("state_at_archive")
        if isinstance(state, dict) and all(k in state for k in ROW_KEYS[1:]):
            con = db()
            con.execute(
                "INSERT OR REPLACE INTO state(card_id, seen, marked, ease, interval, reps, lapses, due) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (card_id, state["seen"], state["marked"], state["ease"],
                 state["interval"], state["reps"], state["lapses"], state["due"]),
            )
            con.commit()
            con.close()

        # 恢复成功就移除归档文件，否则再删一次会读到旧记录
        os.unlink(path)
        return card


def archive_count():
    """回收站张数。与列表用同一套读取逻辑，坏文件不会让数字对不上。"""
    return len(list_archived())


def list_archived():
    """回收站列表。删除的卡统一在 data/archived/，按删除时间倒序返回。"""
    if not os.path.isdir(ARCHIVE):
        return []
    items = []
    for name in os.listdir(ARCHIVE):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(ARCHIVE, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            # 手改坏一个归档文件不能拖垮整个回收站
            continue
        card = rec.get("card") if isinstance(rec.get("card"), dict) else {}
        items.append({
            "id": rec.get("id") or card.get("id") or name[:-5],
            "q": rec.get("q") or card.get("q", ""),
            "cat": rec.get("cat") or card.get("cat", ""),
            "cat_name": rec.get("cat_name") or CAT_NAMES.get(
                rec.get("cat") or card.get("cat", ""),
                rec.get("cat") or card.get("cat", "")),
            "level": rec.get("level") or card.get("level", ""),
            "archived_at": rec.get("archived_at") or "",
            "reason": rec.get("reason") or "",
            "reason_text": rec.get("reason_text") or REASONS.get(rec.get("reason"), ""),
        })
    items.sort(key=lambda x: x["archived_at"], reverse=True)
    return items


def ensure_categories(data):
    """按卡片里实际出现的 cat 补齐分类元数据。

    删空分类时 categories 会被移除，之后用 JSON 或新增接口补回同分类卡，
    分类表里可能还没有它。这里统一兜底，避免前端拿到一堆没有名字的分类。
    """
    cats = list(data.get("categories", []))
    have = {c["id"] for c in cats}
    for card in data.get("cards", []):
        cat = card.get("cat")
        if cat and cat not in have:
            cats.append({"id": cat, "name": CAT_NAMES.get(cat, cat), "summary": ""})
            have.add(cat)
    return cats


def delete_category(cat):
    """删除整个分类，卡片先全部归档到回收站，再移除分类。

    用户明确要求右键直接删、不弹确认。这里不做二次确认，但保证卡片
    仍能从回收站逐张恢复，避免误删后数据直接消失。
    """
    if not isinstance(cat, str) or cat not in CAT_NAMES:
        raise BadBody("分类不存在")

    with _lock:
        data = load_cards()
        cards = [c for c in data["cards"] if c["cat"] == cat]
        con = db()
        rows = {
            r[0]: dict(zip(("seen", "marked", "ease", "interval",
                            "reps", "lapses", "due"), r[1:]))
            for r in con.execute(
                "SELECT card_id, seen, marked, ease, interval, reps, lapses, due FROM state"
            ).fetchall()
        }
        con.close()

        default_state = dict(seen=0, marked=0, ease=2.5, interval=0.0,
                            reps=0, lapses=0, due=0.0)
        for card in cards:
            _archive_record(card, rows.get(card["id"], default_state), None)

        data["cards"] = [c for c in data["cards"] if c["cat"] != cat]
        used = {c["cat"] for c in data["cards"]}
        data["categories"] = [c for c in data["categories"] if c["id"] in used]
        save_cards(data)

        if cards:
            con = db()
            con.executemany("DELETE FROM state WHERE card_id=?",
                            [(c["id"],) for c in cards])
            con.commit()
            con.close()
        return {"cat": cat, "deleted": len(cards),
                "name": CAT_NAMES.get(cat, cat)}


def add_card(cat, q, a, detail="", level="basic"):
    """新增一张卡。id 按分类前缀续号，分类不在表里时自动补注册。"""
    if not isinstance(cat, str) or cat not in CAT_NAMES:
        raise BadBody("分类不存在")
    if not isinstance(q, str) or not q.strip():
        raise BadBody("问题不能为空")
    if not isinstance(a, str) or not a.strip():
        raise BadBody("答案不能为空")
    if len(q) > 300:
        raise BadBody("问题超出 300 字")
    if len(a) > 3000:
        raise BadBody("答案超出 3000 字")
    if not isinstance(detail, str):
        raise BadBody("解释必须是文本")
    if len(detail) > 3000:
        raise BadBody("解释超出 3000 字")
    if level not in ("basic", "core", "practice", "edge"):
        raise BadBody("难度档不存在")

    with _lock:
        data = load_cards()
        cards = data["cards"]
        nums = []
        for c in cards:
            m = re.match(rf"^{re.escape(cat)}-(\d+)$", c["id"])
            if m:
                nums.append(int(m.group(1)))
        cid = f"{cat}-{max(nums, default=0) + 1:03d}"
        while any(c["id"] == cid for c in cards):
            nums.append(int(cid.rsplit("-", 1)[1]) + 1)
            cid = f"{cat}-{max(nums):03d}"
        card = {
            "id": cid,
            "cat": cat,
            "q": q.strip(),
            "a": a.strip(),
            "detail": detail.strip(),
            "level": level,
        }
        cards.append(card)
        if cat not in {c["id"] for c in data["categories"]}:
            data["categories"] = ensure_categories(data)
        save_cards(data)
        return card


def clear_archived():
    """清空回收站。归档文件全删，卡片本身早已从主库移除。"""
    with _lock:
        if not os.path.isdir(ARCHIVE):
            return 0
        count = 0
        for name in os.listdir(ARCHIVE):
            if not name.endswith(".json"):
                continue
            try:
                os.unlink(os.path.join(ARCHIVE, name))
                count += 1
            except OSError:
                pass
        return count


def edit_card(card_id, q, a):
    """改卡片的问题与答案。错别字、标点、说法由用户自己改，不走 LLM。

    只改 q 与 a。detail 是 LLM 补全的摘要、source 是导入来源、排程状态在库里，
    三者都与文本编辑无关，动了会连带丢数据。
    """
    # 三类非字符串会在这里直接把请求打成断连而不是 400：id 传数字时
    # (123 or "") 返回 123，对它调 strip 抛 AttributeError，
    # socketserver 把异常吞掉只断一条连接，客户端拿到的是连接被远端关闭
    for name, v in (("id", card_id), ("q", q), ("a", a)):
        if v is not None and not isinstance(v, str):
            raise BadBody(f"{name} 必须是文本")
    card_id = (card_id or "").strip()
    q = (q or "").strip()
    a = (a or "").strip()
    if not card_id:
        raise BadBody("缺少卡片 id")
    if not q:
        raise BadBody("问题不能为空")
    if not a:
        raise BadBody("答案不能为空")
    if len(q) > 300:
        raise BadBody("问题超出 300 字上限")
    if len(a) > 3000:
        raise BadBody("答案超出 3000 字上限")

    with _lock:
        data = load_cards()
        hit = next((c for c in data["cards"] if c["id"] == card_id), None)
        if hit is None:
            raise BadBody("卡片不存在")
        hit["q"] = q
        hit["a"] = a
        save_cards(data)
    # 问题文本决定 AI 子分类，改完当场返回新的，省一次请求
    return {"ok": True, "card": hit, "sub": sub_of(hit), "summary": summary()}


_db_inited = set()
_db_init_lock = threading.Lock()


def db():
    """打开排程库。

    每个请求一个新连接，打开后立刻设 busy_timeout 与 WAL。WAL 让读不挡写、
    写不挡读，多点几下卡片时不会冒 database is locked。

    CREATE TABLE 只在每个库文件第一次打开时执行。旧版每次 db() 都建表并提交，
    连读路径也在拿写锁，并发一高就互相等。
    """
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    c.execute("PRAGMA busy_timeout=5000")
    if DB_PATH not in _db_inited:
        with _db_init_lock:
            if DB_PATH not in _db_inited:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("""CREATE TABLE IF NOT EXISTS state (
        card_id TEXT PRIMARY KEY,
        seen INTEGER DEFAULT 0,
        marked INTEGER DEFAULT 0,
        ease REAL DEFAULT 2.5,
        interval REAL DEFAULT 0,
        reps INTEGER DEFAULT 0,
        lapses INTEGER DEFAULT 0,
        due REAL DEFAULT 0
    )""")
                c.commit()
                _db_inited.add(DB_PATH)
    return c


def review(s, q):
    """SM-2 间隔重复。q: 0 完全不会, 1 有点印象, 2 完全掌握。

    三档语义必须区分。q=1 是「记得但不牢」，属于成功回忆，推进间隔但 ease 小幅下调。
    早期版本写的是 if q < 2，把 q=1 与 q=0 归为失败，答对三次攒到 8 天的卡一记印象就清零。
    """
    ease, interval, reps, lapses = s["ease"], s["interval"], s["reps"], s["lapses"]
    if q == 0:
        lapses += 1
        reps = 0
        interval = 0
        ease = max(EASE_MIN, ease - 0.2)
    else:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 3
        else:
            interval = round(interval * ease, 4)
        reps += 1
        # 记得但不牢 ease 略降，完全掌握 ease 升。上限 2.8 是必须的，
        # 否则 ease 无界增长会让间隔倍数失控，180 天上限形同虚设
        ease = min(EASE_MAX, ease + 0.1) if q == 2 else max(EASE_MIN, ease - 0.05)
    interval = min(interval, MAX_INTERVAL)
    return {
        "ease": round(ease, 4),
        "interval": interval,
        "reps": reps,
        "lapses": lapses,
        "due": time.time() + interval * DAY,
    }


def merge(cards):
    """把排程状态拼到卡片上。

    必须构造新字典。旧版直接 c["seen"] = ... 改的是 load_cards() 返回的缓存对象，
    而 edit_card 与归档又把这份缓存 dump 回 cards.json，于是一次编辑之后，
    当时全部卡片的 SM-2 快照就冻进了题库文件。讽刺的是读路径反而一直是对的，
    因为 merge 每次都会覆盖这些字段，所以这个 bug 从来没有任何症状，
    只是 cards.json 越来越脏。
    """
    con = db()
    rows = {
        r[0]: r
        for r in con.execute(
            "SELECT card_id, seen, marked, ease, interval, reps, lapses, due FROM state"
        )
    }
    con.close()
    now = time.time()
    out = []
    for c in cards:
        s = rows.get(c["id"])
        if s:
            seen, marked = s[1], s[2]
            ease, interval, reps, lapses, due = s[3], s[4], s[5], s[6], s[7]
        else:
            seen, marked, ease, interval, reps, lapses, due = 0, 0, 2.5, 0, 0, 0, 0
        out.append({
            **c,
            "seen": seen,
            "marked": marked,
            "ease": ease,
            "interval": interval,
            "reps": reps,
            "lapses": lapses,
            "due": due,
            "due_now": seen == 0 or due <= now,
            "due_in": max(0, round((due - now) / DAY, 1)) if seen else 0,
        })
    return out


def summary(cards=None):
    """只统计仍存在于卡片库的 id。state 表可能残留已删卡的孤儿行，直接用行数会算出负数。"""
    cards = load_cards()["cards"] if cards is None else cards
    ids = {c["id"] for c in cards}
    con = db()
    rows = con.execute(
        "SELECT card_id, seen, marked, due, reps, lapses FROM state").fetchall()
    con.close()
    now = time.time()
    seen = marked = due = known = mastered = 0
    for cid, s, m, d, reps, lapses in rows:
        if cid not in ids:
            continue
        known += 1
        if s:
            seen += 1
        if m:
            marked += 1
        # 真掌握口径：连续答对三次且从未答错过。只答过一次不算掌握，
        # 那种数字好看但没用，进度条会给人已经学会的错觉
        if s and reps >= 3 and lapses == 0:
            mastered += 1
        if not s or d <= now:
            due += 1
    return {
        "total": len(ids),
        "seen": seen,
        "mastered": mastered,
        "marked": marked,
        "due": due + len(ids) - known,
    }


class BadBody(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache="no-store", etag=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        # svg 走静态服务后可能被当成活动内容，CSP 把它钉死为图片
        if ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        # manifest 会被浏览器 fetch，缺 CORS 头就解析失败
        if ctype == "application/manifest+json":
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, fp, ctype):
        """静态文件带 ETag。重复打开页面时浏览器带 If-None-Match 回来，
        命中就回 304，css、js 与图标不再整包重下。

        接口仍然 no-store：题库与进度必须每次都是新的。
        """
        st = os.stat(fp)
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        with open(fp, "rb") as f:
            data = f.read()
        self._send(200, data, ctype, cache="no-cache", etag=etag)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self, allow_list=False):
        raw = self.headers.get("Content-Length", "0").strip()
        try:
            n = int(raw)
        except ValueError:
            raise BadBody("Content-Length 不是整数")
        if n < 0:
            raise BadBody("请求体超出上限")
        if n > MAX_BODY:
            # 先把已声明的主体排空再回 400。直接拒绝会让还在写大 body 的
            # 客户端收到连接重置，根本看不到「请求体超出上限」这句话。
            # 最多排空两倍上限，避免一个巨大的 Content-Length 把线程拖住
            left = min(n, MAX_BODY * 2)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            raise BadBody("请求体超出上限")
        data = self.rfile.read(n) if n else b""
        if not data:
            return [] if allow_list else {}
        try:
            body = json.loads(data)
        except json.JSONDecodeError as e:
            raise BadBody(f"请求体不是合法 JSON：{e}")
        if isinstance(body, list):
            # 撤销清空发的是快照行数组，顶层就得是数组
            if not allow_list:
                raise BadBody("请求体必须是 JSON 对象")
            return body
        if not isinstance(body, dict):
            raise BadBody("请求体必须是 JSON 对象")
        return body

    def _read_rows(self):
        """读撤销用的快照行。

        旧版在这里直接 json.loads 加 r["card_id"]，Content-Length 不是整数、
        body 不是合法 JSON、行里缺字段，三种都变成连接被断开，用户看到的是
        撤销没反应，拿不到任何原因。行数还不设上限，一个巨大的 body 能读爆内存。
        """
        rows = self._read_body(allow_list=True)
        if len(rows) > MAX_ROWS:
            raise BadBody(f"行数超出上限 {MAX_ROWS}")
        out = []
        for r in rows:
            if not isinstance(r, dict):
                raise BadBody("每一行必须是 JSON 对象")
            if any(k not in r for k in ROW_KEYS):
                raise BadBody("行缺少字段：" + "、".join(k for k in ROW_KEYS if k not in r))
            if not isinstance(r["card_id"], str) or not r["card_id"]:
                raise BadBody("card_id 必须是非空文本")
            for k in ("seen", "marked", "reps", "lapses"):
                if isinstance(r[k], bool) or not isinstance(r[k], int):
                    raise BadBody(f"{k} 必须是整数")
            for k in ("ease", "interval", "due"):
                if isinstance(r[k], bool) or not isinstance(r[k], (int, float)):
                    raise BadBody(f"{k} 必须是数字")
            if not (EASE_MIN <= r["ease"] <= EASE_MAX):
                raise BadBody(f"ease 超出 {EASE_MIN} 到 {EASE_MAX}")
            if r["interval"] < 0 or r["interval"] > MAX_INTERVAL:
                raise BadBody(f"interval 超出 0 到 {MAX_INTERVAL}")
            if r["reps"] < 0 or r["lapses"] < 0:
                raise BadBody("reps 与 lapses 不能为负")
            out.append(r)
        return out

    def _card_id(self, body):
        cid = body.get("id")
        if not isinstance(cid, str) or not cid:
            raise BadBody("缺少 id")
        if cid not in card_ids():
            raise BadBody(f"卡片不存在：{cid}")
        return cid

    def do_GET(self):
        try:
            return self._get()
        except BadBody as e:
            return self._json({"error": str(e)}, 400)
        except Exception:
            # 未捕获异常以前会被 socketserver 吞掉，客户端只看到连接被断开。
            # 这里完整打印到终端，同时回一个明确的 500 JSON
            traceback.print_exc()
            return self._json({"error": "服务器内部错误，详见终端日志"}, 500)

    def _get(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p == "/api/cards":
            data = load_cards()
            cat = q.get("cat", ["all"])[0]
            sub = q.get("sub", [""])[0]
            mode = q.get("mode", ["all"])[0]
            cards = data["cards"]
            if cat != "all":
                cards = [c for c in cards if c["cat"] == cat]
            if sub:
                cards = [c for c in cards if sub_of(c) == sub]
            cards = merge(cards)
            if mode == "due":
                # 到期复习只放复习过且已到期的卡。未学卡 due_now 也是 true，
                # 混进来会让本模式开局等于全部卡片模式
                cards = [c for c in cards if c["seen"] and c["due_now"]]
            # sub 必须由这个接口自己带上。早期版本只有 /api/sidebar 会给缓存对象补
            # sub，两个请求并发时谁先返回决定了 cards 带不带 sub：带上就有队列，
            # 带不上前端就筛出空列表、卡片区显示「本轮完成」。纯靠竞态的正确性
            # 等于没有正确性。构造新对象而不是改缓存，避免再引入共享状态
            cards = [{**c, "sub": sub_of(c)} for c in cards]
            return self._json({
                "categories": ensure_categories(data),
                "cards": cards,
                "summary": summary(data["cards"]),
            })

        if p == "/api/progress":
            return self._json({"summary": summary()})

        if p == "/api/llm/models":
            return self._json({"models": llm_model_list()})

        if p == "/api/recycle":
            items = list_archived()
            return self._json({"items": items, "total": len(items)})

        if p == "/api/sidebar":
            # 只返回分组与张数。原先这里有个循环给缓存里每张卡补 sub，
            # 那是在给别的接口打补丁，已改由 /api/cards 自己带
            data = load_cards()
            return self._json({
                "groups": sidebar_tree(data["cards"], data["categories"]),
                "recycle": archive_count(),
            })

        if p == "/api/snapshot":
            return self._json({"rows": self.snapshot()})

        if p == "/api/open/cards":
            try:
                return self._json(self.open_cards(q))
            except BadBody as e:
                return self._json({"error": str(e)}, 400)

        if p == "/favicon.ico":
            # 浏览器无条件请求它，回 204 表示明确没有，不在控制台留 404 噪声
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if is_spa_route(p):
            # 前端路由。直接访问 /net 这类地址或刷新时都要能拿到页面，
            # 由前端读路径决定显示哪个分类。根路径也在这里统一处理
            return self._serve_file(os.path.join(WEB, "index.html"),
                                    "text/html; charset=utf-8")

        rel = p.lstrip("/")
        fp = os.path.realpath(os.path.join(WEB, rel))
        # realpath 之后要按目录边界比较，只比前缀会让 web 同级目录 web_x 变成可读
        inside = fp == WEB or fp.startswith(WEB + os.sep)
        ctype = _web_types.get(os.path.splitext(fp)[1])
        if not inside or not ctype or not os.path.isfile(fp):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        return self._serve_file(fp, ctype)

    def archive(self, body):
        card_id = self._card_id(body)
        # 不要求前端传 reason，服务端按排程状态推断
        record = archive_card(card_id, body.get("reason"))
        return self._json({"ok": True, "archived": record, "summary": summary()})

    def do_POST(self):
        try:
            return self._post()
        except BadBody as e:
            return self._json({"error": str(e)}, 400)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "服务器内部错误，详见终端日志"}, 500)

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/open/cards":
            # 对外接口是只读的，明确告诉调用方用错了方法，而不是给个含糊的 404
            return self._json({"error": "该接口只读，请用 GET"}, 405)
        if path not in ("/api/answer", "/api/mark", "/api/archive",
                        "/api/unarchive", "/api/explain", "/api/edit",
                        "/api/add", "/api/delete-category"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        body = self._read_body()
        if path == "/api/delete-category":
            d = delete_category(body.get("cat"))
            return self._json({"ok": True, **d, "summary": summary()})
        if path == "/api/add":
            card = add_card(body.get("cat"), body.get("q"), body.get("a"),
                            body.get("detail", ""), body.get("level", "basic"))
            return self._json({"ok": True, "card": card, "summary": summary()})
        if path == "/api/archive":
            return self.archive(body)
        if path == "/api/unarchive":
            # 恢复的是已删卡，它已经不在卡片库里，不能走 _card_id
            cid = body.get("id")
            if not isinstance(cid, str) or not cid:
                raise BadBody("缺少 id")
            card = unarchive_card(cid)
            return self._json({"ok": True, "card": card, "summary": summary()})
        if path == "/api/edit":
            return self._json(edit_card(body.get("id"),
                                        body.get("q"), body.get("a")))
        if path == "/api/explain":
            return self._json(explain_card(body.get("q"), body.get("a"),
                                           body.get("model")))
        card_id = self._card_id(body)
        if path == "/api/mark":
            return self.mark(card_id, 1 if body.get("marked") else 0)
        q = body.get("quality")
        if isinstance(q, bool) or not isinstance(q, int) or q not in (0, 1, 2):
            raise BadBody("quality 必须是 0、1、2 之一的整数")
        return self.answer(card_id, q)

    def answer(self, card_id, q):
        """评分只改排程，marked 保留库中原值。

        早期版本让前端在评分时回传 marked，前端对象一旦不同步就会把收藏静默清成 0。
        收藏是独立状态，只有 /api/mark 能改它。
        """
        with _lock:
            con = db()
            row = con.execute(
                "SELECT ease, interval, reps, lapses FROM state WHERE card_id=?", (card_id,)
            ).fetchone()
            cur = {"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0} if not row else {
                "ease": row[0], "interval": row[1], "reps": row[2], "lapses": row[3]
            }
            nxt = review(cur, q)
            con.execute(
                "INSERT INTO state(card_id, seen, marked, ease, interval, reps, lapses, due) "
                "VALUES(?,1,0,?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET "
                "seen=1, ease=excluded.ease, interval=excluded.interval,"
                "reps=excluded.reps, lapses=excluded.lapses, due=excluded.due",
                (card_id, nxt["ease"], nxt["interval"],
                 nxt["reps"], nxt["lapses"], nxt["due"]),
            )
            con.commit()
            con.close()
        return self._json({"ok": True, "next": nxt, "summary": summary()})

    def mark(self, card_id, marked):
        """收藏是独立状态，不能影响 SM-2 排程"""
        with _lock:
            con = db()
            con.execute(
                "INSERT INTO state(card_id, marked) VALUES(?,?) "
                "ON CONFLICT(card_id) DO UPDATE SET marked=excluded.marked",
                (card_id, marked),
            )
            con.commit()
            con.close()
        return self._json({"ok": True, "summary": summary()})

    def do_DELETE(self):
        try:
            return self._delete()
        except BadBody as e:
            return self._json({"error": str(e)}, 400)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "服务器内部错误，详见终端日志"}, 500)

    def _delete(self):
        path = urlparse(self.path).path
        if path == "/api/open/cards":
            return self._json({"error": "该接口只读，请用 GET"}, 405)
        if path == "/api/recycle":
            return self._json({"ok": True, "cleared": clear_archived()})
        if path not in ("/api/reset", "/api/restore"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        rows = self._read_rows() if path == "/api/restore" else []
        with _lock:
            con = db()
            dropped = 0
            if path == "/api/restore":
                # 快照可能是几分钟前取的，期间卡可能被删。写回去的孤儿行
                # 在读的时候会被 summary 与 merge 过滤，但会一直堆在库里，
                # 所以在这里就直接丢掉，并把数量告诉前端
                ids = card_ids()
                keep = [r for r in rows if r["card_id"] in ids]
                dropped = len(rows) - len(keep)
                con.execute("DELETE FROM state")
                for r in keep:
                    con.execute(
                        "INSERT OR REPLACE INTO state(card_id, seen, marked, ease, interval, reps, lapses, due) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (r["card_id"], r["seen"], r["marked"], r["ease"],
                         r["interval"], r["reps"], r["lapses"], r["due"]),
                    )
            else:
                con.execute("DELETE FROM state")
            con.commit()
            con.close()
        return self._json({"ok": True, "restored": len(rows) - dropped,
                           "dropped": dropped, "summary": summary()})

    def open_cards(self, q):
        """对外只读接口。给外部 app 或脚本取题库用，不带任何复习状态。

        与 /api/cards 的区别：
        /api/cards 是给本项目界面用的，会把 SM-2 排程状态拼进每张卡。
        这里只返回题面本身与溯源信息，不泄露学习进度，也不接受任何写操作。
        """
        data = load_cards()
        cards = data["cards"]

        cat = q.get("cat", ["all"])[0]
        if cat != "all":
            cards = [c for c in cards if c["cat"] == cat]

        level = q.get("level", ["all"])[0]
        if level != "all":
            cards = [c for c in cards if c.get("level") == level]

        theme = q.get("theme", ["all"])[0]
        if theme != "all":
            cards = [c for c in cards if c.get("theme") == theme]

        source = q.get("source", ["all"])[0]
        if source != "all":
            cards = [c for c in cards if c.get("source") == source]

        kw = q.get("q", [""])[0].strip().lower()
        if kw:
            cards = [
                c for c in cards
                if kw in (c["q"] + c["a"] + c.get("detail", "")).lower()
            ]

        total = len(cards)
        try:
            page = max(1, int(q.get("page", ["1"])[0]))
            size = int(q.get("page_size", ["50"])[0])
        except ValueError:
            raise BadBody("page 与 page_size 必须是整数")
        size = max(1, min(size, MAX_PAGE_SIZE))

        start = (page - 1) * size
        window = cards[start:start + size]

        # 逐字段挑，不直接回整张卡。将来卡片里加了什么私有字段也不会顺带泄露
        out = [{
            "id": c["id"],
            "cat": c["cat"],
            "level": c.get("level", ""),
            "q": c["q"],
            "a": c["a"],
            "detail": c.get("detail", ""),
            "source": c.get("source", ""),
            "source_url": c.get("source_url", ""),
            "author": c.get("author", ""),
            "theme": c.get("theme", ""),
        } for c in window]

        return {
            "total": total,
            "page": page,
            "page_size": size,
            "pages": (total + size - 1) // size if total else 0,
            "categories": ensure_categories(data),
            "levels": ["basic", "core", "practice", "edge"],
            "themes": sorted({c["theme"] for c in cards if c.get("theme")}),
            "sources": sorted({c["source"] for c in cards if c.get("source")}),
            "cards": out,
        }

    def snapshot(self):
        """清空前进度快照，供撤销用。界面不弹确认框，靠可撤销替代二次确认"""
        con = db()
        rows = [
            dict(zip(("card_id", "seen", "marked", "ease", "interval", "reps", "lapses", "due"), r))
            for r in con.execute(
                "SELECT card_id, seen, marked, ease, interval, reps, lapses, due FROM state"
            )
        ]
        con.close()
        return rows


if __name__ == "__main__":
    db().close()
    print(f"http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


