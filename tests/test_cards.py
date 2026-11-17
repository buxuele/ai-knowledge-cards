"""知识点问答卡 接口与数据完整性测试"""

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import server  # noqa: E402

# 本机开着 Clash 系统代理，urllib 会把 127.0.0.1 的测试请求也发进代理，返回 502。测试必须直连。
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

def read_data():
    """每次读盘，不在模块加载时缓存。缓存会让数据改了测试却还拿旧值断言。"""
    with open(os.path.join(BASE, "data", "cards.json"), encoding="utf-8") as f:
        return json.load(f)


DATA = read_data()
CATS = {c["id"] for c in DATA["categories"]}


def first_id(get):
    """从接口取一张还没被动过的卡。

    硬编码卡片 id 有两个致命问题：归档用例会真删卡，删掉那张之后断言就撞车；
    卡库增删后 id 也会变。表现都是随机失败，排查成本极高。
    """
    cards = get("/api/cards")["cards"]
    if not cards:
        raise AssertionError("卡片库已空")
    return cards[0]["id"]


class TestData(unittest.TestCase):
    def test_ids_unique(self):
        ids = [c["id"] for c in DATA["cards"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cat_valid(self):
        for c in DATA["cards"]:
            self.assertIn(c["cat"], CATS, c["id"])

    def test_required_fields(self):
        for c in DATA["cards"]:
            for f in ("q", "a", "cat"):
                self.assertTrue(c.get(f), f"{c['id']} 缺 {f}")

    def test_level_valid(self):
        for c in DATA["cards"]:
            self.assertIn(c["level"], {"basic", "core", "practice", "edge"}, c["id"])

    def test_no_empty_category(self):
        """分类不许有零张卡的空标签，出现就说明迁移没做干净"""
        data = read_data()
        used = {c["cat"] for c in data["cards"]}
        for cat in data["categories"]:
            self.assertIn(cat["id"], used, f"分类 {cat['name']} 下一张卡都没有")
        self.assertEqual(len(data["categories"]), len(used))


class TestSM2(unittest.TestCase):
    def test_first_good_is_one_day(self):
        r = server.review({"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0}, 2)
        self.assertEqual(r["reps"], 1)
        self.assertEqual(r["interval"], 1)

    def test_second_good_is_three_day(self):
        r = server.review({"ease": 2.5, "interval": 1, "reps": 1, "lapses": 0}, 2)
        self.assertEqual(r["interval"], 3)

    def test_third_good_grows_by_ease(self):
        r = server.review({"ease": 2.5, "interval": 3, "reps": 2, "lapses": 0}, 2)
        self.assertEqual(r["interval"], 7.5)

    def test_fail_resets_reps_and_counts_lapse(self):
        r = server.review({"ease": 2.5, "interval": 30, "reps": 5, "lapses": 1}, 0)
        self.assertEqual(r["reps"], 0)
        self.assertEqual(r["interval"], 0)
        self.assertEqual(r["lapses"], 2)

    def test_fail_lowers_ease_but_floor(self):
        r = server.review({"ease": 1.4, "interval": 5, "reps": 2, "lapses": 0}, 0)
        self.assertGreaterEqual(r["ease"], 1.3)

    def test_vague_advances_not_reset(self):
        "有点印象是成功回忆，必须推进间隔。旧版本写 if q < 2 把它当失败清零"
        s = {"ease": 2.5, "interval": 8, "reps": 3, "lapses": 0}
        r = server.review(s, 1)
        self.assertEqual(r["interval"], 20)
        self.assertEqual(r["reps"], 4)
        self.assertEqual(r["lapses"], 0)

    def test_vague_lowers_ease_gently(self):
        r = server.review({"ease": 2.5, "interval": 5, "reps": 2, "lapses": 0}, 1)
        self.assertAlmostEqual(r["ease"], 2.45)

    def test_ease_capped_high(self):
        "ease 无上限增长会让间隔倍数失控，180 天上限形同虚设"
        s = {"ease": 2.5, "interval": 3, "reps": 1, "lapses": 0}
        for _ in range(40):
            s = server.review(s, 2)
        self.assertLessEqual(s["ease"], server.EASE_MAX)

    def test_ease_capped_low(self):
        s = {"ease": 1.35, "interval": 5, "reps": 2, "lapses": 0}
        r = server.review(s, 1)
        self.assertGreaterEqual(r["ease"], server.EASE_MIN)

    def test_success_raises_ease(self):
        r = server.review({"ease": 2.5, "interval": 5, "reps": 2, "lapses": 0}, 2)
        self.assertAlmostEqual(r["ease"], 2.6)

    def test_interval_capped(self):
        r = server.review({"ease": 2.5, "interval": 5000, "reps": 20, "lapses": 0}, 2)
        self.assertLessEqual(r["interval"], server.MAX_INTERVAL)

    def test_due_in_future_on_success(self):
        r = server.review({"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0}, 2)
        self.assertGreater(r["due"], time.time())


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        server.DB_PATH = cls.tmp.name
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.unlink(cls.tmp.name)

    def url(self, p):
        return f"http://127.0.0.1:{self.port}{p}"

    def get(self, p):
        return json.loads(urllib.request.urlopen(self.url(p), timeout=5).read())

    def post(self, p, body):
        req = urllib.request.Request(
            self.url(p), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def remaining_id(self):
        """运行时取卡 id。硬编码 id 会随卡片增删失效"""
        return first_id(self.get)

    def pick_card(self, pred, what):
        """按条件在运行时挑一张卡，挑不到就明说。

        硬编码 id 的两种死法：卡被归档后断言直接 404；
        或者挑到一张已被别的用例动过的卡，断言随机失败。
        需要「没答过的卡」「没收藏的卡」这类前提时，必须按条件现挑。
        """
        for c in self.get("/api/cards")["cards"]:
            if pred(c):
                return c["id"]
        raise AssertionError(f"卡库里没有{what}")

    def fresh_id(self):
        """一张没答过也没收藏的卡"""
        return self.pick_card(
            lambda c: not c["seen"] and c["reps"] == 0 and not c["marked"],
            "没答过也没收藏的卡")

    def test_cards_shape(self):
        data = read_data()
        d = self.get("/api/cards")
        self.assertEqual(len(d["cards"]), len(data["cards"]))
        self.assertEqual(len(d["categories"]), len(data["categories"]))
        for c in d["cards"]:
            for f in ("seen", "marked", "ease", "interval", "reps", "lapses", "due", "due_now"):
                self.assertIn(f, c, c["id"])

    def test_summary_present(self):
        s = self.get("/api/cards")["summary"]
        for f in ("total", "seen", "mastered", "marked", "due"):
            self.assertIn(f, s)
        self.assertEqual(s["total"], len(DATA["cards"]))

    def mastered(self):
        """全库已掌握数。这几条用例只比前后差值，不再假设跑之前全局是几"""
        return self.get("/api/cards")["summary"]["mastered"]

    def test_mastered_needs_three_clean_reps(self):
        """真掌握口径是连续答对三次且从未答错。只答一次不算，答错一次清零"""
        cid = self.fresh_id()
        before = self.mastered()
        # 答一次不够三次
        self.post("/api/answer", {"id": cid, "quality": 2})
        self.assertEqual(self.mastered(), before, "答一次就进掌握，口径太松")
        # 第二次之后卡已推到未来，继续答需要先把它拉回到期
        self.post("/api/answer", {"id": cid, "quality": 2})
        con = server.db()
        con.execute("UPDATE state SET due=0 WHERE card_id=?", (cid,))
        con.commit()
        con.close()
        self.post("/api/answer", {"id": cid, "quality": 2})
        con = server.db()
        row = con.execute("SELECT reps, lapses FROM state WHERE card_id=?",
                          (cid,)).fetchone()
        con.close()
        self.assertGreaterEqual(row[0], 3)
        self.assertEqual(self.mastered(), before + 1, "三次连对之后必须进掌握")

    def test_mastered_resets_on_lapse(self):
        """答错一次，已掌握的卡必须掉出统计，否则数字只涨不跌是假的"""
        cid = self.fresh_id()
        before = self.mastered()
        for _ in range(3):
            self.post("/api/answer", {"id": cid, "quality": 2})
            con = server.db()
            con.execute("UPDATE state SET due=0 WHERE card_id=?", (cid,))
            con.commit()
            con.close()
        con = server.db()
        row = con.execute("SELECT reps FROM state WHERE card_id=?", (cid,)).fetchone()
        con.close()
        self.assertGreaterEqual(row[0], 3)
        self.assertEqual(self.mastered(), before + 1, "三次连对之后必须进掌握")
        self.post("/api/answer", {"id": cid, "quality": 0})
        self.assertEqual(self.mastered(), before, "答错一次必须掉出来")

    def test_mastered_requires_zero_lapses(self):
        """直接构造 reps 够但答错过一次的行。

        只走接口测不到 lapses 这半个条件：review 遇到 q=0 会同时把 reps 清零，
        所以 reps>=3 单独就能把这类卡挡住，lapses 条件在这条路径上永远是冗余的，
        删掉它测试照样通过。要真验判定函数，只能直接写库。

        只比这张卡带来的增减，不假设全库此时是 0，
        否则前面哪条用例先掌握了别的卡，这里就随机失败。
        """
        cid = self.fresh_id()
        before = self.mastered()
        self.post("/api/answer", {"id": cid, "quality": 2})
        con = server.db()
        con.execute(
            "UPDATE state SET seen=1, reps=5, lapses=1, due=? WHERE card_id=?",
            (time.time() - 10, cid))
        con.commit()
        con.close()
        self.assertEqual(self.mastered(), before, "答错过的卡不该算掌握")

        con = server.db()
        con.execute("UPDATE state SET lapses=0 WHERE card_id=?", (cid,))
        con.commit()
        con.close()
        self.assertEqual(self.mastered(), before + 1, "只清掉 lapses 就该算掌握")


    def test_filter_by_cat(self):
        d = self.get("/api/cards?cat=net")
        self.assertTrue(d["cards"])
        for c in d["cards"]:
            self.assertEqual(c["cat"], "net")

    def test_mode_due_shrinks_set(self):
        all_c = self.get("/api/cards?cat=all")["cards"]
        due = self.get("/api/cards?cat=all&mode=due")["cards"]
        self.assertLessEqual(len(due), len(all_c))
        for c in due:
            self.assertTrue(c["due_now"])

    def test_grade_persists_schedule(self):
        cid = self.fresh_id()
        d = self.post("/api/answer", {"id": cid, "quality": 2})
        self.assertTrue(d["ok"])
        self.assertEqual(d["next"]["reps"], 1)
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["seen"], 1)
        self.assertFalse(c["due_now"])

    def test_due_mode_excludes_unseen(self):
        """全新卡 due_now 也是 true，混进到期复习会让开局等于全部卡片模式"""
        all_c = self.get("/api/cards?cat=all")["cards"]
        fresh = [c for c in all_c if not c["seen"]]
        self.assertTrue(fresh, "库里应有未学的卡")
        self.assertTrue(all(c["due_now"] for c in fresh))
        seen_ids = {c["id"] for c in self.get("/api/cards?cat=all&mode=due")["cards"]}
        for c in fresh:
            self.assertNotIn(c["id"], seen_ids)

    def test_fail_makes_due_now(self):
        cid = self.fresh_id()
        self.post("/api/answer", {"id": cid, "quality": 2})
        self.post("/api/answer", {"id": cid, "quality": 0})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertTrue(c["due_now"])
        self.assertEqual(c["lapses"], 1)

    def test_bad_quality_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/answer", {"id": self.remaining_id(), "quality": 9})
        self.assertEqual(e.exception.code, 400)

    def test_missing_id_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/answer", {"quality": 2})
        self.assertEqual(e.exception.code, 400)

    def test_marked_persists(self):
        cid = self.pick_card(lambda c: not c["marked"], "没被收藏的卡")
        self.post("/api/answer", {"id": cid, "quality": 2})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["marked"], 0)

    def test_grading_never_clears_mark(self):
        """前端早期在评分时回传 marked，两边对象不同步就把收藏静默清成 0"""
        cid = self.fresh_id()
        self.post("/api/mark", {"id": cid, "marked": 1})
        self.post("/api/answer", {"id": cid, "quality": 2})
        self.post("/api/answer", {"id": cid, "quality": 0})
        self.post("/api/answer", {"id": cid, "quality": 2})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["marked"], 1)

    def test_mark_endpoint_is_only_way_to_mark(self):
        cid = self.pick_card(lambda c: not c["marked"], "没被收藏的卡")
        self.post("/api/answer", {"id": cid, "quality": 2, "marked": 1})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["marked"], 0)

    def test_mark_endpoint_does_not_touch_schedule(self):
        cid = self.fresh_id()
        self.post("/api/mark", {"id": cid, "marked": 1})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["marked"], 1)
        self.assertEqual(c["seen"], 0)
        self.assertEqual(c["reps"], 0)
        self.assertTrue(c["due_now"])

    def test_mark_toggle_off(self):
        cid = self.remaining_id()
        self.post("/api/mark", {"id": cid, "marked": 1})
        self.post("/api/mark", {"id": cid, "marked": 0})
        c = next(x for x in self.get("/api/cards")["cards"] if x["id"] == cid)
        self.assertEqual(c["marked"], 0)

    def test_mark_missing_id(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/mark", {"marked": 1})
        self.assertEqual(e.exception.code, 400)

    def test_reset_clears(self):
        # 清空用例只验汇总归零，用哪张卡都一样，按运行时取，
        # 不再写死 ai-003 这种会被归档掉的 id
        self.post("/api/answer", {"id": self.remaining_id(), "quality": 2})
        req = urllib.request.Request(self.url("/api/reset"), method="DELETE")
        urllib.request.urlopen(req, timeout=5)
        s = self.get("/api/progress")["summary"]
        self.assertEqual(s["seen"], 0)
        self.assertEqual(s["marked"], 0)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url("/nope.html"), timeout=5)
        self.assertEqual(e.exception.code, 404)

    def test_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url("/../server.py"), timeout=5)
        self.assertEqual(e.exception.code, 404)

    def test_prefix_collision_dir_blocked(self):
        """同级目录 web_x 前缀和 web 相同，只比字符串前缀会放行"""
        sibling = os.path.join(server.BASE, "web_x")
        os.makedirs(sibling, exist_ok=True)
        with open(os.path.join(sibling, "leak.txt"), "w") as f:
            f.write("leaked")
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.url("/../web_x/leak.txt"), timeout=5)
            self.assertEqual(e.exception.code, 404)
        finally:
            import shutil
            shutil.rmtree(sibling)

    def test_non_allowlisted_extension_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url("/../data/cards.json"), timeout=5)
        self.assertEqual(e.exception.code, 404)

    def test_static_asset_served(self):
        r = urllib.request.urlopen(self.url("/app.js"), timeout=5)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")

    def test_icon_svg_served(self):
        r = urllib.request.urlopen(self.url("/icon.svg"), timeout=5)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Content-Type"), "image/svg+xml")

    def test_svg_locked_by_csp(self):
        """svg 能带脚本，走静态服务必须用 CSP 钉死为图片"""
        r = urllib.request.urlopen(self.url("/icon.svg"), timeout=5)
        self.assertIn("default-src 'none'", r.headers.get("Content-Security-Policy", ""))

    def test_favicon_ico_no_error(self):
        """浏览器无条件请求 ico，回 204 而非 404，避免控制台噪声"""
        r = urllib.request.urlopen(self.url("/favicon.ico"), timeout=5)
        self.assertEqual(r.status, 204)


