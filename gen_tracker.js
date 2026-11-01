// Gen Manager — ComfyUI サイドバーの「生成管理」ギャラリー
//
//  ・output の全ファイルを日付ごとに表示(新しい順)
//  ・お気に入り(★)・タグ・メモ・条件を付けられる
//  ・クリックで、生成設定(プロンプト・モデル・LoRA・サンプラー)と所要時間を確認
//  ・種類 / 日付範囲 / 条件 / タグ / お気に入りのみ で絞り込み
//
// 外部由来の文字列(プロンプト・メモ・ファイル名など)は textContent / createTextNode だけで表示し、
// innerHTML には埋め込まない。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const API = "/gen_tracker";
const PAGE = 400;           // 一度に描画するカードの数(大量の画像でも重くならないように)
const NO_CONDITION = "__none__";

// ---------------------------------------------------------------- 小道具
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// replaceChildren は null を "null" という文字にしてしまうので、空要素を除いてから渡す
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((c) => c != null && c !== false));
}

const pad2 = (n) => String(n).padStart(2, "0");
function fmtDay(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
function fmtDayLabel(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日(${"日月火水木金土"[d.getDay()]})`;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function fmtDur(sec) {
  if (sec == null) return "";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  if (m < 60) return `${m}m${pad2(Math.round(s))}s`;
  return `${Math.floor(m / 60)}h${pad2(m % 60)}m`;
}
const createdTs = (item) => item.ctime || item.mtime;

async function call(path, { method = "GET", params, body } = {}) {
  let url = `${API}${path}`;
  if (params) {
    const s = new URLSearchParams(params).toString();
    if (s) url += `?${s}`;
  }
  const res = await api.fetchApi(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* JSON でない応答 */
  }
  if (!res.ok || !data || data.ok === false) {
    throw new Error((data && data.error) || `通信に失敗しました (${res.status})`);
  }
  return data;
}

const viewUrl = (item) =>
  api.apiURL(`/view?filename=${encodeURIComponent(item.filename)}&subfolder=${encodeURIComponent(item.subfolder || "")}&type=output`);

function store(key, value) {
  // ブラウザの保存領域は使えないことがある(プライベートウィンドウなど)ので、失敗しても動くようにする
  try {
    if (value === undefined) return window.localStorage.getItem(key);
    window.localStorage.setItem(key, value);
  } catch (_) {
    return null;
  }
  return null;
}

// ---------------------------------------------------------------- スタイル
const CSS = `
.gt{--gt-fg:var(--input-text,#ddd);--gt-bg:var(--comfy-menu-bg,#15151a);--gt-in:var(--comfy-input-bg,#2a2a35);
 --gt-bd:var(--border-color,#444);--gt-mut:#999;--gt-ac:#e8b400;--gt-tag:#7fc8ff;--gt-ng:#e5646e;
 display:flex;flex-direction:column;height:100%;padding:8px;box-sizing:border-box;font-size:12px;color:var(--gt-fg)}
.gt *{box-sizing:border-box}
.gt button,.gtm button{padding:5px 9px;border-radius:4px;border:1px solid var(--gt-bd,#444);background:var(--gt-in,#2a2a35);color:inherit;cursor:pointer;font-size:12px}
.gt button:hover,.gtm button:hover{filter:brightness(1.2)}
.gt button:disabled,.gtm button:disabled{opacity:.45;cursor:default}
.gt button.on,.gtm button.on{background:var(--gt-ac,#e8b400);border-color:var(--gt-ac,#e8b400);color:#111}
.gt button.pri,.gtm button.pri{background:#e8b400;border-color:#e8b400;color:#111}
.gt button.danger,.gtm button.danger{border-color:#7a3a2a;background:#3a2420;color:#ffb199}
.gt input,.gt select,.gtm input,.gtm select,.gtm textarea{padding:4px 6px;border-radius:4px;border:1px solid var(--gt-bd,#444);background:var(--gt-in,#2a2a35);color:inherit;font-size:12px;min-width:0}
.gt-row{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.gt-row label{color:var(--gt-mut);white-space:nowrap}
.gt-grow{flex:1 1 90px}
.gt-mut{color:var(--gt-mut)}
.gt-msg{min-height:14px;font-size:11px;margin-bottom:4px}.gt-msg.err{color:var(--gt-ng)}
.gt-panel{border:1px solid var(--gt-bd);border-radius:6px;padding:8px;margin-bottom:8px;line-height:1.7}
.gt-banner{border:1px solid #6b5a1e;background:#2d2a18;border-radius:6px;padding:6px 8px;margin-bottom:6px;display:flex;gap:6px;align-items:center}
.gt-list{flex:1;overflow-y:auto}
.gt-day{position:sticky;top:0;background:var(--gt-bg);padding:6px 2px 4px;font-weight:600;border-bottom:1px solid #333;margin-top:8px;z-index:1}
.gt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;align-content:start;padding:6px 0}
.gt-card{position:relative;border-radius:4px;overflow:hidden;background:#111;border:1px solid #333;display:flex;flex-direction:column;cursor:pointer}
.gt-thumb{position:relative;width:100%;aspect-ratio:1/1;background:#000}
.gt-thumb img,.gt-thumb video{width:100%;height:100%;object-fit:cover;display:block}
.gt-audio{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:28px;background:#181820}
.gt-star{position:absolute;top:2px;right:2px;font-size:15px;cursor:pointer;background:rgba(0,0,0,.55);border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;line-height:1;color:#ccc}
.gt-star.on{color:#ffd23f}
.gt-badge{position:absolute;left:2px;top:2px;padding:1px 4px;border-radius:3px;background:rgba(0,0,0,.65);font-size:10px;color:#fff}
.gt-info{padding:3px 4px;font-size:10px;color:var(--gt-mut);line-height:1.4}
.gt-info .c{color:var(--gt-ac);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gt-info .t{color:var(--gt-tag);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gt-empty{padding:16px;color:#777;text-align:center;line-height:1.8}
.gtm-ov{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center}
.gtm{background:var(--comfy-menu-bg,#1e1e26);color:var(--input-text,#ddd);border:1px solid var(--border-color,#444);border-radius:8px;padding:14px;width:440px;max-width:94vw;max-height:92vh;overflow-y:auto;font-size:12px;box-sizing:border-box}
.gtm *{box-sizing:border-box}
.gtm .gt-row{display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.gtm .gt-row label{color:#999}
.gtm .gt-grow{flex:1 1 90px}
.gtm .pv{text-align:center;margin-bottom:8px}
.gtm .pv img,.gtm .pv video{max-width:100%;max-height:240px;border-radius:4px;border:1px solid #444}
.gtm .lab{color:#aaa;margin:8px 0 2px}
.gtm .blk{background:#111;border:1px solid #333;border-radius:4px;padding:6px;max-height:96px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;user-select:text}
.gtm .blkh{display:flex;justify-content:space-between;align-items:center;color:#aaa;margin-bottom:2px}
.gtm .blkh button{padding:1px 8px;font-size:10px}
`;

// ---------------------------------------------------------------- 状態
const S = {
  root: null,
  items: [], conditions: [], unrecorded: 0,
  filters: { fav: false, type: "", condition: "", tag: "", from: "", to: "" },
  limit: PAGE,
  settings: { current_condition: "", condition_rule: "model" },
  panelOpen: false,
  msg: { text: "", err: false },
};
const mounted = () => S.root && document.body.contains(S.root);
const q = (sel) => S.root && S.root.querySelector(sel);

function setMsg(text, err = false) {
  S.msg = { text, err };
  const el = q(".gt-msg");
  if (el) {
    el.textContent = text;
    el.className = "gt-msg" + (err ? " err" : "");
  }
}

// ---------------------------------------------------------------- データ
async function load() {
  if (!mounted()) return;
  setMsg("読み込み中…");
  try {
    const d = await call("/files");
    Object.assign(S, { items: d.items, conditions: d.conditions, unrecorded: d.unrecorded_png });
  } catch (e) {
    fill(q(".gt-list"), h("div", { class: "gt-empty", style: "color:#f66" }, `一覧の取得に失敗しました: ${e.message}`));
    setMsg("", false);
    return;
  }
  setMsg("");
  renderFilters();
  renderBanner();
  renderList();
}

let syncing = false;
async function doSync(manual) {
  if (syncing) return;
  syncing = true;
  try {
    const r = await call("/sync", { method: "POST", body: {} });
    if (manual) setMsg(r.added ? `${r.added}件の生成を記録しました` : "新しい生成はありませんでした");
    if (r.added && mounted()) await load();
  } catch (e) {
    setMsg(`記録の同期に失敗: ${e.message}`, true);
  } finally {
    syncing = false;
  }
}

let syncTimers = [];
function scheduleSync() {
  // 履歴に載るのが実行完了イベントの少し後なので、遅らせて2回確かめる
  syncTimers.forEach(clearTimeout);
  syncTimers = [setTimeout(() => doSync(false), 1500), setTimeout(() => doSync(false), 6000)];
}

async function updateFile(item, patch) {
  await call("/file_update", { method: "POST", body: { path: item.path, ...patch } });
}

// ---------------------------------------------------------------- 絞り込み
function filtered() {
  const f = S.filters;
  return S.items.filter((i) => {
    if (f.fav && !i.favorite) return false;
    if (f.type && i.ext !== f.type) return false;
    if (f.condition) {
      const c = (i.run && i.run.condition) || "";
      if (f.condition === NO_CONDITION ? c !== "" : c !== f.condition) return false;
    }
    if (f.tag && !(i.tags || []).includes(f.tag)) return false;
    const day = fmtDay(createdTs(i));
    if (f.from && day < f.from) return false;
    if (f.to && day > f.to) return false;
    return true;
  });
}

function optionList(select, first, values, current) {
  select.replaceChildren(h("option", { value: "" }, first), ...values.map(([v, label]) => h("option", { value: v }, label)));
  select.value = values.some(([v]) => v === current) ? current : "";
  return select.value;
}

function renderFilters() {
  const f = S.filters;
  const types = {};
  const tags = {};
  for (const i of S.items) {
    types[i.ext] = (types[i.ext] || 0) + 1;
    for (const t of i.tags || []) tags[t] = (tags[t] || 0) + 1;
  }
  f.type = optionList(q(".f-type"), `種類: すべて (${S.items.length})`,
    Object.keys(types).sort().map((e) => [e, `.${e} (${types[e]})`]), f.type);
  f.condition = optionList(q(".f-cond"), "条件: すべて",
    [[NO_CONDITION, "(条件なし)"], ...S.conditions.map((c) => [c, c])], f.condition);
  f.tag = optionList(q(".f-tag"), "タグ: すべて", Object.keys(tags).sort().map((t) => [t, `#${t} (${tags[t]})`]), f.tag);
}

function renderBanner() {
  const el = q(".gt-bannerbox");
  if (!el) return;
  const hide = store("gt_banner_hidden") === "1";
  if (S.unrecorded > 0 && !hide) {
    fill(el, h("div", { class: "gt-banner" },
      h("span", { class: "gt-grow" }, `記録のないPNGが${S.unrecorded}枚あります。設定が入っていれば、過去分として取り込めます。`),
      h("button", { onclick: () => importPast() }, "取り込む"),
      h("button", { title: "このメッセージを閉じる", onclick: () => { store("gt_banner_hidden", "1"); renderBanner(); } }, "×")));
  } else {
    fill(el);
  }
}

// ---------------------------------------------------------------- 一覧
function renderList() {
  const list = q(".gt-list");
  const shown = filtered();
  const favCount = S.items.filter((i) => i.favorite).length;
  q(".gt-status").textContent = `${shown.length}件表示 / 全${S.items.length}件 / お気に入り${favCount}件`;

  if (shown.length === 0) {
    fill(list, h("div", { class: "gt-empty" },
      S.items.length === 0
        ? ["まだ画像がありません。", h("br"), "ComfyUI で生成すると、ここに表示されます。"]
        : "該当する画像・動画がありません"));
    return;
  }
  const dayCounts = {};
  for (const i of shown) dayCounts[fmtDay(createdTs(i))] = (dayCounts[fmtDay(createdTs(i))] || 0) + 1;

  const kids = [];
  let current = null;
  let grid = null;
  for (const item of shown.slice(0, S.limit)) {
    const day = fmtDay(createdTs(item));
    if (day !== current) {
      current = day;
      kids.push(h("div", { class: "gt-day" }, `${fmtDayLabel(createdTs(item))}(${dayCounts[day]}件)`));
      grid = h("div", { class: "gt-grid" });
      kids.push(grid);
    }
    grid.append(makeCard(item));
  }
  if (shown.length > S.limit) {
    kids.push(h("div", { style: "text-align:center;padding:10px" },
      h("button", { onclick: () => { S.limit += PAGE; renderList(); } }, `さらに${Math.min(PAGE, shown.length - S.limit)}件を表示(残り${shown.length - S.limit}件)`)));
  }
  fill(list, kids);
}

function starEl(item, onToggle) {
  const star = h("div", { class: "gt-star" + (item.favorite ? " on" : ""), title: "お気に入り" }, item.favorite ? "★" : "☆");
  star.addEventListener("click", async (e) => {
    e.stopPropagation();
    const next = !item.favorite;
    try {
      await updateFile(item, { favorite: next });
      item.favorite = next;
      star.textContent = next ? "★" : "☆";
      star.classList.toggle("on", next);
      if (onToggle) onToggle();
    } catch (err) {
      setMsg(`お気に入りを保存できませんでした: ${err.message}`, true);
    }
  });
  return star;
}

function makeCard(item) {
  let media;
  if (item.kind === "video") media = h("video", { src: `${viewUrl(item)}#t=0.1`, muted: true, preload: "metadata", title: item.filename });
  else if (item.kind === "audio") media = h("div", { class: "gt-audio", title: item.filename }, "🎵");
  else media = h("img", { src: viewUrl(item), loading: "lazy", title: item.filename, alt: item.filename });

  const run = item.run;
  const afterToggle = () => {
    q(".gt-status").textContent = q(".gt-status").textContent.replace(/お気に入り\d+件/, `お気に入り${S.items.filter((i) => i.favorite).length}件`);
    if (S.filters.fav && !item.favorite) renderList();
  };
  return h("div", { class: "gt-card", onclick: () => openModal(item) },
    h("div", { class: "gt-thumb" }, media, starEl(item, afterToggle),
      run && run.duration_sec != null ? h("span", { class: "gt-badge" }, fmtDur(run.duration_sec)) : null),
    h("div", { class: "gt-info" },
      h("div", { title: `作成: ${fmtDayLabel(createdTs(item))} ${fmtTime(createdTs(item))}` }, fmtTime(createdTs(item))),
      run && run.condition ? h("div", { class: "c", title: `条件: ${run.condition}` }, run.condition) : null,
      item.tags && item.tags.length ? h("div", { class: "t", title: item.tags.join(", ") }, item.tags.map((t) => `#${t}`).join(" ")) : null));
}

// ---------------------------------------------------------------- 詳細(クリック時)
function infoBlock(label, text) {
  const btn = h("button", {
    onclick: async () => {
      try { await navigator.clipboard.writeText(text); btn.textContent = "コピーしました"; } catch (_) { btn.textContent = "失敗"; }
      setTimeout(() => { btn.textContent = "コピー"; }, 1200);
    },
  }, "コピー");
  // 本文は外部由来の文字列なので、必ず textContent(h の文字ノード)で入れる
  return h("div", null, h("div", { class: "blkh" }, h("span", null, label), btn), h("div", { class: "blk" }, text));
}

function renderInfo(box, data) {
  const blocks = [];
  if (!data || !data.ok) {
    fill(box, h("div", { class: "gt-mut" }, (data && data.error) || "生成情報を取得できませんでした"));
    return;
  }
  if (data.positive) blocks.push(infoBlock("ポジティブプロンプト", data.positive));
  if (data.negative) blocks.push(infoBlock("ネガティブプロンプト", data.negative));
  for (const t of data.other_texts || []) blocks.push(infoBlock(`その他: ${t.label}`, t.text));
  const lines = [];
  if (data.model) lines.push(`モデル: ${data.model}`);
  for (const l of data.loras || []) lines.push(`LoRA: ${l.name}${l.strength != null ? ` (強度 ${l.strength})` : ""}`);
  const s = data.sampler;
  if (s) {
    const parts = [];
    if (s.sampler_name) parts.push(s.sampler_name);
    if (s.scheduler) parts.push(s.scheduler);
    if (s.steps != null) parts.push(`steps ${s.steps}`);
    if (s.cfg != null) parts.push(`cfg ${s.cfg}`);
    if (s.denoise != null && s.denoise !== 1) parts.push(`denoise ${s.denoise}`);
    if (parts.length) lines.push(`サンプラー: ${parts.join(" / ")}`);
    if (s.seed != null) lines.push(`シード: ${s.seed}`);
  }
  if (lines.length) blocks.push(infoBlock("生成設定", lines.join("\n")));
  fill(box, blocks.length ? blocks.map((b) => h("div", { style: "margin-bottom:8px" }, b)) : h("div", { class: "gt-mut" }, "プロンプト情報が見つかりませんでした"));
}

function openModal(item) {
  let fav = item.favorite;
  const run = item.run;
  const ov = h("div", { class: "gtm-ov", onclick: (e) => { if (e.target === ov) close(); } });
  const close = () => { document.removeEventListener("keydown", onKey); ov.remove(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);

  const url = viewUrl(item);
  let preview;
  if (item.kind === "video") preview = h("video", { src: url, controls: true, preload: "metadata" });
  else if (item.kind === "audio") preview = h("audio", { src: url, controls: true });
  else preview = h("img", { src: url, alt: item.filename });

  const favBtn = h("button", { onclick: () => { fav = !fav; paintFav(); } });
  const paintFav = () => { favBtn.textContent = fav ? "★ お気に入り" : "☆ お気に入りにする"; favBtn.classList.toggle("on", fav); };
  paintFav();

  const condIn = h("input", { class: "gt-grow", value: (run && run.condition) || "", placeholder: "例: lora=0.6" });
  const tagsIn = h("input", { style: "width:100%", value: (item.tags || []).join(", "), placeholder: "例: 候補, 良い表情(カンマ区切り)" });
  const noteIn = h("textarea", { rows: 3, style: "width:100%;resize:vertical", placeholder: "メモ" });
  noteIn.value = item.note || "";
  const infoBox = h("div");
  const err = h("div", { style: "color:#f66;min-height:14px;margin-top:6px" });

  const facts = [`作成: ${fmtDayLabel(createdTs(item))} ${fmtTime(createdTs(item))}`];
  if (run && run.duration_sec != null) facts.push(`所要時間: ${fmtDur(run.duration_sec)}`);

  const panel = h("div", { class: "gtm", role: "dialog", "aria-label": "画像の詳細" },
    h("div", { class: "gt-row", style: "justify-content:space-between" },
      h("b", { style: "word-break:break-all" }, item.filename),
      h("a", { href: url, target: "_blank", rel: "noopener", style: "color:#7fc8ff;white-space:nowrap" }, "元のサイズで開く")),
    h("div", { class: "pv" }, preview),
    h("div", { class: "gt-row" }, favBtn, h("span", { class: "gt-mut" }, facts.join("  /  "))),
    h("div", { class: "lab" }, "条件(比べたい設定のラベル。同じ生成の画像すべてに付きます)"),
    h("div", { class: "gt-row" }, condIn),
    h("div", { class: "lab" }, "タグ"), tagsIn,
    h("div", { class: "lab" }, "メモ"), noteIn,
    h("div", { class: "lab", style: "margin-top:12px" }, "生成設定"), infoBox,
    err,
    h("div", { class: "gt-row", style: "justify-content:flex-end;margin-top:8px" },
      h("button", { onclick: close }, "キャンセル"),
      h("button", {
        class: "pri",
        onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          const tags = tagsIn.value.split(/[,、]/).map((t) => t.trim()).filter(Boolean);
          const newCond = condIn.value.trim();
          const patch = { favorite: fav, tags, note: noteIn.value };
          const condChanged = newCond !== ((run && run.condition) || "");
          if (condChanged) patch.condition = newCond;   // 変えていないときは送らない(不要な記録を作らない)
          try {
            await updateFile(item, patch);
            Object.assign(item, { favorite: fav, tags, note: noteIn.value });
            close();
            if (condChanged) await load();   // 同じ生成の他の画像にも反映されるので読み直す
            else { renderFilters(); renderList(); }
          } catch (er) {
            err.textContent = `保存できませんでした: ${er.message}`;
            btn.disabled = false;
          }
        },
      }, "保存")));
  ov.append(panel);
  document.body.append(ov);

  // 生成設定(PNG のみ)。読み込み中に閉じられた場合の結果は捨てる
  if (item.ext === "png") {
    fill(infoBox, h("div", { class: "gt-mut" }, "生成情報を読み込み中…"));
    call("/file_info", { params: { path: item.path } })
      .then((d) => { if (ov.isConnected) renderInfo(infoBox, d); })
      .catch((e) => { if (ov.isConnected) renderInfo(infoBox, { ok: false, error: e.message }); });
  } else {
    fill(infoBox, h("div", { class: "gt-mut" }, "生成設定の表示はPNG画像のみ対応しています"));
  }
}

// ---------------------------------------------------------------- 管理パネル(過去分・自動設定・再起動)
async function importPast() {
  const out = q(".gt-importmsg");
  const btns = S.root.querySelectorAll(".gt-import");
  btns.forEach((b) => (b.disabled = true));
  setMsg("過去分を取り込み中…(枚数が多いと時間がかかります)");
  try {
    const r = await call("/ingest", { method: "POST", body: { experiment: "過去分" } });
    const text = `${r.added}件を取り込みました(登録済み ${r.known}件 / 設定が入っていない画像 ${r.no_meta}件はスキップ)`;
    setMsg(text);
    if (out) out.textContent = text;
    store("gt_banner_hidden", "1");
    await load();
  } catch (e) {
    setMsg(`取り込みに失敗: ${e.message}`, true);
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

async function restartComfy(btn) {
  if (!confirm("ComfyUIサーバーを再起動します。\n実行中の生成は中断され、キューの内容は失われます。\n続けますか?")) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "再起動中…";
  // ComfyUI-Manager のエンドポイント。再起動が始まるとレスポンスが返らず切断されるのが正常な動作
  await api.fetchApi("/manager/reboot", { method: "GET" }).catch(() => {});
  btn.textContent = "起動待機中…";
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch(`${location.origin}/`, { cache: "no-store" });
      if (res.ok) { clearInterval(poll); location.reload(); return; }
    } catch (_) { /* まだ落ちている/起動中 */ }
    if (attempts >= 60) {   // 約3分
      clearInterval(poll);
      btn.disabled = false;
      btn.textContent = original;
      alert("一定時間たっても再接続できませんでした。ComfyUIが起動しているか確認し、必要ならページを再読み込みしてください。\n(再起動には ComfyUI-Manager が必要です)");
    }
  }, 3000);
}

async function saveSetting(patch) {
  try {
    const r = await call("/settings", { method: "POST", body: patch });
    S.settings = r.settings;
    return true;
  } catch (e) {
    setMsg(`保存できませんでした: ${e.message}`, true);
    return false;
  }
}

function renderPanel() {
  const box = q(".gt-panelbox");
  if (!S.panelOpen) { fill(box); return; }
  const rule = h("select", { onchange: async (e) => { if (await saveSetting({ condition_rule: e.target.value })) setMsg("保存しました"); } },
    [["model", "モデル名"], ["model_lora", "モデル名 + LoRA"], ["none", "付けない"]].map(([v, l]) => h("option", { value: v }, l)));
  rule.value = S.settings.condition_rule;
  const restart = h("button", { class: "danger", title: "新しく入れたカスタムノード・モデルを反映します(ComfyUI-Manager が必要)", onclick: (e) => restartComfy(e.currentTarget) }, "⟳ ComfyUIを再起動");
  fill(box, h("div", { class: "gt-panel" },
    h("div", { class: "gt-mut", style: "margin-bottom:6px" },
      "生成するたびに、設定・所要時間が自動で記録されます。上の「いまの条件」を変えながら生成すると、条件ごとに画像を見比べられます。画像ファイルは動かしません。"),
    h("div", { class: "gt-row" }, h("label", null, "条件が空のときの自動の付け方"), rule),
    h("div", { class: "gt-row" },
      h("button", { class: "gt-import", onclick: () => importPast() }, "過去分を取り込む"),
      h("span", { class: "gt-mut gt-importmsg" }, "output の設定入りPNGを登録します")),
    h("div", { class: "gt-row" }, restart)));
}

// ---------------------------------------------------------------- 枠組み
async function render(el) {
  if (!document.getElementById("gt-style")) document.head.append(h("style", { id: "gt-style" }, CSS));
  const f = S.filters;

  const condNow = h("input", { class: "gt-grow", placeholder: "空なら自動(モデル名)", title: "これから生成する画像に付ける条件名" });
  const saveCond = async () => { if (await saveSetting({ current_condition: condNow.value.trim() })) setMsg("次の生成から、この条件で記録します"); };
  condNow.addEventListener("change", saveCond);
  condNow.addEventListener("keydown", (e) => { if (e.key === "Enter") condNow.blur(); });

  const favBtn = h("button", { style: "flex:1", onclick: (e) => { f.fav = !f.fav; e.currentTarget.classList.toggle("on", f.fav); renderList(); } }, "お気に入りのみ");
  const reset = () => renderList();
  const dateFrom = h("input", { type: "date", class: "gt-grow", onchange: (e) => { f.from = e.target.value; reset(); } });
  const dateTo = h("input", { type: "date", class: "gt-grow", onchange: (e) => { f.to = e.target.value; reset(); } });

  const root = h("div", { class: "gt" },
    h("div", { class: "gt-row" }, h("label", null, "いまの条件"), condNow),
    h("div", { class: "gt-row" },
      favBtn,
      h("button", { onclick: () => { doSync(true); load(); } }, "更新"),
      h("button", { title: "過去分の取り込み・自動設定・再起動", onclick: () => { S.panelOpen = !S.panelOpen; renderPanel(); } }, "⚙")),
    h("div", { class: "gt-panelbox" }),
    h("div", { class: "gt-row" },
      h("select", { class: "gt-grow f-cond", onchange: (e) => { f.condition = e.target.value; S.limit = PAGE; reset(); } }),
      h("select", { class: "gt-grow f-type", onchange: (e) => { f.type = e.target.value; S.limit = PAGE; reset(); } }),
      h("select", { class: "gt-grow f-tag", onchange: (e) => { f.tag = e.target.value; S.limit = PAGE; reset(); } })),
    h("div", { class: "gt-row" }, dateFrom, h("span", { class: "gt-mut" }, "〜"), dateTo,
      h("button", { title: "日付をクリア", onclick: () => { f.from = f.to = ""; dateFrom.value = dateTo.value = ""; reset(); } }, "×")),
    h("div", { class: "gt-bannerbox" }),
    h("div", { class: "gt-msg" }, S.msg.text),
    h("div", { class: "gt-mut gt-status", style: "margin-bottom:4px" }),
    h("div", { class: "gt-list" }));
  S.root = root;
  el.replaceChildren(root);
  favBtn.classList.toggle("on", f.fav);
  dateFrom.value = f.from;
  dateTo.value = f.to;
  renderPanel();

  try {
    const st = await call("/state");
    S.settings = st.settings;
    condNow.value = st.settings.current_condition || "";
    renderPanel();
  } catch (e) {
    setMsg(`状態を取得できません: ${e.message}`, true);
  }
  load();
  doSync(false);   // 開いた時点で、最新の生成まで記録する
}

app.registerExtension({
  name: "GenManager",
  async setup() {
    try {
      app.extensionManager.registerSidebarTab({
        id: "genManager",
        icon: "pi pi-images",
        title: "生成管理",
        tooltip: "生成結果のギャラリー(お気に入り・条件・生成設定)",
        type: "custom",
        render: (el) => {
          render(el).catch((err) => {
            console.error("[GenManager] render failed:", err);
            el.replaceChildren(h("div", { style: "padding:12px;color:#f66" }, `表示に失敗しました: ${err.message}(F12 のコンソールを確認してください)`));
          });
        },
      });
    } catch (err) {
      console.error("[GenManager] registerSidebarTab failed:", err);
    }
    // サイドバーを開いていなくても、生成が終わるたびに記録する
    for (const ev of ["execution_success", "execution_error", "execution_interrupted"]) {
      api.addEventListener(ev, scheduleSync);
    }
  },
});
