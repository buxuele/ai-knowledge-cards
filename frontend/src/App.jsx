import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  S, useStore, cur, filtered, catName, LEVEL_NAME,
  PAGE_SIZE, RECYCLE_PAGE_SIZE, go, enterRecycle, exitRecycle, loadRecycle,
  openCard, flip, nextCard, removeCard, toggleMark, startEdit, cancelEdit,
  saveEdit, showAdd, closeAdd, saveAdd, deleteCategory, showCategoryMenu,
  closeCategoryMenu, setTheme, setMode, setMarkedOnly, setLevel, toggleSidebar,
  loadLlmModels, setLlmModel,
  setListView, clearFilters, gotoPage, load, setRecycleQuery, setRecyclePage,
  restoreFromRecycle, clearRecycle, dismissToast, applyFilter, toast, resetProgress,
} from "./store.js";

const THEMES = [
  ["rock", "深灰"], ["ink", "墨黑"], ["sand", "暖灰"],
];

function Sidebar() {
  useStore();
  const top = (label, n, href, cat, sub, cls) => {
    const on = cat === "recycle" ? S.recycleView
      : (!S.recycleView && cat === S.cat && (sub || "") === (S.sub || ""));
    return (
      <a
        key={`${cat}-${sub}-${label}`}
        className={`node ${cls}${on ? " on" : ""}`}
        href={href}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          if (cat === "recycle") enterRecycle();
          else go(cat, sub);
        }}
        onContextMenu={(e) => {
          if (sub !== "" || cat === "all" || cat === "recycle") return;
          e.preventDefault();
          e.stopPropagation();
          showCategoryMenu(e.clientX, e.clientY, cat, label);
        }}
      >
        <span>{label}</span>
        <span className="n">{n}</span>
      </a>
    );
  };
  return (
    <aside className="side">
      <div className="brand">知识点问答卡</div>
      <nav className="tree" id="tree">
        {top("全部", S.all.length, "/all", "all", "", "")}
        {S.groups.map((g) => (
          <React.Fragment key={g.id}>
            {top(g.name, g.count, g.id === "all" ? "/all" : `/${g.id}`, g.id, "", "grp-hd")}
            {g.children.length > 0 && (g.id !== "ai" || S.cat === "ai") && (
              <div className="kids">
                {g.children.map((c) => top(c.name, c.count, `/ai/${c.id}`, g.id, c.id, "kid"))}
              </div>
            )}
          </React.Fragment>
        ))}
        {top("回收站", S.recycleCount, "/recycle", "recycle", "", "recycle-hd")}
      </nav>
      <div className="side-foot" id="sideFoot">
        <span>共 {S.sum ? S.sum.total : 0} 张　已掌握 {S.sum ? S.sum.mastered || 0 : 0} 张</span>
        <button className="side-add" id="addBtn" onClick={showAdd}>新增</button>
      </div>
    </aside>
  );
}

function TopBar({ onSettings }) {
  useStore();
  const hidden = S.recycleView || S.adding;
  return (
    <header className="top">
      <div className="top-row">
        <button className="side-toggle" id="sideToggle" onClick={toggleSidebar}
          aria-label={S.sideOpen ? "收起侧边栏" : "展开侧边栏"}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <div className="tools" hidden={hidden}>
          <button className="icon-btn" id="shuffleBtn" onClick={() => {
            S.listView = false; S.flipped = false; S.page = 0;
            applyFilter();
          }}>打乱</button>
          <button className={`icon-btn${S.listView ? " on" : ""}`} id="listBtn" onClick={() => setListView(!S.listView)}>列表</button>
          <button className={`icon-btn${S.markedOnly ? " on" : ""}`} id="markFilterBtn" onClick={setMarkedOnly}>只看收藏</button>
          <button className="icon-btn" id="settingsBtn" onClick={onSettings}>设置</button>
        </div>
      </div>
    </header>
  );
}

function Detail({ text }) {
  const parts = useMemo(() => {
    const out = [];
    const re = /```([^\n]*)\n([\s\S]*?)```/g;
    let last = 0, m;
    while ((m = re.exec(text || "")) !== null) {
      if (m.index > last) out.push({ type: "text", value: text.slice(last, m.index) });
      out.push({ type: "code", value: m[2].replace(/\n$/, "") });
      last = re.lastIndex;
    }
    if (last < (text || "").length) out.push({ type: "text", value: text.slice(last) });
    return out;
  }, [text]);
  return (
    <div className="detail" id="d">
      {parts.map((p, i) => p.type === "code" ? (
        <div className="code-block" key={i}>
          <pre><code>{p.value}</code></pre>
          <button className="code-copy" onClick={(e) => copyCode(p.value, e.currentTarget)}>复制</button>
        </div>
      ) : p.value.split("\n").filter((x) => x.trim()).map((line, j) => <p key={`${i}-${j}`}>{line}</p>))}
    </div>
  );
}

