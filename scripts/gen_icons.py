"""生成站点图标全套尺寸。

规范要求至少 favicon.svg、32、180、192、512，外加 site.webmanifest，
并补 apple-touch-icon 与 theme-color。图标必须由脚本生成，改设计只改这里再重跑。

渲染方式用无头 Chrome 把 SVG 光栅化成 PNG。本机没有 cairosvg、rsvg-convert、
inkscape、imagemagick，Chrome 是唯一可用且结果一致的工具。

用法：
    .venv/bin/python scripts/gen_icons.py
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "web", "icon.svg")
OUT = os.path.join(BASE, "web")
PUBLIC = os.path.join(BASE, "frontend", "public")
CDP_PORT = 9360

SIZES = {
    "favicon-32.png": 32,
    "apple-touch-icon.png": 180,
    "icon-192.png": 192,
    "icon-512.png": 512,
}

# 品牌色取自规范指定的两个基色，不引入第四种色相
THEME_COLOR = "#1e222b"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_chrome():
    for p in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ):
        if os.path.exists(p):
            return p
    p = shutil.which("google-chrome") or shutil.which("chromium")
    if p:
        return p
    sys.exit("找不到 Chrome，无法生成 PNG")


def wait(url, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            _opener.open(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def render(sizes):
    """开一次 Chrome，把所有尺寸渲完再关"""
    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="icon_gen_")
    proc = subprocess.Popen(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait(f"http://127.0.0.1:{CDP_PORT}/json/version"):
            sys.exit("Chrome 调试端口未就绪")
        import websocket
        # 路径里有中文，必须先百分号编码，否则 http.client 拒绝非 ASCII
        url = "file://" + urllib.parse.quote(SRC)
        tab = json.loads(_opener.open(urllib.request.Request(
            f"http://127.0.0.1:{CDP_PORT}/json/new?{url}", method="PUT")).read())
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        _id = [0]

        def send(method, **params):
            _id[0] += 1
            ws.send(json.dumps({"id": _id[0], "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == _id[0]:
                    return msg.get("result")

        send("Page.enable")
        for name, size in sizes.items():
            send("Emulation.setDeviceMetricsOverride",
                 width=size, height=size, deviceScaleFactor=1, mobile=False)
            time.sleep(0.4)
            data = send("Page.captureScreenshot", format="png")["data"]
            path = os.path.join(OUT, name)
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            print(f"  {name:26} {size}x{size}  {os.path.getsize(path)} 字节")
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def manifest():
    icons = [{"src": "/icon-192.png?v=4", "sizes": "192x192", "type": "image/png"},
             {"src": "/icon-512.png?v=4", "sizes": "512x512", "type": "image/png"}]
    doc = {
        "name": "知识点问答卡",
        "short_name": "问答卡",
        "start_url": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": icons,
    }
    path = os.path.join(OUT, "site.webmanifest")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"  site.webmanifest       {os.path.getsize(path)} 字节")


def main():
    if not os.path.exists(SRC):
        sys.exit(f"找不到源图 {SRC}")
    print(f"源图 {SRC}")
    render(SIZES)
    manifest()
    # 产物同步到 Vite 的 public 目录，下次构建才能带进 web/dist
    os.makedirs(PUBLIC, exist_ok=True)
    for name in [*SIZES, "site.webmanifest", "icon.svg"]:
        src = os.path.join(OUT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(PUBLIC, name))
    print("完成")


if __name__ == "__main__":
    main()