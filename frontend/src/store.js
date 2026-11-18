// React 渲染层背后的数据与动作层。保留原 app.js 的行为语义，
// 但不再直接操作 DOM，组件通过 useSyncExternalStore 订阅这里的变更。
import { useSyncExternalStore } from "react";

export const PAGE_SIZE = 15;
export const RECYCLE_PAGE_SIZE = 20;
export const LEVEL_NAME = { basic: "基础", core: "核心", practice: "实践", edge: "边界" };
export const Q_LIMIT = 300;
export const A_LIMIT = 3000;

let version = 0;
const listeners = new Set();

export const S = {
  booted: false,
  loading: true,
  loadError: "",
  groups: [],
  all: [],
  cats: [],
  sum: null,
  cat: "all",
  sub: "",
  mode: "all",
  level: "all",
  queue: [],
  i: 0,
  flipped: false,
  doneCount: 0,
  listView: false,
  markedOnly: false,
  recycleView: false,
  recycleCount: 0,
  recycleItems: [],
  recyclePage: 0,
  recycleQuery: "",
  recycleLoading: false,
  recycleError: "",
  adding: false,
  addNonce: 0,
  addError: "",
  page: 0,
  snapshot: null,
  resetArmed: false,
  editing: false,
  editingId: null,
  editError: "",
  catMenu: null,
  toast: null,
  theme: localStorage.getItem("theme") || "rock",
  sideOpen: localStorage.getItem("sidebarClosed") !== "1",
  llmModel: localStorage.getItem("llmModel") || "",
  llmModels: [],
  llmLoading: false,
  llmError: "",
  started: false,
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function emit() {
  version++;
  listeners.forEach((fn) => fn());
}

export function useStore() {
  return useSyncExternalStore(subscribe, () => version, () => version);
}

export async function api(path, options = {}) {
  const r = await fetch(path, options);
  let d = null;
  try {
    d = await r.json();
  } catch (e) {
    // 非 JSON 也按失败处理，保留 HTTP 状态
  }
  if (!r.ok) throw new Error((d && d.error) || `服务返回 ${r.status}`);
  return d;
}

let toastTimer = null;

export function toast(msg, actionLabel = "", onAction = null) {
  clearTimeout(toastTimer);
  S.toast = { msg, actionLabel, onAction };
  emit();
  toastTimer = setTimeout(() => {
    S.toast = null;
    emit();
  }, actionLabel ? 6000 : 4000);
}

export function dismissToast() {
  clearTimeout(toastTimer);
  S.toast = null;
  emit();
}

export function parseRoute(path) {
  const parts = path.split("/").filter(Boolean);
  if (!parts.length) return { cat: "ai", sub: "" };
  if (parts[0] === "recycle") return { cat: "recycle", sub: "" };
  if (parts[0] === "ai" && parts[1]) return { cat: "ai", sub: parts[1] };
  return { cat: parts[0], sub: "" };
}

export function routeOf(cat, sub) {
  if (!cat) return "/";
  if (cat === "all") return "/all";
  return sub ? `/ai/${sub}` : `/${cat}`;
}

export function shuffle(a) {
  const r = a.slice();
  for (let i = r.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [r[i], r[j]] = [r[j], r[i]];
  }
  return r;
}

export function cur() {
  return S.queue[S.i] || null;
}

export function catName(id) {
  const g = S.groups.find((x) => x.id === id);
  return g ? g.name : id;
}

export function filtered() {
  let list = S.all;
  if (S.cat !== "all") list = list.filter((c) => c.cat === S.cat);
  if (S.sub) list = list.filter((c) => c.sub === S.sub);
  if (S.mode === "due") list = list.filter((c) => c.seen && c.due_now);
  if (S.markedOnly) list = list.filter((c) => c.marked);
  if (S.level !== "all") list = list.filter((c) => c.level === S.level);
  return list;
}

export function resetPage() {
  S.page = 0;
}

export function applyFilter(keep = null) {
  const prev = keep && S.queue[S.i];
  S.queue = shuffle(filtered());
  if (prev) {
    const at = S.queue.findIndex((x) => x.id === prev.id);
    S.i = at < 0 ? 0 : at;
  } else {
    S.i = 0;
  }
  S.doneCount = 0;
  S.flipped = false;
  S.adding = false;
  S.recycleView = false;
  emit();
}

export function render() {
  const c = cur();
  if (S.editing && (!c || S.editingId !== c.id)) {
    S.editing = false;
    S.editingId = null;
    S.editError = "";
  }
  emit();
}

export function buildTree() {
  emit();
}

export function updateBar() {
  // 侧边栏底部由组件直接从 S.sum 计算
}

export async function refreshData() {
  const [side, cards] = await Promise.all([api("/api/sidebar"), api("/api/cards")]);
  S.groups = side.groups;
  S.all = cards.cards;
  S.cats = cards.categories;
  S.sum = cards.summary;
  S.recycleCount = side.recycle || 0;
  emit();
}

export async function load(keep = false) {
  const wasBooted = S.booted;
  const prevId = keep && cur() ? cur().id : null;
  if (!wasBooted) {
    S.loading = true;
    S.loadError = "";
    emit();
  }
  try {
    const [side, cards] = await Promise.all([api("/api/sidebar"), api("/api/cards")]);
    S.groups = side.groups;
    S.all = cards.cards;
    S.cats = cards.categories;
    S.sum = cards.summary;
    S.recycleCount = side.recycle || 0;
    S.booted = true;
    S.loading = false;
    S.loadError = "";
    if (S.recycleView) {
      emit();
      await loadRecycle();
      return;
    }
    if (keep && wasBooted) {
      const allowed = new Set(filtered().map((c) => c.id));
      const fresh = new Map(S.all.map((c) => [c.id, c]));
      const kept = S.queue.filter((c) => allowed.has(c.id)).map((c) => fresh.get(c.id) || c);
      const keptIds = new Set(kept.map((c) => c.id));
      S.queue = kept.concat(filtered().filter((c) => !keptIds.has(c.id)));
      const at = prevId ? S.queue.findIndex((c) => c.id === prevId) : -1;
      S.i = at < 0 ? Math.min(S.i, Math.max(0, S.queue.length - 1)) : at;
    } else {
      S.queue = shuffle(filtered());
      S.i = 0;
      S.doneCount = 0;
    }
    emit();
  } catch (e) {
    if (keep && wasBooted) {
      toast(`刷新失败：${e.message}`);
      return;
    }
    S.booted = false;
    S.loading = false;
    S.loadError = `连不上服务：${e.message}。确认服务还在运行，然后重试。`;
    emit();
  }
}

export function go(cat, sub) {
  closeAdd(false);
  exitRecycle();
  S.cat = cat;
  S.sub = sub || "";
  const href = routeOf(S.cat, S.sub);
  if (location.pathname !== href) history.pushState(null, "", href);
  resetPage();
  applyFilter();
}

export function openCard(id) {
  const at = S.queue.findIndex((x) => x.id === id);
  if (at < 0) return null;
  S.i = at;
  S.listView = false;
  S.flipped = false;
  emit();
  return at;
}

export function flip() {
  if (S.flipped || !cur()) return;
  S.flipped = true;
  emit();
}

export async function nextCard(quality = 2) {
  const c = cur();
  if (!c) return;
  const at = S.i;
  if (S.flipped) {
    api("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: c.id, quality }),
    }).then((d) => {
      S.sum = d.summary;
      for (const store of [S.all, S.queue]) {
        const t = store.find((x) => x.id === c.id);
        if (t) {
          Object.assign(t, d.next, { seen: 1 });
          t.due_now = d.next.due <= Date.now() / 1000;
          t.due_in = Math.round(d.next.interval);
        }
      }
      emit();
    }).catch((e) => toast(`这次复习没记上：${e.message}`));
    S.doneCount++;
  }
  if (S.flipped && S.mode === "due") {
    S.queue.splice(at, 1);
    S.i = at;
  } else {
    S.i = at + 1;
  }
  S.flipped = false;
  emit();
}