async function copyCode(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch {}
    ta.remove();
  }
  const old = btn.textContent;
  btn.textContent = "已复制";
  setTimeout(() => { btn.textContent = old; }, 1500);
}

function CardView() {
  useStore();
  const c = cur();
  const [explain, setExplain] = useState({ text: "", warn: false, busy: false });
  const eqRef = useRef(null);
  const eaRef = useRef(null);
  useEffect(() => {
    setExplain({ text: "", warn: false, busy: false });
  }, [c && c.id]);
  useEffect(() => {
    if (!c) return;
    if (eqRef.current) eqRef.current.value = c.q;
    if (eaRef.current) eaRef.current.value = c.a;
  }, [c && c.id, c && c.q, c && c.a]);
  useEffect(() => {
    if (c && S.editing && eqRef.current) {
      eqRef.current.value = c.q;
      eaRef.current.value = c.a;
      eqRef.current.focus();
    }
  }, [S.editing, S.editingId]);
  if (!c) return <section className="card" id="card" hidden />;
  const hidden = S.listView || S.recycleView || S.adding;
  const pos = `${LEVEL_NAME[c.level] || ""}　${S.i + 1} / ${S.queue.length}`;
  const doExplain = async () => {
    setExplain({ text: "等待", warn: false, busy: true });
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 85000);
    try {
      const r = await fetch("/api/explain", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: c.q, a: c.a, model: S.llmModel || undefined }),
        signal: ctl.signal,
      });
      const d = await r.json();
      if (d.ok) {
        setExplain({ text: d.text + (d.model ? `\n\n来自 ${d.model}` : ""), warn: false, busy: false });
      } else {
        let copied = false;
        try { await navigator.clipboard.writeText(d.fallback || ""); copied = true; } catch {}
        setExplain({
          text: `${d.reason ? `${d.reason}。` : ""}${copied ? "已复制到剪贴板，去问别的模型：\n" : "复制这段去问别的模型：\n"}${d.fallback || ""}`,
          warn: true, busy: false,
        });
      }
    } catch (e) {
      setExplain({ text: e.name === "AbortError" ? "解释超时了，模型可能暂时不可用，复制问题去问别的模型" : "解释失败，请检查服务是否在运行", warn: true, busy: false });
    } finally {
      clearTimeout(timer);
    }
  };
  return (
    <section
      className={`card${S.flipped ? " flip" : ""}${S.editing ? " editing" : ""}${explain.text ? " x-on" : ""}`}
      id="card"
      hidden={hidden}
    >
      <div className="card-top">
        <button className={`btn mark${c.marked ? " on" : ""}`} id="markBtn" hidden={S.editing} onClick={toggleMark}>
          {c.marked ? "已收藏" : "收藏"}
        </button>
        <button className="btn" id="explainBtn" hidden={S.editing} disabled={explain.busy} onClick={doExplain}>
          {explain.busy ? "等待" : "解释"}
        </button>
        <button className="btn" id="editBtn" hidden={S.editing} onClick={startEdit}>编辑</button>
        <button className="btn del" id="delBtn" hidden={S.editing} onClick={() => removeCard(c.id)}>删除</button>
        <span className="counter" id="pos">{pos}</span>
        <button className="btn next" id="saveBtn" hidden={!S.editing} onClick={() => saveEdit(eqRef.current.value, eaRef.current.value)}>保存</button>
        <button className="btn" id="cancelBtn" hidden={!S.editing} onClick={cancelEdit}>取消</button>
      </div>

      <div
        className="face front"
        tabIndex={0}
        role="button"
        aria-label="显示答案"
        onClick={() => { if (!window.getSelection().toString()) flip(); }}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } }}
      >
        <p className="q-text" id="q">{c.q}</p>
      </div>

      <div className="face back">
        <p className="q-text back-q" id="q2">{c.q}</p>
        <p className="answer" id="a">{c.a}</p>
        <Detail text={c.detail || ""} />
        {explain.text && <p className={`explain-out${explain.warn ? " warn" : ""}`} id="xout">{explain.text}</p>}
        {!explain.text && <p className="explain-out" id="xout" hidden />}
      </div>

      <div className="edit-box" id="editBox" hidden={!S.editing}>
        <label className="edit-lab" htmlFor="eq">问题</label>
        <textarea id="eq" rows={2} ref={eqRef} key={`q-${c.id}`} defaultValue={c.q} />
        <label className="edit-lab" htmlFor="ea">答案</label>
        <textarea id="ea" rows={3} ref={eaRef} key={`a-${c.id}`} defaultValue={c.a} />
        <p className="edit-err" id="editErr" hidden={!S.editError}>{S.editError}</p>
      </div>

      <div className="foot" id="footFront" hidden={S.flipped}>
        <button className="btn" id="showBtn" onClick={flip}>显示答案</button>
      </div>
      <div className="foot split" id="footBack" hidden={!S.flipped}>
        <button className="btn" id="forgotBtn" onClick={() => nextCard(0)}>没记住</button>
        <button className="btn next" id="nextBtn" onClick={() => nextCard(2)}>下一个</button>
      </div>
    </section>
  );
}

