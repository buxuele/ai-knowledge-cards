"""浏览器验收：自起服务与无头 Chrome，跑完自动清理，输出一份可核对的结论。

以前每轮验证都要临时写一份 CDP 脚本、临时起服务、临时起 Chrome、临时猜端口，
重复成本高且容易漏清理。这里固化成一条命令：

    .venv/bin/python tests/browser_check.py

它替人做三件事：
1. 自己找空闲端口起服务，自己起 Chrome，用完按进程号精确关闭。
2. 把每轮都要验的点固化成用例，改完跑一次就知道有没有回归。
3. 输出全部断言的通过与失败明细，不靠退出码猜。
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(BASE, ".venv", "bin", "python")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9333
SHOT_DIR = os.path.join(BASE, "效果图")

# 本机开着 Clash 系统代理，直连 127.0.0.1 必须绕过，否则请求会被送进代理返回 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_procs = []


def cleanup():
    """只关自己启动的进程，按进程号精确 kill，绝不按名字模糊匹配"""
    for p in reversed(_procs):
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def free_port(start=8900, span=60):
    for port in range(start, start + span):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def wait_ready(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _OPENER.open(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


class Chrome:
    """极简 CDP 客户端，只封装本项目用得到的能力"""

    def __init__(self, port=CDP_PORT):
        self.port = port
        self.ws = None
        self._id = 0

    def start(self, url, width=1280, height=900, scale=2):
        profile = "/tmp/cdp_check_profile"
        subprocess.run(["rm", "-rf", profile], check=False)
        self.proc = subprocess.Popen(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", "--remote-allow-origins=*",
             f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _procs.append(self.proc)
        if not wait_ready(f"http://127.0.0.1:{self.port}/json/version", 15):
            raise RuntimeError("Chrome 调试端口未就绪")
        import websocket
        tab = json.loads(_OPENER.open(
            urllib.request.Request(
                f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT")).read())
        self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=25)
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setDeviceMetricsOverride",
                  width=width, height=height, deviceScaleFactor=scale, mobile=False)
        self._await_render()

    def _await_render(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js("document.getElementById('q') && "
                       "document.getElementById('q').textContent.trim().length > 5"):
                return True
            time.sleep(0.4)
        return False

    def send(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result")

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r["result"].get("value")

    def shot(self, name):
        os.makedirs(SHOT_DIR, exist_ok=True)
        import base64
        path = os.path.join(SHOT_DIR, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(self.send("Page.captureScreenshot", format="png")["data"]))
        return path

    def stop(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


CASES = [
    # (名称, 表达式, 判定函数)。判定函数返回 False 才算失败，
    # 不再用字符串匹配猜结果，那样出过假通过。
    ("复位到初始状态", """
      (async () => {
        // 每条用例跑在同一个页面上，状态会互相污染。
        // 分类、子类、搜索、模式、页码全部复位，后面的用例才有确定起点。
        // 这条必须放在最前面，否则前面用例的残留会让复位本身失效。
        // 顺手把刚打开页面时的落点记下来，那是首页默认分类的唯一真实证据
        const 首屏 = {路径: location.pathname, 分类: S.cat, 子类: S.sub};
        S.cat = 'all'; S.sub = ''; S.mode = 'all';
        S.page = 0; S.i = 0;
        S.listView = false; S.flipped = false;
        // 全部卡片改挂 /all，首页让给了 AI 技术，复位时必须把地址一起改过去，
        // 否则状态说全部、地址说 AI，后面靠后退键的用例会拿到错的起点
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listBtn').classList.remove('on');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('done').hidden = true;
        resetPage();
        applyFilter();
        buildTree();
        await new Promise(r => setTimeout(r, 300));
        return {首屏, 分类: S.cat, 子类: S.sub, 队列: S.queue.length,
                路径: location.pathname,
                选中项: (document.querySelector('.node.on') || {}).textContent || ''};
      })()""",
     lambda v: v["分类"] == "all" and v["子类"] == "" and v["队列"] > 0
     and v["路径"] == "/all"
     and v["选中项"].startswith("全部")
     and v["首屏"]["路径"] == "/ai" and v["首屏"]["分类"] == "ai"
     and v["首屏"]["子类"] == ""),
    ("卡片总数与分类", """
      (async () => {
        if (S.cat !== 'ai') { go('ai', ''); await new Promise(r => setTimeout(r, 250)); }
        const 顶 = [...document.querySelectorAll('.grp-hd')].map(b => b.textContent);
        const 子 = [...document.querySelectorAll('.kids .node')].map(b => b.textContent);
        return { 顶级分类: 顶, 子分类: 子, 总数: S.all.length };
      })()""",
     # 用户会右键删分类，顶级数量和具体分类不再写死；至少要有 AI、网络、数据库
     lambda v: v["总数"] > 0 and len(v["顶级分类"]) >= 3
     and len(v["子分类"]) >= 6
     and any(t.startswith("AI") for t in v["顶级分类"])
     and any(t.startswith("网络") for t in v["顶级分类"])
     and any(t.startswith("数据库") for t in v["顶级分类"])),
    ("无快捷键残留", """
      (async () => {
        window.confirm = () => { throw new Error('弹了确认'); };
        window.prompt = () => { throw new Error('弹了原因'); };
        const before = S.flipped;
        document.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true}));
        await new Promise(r => setTimeout(r, 200));
        return { 空格未翻卡: S.flipped === before };
      })()""",
     lambda v: v["空格未翻卡"] is True),
    ("点空白翻面且按钮保留", """
      (async () => {
        const f = document.querySelector('.face.front');
        const r = f.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.bottom - 30);
        hit.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        await new Promise(r => setTimeout(r, 300));
        const 空白翻面 = S.flipped;
        const 按钮在 = !!document.getElementById('showBtn');
        document.getElementById('nextBtn').click();
        await new Promise(r => setTimeout(r, 300));
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 300));
        return { 空白翻面, 按钮保留: 按钮在, 按钮翻面: S.flipped };
      })()""",
     lambda v: all(v.values())),
    ("划词不误翻面", """
      (async () => {
        // 必须回到正面再测，且要真的选中文字，否则选中判断走不到分支
        if (document.getElementById('card').classList.contains('flip')) {
          document.getElementById('nextBtn').click();
          await new Promise(r => setTimeout(r, 400));
        }
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('q'));
        sel.removeAllRanges();
        sel.addRange(range);
        const 选中长度 = sel.toString().length;
        document.querySelector('.face.front').dispatchEvent(
          new MouseEvent('click', {bubbles: true}));
        await new Promise(r => setTimeout(r, 250));
        const 选中未翻 = !S.flipped;
        sel.removeAllRanges();
        document.querySelector('.face.front').dispatchEvent(
          new MouseEvent('click', {bubbles: true}));
        await new Promise(r => setTimeout(r, 250));
        const 清空后翻面 = S.flipped;
        return { 选中长度, 选中未翻, 清空后翻面 };
      })()""",
     lambda v: v["选中长度"] > 0 and v["选中未翻"] and v["清空后翻面"]),
    ("翻面零跳动", """
      (async () => {
        // 连续测三张，先把上一张的翻面态归零，避免读到中间态
        const card = document.getElementById('card');
        const q1 = document.getElementById('q'), q2 = document.getElementById('q2');
        const out = [];
        for (let n = 0; n < 3; n++) {
          if (card.classList.contains('flip')) {
            document.getElementById('nextBtn').click();
            await new Promise(r => setTimeout(r, 450));
          }
          const a = {h: card.getBoundingClientRect().height,
                     t: card.getBoundingClientRect().top,
                     b: card.getBoundingClientRect().bottom,
                     q: q1.getBoundingClientRect().top,
                     fs: getComputedStyle(q1).fontSize};
          document.getElementById('showBtn').click();
          await new Promise(r => setTimeout(r, 450));
          const b = {h: card.getBoundingClientRect().height,
                     t: card.getBoundingClientRect().top,
                     b: card.getBoundingClientRect().bottom,
                     q: q2.getBoundingClientRect().top,
                     fs: getComputedStyle(q2).fontSize};
          out.push({高度差: b.h - a.h, 顶差: b.t - a.t, 底差: b.b - a.b,
                    问题位移: +(b.q - a.q).toFixed(2), 字号一致: a.fs === b.fs});
        }
        return {三轮: out,
                全零: out.every(r => r.高度差 === 0 && r.顶差 === 0 &&
                                    r.底差 === 0 && r.问题位移 === 0 && r.字号一致)};
      })()""",
     lambda v: v["全零"] is True and len(v["三轮"]) == 3),
    ("字号层级单调递减", """
      (() => {
        const q = parseFloat(getComputedStyle(document.getElementById('q')).fontSize);
        const a = parseFloat(getComputedStyle(document.getElementById('a')).fontSize);
        const d = parseFloat(getComputedStyle(document.getElementById('d')).fontSize);
        return {问题: q, 答案: a, 展开: d, 递减: q > a && a > d};
      })()""",
     lambda v: v["递减"] and v["问题"] == 21),
    ("分页正确", """
      (async () => {
        document.getElementById('listBtn').click();
        await new Promise(r => setTimeout(r, 500));
        const first = {计数: document.getElementById('listCount').textContent,
                       行数: document.querySelectorAll('.row').length,
                       页码按钮: document.querySelectorAll('.pg-num').length};
        window.scrollTo(0, 400);
        await new Promise(r => setTimeout(r, 200));
        const 翻页前滚动 = scrollY;
        document.getElementById('nextPage').click();
        await new Promise(r => setTimeout(r, 450));
        const second = {计数: document.getElementById('listCount').textContent,
                        行数: document.querySelectorAll('.row').length,
                        翻页后滚动: scrollY,
                        当前页: document.querySelector('.pg-num.on').textContent};
        document.getElementById('listBtn').click();
        await new Promise(r => setTimeout(r, 300));
        return {首页: first, 次页: second, 翻页前滚动,
                每页不超15: first.行数 <= 15 && second.行数 <= 15,
                翻页回顶: second.翻页后滚动 < 50 && 翻页前滚动 > 50};
      })()""",
     lambda v: v["每页不超15"] and v["翻页回顶"]
     and "第 1 /" in v["首页"]["计数"] and "第 2 /" in v["次页"]["计数"]
     and v["首页"]["页码按钮"] > 0),
    ("删除零弹窗", """
      (async () => {
        window.confirm = () => { throw new Error('弹了确认'); };
        window.prompt = () => { throw new Error('弹了原因'); };
        const before = S.all.length;
        await removeCard(S.queue[S.i].id);
        return {删前: before, 删后: S.all.length, 少一张: before - S.all.length === 1};
      })()""",
     lambda v: v["少一张"] is True),
    ("清空进度可撤销", """
      (async () => {
        const btn = document.getElementById('resetBtn');
        document.getElementById('listBtn').click();
        await new Promise(r => setTimeout(r, 300));
        await fetch('/api/answer', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: S.all[0].id, quality: 2})});
        const before = (await (await fetch('/api/progress')).json()).summary;
        btn.click();
        await new Promise(r => setTimeout(r, 900));
        const cleared = (await (await fetch('/api/progress')).json()).summary;
        btn.click();
        await new Promise(r => setTimeout(r, 900));
        const back = (await (await fetch('/api/progress')).json()).summary;
        document.getElementById('listBtn').click();
        await new Promise(r => setTimeout(r, 200));
        return {清空前已复习: before.seen, 清空后: cleared.seen,
                撤销后: back.seen, 可撤销: back.seen === before.seen};
      })()""",
     lambda v: v["可撤销"] and v["清空后"] == 0),
    ("按钮文案最短", """
      // 只查操作按钮，分类按钮带张数不算文案问题
      [...document.querySelectorAll('.btn, .icon-btn, .sw')]
        .map(b => b.textContent)""",
     lambda v: all(len(t) <= 5 for t in v)),
    ("解释按钮在收藏右边", """
      (() => {
        const m = document.getElementById('markBtn').getBoundingClientRect();
        const x = document.getElementById('explainBtn').getBoundingClientRect();
        return {在右边: x.left >= m.right,
                文案: document.getElementById('explainBtn').textContent};
      })()""",
     lambda v: v["在右边"] and v["文案"] == "解释"),

    ("删除按钮紧挨编辑在顶排左组", """
      (async () => {
        // 删除原先分居卡片底部左右两处，正面一处背面一处，翻面才看得到另一处。
        // 现在收进顶排左边这一组，紧挨编辑的右边，右侧留给计数与编辑态的按钮
        const card = document.getElementById('card');
        if (card.classList.contains('flip')) {
          document.getElementById('nextBtn').click();
          await new Promise(r => setTimeout(r, 400));
        }
        const box = (el) => el.getBoundingClientRect();
        const del = document.getElementById('delBtn');
        const 顶排 = box(document.querySelector('.card-top'));
        const 编辑右 = box(document.getElementById('editBtn')).right;
        const 计数左 = box(document.getElementById('pos')).left;
        const 正面位置 = box(del);
        const 正面 = {
          只有一个: document.querySelectorAll('.btn.del').length === 1,
          底排已无: document.querySelectorAll('.foot .del').length === 0,
          在排内: 正面位置.top >= 顶排.top - 1 && 正面位置.bottom <= 顶排.bottom + 1,
          紧挨编辑: 正面位置.left >= 编辑右 && 正面位置.left - 编辑右 <= 20,
          在收藏解释右边: 正面位置.left >= box(document.getElementById('explainBtn')).right
                        && 正面位置.left >= box(document.getElementById('markBtn')).right,
          在计数左边: 正面位置.right <= 计数左,
          文案: del.textContent
        };
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 450));
        const 反面位置 = box(del);
        const 反面 = {
          在反面: !document.getElementById('footBack').hidden,
          仍可见: !del.hidden,
          位置不动: Math.abs(反面位置.top - 正面位置.top) < 2
                   && Math.abs(反面位置.left - 正面位置.left) < 2
        };
        // 收尾回正面，别把翻面态留给后面的用例
        document.getElementById('nextBtn').click();
        await new Promise(r => setTimeout(r, 400));
        return {正面, 反面};
      })()""",
     # 文案必须是删除两个字，顶排只剩这一个，紧贴编辑右边，正面与背面位置完全一致
     lambda v: (v["正面"]["只有一个"] and v["正面"]["底排已无"]
                and v["正面"]["在排内"] and v["正面"]["紧挨编辑"]
                and v["正面"]["在收藏解释右边"] and v["正面"]["在计数左边"]
                and v["正面"]["文案"] == "删除"
                and v["反面"]["在反面"] and v["反面"]["仍可见"]
                and v["反面"]["位置不动"])),
    ("解释全挂时复制到剪贴板", """
      (async () => {
        const orig = window.fetch;
        let copied = null;
        window.fetch = async () => ({json: async () => ({
          ok: false, reason: '所有模型都不可用',
          fallback: '测试问题？\\n测试答案。'})});
        navigator.clipboard.writeText = async (t) => { copied = t; };
        document.getElementById('explainBtn').click();
        await new Promise(r => setTimeout(r, 700));
        window.fetch = orig;
        const box = document.getElementById('xout');
        const btn = document.getElementById('explainBtn');
        return {已复制: copied !== null, 复制内容: copied || '',
                已显示: !box.hidden, 是警示: box.classList.contains('warn'),
                有原因前缀: box.textContent.startsWith('所有模型都不可用。'),
                有提示前缀: box.textContent.includes('已复制到剪贴板'),
                按钮已恢复: !btn.disabled && btn.textContent === '解释'};
      })()""",
     # 模型全挂时必须复制问题而不是干等着，按钮也要恢复可用。
     # 文案顺序是先服务端给的原因，再是否复制成功的提示，不能只查开头
     lambda v: v["已复制"] and "测试问题" in v["复制内容"] and v["已显示"]
     and v["是警示"] and v["有原因前缀"] and v["有提示前缀"] and v["按钮已恢复"]),
    ("解释成功后清掉警示样式", """
      (async () => {
        // 先人为挂上警示样式，模拟上一次降级留下的状态。
        // 漏掉 remove('warn') 是实测发现的真 bug，成功结果会一直挂着警告色
        const box = document.getElementById('xout');
        box.classList.add('warn');
        const orig = window.fetch;
        window.fetch = async () => ({json: async () => ({
          ok: true, model: '测试', text: '这是一段模拟的成功解释，长度足够通过检查。'})});
        document.getElementById('explainBtn').click();
        await new Promise(r => setTimeout(r, 700));
        window.fetch = orig;
        return {仍警示: box.classList.contains('warn'),
                正文: box.textContent, 已显示: !box.hidden};
      })()""",
     lambda v: not v["仍警示"] and "模拟的成功解释" in v["正文"] and v["已显示"]),
    ("切卡清空解释区", """
      (async () => {
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 300));
        document.getElementById('nextBtn').click();
        await new Promise(r => setTimeout(r, 700));
        return {隐藏: document.getElementById('xout').hidden,
                空: document.getElementById('xout').textContent === '',
                按钮可用: !document.getElementById('explainBtn').disabled};
      })()""",
     lambda v: v["隐藏"] and v["空"] and v["按钮可用"]),
    ("侧边栏在左且结构完整", """
      (async () => {
        if (S.cat !== 'ai') { go('ai', ''); await new Promise(r => setTimeout(r, 250)); }
        const s = document.querySelector('.side').getBoundingClientRect();
        const m = document.querySelector('main').getBoundingClientRect();
        const nodes = [...document.querySelectorAll('.node')];
        return {在左侧: s.right <= m.left + 1, 宽度: Math.round(s.width),
                节点数: nodes.length,
                有全部: nodes.some(n => n.textContent.startsWith('全部')),
                有AI子类: document.querySelectorAll('.kids .node').length,
                有中文名: nodes.every(n => /[\\u4e00-\\u9fff]/.test(n.textContent)),
                有张数: nodes.every(n => /\\d+$/.test(n.textContent))};
      })()""",
     # 张数用等宽数字并右对齐，靠这条保证每个节点都带了张数
     lambda v: v["在左侧"] and v["宽度"] > 150 and v["有全部"]
     and v["有AI子类"] >= 6 and v["有中文名"] and v["有张数"]),
    ("侧边栏选中项用整圈边框", """
      (() => {
        // 选中态必须是整圈 1px 绿边加淡绿底色，不能只亮左边一条竖线
        const on = document.querySelector('.node.on');
        if (!on) return {没有选中项: true};
        const s = getComputedStyle(on);
        return {左: s.borderLeftColor, 右: s.borderRightColor,
                上: s.borderTopColor, 下: s.borderBottomColor,
                左宽: s.borderLeftWidth, 背景: s.backgroundColor};
      })()""",
     lambda v: not v.get("没有选中项")
     and v["左"] == v["右"] == v["上"] == v["下"]
     and v["左"] == "rgb(154, 219, 55)"
     and v["左宽"] == "1px"
     and v["背景"] != "rgba(0, 0, 0, 0)"),
    ("路由切换分类", """
      (async () => {
        const out = [];
        const pick = async (label) => {
          const a = [...document.querySelectorAll('.node')]
            .find(x => x.textContent.startsWith(label));
          a.click();
          await new Promise(r => setTimeout(r, 600));
          return {标签: label, 路径: location.pathname, 分类: S.cat, 子类: S.sub,
                  队列: S.queue.length,
                  全对: S.queue.every(c => label === '全部' || c.cat === label
                                       || c.sub === (S.sub || '')),
                  选中: (document.querySelector('.node.on') || {}).textContent || ''};
        };
        out.push(await pick('记忆与上下文'));
        out.push(await pick('网络'));
        out.push(await pick('数据库'));
        out.push(await pick('全部'));
        return out;
      })()""",
     # 路径与状态必须同步，且队列里不能混进别的分类的卡。
     # 全部卡片现在挂在 /all，根路径归 AI 技术
     lambda v: (isinstance(v, list) and len(v) == 4
                and v[0]["路径"] == "/ai/memory" and v[0]["队列"] > 0
                and v[1]["路径"] == "/net" and v[1]["全对"] and v[1]["队列"] > 0
                and v[2]["路径"] == "/db" and v[2]["全对"] and v[2]["队列"] > 0
                and v[3]["路径"] == "/all" and v[3]["队列"] > 0)),
    ("后退键还原路由", """
      (async () => {
        if (S.cat !== 'ai') { go('ai', ''); await new Promise(r => setTimeout(r, 350)); }
        [...document.querySelectorAll('.node')]
          .find(x => x.textContent.startsWith('安全与权限')).click();
        await new Promise(r => setTimeout(r, 600));
        const 去安全 = location.pathname;
        [...document.querySelectorAll('.node')]
          .find(x => x.textContent.startsWith('工具与 MCP')).click();
        await new Promise(r => setTimeout(r, 600));
        history.back();
        await new Promise(r => setTimeout(r, 800));
        return {去安全: 去安全, 退后路径: location.pathname,
                分类: S.cat, 子类: S.sub, 队列: S.queue.length,
                还原正确: location.pathname === 去安全 && S.sub === 'security',
                队列对: S.queue.every(c => c.sub === 'security')};
      })()""",
     # 不接 popstate 的话浏览器直接整页刷新，侧边栏路由就白做了
     lambda v: v["还原正确"] and v["队列对"] and v["队列"] > 0
     and v["退后路径"] == "/ai/security"),
    ("路由用例结束后复位", """
      (async () => {
        // 路由类用例会留下 cat 与 sub。后面那些查固定卡片的用例假定在全部范围内，
        // 不复位就会随机失败。这条把状态收干净，让顺序不影响结果。
        S.cat = 'all'; S.sub = ''; S.page = 0; S.i = 0;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        resetPage();
        applyFilter();
        buildTree();
        await new Promise(r => setTimeout(r, 300));
        return {分类: S.cat, 子类: S.sub, 路径: location.pathname,
                队列: S.queue.length};
      })()""",
     lambda v: v["分类"] == "all" and v["子类"] == ""
     and v["路径"] == "/all" and v["队列"] > 0),
    ("卡片上方已无进度条与统计行", """
      (() => {
        // 这一条横线与那行「全库 N 张 已掌握 X 待复习 Y 收藏 Z」按需求删掉了。
        // 掌握口径只留在侧边栏底部，数字仍必须是真 mastered，不是 seen
        const main = document.querySelector('main');
        const 第一个块 = main.children[0];
        const foot = document.getElementById('sideFoot');
        return {进度条在不在: !!document.getElementById('pfill'),
                统计行在不在: !!document.getElementById('ptext'),
                横线在不在: !!document.querySelector('.progress'),
                首个块: 第一个块 ? 第一个块.className : '',
                首块是卡片或启动提示: /boot|card|list-view|done/.test(第一个块 ? 第一个块.className : ''),
                侧边栏脚: foot ? foot.textContent : '',
                脚里有张数: foot ? /共 \\d+ 张/.test(foot.textContent) : false,
                脚里有掌握: foot ? /已掌握 \\d+ 张/.test(foot.textContent) : false};
      })()""",
     # 顺带钉住一件事：删掉的是那张脸，not mastered 口径本身，侧边栏底部仍要报真数
     lambda v: (not v["进度条在不在"] and not v["统计行在不在"]
                and not v["横线在不在"] and v["首块是卡片或启动提示"]
                and v["脚里有张数"] and v["脚里有掌握"])),

    ("首页与全部各自的路由", """
      (() => {
        // 首页让给 AI 技术，全部卡片改挂 /all，两条路径不能再打架
        const 首页 = parseRoute('/');
        const 全部 = parseRoute('/all');
        const ai子类 = parseRoute('/ai/memory');
        return {首页分类: 首页.cat, 首页子类: 首页.sub, 首页路径: routeOf(首页.cat, 首页.sub),
                全部分类: 全部.cat, 全部路径: routeOf(全部.cat, 全部.sub),
                子类分类: ai子类.cat, 子类名: ai子类.sub, 子类路径: routeOf(ai子类.cat, ai子类.sub)};
      })()""",
     lambda v: (v["首页分类"] == "ai" and v["首页子类"] == "" and v["首页路径"] == "/ai"
                and v["全部分类"] == "all" and v["全部路径"] == "/all"
                and v["子类分类"] == "ai" and v["子类名"] == "memory"
                and v["子类路径"] == "/ai/memory")),

    ("顶排四个按钮同亮度", """
      (() => {
        // 收藏与删除原先是压暗的次级色，混在同一排里字色深浅不一，
        // 容易被当成禁用状态。四个按钮的文字色必须完全一致
        const ids = ['markBtn', 'explainBtn', 'editBtn', 'delBtn'];
        const 色 = ids.map(i => getComputedStyle(document.getElementById(i)).color);
        const 框 = ids.map(i => {
          const r = document.getElementById(i).getBoundingClientRect();
          return {左: Math.round(r.left), 右: Math.round(r.right), 高: Math.round(r.height)};
        });
        return {文字色: 色, 同色: new Set(色).size === 1,
                等高: new Set(框.map(x => x.高)).size === 1,
                从左到右: 框.every((x, i) => i === 0 || x.左 >= 框[i - 1].右),
                边框色: getComputedStyle(document.getElementById('delBtn')).borderTopColor};
      })()""",
     lambda v: v["同色"] and v["等高"] and v["从左到右"]
     and v["文字色"][0].startswith("rgb")),
    ("顶部来源下拉已移除", """
      (() => {
        // 顶部只留打乱、列表、只看收藏、外观；新增移到侧边栏底部，
        // 全部卡片与全部难度已经删掉
        const tools = [...document.querySelectorAll('.tools .icon-btn')].map(b => b.textContent.trim());
        return {下拉还在: !!document.getElementById('srcSel'), 顶栏工具: tools,
                有侧栏新增: !!document.getElementById('addBtn'),
                有难度: !!document.getElementById('levelSel')};
      })()""",
     lambda v: not v["下拉还在"] and v["顶栏工具"] == ["打乱", "列表", "只看收藏", "设置"]
     and v["有侧栏新增"] and not v["有难度"]),
    ("卡背不显示来源", """
      (async () => {
        if (S.listView) {
          document.getElementById('listBtn').click();
          await new Promise(r => setTimeout(r, 300));
        }
        const c = S.all.find(x => x.source);
        if (!c) return {没有来源卡: true};
        S.cat = 'all'; S.sub = '';
        resetPage(); applyFilter();
        await new Promise(r => setTimeout(r, 300));
        const at = S.queue.findIndex(x => x.id === c.id);
        if (at < 0) return {进不了队列: true};
        S.i = at; render();
        await new Promise(r => setTimeout(r, 200));
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 300));
        return {已翻: document.getElementById('card').classList.contains('flip'),
                有来源元素: !!document.getElementById('src') || !!document.querySelector('.card-src')};
      })()""",
     lambda v: not v.get("没有来源卡") and not v.get("进不了队列")
     and v["已翻"] and not v["有来源元素"]),

    ("编辑入口与编辑态", """
      (async () => {
        const b = document.getElementById('editBtn');
        const before = {有按钮: !!b, 文案: b ? b.textContent : '',
                        收藏可见: !document.getElementById('markBtn').hidden,
                        解释可见: !document.getElementById('explainBtn').hidden,
                        删除可见: !document.getElementById('delBtn').hidden,
                        保存隐藏: document.getElementById('saveBtn').hidden,
                        取消隐藏: document.getElementById('cancelBtn').hidden,
                        编辑框隐藏: document.getElementById('editBox').hidden};
        const c = cur();
        const 原q = c.q, 原a = c.a;
        b.click();
        await new Promise(r => setTimeout(r, 250));
        const eq = document.getElementById('eq'), ea = document.getElementById('ea');
        return {点前: before, 卡id: c.id,
                点后: {编辑框可见: !document.getElementById('editBox').hidden,
                       收藏隐藏: document.getElementById('markBtn').hidden,
                       解释隐藏: document.getElementById('explainBtn').hidden,
                       删除隐藏: document.getElementById('delBtn').hidden,
                       编辑按钮隐藏: document.getElementById('editBtn').hidden,
                       保存可见: !document.getElementById('saveBtn').hidden,
                       取消可见: !document.getElementById('cancelBtn').hidden,
                       带编辑类: document.getElementById('card').classList.contains('editing'),
                       问题框对: eq.value === 原q,
                       答案框对: ea.value === 原a,
                       焦点在问题框: document.activeElement === eq}};
      })()""",
     # 编辑态里一张卡只能有一套控件，正反面的按钮必须全部退场
     lambda v: (v["点前"]["有按钮"] and v["点前"]["文案"] == "编辑"
                and v["点前"]["保存隐藏"] and v["点前"]["编辑框隐藏"]
                and v["点前"]["收藏可见"] and v["点前"]["解释可见"]
                and v["点前"]["删除可见"]
                and v["点后"]["编辑框可见"] and v["点后"]["保存可见"]
                and v["点后"]["取消可见"] and v["点后"]["收藏隐藏"]
                and v["点后"]["解释隐藏"] and v["点后"]["删除隐藏"]
                and v["点后"]["编辑按钮隐藏"]
                and v["点后"]["带编辑类"] and v["点后"]["问题框对"]
                and v["点后"]["答案框对"] and v["点后"]["焦点在问题框"])),

    ("空问题与超长问题被拦下", """
      (async () => {
        const err = document.getElementById('editErr');
        const id = cur().id;
        document.getElementById('eq').value = '   ';
        document.getElementById('saveBtn').click();
        await new Promise(r => setTimeout(r, 250));
        const 空 = {可见: !err.hidden, 文案: err.textContent, 仍编辑态: S.editing};
        document.getElementById('eq').value = '字'.repeat(301);
        document.getElementById('saveBtn').click();
        await new Promise(r => setTimeout(r, 250));
        const 超长 = {可见: !err.hidden, 文案: err.textContent, 仍编辑态: S.editing};
        document.getElementById('cancelBtn').click();
        await new Promise(r => setTimeout(r, 200));
        return {空, 超长, 取消后: {编辑态: S.editing,
                                编辑框隐藏: document.getElementById('editBox').hidden},
                数据没被改: JSON.stringify(S.all.find(x => x.id === id).q)
                          !== JSON.stringify('字'.repeat(301))};
      })()""",
     # 拦在前端，用户就不用看服务端的错误文案才知道哪儿错了
     lambda v: (v["空"]["可见"] and v["空"]["文案"] == "问题不能为空"
                and v["空"]["仍编辑态"]
                and v["超长"]["可见"] and "300" in v["超长"]["文案"]
                and v["超长"]["仍编辑态"]
                and not v["取消后"]["编辑态"] and v["取消后"]["编辑框隐藏"]
                and v["数据没被改"])),

    ("保存改写问题并当场还原", """
      (async () => {
        const c = cur();
        const id = c.id;
        const 原q = c.q, 原a = c.a, 原src = c.source, 原detail = c.detail;
        const 新q = 原q + '，补个逗号';
        document.getElementById('editBtn').click();
        document.getElementById('eq').value = 新q;
        document.getElementById('saveBtn').click();
        let 等 = 0;
        while (S.editing && 等 < 40) {
          await new Promise(r => setTimeout(r, 200)); 等++;
        }
        const 中 = S.all.find(x => x.id === id);
        const 保存后 = {退出编辑态: !S.editing,
                       错误隐藏: document.getElementById('editErr').hidden,
                       内存已更新: 中.q === 新q,
                       页面已更新: document.getElementById('q').textContent === 新q,
                       答案没动: 中.a === 原a,
                       detail没动: 中.detail === 原detail,
                       source没动: 中.source === 原src};
        // 重取一次，确认写回了服务端而不是只改了前端内存。
        // load 会重排队列并把索引归零，必须按 id 找回当前卡，
        // 否则第二次编辑点开的是另一张卡
        await load();
        let at = S.queue.findIndex(x => x.id === id);
        if (at >= 0) { S.i = at; render(); }
        const 重取 = S.all.find(x => x.id === id);
        const 写回 = 重取.q === 新q;

        // 还原原文，后面的用例不该拿到改过的问题
        document.getElementById('editBtn').click();
        document.getElementById('eq').value = 原q;
        document.getElementById('saveBtn').click();
        等 = 0;
        while (S.editing && 等 < 40) {
          await new Promise(r => setTimeout(r, 200)); 等++;
        }
        await load();
        at = S.queue.findIndex(x => x.id === id);
        if (at >= 0) { S.i = at; render(); }
        await new Promise(r => setTimeout(r, 200));
        const 收尾 = S.all.find(x => x.id === id);
        return {保存后, 写回, 还原成功: 收尾.q === 原q,
                编辑的还是同一张: document.getElementById('eq').value === 原q,
                页面还原: document.getElementById('q').textContent === 原q};
      })()""",
     lambda v: (v["保存后"]["退出编辑态"] and v["保存后"]["错误隐藏"]
                and v["保存后"]["内存已更新"] and v["保存后"]["页面已更新"]
                and v["保存后"]["答案没动"] and v["保存后"]["detail没动"]
                and v["保存后"]["source没动"] and v["写回"]
                and v["还原成功"] and v["页面还原"])),

    ("换卡时编辑态自动收起", """
      (async () => {
        const 第一张 = cur().id;
        document.getElementById('editBtn').click();
        document.getElementById('eq').value = '不该留在这里的内容';
        const 另一张 = S.queue.find(c => c.id !== 第一张);
        const at = S.queue.findIndex(c => c.id === 另一张.id);
        S.i = at;
        render();
        await new Promise(r => setTimeout(r, 250));
        return {编辑态已收: !S.editing,
                编辑框隐藏: document.getElementById('editBox').hidden,
                editingId已清: S.editingId === null,
                输入框没污染新卡: document.getElementById('q').textContent !== '不该留在这里的内容',
                显示的是新卡: document.getElementById('q').textContent === 另一张.q,
                收藏按钮回来: !document.getElementById('markBtn').hidden};
      })()""",
     # 不收的话，上一张的输入框会留在新卡上，改的内容静默写到另一张卡去
     lambda v: v["编辑态已收"] and v["编辑框隐藏"] and v["editingId已清"]
     and v["输入框没污染新卡"] and v["显示的是新卡"] and v["收藏按钮回来"]),

    ("列表行有编辑入口", """
      (async () => {
        S.editing = false;
        document.getElementById('listBtn').click();
        await new Promise(r => setTimeout(r, 400));
        const rows = [...document.querySelectorAll('.row')];
        const 目标行 = rows[1] || rows[0];
        const 目标问题 = 目标行.querySelector('.rq').textContent;
        const ed = 目标行.querySelector('.row-edit');
        const 有编辑按钮 = !!ed && ed.textContent === '编辑';
        const 有删除按钮 = !!目标行.querySelector('.row-del');
        if (ed) ed.click();
        await new Promise(r => setTimeout(r, 500));
        const eq = document.getElementById('eq');
        return {行数: rows.length, 有编辑按钮, 有删除按钮,
                进编辑态: S.editing,
                离开列表: document.getElementById('listView').hidden,
                卡片可见: !document.getElementById('card').hidden,
                问题框对: eq.value === 目标问题};
      })()""",
     lambda v: v["行数"] > 0 and v["行数"] <= 15 and v["有编辑按钮"]
     and v["有删除按钮"] and v["进编辑态"] and v["离开列表"]
     and v["卡片可见"] and v["问题框对"]),

    ("编辑用例结束后复位", """
      (async () => {
        document.getElementById('cancelBtn').click();
        S.cat = 'all'; S.sub = ''; S.page = 0; S.i = 0;
        S.editing = false; S.editingId = null;
        // 关键词也是残留源。不清的话复位后的队列只剩十几张，
        // 后面的用例以为在全部范围内跑，实际只看到了一个子集
        S.mode = 'all';
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        resetPage();
        applyFilter();
        buildTree();
        await new Promise(r => setTimeout(r, 300));
        return {编辑态: S.editing, 分类: S.cat, 子类: S.sub,
                队列: S.queue.length,
                全库: S.all.length,
                编辑框隐藏: document.getElementById('editBox').hidden,
                收藏回来: !document.getElementById('markBtn').hidden,
                解释回来: !document.getElementById('explainBtn').hidden,
                删除回来: !document.getElementById('delBtn').hidden};
      })()""",
     lambda v: not v["编辑态"] and v["分类"] == "all" and v["子类"] == ""
     and v["队列"] == v["全库"] and v["队列"] > 0
     and v["编辑框隐藏"] and v["收藏回来"] and v["解释回来"]
     and v["删除回来"]),

    ("展开解释后内容装得下", """
      (async () => {
        // 卡片常态随内容长高，最小 470；翻面两面同高。
        // 展开解释时解除视口上限，第三段不能被裁
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 350));
        const 翻面高 = Math.round(document.getElementById('card').getBoundingClientRect().height);
        document.getElementById('explainBtn').click();
        for (let k = 0; k < 60; k++) {
          await new Promise(r => setTimeout(r, 500));
          const x = document.getElementById('xout');
          if (!x.hidden && x.textContent.length > 40) break;
        }
        await new Promise(r => setTimeout(r, 350));
        const x = document.getElementById('xout');
        const f = document.querySelector('.face.back');
        const c = document.getElementById('card');
        const 结果 = {显示了: !x.hidden,
                     字数: x.textContent.length,
                     段数: x.textContent.split('\\n').filter(s => s.trim()).length,
                     含例如: x.textContent.includes('例如'),
                     含否则: x.textContent.includes('否则'),
                     // 模型全挂时走降级，文案是提示去问别的模型，不会有三段结构
                     降级: x.classList.contains('warn'),
                     翻面高: 翻面高,
                     展开后高: Math.round(c.getBoundingClientRect().height),
                     长高类: c.classList.contains('x-on'),
                     溢出: f.scrollHeight - f.clientHeight,
                     装得下: f.scrollHeight <= f.clientHeight,
                     滚动条: getComputedStyle(f).scrollbarWidth};
        // 收尾：清解释、翻回正面，别把状态留给后面的用例
        document.getElementById('showBtn') && document.getElementById('footBack');
        x.hidden = true; x.textContent = '';
        c.classList.remove('x-on');
        c.classList.remove('flip');
        document.getElementById('footFront').hidden = false;
        document.getElementById('footBack').hidden = true;
        S.flipped = false;
        await new Promise(r => setTimeout(r, 250));
        结果.收尾后高 = Math.round(c.getBoundingClientRect().height);
        结果.收尾类已清 = !c.classList.contains('x-on');
        return 结果;
      })()""",
     # 装得下与卡片高度是无条件的：降级文案同样会被裁。
     # 三段结构、例如、否则只在模型真的返回结果时要求，走降级时这些不适用
     lambda v: (v["显示了"] and v["装得下"] and v["溢出"] <= 0
                and v["长高类"] and v["翻面高"] >= 470
                and v["展开后高"] >= v["翻面高"] and v["滚动条"] != "none"
                and v["收尾后高"] == v["翻面高"] and v["收尾类已清"]
                and (v["降级"]
                     or (v["段数"] >= 2 and v["含例如"] and v["含否则"])))),
]

# 面试题目录导入的三份文档必须真的进到 AI 路由，并按来源可筛。
# 张数取动态值，只钉住「进得来、筛得准、三块内容都在」这三件事，
# 以后往面试题目录里再加文档不会把这条用例跑红。
CASES.append(
    ("面试题卡进入 AI 路由", """
      (async () => {
        const 面试来源 = ['面试题库', '可能的面试题', '常见面试题 top50'];
        const 面试卡 = S.all.filter(c => 面试来源.includes(c.source));
        S.listView = false;
        S.cat = 'ai'; S.sub = ''; S.mode = 'all';
        history.replaceState(null, '', '/ai');
        document.getElementById('listBtn').classList.remove('on');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('done').hidden = true;
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 300));
        const ai队列 = S.queue.length;
        const ai全对 = S.queue.every(c => c.cat === 'ai');
        const 来源张数 = {};
        for (const s of 面试来源) 来源张数[s] = S.all.filter(c => c.source === s).length;
        // 抽一张面试卡翻面，确认答案与 detail 都在
        const 样本卡 = 面试卡[0];
        const at = 样本卡 ? S.queue.findIndex(x => x.id === 样本卡.id) : -1;
        S.i = at >= 0 ? at : 0;
        render();
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 300));
        const a = document.getElementById('a').textContent.trim();
        const d = document.getElementById('d').textContent.trim();
        return {面试卡总数: 面试卡.length, 来源张数: 来源张数,
                ai路由队列: ai队列, ai路由全对: ai全对,
                样本: {答案字数: a.length, detail字数: d.length,
                      含考察点: d.includes('考察点')}};
      })()""",
     # 张数取动态值：用户会在界面上删卡，写死 110 会在正常删除后误报。
     # 只钉住三份文档都还有卡、都进得来
     lambda v: v["面试卡总数"] > 0
     and v["来源张数"]["面试题库"] > 0
     and v["来源张数"]["可能的面试题"] > 0
     and v["来源张数"]["常见面试题 top50"] > 0
     and v["ai路由队列"] > 0 and v["ai路由全对"]
     and v["样本"]["答案字数"] > 0 and v["样本"]["detail字数"] > 0
     and v["样本"]["含考察点"]),
)


# 2026-10-20 新增：遗忘评分、收藏筛选、空结果状态、删除撤销、全部链接地址。
# 每条自己复位状态，不依赖前面的用例铺路。
CASES.append(
    ("全部节点链接指向 /all", """
      (() => {
        // 点击走 go("all") 到 /all，右键复制链接拿到的是 href。
        // href 写 / 会落到首页 AI 技术，两条路径对不上
        const a = [...document.querySelectorAll('.node')]
          .find(x => x.textContent.startsWith('全部'));
        return {href: a ? a.getAttribute('href') : '', 文案: a ? a.textContent : ''};
      })()""",
     lambda v: v["href"] == "/all"),
)

CASES.append(
    ("没记住按钮记为失败", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('listBtn').classList.remove('on');
        document.getElementById('markFilterBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 300));
        const c = S.queue.find(x => !x.seen) || S.queue[0];
        if (!c) return {无卡: true};
        S.i = S.queue.indexOf(c); render();
        await new Promise(r => setTimeout(r, 150));
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 150));
        const 有按钮 = !!document.getElementById('forgotBtn');
        const 可见 = !document.getElementById('forgotBtn').hidden;
        document.getElementById('forgotBtn').click();
        await new Promise(r => setTimeout(r, 800));
        const p = await (await fetch('/api/cards')).json();
        const t = p.cards.find(x => x.id === c.id);
        return {有按钮, 可见, 卡片: c.id,
                seen: t ? t.seen : null, reps: t ? t.reps : null,
                lapses: t ? t.lapses : null};
      })()""",
     # 没记住是失败回忆：seen 记 1，reps 清零，lapses 加一
     lambda v: not v.get("无卡") and v["有按钮"] and v["可见"]
     and v["seen"] == 1 and v["reps"] == 0 and v["lapses"] >= 1),
)

