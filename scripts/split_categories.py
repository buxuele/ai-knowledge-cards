"""把 Java 全栈、数据库、微服务与分布式拆成更细的顶级分类。

用法：
    .venv/bin/python scripts/split_categories.py

拆分结果：
    java  Java 基础
    spring Spring 框架
    vue   Vue 前端
    db    数据库
    redis Redis 与缓存
    micro 微服务
    dist  分布式与部署

幂等：按 id 归类，重复执行结果不变。
"""

import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(BASE, "data", "cards.json")

CATEGORIES = [
    {"id": "ai", "name": "AI 技术",
     "summary": "Agent 架构、记忆系统、MCP 与 LSP、AI 编程方法论"},
    {"id": "java", "name": "Java 基础",
     "summary": "Java 语法、面向对象、集合、并发与 JVM"},
    {"id": "spring", "name": "Spring 框架",
     "summary": "IoC、AOP、Bean、事务、Spring Boot 与 MyBatis"},
    {"id": "vue", "name": "Vue 前端",
     "summary": "Vue3 响应式、生命周期、组件通信与前端工程"},
    {"id": "net", "name": "网络",
     "summary": "丢包、延迟、抖动的分离判断法，DNS 与无线信道"},
    {"id": "db", "name": "数据库",
     "summary": "关系型数据库、SQL、事务、索引与 NoSQL"},
    {"id": "redis", "name": "Redis 与缓存",
     "summary": "Redis 数据结构、缓存三兄弟、持久化与分布式锁"},
    {"id": "micro", "name": "微服务",
     "summary": "服务拆分、注册中心、网关、熔断限流、配置与可观测"},
    {"id": "dist", "name": "分布式与部署",
     "summary": "CAP 与 BASE、分布式事务、消息队列、容器与发布"},
    {"id": "misc", "name": "杂项", "summary": ""},
]
ORDER = [c["id"] for c in CATEGORIES]

# 从 Java 全栈拆出来的框架卡
SPRING = {
    "java-005", "java-012", "java-013", "java-014", "java-016",
    "java-017", "java-019", "java-031", "java-032", "java-033",
    "java-034", "java-035", "java-036",
}
VUE = {
    "java-006", "java-007", "java-015", "java-018",
    "java-037", "java-038", "java-039", "java-040",
}
# Redis、缓存与缓存一致性
REDIS = {
    "db-113", "db-114", "db-115", "db-120", "db-139",
    "infra-cache-penetration", "infra-cache-consistency",
    "lat-caching-layers",
}
# 微服务治理与服务间调用
MICRO = {
    "dist-001", "dist-002", "dist-003", "dist-004", "dist-005",
    "dist-006", "dist-007", "dist-008", "dist-009", "dist-012",
    "dist-024", "dist-025", "dist-026", "dist-027", "dist-028",
    "dist-029", "dist-035", "dist-037", "dist-038", "dist-039",
}


def pick(card):
    cid, cat = card["id"], card["cat"]
    if cat == "java":
        if cid in SPRING:
            return "spring"
        if cid in VUE:
            return "vue"
    if cat == "db" and cid in REDIS:
        return "redis"
    if cat == "dist" and cid in MICRO:
        return "micro"
    return cat


def remap_archives():
    """已删卡的归档也要跟着换分类，否则从回收站恢复会回到旧分类。"""
    folder = os.path.join(BASE, "data", "archived")
    if not os.path.isdir(folder):
        return 0
    changed = 0
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        old = rec.get("cat", "")
        new = pick({"id": rec.get("id", name[:-5]), "cat": old})
        card = rec.get("card")
        if isinstance(card, dict):
            new_card_cat = pick(card)
        else:
            new_card_cat = new
        if new != old or (isinstance(card, dict) and card.get("cat") != new_card_cat):
            rec["cat"] = new
            if isinstance(card, dict):
                card["cat"] = new_card_cat
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
            changed += 1
    return changed


def main():
    with open(CARDS, encoding="utf-8") as f:
        data = json.load(f)

    for c in data["cards"]:
        c["cat"] = pick(c)

    data["categories"] = CATEGORIES
    # 侧边栏按卡片首次出现的分类排序，顺手按新分类聚拢，顺序与分类表一致
    data["cards"] = [c for _, c in sorted(
        enumerate(data["cards"]), key=lambda x: (ORDER.index(x[1]["cat"]), x[0]))]

    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CARDS)

    counts = {}
    for c in data["cards"]:
        counts[c["cat"]] = counts.get(c["cat"], 0) + 1
    print("分类张数", [(c["name"], counts.get(c["id"], 0)) for c in CATEGORIES])
    print("总计", len(data["cards"]))
    print("归档改分类", remap_archives())


if __name__ == "__main__":
    main()