function ListView() {
  useStore();
  const list = filtered();
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  const page = Math.min(S.page, pages - 1);
  const rows = list.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const nums = [];
  for (let i = 0; i < pages; i++) {
    if (i === 0 || i === pages - 1 || Math.abs(i - page) <= 1) nums.push(i);
  }
  return (
    <section className="list-view" id="listView" hidden={!S.listView || S.recycleView || S.adding}>
      <div className="list-head">
        <span id="listCount">共 {list.length} 张　第 {page + 1} / {pages} 页</span>
        <div className="spacer" />
        <button className={`btn${S.resetArmed ? " on" : ""}`} id="resetBtn" onClick={resetProgress}>
          {S.resetArmed ? "撤销清空" : "清空进度"}
        </button>
      </div>
      <div className="list-body" id="listBody">
        {rows.map((c) => (
          <div
            key={c.id}
            className={`row${c.marked ? " marked" : ""}`}
            tabIndex={0}
            role="button"
            aria-label={c.q}
            onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openCard(c.id); } }}
            onClick={() => openCard(c.id)}
          >
            <span className="rq">{c.q}</span>
            <span className={`pill${c.seen && c.due_now ? " due" : c.seen ? " ok" : ""}`}>
              {c.seen && c.due_now ? "待复习" : c.seen ? `${c.due_in} 天后` : "未学"}
            </span>
            <span className="rt">{catName(c.cat)}</span>
            <button className="row-edit" onClick={(e) => { e.stopPropagation(); if (openCard(c.id) !== null) startEdit(); }}>编辑</button>
            <button className="row-del" onClick={(e) => { e.stopPropagation(); removeCard(c.id); }}>删除</button>
          </div>
        ))}
      </div>
      <div className="pager" id="pager" hidden={pages <= 1}>
        <button className="btn pg" id="prevPage" disabled={page <= 0} onClick={() => gotoPage(page - 1)}>上一页</button>
        <div className="pg-nums" id="pgNums">
          {nums.map((i, idx) => (
            <React.Fragment key={i}>
              {idx > 0 && i - nums[idx - 1] > 1 && <span className="pg-gap">…</span>}
              <button className={`pg-num${i === page ? " on" : ""}`} onClick={() => gotoPage(i)}>{i + 1}</button>
            </React.Fragment>
          ))}
        </div>
        <button className="btn pg" id="nextPage" disabled={page >= pages - 1} onClick={() => gotoPage(page + 1)}>下一页</button>
      </div>
    </section>
  );
}