CASES.append(
    ("空结果显示没有匹配", """
      (async () => {
        // 搜索框已删，用只看收藏制造 0 命中：先清掉所有收藏再开筛选
        const marked = S.all.filter(c => c.marked);
        for (const c of marked) {
          await fetch('/api/mark', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: c.id, marked: 0})});
        }
        await load();
        document.getElementById('markFilterBtn').click();
        await new Promise(r => setTimeout(r, 500));
        const 空状态 = !document.getElementById('empty').hidden;
        const 完成状态 = !document.getElementById('done').hidden;
        const 文案 = document.getElementById('empty').textContent;
        document.getElementById('markFilterBtn').click();
        await new Promise(r => setTimeout(r, 300));
        return {空状态, 完成状态, 文案,
                复位: !S.markedOnly && document.getElementById('empty').hidden};
      })()""",
     # 筛不到东西是没命中，不是本轮学完了
     lambda v: v["空状态"] and not v["完成状态"]
     and "没有匹配" in v["文案"] and v["复位"]),
)

CASES.append(
    ("只看收藏过滤", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('markFilterBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 300));
        const c = S.queue[0];
        if (!c) return {无卡: true};
        await fetch('/api/mark', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: c.id, marked: 1})});
        await load();
        await new Promise(r => setTimeout(r, 300));
        document.getElementById('markFilterBtn').click();
        await new Promise(r => setTimeout(r, 500));
        const 队列 = S.queue.length;
        const 全收藏 = S.queue.every(x => x.marked);
        // 复位：关筛选并取消刚才那次收藏
        document.getElementById('markFilterBtn').click();
        await new Promise(r => setTimeout(r, 200));
        await fetch('/api/mark', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: c.id, marked: 0})});
        await load();
        return {队列, 全收藏, 复位: !S.markedOnly};
      })()""",
     lambda v: not v.get("无卡") and v["队列"] > 0
     and v["全收藏"] and v["复位"]),
)

CASES.append(
    ("删除后可撤销", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('listBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 400));
        const c = cur();
        if (!c) return {无卡: true};
        document.getElementById('delBtn').click();
        await new Promise(r => setTimeout(r, 800));
        const 撤销 = document.querySelector('#toast .toast-act');
        const 有撤销 = !!撤销;
        if (撤销) 撤销.click();
        await new Promise(r => setTimeout(r, 1000));
        const p = await (await fetch('/api/cards')).json();
        const 回来了 = p.cards.some(x => x.id === c.id);
        return {有撤销, 回来了, 卡片: c.id};
      })()""",
     lambda v: not v.get("无卡") and v["有撤销"] and v["回来了"]),
)