export async function removeCard(cardId) {
  let d;
  try {
    d = await api("/api/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: cardId }),
    });
  } catch (e) {
    toast(`删除失败：${e.message}`);
    return;
  }
  S.all = S.all.filter((c) => c.id !== cardId);
  S.queue = S.queue.filter((c) => c.id !== cardId);
  S.sum = d.summary;
  if (S.i >= S.queue.length) S.i = Math.max(0, S.queue.length - 1);
  emit();
  await load(true);
  toast("已删除这张卡", "撤销", async () => {
    try {
      await api("/api/unarchive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: cardId }),
      });
      await load(true);
      toast("卡片已恢复");
    } catch (e) {
      toast(`撤销删除失败：${e.message}`);
    }
  });
}

export function toggleMark() {
  const c = cur();
  if (!c) return;
  const next = !c.marked;
  c.marked = next;
  S.all.forEach((t) => { if (t.id === c.id) t.marked = next; });
  emit();
  api("/api/mark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: c.id, marked: next }),
  }).then((d) => {
    S.sum = d.summary;
    if (S.markedOnly) applyFilter();
    else emit();
  }).catch((e) => {
    c.marked = !next;
    S.all.forEach((t) => { if (t.id === c.id) t.marked = !next; });
    if (S.markedOnly) applyFilter();
    else emit();
    toast(`收藏没改成功：${e.message}`);
  });
  if (S.markedOnly) applyFilter();
}