class TestBadBody(unittest.TestCase):
    """非法请求体必须回 400。旧版会让 handler 抛异常，连接直接断掉，前端只看到 fetch 失败"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        server.DB_PATH = cls.tmp.name
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.unlink(cls.tmp.name)

    def raw(self, path, body, method="POST"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method,
            headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code

    def test_broken_json_400(self):
        self.assertEqual(self.raw("/api/answer", b"{not json"), 400)

    def test_non_object_json_400(self):
        self.assertEqual(self.raw("/api/answer", b"[1,2,3]"), 400)

    def test_string_quality_rejected(self):
        """旧版 int() 会把 "2" 静默转成 2，浮点 1.7 会截断成 1"""
        self.assertEqual(self.raw("/api/answer", b'{"id":"ai-001","quality":"2"}'), 400)
        self.assertEqual(self.raw("/api/answer", b'{"id":"ai-001","quality":1.7}'), 400)
        self.assertEqual(self.raw("/api/answer", b'{"id":"ai-001","quality":true}'), 400)

    def test_unknown_card_id_400(self):
        self.assertEqual(self.raw("/api/answer", b'{"id":"zzz-999","quality":2}'), 400)
        self.assertEqual(self.raw("/api/mark", b'{"id":"zzz-999","marked":1}'), 400)

    def test_bad_content_length_400(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request("POST", "/api/answer", body=b'{"id":"ai-001","quality":2}',
                      headers={"Content-Length": "abc"})
            self.assertEqual(c.getresponse().status, 400)
        finally:
            c.close()

    def test_oversized_body_400(self):
        self.assertEqual(self.raw("/api/answer", b"x" * (server.MAX_BODY + 1)), 400)

    def test_orphan_row_ignored_by_summary(self):
        """state 表里残留已删卡的孤儿行时，due 会算出负数"""
        con = server.db()
        con.execute("INSERT INTO state(card_id, seen, due) VALUES('gone-001', 1, 0)")
        con.commit()
        con.close()
        s = server.summary()
        self.assertGreaterEqual(s["due"], 0)
        self.assertEqual(s["total"], len(DATA["cards"]))
        con = server.db()
        con.execute("DELETE FROM state WHERE card_id='gone-001'")
        con.commit()
        con.close()


class TestArchive(unittest.TestCase):
    """删除的卡片必须进 data/archived/ 且携带删除时的排程状态"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(BASE, "data", "cards.json"), os.path.join(cls.dir, "cards.json"))
        # BASE 必须一起改，否则归档会写进真实 data/ 目录并删掉真卡片
        cls.orig_base = server.BASE
        cls.orig_db = server.DB_PATH
        cls.orig_archive = server.ARCHIVE
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.BASE = cls.orig_base
        server.CARDS_FILE = "data/cards.json"
        server.DB_PATH = cls.orig_db
        server.ARCHIVE = cls.orig_archive
        server._cards_cache["mtime"] = None
        shutil.rmtree(cls.dir)

    def url(self, p):
        return f"http://127.0.0.1:{self.port}{p}"

    def post(self, p, body):
        req = urllib.request.Request(
            self.url(p), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def get(self, p):
        return json.loads(urllib.request.urlopen(self.url(p), timeout=5).read())

    def delete(self, p):
        req = urllib.request.Request(self.url(p), method="DELETE")
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def remaining_id(self):
        """每次取一张还没被本用例删掉的卡。归档会真删卡，硬编码 id 会随卡片增删失效。"""
        return first_id(self.get)

    def test_archived_file_written(self):
        cid = self.remaining_id()
        self.post("/api/answer", {"id": cid, "quality": 2})
        d = self.post("/api/archive", {"id": cid, "reason": "known"})
        self.assertTrue(d["ok"])
        path = os.path.join(server.ARCHIVE, f"{cid}.json")
        self.assertTrue(os.path.isfile(path), "归档文件必须落盘")
        rec = json.load(open(path, encoding="utf-8"))
        self.assertEqual(rec["id"], cid)
        self.assertEqual(rec["reason"], "known")
        self.assertIn("q", rec)
        self.assertIn("a", rec)

    def test_archive_records_schedule_state(self):
        """存 SM-2 状态是为了后续判断删掉的是已掌握还是不该有的内容"""
        cid = self.remaining_id()
        self.post("/api/answer", {"id": cid, "quality": 2})
        self.post("/api/archive", {"id": cid, "reason": "known"})
        rec = json.load(open(os.path.join(server.ARCHIVE, f"{cid}.json"), encoding="utf-8"))
        st = rec["state_at_archive"]
        self.assertEqual(st["seen"], 1)
        self.assertEqual(st["reps"], 1)

    def test_card_removed_from_library(self):
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid, "reason": "trivial"})
        ids = {c["id"] for c in self.get("/api/cards")["cards"]}
        self.assertNotIn(cid, ids)

    def test_schedule_row_removed(self):
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid, "reason": "wrong"})
        con = server.db()
        n = con.execute("SELECT count(*) FROM state WHERE card_id=?", (cid,)).fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

    def test_summary_total_drops(self):
        before = self.get("/api/cards")["summary"]["total"]
        self.post("/api/archive", {"id": self.remaining_id(), "reason": "duplicate"})
        after = self.get("/api/cards")["summary"]["total"]
        self.assertEqual(after, before - 1)

    def test_archive_keeps_full_card(self):
        """归档存整张原卡，恢复时来源、主题与作者字段才不会丢"""
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid})
        rec = json.load(open(os.path.join(server.ARCHIVE, f"{cid}.json"), encoding="utf-8"))
        self.assertEqual(rec["card"]["id"], cid)
        self.assertIn("cat", rec["card"])

    def test_unarchive_restores_card_and_state(self):
        """撤销删除：卡片回库，删除时的排程状态也一起回来"""
        cid = self.remaining_id()
        self.post("/api/answer", {"id": cid, "quality": 2})
        before = self.get("/api/cards")["summary"]["total"]
        self.post("/api/archive", {"id": cid})
        self.assertEqual(self.get("/api/cards")["summary"]["total"], before - 1)

        d = self.post("/api/unarchive", {"id": cid})
        self.assertTrue(d["ok"])
        self.assertEqual(d["card"]["id"], cid)
        self.assertEqual(self.get("/api/cards")["summary"]["total"], before)
        ids = {c["id"] for c in self.get("/api/cards")["cards"]}
        self.assertIn(cid, ids)

        con = server.db()
        row = con.execute("SELECT seen, reps FROM state WHERE card_id=?", (cid,)).fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 1)

    def test_recycle_lists_archived_card(self):
        """删除的卡必须出现在回收站列表，带上题面、分类与删除时间"""
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid, "reason": "known"})
        d = self.get("/api/recycle")
        hit = [x for x in d["items"] if x["id"] == cid]
        self.assertEqual(len(hit), 1, "回收站里找不到刚删的卡")
        self.assertTrue(hit[0]["q"])
        self.assertTrue(hit[0]["cat"])
        self.assertTrue(hit[0]["archived_at"])
        self.assertEqual(d["total"], len(d["items"]))

    def test_recycle_removes_after_restore(self):
        """从回收站恢复后，这张卡不能再留在列表里"""
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid})
        before = self.get("/api/recycle")["items"]
        self.assertIn(cid, {x["id"] for x in before})
        self.post("/api/unarchive", {"id": cid})
        after = self.get("/api/recycle")["items"]
        self.assertNotIn(cid, {x["id"] for x in after})

    def test_clear_recycle(self):
        """清空回收站后列表与计数都归零"""
        self.post("/api/archive", {"id": self.remaining_id()})
        self.assertGreaterEqual(self.get("/api/recycle")["total"], 1)
        d = self.delete("/api/recycle")
        self.assertTrue(d["ok"])
        self.assertGreaterEqual(d["cleared"], 1)
        self.assertEqual(self.get("/api/recycle")["total"], 0)
        self.assertEqual(self.get("/api/sidebar")["recycle"], 0)

    def test_sidebar_reports_recycle_count(self):
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid})
        d = self.get("/api/sidebar")
        self.assertGreaterEqual(d["recycle"], 1)

    def test_unarchive_twice_is_400(self):
        cid = self.remaining_id()
        self.post("/api/archive", {"id": cid})
        self.post("/api/unarchive", {"id": cid})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/unarchive", {"id": cid})
        self.assertEqual(cm.exception.code, 400)

    def test_unarchive_missing_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/unarchive", {"id": "nonexistent-card"})
        self.assertEqual(cm.exception.code, 400)

    def test_static_etag_returns_304(self):
        """静态资源带 ETag，第二次请求回 304 而不是整包重下"""
        req = urllib.request.Request(self.url("/style.css"))
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            etag = r.headers.get("ETag")
            self.assertTrue(etag, "静态资源必须带 ETag")
        req2 = urllib.request.Request(self.url("/style.css"),
                                      headers={"If-None-Match": etag})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req2, timeout=5)
        self.assertEqual(cm.exception.code, 304)

    def test_bad_reason_falls_back_to_infer(self):
        """前端不再选原因，服务端按排程状态推断。非法值同样走推断而不是报错"""
        d = self.post("/api/archive", {"id": self.remaining_id(), "reason": "随便写"})
        self.assertTrue(d["ok"])
        self.assertIn(d["archived"]["reason"], server.REASONS)

    def test_reason_omitted_infers_from_state(self):
        """未传 reason 时按删除那一刻的状态推断。id 运行时取，避免硬编码随卡片增删失效"""
        cid = self.remaining_id()
        d = self.post("/api/archive", {"id": cid, "reason": None})
        rec = d["archived"]
        self.assertEqual(rec["state_at_archive"]["seen"], 0)
        self.assertEqual(rec["reason"], "unrelated")

    def test_infer_known_when_reps_high(self):
        self.assertEqual(
            server.infer_reason(dict(seen=1, reps=5, lapses=0)), "known")

    def test_infer_wrong_when_lapses_high(self):
        self.assertEqual(
            server.infer_reason(dict(seen=1, reps=0, lapses=4)), "wrong")

    def test_infer_unrelated_when_unseen(self):
        self.assertEqual(
            server.infer_reason(dict(seen=0, reps=0, lapses=0)), "unrelated")

    def test_infer_trivial_otherwise(self):
        self.assertEqual(
            server.infer_reason(dict(seen=1, reps=1, lapses=0)), "trivial")

    def test_unknown_id_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/archive", {"id": "zzz-001", "reason": "known"})
        self.assertEqual(e.exception.code, 400)

    def test_every_reason_accepted(self):
        """显式传入每个合法 reason 都应通过，便于将来补写归档原因"""
        for r in server.REASONS:
            d = self.post("/api/archive", {"id": self.remaining_id(), "reason": r})
            self.assertTrue(d["ok"])
            self.assertEqual(d["archived"]["reason"], r)