CASES.append(
    ("空分类不显示", """
      (() => {
        // 回收站不是分类，空的时候也保留入口；其余 0 张的节点必须从侧边栏消失
        const 零分类 = [...document.querySelectorAll('.node')]
          .filter(n => !n.textContent.startsWith('回收站'))
          .filter(n => (n.querySelector('.n') || {}).textContent === '0')
          .map(n => n.textContent);
        return {零分类};
      })()""",
     lambda v: v["零分类"] == []),
)

CASES.append(
    ("新增卡片", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.level = 'all';
        S.markedOnly = false; S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 400));
        const q = '浏览器新增测试卡？';
        document.getElementById('addBtn').click();
        const catSel = document.getElementById('addCat');
        const addCatId = catSel.options[0].value;
        catSel.value = addCatId;
        document.getElementById('addLevel').value = 'basic';
        document.getElementById('addQ').value = q;
        document.getElementById('addA').value = '这是新增答案。';
        document.getElementById('addDetail').value = '解释。\\n例如一个例子。';
        document.getElementById('addSave').click();
        let 等 = 0;
        while (S.adding && 等 < 30) {
          await new Promise(r => setTimeout(r, 200)); 等++;
        }
        const p = await (await fetch('/api/cards')).json();
        const hit = p.cards.find(x => x.q === q);
        return {表单已关: !S.adding, 工具可见: !document.querySelector('.tools').hidden,
                已入库: !!hit, 分类: hit ? hit.cat : '', 期望分类: addCatId,
                页面是这张: document.getElementById('q').textContent === q};
      })()""",
     lambda v: v["表单已关"] and v["工具可见"] and v["已入库"]
     and v["分类"] == v["期望分类"] and v["页面是这张"]),
)


CASES.append(
    ("代码块渲染成代码", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.level = 'all';
        S.markedOnly = false; S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 350));
        const idx = S.queue.findIndex(c => (c.detail || '').includes('```'));
        if (idx < 0) return {无代码卡: true};
        S.i = idx; render();
        await new Promise(r => setTimeout(r, 200));
        const box = document.getElementById('d');
        return {有代码块: !!box.querySelector('pre code'),
                有复制按钮: !!box.querySelector('.code-copy'),
                无围栏残留: !box.textContent.includes('```'),
                代码非空: (box.querySelector('pre code') || {}).textContent !== ''};
      })()""",
     lambda v: not v.get("无代码卡") and v["有代码块"] and v["有复制按钮"]
     and v["无围栏残留"] and v["代码非空"]),
)

