"""把今天补的后端面试卡导出成 Markdown，再调用 make-epub-books 生成 EPUB。

用法：
    python scripts/build_epub.py
    先安装 make-epub-books 依赖，或用 MAKE_EPUB_DIR 指向工具目录。

产物：
    电子书/后端面试知识卡.epub
    电子书/后端面试知识卡_封面.png
    电子书/源文件/            可重新打包的 Markdown
"""

import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(BASE, "data", "cards.json")
OUT_DIR = os.path.join(BASE, "电子书")
SRC_DIR = os.path.join(OUT_DIR, "源文件")
TOOL_DIR = os.environ.get("MAKE_EPUB_DIR", "")
if not TOOL_DIR:
    raise SystemExit("请设置 MAKE_EPUB_DIR，指向 make-epub-books 目录")
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from make_epub import create_epub  # noqa: E402

BOOK_TITLE = "后端面试知识卡"
COVER = os.path.join(OUT_DIR, f"{BOOK_TITLE}_封面.png")
OUTPUT = os.path.join(OUT_DIR, f"{BOOK_TITLE}.epub")

# 今天新增的批次。早期遗留卡不混进这本书
THEMES = {
    "java-interview", "java-basics",
    "db-interview", "db-basics",
    "dist-interview", "dist-basics",
}

LEVEL_ORDER = {"basic": 0, "core": 1, "practice": 2, "edge": 3}
LEVEL_NAME = {"basic": "基础", "core": "核心", "practice": "实践", "edge": "边界"}

PARTS = [
    ("第1部分 Java 全栈", [
        ("第1章 Java 基础", "java", "Java 语法、面向对象、集合、并发与 JVM"),
        ("第2章 Spring 框架", "spring", "IoC、AOP、Bean、事务、Spring Boot 与 MyBatis"),
        ("第3章 Vue 前端", "vue", "Vue3 响应式、生命周期、组件通信与前端工程"),
    ]),
    ("第2部分 数据库", [
        ("第1章 数据库", "db", "关系型数据库、SQL、事务、索引与 NoSQL"),
        ("第2章 Redis 与缓存", "redis", "Redis 数据结构、缓存三兄弟、持久化与分布式锁"),
    ]),
    ("第3部分 微服务与分布式", [
        ("第1章 微服务", "micro", "服务拆分、注册中心、网关、熔断限流、配置与可观测"),
        ("第2章 分布式与部署", "dist", "CAP 与 BASE、分布式事务、消息队列、容器与发布"),
    ]),
]


def load_today_cards():
    with open(CARDS, encoding="utf-8") as f:
        cards = json.load(f)["cards"]
    return [c for c in cards if c.get("theme") in THEMES]


def chapter_markdown(summary, cards):
    lines = [
        f"本卷收录 {len(cards)} 张卡，覆盖{summary}。",
        "",
        "卡片按基础、核心、实践、边界四档从易到难排列。每张先给一句结论，再给解释和例子。",
        "",
    ]
    for i, c in enumerate(cards, 1):
        lines.append(f"## {i}. {c['q']}")
        lines.append("")
        lines.append(f"答：{c['a']}")
        lines.append("")
        detail = (c.get("detail") or "").strip()
        if detail:
            lines.append(detail.replace("\n", "\n\n"))
            lines.append("")
        lines.append(f"难度：{LEVEL_NAME.get(c.get('level'), c.get('level', ''))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_source(cards):
    if os.path.isdir(SRC_DIR):
        shutil.rmtree(SRC_DIR)
    os.makedirs(SRC_DIR)

    total = len(cards)
    intro = [
        "这是一本从知识点问答卡整理出来的面试复习书，收录今天补的三个方向：Java 全栈、数据库、微服务与分布式。",
        "",
        f"全书共 {total} 张卡，分成 3 个部分、7 章。每张卡只讲一个概念，先给结论，再给解释，最后用一个具体例子落地。题目参考 2026 年公开的 Java、数据库与后端面试题库常见考点，答案与例子为本书重写。",
        "",
        "## 怎么读",
        "",
        "1. 第一次读按目录顺序看，先建立每个方向的概念地图。",
        "2. 第二遍只挑自己答不上来的卡，遮住答案先自测。",
        "3. 面试前一晚看每章最后的核心与边界卡，那里是追问最多的地方。",
        "",
        "## 难度标记",
        "",
        "1. 基础：只需要记住定义和直觉。",
        "2. 核心：考区别、原因和适用场景。",
        "3. 实践：考具体怎么做。",
        "4. 边界：考极端情况、坑和取舍。",
        "",
    ]
    with open(os.path.join(SRC_DIR, "序言.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(intro))

    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)

    for part, chapters in PARTS:
        part_dir = os.path.join(SRC_DIR, part)
        os.makedirs(part_dir)
        for chapter, cat, summary in chapters:
            rows = by_cat.get(cat, [])
            if not rows:
                continue
            rows.sort(key=lambda c: (LEVEL_ORDER.get(c.get("level"), 9), c["id"]))
            path = os.path.join(part_dir, f"{chapter}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(chapter_markdown(summary, rows))
            print(f"{part} / {chapter}: {len(rows)} 张")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cards = load_today_cards()
    print(f"今天的卡 {len(cards)} 张")
    write_source(cards)
    out = create_epub(
        src_dir=SRC_DIR,
        book_title=BOOK_TITLE,
        author="知识点问答卡",
        cover_subtitle="2026年11月06日",
        cover_theme="light_ivory",
        cover_image=COVER,
        auto_gen_cover=True,
        output_file=OUTPUT,
    )
    print("输出", out)


if __name__ == "__main__":
    main()