class TestEmptyCategory(unittest.TestCase):
    """最后一卡被删光的分类要自动消失，从回收站恢复时再补回来"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.data = {
            "categories": [
                {"id": "ai", "name": "AI 技术", "summary": ""},
                {"id": "java", "name": "Java 基础", "summary": ""},
            ],
            "cards": [
                {"id": "ai-keep", "cat": "ai", "level": "basic",
                 "q": "保留的问题？", "a": "保留。", "detail": "例如保留。"},
                {"id": "java-gone", "cat": "java", "level": "basic",
                 "q": "会被删的问题？", "a": "会删。", "detail": "例如会删。"},
            ],
        }
        with open(os.path.join(cls.dir, "cards.json"), "w", encoding="utf-8") as f:
            json.dump(cls.data, f, ensure_ascii=False)
        cls.orig = (server.BASE, server.CARDS_FILE, server.DB_PATH,
                    server.ARCHIVE, server._cards_cache["mtime"])
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        server.db().close()

    @classmethod
    def tearDownClass(cls):
        server.BASE, server.CARDS_FILE = cls.orig[0], cls.orig[1]
        server.DB_PATH, server.ARCHIVE = cls.orig[2], cls.orig[3]
        server._cards_cache["mtime"] = cls.orig[4]
        shutil.rmtree(cls.dir)

    def test_last_card_removes_category_and_restore_adds_it_back(self):
        server.archive_card("java-gone", "trivial")
        ids = {c["id"] for c in server.load_cards()["categories"]}
        self.assertNotIn("java", ids, "0 张的分类没有删掉")
        server.unarchive_card("java-gone")
        ids = {c["id"] for c in server.load_cards()["categories"]}
        self.assertIn("java", ids, "恢复卡片时分类没有补回来")


class TestDeleteCategory(unittest.TestCase):
    """右键删除分类：卡片全部进回收站，分类移除，恢复时分类补回"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.data = {
            "categories": [
                {"id": "ai", "name": "AI 技术", "summary": ""},
                {"id": "java", "name": "Java 基础", "summary": ""},
            ],
            "cards": [
                {"id": "ai-keep", "cat": "ai", "level": "basic",
                 "q": "保留的问题？", "a": "保留。", "detail": "例如保留。"},
                {"id": "java-001", "cat": "java", "level": "basic",
                 "q": "删除分类里的第一题？", "a": "第一。", "detail": "例如第一。"},
                {"id": "java-002", "cat": "java", "level": "core",
                 "q": "删除分类里的第二题？", "a": "第二。", "detail": "例如第二。"},
            ],
        }
        with open(os.path.join(cls.dir, "cards.json"), "w", encoding="utf-8") as f:
            json.dump(cls.data, f, ensure_ascii=False)
        cls.orig = (server.BASE, server.CARDS_FILE, server.DB_PATH,
                    server.ARCHIVE, server._cards_cache["mtime"])
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        con = server.db()
        con.execute(
            "INSERT INTO state(card_id, seen, marked, ease, interval, reps, lapses, due) "
            "VALUES('java-001',1,0,2.5,1,1,0,0)")
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        server.BASE, server.CARDS_FILE = cls.orig[0], cls.orig[1]
        server.DB_PATH, server.ARCHIVE = cls.orig[2], cls.orig[3]
        server._cards_cache["mtime"] = cls.orig[4]
        shutil.rmtree(cls.dir)

    def test_delete_category_archives_cards_and_removes_category(self):
        d = server.delete_category("java")
        self.assertEqual(d["deleted"], 2)
        data = server.load_cards()
        self.assertNotIn("java", {c["id"] for c in data["categories"]})
        self.assertFalse([c for c in data["cards"] if c["cat"] == "java"])
        self.assertEqual(server.archive_count(), 2)
        con = server.db()
        n = con.execute("SELECT count(*) FROM state WHERE card_id LIKE 'java-%'").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

        # 从回收站恢复一张，分类必须补回来
        server.unarchive_card("java-001")
        data = server.load_cards()
        self.assertIn("java", {c["id"] for c in data["categories"]})
        self.assertTrue([c for c in data["cards"] if c["id"] == "java-001"])