CASES.append(
    ("回收站搜索与清空", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.level = 'all';
        S.markedOnly = false; S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 350));
        const c = cur();
        if (!c) return {无卡: true};
        document.getElementById('delBtn').click();
        await new Promise(r => setTimeout(r, 800));
        [...document.querySelectorAll('.node')]
          .find(x => x.textContent.startsWith('回收站')).click();
        await new Promise(r => setTimeout(r, 600));
        const search = document.getElementById('recycleSearch');
        search.value = c.q.slice(0, 6);
        search.dispatchEvent(new Event('input', {bubbles: true}));
        await new Promise(r => setTimeout(r, 250));
        const 搜到 = document.querySelectorAll('#recycleBody .recycle-row').length;
        search.value = '';
        search.dispatchEvent(new Event('input', {bubbles: true}));
        await new Promise(r => setTimeout(r, 200));
        window.confirm = () => true;
        document.getElementById('recycleClearBtn').click();
        await new Promise(r => setTimeout(r, 900));
        const 清空后 = document.querySelectorAll('#recycleBody .recycle-row').length;
        document.getElementById('recycleBackBtn').click();
        await new Promise(r => setTimeout(r, 400));
        return {搜到, 清空后, 已退出: !S.recycleView};
      })()""",
     lambda v: not v.get("无卡") and v["搜到"] >= 1 and v["清空后"] == 0
     and v["已退出"]),
)

CASES.append(
    ("回收站可恢复", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('listBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 400));
        const c = cur();
        if (!c) return {无卡: true};
        const 全库 = S.all.length;
        document.getElementById('delBtn').click();
        await new Promise(r => setTimeout(r, 800));
        const 节点 = [...document.querySelectorAll('.node')]
          .find(x => x.textContent.startsWith('回收站'));
        const 有节点 = !!节点;
        if (节点) 节点.click();
        await new Promise(r => setTimeout(r, 600));
        const 在回收站 = S.recycleView
          && !document.getElementById('recycle').hidden
          && document.querySelector('.tools').hidden;
        const row = document.querySelector(
          `#recycleBody .recycle-row[data-id="${c.id}"]`);
        const 有这一行 = !!row;
        const 恢复 = row && row.querySelector('.row-restore');
        if (恢复) 恢复.click();
        await new Promise(r => setTimeout(r, 1200));
        const p = await (await fetch('/api/cards')).json();
        const 回到卡库 = p.cards.some(x => x.id === c.id);
        const 行已消失 = !document.querySelector(
          `#recycleBody .recycle-row[data-id="${c.id}"]`);
        const 全库恢复 = p.cards.length === 全库;
        document.getElementById('recycleBackBtn').click();
        await new Promise(r => setTimeout(r, 500));
        return {有节点, 在回收站, 有这一行, 回到卡库, 行已消失, 全库恢复,
                退出回收站: !S.recycleView,
                工具可见: !document.querySelector('.tools').hidden,
                路径: location.pathname};
      })()""",
     lambda v: not v.get("无卡") and v["有节点"] and v["在回收站"]
     and v["有这一行"] and v["回到卡库"] and v["行已消失"] and v["全库恢复"]
     and v["退出回收站"] and v["工具可见"] and v["路径"] == "/all"),
)