export function startEdit() {
  const c = cur();
  if (!c) return;
  S.editing = true;
  S.editingId = c.id;
  S.editError = "";
  emit();
}

export function cancelEdit() {
  S.editing = false;
  S.editingId = null;
  S.editError = "";
  emit();
}

export async function saveEdit(q, a) {
  const c = cur();
  if (!c) return;
  q = (q || "").trim();
  a = (a || "").trim();
  if (!q) { S.editError = "问题不能为空"; emit(); return; }
  if (!a) { S.editError = "答案不能为空"; emit(); return; }
  if (q.length > Q_LIMIT) { S.editError = `问题超出 ${Q_LIMIT} 字`; emit(); return; }
  if (a.length > A_LIMIT) { S.editError = `答案超出 ${A_LIMIT} 字`; emit(); return; }
  const id = c.id;
  try {
    const d = await api("/api/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, q, a }),
    });
    if (!d.ok) throw new Error(d.error || "保存失败");
  } catch (e) {
    S.editError = e.message;
    emit();
    return;
  }
  S.editing = false;
  S.editingId = null;
  S.editError = "";
  await load(true);
  const at = S.queue.findIndex((x) => x.id === id);
  if (at >= 0) S.i = at;
  else toast("问题文本改变了分类，这张卡已移出当前筛选");
  emit();
}

export function showAdd() {
  if (!S.booted) return toast("题库还在加载，稍后再试");
  if (S.editing) return toast("先保存或取消正在编辑的卡");
  S.adding = true;
  S.addNonce++;
  S.addError = "";
  emit();
}

export function closeAdd(renderNow = true) {
  S.adding = false;
  S.addError = "";
  if (renderNow) emit();
}

export async function saveAdd({ cat, q, a, detail, level }) {
  q = (q || "").trim();
  a = (a || "").trim();
  if (!q) { S.addError = "问题不能为空"; emit(); return false; }
  if (!a) { S.addError = "答案不能为空"; emit(); return false; }
  if (q.length > Q_LIMIT) { S.addError = `问题超出 ${Q_LIMIT} 字`; emit(); return false; }
  if (a.length > A_LIMIT) { S.addError = `答案超出 ${A_LIMIT} 字`; emit(); return false; }
  let d;
  try {
    d = await api("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cat, q, a, detail: detail || "", level }),
    });
  } catch (e) {
    S.addError = e.message;
    emit();
    return false;
  }
  const card = d.card;
  closeAdd(false);
  S.cat = card.cat;
  S.sub = "";
  S.mode = "all";
  S.markedOnly = false;
  S.level = "all";
  S.listView = false;
  if (location.pathname !== `/${card.cat}`) history.pushState(null, "", `/${card.cat}`);
  await load();
  const at = S.queue.findIndex((x) => x.id === card.id);
  if (at >= 0) S.i = at;
  emit();
  toast(`已新增 ${card.id}`);
  return true;
}

export function enterRecycle(push = true) {
  closeAdd(false);
  S.recycleView = true;
  S.listView = false;
  S.recyclePage = 0;
  S.recycleQuery = "";
  if (push && location.pathname !== "/recycle") history.pushState(null, "", "/recycle");
  emit();
  loadRecycle();
}

export function exitRecycle() {
  S.recycleView = false;
  emit();
}

export async function loadRecycle() {
  S.recycleLoading = true;
  S.recycleError = "";
  emit();
  try {
    const d = await api("/api/recycle");
    S.recycleItems = d.items;
    S.recycleCount = d.total;
  } catch (e) {
    S.recycleError = `回收站加载失败：${e.message}`;
  } finally {
    S.recycleLoading = false;
    emit();
  }
}

export function setRecycleQuery(q) {
  S.recycleQuery = q;
  S.recyclePage = 0;
  emit();
}

export function setRecyclePage(n) {
  S.recyclePage = n;
  emit();
}

