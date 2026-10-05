"use strict";
const $ = (id) => document.getElementById(id);
const state = {token: "", papers: [], paper: null, page: 1, view: "split", chatOpen: false, config: null, reasoningEffort: "default", translations: {}, partial: {}, translationSessions: new Map(), lockUpdates: new Set(), chats: new Map(), chatController: null, importing: false, imageUrl: null, imageSequence: 0, selectionSequence: 0, importMode: "upload", file: null};
function translationSession(id) {
  if (!state.translationSessions.has(id)) state.translationSessions.set(id, {translations:{}, partial:{}, job:null, revision:0});
  return state.translationSessions.get(id);
}
function currentTranslationJob() { return state.paper ? translationSession(state.paper.id).job : null; }
function setChatOpen(open, focus = true) {
  state.chatOpen = !!open && !!state.paper;
  $("chat-panel").classList.toggle("hidden", !state.chatOpen);
  $("assistant-toggle").classList.toggle("active", state.chatOpen);
  $("assistant-toggle").setAttribute("aria-expanded", String(state.chatOpen));
  if (focus) (state.chatOpen ? $("question") : $("assistant-toggle")).focus();
}
$("assistant-toggle").addEventListener("click", () => setChatOpen(!state.chatOpen));
$("chat-close").addEventListener("click", () => setChatOpen(false));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && state.chatOpen && !document.querySelector("dialog[open]")) {
    event.preventDefault(); setChatOpen(false);
  }
});
const zoomLevels = [100,125,150,175,200,250,300];
let zoomIndex = 0;
function setZoom(index) { zoomIndex = Math.max(0, Math.min(zoomLevels.length - 1, index)); for (const zoom of zoomLevels) $("original-pane").classList.remove(`zoom-${zoom}`); $("original-pane").classList.add(`zoom-${zoomLevels[zoomIndex]}`); $("zoom-reset").textContent = `${zoomLevels[zoomIndex]}%`; $("zoom-out").disabled = zoomIndex === 0; $("zoom-in").disabled = zoomIndex === zoomLevels.length - 1; }
$("zoom-out").addEventListener("click", () => setZoom(zoomIndex - 1)); $("zoom-in").addEventListener("click", () => setZoom(zoomIndex + 1)); $("zoom-reset").addEventListener("click", () => setZoom(0));
let toastTimer;
function toast(message) { $("toast").textContent = message; $("toast").classList.remove("hidden"); clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 5000); }
function escapeHTML(text) { return String(text).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
// Render safe Markdown and LaTeX in both translations and assistant replies.
function markdown(text, citations = false) {
  return PaperReaderRichText.markdown(text, citations ? state.paper?.pages.length || 0 : 0);
}
async function api(path, options = {}) {
  const headers = {"X-PaperReader-Token": state.token, ...options.headers};
  if (options.body && !(options.body instanceof FormData)) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.body); }
  const response = await fetch(path, {...options, headers});
  if (!response.ok) { let message = `请求失败（HTTP ${response.status}）`; try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch {} throw new Error(message); }
  return response;
}
async function jsonApi(path, options) { return (await api(path, options)).json(); }
async function readStream(path, body, controller, onEvent) {
  const response = await api(path, {method: "POST", body, signal: controller.signal});
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "", complete = false;
  function consume() {
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
      let event = "message", data = [];
      for (const line of frame.split("\n")) { if (line.startsWith("event:")) event = line.slice(6).trim(); if (line.startsWith("data:")) data.push(line.slice(5).trimStart()); }
      if (!data.length) continue;
      const parsed = JSON.parse(data.join("\n"));
      if (event === "error") throw new Error(parsed.message || "生成失败，请重试。");
      if (event === "done") complete = true;
      onEvent(event, parsed);
    }
  }
  try {
    while (true) { const {done, value} = await reader.read(); if (done) {buffer += decoder.decode(); consume(); break;} buffer += decoder.decode(value, {stream:true}).replace(/\r\n/g, "\n"); consume(); }
    if (!complete && !controller.signal.aborted) throw new Error("连接提前结束，请重试。已完成内容仍保留。");
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
function showDialog(id) { if (!$(id).open) $(id).showModal(); }
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
let libraryFilter = "all";
let readingHistory = {};
try { readingHistory = JSON.parse(localStorage.getItem("paperreader-reading") || "{}"); } catch {}
function saveReading() {
  if (!state.paper) return;
  readingHistory[state.paper.id] = {page: state.page, opened: Date.now()};
  try { localStorage.setItem("paperreader-reading", JSON.stringify(readingHistory)); } catch {}
}
function showLibrary() {
  saveReading(); setChatOpen(false, false); setOutlineOpen(false);
  $("reader").classList.add("hidden"); $("welcome").classList.remove("hidden");
  document.querySelectorAll("[data-reader-tool]").forEach(el => el.classList.add("hidden"));
  $("breadcrumb").textContent = "文献库"; renderLibrary();
}
function renderLibrary() {
  const query = $("search").value.trim().toLowerCase();
  const papers = state.papers.filter(p => p.title.toLowerCase().includes(query) && (libraryFilter === "all" || p.source === libraryFilter));
  const sort = $("library-sort").value;
  papers.sort((a, b) => sort === "title" ? a.title.localeCompare(b.title) : sort === "created" ? b.created.localeCompare(a.created) : (readingHistory[b.id]?.opened || 0) - (readingHistory[a.id]?.opened || 0) || b.created.localeCompare(a.created));
  $("all-count").textContent = state.papers.length;
  $("library-title").textContent = {all:"全部论文",upload:"本地 PDF",arxiv:"arXiv 论文"}[libraryFilter];
  $("library-results").textContent = `${papers.length} 篇论文${query ? " · 搜索结果" : ""}`;
  $("workspace-library").replaceChildren();
  $("library-empty-state").classList.toggle("hidden", papers.length > 0);
  $("library-empty-state").querySelector("h2").textContent = query || libraryFilter !== "all" ? "暂无匹配的论文" : "从第一篇论文开始";
  $("paper-count").textContent = state.papers.length;
  $("library").replaceChildren();
  if (!papers.length) { const empty = document.createElement("div"); empty.className = "library-empty"; empty.textContent = query ? "没有匹配的论文" : "你的下一次发现，\n从一篇论文开始。"; $("library").append(empty); }
  // The sidebar always follows import time, independently of the main library's sort.
  const sidebarPapers = [...papers].sort((a, b) => b.created.localeCompare(a.created) || a.id.localeCompare(b.id));
  for (const paper of sidebarPapers) {
    const button = document.createElement("button"); button.className = `library-item${state.paper?.id === paper.id ? " active" : ""}`;
    const title = document.createElement("strong"); title.textContent = paper.title;
    const session = state.translationSessions.get(paper.id);
    const translated = Math.max(paper.translated_count || 0, Object.keys(session?.translations || {}).length);
    const meta = document.createElement("small"); meta.textContent = `${paper.source === "arxiv" ? "arXiv" : "PDF"} · ${paper.page_count} 页`;
    const progress = document.createElement("small"); progress.className = "library-translation-progress";
    progress.textContent = `已翻译 ${translated}/${paper.page_count} 页${paper.translation_locked ? " · 已锁定" : session?.job ? " · 翻译中" : ""}`;
    button.append(title, meta, progress); button.title = paper.title; button.addEventListener("click", () => selectPaper(paper.id).catch(e => toast(e.message))); $("library").append(button);
  }
  for (const paper of papers) {
    const row = document.createElement("button"); row.className = "paper-row"; row.setAttribute("role", "listitem");
    const last = readingHistory[paper.id];
    row.innerHTML = `<span class="paper-row-title"><span class="paper-row-icon" aria-hidden="true">▤</span><span><strong>${escapeHTML(paper.title)}</strong><small>${escapeHTML(paper.metadata?.author || "本地文献")} · ${escapeHTML(paper.created.slice(0,10))}</small></span></span><span class="paper-row-source">${paper.source === "arxiv" ? "arXiv" : "PDF"}<small>${paper.page_count} 页</small></span><span class="paper-row-progress">${last ? `读到第 ${Math.min(last.page,paper.page_count)} 页` : "未读"}<span class="progress-track"><i style="width:${last ? Math.min(100,last.page/paper.page_count*100) : 0}%"></i></span></span>`;
    row.addEventListener("click", () => selectPaper(paper.id).catch(e => toast(e.message))); $("workspace-library").append(row);
  }
}
async function refreshLibrary() { state.papers = await jsonApi("/api/papers"); renderLibrary(); }
function stopJobs(id = state.paper?.id) { state.translationSessions.get(id)?.job?.controller.abort(); state.chatController?.abort(); }
async function selectPaper(id) {
  const sequence = ++state.selectionSequence;
  if (id === state.paper?.id) { openReader(); return; }
  saveReading();
  state.chatController?.abort();
  const session = translationSession(id), revision = session.revision;
  const [paper, saved] = await Promise.all([jsonApi(`/api/papers/${id}`), jsonApi(`/api/papers/${id}/translations`)]);
  if (sequence !== state.selectionSequence) return;
  session.translations = session.job || session.revision !== revision ? {...saved, ...session.translations} : saved;
  state.paper = paper; state.page = 1; state.translations = session.translations; state.partial = session.partial;
  const listed = state.papers.find(item => item.id === id);
  if (listed) listed.translation_locked = paper.translation_locked;
  setChatOpen(false, false); setOutlineOpen(false); openReader();
  $("assistant-toggle").disabled = false;
  $("welcome").classList.add("hidden"); $("reader").classList.remove("hidden");
  $("breadcrumb").textContent = paper.title; $("paper-title").textContent = paper.title;
  $("paper-author").textContent = paper.metadata.author || "本地论文";
  $("paper-pages").textContent = `${paper.pages.length} 页`;
  $("paper-source").textContent = paper.source === "arxiv" ? `ARXIV · ${paper.metadata.arxiv_id}` : "PDF · 本地论文";
  $("page-total").textContent = `/ ${paper.pages.length}`; $("page-number").max = paper.pages.length;
  renderLibrary(); renderChat(); updateModel(); buildArticle(); buildOutline(); setPage(readingHistory[id]?.page || 1);
  if (paper.metadata.empty_pages?.length) toast(`第 ${paper.metadata.empty_pages.join("、")} 页没有文字层；可查看原文图像。`);
}
async function renderImage(figureTarget = null) {
  if (!state.paper) return;
  const seq = ++state.imageSequence, id = state.paper.id, page = state.page;
  $("pdf-page").classList.add("loading"); $("image-error").classList.add("hidden");
  $("pdf-figure-highlight").classList.add("hidden");
  try {
    const response = await api(`/api/papers/${id}/page/${page}/image`), blob = await response.blob();
    if (seq !== state.imageSequence) return;
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
    state.imageUrl = URL.createObjectURL(blob); $("pdf-page").src = state.imageUrl; $("pdf-page").alt = `论文原文第 ${page} 页`;
    await $("pdf-page").decode();
    if (seq !== state.imageSequence) return;
    if (figureTarget) {
      const [left, top, right, bottom] = figureTarget.bbox;
      const highlight = $("pdf-figure-highlight");
      Object.assign(highlight.style, {left:`${left * 100}%`, top:`${top * 100}%`, width:`${(right-left) * 100}%`, height:`${(bottom-top) * 100}%`});
      highlight.classList.remove("hidden"); highlight.scrollIntoView({block:"center", inline:"nearest"});
    } else { $("pdf-scroll").scrollTop = 0; $("pdf-scroll").scrollLeft = 0; }
  } catch (e) { if (seq === state.imageSequence) { $("image-error").classList.remove("hidden"); toast(e.message); } }
  finally { if (seq === state.imageSequence) $("pdf-page").classList.remove("loading"); }
}
function setPage(page) {
  if (!state.paper) return;
  state.page = Math.min(state.paper.pages.length, Math.max(1, Number.isFinite(Number(page)) ? Math.trunc(Number(page)) : 1));
  $("page-number").value = state.page; $("page-prev").disabled = state.page === 1; $("page-next").disabled = state.page === state.paper.pages.length;
  saveReading(); updateReadingPosition(); renderImage(); renderTranslation();
  $("translation-text").querySelector(`[data-reading-page="${state.page}"]`)?.scrollIntoView({block:"start"});
  // Keep the selected page when a short final page cannot reach the scroll area's top.
  readingScrollTarget = $("reading-scroll").scrollTop;
}
function setView(view) { state.view = view; document.querySelectorAll("[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === view)); $("original-pane").classList.toggle("hidden", view === "translation"); $("translation-pane").classList.toggle("hidden", view === "original"); $("reading-splitter").classList.toggle("hidden", view !== "split"); $("document-content").classList.toggle("split-view", view === "split"); }
function buildArticle() {
  $("translation-text").replaceChildren();
  state.paper.pages.forEach((page, i) => {
    const section = document.createElement("section"); section.className = "reading-page prose"; section.dataset.readingPage = i + 1;
    section.innerHTML = `<div class="reading-page-label">第 ${i + 1} 页 <span></span></div><div class="page-body"></div>`;
    $("translation-text").append(section);
  });
}
function updateReadingPosition() {
  $("page-number").value = state.page; $("pdf-page-label").textContent = `${state.page} / ${state.paper.pages.length}`;
  $("page-prev").disabled = state.page === 1; $("page-next").disabled = state.page === state.paper.pages.length;
  $("reading-progress-bar").style.width = `${state.page / state.paper.pages.length * 100}%`;
  document.querySelectorAll("[data-outline-page]").forEach(el => el.classList.toggle("active", Number(el.dataset.outlinePage) === state.page));
}
function renderTranslation() {
  if (!state.paper) return;
  const partial = state.partial[state.page], saved = state.translations[state.page];
  state.paper.pages.forEach((page, i) => {
    const number = i + 1, section = $("translation-text").children[i];
    const body = state.partial[number] ?? state.translations[number];
    const layout = state.paper.metadata.reading_layout?.pages[i];
    const text = body === undefined ? layout?.text || (typeof page === "string" ? page : page.text) || "本页没有文字层，请查看原始 PDF。" : body;
    const streaming = state.partial[number] !== undefined && !!currentTranslationJob();
    if (section.readingText !== text || section.streaming !== streaming) {
      const pageBody = section.querySelector(".page-body");
      pageBody.innerHTML = PaperReaderRichText.markdown(text, 0, {page:number, figures:layout?.figures || []});
      if (streaming) {
        const marker = document.createElement("span"); marker.className = "streaming-marker";
        pageBody.insertBefore(marker, pageBody.querySelector(".page-footnotes"));
      }
      section.readingText = text; section.streaming = streaming;
    }
    section.querySelector(".reading-page-label span").textContent = body === undefined ? "英文原文 · 待翻译" : "中文译文";
  });
  const job = currentTranslationJob();
  translationBusy(!!job);
  const locked = state.paper.translation_locked;
  const status = job ? `正在翻译第 ${job.page} 页 · ${job.completed}/${job.total}` : partial !== undefined ? `本页译文未完成，${locked ? "解锁后" : "可"}重新翻译` : `已翻译 ${Object.keys(state.translations).length} / ${state.paper.pages.length} 页`;
  $("translation-status").textContent = `${locked ? "翻译已锁定 · " : ""}${status}`;
}
$("translation-text").addEventListener("click", event => {
  const button = event.target.closest(".figure-placeholder");
  if (button && state.paper) {
    const page = Number(button.dataset.figurePage);
    const figure = state.paper.metadata.reading_layout?.pages[page - 1]?.figures.find(f => f.id === button.dataset.figure);
    if (!figure || !Array.isArray(figure.bbox) || figure.bbox.length !== 4 || !figure.bbox.every(v => Number.isFinite(v) && v >= 0 && v <= 1)) return;
    setChatOpen(false, false); setView("split"); state.page = page; saveReading(); updateReadingPosition(); renderImage(figure);
    return;
  }
  const link = event.target.closest('.footnote-ref a, .footnote-back');
  if (link) {
    const target = document.getElementById(link.getAttribute("href").slice(1));
    if (target) { event.preventDefault(); target.scrollIntoView({block:"center"}); target.setAttribute("tabindex", "-1"); target.focus({preventScroll:true}); }
  }
});
$("search").addEventListener("input", renderLibrary);
$("library-sort").addEventListener("change", renderLibrary);
$("reader-back").addEventListener("click", showLibrary);
document.querySelectorAll("[data-library-filter]").forEach(button => button.addEventListener("click", () => {libraryFilter = button.dataset.libraryFilter; document.querySelectorAll("[data-library-filter]").forEach(el => el.classList.toggle("active", el === button)); showLibrary();}));
function openReader() { $("welcome").classList.add("hidden"); $("reader").classList.remove("hidden"); document.querySelectorAll("[data-reader-tool]").forEach(el => el.classList.remove("hidden")); $("breadcrumb").textContent = state.paper.title; setView(state.view); if (window.innerWidth <= 540 && !document.querySelector(".app").classList.contains("sidebar-collapsed")) $("sidebar-toggle").click(); }
$("sidebar-toggle").addEventListener("click", () => {const collapsed = document.querySelector(".app").classList.toggle("sidebar-collapsed"); $("library-sidebar").inert = collapsed; $("sidebar-toggle").setAttribute("aria-expanded", String(!collapsed)); $("sidebar-toggle").setAttribute("aria-label", collapsed ? "展开文献库导航" : "收起文献库导航");});
function setOutlineOpen(open) { $("outline-panel").classList.toggle("hidden", !open); $("outline-toggle").setAttribute("aria-expanded", String(open)); }
$("outline-toggle").addEventListener("click", () => setOutlineOpen($("outline-panel").classList.contains("hidden")));
$("outline-close").addEventListener("click", () => setOutlineOpen(false));
function buildOutline() {
  $("outline-items").replaceChildren();
  state.paper.pages.forEach((page, i) => {
    const text = typeof page === "string" ? page : page.text || "";
    const headings = text.split("\n").map(line => line.trim()).filter(line => /^(abstract|摘要|\d+(?:\.\d+)*[.\s]+[A-Z\u4e00-\u9fff]|references|conclusions?)/i.test(line) && line.length < 100).slice(0,4);
    for (const title of headings.length ? headings : [`第 ${i + 1} 页`]) {const button = document.createElement("button"); button.dataset.outlinePage = i + 1; button.textContent = `${title} · ${i + 1}`; button.addEventListener("click", () => {setPage(i+1); setOutlineOpen(false);}); $("outline-items").append(button);}
  });
}
let scrollTick, readingScrollTarget = null;
$("reading-scroll").addEventListener("scroll", () => {
  cancelAnimationFrame(scrollTick);
  scrollTick = requestAnimationFrame(() => {
    if (!state.paper || state.view === "original") return;
    if (readingScrollTarget !== null && Math.abs($("reading-scroll").scrollTop - readingScrollTarget) < 1) return;
    readingScrollTarget = null;
    const top = $("reading-scroll").getBoundingClientRect().top + 85;
    let current = 1;
    for (const section of $("translation-text").children) {if (section.getBoundingClientRect().top <= top) current = Number(section.dataset.readingPage); else break;}
    if (current !== state.page) {state.page = current; saveReading(); updateReadingPosition(); if ($("follow-reading").checked) renderImage();}
  });
});
$("follow-reading").addEventListener("change", () => {if ($("follow-reading").checked) renderImage();});
$("pdf-close").addEventListener("click", () => setView("translation"));
function applyReadingSettings() { document.documentElement.style.setProperty("--reading-size", `${$("reading-font-size").value}px`); document.documentElement.style.setProperty("--reading-font", "var(--serif)"); $("reading-font-label").textContent = `${$("reading-font-size").value} px`; try {localStorage.setItem("paperreader-typography", JSON.stringify({size:$("reading-font-size").value,font:"serif"}));} catch {} }
$("reading-settings-open").addEventListener("click", () => showDialog("reading-settings-dialog"));
$("reading-font-size").addEventListener("input", applyReadingSettings); $("reading-font-family").addEventListener("change", applyReadingSettings);
$("reading-reset").addEventListener("click", () => {$("reading-font-size").value = 18; $("reading-font-family").value = "serif"; applyReadingSettings();});
try { const saved = JSON.parse(localStorage.getItem("paperreader-typography") || "null"); if (saved) {$("reading-font-size").value = saved.size; $("reading-font-family").value = "serif";} } catch {}
applyReadingSettings();
let splitRatio = 50;
function setSplitRatio(value) {splitRatio = Math.max(30,Math.min(70,value)); $("document-content").style.setProperty("--split-ratio", `${splitRatio}%`); $("reading-splitter").setAttribute("aria-valuenow", String(Math.round(splitRatio)));}
$("reading-splitter").addEventListener("pointerdown", event => {event.preventDefault(); $("reading-splitter").setPointerCapture(event.pointerId);});
$("reading-splitter").addEventListener("pointermove", event => {if (!$("reading-splitter").hasPointerCapture(event.pointerId)) return; const rect = $("document-content").getBoundingClientRect(); setSplitRatio((event.clientX-rect.left)/rect.width*100);});
$("reading-splitter").addEventListener("dblclick", () => setSplitRatio(50));
$("reading-splitter").addEventListener("keydown", event => {if (["ArrowLeft","ArrowRight","Home"].includes(event.key)) {event.preventDefault(); setSplitRatio(event.key === "Home" ? 50 : splitRatio + (event.key === "ArrowLeft" ? -2 : 2));}});
$("page-prev").addEventListener("click", () => setPage(state.page - 1)); $("page-next").addEventListener("click", () => setPage(state.page + 1));
$("page-number").addEventListener("change", e => setPage(e.target.value));
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
function openImport(mode = "upload") { setImportMode(mode); $("import-error").textContent = ""; showDialog("import-dialog"); }
function setImportMode(mode) { state.importMode = mode; document.querySelectorAll("[data-import]").forEach(b => b.classList.toggle("active", b.dataset.import === mode)); $("upload-section").classList.toggle("hidden", mode !== "upload"); $("arxiv-section").classList.toggle("hidden", mode !== "arxiv"); }
$("import-open").addEventListener("click", () => openImport()); $("welcome-upload").addEventListener("click", () => openImport()); $("welcome-arxiv").addEventListener("click", () => openImport("arxiv"));
document.querySelectorAll("[data-import]").forEach(b => b.addEventListener("click", () => setImportMode(b.dataset.import)));
function chooseFile(file) { if (!file) return; if (!file.name.toLowerCase().endsWith(".pdf")) {$("import-error").textContent = "请选择 PDF 文件。"; return;} if (file.size > 50 * 1024 * 1024) {$("import-error").textContent = "PDF 不能超过 50 MB。"; return;} state.file = file; $("file-label").textContent = file.name; $("import-error").textContent = ""; }
$("file-input").addEventListener("change", e => chooseFile(e.target.files[0]));
$("dropzone").addEventListener("dragover", e => {e.preventDefault(); $("dropzone").classList.add("dragging");});
$("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("dragging"));
$("dropzone").addEventListener("drop", e => {e.preventDefault(); $("dropzone").classList.remove("dragging"); chooseFile(e.dataTransfer.files[0]);});
$("import-form").addEventListener("submit", async e => {
  e.preventDefault(); if (state.importing) return;
  if (state.importMode === "upload" && !state.file) {$("import-error").textContent = "请先选择 PDF 文件。"; return;}
  if (state.importMode === "arxiv" && !$("arxiv-id").value.trim()) {$("import-error").textContent = "请输入 arXiv 编号或链接。"; return;}
  state.importing = true; $("import-submit").disabled = true; $("import-error").textContent = ""; $("import-progress").textContent = state.importMode === "arxiv" ? "正在下载与解析论文…" : "正在解析 PDF…";
  try {
    let paper;
    if (state.importMode === "upload") { const body = new FormData(); body.append("file", state.file); paper = await jsonApi("/api/papers/upload", {method:"POST", body}); }
    else paper = await jsonApi("/api/papers/arxiv", {method:"POST", body:{arxiv_id:$("arxiv-id").value.trim()}});
    await refreshLibrary(); await selectPaper(paper.id); $("import-dialog").close(); toast("论文已加入你的论文库"); state.file = null; $("file-input").value = ""; $("file-label").textContent = "点击选择，或把 PDF 拖到这里";
  } catch (error) {$("import-error").textContent = error.message;}
  finally {state.importing = false; $("import-submit").disabled = false; $("import-progress").textContent = "导入后即可阅读原文";}
});
function openSettings() {
  let saved; try { saved = JSON.parse(localStorage.getItem("paperreader-preferences") || "null"); } catch {}
  const config = state.config || saved || {base_url:"https://api.openai.com/v1",model:""};
  $("base-url").value = config.base_url; $("model").value = config.model; $("api-key").value = state.config?.api_key || ""; $("settings-error").textContent = ""; $("settings-error").classList.remove("success"); showDialog("settings-dialog");
  $("reasoning-effort").value = state.reasoningEffort;
  $("api-key").type = "password"; $("key-toggle").textContent = "显示";
}
$("settings-open").addEventListener("click", openSettings); $("settings-top").addEventListener("click", openSettings);
$("provider").addEventListener("change", e => { const presets = {openai:{base_url:"https://api.openai.com/v1",model:""},deepseek:{base_url:"https://api.deepseek.com/v1",model:"deepseek-chat"},local:{base_url:"http://localhost:11434/v1",model:""}}; const p = presets[e.target.value]; if (p) {$("base-url").value = p.base_url; $("model").value = p.model; if (e.target.value === "local") $("api-key").value = "local";} });
$("key-toggle").addEventListener("click", () => { const show = $("api-key").type === "password"; $("api-key").type = show ? "text" : "password"; $("key-toggle").textContent = show ? "隐藏" : "显示"; });
function configFromForm() { return {base_url:$("base-url").value.trim().replace(/\/+$/, ""), model:$("model").value.trim(), api_key:$("api-key").value.trim(), reasoning_effort:$("reasoning-effort").value}; }
function validConfig(config) { if (!config.api_key || !config.model) throw new Error("请输入 API Key 和模型名称。"); let url; try {url = new URL(config.base_url);} catch {throw new Error("请输入有效的 API Base URL。");} if (!["https:","http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("API Base URL 格式不正确。"); if (url.protocol === "http:" && !["localhost","127.0.0.1","[::1]"].includes(url.hostname)) throw new Error("远程 API 请使用 HTTPS；本地模型可使用 HTTP。"); }
function requireConfig() { if (state.config) return true; openSettings(); toast("请先配置 API Key 和模型"); return false; }
const reasoningLabels = {default:"服务默认", none:"关闭", minimal:"极低", low:"低", medium:"中", high:"高", xhigh:"极高", max:"最高"};
function updateModel() { $("api-indicator").textContent = state.config ? "已配置" : "未配置"; $("api-indicator").classList.toggle("configured", !!state.config); $("chat-model").textContent = state.config ? `${state.config.model} · 思考强度：${reasoningLabels[state.config.reasoning_effort || "default"]}` : "使用你的模型，读懂这篇论文"; }
function savePreferences(config) { try {localStorage.setItem("paperreader-preferences", JSON.stringify({base_url:config.base_url, model:config.model, reasoning_effort:config.reasoning_effort}));} catch {} }
$("reasoning-effort").addEventListener("change", async () => {
  const select = $("reasoning-effort"), button = $("settings-form").querySelector('[type="submit"]');
  const effort = select.value, previous = state.reasoningEffort;
  state.reasoningEffort = effort; select.disabled = true; button.disabled = true;
  $("settings-error").textContent = ""; $("settings-error").classList.remove("success");
  try {
    const saved = await jsonApi("/api/config/reasoning-effort", {method:"PATCH", body:{reasoning_effort:effort}, keepalive:true});
    $("settings-storage-path").textContent = saved.path;
    if (state.config) {state.config = {...state.config, reasoning_effort:effort}; savePreferences(state.config);}
    updateModel();
  } catch (error) {
    state.reasoningEffort = previous; select.value = previous;
    if ($("settings-dialog").open) $("settings-error").textContent = error.message; else toast(error.message);
  } finally {select.disabled = false; button.disabled = false;}
});
$("settings-form").addEventListener("submit", async e => {
  e.preventDefault(); const config = configFromForm();
  const button = $("settings-form").querySelector('[type="submit"]'); button.disabled = true; $("reasoning-effort").disabled = true;
  try { validConfig(config); const saved = await jsonApi("/api/config", {method:"POST", body:{config}}); $("settings-storage-path").textContent = saved.path; state.chatController?.abort(); state.config = config; state.reasoningEffort = config.reasoning_effort; savePreferences(config); updateModel(); $("settings-dialog").close(); toast("API 设置已保存到本机，下次启动自动恢复；已有翻译任务继续使用原配置"); } catch (error) { if ($("settings-dialog").open) $("settings-error").textContent = error.message; else toast(error.message); } finally {button.disabled = false; $("reasoning-effort").disabled = false;}
});
$("test-api").addEventListener("click", async () => { const config = configFromForm(); $("settings-error").classList.remove("success"); try { validConfig(config); $("test-api").disabled = true; $("test-api").textContent = "连接中…"; const result = await jsonApi("/api/config/test", {method:"POST",body:{config}}); $("settings-error").textContent = result.message; $("settings-error").classList.add("success"); } catch (error) {$("settings-error").textContent = error.message;} finally {$("test-api").disabled = false; $("test-api").textContent = "测试连接";} });
function translationBusy(busy) {
  const locked = !!state.paper?.translation_locked, saving = state.lockUpdates.has(state.paper?.id);
  for (const id of ["translate-current", "translate-from-current", "translate-all", "retranslate-all"]) $(id).disabled = busy || locked || saving;
  $("translate-stop").classList.toggle("hidden", !busy);
  $("translation-lock").disabled = saving;
  $("translation-lock").setAttribute("aria-pressed", String(locked));
  $("translation-lock").setAttribute("aria-label", locked ? "解锁这篇论文的翻译" : "锁定这篇论文的翻译");
  $("translation-lock").title = locked ? "解锁后可继续翻译或重译这篇论文" : "锁定后禁止翻译和重译，并停止这篇论文正在运行的翻译";
  $("translation-lock-label").textContent = `翻译锁：${locked ? "已锁定" : "已解锁"}`;
  $("translation-lock-shackle").setAttribute("d", locked ? "M8 10V6a4 4 0 0 1 8 0v4" : "M8 10V6a4 4 0 0 1 8 0");
}
$("translation-lock").addEventListener("click", async () => {
  const paper = state.paper;
  if (!paper || state.lockUpdates.has(paper.id)) return;
  const id = paper.id;
  state.lockUpdates.add(id); translationBusy(!!currentTranslationJob());
  try {
    const result = await jsonApi(`/api/papers/${id}/translation-lock`, {method:"PATCH", body:{locked:!paper.translation_locked}});
    paper.translation_locked = result.locked;
    const listed = state.papers.find(item => item.id === id);
    if (listed) listed.translation_locked = result.locked;
    if (state.paper?.id === id) state.paper.translation_locked = result.locked;
    const session = state.translationSessions.get(id), job = session?.job;
    if (result.locked && job) {job.locked = true; job.controller.abort();}
    if (result.locked && session) for (const page of Object.keys(session.partial)) delete session.partial[page];
    renderLibrary();
    toast(`《${paper.title}》\n${result.locked ? "翻译已锁定，已完成译文已保存" : "翻译已解锁，可以继续翻译或重译"}`);
  } catch (error) {toast(error.message);}
  finally {state.lockUpdates.delete(id); if (state.paper?.id === id) renderTranslation();}
});
async function translate(scope, replaceAll = false) {
  if (!state.paper || state.paper.translation_locked || state.lockUpdates.has(state.paper.id) || currentTranslationJob() || !requireConfig()) return;
  const paper = state.paper, id = paper.id, config = {...state.config}, session = translationSession(id);
  const startPage = scope === "all" ? 1 : state.page;
  const pages = scope === "current" ? [startPage] : paper.pages.map((_,i) => i + 1).slice(startPage - 1);
  const controller = new AbortController(), job = {controller, completed:0, total:pages.length, page:pages[0]};
  for (const page of pages) delete session.partial[page];
  session.job = job; renderTranslation(); renderLibrary(); setView(state.view === "original" ? "split" : state.view);
  const force = replaceAll || (scope === "current" && session.translations[startPage] !== undefined);
  try {
    await readStream(`/api/papers/${id}/translate`, {config,pages,force}, controller, (event,data) => {
      if (session.job !== job || controller.signal.aborted) return;
      if (data.page) job.page = data.page;
      if (event === "page_start") session.partial[data.page] = "";
      if (event === "delta") session.partial[data.page] = (session.partial[data.page] || "") + data.text;
      if (event === "page_done") {session.translations[data.page] = data.text; delete session.partial[data.page]; session.revision++; job.completed++; renderLibrary();}
      if (state.paper?.id === id && data.page) renderTranslation();
    });
    const label = scope === "all" ? "全文" : scope === "from-current" ? `第 ${startPage} 页及之后页面` : "本页";
    if (!controller.signal.aborted) toast(`《${paper.title}》\n${label}翻译完成，译文已保存`);
  } catch (error) {if (!job.locked) toast(`《${paper.title}》\n${error.name === "AbortError" ? "翻译已停止，已完成页的译文已保存" : error.message}`);}
  finally {if (session.job === job) {session.job = null; renderLibrary(); if (state.paper?.id === id) renderTranslation();}}
}
$("translate-current").addEventListener("click", () => translate("current")); $("translate-from-current").addEventListener("click", () => translate("from-current")); $("translate-all").addEventListener("click", () => translate("all")); $("retranslate-all").addEventListener("click", () => translate("all", true)); $("translate-stop").addEventListener("click", () => currentTranslationJob()?.controller.abort());
function downloadBlob(blob, name) { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
function safeName(title) {return title.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0,100);}
$("export-translation").addEventListener("click", () => {if (!state.paper) return; const entries = Object.entries(state.translations).sort((a,b) => Number(a[0])-Number(b[0])); if (!entries.length) {toast("还没有已完成的译文，请先翻译。"); return;} const text = `# ${state.paper.title}\n\n` + entries.map(([page,content]) => `## 第 ${page} 页\n\n${content}`).join("\n\n---\n\n"); downloadBlob(new Blob([text], {type:"text/markdown;charset=utf-8"}), `${safeName(state.paper.title)}-中文译文.md`); });
$("pdf-download").addEventListener("click", async () => {if (!state.paper) return; const id = state.paper.id, name = state.paper.metadata.filename || "paper.pdf"; try {const res = await api(`/api/papers/${id}/pdf`); downloadBlob(await res.blob(), name);} catch (e) {toast(e.message);} });
function messagesForPaper() { if (!state.paper) return []; if (!state.chats.has(state.paper.id)) state.chats.set(state.paper.id, []); return state.chats.get(state.paper.id); }
function renderChat() {
  const container = $("chat-messages"); const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  const messages = messagesForPaper();
  if (!messages.length) {container.innerHTML = '<div class="chat-empty"><div class="assistant-art">✦</div><h3>读论文，也可以是对话</h3><p>从核心贡献到实验细节，<br>把你的问题交给论文助手。</p><div class="suggestions"><button data-question="这篇论文的核心贡献是什么？">这篇论文的核心贡献是什么？ <span>↗</span></button><button data-question="请解释论文提出的方法。">帮我理解论文的方法 <span>↗</span></button><button data-question="论文的实验结果和局限性是什么？">看看实验结果与局限 <span>↗</span></button></div></div>'; return;}
  container.replaceChildren();
  for (const message of messages) {
    const item = document.createElement("div"); item.className = `message ${message.role}${message.error ? " error" : ""}`;
    const label = document.createElement("div"); label.className = "message-label"; label.textContent = message.role === "user" ? "你" : "✦ 论文助手";
    const body = document.createElement("div"); body.className = "message-body prose";
    if (message.role === "user") body.textContent = message.content;
    else body.innerHTML = message.content ? markdown(message.content, true) : '<span class="message-pending">正在阅读论文…</span>';
    if (message.error) {const error = document.createElement("p"); error.className = "inline-error"; error.textContent = message.error; body.append(error);}
    item.append(label, body);
    if (message.sources?.length) { const details = document.createElement("details"); details.className = "sources-toggle"; const summary = document.createElement("summary"); summary.textContent = `检索参考 · ${new Set(message.sources.map(s => s.page)).size} 页`; details.append(summary); for (const source of message.sources) {const excerpt = document.createElement("div"); excerpt.className = "source-excerpt"; const cite = document.createElement("button"); cite.className = "citation"; cite.dataset.cite = source.page; cite.textContent = `第 ${source.page} 页 ↗`; const text = document.createElement("p"); text.textContent = source.text; excerpt.append(cite,text); details.append(excerpt);} item.append(details); }
    container.append(item);
  }
  if (nearBottom || state.chatController) container.scrollTop = container.scrollHeight;
}
$("chat-messages").addEventListener("click", e => {const cite = e.target.closest("[data-cite]"); if (cite) {setPage(Number(cite.dataset.cite)); setView("original"); setChatOpen(false);} const suggestion = e.target.closest("[data-question]"); if (suggestion) {$("question").value = suggestion.dataset.question; sendQuestion();} });
function chatBusy(busy) {$("chat-send").classList.toggle("hidden", busy); $("chat-stop").classList.toggle("hidden", !busy); $("chat-clear").disabled = busy;}
async function sendQuestion() {
  const question = $("question").value.trim(); if (!question || !state.paper || state.chatController || !requireConfig()) return;
  if (question.length > 4000) {toast("问题不能超过 4000 个字符。"); return;}
  const id = state.paper.id, config = state.config, messages = messagesForPaper();
  const history = messages.filter(m => m.content && !m.error && !m.incomplete).slice(-12).map(({role,content}) => ({role,content:content.slice(0,12000)}));
  messages.push({role:"user",content:question}); const reply = {role:"assistant",content:"",sources:[],incomplete:true}; messages.push(reply);
  $("question").value = ""; const controller = new AbortController(); state.chatController = controller; chatBusy(true); renderChat();
  try { await readStream(`/api/papers/${id}/chat`, {config,question,history}, controller, (event,data) => { if (controller.signal.aborted) return; if (event === "delta") reply.content += data.text; if (event === "sources") reply.sources = data.sources; if (event === "done") reply.incomplete = false; if (state.paper?.id === id) {if (event === "status") $("chat-status").textContent = data.message; if (event === "delta") $("chat-status").textContent = "正在生成回答…"; renderChat();} }); }
  catch (error) {reply.error = error.name === "AbortError" ? "回答已停止，以上内容可能不完整。" : error.message; if (state.paper?.id === id) renderChat();}
  finally {if (state.chatController === controller) {state.chatController = null; chatBusy(false); $("chat-status").textContent = "";}}
}
$("chat-send").addEventListener("click", sendQuestion); $("chat-stop").addEventListener("click", () => state.chatController?.abort());
$("question").addEventListener("keydown", e => {if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {e.preventDefault(); sendQuestion();} });
$("chat-clear").addEventListener("click", () => {if (!state.paper || state.chatController) return; state.chats.set(state.paper.id, []); renderChat();});
$("paper-delete").addEventListener("click", () => {if (state.paper) showDialog("delete-dialog");});
$("delete-confirm").addEventListener("click", async () => {if (!state.paper) return; const id = state.paper.id; $("delete-confirm").disabled = true; stopJobs(); try {await jsonApi(`/api/papers/${id}`, {method:"DELETE"}); if (state.paper?.id === id) {setChatOpen(false, false); state.paper = null; $("assistant-toggle").disabled = true; ++state.selectionSequence; ++state.imageSequence; state.translations = {}; state.partial = {}; $("reader").classList.add("hidden"); $("welcome").classList.remove("hidden"); delete readingHistory[id]; showLibrary();} state.chats.delete(id); state.translationSessions.delete(id); await refreshLibrary(); $("delete-dialog").close(); toast("论文与译文已删除");} catch (e) {toast(e.message);} finally {$("delete-confirm").disabled = false;} });
window.addEventListener("beforeunload", () => {saveReading(); for (const session of state.translationSessions.values()) session.job?.controller.abort(); state.chatController?.abort();});
if (window.innerWidth <= 540) $("sidebar-toggle").click();
async function init() {try { const res = await fetch("/api/session"); if (!res.ok) throw new Error("无法初始化，请重新启动 PaperReader。"); const session = await res.json(); state.token = session.token; $("app-version").textContent = `v${session.version}`; try {const saved = await jsonApi("/api/config"); state.config = saved.config; state.reasoningEffort = saved.reasoning_effort || saved.config?.reasoning_effort || "default"; $("settings-storage-path").textContent = saved.path; if (saved.error) toast(saved.error);} catch (e) {toast(e.message);} updateModel(); await refreshLibrary(); } catch (e) {toast(e.message);} }
init();