class TestSnapshot(unittest.TestCase):
    """清空进度改为可撤销而不是二次确认，快照与恢复必须成对可用"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(BASE, "data", "cards.json"), os.path.join(cls.dir, "cards.json"))
        cls.orig_base, cls.orig_db, cls.orig_archive = server.BASE, server.DB_PATH, server.ARCHIVE
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.BASE, server.CARDS_FILE = cls.orig_base, "data/cards.json"
        server.DB_PATH, server.ARCHIVE = cls.orig_db, cls.orig_archive
        server._cards_cache["mtime"] = None
        shutil.rmtree(cls.dir)

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def post(self, p, body):
        return self.call("POST", p, body)

    def live_id(self):
        """运行时取一张真实存在的卡。写死 ai-001 会在卡被删后随机失败"""
        return self.call("GET", "/api/cards")["cards"][0]["id"]

    def test_snapshot_returns_rows(self):
        self.post("/api/answer", {"id": self.live_id(), "quality": 2})
        rows = self.call("GET", "/api/snapshot")["rows"]
        self.assertTrue(rows)
        self.assertIn("card_id", rows[0])
        self.assertIn("reps", rows[0])

    def test_reset_then_restore(self):
        self.post("/api/answer", {"id": self.live_id(), "quality": 2})
        before = self.call("GET", "/api/progress")["summary"]
        self.assertGreaterEqual(before["seen"], 1)
        rows = self.call("GET", "/api/snapshot")["rows"]

        self.call("DELETE", "/api/reset")
        cleared = self.call("GET", "/api/progress")["summary"]
        self.assertEqual(cleared["seen"], 0)

        self.call("DELETE", "/api/restore", rows)
        after = self.call("GET", "/api/progress")["summary"]
        self.assertEqual(after["seen"], before["seen"])
        self.assertEqual(after["marked"], before["marked"])

    def test_restore_empty_is_safe(self):
        self.call("DELETE", "/api/restore", [])
        s = self.call("GET", "/api/progress")["summary"]
        self.assertEqual(s["seen"], 0)

    def test_bad_restore_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("DELETE", "/api/nope")
        self.assertEqual(e.exception.code, 404)


class TestOpenApi(unittest.TestCase):
    """对外只读接口。不带复习状态，只返回题面与溯源"""

    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, p):
        return json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{p}", timeout=8).read())

    def test_no_schedule_fields_leaked(self):
        """对外接口不能带复习状态，那属于用户私有数据"""
        d = self.get("/api/open/cards?page_size=200")
        for c in d["cards"]:
            for f in ("seen", "marked", "ease", "reps", "lapses", "due", "due_now", "due_in"):
                self.assertNotIn(f, c, f"{f} 不该出现在对外接口")

    def test_no_summary_leaked(self):
        d = self.get("/api/open/cards")
        self.assertNotIn("summary", d)

    def test_basic_shape(self):
        d = self.get("/api/open/cards")
        for f in ("total", "page", "page_size", "pages", "categories", "levels", "cards"):
            self.assertIn(f, d)
        c = d["cards"][0]
        for f in ("id", "cat", "level", "q", "a", "detail", "source", "source_url"):
            self.assertIn(f, c)

    def test_pagination_covers_all(self):
        total = self.get("/api/open/cards")["total"]
        size = 50
        seen = []
        page = 1
        while True:
            d = self.get(f"/api/open/cards?page={page}&page_size={size}")
            seen += [c["id"] for c in d["cards"]]
            if page >= d["pages"]:
                break
            page += 1
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total, "分页结果不应重复")

    def test_page_size_capped(self):
        d = self.get("/api/open/cards?page_size=9999")
        self.assertLessEqual(d["page_size"], server.MAX_PAGE_SIZE)

    def test_bad_page_size_400(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/open/cards?page=abc")
        self.assertEqual(e.exception.code, 400)

    def test_filter_by_cat(self):
        d = self.get("/api/open/cards?cat=ai")
        self.assertTrue(d["cards"])
        for c in d["cards"]:
            self.assertEqual(c["cat"], "ai")

    def test_filter_by_level(self):
        d = self.get("/api/open/cards?level=practice")
        self.assertTrue(d["cards"])
        for c in d["cards"]:
            self.assertEqual(c["level"], "practice")

    def test_filter_by_theme(self):
        d = self.get("/api/open/cards?theme=foundations")
        self.assertTrue(d["cards"])
        for c in d["cards"]:
            self.assertEqual(c["theme"], "foundations")

    def test_filter_by_keyword(self):
        d = self.get("/api/open/cards?q=%E6%8F%90%E7%A4%BA%E8%AF%8D")
        self.assertTrue(d["cards"])

    def test_combined_filters(self):
        d = self.get("/api/open/cards?cat=ai&level=practice&page_size=200")
        for c in d["cards"]:
            self.assertEqual(c["cat"], "ai")
            self.assertEqual(c["level"], "practice")

    def test_forge_cards_imported(self):
        d = self.get("/api/open/cards?theme=foundations&page_size=200")
        # 用户会删卡，写死 30 会误报。只钉住还有 foundations 卡且都带 source_url
        self.assertGreater(d["total"], 0)
        self.assertTrue(all(c["source_url"] for c in d["cards"]))

    def test_read_only_rejects_post(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/open/cards",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 405)

    def test_read_only_rejects_delete(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/open/cards", method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 405)


class TestExplain(unittest.TestCase):
    """解释接口。两个模型都不可用时必须降级，绝不能让界面干等着。"""

    def setUp(self):
        self._saved = server._llm_ready
        server._llm_ready = None
        server._llm_lock = threading.Lock()

    def tearDown(self):
        server._llm_ready = self._saved

    def test_missing_question_rejected(self):
        with self.assertRaises(server.BadBody):
            server.explain_card("", "答案")

    def test_prompt_asks_for_three_sections(self):
        """提示词改过一次。原版要求「200 到 400 字、用自然段」，实测拿到 586 字
        一大坨：字数上限被无视，「自然段」被理解成「不用 markdown」，
        连一个换行都没有，例子埋在中段。现在每段派了任务与字数"""
        p = server.EXPLAIN_PROMPT
        self.assertIn("第一段", p)
        self.assertIn("第二段", p)
        self.assertIn("第三段", p)
        self.assertIn("例如", p)
        self.assertIn("否则", p)
        self.assertIn("180", p)
        self.assertNotIn("200 到 400", p)
        # 后来补的两条：说人话，第二段必须落到具体的人事或数字，
        # 不许再说「这样做能提升效率」这种没画面的道理
        self.assertIn("日常词", p)
        self.assertIn("具体", p)
        self.assertIn("大白话", p)

    def test_ensure_breaks_fixes_flat_text(self):
        """模型偶尔仍把三段连成一句。实测五次里有一次零换行，靠这个兜底"""
        flat = "该记事实。例如，用户问天气时应存事实而非对话。否则，只存对话无法复用知识。"
        out = server._ensure_breaks(flat)
        self.assertGreaterEqual(out.count("\n"), 2, "没有补出分段")
        self.assertEqual(out.replace("\n", ""), flat, "补换行时改了正文")

    def test_ensure_breaks_leaves_good_text_alone(self):
        """已经分好段的一个字都不能动"""
        good = "结论。\n例如，一个例子。\n否则，会出问题。"
        self.assertEqual(server._ensure_breaks(good), good)

    def test_ensure_breaks_tolerates_extra_newlines(self):
        """空行分段是模型更常见的写法，有换行就该直接放行"""
        spread = "结论。\n\n例如，一个例子。\n\n否则，会出问题。"
        self.assertEqual(server._ensure_breaks(spread), spread)

    def test_falls_back_when_all_models_unavailable(self):
        """密钥配了但全失败时，返回可复制的文本，前端据此复制到剪贴板"""
        os.environ["NVIDIA_API_KEY"] = "bogus"
        os.environ["OPENROUTER_API_KEY"] = "bogus"
        d = server.explain_card("什么是幂等性？", "重复执行效果相同。")
        self.assertFalse(d["ok"])
        self.assertIn("幂等性", d["fallback"])
        self.assertIn("请深入解释", d["fallback"])

    def test_falls_back_when_no_provider_available(self):
        """一个 provider 都不可用时必须降级，不能抛异常。

        不能靠清空环境变量来触发。server.load_env 用 setdefault 从 .env 补密钥，
        just_run.sh 不加载 .env 所以服务端必须自己读，pop 掉立刻会被填回来。
        直接把 provider 列表置空，测的才是真正的降级分支。
        """
        server._llm_ready = (object(), [])
        d = server.explain_card("什么是 CAP？", "三选二。")
        self.assertFalse(d["ok"])
        self.assertEqual(d["reason"], "llm 模块加载失败")
        self.assertIn("CAP", d["fallback"])

    def test_module_load_failure_degrades(self):
        """模块加载异常也必须降级返回，不能把异常抛给前端"""
        server._llm_ready = (None, [])
        d = server.explain_card("什么是背压？", "消费慢时降低生产速度。")
        self.assertFalse(d["ok"])
        self.assertTrue(d["fallback"])

    def test_budget_is_bounded(self):
        """总预算必须存在，否则两个模型各超时一次就是几分钟，按钮像死了"""
        self.assertLess(server.EXPLAIN_BUDGET, 120)
        self.assertLessEqual(server.EXPLAIN_TIMEOUT, server.EXPLAIN_BUDGET)
        # 最坏情况：provider 数量 × 单次超时，必须小于总预算
        worst = 2 * server.EXPLAIN_TIMEOUT
        self.assertLessEqual(worst, server.EXPLAIN_BUDGET + 30)

    def test_explain_is_post_only(self):
        srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/explain", method="GET")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


class TestSidebar(unittest.TestCase):
    """侧边栏与前端路由。分类张数必须自洽，路由不能把 404 变成 200。"""

    def setUp(self):
        self.cards = server.load_cards()["cards"]

    def test_ai_subs_cover_all_ai_cards(self):
        """AI 子类张数合计必须等于 AI 卡总数。不相等说明有卡没归类或有重复归类。"""
        tree = server.sidebar_tree(self.cards)
        ai = [g for g in tree if g["id"] == "ai"][0]
        sub_total = sum(c["count"] for c in ai["children"])
        ai_cards = [c for c in self.cards if c["cat"] == "ai"]
        self.assertEqual(ai["count"], len(ai_cards))
        self.assertEqual(sub_total, len(ai_cards))

    def test_non_ai_has_no_children(self):
        tree = server.sidebar_tree(self.cards)
        for g in tree:
            if g["id"] != "ai":
                self.assertEqual(g["children"], [], f"{g['id']} 不该有子类")

    def test_each_card_gets_exactly_one_sub(self):
        """逐张验 sub_of 返回值合法，且 AI 卡绝不能返回空"""
        for c in self.cards:
            if c["cat"] == "ai":
                self.assertIn(c and server.sub_of(c), server.AI_SUB_ORDER,
                              f"{c['id']} 没归到任何子类")
            else:
                self.assertEqual(server.sub_of(c), "", f"{c['id']} 不是 AI 卡却有子类")

    def test_spa_route_whitelist(self):
        """白名单外的路径必须 404。任意回落会让不存在的地址拿到 200"""
        for p in ("/", "/all", "/net", "/db", "/misc", "/java", "/spring",
                  "/vue", "/redis", "/micro", "/dist", "/books", "/recycle",
                  "/ai", "/ai/memory", "/ai/rag"):
            self.assertTrue(server.is_spa_route(p), p)
        for p in ("/bogus", "/ai/bogus", "/net/x", "/ai/memory/extra", "/../x"):
            self.assertFalse(server.is_spa_route(p), p)


class TestSidebarApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        server.DB_PATH = cls.tmp.name
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.unlink(cls.tmp.name)

    def get(self, p):
        o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = o.open(f"http://127.0.0.1:{self.port}{p}", timeout=8)
        return r.status, r.read(), r.headers.get("Content-Type")

    def test_sidebar_endpoint(self):
        code, body, ctype = self.get("/api/sidebar")
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        d = json.loads(body)
        ids = [g["id"] for g in d["groups"]]
        self.assertIn("ai", ids)
        self.assertEqual(ids[0], "ai", "AI 必须排第一个")
        for g in d["groups"]:
            self.assertTrue(g["name"], "分组必须有中文名")

    def test_sidebar_has_no_duplicate_groups(self):
        """早期版本在循环里每张卡 append 一次，240 张卡产出 240 个重复分组"""
        d = json.loads(self.get("/api/sidebar")[1])
        ids = [g["id"] for g in d["groups"]]
        self.assertEqual(len(ids), len(set(ids)), "分组出现重复")

    def test_spa_routes_return_html(self):
        for p in ("/", "/all", "/net", "/db", "/java", "/spring", "/vue",
                  "/redis", "/micro", "/dist", "/books", "/recycle", "/ai/memory"):
            code, body, ctype = self.get(p)
            self.assertEqual(code, 200, p)
            self.assertIn("text/html", ctype, p)
            self.assertIn(b"app.js", body, p)

    def test_unknown_route_is_404(self):
        o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for p in ("/bogus", "/ai/bogus", "/ai/memory/extra"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                o.open(f"http://127.0.0.1:{self.port}{p}", timeout=5)
            self.assertEqual(e.exception.code, 404, p)

    def test_sub_filter_returns_only_that_sub(self):
        """sub 参数必须真的过滤。全部返回就是没生效"""
        o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        total = 0
        for s in server.AI_SUB_ORDER:
            d = json.loads(o.open(
                f"http://127.0.0.1:{self.port}/api/cards?cat=ai&sub={s}",
                timeout=8).read())
            # 用户可能把某个子类的卡全删了，空子类允许存在；有卡时必须过滤正确
            if not d["cards"]:
                continue
            for c in d["cards"]:
                self.assertEqual(c.get("sub"), s, c["id"])
            total += len(d["cards"])
        ai_all = json.loads(o.open(
            f"http://127.0.0.1:{self.port}/api/cards?cat=ai", timeout=8).read())
        self.assertEqual(total, len(ai_all["cards"]),
                         "各子类之和必须等于 AI 全部，否则有卡被漏掉")


class TestEdit(unittest.TestCase):
    """编辑接口。全部跑在 cards.json 的临时副本上，绝不动真实卡库。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(BASE, "data", "cards.json"),
                    os.path.join(cls.dir, "cards.json"))
        cls.orig_base, cls.orig_db = server.BASE, server.DB_PATH
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server._cards_cache["mtime"] = None
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.BASE, server.CARDS_FILE = cls.orig_base, "data/cards.json"
        server.DB_PATH = cls.orig_db
        server._cards_cache["mtime"] = None
        shutil.rmtree(cls.dir)

    def post(self, body, path="/api/edit"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=8)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                # 404 这类是 text/plain 的 not found，不是 JSON，
                # 按 JSON 解析会把服务端的正常行为误报成测试崩溃
                return e.code, {"raw": raw.decode("utf-8", "replace")}

    def cards(self):
        server._cards_cache["mtime"] = None
        return server.load_cards()["cards"]

    def pick(self, sub):
        """运行时挑卡，不写死 id。写死的 id 会随卡库增删失效且报错随机"""
        for c in self.cards():
            if c["cat"] == "ai" and server.sub_of(c) == sub:
                return c
        raise AssertionError(f"库里没有 {sub} 类的卡，改用别的子类")

    def test_rewrite_q_and_a(self):
        c = self.pick("memory")
        code, d = self.post({"id": c["id"], "q": "记忆系统该记对话，还是该记事实？",
                             "a": "该记事实。"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        hit = [x for x in self.cards() if x["id"] == c["id"]][0]
        self.assertEqual(hit["q"], "记忆系统该记对话，还是该记事实？")
        self.assertEqual(hit["a"], "该记事实。")

    def test_edit_only_touches_q_and_a(self):
        """detail 是补全摘要、source 是导入来源、level 是层级，都与文本编辑无关"""
        c = self.pick("rag")
        before = dict(c)
        new_a = c["a"] + "（改过答案）"
        self.post({"id": c["id"], "q": c["q"] + "？", "a": new_a})
        after = [x for x in self.cards() if x["id"] == c["id"]][0]
        for field in ("cat", "level", "source", "detail", "id"):
            self.assertEqual(after.get(field), before.get(field),
                             f"{field} 被编辑接口动了")
        self.assertEqual(after["q"], before["q"] + "？")
        self.assertEqual(after["a"], new_a)

    def test_edit_keeps_review_state(self):
        """改文本不该顺手清掉排程。清了用户会从头再复习一遍"""
        c = self.pick("eval")
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/answer", method="POST",
            data=json.dumps({"id": c["id"], "quality": 2}).encode(),
            headers={"Content-Type": "application/json"}), timeout=8)
        seen_before = server.summary()["seen"]
        self.assertGreaterEqual(seen_before, 1)

        self.post({"id": c["id"], "q": c["q"] + "（改）", "a": c["a"]})
        self.assertEqual(server.summary()["seen"], seen_before,
                         "编辑文本把复习进度清了")

    def test_edit_changes_sub_category(self):
        """子分类由问题文本推导，改了问题就该换类，这是规则的正常行为"""
        c = self.pick("memory")
        orig_q = c["q"]
        self.addCleanup(self.post, {"id": c["id"], "q": orig_q, "a": c["a"]})
        self.post({"id": c["id"], "q": "提示注入是什么？", "a": c["a"]})
        hit = [x for x in self.cards() if x["id"] == c["id"]][0]
        self.assertEqual(server.sub_of(hit), "security")

    def test_add_card(self):
        """新增接口要落盘，id 按分类续号，分类表不能缺"""
        before = len(self.cards())
        code, d = self.post({
            "cat": "java", "q": "新增的问题？", "a": "新增的答案。",
            "detail": "解释。\n例如一个例子。", "level": "basic",
        }, path="/api/add")
        self.assertEqual(code, 200)
        card = d["card"]
        self.assertTrue(card["id"].startswith("java-"))
        self.assertEqual(card["cat"], "java")
        self.assertEqual(len(self.cards()), before + 1)
        self.assertIn("java", {c["id"] for c in server.ensure_categories(server.load_cards())})

    def test_add_rejects_bad_fields(self):
        for body, msg in (
            ({"cat": "nope", "q": "x", "a": "y"}, "分类不存在"),
            ({"cat": "java", "q": "  ", "a": "y"}, "问题不能为空"),
            ({"cat": "java", "q": "x", "a": " "}, "答案不能为空"),
            ({"cat": "java", "q": "x", "a": "y", "level": "hard"}, "难度档不存在"),
        ):
            code, d = self.post(body, path="/api/add")
            self.assertEqual(code, 400, body)
            self.assertIn(msg, d.get("error", ""))

    def test_validation_errors(self):
        c = self.pick("tool")
        cases = [
            ({"id": c["id"], "q": "   ", "a": "x"}, "问题不能为空"),
            ({"id": c["id"], "q": "x", "a": "  "}, "答案不能为空"),
            ({"q": "x", "a": "y"}, "缺少卡片 id"),
            ({"id": "nope-999", "q": "x", "a": "y"}, "卡片不存在"),
            ({"id": c["id"], "q": "字" * 301, "a": "y"}, "问题超出 300 字上限"),
            ({"id": c["id"], "q": "x", "a": "字" * 3001}, "答案超出 3000 字上限"),
        ]
        for body, want in cases:
            code, d = self.post(body)
            self.assertEqual(code, 400, want)
            self.assertEqual(d.get("error"), want)

    def test_non_string_fields_return_400_not_crash(self):
        """早期版本 id 传 123 会抛 AttributeError，被 socketserver 吞掉，
        客户端拿到的是连接被远端关闭而不是 400，完全没法判断哪儿错了"""
        for body in ({"id": 123, "q": "x", "a": "y"},
                     {"id": ["ai-001"], "q": "x", "a": "y"},
                     {"id": "ai-001", "q": 42, "a": "y"},
                     {"id": "ai-001", "q": "x", "a": {"n": 1}},
                     {"id": True, "q": "x", "a": "y"}):
            code, d = self.post(body)
            self.assertEqual(code, 400, f"{body} 没被拦下")
            self.assertIn("必须是文本", d.get("error", ""))

    def test_service_survives_bad_requests(self):
        """打几条非法请求后，正常请求必须照常服务"""
        for body in ({"id": 123, "q": "x", "a": "y"},
                     {"id": "ai-001", "q": 42, "a": "y"}):
            self.assertEqual(self.post(body)[0], 400)
        c = next(x for x in self.cards() if x["cat"] == "ai")
        self.assertEqual(self.post({"id": c["id"], "q": c["q"], "a": c["a"]})[0], 200)

    def test_unknown_edit_path_is_404(self):
        c = self.pick("agent")
        code, d = self.post({"id": c["id"], "q": "x", "a": "y"}, path="/api/edits")
        self.assertEqual(code, 404)
        self.assertNotIn("ok", d)

    def test_readonly_open_api_rejects_post(self):
        code, d = self.post({"id": "ai-001", "q": "x", "a": "y"},
                            path="/api/open/cards")
        self.assertEqual(code, 405)