CASES.append(
    ("右键分类直接删除", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.level = 'all';
        S.markedOnly = false; S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 350));
        const node = [...document.querySelectorAll('.grp-hd')]
          .find(n => !n.textContent.startsWith('AI'));
        if (!node) return {没有可删分类: true};
        const catId = node.getAttribute('href').replace('/', '');
        let 弹了确认 = false;
        window.confirm = () => { 弹了确认 = true; return true; };
        node.dispatchEvent(new MouseEvent('contextmenu', {
          bubbles: true, cancelable: true, clientX: 120, clientY: 200,
        }));
        await new Promise(r => setTimeout(r, 200));
        const menu = document.querySelector('.cat-menu');
        const del = menu && menu.querySelector('.cat-del');
        const 有菜单 = !!menu;
        const 有删除按钮 = !!del;
        if (del) del.click();
        await new Promise(r => setTimeout(r, 1200));
        const p = await (await fetch('/api/cards')).json();
        const r = await (await fetch('/api/recycle')).json();
        const 分类没了 = !p.categories.some(c => c.id === catId);
        const 卡进回收站 = r.items.some(x => x.cat === catId);
        return {有菜单, 有删除按钮, 弹了确认, 分类没了, 卡进回收站,
                路径: location.pathname};
      })()""",
     lambda v: not v.get("没有可删分类") and v["有菜单"] and v["有删除按钮"]
     and not v["弹了确认"] and v["分类没了"] and v["卡进回收站"]),
)

CASES.append(
    ("设置面板可切换外观和模型", """
      (async () => {
        document.getElementById('settingsBtn').click();
        await new Promise(r => setTimeout(r, 500));
        const panel = document.querySelector('.settings-panel');
        const 外观按钮 = [...document.querySelectorAll('.settings-themes .sw')].map(b => b.textContent);
        const 模型框 = document.getElementById('llmModel');
        const 有面板 = !!panel;
        const 有外观 = 外观按钮.length === 3;
        const 有模型 = !!模型框;
        if (外观按钮.length) document.querySelector('.settings-themes .sw').click();
        await new Promise(r => setTimeout(r, 150));
        document.getElementById('settingsClose').click();
        await new Promise(r => setTimeout(r, 150));
        return {有面板, 有外观, 外观按钮, 有模型, 面板已关: !document.querySelector('.settings-panel')};
      })()""",
     lambda v: v["有面板"] and v["有外观"] and v["有模型"] and v["面板已关"]),
)

CASES.append(
    ("删除后停在同一位置", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('listBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 400));
        if (S.queue.length < 6) return {卡太少: true};
        // 停在第 4 张，删掉它之后应该顺手看到原来的第 5 张，位置仍是第 4
        S.i = 3; render();
        await new Promise(r => setTimeout(r, 150));
        const 删前位置 = S.i + 1;
        const 删前队列 = S.queue.length;
        const 下一张 = S.queue[4].id;
        document.getElementById('delBtn').click();
        await new Promise(r => setTimeout(r, 900));
        const 删后位置 = S.i + 1;
        const 删后队列 = S.queue.length;
        const 停在下一张 = !!cur() && cur().id === 下一张;
        const 撤销 = document.querySelector('#toast .toast-act');
        if (撤销) 撤销.click();
        await new Promise(r => setTimeout(r, 900));
        return {删前位置, 删后位置, 删前队列, 删后队列, 停在下一张,
                有撤销: !!撤销, 恢复后位置: S.i + 1};
      })()""",
     lambda v: not v.get("卡太少") and v["删后队列"] == v["删前队列"] - 1
     and v["删后位置"] == v["删前位置"] and v["停在下一张"] and v["有撤销"]),
)