export async function restoreFromRecycle(id) {
  try {
    await api("/api/unarchive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    await refreshData();
    await loadRecycle();
    toast("已恢复到原分类");
  } catch (e) {
    toast(`恢复失败：${e.message}`);
  }
}

export async function clearRecycle() {
  if (!S.recycleItems.length) return toast("回收站已经是空的");
  if (!window.confirm(`永久删除回收站里的 ${S.recycleItems.length} 张卡？`)) return;
  try {
    const d = await api("/api/recycle", { method: "DELETE" });
    await refreshData();
    await loadRecycle();
    toast(`已清空回收站 ${d.cleared} 张`);
  } catch (e) {
    toast(`清空失败：${e.message}`);
  }
}

export function showCategoryMenu(x, y, cat, name) {
  S.catMenu = { x, y, cat, name };
  emit();
}

export function closeCategoryMenu() {
  if (!S.catMenu) return;
  S.catMenu = null;
  emit();
}

export async function deleteCategory(cat, name) {
  closeCategoryMenu();
  let d;
  try {
    d = await api("/api/delete-category", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cat }),
    });
  } catch (e) {
    toast(`删除分类失败：${e.message}`);
    return;
  }
  const wasCurrent = S.cat === cat;
  await refreshData();
  go(wasCurrent ? "all" : S.cat, wasCurrent ? "" : S.sub);
  toast(d.deleted ? `已删除分类 ${name}，${d.deleted} 张卡已移入回收站` : `已删除空分类 ${name}`);
}

export function toggleSidebar() {
  S.sideOpen = !S.sideOpen;
  localStorage.setItem("sidebarClosed", S.sideOpen ? "0" : "1");
  emit();
}

export async function loadLlmModels() {
  S.llmLoading = true;
  S.llmError = "";
  emit();
  try {
    const d = await api("/api/llm/models");
    S.llmModels = d.models || [];
    if (S.llmModel && !S.llmModels.some((m) => m.id === S.llmModel)) {
      S.llmModel = "";
      localStorage.removeItem("llmModel");
    }
  } catch (e) {
    S.llmError = `模型列表加载失败：${e.message}`;
  } finally {
    S.llmLoading = false;
    emit();
  }
}

export function setLlmModel(id) {
  S.llmModel = id;
  if (id) localStorage.setItem("llmModel", id);
  else localStorage.removeItem("llmModel");
  emit();
}

export function setTheme(t) {
  S.theme = t;
  localStorage.setItem("theme", t);
  document.body.dataset.t = t;
  const meta = document.getElementById("themeColorMeta");
  if (meta) {
    meta.content = { rock: "#1c1c1e", ink: "#151517", sand: "#211f1b" }[t] || "#1e222b";
  }
  emit();
}

export function setMode() {
  S.mode = S.mode === "due" ? "all" : "due";
  resetPage();
  applyFilter();
}

export function setMarkedOnly() {
  S.markedOnly = !S.markedOnly;
  resetPage();
  applyFilter();
}

export function setLevel(level) {
  S.level = level;
  resetPage();
  applyFilter();
}

export function setListView(next = !S.listView) {
  S.listView = next;
  S.flipped = false;
  emit();
}

export function clearFilters() {
  S.markedOnly = false;
  S.mode = "all";
  S.level = "all";
  resetPage();
  applyFilter();
}

export function gotoPage(n) {
  S.page = Math.max(0, n);
  emit();
  window.scrollTo({ top: 0, behavior: "auto" });
}

export async function resetProgress() {
  if (S.resetArmed && S.snapshot) {
    try {
      await api("/api/restore", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(S.snapshot),
      });
      S.snapshot = null;
      S.resetArmed = false;
      await load();
      toast("进度已恢复");
    } catch (e) {
      toast(`撤销失败：${e.message}`);
    }
    return;
  }
  let snap;
  try {
    snap = await api("/api/snapshot");
  } catch (e) {
    toast(`取快照失败：${e.message}`);
    return;
  }
  S.snapshot = snap.rows;
  try {
    await api("/api/reset", { method: "DELETE" });
  } catch (e) {
    S.snapshot = null;
    toast(`清空失败：${e.message}`);
    return;
  }
  S.resetArmed = true;
  await load();
  toast("进度已清空");
}

export function initApp() {
  if (S.started) return;
  S.started = true;
  document.body.dataset.t = S.theme;
  const r = parseRoute(location.pathname);
  S.recycleView = r.cat === "recycle";
  if (!S.recycleView) {
    S.cat = r.cat;
    S.sub = r.sub;
  }
  if (location.pathname === "/") history.replaceState(null, "", routeOf(S.cat, S.sub));
  load();
}

// 兼容原浏览器验收脚本使用的全局入口。React 组件仍是实际渲染层。
export function installTestBridge() {
  Object.assign(window, {
    S, applyFilter, buildTree, render, resetPage, cur, load, removeCard, go,
    showAdd, closeAdd, saveAdd, enterRecycle, exitRecycle, deleteCategory,
    nextCard, toggleMark, startEdit, cancelEdit, saveEdit, flip, setLevel,
    setMode, setMarkedOnly, setListView, clearFilters, gotoPage, openCard,
    parseRoute, routeOf, resetProgress, toggleSidebar, loadLlmModels,
    setLlmModel,
  });
}