class TestCardsSubField(unittest.TestCase):
    """回归测试。这个 bug 藏了两轮：sub 字段过去只有 /api/sidebar 顺手补进缓存，
    前端两条请求并发，谁先返回决定 /api/cards 带不带 sub。带不上就筛出空列表，
    卡片区显示「本轮完成」。必须在没调过 sidebar 的前提下验一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(BASE, "data", "cards.json"),
                    os.path.join(cls.dir, "cards.json"))
        cls.orig_base, cls.orig_db = server.BASE, server.DB_PATH
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server._cards_cache["mtime"] = None
        server.db().close()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.BASE, server.CARDS_FILE = cls.orig_base, "data/cards.json"
        server.DB_PATH = cls.orig_db
        server._cards_cache["mtime"] = None
        shutil.rmtree(cls.dir)

    def get(self, path):
        r = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=8)
        return json.loads(r.read())

    def test_cards_carries_sub_on_cold_cache(self):
        """冷缓存、只调 cards、没碰过 sidebar，每张卡仍要带 sub"""
        server._cards_cache["mtime"] = None
        d = self.get("/api/cards")
        self.assertTrue(d["cards"], "一张卡都没返回")
        缺 = [c["id"] for c in d["cards"] if "sub" not in c]
        self.assertEqual(缺, [], f"{len(缺)} 张卡缺 sub 字段")

    def test_sub_field_matches_the_rule(self):
        """带回来的 sub 必须等于服务端规则算出来的，不能是空串占位"""
        d = self.get("/api/cards?cat=ai")
        self.assertTrue(d["cards"])
        for c in d["cards"]:
            self.assertEqual(c["sub"], server.sub_of(c), c["id"])
            self.assertTrue(c["sub"], f"{c['id']} 的 sub 是空的")

    def test_sub_filter_agrees_with_field(self):
        """query 过滤出来的每张卡，字段必须与过滤条件一致"""
        for s in server.AI_SUB_ORDER:
            d = self.get(f"/api/cards?cat=ai&sub={s}")
            # 空子类可以存在，只要不是过滤失效把空结果错报成有卡
            if not d["cards"]:
                continue
            for c in d["cards"]:
                self.assertEqual(c["sub"], s, c["id"])

    def test_sidebar_does_not_mutate_cache(self):
        """sidebar 只该返回分组。给共享缓存打补丁正是当初那个竞态的来源"""
        server._cards_cache["mtime"] = None
        self.get("/api/sidebar")
        cached = server.load_cards()["cards"]
        脏 = [c["id"] for c in cached if "sub" in c]
        self.assertEqual(脏, [], f"sidebar 往缓存里写了 sub，污染了 {len(脏)} 张")


class TestIcons(unittest.TestCase):
    """图标全套必须由 scripts/gen_icons.py 生成，产物不许手搓"""

    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, p):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{p}", timeout=8)

    def test_all_icon_files_exist(self):
        for f in ("icon.svg", "favicon-32.png", "apple-touch-icon.png",
                  "icon-192.png", "icon-512.png", "site.webmanifest"):
            path = os.path.join(BASE, "web", f)
            self.assertTrue(os.path.isfile(path), f"{f} 必须存在，先跑 scripts/gen_icons.py")

    def test_icon_mime_types(self):
        want = {
            "icon.svg": "image/svg+xml",
            "favicon-32.png": "image/png",
            "apple-touch-icon.png": "image/png",
            "icon-192.png": "image/png",
            "icon-512.png": "image/png",
            "site.webmanifest": "application/manifest+json",
        }
        for path, mime in want.items():
            r = self.get("/" + path)
            self.assertEqual(r.status, 200, path)
            self.assertEqual(r.headers.get("Content-Type"), mime, path)

    def test_manifest_has_cors(self):
        """manifest 靠 fetch 读取，缺 CORS 头浏览器直接拒绝"""
        r = self.get("/site.webmanifest")
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_manifest_parses_and_has_icons(self):
        d = json.loads(self.get("/site.webmanifest").read())
        for f in ("name", "short_name", "start_url", "theme_color", "icons"):
            self.assertIn(f, d)
        sizes = {i["sizes"] for i in d["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)

    def test_png_magic_bytes(self):
        """确认是真 PNG 不是空壳文件"""
        for f in ("favicon-32.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png"):
            head = self.get("/" + f).read(8)
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", f)

    def test_index_references_all_icons(self):
        html = self.get("/").read().decode("utf-8")
        for token in ("icon.svg", "favicon-32.png", "apple-touch-icon.png",
                      "site.webmanifest", "theme-color"):
            self.assertIn(token, html, f"index.html 缺少 {token}")


class TestMergeIsolation(unittest.TestCase):
    """回归。merge 一度直接改 load_cards() 返回的缓存对象，而 edit_card 与
    归档又把这份缓存 dump 回 cards.json，于是任何一次编辑都会把当时全部卡片的
    SM-2 快照冻进题库文件。读路径反而一直正常，因为 merge 每次都覆盖这些字段，
    所以这个 bug 从来没有任何界面症状，只是 cards.json 越来越脏。

    这类 bug 用原库测不出来：现有 240 张本来就带着这些字段，分不清是渗漏还是
    本来就有。必须用只含基础字段的干净卡片测。
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cards = [{
            "id": "clean-001", "cat": "ai", "level": "basic",
            "q": "干净卡片的问题？", "a": "干净卡片的答案。",
            "detail": "干净卡片的详情。",
        }]
        with open(os.path.join(cls.dir, "cards.json"), "w", encoding="utf-8") as f:
            json.dump({"categories": [], "cards": cls.cards}, f, ensure_ascii=False)
        cls.orig = (server.BASE, server.CARDS_FILE, server.DB_PATH,
                    server.ARCHIVE, server._cards_cache["mtime"])
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = os.path.join(cls.dir, "progress.db")
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        con = server.db()
        con.execute(
            "INSERT INTO state(card_id, seen, marked, ease, interval, reps, lapses, due) "
            "VALUES('clean-001',1,1,2.7,36,5,1,?)", (time.time() + 999,))
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        server.BASE, server.CARDS_FILE = cls.orig[0], cls.orig[1]
        server.DB_PATH, server.ARCHIVE = cls.orig[2], cls.orig[3]
        server._cards_cache["mtime"] = cls.orig[4]
        shutil.rmtree(cls.dir)

    SRS = ("seen", "marked", "ease", "interval", "reps", "lapses", "due",
           "due_now", "due_in")

    def test_merge_returns_new_objects(self):
        merged = server.merge(server.load_cards()["cards"])
        self.assertEqual(sorted(merged[0].keys()),
                         sorted(list(self.cards[0].keys()) + list(self.SRS)))

    def test_merge_does_not_touch_cache(self):
        server.merge(server.load_cards()["cards"])
        cached = server.load_cards()["cards"][0]
        extra = [k for k in self.SRS if k in cached]
        self.assertEqual(extra, [], f"merge 污染了缓存 {extra}")

    def test_edit_does_not_write_srs_into_file(self):
        server.edit_card("clean-001", "改过的问题？", "改过的答案。")
        with open(os.path.join(self.dir, "cards.json"), encoding="utf-8") as f:
            hit = json.load(f)["cards"][0]
        leaked = [k for k in self.SRS if k in hit]
        self.assertEqual(leaked, [], f"编辑把排程状态写进了题库文件 {leaked}")

    def test_archive_does_not_write_srs_into_file(self):
        server.archive_card("clean-001", "known")
        with open(os.path.join(self.dir, "cards.json"), encoding="utf-8") as f:
            left = json.load(f)["cards"]
        self.assertEqual(left, [])

    def test_atomic_write_leaves_no_temp_file(self):
        before = set(os.listdir(self.dir))
        server.save_cards({"categories": [], "cards": self.cards})
        leftover = [x for x in set(os.listdir(self.dir)) - before
                    if x.endswith(".tmp")]
        self.assertEqual(leftover, [], "原子写残留了临时文件")

    def test_atomic_write_is_all_or_nothing(self):
        """写失败时必须留下原文件，且不留临时文件残骸。"""
        path = os.path.join(self.dir, "cards.json")
        original = open(path, encoding="utf-8").read()

        with self.assertRaises(TypeError):
            server.atomic_write_json(path, {"bad": object()})
        self.assertEqual(open(path, encoding="utf-8").read(), original,
                         "写失败把原文件改坏了")
        self.assertEqual([x for x in os.listdir(self.dir) if x.endswith(".tmp")], [],
                         "写失败留下了临时文件")