CASES.append(
    ("长内容卡片随内容长高", """
      (async () => {
        S.cat = 'all'; S.sub = ''; S.mode = 'all'; S.markedOnly = false;
        S.listView = false;
        if (location.pathname !== '/all') history.replaceState(null, '', '/all');
        document.getElementById('listView').hidden = true;
        document.getElementById('card').hidden = false;
        document.getElementById('listBtn').classList.remove('on');
        resetPage(); applyFilter(); buildTree();
        await new Promise(r => setTimeout(r, 400));
        // 找一张 detail 超过 900 字的卡，正是以前会被 470 固定高度裁掉的那类
        const idx = S.queue.findIndex(c => (c.detail || '').length >= 900);
        if (idx < 0) return {没有长卡: true};
        S.i = idx; render();
        await new Promise(r => setTimeout(r, 200));
        document.getElementById('showBtn').click();
        await new Promise(r => setTimeout(r, 300));
        const card = document.getElementById('card');
        const back = document.querySelector('.face.back');
        return {卡片: S.queue[idx].id,
                detail字数: (S.queue[idx].detail || '').length,
                高度: Math.round(card.getBoundingClientRect().height),
                最小高度: parseFloat(getComputedStyle(card).minHeight),
                溢出: back.scrollHeight - back.clientHeight,
                视口: window.innerHeight};
      })()""",
     # 长 detail 的卡必须比 470 高，并且完整装下不内部滚动。
     # 页面可以跟着变长，这正是把卡片拉长的目的
     lambda v: v.get("没有长卡") or (v["高度"] > 470 and v["溢出"] <= 0)),
)