function RecycleView() {
  useStore();
  const q = S.recycleQuery.trim().toLowerCase();
  const list = q ? S.recycleItems.filter((it) => `${it.q} ${it.cat_name || it.cat} ${it.reason_text}`.toLowerCase().includes(q)) : S.recycleItems;
  const pages = Math.max(1, Math.ceil(list.length / RECYCLE_PAGE_SIZE));
  const page = Math.min(S.recyclePage, pages - 1);
  const rows = list.slice(page * RECYCLE_PAGE_SIZE, page * RECYCLE_PAGE_SIZE + RECYCLE_PAGE_SIZE);
  const by = {};
  S.recycleItems.forEach((it) => {
    const label = { unrelated: "未看就删", known: "已掌握", wrong: "答案有问题", trivial: "内容浅显", duplicate: "重复", other: "其他" }[it.reason] || "其他";
    by[label] = (by[label] || 0) + 1;
  });
  const stats = Object.entries(by).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join("　");
  return (
    <section className="list-view" id="recycle" hidden={!S.recycleView}>
      <div className="list-head">
        <span id="recycleCount">{S.recycleLoading ? "加载中" : q ? `命中 ${list.length} / ${S.recycleItems.length} 张` : `共 ${S.recycleItems.length} 张`}</span>
        <div className="spacer" />
        <input className="recycle-search" id="recycleSearch" type="search" value={S.recycleQuery}
          placeholder="搜题面、分类或原因" aria-label="搜索回收站" onChange={(e) => setRecycleQuery(e.target.value)} />
        <button className="btn" id="recycleClearBtn" onClick={clearRecycle}>清空</button>
        <button className="btn" id="recycleBackBtn" onClick={() => go(S.cat, S.sub)}>返回</button>
      </div>
      <p className="recycle-stats" id="recycleStats" hidden={!S.recycleItems.length}>{stats}</p>
      <div className="list-body" id="recycleBody">
        {S.recycleError && <p className="recycle-empty">{S.recycleError}</p>}
        {!S.recycleError && !list.length && <p className="recycle-empty">{q ? "没有匹配的已删卡片，换个关键词再试。" : "回收站是空的。删除的卡片会先放到这里，恢复后回到原分类。"}</p>}
        {rows.map((it) => (
          <div className="row recycle-row" key={it.id} data-id={it.id}>
            <span className="rq">{it.q}</span>
            <span className="rt">{it.cat_name || catName(it.cat)}　{it.archived_at}　{it.reason_text}</span>
            <button className="row-restore" onClick={() => restoreFromRecycle(it.id)}>恢复</button>
          </div>
        ))}
      </div>
      <div className="pager" id="recyclePager" hidden={pages <= 1}>
        <button className="btn pg" id="recyclePrev" disabled={page <= 0} onClick={() => setRecyclePage(page - 1)}>上一页</button>
        <span id="recyclePageText">第 {page + 1} / {pages} 页</span>
        <button className="btn pg" id="recycleNext" disabled={page >= pages - 1} onClick={() => setRecyclePage(page + 1)}>下一页</button>
      </div>
    </section>
  );
}