class TestRestoreValidation(unittest.TestCase):
    """回归。/api/restore 曾是全项目防护最薄的接口：Content-Length 不是整数、
    body 不是合法 JSON、行里缺字段，三种都变成连接被断开，用户只看到撤销没反应。

    非法 Content-Length 用 http.client 发不出来，客户端自己就抛 TypeError，
    请求根本到不了服务端，测的就不是服务端。这里用裸 socket 把畸形请求真打过去。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(BASE, "data", "cards.json"),
                    os.path.join(cls.dir, "cards.json"))
        cls.orig = (server.BASE, server.CARDS_FILE, server.DB_PATH, server.ARCHIVE)
        server.BASE = cls.dir
        server.CARDS_FILE = "cards.json"
        server.DB_PATH = cls.tmp.name
        server.ARCHIVE = os.path.join(cls.dir, "archived")
        server._cards_cache["mtime"] = None
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        server.db().close()
        # 不写死 ai-001，用户随时可能删卡
        cls.valid_card = server.load_cards()["cards"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.BASE, server.CARDS_FILE = cls.orig[0], cls.orig[1]
        server.DB_PATH, server.ARCHIVE = cls.orig[2], cls.orig[3]
        server._cards_cache["mtime"] = None
        os.unlink(cls.tmp.name)
        shutil.rmtree(cls.dir)

    def raw_delete(self, body, headers):
        head = "DELETE /api/restore HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        for k, v in headers.items():
            head += f"{k}: {v}\r\n"
        head += "Connection: close\r\n\r\n"
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(head.encode() + body)
            chunks = []
            while True:
                b = s.recv(4096)
                if not b:
                    break
                chunks.append(b)
            data = b"".join(chunks)
            status = int(data.split(b" ", 2)[1])
            payload = data.split(b"\r\n\r\n", 1)[-1]
            try:
                return status, json.loads(payload)
            except json.JSONDecodeError:
                return status, {}
        finally:
            s.close()

    def row(self, **over):
        base = {"card_id": self.valid_card, "seen": 1, "marked": 0, "ease": 2.5,
                "interval": 1, "reps": 1, "lapses": 0, "due": 0}
        base.update(over)
        return json.dumps([base]).encode()

    def send(self, body, headers=None):
        headers = headers or {"Content-Length": str(len(body))}
        return self.raw_delete(body, headers)

    def test_bad_content_length_is_400(self):
        code, _ = self.send(b"[]", {"Content-Length": "abc"})
        self.assertEqual(code, 400)

    def test_broken_json_is_400(self):
        code, _ = self.send(b"{not json", {"Content-Length": "9"})
        self.assertEqual(code, 400)

    def test_row_missing_field_is_400(self):
        code, _ = self.send(b'[{"card_id":"ai-001"}]')
        self.assertEqual(code, 400)

    def test_top_level_not_list_is_400(self):
        code, _ = self.send(b'{"rows":[]}')
        self.assertEqual(code, 400)

    def test_boolean_seen_rejected(self):
        code, _ = self.send(self.row(seen=True))
        self.assertEqual(code, 400)

    def test_ease_out_of_range_rejected(self):
        code, _ = self.send(self.row(ease=99))
        self.assertEqual(code, 400)

    def test_negative_interval_rejected(self):
        code, _ = self.send(self.row(interval=-5))
        self.assertEqual(code, 400)

    def test_negative_reps_rejected(self):
        code, _ = self.send(self.row(reps=-1))
        self.assertEqual(code, 400)

    def test_unknown_card_id_dropped_not_written(self):
        """快照期间卡可能被删。写回去的孤儿行读的时候会被过滤，但会一直堆在
        库里，所以恢复时就丢掉，并把数量回给前端。"""
        code, d = self.send(self.row(card_id="ghost-001"))
        self.assertEqual(code, 200)
        self.assertEqual(d["dropped"], 1)
        con = server.db()
        n = con.execute("SELECT count(*) FROM state WHERE card_id='ghost-001'").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

    def test_valid_rows_restored(self):
        code, d = self.send(self.row())
        self.assertEqual(code, 200)
        self.assertEqual(d["restored"], 1)
        con = server.db()
        row = con.execute("SELECT seen, reps FROM state WHERE card_id=?",
                          (self.valid_card,)).fetchone()
        con.close()
        self.assertEqual(row, (1, 1))


class TestExplainGate(unittest.TestCase):
    """回归。explain 曾用全局锁把整条链路锁 75 秒，第二次点击排在锁外，
    而它的预算时钟在等锁之前就开始走，拿到锁时预算已尽，只能回一句
    「所有模型都不可用或超时」。模型明明是好的，这句提示把人带错方向。

    改成非阻塞闸门：同时只放一个在飞，第二个立刻得到明确答复。
    """

    def setUp(self):
        self._saved = server._llm_ready
        server._llm_ready = (self.fake_mod(), [{
            "name": "fake", "key_env": "FAKE_KEY", "model": "m",
            "max_tokens": 2000,
        }])
        os.environ["FAKE_KEY"] = "x"

    def tearDown(self):
        # 只由主线程恢复。并发的两个线程若都跑 tearDown，后一个会把前一个
        # 还没读完的状态恢复掉，测出来就是偶发的假失败
        if threading.current_thread() is threading.main_thread():
            server._llm_ready = self._saved
        os.environ.pop("FAKE_KEY", None)

    @staticmethod
    def fake_mod():
        import types
        delay = 6.0

        class Client:
            timeout = 45

            def __init__(self):
                self.chat = types.SimpleNamespace(completions=self)

            def create(self, **kw):
                time.sleep(delay)
                text = ("核心结论写在这里，说得足够长以便通过长度门槛。"
                        "\n例如一个具体场景。\n否则会出问题。") * 3
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=text))])

        return types.SimpleNamespace(make_client=lambda p: Client(),
                                     clean=lambda s: s or "")

    def test_second_call_is_refused_not_queued(self):
        res = {}
        lock = threading.Lock()

        def call(n):
            t = time.time()
            d = server.explain_card("什么是幂等？", "重复执行效果相同。")
            with lock:
                res[n] = (round(time.time() - t, 1), d["ok"], d.get("reason", ""))

        a = threading.Thread(target=call, args=(1,))
        a.start()
        time.sleep(0.4)
        b = threading.Thread(target=call, args=(2,))
        b.start()
        a.join()
        b.join()

        # 按启动顺序判断，不能按耗时排序。被闸门挡下的那个耗时接近 0，
        # 一排序就被排到最前面，先启动的那个反而找不到
        self.assertTrue(res[1][1], "先启动的解释应当成功")
        self.assertFalse(res[2][1], "后启动的应当被闸门挡下")
        self.assertIn("另一张卡", res[2][2])
        # 串行的话后启动的那个至少要等先启动的跑完
        self.assertLess(res[2][0], 3, "后启动的在排队而不是被立刻拒绝")

    def test_gate_released_after_failure(self):
        """闸门必须在所有返回路径上都释放，否则一次失败之后解释功能永久卡死。"""
        saved = server._llm_ready
        server._llm_ready = (saved[0], [])
        try:
            d = server.explain_card("什么是幂等？", "重复执行效果相同。")
        finally:
            server._llm_ready = saved
        self.assertFalse(d["ok"])
        self.assertTrue(server._explain_gate.acquire(blocking=False),
                        "闸门没有释放")
        server._explain_gate.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
