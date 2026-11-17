"""把一本 EPUB 拆成可阅读的 HTML 章节，图片一起落地。

改顶部 BOOK 与 OUT_NAME 两个变量即可换书。输出在 ai_books/html/<OUT_NAME>/。
用法：
    python scripts/epub_to_html.py
"""

import os
import posixpath
import re
import shutil

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(BASE, "ai_books", "Agent设计模式图解可复用智能体架构.epub")
OUT_NAME = "agent-design-patterns"
OUT = os.path.join(BASE, "ai_books", "html", OUT_NAME)

CSS = """
body { margin: 0 auto; max-width: 860px; padding: 40px 24px 80px;
  background: #15181e; color: #e6e9ef; font: 17px/1.9 -apple-system,
  "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
a { color: #9adb37; }
h1, h2, h3 { line-height: 1.4; color: #f2f4f8; }
h1 { font-size: 32px; margin: 0 0 28px; }
h2 { font-size: 26px; margin: 36px 0 16px; }
h3 { font-size: 21px; margin: 28px 0 12px; }
p { margin: 0 0 14px; }
img { max-width: 100%; height: auto; display: block; margin: 20px auto;
  border-radius: 8px; }
blockquote { margin: 18px 0; padding: 8px 18px; border-left: 3px solid #9adb37;
  color: #b9c0cc; background: #1c2028; }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre { background: #1c2028; padding: 16px; border-radius: 8px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 18px 0; }
th, td { border: 1px solid #343a46; padding: 8px 10px; text-align: left; }
.nav { display: flex; justify-content: space-between; gap: 16px;
  margin: 40px 0 0; padding-top: 20px; border-top: 1px solid #343a46; }
.toc { list-style: none; padding: 0; }
.toc li { margin: 0 0 8px; }
"""


def clean_title(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def toc_titles(book):
    out = {}

    def walk(node):
        if isinstance(node, tuple):
            for child in node:
                walk(child)
        elif isinstance(node, epub.Link):
            out[node.href.split("#")[0]] = clean_title(node.title)
        elif isinstance(node, epub.Section):
            out[node.href.split("#")[0]] = clean_title(node.title)

    for node in book.toc:
        walk(node)
    return out


def chapter_key(name):
    m = re.search(r"Chapter(\d+)", name)
    return f"ch{m.group(1)}" if m else name


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "images"), exist_ok=True)
    book = epub.read_epub(BOOK)
    titles = toc_titles(book)
    by_name = {item.get_name(): item for item in book.get_items()}
    groups = {}
    group_index = {}

    for idref in book.spine:
        item = book.get_item_with_id(idref[0] if isinstance(idref, tuple) else idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        body = soup.body or soup
        for tag in body.find_all(["script", "style"]):
            tag.decompose()
        text = body.get_text(" ", strip=True)
        if len(text) < 120:
            continue
        heading = body.find(["h1", "h2", "h3"])
        title = titles.get(item.get_name()) or (heading.get_text(" ", strip=True) if heading else "")
        title = clean_title(title) or f"章节 {len(groups) + 1}"
        key = chapter_key(item.get_name())
        if key not in groups:
            group_index[key] = len(groups) + 1
            groups[key] = {"title": title, "html": ""}
        gidx = group_index[key]

        for img in body.find_all("img"):
            src = img.get("src", "")
            if not src or src.startswith(("http://", "https://", "data:")):
                continue
            name = posixpath.normpath(posixpath.join(
                posixpath.dirname(item.get_name()), src.split("#")[0].split("?")[0]))
            target = by_name.get(name)
            if target is None:
                continue
            ext = os.path.splitext(name)[1].lower() or ".png"
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", posixpath.basename(name))
            out_name = f"{gidx:02d}-{len(groups[key]['html']) % 10000:04d}-{safe}"
            if not out_name.lower().endswith(ext):
                out_name += ext
            with open(os.path.join(OUT, "images", out_name), "wb") as f:
                f.write(target.get_content())
            img["src"] = f"images/{out_name}"
            img.attrs.pop("srcset", None)

        groups[key]["html"] += str(body)

    pages = [(g["title"], g["html"]) for g in groups.values()]
    if not pages:
        raise SystemExit("没有解析到章节")

    links = []
    for i, (title, body_html) in enumerate(pages):
        fname = f"{i + 1:03d}.html"
        prev_link = f'<a href="{i:03d}.html">上一章</a>' if i > 0 else "<span></span>"
        next_link = f'<a href="{i + 2:03d}.html">下一章</a>' if i + 1 < len(pages) else "<span></span>"
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>{title}</h1>{body_html}<div class="nav">{prev_link}<a href="index.html">目录</a>{next_link}</div>
</body></html>"""
        with open(os.path.join(OUT, fname), "w", encoding="utf-8") as f:
            f.write(html)
        links.append((fname, title))

    items = "\n".join(f'<li><a href="{f}">{t}</a></li>' for f, t in links)
    index = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{book.get_metadata('DC', 'title')[0][0]}</title><style>{CSS}</style></head><body>
<h1>{book.get_metadata('DC', 'title')[0][0]}</h1><ul class="toc">{items}</ul>
</body></html>"""
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(index)
    print(f"输出 {len(pages)} 章到 {OUT}")


if __name__ == "__main__":
    main()