function AddView() {
  useStore();
  const refs = { cat: useRef(), level: useRef(), q: useRef(), a: useRef(), detail: useRef() };
  return (
    <section className="list-view add-view" id="addView" hidden={!S.adding}>
      <div className="list-head">
        <span>新增卡片</span><div className="spacer" />
        <button className="btn" id="addCancel" onClick={() => closeAdd()}>取消</button>
        <button className="btn next" id="addSave" onClick={async () => {
          const ok = await saveAdd({
            cat: refs.cat.current.value, level: refs.level.current.value,
            q: refs.q.current.value, a: refs.a.current.value, detail: refs.detail.current.value,
          });
          if (!ok) return;
        }}>保存</button>
      </div>
      <div className="add-form">
        <div className="add-row">
          <label className="edit-lab" htmlFor="addCat">分类</label>
          <select id="addCat" ref={refs.cat} defaultValue={S.cat !== "all" ? S.cat : (S.cats[0] && S.cats[0].id)}>
            {S.cats.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <label className="edit-lab" htmlFor="addLevel">难度</label>
          <select id="addLevel" ref={refs.level} defaultValue="basic">
            <option value="basic">基础</option><option value="core">核心</option>
            <option value="practice">实践</option><option value="edge">边界</option>
          </select>
        </div>
        <label className="edit-lab" htmlFor="addQ">问题</label>
        <textarea id="addQ" rows={2} ref={refs.q} placeholder="一个问题只考一个概念" />
        <label className="edit-lab" htmlFor="addA">答案</label>
        <textarea id="addA" rows={3} ref={refs.a} placeholder="先用一句话回答" />
        <label className="edit-lab" htmlFor="addDetail">解释与例子</label>
        <textarea id="addDetail" rows={5} ref={refs.detail} placeholder="解释为什么，另起一行用例如给一个具体例子" />
        {S.addError && <p className="edit-err" id="addErr">{S.addError}</p>}
        {!S.addError && <p className="edit-err" id="addErr" hidden />}
      </div>
    </section>
  );
}

function DoneEmpty() {
  useStore();
  const c = cur();
  const hiddenBase = S.listView || S.recycleView || S.adding || !S.booted;
  const done = !hiddenBase && !c && S.doneCount > 0;
  const empty = !hiddenBase && !c && S.doneCount === 0;
  const due = S.all.filter((x) => x.due_now).length;
  return (
    <>
      <section className="done" id="done" hidden={!done}>
        <h3>本轮完成</h3><p id="doneText">本轮 {S.doneCount} 张　全库待复习 {due} 张</p>
        <button className="btn" id="againBtn" onClick={() => applyFilter()}>再来一轮</button>
      </section>
      <section className="done" id="empty" hidden={!empty}>
        <h3>没有匹配的卡片</h3><p>换个分类，或清空收藏与到期条件再试。</p>
        <button className="btn" id="clearFilterBtn" onClick={clearFilters}>清除筛选</button>
      </section>
    </>
  );
}

function Boot() {
  useStore();
  return (
    <section className="boot" id="boot" hidden={S.booted}>
      <p id="bootMsg">{S.loading ? "正在加载题库。" : S.loadError}</p>
      <button className="btn" id="retryBtn" onClick={() => load()}>重试</button>
    </section>
  );
}

function Settings({ open, onClose }) {
  useStore();
  useEffect(() => {
    if (open) loadLlmModels();
  }, [open]);
  if (!open) return null;
  return (
    <div className="settings-mask" onClick={onClose}>
      <section className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <div className="settings-head">
          <h2>设置</h2>
          <button className="btn" id="settingsClose" onClick={onClose}>关闭</button>
        </div>
        <h3>外观</h3>
        <div className="settings-themes">
          {THEMES.map(([t, label]) => (
            <button key={t} className={`sw${S.theme === t ? " on" : ""}`} data-t={t}
              onClick={() => setTheme(t)}>{label}</button>
          ))}
        </div>
        <h3>LLM 模型</h3>
        {S.llmLoading && <p className="settings-hint">正在读取模型列表。</p>}
        {!S.llmLoading && S.llmError && <p className="settings-hint warn">{S.llmError}</p>}
        {!S.llmLoading && !S.llmError && (
          <select className="settings-select" id="llmModel"
            value={S.llmModel} onChange={(e) => setLlmModel(e.target.value)}>
            <option value="">自动按顺序选择</option>
            {S.llmModels.map((m) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        )}
        <p className="settings-hint">解释按钮优先使用选中的模型，失败后仍会尝试其他已配置模型。</p>
      </section>
    </div>
  );
}

function CatMenu() {
  useStore();
  if (!S.catMenu) return null;
  const { x, y, cat, name } = S.catMenu;
  return (
    <div className="cat-menu" style={{ left: Math.min(x, innerWidth - 100), top: Math.min(y, innerHeight - 50) }}>
      <button className="cat-del" onClick={() => deleteCategory(cat, name)}>删除分类</button>
    </div>
  );
}

function Toast() {
  useStore();
  if (!S.toast) return null;
  const t = S.toast;
  return (
    <div className="toast" id="toast" role="status" aria-live="polite">
      <span>{t.msg}</span>
      {t.actionLabel && <button className="toast-act" onClick={() => { const fn = t.onAction; dismissToast(); fn && fn(); }}>{t.actionLabel}</button>}
    </div>
  );
}

export default function App() {
  useStore();
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => {
    const onPop = () => {
      const r = location.pathname === "/recycle" ? { cat: "recycle", sub: "" } : filteredRoute();
      if (r.cat === "recycle") { enterRecycle(false); return; }
      exitRecycle();
      S.cat = r.cat; S.sub = r.sub; S.page = 0; S.i = 0;
      applyFilter();
    };
    const close = (e) => {
      if (e.type === "contextmenu" && e.target.closest && e.target.closest(".node")) return;
      closeCategoryMenu();
    };
    addEventListener("popstate", onPop);
    document.addEventListener("click", close);
    document.addEventListener("contextmenu", close);
    addEventListener("scroll", close, true);
    const esc = (e) => { if (e.key === "Escape") closeCategoryMenu(); };
    document.addEventListener("keydown", esc);
    return () => {
      removeEventListener("popstate", onPop);
      document.removeEventListener("click", close);
      document.removeEventListener("contextmenu", close);
      removeEventListener("scroll", close, true);
      document.removeEventListener("keydown", esc);
    };
  }, []);
  return (
    <div className={`shell${S.sideOpen ? "" : " side-collapsed"}`}>
      <Sidebar />
      <div className="body">
        <TopBar onSettings={() => setSettingsOpen(true)} />
        <main>
          <Boot />
          <CardView />
          <ListView />
          <RecycleView />
          <AddView />
          <DoneEmpty />
          <Toast />
          <CatMenu />
          <Settings open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        </main>
      </div>
    </div>
  );
}

function filteredRoute() {
  const parts = location.pathname.split("/").filter(Boolean);
  if (!parts.length) return { cat: "ai", sub: "" };
  if (parts[0] === "ai" && parts[1]) return { cat: "ai", sub: parts[1] };
  return { cat: parts[0], sub: "" };
}