def pick_cases(all_cases, keys):
    """按名字片段挑用例。挑中哪几条由名字决定，不靠顺序猜。

    单条跑时没有前置用例铺垫状态，所以自动把开头的复位用例带上。
    少了它，涉及筛选、编辑、翻面的用例会在别人的残留状态里跑，随机失败。"""
    if not keys:
        return all_cases
    chosen = [c for c in all_cases if any(k in c[0] for k in keys)]
    if not chosen:
        names = "\n  ".join(name for name, _, _ in all_cases)
        raise SystemExit(f"没有用例名字含 {keys}。可用名字：\n  {names}")
    reset = all_cases[0]
    if reset not in chosen:
        chosen = [reset] + chosen
    return chosen


def shot_name(keys):
    """跑全量时用固定名，挑着跑时带上名字，避免单条运行的截图盖掉全量的那份"""
    if not keys:
        return "验收_自动检查.png"
    safe = "_".join(k.replace("/", "或").replace(" ", "") for k in keys)
    return f"验收_挑跑_{safe}.png"


def main():
    # 删除用例会真删卡并改写 data/cards.json，必须先在临时副本上跑，跑完丢弃。
    # 早期版本直接跑真实目录，跑一次少两张卡，是脚本本身的缺陷。
    ap = argparse.ArgumentParser(
        description="浏览器验收。不带参数跑全部用例",
        epilog="例：.venv/bin/python tests/browser_check.py --case 删除")
    ap.add_argument("--case", action="append", default=[], metavar="关键词",
                    help="只跑名字含这个词的用例，可重复。会自动带上开头的复位用例")
    ap.add_argument("--list", action="store_true", help="列出全部用例名后退出")
    args = ap.parse_args()

    if args.list:
        for name, _, _ in CASES:
            print(name)
        return 0

    cases = pick_cases(CASES, args.case)

    work = tempfile.mkdtemp(prefix="browser_check_")
    os.makedirs(os.path.join(work, "data"), exist_ok=True)
    shutil.copy(os.path.join(BASE, "data", "cards.json"),
                os.path.join(work, "data", "cards.json"))
    for item in ("server.py", "web"):
        src = os.path.join(BASE, item)
        dst = os.path.join(work, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)

    port = free_port()
    env = dict(os.environ, FLASH_PORT=str(port))
    log = open("/tmp/browser_check_server.log", "w")
    srv = subprocess.Popen([PY, "server.py"], cwd=work, env=env,
                           stdout=log, stderr=subprocess.STDOUT)
    _procs.append(srv)
    if not wait_ready(f"http://127.0.0.1:{port}/api/progress"):
        print("服务未就绪，进程已退出则日志如下")
        if srv.poll() is not None:
            print(f"  服务退出码 {srv.returncode}")
            with open("/tmp/browser_check_server.log") as f:
                print("  " + f.read()[:800].replace("\n", "\n  "))
        log.close()
        shutil.rmtree(work, ignore_errors=True)
        return 1

    ch = Chrome()
    ch.start(f"http://127.0.0.1:{port}")
    print(f"服务端口 {port}　Chrome 调试端口 {CDP_PORT}")
    if args.case:
        print(f"挑跑 {len(cases)} / 共 {len(CASES)} 项，关键词 {'、'.join(args.case)}，"
              f"已自动带上开头的复位用例")
    print("=" * 60)

    failed = 0
    for name, expr, check in cases:
        try:
            val = ch.js(expr)
            ok = bool(check(val))
            flag = "通过" if ok else "不通过"
            if not ok:
                failed += 1
            print(f"[{flag}] {name}")
            print(f"        {json.dumps(val, ensure_ascii=False)}")
        except Exception as e:
            failed += 1
            print(f"[出错] {name}　{type(e).__name__}: {e}")

    print("=" * 60)
    shot = ch.shot(shot_name(args.case))
    print(f"截图 {shot}")
    print(f"用例 {len(cases)} 项，异常 {failed} 项")

    ch.stop()
    cleanup()
    return 1 if failed else 0


CASES.append(
    ("侧边栏可折叠", """
      (async () => {
        const shell = document.querySelector('.shell');
        const btn = document.getElementById('sideToggle');
        if (shell.classList.contains('side-collapsed')) {
          btn.click(); await new Promise(r => setTimeout(r, 200));
        }
        const before = !shell.classList.contains('side-collapsed');
        btn.click();
        await new Promise(r => setTimeout(r, 250));
        const collapsed = shell.classList.contains('side-collapsed')
          && getComputedStyle(document.querySelector('.side')).display === 'none'
          && !!document.getElementById('sideToggle');
        btn.click();
        await new Promise(r => setTimeout(r, 250));
        return {折叠前展开: before, 折叠成功: collapsed,
                展开成功: !shell.classList.contains('side-collapsed'),
                按钮仍在: !!document.getElementById('sideToggle')};
      })()""",
     lambda v: v["折叠前展开"] and v["折叠成功"] and v["展开成功"] and v["按钮仍在"]),
)

if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        cleanup()
