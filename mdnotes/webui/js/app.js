/* ===========================================================
   MDNotes Web · 前端主控制器
   阶段2：多标签 / 补全 / 图片 / 大纲 / 快捷键
   阶段3：全文搜索 / 标签云 / 反向链接 / 未关联 / 回收站 / 快速切换器(Ctrl+P)
   =========================================================== */
(function () {
  "use strict";

  var state = {
    currentRel: null,
    dirty: false,
    notesDir: "",
    tabs: {},
    tabOrder: [],
    noteNames: [],
    noteAliases: [],
    codeLangs: [],
    fontSize: 14,
    typewriterScroll: true,
    scrollLinked: true,
    _syncLock: false,
    autoSave: true,
    autoSaveDelayMs: 2000,
    heatDays: 120,
    theme: "genshin",
    restoreSession: true,
    keymap: {},
    _lockShowing: false,
  };

  var editor = null;
  var previewTimer = null, outlineTimer = null, saveTimer = null, searchTimer = null, autoSaveTimer = null, ovTimer = null;

  // ── 后端调用（Electron 混合架构：本地 HTTP 服务） ────
  // 由 electron_server.py 提供 http://127.0.0.1:<随机端口>/api/<method>
  // 携带 X-Auth-Token（Electron 主进程随机生成，经 preload 下发），后端据此鉴权。
  var API_BASE = null;
  var API_TOKEN = null;
  function apiBase() {
    if (API_BASE) return Promise.resolve(API_BASE);
    if (typeof window.desktop !== "undefined" && window.desktop.apiBase) {
      return window.desktop.apiBase().then(function (b) { API_BASE = b; return b; });
    }
    return Promise.reject(new Error("未连接后端服务"));
  }
  function apiToken() {
    if (API_TOKEN) return Promise.resolve(API_TOKEN);
    if (typeof window.desktop !== "undefined" && window.desktop.apiToken) {
      return window.desktop.apiToken().then(function (t) { API_TOKEN = t; return t; });
    }
    return Promise.resolve(""); // 非 Electron 环境（浏览器直连调试）无令牌，后端允许空令牌时可用
  }
  function call(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    return Promise.all([apiBase(), apiToken()]).then(function (res) {
      var base = res[0], token = res[1];
      var headers = { "Content-Type": "application/json" };
      if (token) headers["X-Auth-Token"] = token;
      // 超时保护：后端异常卡死时不至于让 UI 永久挂起
      var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 20000) : null;
      return fetch(base + "/api/" + encodeURIComponent(method), {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ args: args }),
        signal: ctrl ? ctrl.signal : undefined,
      }).then(function (r) {
        return r.json().catch(function () {
          return { success: false, error: "HTTP " + r.status };
        }).then(function (data) {
          if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
          return data;
        });
      }).then(function (v) { if (timer) clearTimeout(timer); return v; },
              function (e) { if (timer) clearTimeout(timer); throw e; });
    });
  }
  // 原生对话框（Electron 主进程提供）；返回 Promise<string|null>
  function pickFolder(initial) {
    if (typeof window.desktop !== "undefined" && window.desktop.pickFolder) return window.desktop.pickFolder(initial || "");
    return Promise.resolve(null);
  }
  function pickSaveFile(name) {
    if (typeof window.desktop !== "undefined" && window.desktop.pickSaveFile) return window.desktop.pickSaveFile(name || "");
    return Promise.resolve(null);
  }

  function el(id) { return document.getElementById(id); }
  // 安全绑定：任一按钮 id 缺失也不会中断其余绑定（避免连锁崩溃）
  function on(id, evt, handler) {
    var node = el(id);
    if (!node) { log_once("按钮未找到，跳过绑定: " + id); return; }
    node.addEventListener(evt, handler);
  }
  var _warned = {};
  function log_once(msg) { if (!_warned[msg]) { _warned[msg] = 1; console.warn(msg); } }
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  var esc = escapeHtml;

  // ── 编辑器初始化 ──────────────────────────────────────
  function initEditor() {
    editor = CodeMirror(el("editor-host"), {
      value: "",
      mode: "markdown",
      theme: "material-darker",
      lineNumbers: true,
      lineWrapping: true,
      autoCloseBrackets: true,
      matchBrackets: true,
      tabSize: 4,
      indentUnit: 4,
      extraKeys: {
        "Ctrl-S": function () { doSave(); },
        "Ctrl-B": function () { wrapSelection("**", "**"); },
        "Ctrl-I": function () { wrapSelection("*", "*"); },
        "Ctrl-K": function () { showCommandPalette(); },
        "Ctrl-Shift-K": function () { insertCodeBlock(); },
        "Ctrl-=": function () { zoom(1); },
        "Ctrl--": function () { zoom(-1); },
        "Esc": function () { if (editor.state.completionActive) editor.closeHint(); },
      },
    });
    editor.on("change", function (cm, change) {
      // setValue 触发的 change（打开笔记/恢复草稿）不算用户编辑，避免误标脏
      if (change && change.origin === "setValue") return;
      if (!state.dirty) { state.dirty = true; updateSaveStatus(); markTabDirty(true); }
      schedulePreview();
      scheduleOutline();
      updateStats();
      scheduleAutoSave();
    });
    editor.on("cursorActivity", function () {
      maybeTriggerCompletion();
      if (state.typewriterScroll) scrollCursorToCenter(false);
    });
    // 编辑区滚动 → 预览区按比例联动
    editor.on("scroll", syncPreviewFromEditor);
    setupImagePaste();
  }

  // 滚动联动：编辑区 ↔ 预览区按滚动比例双向同步
  function _scrollRatio(node) {
    var max = node.scrollHeight - node.clientHeight;
    return max > 0 ? node.scrollTop / max : 0;
  }
  function _setScrollRatio(node, ratio) {
    var max = node.scrollHeight - node.clientHeight;
    node.scrollTop = max > 0 ? ratio * max : 0;
  }
  function syncPreviewFromEditor() {
    if (!editor || state._syncLock || !state.scrollLinked) return;
    var pv = el("preview");
    if (!pv) return;
    state._syncLock = true;
    _setScrollRatio(pv, _scrollRatio(editor.getScrollerElement()));
    state._syncLock = false;
  }
  function syncEditorFromPreview() {
    if (!editor || state._syncLock || !state.scrollLinked) return;
    // 预览渲染后的位置恢复不算用户滚动：跳过编辑器联动，避免把编辑区一并拉回
    if (state._renderRestore) return;
    var scroller = editor.getScrollerElement();
    var max = scroller.scrollHeight - scroller.clientHeight;
    state._syncLock = true;
    editor.scrollTo(null, max > 0 ? _scrollRatio(el("preview")) * max : 0);
    state._syncLock = false;
  }

  // 打字机滚动：光标移出可视区时，将当前行滚动到视口中央
  function scrollCursorToCenter(force) {
    if (!editor) return;
    var scroller = editor.getScrollerElement();
    var scrollTop = scroller.scrollTop;
    var viewH = scroller.clientHeight;
    var coords = editor.cursorCoords(null, "local");
    var lineTop = coords.top - scrollTop;
    var lineBottom = coords.bottom - scrollTop;
    // 仅在光标已离开可视区（或强制时）才滚动，避免正常输入时抖动
    var outOfView = lineBottom < 0 || lineTop > viewH;
    if (!force && !outOfView) return;
    var target = scrollTop + (lineTop + lineBottom) / 2 - viewH / 2;
    scroller.scrollTop = target;
  }

  function wrapSelection(left, right) {
    var sel = editor.getSelection();
    if (sel) editor.replaceSelection(left + sel + right);
    else {
      editor.replaceSelection(left + right);
      var cur = editor.getCursor();
      editor.setCursor({ line: cur.line, ch: cur.ch - right.length });
    }
  }
  function insertLink() {
    var sel = editor.getSelection();
    if (sel) editor.replaceSelection("[" + sel + "](url)");
    else editor.replaceSelection("[](url)");
  }
  function insertCodeBlock() {
    var sel = editor.getSelection();
    if (sel) editor.replaceSelection("```\n" + sel + "\n```");
    else editor.replaceSelection("```\n\n```");
  }
  function zoom(delta) {
    state.fontSize = Math.max(10, Math.min(28, state.fontSize + delta));
    editor.getWrapperElement().style.fontSize = state.fontSize + "px";
    editor.refresh();
  }

  // ── 多标签页 ──────────────────────────────────────────
  // 打开笔记后统一应用界面状态（供磁盘读取与内存草稿两条路径复用）
  function applyNoteState(relPath, content) {
    state.currentRel = relPath;
    state.dirty = !!state.tabs[relPath].dirty;
    state.tabs[relPath].loaded = true;
    editor.setValue(content || "");
    editor.clearHistory();
    el("file-label").textContent = relPath;
    markActive(relPath);
    renderTabs();
    updateSaveStatus();
    updateStats();
    schedulePreview(true);
    scheduleOutline(true);
    call("push_recent_file", relPath);
    if (document.querySelector('.side-tab[data-tab="recent"]').classList.contains("active")) loadRecent();
    // 刷新收藏按钮高亮状态
    call("is_favorite", relPath).then(function (r) {
      var b = el("btn-fav");
      if (b) b.classList.toggle("primary", !!r);
    });
    updateEmptyState();
  }
  function openNote(relPath) {
    // 防御：切走前保存当前编辑器内容；若 currentRel 标签已被删除（关闭当前标签场景），跳过
    if (state.currentRel && state.currentRel !== relPath && state.tabs[state.currentRel]) {
      state.tabs[state.currentRel].content = editor.getValue();
      state.tabs[state.currentRel].dirty = state.dirty;
    }
    if (!state.tabs[relPath]) {
      state.tabs[relPath] = { content: "", dirty: false, loaded: false };
      state.tabOrder.push(relPath);
    }
    var t = state.tabs[relPath];
    // 该标签已加载过且内存中有未保存内容：直接用内存内容，避免磁盘覆盖丢失修改
    if (t.loaded && t.dirty) {
      applyNoteState(relPath, t.content);
      return;
    }
    call("open_note", relPath).then(function (res) {
      if (!res.success) { messageBox({ title: "打开失败", message: res.error || "打开失败" }); return; }
      state.tabs[relPath].content = res.content || "";
      state.tabs[relPath].dirty = false;
      applyNoteState(relPath, res.content || "");
    });
  }
  function closeTab(relPath, ev, force) {
    if (ev) ev.stopPropagation();
    var t = state.tabs[relPath];
    if (t && t.dirty && !force) {
      confirmBox({ title: "关闭笔记", message: "笔记「" + relPath + "」有未保存修改，确定关闭？", onOk: function () { closeTab(relPath, ev, true); } });
      return;
    }
    delete state.tabs[relPath];
    state.tabOrder = state.tabOrder.filter(function (p) { return p !== relPath; });
    if (state.currentRel === relPath) {
      var next = state.tabOrder[state.tabOrder.length - 1] || null;
      state.currentRel = null; // 先清空，openNote 不会再访问已删除的标签对象
      if (next) openNote(next);
      else {
        editor.setValue("");
        el("file-label").textContent = "未打开笔记";
        renderTabs(); updateSaveStatus(); updateStats();
        schedulePreview(true); scheduleOutline(true);
        updateEmptyState();
      }
    } else renderTabs();
  }
  function renderTabs() {
    var bar = el("tabbar"); bar.innerHTML = "";
    state.tabOrder.forEach(function (rel) {
      var t = state.tabs[rel];
      var div = document.createElement("div");
      div.className = "tab" + (rel === state.currentRel ? " active" : "") + (t.dirty ? " dirty" : "");
      var name = document.createElement("span");
      name.className = "tab-name";
      name.textContent = rel.split("/").pop().replace(/\.md$/, "");
      div.appendChild(name);
      var close = document.createElement("span");
      close.className = "tab-close"; close.textContent = "×";
      close.addEventListener("click", function (e) { closeTab(rel, e); });
      div.appendChild(close);
      div.addEventListener("click", function () { openNote(rel); });
      div.addEventListener("contextmenu", function (e) {
        e.preventDefault(); showTabMenu(rel, e.clientX, e.clientY);
      });
      bar.appendChild(div);
    });
  }
  // 通用右键上下文菜单：items = [{label, danger?, disabled?, act}]
  function openCtxMenu(items, x, y) {
    var menu = el("tab-menu");
    menu.innerHTML = "";
    items.forEach(function (it) {
      var d = document.createElement("div");
      d.className = "tm-item" + (it.danger ? " danger" : "");
      d.textContent = it.label;
      if (it.disabled) { d.style.opacity = "0.4"; d.style.pointerEvents = "none"; }
      d.addEventListener("click", function () { menu.style.display = "none"; it.act(); });
      menu.appendChild(d);
    });
    menu.style.display = "flex";
    menu.style.left = Math.min(x, window.innerWidth - 150) + "px";
    menu.style.top = Math.min(y, window.innerHeight - 160) + "px";
  }
  function showTabMenu(rel, x, y) {
    var idx = state.tabOrder.indexOf(rel);
    var others = state.tabOrder.length > 1;
    var right = idx < state.tabOrder.length - 1;
    var items = [
      { label: "关闭", act: function () { closeTab(rel); } },
      { label: "关闭其他", danger: false, disabled: !others, act: function () {
        state.tabOrder.slice().forEach(function (p) { if (p !== rel) closeTabTarget(p); });
        renderTabs();
      } },
      { label: "关闭右侧", danger: true, disabled: !right, act: function () {
        state.tabOrder.slice(idx + 1).forEach(function (p) { closeTabTarget(p); });
        renderTabs();
      } },
    ];
    openCtxMenu(items, x, y);
  }
  function closeTabTarget(relPath) {
    var t = state.tabs[relPath];
    if (t && t.dirty) { closeTab(relPath); return; }
    delete state.tabs[relPath];
    state.tabOrder = state.tabOrder.filter(function (p) { return p !== relPath; });
    if (state.currentRel === relPath) {
      var next = state.tabOrder[state.tabOrder.length - 1] || null;
      state.currentRel = null; // 先清空，openNote 不会再访问已删除的标签对象
      if (next) openNote(next);
      else { editor.setValue(""); el("file-label").textContent = "未打开笔记"; updateEmptyState(); }
    }
    // 不在循环内重复渲染，交由调用方统一 renderTabs（避免批量关闭时标签栏抖动）
  }
  function markTabDirty(dirty) {
    if (state.currentRel) {
      if (!state.tabs[state.currentRel]) state.tabs[state.currentRel] = { content: "", dirty: false, loaded: true };
      state.tabs[state.currentRel].dirty = dirty;
    }
    // dirty 状态未变化时跳过整栏重建（避免每次按键都重绘标签栏）
    if (dirty === _tabsDirty) return;
    _tabsDirty = dirty;
    renderTabs();
  }
  var _tabsDirty = null;
  function markActive(relPath) {
    document.querySelectorAll(".tree-node").forEach(function (n) {
      n.classList.toggle("active", n.dataset.path === relPath && n.dataset.isdir !== "1");
    });
  }
  function updateEmptyState() {
    var empty = el("editor-empty");
    if (!empty) return;
    empty.style.display = state.currentRel ? "none" : "flex";
  }

  // ── 保存 / 新建 ───────────────────────────────────────
  function scheduleAutoSave() {
    if (autoSaveTimer) clearTimeout(autoSaveTimer);
    if (!state.autoSave || !state.dirty || !state.currentRel) return;
    autoSaveTimer = setTimeout(function () {
      if (state.dirty && state.currentRel) doSave(true);
    }, state.autoSaveDelayMs);
  }
  function doSave(auto) {
    if (!state.currentRel) { if (!auto) messageBox({ title: "提示", message: "请先打开或新建笔记" }); return; }
    if (autoSaveTimer) { clearTimeout(autoSaveTimer); autoSaveTimer = null; }
    // 防御：确保当前标签对象存在
    if (!state.tabs[state.currentRel]) state.tabs[state.currentRel] = { content: "", dirty: false, loaded: true };
    var savedRel = state.currentRel;
    var content = editor.getValue();
    state.tabs[savedRel].content = content;
    call("save_note", savedRel, content).then(function (res) {
      if (!res.success) { messageBox({ title: "保存失败", message: res.error || "保存失败" }); return; }
      // 竞态保护：仅当保存期间没有新修改、且仍停留在同一标签时才清除脏标记
      if (state.currentRel === savedRel && editor.getValue() === content) {
        state.dirty = false;
        state.tabs[savedRel].dirty = false;
        _tabsDirty = null; // 强制下一次 markTabDirty 重建标签栏（脏点状态已变化）
      }
      updateSaveStatus(); renderTabs(); flashStatus(auto ? "自动保存" : "已保存", "saved-ok");
      // 保存后刷新别名候选（笔记内容可能新增/修改了 aliases）
      call("list_note_aliases").then(function (a) { state.noteAliases = a || []; });
    });
  }
  function newNote() {
    inputDialog({
      title: "新建笔记",
      label: "笔记名称",
      placeholder: "为这篇笔记起个名字（无需输入 .md）",
      defaultValue: "未命名",
      confirmText: "创建",
      hint: "将创建在笔记根目录。同名文件会被自动重命名。",
      onConfirm: function (name) {
        call("create_note", name, "").then(function (res) {
          if (!res.success) { return false; }
          loadTree(); switchSideTab("files"); openNote(res.rel_path);
          return true;
        });
        return true; // 先关弹窗，结果通过异步回调处理
      },
    });
  }
  function newFolder() {
    inputDialog({
      title: "新建文件夹",
      label: "文件夹名称",
      placeholder: "文件夹名称",
      defaultValue: "新文件夹",
      confirmText: "创建",
      hint: "将创建在笔记根目录。",
      onConfirm: function (name) {
        call("create_folder", "", name).then(function (res) {
          if (!res.success) { messageBox({ title: "创建失败", message: res.error || "创建失败" }); return; }
          loadTree();
        });
        return true;
      },
    });
  }

  // ── 实时预览 ──────────────────────────────────────────
  var _lastPreviewSrc = null; // 缓存上次渲染的源，避免无变化时的 IPC + DOM 重建
  function schedulePreview(immediate) {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, immediate ? 0 : 200);
  }
  function renderPreview() {
    var src = editor.getValue();
    if (src === _lastPreviewSrc) return; // 内容未变，跳过
    _lastPreviewSrc = src;
    var host = el("preview");
    // 渲染前记录预览自身滚动比例，渲染完成后恢复，避免把用户正在阅读的位置拉走
    var prevRatio = _scrollRatio(host);
    call("render_markdown", src).then(function (res) {
      if (!res.success) return;
      host.innerHTML = '<div class="markdown-body">' + res.html + "</div>";
      // 给标题加锚点 id，便于大纲跳转
      var idx = 0;
      host.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach(function (h) {
        h.id = "mdn-h-" + (idx++);
      });
      renderMath(host); bindWikilinks(host); bindTaskCheckboxes(host);
      bindPreviewImages(host);
      renderPreviewOutline();
      // 渲染后恢复预览自身滚动比例（而非强制用编辑器比例），
      // 避免打开笔记/内容刷新时把用户正在阅读的位置拉回顶部。
      // 恢复动作不触发编辑器联动（_renderRestore），防止把编辑区一并拉走；
      // 标记由超时兜底清除（scroll 事件可能不触发，如位置未变化时）。
      state._renderRestore = true;
      _setScrollRatio(host, prevRatio);
      clearTimeout(state._renderRestoreTimer);
      state._renderRestoreTimer = setTimeout(function () { state._renderRestore = false; }, 120);
    });
  }
  function renderMath(host) {
    try {
      if (typeof mermaid !== "undefined") {
        // Mermaid 主题跟随应用主题：暗色主题用 dark，亮色主题用 neutral
        mermaid.initialize({ startOnLoad: false, theme: isDarkTheme() ? "dark" : "neutral", securityLevel: "strict" });
        host.querySelectorAll(".mermaid").forEach(function (m) { mermaid.run({ nodes: [m] }); });
      }
      host.querySelectorAll(".arithmatex").forEach(function (e2) {
        try { katex.render(e2.textContent, e2, { throwOnError: false, displayMode: e2.classList.contains("arithmatex-block") }); }
        catch (e) { /* ignore */ }
      });
    } catch (e) { /* ignore */ }
  }
  function bindPreviewImages(host) {
    // 给预览中的图片绑定右键菜单（OCR 识别）
    host.querySelectorAll("img").forEach(function (img) {
      img.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        var src = img.getAttribute("src") || "";
        // 从 file:/// URI 提取 images/xxx 相对路径
        var m = src.match(/images\/[^\s"')]+/i);
        var rel = m ? m[0] : null;
        showImgMenu(rel, e.clientX, e.clientY);
      });
      // 双击图片也触发 OCR（便捷）
      img.addEventListener("dblclick", function () {
        var src = img.getAttribute("src") || "";
        var m = src.match(/images\/[^\s"')]+/i);
        var rel = m ? m[0] : null;
        if (rel) ocrCurrentImage(rel);
      });
      img.style.cursor = "zoom-in";
      img.title = "右键识别文字（OCR）/ 双击识别";
    });
  }
  function showImgMenu(rel, x, y) {
    // 复用全局 #tab-menu 容器（与标签右键菜单一致），避免连续右键时在 DOM 叠加多个菜单
    var menu = el("tab-menu");
    menu.innerHTML = "";
    if (rel) {
      menu.innerHTML = '<div class="ctx-item" id="ctx-ocr">🔍 识别图片文字（OCR）</div>';
    } else {
      menu.innerHTML = '<div class="ctx-item ctx-disabled">无法定位图片</div>';
    }
    menu.style.display = "flex";
    menu.style.left = Math.min(x, window.innerWidth - 150) + "px";
    menu.style.top = Math.min(y, window.innerHeight - 80) + "px";
    var ocrItem = menu.querySelector("#ctx-ocr");
    if (ocrItem) ocrItem.addEventListener("click", function () { menu.style.display = "none"; ocrCurrentImage(rel); });
  }
  function bindWikilinks(host) {
    host.querySelectorAll(".wikilink").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        var t = a.getAttribute("data-target");
        if (!t) return;
        // 先尝试精确匹配（stem.md），失败再用别名解析
        var direct = t.endsWith(".md") ? t : t + ".md";
        var exists = state.noteNames.indexOf(t.endsWith(".md") ? t.slice(0, -3) : t) !== -1;
        if (exists) { openNote(direct); return; }
        call("resolve_wikilink", t).then(function (path) {
          if (path) openNote(path);
          else messageBox({ title: "未找到", message: "未找到笔记：「" + t + "」" });
        });
      });
    });
  }

  // ── 大纲 ──────────────────────────────────────────────
  function scheduleOutline(immediate) {
    if (outlineTimer) clearTimeout(outlineTimer);
    outlineTimer = setTimeout(renderOutline, immediate ? 0 : 250);
  }
  function renderOutline() {
    var text = editor.getValue();
    var host = el("outline");
    var items = [];
    // 直接按行扫描比正则 + substr/indexOf 切片高效（长文档避免 O(n²) 切片）
    var lines = text.split("\n");
    for (var li = 0; li < lines.length; li++) {
      var mt = /^(#{1,6})\s+(.+)$/.exec(lines[li]);
      if (mt) items.push({ level: mt[1].length, text: mt[2].trim(), line: li });
    }
    host.innerHTML = "";
    if (!items.length) { host.innerHTML = '<div class="ol-empty">打开笔记后显示大纲</div>'; return; }
    items.forEach(function (it) {
      var div = document.createElement("div");
      div.className = "ol-item lvl-" + it.level;
      div.textContent = it.text; div.title = "第 " + (it.line + 1) + " 行";
      div.addEventListener("click", function () {
        editor.setCursor({ line: it.line, ch: 0 }); editor.focus();
      });
      host.appendChild(div);
    });
  }

  // ── 全屏预览大纲 ──────────────────────────────────────
  function renderPreviewOutline() {
    var list = el("po-list");
    if (!list) return;
    list.innerHTML = "";
    var text = editor.getValue();
    var lines = text.split("\n");
    var headings = [];
    for (var li = 0; li < lines.length; li++) {
      var mt = /^(#{1,6})\s+(.+)$/.exec(lines[li]);
      if (mt) headings.push({ level: mt[1].length, text: mt[2].trim(), line: li, id: "mdn-h-" + headings.length });
    }
    if (!headings.length) { list.innerHTML = '<div class="ol-empty">暂无标题</div>'; return; }
    headings.forEach(function (it) {
      var div = document.createElement("div");
      div.className = "po-item lvl-" + it.level;
      div.textContent = it.text;
      div.addEventListener("click", function () {
        var target = el("preview").querySelector("#" + it.id);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      list.appendChild(div);
    });
  }

  // ── 补全 ──────────────────────────────────────────────
  function maybeTriggerCompletion() {
    var cur = editor.getCursor();
    var line = editor.getLine(cur.line);
    var before = line.slice(0, cur.ch);
    var wikiIdx = before.lastIndexOf("[[");
    if (wikiIdx !== -1) {
      var between = before.slice(wikiIdx + 2);
      if (between.indexOf("]]") === -1 && between.indexOf("\n") === -1 && (wikiIdx === 0 || before[wikiIdx - 1] !== "!")) {
        // 候选 = 笔记名 + 所有别名（去重）
        var aliasList = [];
        state.noteAliases.forEach(function (a) { a.aliases.forEach(function (al) { aliasList.push(al); }); });
        var wikiCands = state.noteNames.concat(aliasList).filter(function (v, i, arr) { return arr.indexOf(v) === i; });
        showCompletion(wikiCands, function (p) { replaceBetween(wikiIdx + 2, cur.ch, p + "]]"); });
        return;
      }
    }
    var hashIdx = before.lastIndexOf("#");
    if (hashIdx !== -1) {
      var ok = hashIdx === 0 || /\s/.test(before[hashIdx - 1]);
      var tb = before.slice(hashIdx + 1);
      if (ok && tb.indexOf(" ") === -1 && tb.indexOf("\n") === -1) {
        showCompletion(state.noteNames, function (p) { replaceBetween(hashIdx + 1, cur.ch, p); });
        return;
      }
    }
    if (line.trim().startsWith("```") && cur.ch > 3 && line.slice(3, cur.ch).indexOf(" ") === -1) {
      var lp = line.slice(3, cur.ch);
      if (lp.indexOf("\n") === -1) {
        showCompletion(state.codeLangs, function (p) { replaceBetween(3, cur.ch, p); });
        return;
      }
    }
  }
  function replaceBetween(fromCh, toCh, text) {
    var cur = editor.getCursor();
    editor.replaceRange(text, { line: cur.line, ch: fromCh }, { line: cur.line, ch: toCh });
    editor.setCursor({ line: cur.line, ch: fromCh + text.length });
  }
  function showCompletion(list, onPick) {
    if (!list || !list.length) return;
    var cur = editor.getCursor();
    var hint = { list: list.map(function (s) { return { text: s, displayText: s }; }), from: cur, to: cur };
    CodeMirror.showHint(editor, function () { return hint; }, { completeSingle: false });
  }

  // ── 图片 ──────────────────────────────────────────────
  function setupImagePaste() {
    var host = el("editor-host");
    host.addEventListener("paste", function (e) {
      var items = (e.clipboardData || e.originalEvent.clipboardData).items;
      for (var i = 0; i < items.length; i++) {
        if (items[i].type.indexOf("image") === 0) {
          var blob = items[i].getAsFile();
          if (blob) { e.preventDefault(); blobToB64(blob, function (b64, ext) { saveAndInsertImage(b64, ext); }); }
          return;
        }
      }
    });
    host.addEventListener("drop", function (e) {
      var files = e.dataTransfer.files;
      for (var i = 0; i < files.length; i++) {
        var f = files[i];
        if (f.type.indexOf("image") === 0) {
          e.preventDefault();
          blobToB64(f, function (b64, ext) { saveAndInsertImage(b64, ext); });
          return;
        }
      }
    });
  }
  function blobToB64(blob, cb) {
    var reader = new FileReader();
    reader.onload = function () {
      var r = reader.result, comma = r.indexOf(",");
      var ext = (r.slice(0, comma).match(/image\/([a-zA-Z0-9.]+)/) || [])[1] || "png";
      cb(r.slice(comma + 1), "." + ext);
    };
    reader.readAsDataURL(blob);
  }
  function saveAndInsertImage(b64, ext) {
    call("save_image", b64, ext).then(function (res) {
      if (!res.success) { messageBox({ title: "图片保存失败", message: res.error || "图片保存失败" }); return; }
      var cur = editor.getCursor();
      editor.replaceRange("![](" + res.rel_path + ")", cur);
      schedulePreview();
    });
  }

  // ── 编辑工具栏（MDNotes 1.x 编辑体验增强）─────────────────
  // 所有操作在 CodeMirror 上做最小侵入式插入，保持纯源码、零构建、离线可用。
  function insertAtLineStart(prefix) {
    // 在光标所在行行首插入前缀（用于标题/引用/列表）
    var cur = editor.getCursor();
    var line = editor.getLine(cur.line);
    // 若已存在同类前缀则不再重复（幂等）
    if (line.indexOf(prefix) === 0) { editor.setCursor({ line: cur.line, ch: line.length }); editor.focus(); return; }
    editor.replaceRange(prefix, { line: cur.line, ch: 0 });
    editor.setCursor({ line: cur.line, ch: prefix.length });
    editor.focus();
    schedulePreview(); scheduleOutline();
  }
  function insertTaskList() {
    var cur = editor.getCursor();
    editor.replaceRange("- [ ] ", { line: cur.line, ch: 0 });
    editor.setCursor({ line: cur.line, ch: 6 });
    editor.focus();
    schedulePreview();
  }
  function insertTable() {
    var cur = editor.getCursor();
    var tbl = "| 列1 | 列2 | 列3 |\n| --- | --- | --- |\n| 单元格 | 单元格 | 单元格 |\n| 单元格 | 单元格 | 单元格 |\n";
    // 若当前行非空，先换行
    if (editor.getLine(cur.line).trim() !== "") tbl = "\n" + tbl;
    editor.replaceRange(tbl, cur);
    editor.setCursor({ line: cur.line + (tbl[0] === "\n" ? 1 : 0), ch: 2 });
    editor.focus();
    schedulePreview();
  }
  function insertQuote() { insertAtLineStart("> "); }
  function insertUl() { insertAtLineStart("- "); }
  function insertOl() { insertAtLineStart("1. "); }
  function insertHeading(level) { insertAtLineStart(new Array(level + 1).join("#") + " "); }
  function insertLinkToolbar() { insertLink(); schedulePreview(); }
  function insertImageToolbar() {
    // 触发隐藏的文件选择框，选图后走 save_image 流程
    var inp = el("img-pick");
    if (!inp) {
      inp = document.createElement("input");
      inp.type = "file"; inp.id = "img-pick"; inp.accept = "image/*"; inp.style.display = "none";
      inp.addEventListener("change", function () {
        if (!inp.files || !inp.files.length) return;
        var f = inp.files[0];
        if (f.type.indexOf("image") === 0) blobToB64(f, function (b64, ext) { saveAndInsertImage(b64, ext); });
        inp.value = "";
      });
      document.body.appendChild(inp);
    }
    inp.click();
  }
  function execEditorAction(act) {
    if (!editor) return;
    switch (act) {
      case "bold": wrapSelection("**", "**"); schedulePreview(); break;
      case "italic": wrapSelection("*", "*"); schedulePreview(); break;
      case "h1": insertHeading(1); break;
      case "h2": insertHeading(2); break;
      case "ul": insertUl(); break;
      case "ol": insertOl(); break;
      case "task": insertTaskList(); break;
      case "quote": insertQuote(); break;
      case "code": insertCodeBlock(); schedulePreview(); break;
      case "table": insertTable(); break;
      case "link": insertLinkToolbar(); break;
      case "image": insertImageToolbar(); break;
    }
  }
  // 任务清单勾选同步：点击预览里的复选框，写回源码 [ ] / [x]
  function bindTaskCheckboxes(host) {
    host.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (cb.dataset.mdnBound) return;
      cb.dataset.mdnBound = "1";
      cb.addEventListener("change", function () {
        var checked = cb.checked;
        // 通过 data-line 找到源码行（渲染时由 render_markdown 写入）
        var lineNo = parseInt(cb.getAttribute("data-line") || "-1", 10);
        if (isNaN(lineNo) || lineNo < 0) return;
        var line = editor.getLine(lineNo);
        if (!line) return;
        var nl = line.replace(/^(\s*[-*+]\s+)\[([ xX])\]/, function (m, p1, p2) {
          return p1 + "[" + (checked ? "x" : " ") + "]";
        });
        if (nl !== line) { editor.replaceRange(nl, { line: lineNo, ch: 0 }, { line: lineNo, ch: line.length }); schedulePreview(); }
      });
    });
  }

  // ── 文件树 ────────────────────────────────────────────
  function loadTree() {
    call("list_tree").then(function (nodes) { renderTree(nodes, el("filetree")); });
  }
  function renderTree(nodes, container) {
    container.innerHTML = "";
    if (!nodes || !nodes.length) { container.innerHTML = '<div class="tree-empty">暂无笔记，点击「+ 笔记」新建</div>'; return; }
    nodes.forEach(function (n) { container.appendChild(renderNode(n)); });
  }
  function renderNode(node) {
    var div = document.createElement("div");
    div.className = "tree-node" + (node.is_dir ? " dir" : "");
    div.dataset.path = node.path; div.dataset.isdir = node.is_dir ? "1" : "0";
    var ico = document.createElement("span");
    ico.className = "tree-ico";
    ico.textContent = node.is_dir ? "📂" : "📝";
    div.appendChild(ico);
    var label = document.createElement("span");
    label.className = "tree-label";
    label.textContent = node.name;
    label.style.overflow = "hidden"; label.style.textOverflow = "ellipsis";
    div.appendChild(label);
    div.addEventListener("contextmenu", function (e) {
      e.preventDefault(); e.stopPropagation();
      showTreeMenu(node, e.clientX, e.clientY);
    });
    if (node.is_dir) {
      var children = document.createElement("div");
      children.className = "tree-children";
      (node.children || []).forEach(function (c) { children.appendChild(renderNode(c)); });
      var newBtn = document.createElement("span");
      newBtn.className = "tree-newdir"; newBtn.textContent = "＋";
      newBtn.title = "在此目录下新建文件夹";
      newBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        inputDialog({
          title: "新建文件夹",
          label: "文件夹名称",
          placeholder: "文件夹名称",
          defaultValue: "新文件夹",
          confirmText: "创建",
          hint: "将创建在「" + node.name + "」目录下。",
          onConfirm: function (name) {
            call("create_folder", node.path, name).then(function (r) {
              if (!r.success) { messageBox({ title: "创建失败", message: r.error || "创建失败" }); return; }
              loadTree();
            });
            return true;
          },
        });
      });
      div.appendChild(newBtn);
      div.addEventListener("click", function (e) {
        e.stopPropagation();
        var collapsed = div.classList.toggle("collapsed");
        children.style.display = collapsed ? "none" : "";
      });
      var wrap = document.createElement("div"); wrap.appendChild(div); wrap.appendChild(children);
      return wrap;
    }
    div.addEventListener("click", function () { openNote(node.path); });
    return div;
  }
  // 文件树节点右键菜单：新建笔记 / 新建文件夹 / 重命名 / 删除
  function showTreeMenu(node, x, y) {
    var isDir = !!node.is_dir;
    var base = isDir ? node.path : node.path.split("/").slice(0, -1).join("/");
    var items = [
      { label: "📝 新建笔记", act: function () {
        inputDialog({
          title: "新建笔记",
          label: "笔记名称",
          placeholder: "为这篇笔记起个名字（无需输入 .md）",
          defaultValue: "未命名",
          confirmText: "创建",
          hint: isDir ? "将创建在「" + node.name + "」目录下。" : "将创建在当前目录。",
          onConfirm: function (name) {
            call("create_note", name, base).then(function (r) {
              if (!r.success) { messageBox({ title: "创建失败", message: r.error || "创建失败" }); return; }
              loadTree(); switchSideTab("files"); openNote(r.rel_path);
              return true;
            });
            return true;
          },
        });
      } },
      { label: "📁 新建文件夹", act: function () {
        inputDialog({
          title: "新建文件夹",
          label: "文件夹名称",
          placeholder: "文件夹名称",
          defaultValue: "新文件夹",
          confirmText: "创建",
          hint: isDir ? "将创建在「" + node.name + "」目录下。" : "将创建在当前目录。",
          onConfirm: function (name) {
            call("create_folder", base, name).then(function (r) {
              if (!r.success) { messageBox({ title: "创建失败", message: r.error || "创建失败" }); return; }
              loadTree();
            });
            return true;
          },
        });
      } },
      { label: "✏ 重命名", act: function () {
        inputDialog({
          title: isDir ? "重命名文件夹" : "重命名笔记",
          label: "新名称",
          placeholder: isDir ? "文件夹名称" : "笔记名称（无需 .md）",
          defaultValue: node.name.replace(/\.md$/, ""),
          confirmText: "重命名",
          onConfirm: function (name) {
            var p = isDir ? call("rename_folder", node.path, name) : call("rename_note", node.path, name);
            p.then(function (r) {
              if (!r || !r.success) { messageBox({ title: "重命名失败", message: (r && r.error) || "重命名失败" }); return; }
              loadTree();
              if (!isDir && state.currentRel === node.path) { state.currentRel = r.rel_path; el("file-label").textContent = r.rel_path; }
            });
            return true;
          },
        });
      } },
      { label: "ℹ 属性", disabled: isDir, act: function () { showNoteProperties(node.path); } },
      { label: "⭐ 收藏 / 取消收藏", disabled: isDir, act: function () { toggleFavorite(node.path); } },
      { label: "🗑 删除", danger: true, act: function () {
        confirmBox({
          title: "删除" + (isDir ? "文件夹" : "笔记"),
          message: "确定将「" + node.name + "」移入回收站？" + (isDir ? "（含其中全部内容，可在回收站恢复）" : ""),
          danger: true,
          okText: "删除",
          onOk: function () {
            var p = isDir ? call("delete_folder", node.path) : call("delete_note", node.path);
            p.then(function (r) {
              if (!r || !r.success) { messageBox({ title: "删除失败", message: (r && r.error) || "删除失败" }); return; }
              loadTree();
              if (!isDir && state.currentRel === node.path) {
                state.currentRel = null; editor.setValue(""); el("file-label").textContent = "未打开笔记";
                updateEmptyState(); renderTabs(); updateSaveStatus();
              }
            });
          },
        });
      } },
    ];
    openCtxMenu(items, x, y);
  }

  // ── 状态栏 / 分隔条 / 面板切换 ─────────────────────────
  function updateStats() {
    // 用 O(1) 的 API 取字数/行数，避免每次按键都 getValue() 重建整篇字符串
    var doc = editor.getDoc();
    el("status-words").textContent = doc.size + " 字";
    el("status-lines").textContent = editor.lineCount() + " 行";
  }
  function updateSaveStatus() {
    var s = el("status-save");
    if (state.dirty) { s.textContent = "未保存"; s.className = "saved-dirty"; }
    else { s.textContent = "已保存"; s.className = "saved-ok"; }
  }
  function flashStatus(msg, cls) {
    var s = el("status-save"); s.textContent = msg; s.className = cls;
    setTimeout(updateSaveStatus, 1500);
  }
  function initGutters() {
    // 每个分隔条调整其「左侧」相邻面板的宽度（基于面板真实边界计算，避免累积误差）
    bindGutter(el("gutter-side"), el("sidebar"), "sidebar-w", 160, 440);
    bindGutter(el("gutter-outline"), el("editor-pane"), "editor-w", 240, 900);
    // 预览分隔条调整其「右侧」面板（预览）的宽度
    bindGutter(el("gutter-preview"), el("preview-pane"), "preview-w", 220, 1100, true);
  }
  function bindGutter(g, panelEl, cssVar, min, max, fromRight) {
    if (!g || !panelEl) return;
    g.addEventListener("mousedown", function (e) {
      e.preventDefault();
      document.body.style.cursor = "col-resize";
      document.body.classList.add("col-resizing");
      var rect = panelEl.getBoundingClientRect();
      var main = el("main");
      var isNoOutlinePreview = cssVar === "preview-w" && main.classList.contains("no-outline");
      function move(ev) {
        // 隐去大纲时，预览分隔条改为调整 editor/preview 的 fr 比例
        if (isNoOutlinePreview) {
          setNoOutlineRatioFromMouse(ev.clientX);
          return;
        }
        var w;
        if (fromRight) w = rect.right - ev.clientX;       // 调整右侧面板：以面板右边界为基准
        else w = ev.clientX - rect.left;                   // 调整左侧面板：以面板左边界为基准
        w = Math.min(max, Math.max(min, w));
        document.documentElement.style.setProperty("--" + cssVar, w + "px");
      }
      function up() {
        document.body.style.cursor = "";
        document.body.classList.remove("col-resizing");
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        // 面板宽度变化后刷新编辑器，避免行宽测量陈旧导致换行/缩进错乱
        if (editor && editor.refresh) { try { editor.refresh(); } catch (e) { /* 忽略 */ } }
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }
  function setNoOutlineRatio(ratio) {
    ratio = Math.max(0.15, Math.min(0.85, ratio));
    var ed = (ratio * 10).toFixed(2) + "fr";
    var pr = ((1 - ratio) * 10).toFixed(2) + "fr";
    // 通过 CSS 变量控制（layout.css 的 #main.no-outline 读取这两个变量），
    // 不用内联 gridTemplateColumns，避免覆盖媒体查询与类规则
    document.documentElement.style.setProperty("--editor-w", ed);
    document.documentElement.style.setProperty("--preview-w", pr);
  }
  function setNoOutlineRatioFromMouse(clientX) {
    var main = el("main");
    var mainRect = main.getBoundingClientRect();
    var sideW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w")) || 220;
    var avail = Math.max(1, mainRect.width - sideW - 18); // 3 条 6px 分隔条
    var x = clientX - mainRect.left - sideW - 6;          // 在 editor+preview 区域内的位置
    setNoOutlineRatio(x / avail);
  }
  var _savedW = null;
  function toggleOutline() {
    var main = el("main");
    var hide = !main.classList.contains("no-outline");
    if (hide) {
      // 保存用户拖动的像素宽度，隐藏期间切到 fr 比例，恢复时还原
      var root = getComputedStyle(document.documentElement);
      _savedW = {
        editor: root.getPropertyValue("--editor-w") || "",
        preview: root.getPropertyValue("--preview-w") || ""
      };
      main.classList.add("no-outline");
      setNoOutlineRatio(0.5);
    } else {
      main.classList.remove("no-outline");
      if (_savedW) {
        var st = document.documentElement.style;
        if (_savedW.editor) st.setProperty("--editor-w", _savedW.editor);
        else st.removeProperty("--editor-w");
        if (_savedW.preview) st.setProperty("--preview-w", _savedW.preview);
        else st.removeProperty("--preview-w");
        _savedW = null;
      }
    }
  }
  function togglePreview() { el("main").classList.toggle("no-preview"); }
  function togglePreviewFull() {
    var on = document.body.classList.toggle("preview-full");
    var b = el("btn-preview-full");
    if (b) { b.textContent = on ? "✕ 退出" : "⛶ 全屏"; b.title = on ? "退出全屏预览 (Esc)" : "全屏预览 (Esc 退出)"; }
    if (on) renderPreviewOutline();
  }

  // ══ 阶段3：侧边栏 Tab / 搜索 / 标签云 ══════════════════
  var _graphCy = null;
  function loadGraph() {
    var canvas = el("graph-canvas");
    var stat = el("graph-stat");
    if (typeof cytoscape === "undefined") {
      canvas.innerHTML = '<div class="graph-empty">图谱库未加载（离线时不可用）。请联网后重新打开，或检查 vendor/cytoscape。</div>';
      return;
    }
    canvas.innerHTML = '<div class="graph-empty">正在构建关系图谱…</div>';
    call("build_graph").then(function (res) {
      if (!res || !res.nodes) { canvas.innerHTML = '<div class="graph-empty">图谱数据为空</div>'; return; }
      var nodes = res.nodes, edges = res.edges;
      if (!nodes.length) { canvas.innerHTML = '<div class="graph-empty">还没有任何笔记或双链</div>'; return; }
      stat.textContent = nodes.length + " 节点 · " + edges.length + " 连线";
      canvas.innerHTML = "";
      var elements = [];
      nodes.forEach(function (n) {
        elements.push({ data: { id: n.id, label: n.title || n.path, tags: (n.tags || []).join(",") }, classes: (n.tags && n.tags.length) ? "tagged" : "" });
      });
      edges.forEach(function (e) {
        elements.push({ data: { id: "e_" + e.source + "->" + e.target, source: e.source, target: e.target } });
      });
      if (_graphCy) { try { _graphCy.destroy(); } catch (x) {} _graphCy = null; }
      _graphCy = cytoscape({
        container: canvas,
        elements: elements,
        style: [
          {
            selector: "node",
            style: {
              "background-color": "#4cc9f0",
              "label": "data(label)",
              "color": "#e6ebf2",
              "font-size": "10px",
              "text-valign": "bottom",
              "text-margin-y": 4,
              "text-wrap": "wrap",
              "text-max-width": "90px",
              "width": "mapData(degree, 0, 12, 18, 46)",
              "height": "mapData(degree, 0, 12, 18, 46)",
              "border-width": 2,
              "border-color": "#1b2433",
            },
          },
          {
            selector: "node.tagged",
            style: { "background-color": "#f6c453" },
          },
          {
            selector: "node:selected",
            style: { "border-color": "#ffd166", "border-width": 4, "background-color": "#ffd166" },
          },
          {
            selector: "edge",
            style: {
              "width": 1.5,
              "line-color": "#33415c",
              "curve-style": "bezier",
              "target-arrow-shape": "triangle",
              "target-arrow-color": "#33415c",
              "opacity": 0.7,
            },
          },
          {
            selector: "edge.highlight, node.highlight",
            style: { "line-color": "#4cc9f0", "background-color": "#4cc9f0", "opacity": 1, "z-index": 99 },
          },
        ],
        layout: { name: "cose", animate: true, animationDuration: 600, nodeRepulsion: 8000, idealEdgeLength: 90, padding: 24 },
      });
      _graphCy.on("tap", "node", function (evt) {
        var id = evt.target.id();
        openNote(id);
        switchSideTab("files");
      });
      _graphCy.on("mouseover", "node", function (evt) {
        var nb = evt.target.neighborhood();
        nb.addClass("highlight");
      });
      _graphCy.on("mouseout", "node", function () {
        _graphCy.elements().removeClass("highlight");
      });
    });
  }

  function switchSideTab(name) {
    document.querySelectorAll(".side-tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tab === name);
    });
    ["files", "search", "tasks", "attachments", "overview", "tags", "recent", "favorites", "graph", "heatmap", "templates"].forEach(function (n) {
      el("side-" + n).style.display = (n === name) ? "flex" : "none";
    });
    if (name === "tags") loadTags();
    if (name === "recent") loadRecent();
    if (name === "favorites") loadFavorites();
    if (name === "graph") loadGraph();
    if (name === "heatmap") loadHeatmap();
    if (name === "templates") loadTemplates();
    if (name === "tasks") loadTasks();
    if (name === "attachments") loadAttachments();
    if (name === "overview") loadOverview();
  }
  function initSideTabs() {
    document.querySelectorAll(".side-tab[data-tab]").forEach(function (b) {
      b.addEventListener("click", function () { switchSideTab(b.dataset.tab); });
    });
    var si = el("search-input");
    si.addEventListener("input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(doSearch, 300);
    });
    on("search-tag", "input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(doSearch, 300);
    });
    on("search-date-from", "change", doSearch);
    on("search-date-to", "change", doSearch);
    on("search-scope", "change", doSearch);
    on("search-saved", "change", applySavedSearch);
    on("btn-search-save", "click", saveSearchDialog);
    loadSavedSearches();
    on("task-filter", "change", loadTasks);
    on("btn-att-refresh", "click", loadAttachments);
    on("ov-view", "change", loadOverview);
    on("ov-group", "change", loadOverview);
    on("ov-filter", "input", function () {
      if (ovTimer) clearTimeout(ovTimer);
      ovTimer = setTimeout(loadOverview, 200);
    });
    el("btn-refresh-tags").addEventListener("click", loadTags);
    on("btn-rename-tag", "click", showTagRename);
    on("btn-graph-rebuild", "click", loadGraph);
    on("btn-heat-range", "click", function () {
      // 在 120 / 365 天之间切换
      state.heatDays = (state.heatDays === 365) ? 120 : 365;
      el("btn-heat-range").textContent = "近 " + state.heatDays + " 天";
      loadHeatmap();
    });
    on("btn-new-from-tpl", "click", showNewFromTemplate);
    on("btn-manage-tpl", "click", showManageTemplates);
  }
  function doSearch() {
    var q = el("search-input").value.trim();
    var box = el("search-results");
    if (!q) { box.innerHTML = ""; return; }
    // 提取纯文本关键词（去掉 #tag 语法）用于命中高亮
    var kw = q.split(/\s+/).filter(function (t) { return t && !t.startsWith("#"); }).join("|");
    var re = kw ? new RegExp("(" + kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi") : null;
    var scope = el("search-scope") ? el("search-scope").value : "";
    var tag = el("search-tag") ? el("search-tag").value.trim().replace(/^#/, "") : "";
    var dFrom = el("search-date-from") ? el("search-date-from").value : "";
    var dTo = el("search-date-to") ? el("search-date-to").value : "";
    call("search", q, scope, tag, dFrom, dTo).then(function (results) {
      box.innerHTML = "";
      if (!results || !results.length) { box.innerHTML = '<div class="modal-empty">无结果</div>'; return; }
      results.forEach(function (r) {
        var div = document.createElement("div");
        div.className = "result-item";
        // snippet 来自笔记原文，先转义再高亮，防止笔记内容注入 HTML（XSS）
        var snip = escapeHtml(r.snippet || "");
        if (re) snip = snip.replace(re, "<mark>$1</mark>");
        div.innerHTML = "<div>" + escapeHtml(r.path) + "</div>" +
          (snip ? '<div class="snippet">' + snip + "</div>" : "");
        div.addEventListener("click", function () { openNote(r.path); switchSideTab("files"); });
        box.appendChild(div);
      });
    });
  }
  // ── 保存的搜索 ────────────────────────────────────────
  function loadSavedSearches() {
    call("list_saved_searches").then(function (list) {
      var sel = el("search-saved");
      if (!sel) return;
      var cur = sel.value;
      sel.innerHTML = '<option value="">保存的搜索…</option>';
      (list || []).forEach(function (s) {
        var o = document.createElement("option");
        o.value = s.name;
        o.textContent = "💾 " + s.name + (s.query ? "（" + s.query + "）" : "");
        sel.appendChild(o);
      });
      sel.value = cur;
    });
  }
  function applySavedSearch() {
    var name = el("search-saved").value;
    if (!name) return;
    call("list_saved_searches").then(function (list) {
      var s = (list || []).filter(function (x) { return x.name === name; })[0];
      if (!s) return;
      el("search-input").value = s.query || "";
      el("search-scope").value = s.scope || "";
      el("search-tag").value = s.tag ? "#" + s.tag : "";
      doSearch();
    });
  }
  function saveSearchDialog() {
    var q = el("search-input").value.trim();
    var scope = el("search-scope") ? el("search-scope").value : "";
    var tag = el("search-tag") ? el("search-tag").value.trim().replace(/^#/, "") : "";
    if (!q && !tag) { toast("请输入关键词或标签后再保存", "warn"); return; }
    inputDialog({
      title: "保存搜索",
      label: "搜索名称",
      placeholder: "如：本周项目待办",
      onConfirm: function (name) {
        call("save_search", name, q, scope, tag).then(function (r) {
          if (r && r.success) { toast("已保存搜索", "ok"); loadSavedSearches(); }
          else messageBox({ title: "保存失败", message: (r && r.error) || "保存失败" });
        });
      }
    });
  }
  // ── 任务视图 ──────────────────────────────────────────
  function loadTasks() {
    var filter = el("task-filter") ? el("task-filter").value : "";
    call("scan_tasks", filter).then(function (list) {
      var box = el("task-list"); box.innerHTML = "";
      var stat = el("task-stat");
      list = list || [];
      var openN = list.filter(function (t) { return !t.done; }).length;
      if (stat) stat.textContent = list.length + " 项" + (openN ? "（未完成 " + openN + "）" : "");
      if (!list.length) { box.innerHTML = '<div class="modal-empty">暂无任务</div>'; return; }
      list.forEach(function (t) {
        var div = document.createElement("div");
        div.className = "task-item" + (t.done ? " done" : "");
        var row = document.createElement("div");
        row.className = "task-row";
        var cb = document.createElement("span");
        cb.className = "task-cb"; cb.textContent = t.done ? "☑" : "☐";
        var txt = document.createElement("span");
        txt.className = "task-txt"; txt.textContent = t.text;
        var loc = document.createElement("button");
        loc.className = "mini task-loc"; loc.textContent = "定位";
        loc.title = "打开笔记并定位到该任务";
        row.appendChild(cb); row.appendChild(txt); row.appendChild(loc);
        var meta = document.createElement("div");
        meta.className = "task-meta";
        meta.textContent = t.path + ":" + t.line + (t.due ? " · 截止 " + t.due : "");
        div.appendChild(row); div.appendChild(meta);
        loc.addEventListener("click", function () { openNoteAtLine(t.path, t.line); });
        box.appendChild(div);
      });
    });
  }
  function openNoteAtLine(relPath, line) {
    function jump() {
      if (state.currentRel === relPath) {
        setTimeout(function () {
          var l = Math.max(0, (parseInt(line, 10) || 1) - 1);
          editor.setCursor({ line: l, ch: 0 });
          editor.focus();
          scrollCursorToCenter(true);
        }, 150);
      }
    }
    if (state.currentRel === relPath) jump();
    else { openNote(relPath); setTimeout(jump, 450); }
  }

  // ── 附件管理器 ────────────────────────────────────────
  function loadAttachments() {
    call("list_attachments_all").then(function (list) {
      var box = el("attachment-list"); box.innerHTML = "";
      var stat = el("att-stat");
      list = list || [];
      if (!list.length) {
        if (stat) stat.textContent = "0 个附件";
        box.innerHTML = '<div class="modal-empty">暂无附件</div>'; return;
      }
      var orphanN = list.filter(function (a) { return !a.referenced; }).length;
      if (stat) stat.textContent = list.length + " 个附件" + (orphanN ? "（" + orphanN + " 未引用）" : "");
      list.forEach(function (a) {
        var div = document.createElement("div");
        div.className = "att-item" + (a.referenced ? "" : " orphan");
        var isImg = /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(a.name || "");
        var head = document.createElement("div");
        head.className = "att-head";
        var icon = document.createElement("span");
        icon.className = "att-icon"; icon.textContent = isImg ? "🖼" : "📎";
        var name = document.createElement("span");
        name.className = "att-name"; name.title = a.rel_path; name.textContent = a.name;
        var badge = document.createElement("span");
        badge.className = a.referenced ? "att-badge" : "att-badge orphan";
        badge.textContent = a.referenced ? "已引用" : "未引用";
        head.appendChild(icon); head.appendChild(name); head.appendChild(badge);
        var meta = document.createElement("div");
        meta.className = "task-meta";
        meta.textContent = a.rel_path + " · " + fmtBytes(a.size) + " · " + fmtDate(a.mtime);
        div.appendChild(head); div.appendChild(meta);
        if (!a.referenced) {
          var del = document.createElement("button");
          del.className = "mini danger att-del"; del.textContent = "删除";
          del.title = "该附件未被任何笔记引用";
          head.appendChild(del);
          del.addEventListener("click", function () {
            confirmBox({ title: "删除附件", message: "删除未引用的附件「" + a.rel_path + "」？此操作不可恢复。", danger: true, onOk: function () {
              call("delete_attachment", a.rel_path).then(function (r) {
                if (r && r.success) { toast("已删除附件", "ok"); loadAttachments(); }
                else messageBox({ title: "删除失败", message: (r && r.error) || "删除失败" });
              });
            } });
          });
        }
        box.appendChild(div);
      });
    });
  }

  // ── 笔记总览（表格 / 看板） ───────────────────────────
  function loadOverview() {
    var view = el("ov-view") ? el("ov-view").value : "table";
    var group = el("ov-group") ? el("ov-group").value : "status";
    var filter = (el("ov-filter") ? el("ov-filter").value : "").trim().toLowerCase();
    call("list_all_notes_meta").then(function (list) {
      var box = el("overview-list"); box.innerHTML = "";
      list = list || [];
      if (filter) {
        list = list.filter(function (n) {
          return (n.title || "").toLowerCase().indexOf(filter) !== -1 ||
            (n.folder || "").toLowerCase().indexOf(filter) !== -1 ||
            (n.tags || []).join(" ").toLowerCase().indexOf(filter) !== -1;
        });
      }
      list.sort(function (a, b) { return (b.mtime || 0) - (a.mtime || 0); });
      if (!list.length) { box.innerHTML = '<div class="modal-empty">无匹配笔记</div>'; return; }
      if (view === "table") renderOverviewTable(box, list);
      else renderOverviewBoard(box, list, group);
    });
  }
  function renderOverviewTable(box, list) {
    var table = document.createElement("table");
    table.className = "ov-table";
    var thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>标题</th><th>文件夹</th><th>标签</th><th>状态</th><th>优先级</th><th>修改</th><th></th></tr>";
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    list.slice(0, 500).forEach(function (n) {
      var tr = document.createElement("tr");
      tr.className = "ov-row";
      var tdTitle = document.createElement("td");
      tdTitle.className = "ov-title"; tdTitle.textContent = n.title || n.path;
      var tdFolder = document.createElement("td");
      tdFolder.className = "ov-muted"; tdFolder.textContent = n.folder || "";
      var tdTags = document.createElement("td");
      tdTags.className = "ov-tags";
      (n.tags || []).slice(0, 3).forEach(function (t) {
        var s = document.createElement("span"); s.className = "ov-tag"; s.textContent = "#" + t; tdTags.appendChild(s);
      });
      var tdStatus = document.createElement("td");
      tdStatus.className = "ov-badge"; tdStatus.textContent = n.status || "";
      var tdPri = document.createElement("td");
      tdPri.className = "ov-badge"; tdPri.textContent = n.priority || "";
      var tdTime = document.createElement("td");
      tdTime.className = "ov-muted"; tdTime.textContent = fmtDate(n.mtime);
      var tdAct = document.createElement("td");
      var edit = document.createElement("button");
      edit.className = "mini"; edit.textContent = "属性"; edit.title = "编辑笔记属性";
      tdAct.appendChild(edit);
      tr.appendChild(tdTitle); tr.appendChild(tdFolder); tr.appendChild(tdTags);
      tr.appendChild(tdStatus); tr.appendChild(tdPri); tr.appendChild(tdTime); tr.appendChild(tdAct);
      tr.addEventListener("click", function (e) { if (!e.target.closest(".mini")) { openNote(n.path); switchSideTab("files"); } });
      edit.addEventListener("click", function (e) { e.stopPropagation(); editNoteProperties(n.path); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }
  function renderOverviewBoard(box, list, group) {
    // 分组值 -> 笔记列表
    var groups = {};
    var order = [];
    list.forEach(function (n) {
      var key;
      if (group === "priority") key = (n.priority || "无优先级");
      else if (group === "folder") key = (n.folder || "根目录");
      else key = (n.status || "无状态");
      if (!groups[key]) { groups[key] = []; order.push(key); }
      groups[key].push(n);
    });
    order.forEach(function (key) {
      var col = document.createElement("div");
      col.className = "ov-col";
      var head = document.createElement("div");
      head.className = "ov-col-head";
      head.textContent = key + " (" + groups[key].length + ")";
      col.appendChild(head);
      groups[key].slice(0, 100).forEach(function (n) {
        var card = document.createElement("div");
        card.className = "ov-card";
        card.innerHTML = "";
        var title = document.createElement("div");
        title.className = "ov-card-title"; title.textContent = n.title || n.path;
        var meta = document.createElement("div");
        meta.className = "ov-card-meta";
        meta.textContent = fmtDate(n.mtime) + (n.priority ? " · " + n.priority : "") + (n.due ? " · 截止 " + n.due : "");
        card.appendChild(title);
        var tags = document.createElement("div");
        tags.className = "ov-tags";
        (n.tags || []).slice(0, 4).forEach(function (t) {
          var s = document.createElement("span"); s.className = "ov-tag"; s.textContent = "#" + t; tags.appendChild(s);
        });
        if (tags.childNodes.length) card.appendChild(tags);
        card.appendChild(meta);
        card.addEventListener("click", function () { openNote(n.path); switchSideTab("files"); });
        col.appendChild(card);
      });
      box.appendChild(col);
    });
  }

  // ── 笔记属性编辑（frontmatter） ───────────────────────
  function editNoteProperties(relPath) {
    if (!relPath) { messageBox({ title: "提示", message: "请先选择一篇笔记" }); return; }
    call("get_note_frontmatter", relPath).then(function (r) {
      if (!r || !r.success) { messageBox({ title: "读取失败", message: (r && r.error) || "读取失败" }); return; }
      var f = r.fields || {};
      var known = { tags: 1, status: 1, priority: 1, due: 1, type: 1 };
      var extraLines = [];
      Object.keys(f).forEach(function (k) {
        if (!known[k]) {
          var v = f[k];
          extraLines.push(k + ": " + (Array.isArray(v) ? v.join(", ") : String(v)));
        }
      });
      var wrap = document.createElement("div");
      wrap.className = "mdn-input-dialog props-form";
      function fieldRow(label, id, val, ph) {
        var row = document.createElement("label");
        row.className = "cfg-row";
        var sp = document.createElement("span"); sp.textContent = label;
        var inp = document.createElement("input");
        inp.className = "input"; inp.id = id; inp.value = val || ""; inp.placeholder = ph || "";
        row.appendChild(sp); row.appendChild(inp);
        return row;
      }
      wrap.appendChild(fieldRow("标签", "pf-tags", (f.tags || []).join(", "), "逗号分隔，如 #项目, #todo"));
      wrap.appendChild(fieldRow("状态", "pf-status", f.status, "如 todo / doing / done"));
      wrap.appendChild(fieldRow("优先级", "pf-priority", f.priority, "如 high / medium / low"));
      wrap.appendChild(fieldRow("类型", "pf-type", f.type, "如 note / daily / project"));
      wrap.appendChild(fieldRow("截止日期", "pf-due", f.due, "YYYY-MM-DD"));
      var extLabel = document.createElement("div");
      extLabel.className = "mdn-hint"; extLabel.textContent = "其他字段（每行 key: value）";
      var ext = document.createElement("textarea");
      ext.className = "input pf-extra"; ext.rows = 3; ext.value = extraLines.join("\n");
      wrap.appendChild(extLabel); wrap.appendChild(ext);
      var err = document.createElement("div"); err.className = "mdn-err";
      var actions = document.createElement("div"); actions.className = "mdn-actions";
      var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
      var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "保存";
      actions.appendChild(cancel); actions.appendChild(ok);
      wrap.appendChild(err); wrap.appendChild(actions);
      var closeM = openModal("编辑属性 · " + relPath.split("/").pop(), wrap, { wide: true });
      cancel.addEventListener("click", closeM);
      ok.addEventListener("click", function () {
        var fields = {};
        var tagsStr = el("pf-tags").value.trim();
        fields.tags = tagsStr ? tagsStr.split(/[,，]/).map(function (s) { return s.trim().replace(/^#/, ""); }).filter(Boolean) : [];
        ["status", "priority", "type", "due"].forEach(function (k) {
          var v = el("pf-" + k).value.trim();
          fields[k] = v || null;
        });
        var extra = {};
        var bad = false;
        ext.value.split("\n").forEach(function (line) {
          line = line.trim();
          if (!line) return;
          var idx = line.indexOf(":");
          if (idx <= 0) { bad = true; return; }
          extra[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
        });
        if (bad) { err.textContent = "其他字段格式错误：应为 key: value（每行一条）"; return; }
        Object.keys(extra).forEach(function (k) { fields[k] = extra[k]; });
        call("save_note_properties", relPath, fields).then(function (res) {
          if (res && res.success) {
            closeM(); toast("属性已保存", "ok");
            // 当前打开的正是该笔记且无未保存编辑时，重载以展示最新 frontmatter
            if (state.currentRel === relPath && !state.dirty) openNote(relPath);
            loadTree(); loadTags();
            if (el("side-overview").style.display !== "none") loadOverview();
          } else messageBox({ title: "保存失败", message: (res && res.error) || "保存失败" });
        });
      });
    });
  }

  // 通用格式化工具
  function fmtBytes(n) {
    n = n || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }
  function fmtDate(ts) {
    if (!ts) return "-";
    var d = new Date(ts * 1000);
    var p = function (x) { return (x < 10 ? "0" : "") + x; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function loadTags() {
    var PALETTE = ["", "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c", "#3498db", "#9b59b6", "#e84393"];
    call("list_tags_hierarchical").then(function (tree) {
      call("get_tag_colors").then(function (colors) {
        colors = colors || {};
        var box = el("tag-list"); box.innerHTML = "";
        if (!tree || !tree.length) { box.innerHTML = '<div class="modal-empty">暂无标签</div>'; return; }
        // 根容器作为“拖到空白处 = 移到根”的放置目标
        box.addEventListener("dragover", function (e) { e.preventDefault(); });
        box.addEventListener("drop", function (e) {
          e.preventDefault();
          var src = e.dataTransfer.getData("text/tag-src");
          if (src) moveTag(src, "");
        });
        function renderNodes(nodes) {
          nodes.forEach(function (n) {
            var isParent = n.children && n.children.length;
            var div = document.createElement("div");
            div.className = "tag-item" + (isParent ? " tag-parent" : " leaf");
            div.draggable = true;
            var color = colors[n.name] || "";
            var dot = document.createElement("span");
            dot.className = "tag-dot" + (color ? " has" : "");
            if (color) dot.style.background = color;
            dot.title = "点击设置标签颜色";
            var label = document.createElement("span");
            label.className = "tag-label";
            label.textContent = "🏷 " + n.name.split("/").pop() + (isParent ? " ▾" : "");
            var count = document.createElement("span");
            count.className = "count"; count.textContent = n.count;
            div.appendChild(dot); div.appendChild(label); div.appendChild(count);
            // 点击标签：筛选笔记（色点点击不触发）
            div.addEventListener("click", function (e) {
              if (e.target.classList.contains("tag-dot") || e.target.classList.contains("tag-swatch")) return;
              el("search-input").value = "#" + n.name + " ";
              switchSideTab("search"); doSearch();
            });
            dot.addEventListener("click", function (e) {
              e.stopPropagation();
              openTagPalette(dot, n.name, PALETTE, color);
            });
            // HTML5 拖拽：把当前标签移动到目标标签下
            div.addEventListener("dragstart", function (e) {
              e.dataTransfer.setData("text/plain", n.name);
              e.dataTransfer.setData("text/tag-src", n.name);
              e.dataTransfer.effectAllowed = "move";
              div.classList.add("dragging");
            });
            div.addEventListener("dragend", function () { div.classList.remove("dragging"); });
            div.addEventListener("dragover", function (e) { e.preventDefault(); e.stopPropagation(); div.classList.add("drop-hint"); });
            div.addEventListener("dragleave", function () { div.classList.remove("drop-hint"); });
            div.addEventListener("drop", function (e) {
              e.preventDefault(); e.stopPropagation();
              div.classList.remove("drop-hint");
              var src = e.dataTransfer.getData("text/tag-src");
              if (src && src !== n.name) moveTag(src, n.name);
            });
            box.appendChild(div);
            if (isParent) renderNodes(n.children);
          });
        }
        renderNodes(tree);
      });
    });
  }
  function openTagPalette(dot, tag, palette, current) {
    var old = document.querySelector(".tag-palette");
    if (old) old.remove();
    var pal = document.createElement("div");
    pal.className = "tag-palette";
    palette.forEach(function (c) {
      var sw = document.createElement("span");
      sw.className = "tag-swatch" + (c === current ? " sel" : "") + (c === "" ? " clear" : "");
      if (c) sw.style.background = c;
      sw.title = c ? c : "清除颜色";
      sw.addEventListener("click", function (e) {
        e.stopPropagation();
        call("set_tag_color", tag, c).then(function (r) {
          pal.remove();
          if (r && r.success) loadTags();
          else messageBox({ title: "设置失败", message: (r && r.error) || "设置失败" });
        });
      });
      pal.appendChild(sw);
    });
    var panel = el("side-tags");
    var rect = dot.getBoundingClientRect();
    var prect = panel.getBoundingClientRect();
    pal.style.top = (rect.bottom - prect.top + 4) + "px";
    pal.style.left = Math.max(0, rect.left - prect.left) + "px";
    panel.appendChild(pal);
    var closer = function (e) {
      if (!pal.contains(e.target) && !e.target.classList.contains("tag-dot")) {
        pal.remove();
        document.removeEventListener("click", closer);
      }
    };
    document.addEventListener("click", closer);
  }
  function moveTag(source, dest) {
    call("tag_move", source, dest).then(function (r) {
      if (r && r.success) {
        toast(dest ? "已移动到 " + dest + " 下" : "已移到根层级", "ok");
        loadTags(); loadTree();
      } else messageBox({ title: "移动失败", message: (r && r.error) || "移动失败" });
    });
  }

  // ══ 阶段3：模态系统（反向链接 / 未关联 / 回收站 / 快速切换） ══
  function openModal(title, bodyNode, opts) {
    // opts 可为函数(onClose) 或对象 { onClose, wide, dismissible }
    // dismissible=false（默认）：点击遮罩空白处不关闭弹窗，避免误触丢失编辑内容
    var onClose = null, wide = false, dismissible = false;
    if (typeof opts === "function") onClose = opts;
    else if (opts) { onClose = opts.onClose || null; wide = !!opts.wide; dismissible = !!opts.dismissible; }
    var box = el("modal-box");
    box.classList.toggle("wide", wide);
    el("modal-head").innerHTML = escapeHtml(title) + '<span class="modal-close">×</span>';
    var bd = el("modal-body"); bd.innerHTML = "";
    if (typeof bodyNode === "string") bd.innerHTML = bodyNode;
    else bd.appendChild(bodyNode);
    el("modal-overlay").style.display = "flex";
    el("modal-head").querySelector(".modal-close").addEventListener("click", function () { closeModal(); });
    // 仅当显式允许时，点击遮罩空白处才关闭；弹窗内部任意点击都不会冒泡触发关闭
    el("modal-overlay").onclick = function (e) {
      if (dismissible && e.target === el("modal-overlay")) closeModal();
    };
    function stopBox(e) { e.stopPropagation(); }
    box.addEventListener("click", stopBox);
    function closeModal() {
      el("modal-overlay").style.display = "none";
      el("modal-overlay").onclick = null;
      box.removeEventListener("click", stopBox);
      box.classList.remove("wide");
      if (onClose) onClose();
    }
    return closeModal;
  }
  function modalList(items, renderRow) {
    var list = document.createElement("div");
    list.className = "modal-list";
    if (!items || !items.length) { list.innerHTML = '<div class="modal-empty">空</div>'; return list; }
    items.forEach(function (it) { list.appendChild(renderRow(it)); });
    return list;
  }

  // 通用输入弹窗（替代浏览器原生 prompt）
  // opts: { title, label, placeholder, defaultValue, confirmText, onConfirm(value) }
  function inputDialog(opts) {
    var wrap = document.createElement("div");
    wrap.className = "mdn-input-dialog";
    var field = document.createElement("div");
    field.className = "mdn-field";
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = opts.placeholder || "";
    input.value = opts.defaultValue || "";
    field.appendChild(input);
    var hint = document.createElement("div");
    hint.className = "mdn-hint";
    hint.textContent = opts.hint || "";
    var err = document.createElement("div");
    err.className = "mdn-err";
    var actions = document.createElement("div");
    actions.className = "mdn-actions";
    var cancel = document.createElement("button");
    cancel.className = "btn"; cancel.textContent = "取消";
    var ok = document.createElement("button");
    ok.className = "btn primary"; ok.textContent = opts.confirmText || opts.okText || "确定";
    actions.appendChild(cancel); actions.appendChild(ok);
    wrap.appendChild(field); wrap.appendChild(hint); wrap.appendChild(err); wrap.appendChild(actions);

    var closeM = openModal(opts.title || "输入", wrap);
    function submit() {
      var v = input.value.trim();
      if (!v) { err.textContent = "名称不能为空"; input.focus(); return; }
      var res = opts.onConfirm ? opts.onConfirm(v) : true;
      if (res === false) { err.textContent = "名称无效，请更换"; input.focus(); return; }
      closeM();
    }
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", closeM);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
      else if (e.key === "Escape") { e.preventDefault(); closeM(); }
    });
    setTimeout(function () { input.focus(); input.select(); }, 40);
  }

  // 通用提示框（替代浏览器原生 alert）
  // opts: { title, message, okText, onOk }
  function messageBox(opts) {
    var wrap = document.createElement("div");
    wrap.className = "mdn-msgbox";
    var msg = document.createElement("div");
    msg.className = "mdn-msg";
    msg.textContent = opts.message || "";
    var actions = document.createElement("div");
    actions.className = "mdn-actions";
    var ok = document.createElement("button");
    ok.className = "btn primary"; ok.textContent = opts.okText || "确定";
    actions.appendChild(ok);
    wrap.appendChild(msg); wrap.appendChild(actions);
    var closeM = openModal(opts.title || "提示", wrap);
    ok.addEventListener("click", function () { closeM(); if (opts.onOk) opts.onOk(); });
    setTimeout(function () { ok.focus(); }, 40);
    return closeM;
  }

  // 通用确认框（替代浏览器原生 confirm）
  // opts: { title, message, okText, cancelText, danger, onOk }  onOk 返回 true 才关闭
  function confirmBox(opts) {
    var wrap = document.createElement("div");
    wrap.className = "mdn-msgbox";
    var msg = document.createElement("div");
    msg.className = "mdn-msg";
    msg.textContent = opts.message || "";
    var actions = document.createElement("div");
    actions.className = "mdn-actions";
    var cancel = document.createElement("button");
    cancel.className = "btn"; cancel.textContent = opts.cancelText || "取消";
    var ok = document.createElement("button");
    ok.className = "btn" + (opts.danger ? " danger" : " primary");
    ok.textContent = opts.okText || "确定";
    actions.appendChild(cancel); actions.appendChild(ok);
    wrap.appendChild(msg); wrap.appendChild(actions);
    var closeM = openModal(opts.title || "确认", wrap);
    function submit() { if (!opts.onOk || opts.onOk() !== false) closeM(); }
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", closeM);
    setTimeout(function () { ok.focus(); }, 40);
    return closeM;
  }

  // 轻量 toast 提示（顶部居中淡入淡出，不阻塞操作）
  function toast(message, kind) {
    var t = document.createElement("div");
    t.className = "mdn-toast" + (kind ? " " + kind : "");
    t.textContent = message;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 250);
    }, 2200);
  }

  function showBacklinks() {
    if (!state.currentRel) { messageBox({ title: "提示", message: "请先打开笔记" }); return; }
    call("list_backlinks", state.currentRel).then(function (res) {
      var list = modalList(res, function (r) {
        var row = document.createElement("div");
        row.className = "modal-row";
        row.innerHTML = "<span>" + escapeHtml(r.path) + "</span>" +
          (r.snippet ? '<span class="row-snippet">' + escapeHtml(r.snippet) + "</span>" : "");
        row.addEventListener("click", function () { openNote(r.path); closeM(); });
        return row;
      });
      var closeM = openModal("反向链接 · " + state.currentRel, list);
    });
  }
  function showUnlinked() {
    call("list_unlinked").then(function (res) {
      var list = modalList(res, function (path) {
        var row = document.createElement("div");
        row.className = "modal-row";
        row.innerHTML = "<span>" + escapeHtml(path) + "</span>";
        row.addEventListener("click", function () { openNote(path); closeM(); });
        return row;
      });
      var closeM = openModal("未关联笔记", list);
    });
  }
  function showTrash() {
    function refresh() {
      call("list_trash").then(function (res) {
        var list = modalList(res, function (r) {
          var row = document.createElement("div");
          row.className = "modal-row";
          row.innerHTML = "<span>" + escapeHtml(r.path) + "</span>";
          var actions = document.createElement("span");
          actions.className = "row-actions";
          var restore = document.createElement("span");
          restore.className = "mini"; restore.textContent = "恢复";
          restore.addEventListener("click", function (e) {
            e.stopPropagation();
            call("restore_trash", r.path).then(function (x) {
              if (x.success) { loadTree(); refresh(); } else messageBox({ title: "恢复失败", message: x.error || "恢复失败" });
            });
          });
          var del = document.createElement("span");
          del.className = "mini danger"; del.textContent = "删除";
          del.addEventListener("click", function (e) {
            e.stopPropagation();
            confirmBox({ title: "彻底删除", message: "彻底删除「" + r.path + "」？", danger: true, onOk: function () {
              call("delete_trash", r.path).then(function (x) { if (x.success) refresh(); else messageBox({ title: "删除失败", message: x.error || "删除失败" }); });
            } });
          });
          actions.appendChild(restore); actions.appendChild(del);
          row.appendChild(actions);
          row.addEventListener("click", function () { openNote(r.path); });  // 仅展示路径；恢复后才有实体
          return row;
        });
        // 头部加清空按钮
        var head = document.createElement("div");
        head.style.cssText = "display:flex;gap:8px;margin-bottom:10px;";
        var emptyBtn = document.createElement("button");
        emptyBtn.className = "btn danger"; emptyBtn.textContent = "清空回收站";
        emptyBtn.addEventListener("click", function () {
          confirmBox({ title: "清空回收站", message: "确定清空回收站？无法恢复", danger: true, onOk: function () {
            call("empty_trash").then(function (x) { if (x.success) refresh(); else messageBox({ title: "清空失败", message: x.error || "清空失败" }); });
          } });
        });
        head.appendChild(emptyBtn);
        var wrap = document.createElement("div");
        wrap.appendChild(head); wrap.appendChild(list);
        var closeM = openModal("回收站", wrap);
      });
    }
    refresh();
  }

  // ── 每日笔记 ──────────────────────────────────────────
  function openDailyNote() {
    call("open_daily_note").then(function (res) {
      if (!res.success) { messageBox({ title: "打开失败", message: res.error || "打开失败" }); return; }
      loadTree(); openNote(res.rel_path);
    });
  }

  // ── 闪念笔记 ──────────────────────────────────────────
  function openFlashNote() {
    var overlay = el("flash-overlay");
    var input = el("flash-input");
    overlay.style.display = "flex";
    input.value = "";
    setTimeout(function () { input.focus(); }, 30);
    function close() { overlay.style.display = "none"; input.onkeydown = null; overlay.onclick = null; }
    input.onkeydown = function (e) {
      if (e.key === "Enter") {
        var text = input.value.trim();
        close();
        if (!text) return;
        call("create_flash_note", text).then(function (r) {
          if (!r.success) { messageBox({ title: "保存失败", message: r.error || "保存失败" }); return; }
          loadTree(); switchSideTab("files");
        });
      } else if (e.key === "Escape") {
        close();
      }
    };
    overlay.onclick = function (e) { if (e.target === overlay) close(); };
  }

  // ── 最近文件 ──────────────────────────────────────────
  function loadRecent() {
    call("list_recent_files", 12).then(function (list) {
      var box = el("recent-list"); box.innerHTML = "";
      if (!list || !list.length) { box.innerHTML = '<div class="modal-empty">暂无最近文件</div>'; return; }
      list.forEach(function (rel, i) {
        var div = document.createElement("div");
        div.className = "recent-item";
        div.innerHTML = '<span class="rk">Ctrl+' + (i + 1) + '</span>' +
          '<span>' + escapeHtml(rel.split("/").pop().replace(/\.md$/, "")) + '</span>' +
          '<span class="rp">' + escapeHtml(rel) + '</span>';
        div.addEventListener("click", function () { openNote(rel); switchSideTab("files"); });
        box.appendChild(div);
      });
    });
  }

  // ── 收藏 ──────────────────────────────────────────────
  function loadFavorites() {
    call("list_favorites").then(function (list) {
      var box = el("favorites-list"); box.innerHTML = "";
      if (!list || !list.length) { box.innerHTML = '<div class="modal-empty">暂无收藏 · 在笔记右键可收藏 ⭐</div>'; return; }
      list.forEach(function (rel) {
        var div = document.createElement("div");
        div.className = "recent-item";
        div.innerHTML = '<span class="rk">⭐</span>' +
          '<span>' + escapeHtml(rel.split("/").pop().replace(/\.md$/, "")) + '</span>' +
          '<span class="rp">' + escapeHtml(rel) + '</span>';
        div.addEventListener("click", function () { openNote(rel); switchSideTab("files"); });
        box.appendChild(div);
      });
    });
  }
  function toggleFavorite(relPath) {
    if (!relPath) { messageBox({ title: "提示", message: "请先打开或选择一篇笔记" }); return; }
    call("toggle_favorite", relPath).then(function (r) {
      if (!r || !r.success) { messageBox({ title: "收藏失败", message: (r && r.error) || "收藏失败" }); return; }
      toast(r.favorite ? "已收藏 ⭐" : "已取消收藏", "ok");
      if (el("side-favorites").style.display !== "none") loadFavorites();
    });
  }

  // ── 孤儿附件清理 ──────────────────────────────────────
  function showOrphanAttachments() {
    call("list_orphan_attachments").then(function (list) {
      if (!list || !list.length) { messageBox({ title: "孤儿附件", message: "没有孤儿附件 🎉" }); return; }
      var rows = list.map(function (o) {
        return '<li class="modal-item"><span>🖼 ' + esc(o.name) +
          ' <span class="tip">(' + (o.size || 0) + ' B)</span></span>' +
          '<span class="row-btns"><button class="mini del-o" data-rp="' + esc(o.rel_path) +
          '">删除</button></span></li>';
      }).join("");
      openModal("孤儿附件 (" + list.length + ")", "<ul class='modal-list'>" + rows + "</ul>");
      Array.prototype.forEach.call(document.querySelectorAll(".del-o"), function (b) {
        b.addEventListener("click", function () {
          confirmBox({ title: "删除附件", message: "删除附件「" + b.dataset.rp + "」？", danger: true, onOk: function () {
            call("delete_attachment", b.dataset.rp).then(function (r) {
              if (r && r.success) b.closest(".modal-item").remove();
              else messageBox({ title: "删除失败", message: "删除失败：" + ((r && r.error) || "") });
            });
          } });
        });
      });
    });
  }

  // ── 笔记属性 ──────────────────────────────────────────
  function showNoteProperties(relPath) {
    relPath = relPath || state.currentRel;
    if (!relPath) { messageBox({ title: "提示", message: "请先打开或选择一篇笔记" }); return; }
    call("get_note_properties", relPath).then(function (r) {
      if (!r || !r.success) { messageBox({ title: "读取失败", message: (r && r.error) || "读取失败" }); return; }
      function fmtSize(n) {
        if (n < 1024) return n + " B";
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
        return (n / (1024 * 1024)).toFixed(2) + " MB";
      }
      function fmtTime(ts) {
        if (!ts) return "-";
        var d = new Date(ts * 1000);
        return d.toLocaleString();
      }
      var html = '<div class="about-card" style="text-align:left;">' +
        '<div class="ac-rows">' +
        row("名称", r.name + ".md") +
        row("路径", r.rel_path) +
        row("绝对路径", r.absolute_path) +
        row("大小", fmtSize(r.size)) +
        row("创建时间", fmtTime(r.created)) +
        row("修改时间", fmtTime(r.modified)) +
        row("字符数", r.char_count) +
        row("字数（不含空格）", r.word_count) +
        row("行数", r.line_count) +
        row("标签", (r.tags && r.tags.length) ? r.tags.map(function (t) { return "#" + t; }).join(" ") : "无") +
        "</div>" +
        '<div class="mdn-actions"><button class="btn primary" id="btn-edit-props">✎ 编辑属性</button></div></div>';
      var closeProps = openModal("笔记属性 · " + r.name, html);
      var ed = el("btn-edit-props");
      if (ed) ed.addEventListener("click", function () { closeProps(); editNoteProperties(relPath); });
    });
    function row(k, v) {
      return '<div class="ac-row"><span>' + escapeHtml(k) + '</span><span>' + escapeHtml(String(v)) + '</span></div>';
    }
  }

  // ── 聚焦模式 ──────────────────────────────────────────
  function toggleFocusMode() {
    document.body.classList.toggle("focus-mode");
    // 隐藏/显示面板后必须刷新 CodeMirror，否则编辑区尺寸/焦点异常
    if (editor) {
      setTimeout(function () {
        editor.refresh();
        if (state.currentRel) editor.focus();
      }, 80);
    }
  }

  // 快速切换器 Ctrl+P
  function showQuickSwitcher() {
    var box = document.createElement("div");
    var input = document.createElement("input");
    input.type = "text"; input.placeholder = "输入笔记名过滤…"; input.className = "input";
    var list = document.createElement("div"); list.className = "modal-list";
    box.appendChild(input); box.appendChild(list);
    var closeM = openModal("快速切换 (Ctrl+P)", box);
    function render(filter) {
      call("list_note_names").then(function (names) {
        list.innerHTML = "";
        var f = (filter || "").toLowerCase();
        var matched = names.filter(function (n) { return n.toLowerCase().indexOf(f) !== -1; }).slice(0, 50);
        if (!matched.length) { list.innerHTML = '<div class="modal-empty">无匹配</div>'; return; }
        matched.forEach(function (n) {
          var row = document.createElement("div");
          row.className = "modal-row"; row.textContent = n;
          row.addEventListener("click", function () { openNote(n + ".md"); closeM(); });
          list.appendChild(row);
        });
      });
    }
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        var first = list.querySelector(".modal-row");
        if (first) first.click();
      }
    });
    render("");
    setTimeout(function () { input.focus(); }, 30);
  }

  // ── 全局命令面板 Ctrl+K ───────────────────────────────
  function showCommandPalette() {
    var box = document.createElement("div");
    box.className = "cmd-palette";
    var input = document.createElement("input");
    input.type = "text"; input.placeholder = "输入命令、笔记名或 #标签…"; input.className = "input cmd-input";
    var list = document.createElement("div"); list.className = "cmd-list";
    box.appendChild(input); box.appendChild(list);
    var closeM = openModal("命令面板 · Ctrl+K", box, { wide: true });

    // 静态命令（动作型）
    var commands = [
      { icon: "📝", label: "新建笔记", run: function () { closeM(); newNote(); } },
      { icon: "💾", label: "保存当前笔记", run: function () { closeM(); doSave(); } },
      { icon: "🔆", label: "切换聚焦模式", run: function () { closeM(); toggleFocusMode(); } },
      { icon: "⛶", label: "预览全屏", run: function () { closeM(); togglePreviewFull(); } },
      { icon: "📅", label: "打开每日笔记", run: function () { closeM(); openDailyNote(); } },
      { icon: "⚡", label: "闪念笔记", run: function () { closeM(); openFlashNote(); } },
      { icon: "🔗", label: "反向链接", run: function () { closeM(); showBacklinks(); } },
      { icon: "🗑", label: "回收站", run: function () { closeM(); showTrash(); } },
      { icon: "🕑", label: "历史版本", run: function () { closeM(); showHistory(); } },
      { icon: "🔄", label: "同步设置", run: function () { closeM(); showSync(); } },
      { icon: "⚙", label: "设置", run: function () { closeM(); showSettings(); } },
      { icon: "🏷", label: "标签视图", run: function () { closeM(); switchSideTab("tags"); } },
      { icon: "🕒", label: "最近文件视图", run: function () { closeM(); switchSideTab("recent"); } },
      { icon: "🕸", label: "关系图谱", run: function () { closeM(); switchSideTab("graph"); } },
      { icon: "📊", label: "字数热力图", run: function () { closeM(); switchSideTab("heatmap"); } },
      { icon: "📋", label: "模板笔记", run: function () { closeM(); switchSideTab("templates"); } },
      { icon: "📁", label: "从文件夹导入", run: function () { closeM(); importFrom("folder"); } },
      { icon: "🌿", label: "导入语雀", run: function () { closeM(); importFrom("yuque"); } },
      { icon: "📝", label: "导入 Notion", run: function () { closeM(); importFrom("notion"); } },
    ];
    var noteNames = [];   // 延迟加载
    var tagsList = [];    // 延迟加载
    call("list_note_names").then(function (n) { noteNames = n || []; });
    call("list_tags").then(function (t) { tagsList = Object.keys(t || {}); });

    function render(filter) {
      var f = (filter || "").trim().toLowerCase();
      list.innerHTML = "";
      var items = [];
      // 1) 精确/模糊匹配笔记名
      if (f && !f.startsWith("#")) {
        noteNames.filter(function (n) { return n.toLowerCase().indexOf(f) !== -1; }).slice(0, 8).forEach(function (n) {
          items.push({ icon: "📄", label: n, sub: "笔记", run: function () { closeM(); openNote(n + ".md"); } });
        });
      }
      // 2) # 标签筛选
      if (f.startsWith("#") || (f && tagsList.some(function (t) { return t.toLowerCase().indexOf(f) !== -1; }))) {
        tagsList.filter(function (t) { return t.toLowerCase().indexOf(f.replace(/^#/, "")) !== -1; }).slice(0, 6).forEach(function (t) {
          items.push({ icon: "🏷", label: "#" + t, sub: "标签", run: function () { closeM(); searchByTag(t); } });
        });
      }
      // 3) 命令
      commands.forEach(function (c) {
        if (!f || c.label.toLowerCase().indexOf(f) !== -1) items.push({ icon: c.icon, label: c.label, sub: "命令", run: c.run });
      });
      if (!items.length) { list.innerHTML = '<div class="modal-empty">无匹配结果</div>'; return; }
      items.forEach(function (it, i) {
        var row = document.createElement("div");
        row.className = "cmd-row" + (i === 0 ? " active" : "");
        row.innerHTML = '<span class="cmd-ico">' + it.icon + '</span>' +
          '<span class="cmd-label">' + escapeHtml(it.label) + '</span>' +
          (it.sub ? '<span class="cmd-sub">' + escapeHtml(it.sub) + '</span>' : '');
        row.addEventListener("click", function () { it.run(); });
        list.appendChild(row);
      });
    }
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      var rows = Array.prototype.slice.call(list.querySelectorAll(".cmd-row"));
      var cur = rows.findIndex(function (r) { return r.classList.contains("active"); });
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (rows.length) { if (cur >= 0) rows[cur].classList.remove("active"); var n = (cur + 1) % rows.length; rows[n].classList.add("active"); rows[n].scrollIntoView({ block: "nearest" }); }
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        if (rows.length) { if (cur >= 0) rows[cur].classList.remove("active"); var p = (cur - 1 + rows.length) % rows.length; rows[p].classList.add("active"); rows[p].scrollIntoView({ block: "nearest" }); }
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        var sel = list.querySelector(".cmd-row.active") || list.querySelector(".cmd-row");
        if (sel) sel.click();
      }
    });
    render("");
    setTimeout(function () { input.focus(); }, 30);
  }
  function searchByTag(tag) {
    switchSideTab("search");
    var si = el("search-input");
    if (si) { si.value = "#" + tag; si.dispatchEvent(new Event("input")); }
  }

  // ── 关于 / 自启 ───────────────────────────────────────
  function about() {
    var ver = state.version || "1.0.0";
    var card = document.createElement("div");
    card.className = "about-card";
    card.innerHTML =
      '<div class="ac-logo">✦</div>' +
      '<div class="ac-title">Teyvat Irminsul · 提瓦特世界树</div>' +
      '<div class="ac-tag">愿风神护佑你的每一个灵感</div>' +
      '<div class="ac-rows">' +
      row("版本", ver) +
      row("技术栈", "Electron · Python · CodeMirror") +
      row("数据存储", state.notesDir || "本地文件") +
      row("核心特性", "Markdown · 双向链接 · 离线优先") +
      '</div>' +
      '<div class="ac-foot">完全离线 · 数据自有 · 不收集任何信息</div>';
    openModal("关于 Teyvat Irminsul", card);
    function row(k, v) {
      return '<div class="ac-row"><span>' + escapeHtml(k) + '</span><span>' + escapeHtml(v) + '</span></div>';
    }
  }
  function initStartupBtn() {
    // 开机自启由 Electron 主进程管理（app.setLoginItemSettings）
    if (typeof window.desktop === "undefined" || !window.desktop.isStartupEnabled) return;
    var b = el("btn-startup");
    window.desktop.isStartupEnabled().then(function (on) {
      b.classList.toggle("primary", !!on);
      b.textContent = on ? "✓ 开机自启" : "开机自启";
    });
    b.addEventListener("click", function () {
      window.desktop.toggleStartup().then(function (on) {
        b.classList.toggle("primary", !!on);
        b.textContent = on ? "✓ 开机自启" : "开机自启";
      });
    });
  }

  // ── 启动 ──────────────────────────────────────────────
  function bootstrap() {
    initEditor();
    initGutters();
    initSideTabs();
    // 预览区滚动 → 编辑区按比例联动
    var pv = el("preview");
    if (pv) pv.addEventListener("scroll", syncEditorFromPreview);

    on("btn-new", "click", newNote);
    on("btn-save", "click", doSave);
    on("btn-toggle-preview", "click", togglePreview);
    on("btn-toggle-outline", "click", toggleOutline);
    on("btn-preview-full", "click", togglePreviewFull);
    on("btn-refresh", "click", loadTree);
    on("btn-about", "click", about);
    on("btn-new-folder", "click", newFolder);
    on("btn-toggle-sidebar", "click", function () {
      document.body.classList.toggle("show-sidebar");
    });
    on("btn-more", "click", function (e) {
      e.stopPropagation();
      document.body.classList.toggle("show-more");
    });
    // 二级折叠菜单：点击标题展开/收起，点击子按钮执行后关闭整个菜单
    document.querySelectorAll(".submenu-title").forEach(function (t) {
      t.addEventListener("click", function (e) {
        e.stopPropagation();
        t.closest(".submenu").classList.toggle("open");
      });
    });
    document.querySelectorAll(".submenu-body .btn").forEach(function (b) {
      b.addEventListener("click", function () { document.body.classList.remove("show-more"); });
    });
    // 侧栏「⋮」更多 Tab
    var sideMore = document.querySelector(".side-more");
    var sideMoreBtn = document.querySelector(".side-tab-more");
    if (sideMore && sideMoreBtn) {
      sideMoreBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        sideMore.classList.toggle("open");
      });
      sideMore.querySelectorAll(".side-tab[data-tab]").forEach(function (b) {
        b.addEventListener("click", function () {
          switchSideTab(b.dataset.tab);
          sideMore.classList.remove("open");
        });
      });
      document.addEventListener("click", function (e) {
        if (sideMore && !e.target.closest(".side-more")) sideMore.classList.remove("open");
      });
    }
    document.addEventListener("click", function (e) {
      // 点击「⋯」或菜单内部非功能按钮时不关闭；点外部关闭
      var inside = e.target.closest("#btn-more") || e.target.closest("#tb-more");
      if (!inside) document.body.classList.remove("show-more");
    });
    on("btn-daily", "click", openDailyNote);
    on("btn-flash", "click", openFlashNote);
    on("btn-orphan", "click", showOrphanAttachments);
    on("btn-note-props", "click", function () { showNoteProperties(); });
    on("btn-fav", "click", function () { if (state.currentRel) toggleFavorite(state.currentRel); else messageBox({ title: "提示", message: "请先打开一篇笔记" }); });
    on("btn-focus", "click", toggleFocusMode);
    on("btn-backlinks", "click", showBacklinks);
    on("btn-unlinked", "click", showUnlinked);
    on("btn-trash", "click", showTrash);
    on("btn-history", "click", showHistory);
    on("btn-sync", "click", showSync);
    on("btn-settings", "click", showSettings);
    on("btn-export", "click", exportZip);
    on("btn-export-site", "click", exportSite);
    on("btn-pdf", "click", exportPdf);
    on("btn-import-folder", "click", function () { importFrom("folder"); });
    on("btn-import-yuque", "click", function () { importFrom("yuque"); });
    on("btn-import-notion", "click", function () { importFrom("notion"); });
    on("btn-import-zip", "click", function () { el("import-file").click(); });
    on("import-file", "change", onImportFile);
    on("ee-new", "click", newNote);
    on("btn-png", "click", exportNotePng);
    on("btn-encrypt", "click", showEncryptDialog);
    on("btn-applock", "click", showAppLockSetup);
    on("btn-batchtag", "click", showBatchTag);
    document.addEventListener("click", function () { el("tab-menu").style.display = "none"; });

    // 编辑工具栏按钮
    Array.prototype.forEach.call(document.querySelectorAll("#editor-toolbar .etb"), function (b) {
      b.addEventListener("click", function () { execEditorAction(b.getAttribute("data-act")); });
    });

    document.addEventListener("keydown", function (e) {
      var ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && (e.key === "p" || e.key === "P")) {
        e.preventDefault(); showQuickSwitcher(); return;
      }
      if (ctrl && (e.key === "k" || e.key === "K")) {
        e.preventDefault(); showCommandPalette(); return;
      }
      if (ctrl && (e.key === "d" || e.key === "D")) {
        e.preventDefault(); openDailyNote(); return;
      }
      if (ctrl && e.shiftKey && (e.key === "N" || e.key === "n")) {
        e.preventDefault(); openFlashNote(); return;
      }
      if (e.key === "F11") {
        e.preventDefault(); toggleFocusMode(); return;
      }
      // Ctrl+. 切换聚焦模式（F11 在部分 WebView/浏览器会被系统全屏抢占，故提供备用键）
      if (ctrl && (e.key === "." || e.code === "Period")) {
        e.preventDefault(); toggleFocusMode(); return;
      }
      // 聚焦模式下按 Esc 退出
      if (e.key === "Escape" && document.body.classList.contains("focus-mode")) {
        e.preventDefault(); toggleFocusMode(); return;
      }
      // 预览全屏模式下按 Esc 退出
      if (e.key === "Escape" && document.body.classList.contains("preview-full")) {
        e.preventDefault(); togglePreviewFull(); return;
      }
      // Ctrl+1~9 打开最近文件
      if (ctrl && /^[1-9]$/.test(e.key)) {
        e.preventDefault();
        call("list_recent_files", 12).then(function (list) {
          var idx = parseInt(e.key, 10) - 1;
          if (list && list[idx]) openNote(list[idx]);
        });
      }
    });

    call("get_notes_dir").then(function (d) { state.notesDir = d || ""; el("status-notesdir").textContent = state.notesDir; });
    call("get_config").then(function (cfg) {
      cfg = cfg || {};
      var ed = cfg.editor || {};
      state.typewriterScroll = ed.typewriter_scroll !== false;
      state.scrollLinked = ed.scroll_linked !== false;
      state.autoSave = ed.auto_save !== false;
      state.autoSaveDelayMs = ed.auto_save_delay_ms || 2000;
      state.version = cfg.version || "1.0.0";
      state.restoreSession = ed.restore_session !== false;
      state.keymap = ed.keymap || {};
      applyKeymap(state.keymap);
      // 应用主题
      applyTheme(cfg.theme || "genshin");
    });
    var resizeT = null;
    window.addEventListener("resize", function () {
      if (resizeT) clearTimeout(resizeT);
      resizeT = setTimeout(function () { if (state.typewriterScroll) scrollCursorToCenter(true); }, 150);
    });
    Promise.all([
      call("list_note_names").then(function (n) { state.noteNames = n || []; }),
      call("list_code_languages").then(function (l) { state.codeLangs = l || []; }),
      call("list_note_aliases").then(function (a) { state.noteAliases = a || []; }),
    ]).then(function () {
      renderTabs(); loadTree(); initStartupBtn(); renderPreview(); renderOutline();
      checkDraftsOnStartup();
      updateEmptyState();
      initAppLock();
      restoreSessionOnStartup();
      populateSearchScope();
    });
  }

  // ── 阶段4+：应用锁 ─────────────────────────────────
  function initAppLock() {
    call("app_lock_enabled").then(function (en) {
      if (!en) return;
      state._lockShowing = true;
      var ov = el("app-lock");
      ov.style.display = "flex";
      document.body.classList.add("app-locked");
      var input = el("app-lock-input");
      input.value = ""; el("app-lock-msg").textContent = "";
      setTimeout(function () { input.focus(); }, 60);
      function tryUnlock() {
        var pw = input.value;
        if (!pw) { el("app-lock-msg").textContent = "请输入密码"; input.focus(); return; }
        call("app_lock_verify", pw).then(function (v) {
          if (v) {
            ov.style.display = "none";
            document.body.classList.remove("app-locked");
            state._lockShowing = false;
            el("app-lock-msg").textContent = "";
          } else { el("app-lock-msg").textContent = "密码错误"; input.select(); }
        });
      }
      el("app-lock-unlock").onclick = tryUnlock;
      el("app-lock-reset").onclick = function () { showAppLockSetup(); };
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); tryUnlock(); } });
    });
  }
  function showAppLockSetup() {
    call("app_lock_enabled").then(function (en) {
      if (!en) { showAppLockSet(); return; }
      var box = document.createElement("div"); box.className = "mdn-input-dialog";
      var note = document.createElement("div"); note.className = "mdn-hint";
      note.textContent = "应用锁已启用。修改或关闭需验证当前密码。";
      var old = document.createElement("input"); old.className = "input"; old.type = "password"; old.placeholder = "当前密码";
      var err = document.createElement("div"); err.className = "mdn-err";
      var actions = document.createElement("div"); actions.className = "mdn-actions";
      var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "关闭锁";
      var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "修改密码";
      actions.appendChild(cancel); actions.appendChild(ok);
      box.appendChild(note); box.appendChild(old); box.appendChild(err); box.appendChild(actions);
      var closeM = openModal("应用锁", box);
      ok.addEventListener("click", function () {
        call("app_lock_verify", old.value).then(function (v) {
          if (!v) { err.textContent = "当前密码错误"; return; }
          closeM(); showAppLockSet();
        });
      });
      cancel.addEventListener("click", function () {
        call("app_lock_verify", old.value).then(function (v) {
          if (!v) { err.textContent = "当前密码错误"; return; }
          call("app_lock_disable", old.value).then(function (r) {
            if (r && r.success) {
              closeM(); toast("应用锁已关闭", "ok");
              if (state._lockShowing) { el("app-lock").style.display = "none"; document.body.classList.remove("app-locked"); state._lockShowing = false; }
            } else messageBox({ title: "失败", message: (r && r.error) || "关闭失败" });
          });
        });
      });
    });
  }
  function showAppLockSet() {
    var box = document.createElement("div"); box.className = "mdn-input-dialog";
    var f1 = document.createElement("input"); f1.className = "input"; f1.type = "password"; f1.placeholder = "新密码";
    var f2 = document.createElement("input"); f2.className = "input"; f2.type = "password"; f2.placeholder = "确认密码";
    var err = document.createElement("div"); err.className = "mdn-err";
    var actions = document.createElement("div"); actions.className = "mdn-actions";
    var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
    var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "保存";
    actions.appendChild(cancel); actions.appendChild(ok);
    box.appendChild(f1); box.appendChild(f2); box.appendChild(err); box.appendChild(actions);
    var closeM = openModal("设置应用锁密码", box);
    ok.addEventListener("click", function () {
      if (!f1.value) { err.textContent = "密码不能为空"; return; }
      if (f1.value !== f2.value) { err.textContent = "两次输入不一致"; return; }
      call("app_lock_set", f1.value).then(function (r) {
        if (r && r.success) { closeM(); toast("应用锁已启用（下次启动生效）", "ok"); }
        else messageBox({ title: "设置失败", message: (r && r.error) || "设置失败" });
      });
    });
    cancel.addEventListener("click", closeM);
    setTimeout(function () { f1.focus(); }, 40);
  }

  // ── 阶段4+：单篇笔记加密 ─────────────────────────────
  function showEncryptDialog() {
    if (!state.currentRel) { messageBox({ title: "提示", message: "请先打开一篇笔记" }); return; }
    call("encrypt_status", state.currentRel).then(function (st) {
      if (st && st.encrypted) {
        confirmBox({
          title: "解密笔记", okText: "解密", danger: false,
          message: "当前笔记已加密，磁盘上为密文。确认解密为明文？",
          onOk: function () {
            call("encrypt_toggle", state.currentRel, "", false).then(function (r) {
              if (r && r.success) { toast("已解密", "ok"); openNote(state.currentRel); }
              else messageBox({ title: "解密失败", message: (r && r.error) || "解密失败" });
            });
          }
        });
      } else {
        var box = document.createElement("div"); box.className = "mdn-input-dialog";
        var note = document.createElement("div"); note.className = "mdn-hint";
        note.textContent = "用主密码派生密钥加密。若已在设置中设置加密主密码，则留空使用主密码。";
        var f = document.createElement("input"); f.className = "input"; f.type = "password"; f.placeholder = "加密密码（可留空用主密码）";
        var err = document.createElement("div"); err.className = "mdn-err";
        var actions = document.createElement("div"); actions.className = "mdn-actions";
        var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
        var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "加密";
        actions.appendChild(cancel); actions.appendChild(ok);
        box.appendChild(note); box.appendChild(f); box.appendChild(err); box.appendChild(actions);
        var closeM = openModal("加密当前笔记", box);
        ok.addEventListener("click", function () {
          call("encrypt_toggle", state.currentRel, f.value, true).then(function (r) {
            if (r && r.success) { closeM(); toast("已加密", "ok"); }
            else { err.textContent = (r && r.error) || "加密失败"; }
          });
        });
        cancel.addEventListener("click", closeM);
        setTimeout(function () { f.focus(); }, 40);
      }
    });
  }

  // ── 阶段4+：导出当前笔记为 PNG ─────────────────────────
  function cssTextForExport(keys) {
    var out = "";
    try {
      var sheets = document.styleSheets;
      for (var i = 0; i < sheets.length; i++) {
        var s = sheets[i];
        if (!s.href) continue;
        var matched = keys.some(function (k) { return s.href.indexOf(k) !== -1; });
        if (!matched) continue;
        for (var j = 0; j < s.cssRules.length; j++) {
          try { out += s.cssRules[j].cssText + "\n"; } catch (e) { /* ignore */ }
        }
      }
    } catch (e) { /* ignore */ }
    return out;
  }
  function exportNotePng() {
    if (!state.currentRel) { messageBox({ title: "提示", message: "请先打开一篇笔记" }); return; }
    toast("正在生成图片…", "ok");
    call("export_note_png_html", state.currentRel).then(function (r) {
      if (!r || !r.success) { messageBox({ title: "导出失败", message: (r && r.error) || "导出失败" }); return; }
      // 把主题与预览样式内联进 foreignObject，避免 iframe 跨域污染 canvas
      var css = cssTextForExport(["theme.css", "preview.css"]);
      var doc = "<!DOCTYPE html><html><head><meta charset='utf-8'><style>" + css + "</style></head><body>" + r.html + "</body></html>";
      var svgNS = "http://www.w3.org/2000/svg";
      var svg = document.createElementNS(svgNS, "svg");
      var width = 880;
      var fo = document.createElementNS(svgNS, "foreignObject");
      fo.setAttribute("width", width); fo.setAttribute("height", "10000");
      fo.innerHTML = doc;
      svg.appendChild(fo);
      svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      svg.setAttribute("xmlns:xhtml", "http://www.w3.org/1999/xhtml");
      var blob = new Blob([svg.outerHTML], { type: "image/svg+xml;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.decoding = "async"; img.style.width = width + "px";
      img.onload = function () {
        var scale = 2;
        var canvas = document.createElement("canvas");
        canvas.width = width * scale;
        canvas.height = (img.naturalHeight || 1000) * scale;
        var ctx = canvas.getContext("2d");
        ctx.scale(scale, scale);
        ctx.fillStyle = getComputedStyle(document.body).backgroundColor || "#fff";
        ctx.fillRect(0, 0, canvas.width / scale, canvas.height / scale);
        ctx.drawImage(img, 0, 0, width);
        var a = document.createElement("a");
        a.download = state.currentRel.replace(/\.md$/, "").replace(/[/\\]/g, "_") + ".png";
        a.href = canvas.toDataURL("image/png");
        a.click();
        URL.revokeObjectURL(url);
        toast("已导出 PNG", "ok");
      };
      img.onerror = function () { URL.revokeObjectURL(url); messageBox({ title: "导出失败", message: "图片渲染失败" }); };
      img.src = url;
    });
  }

  // ── 阶段4+：批量打标签 / 标签重命名 ─────────────────────
  function parseTags(str) {
    return (str || "").split(",").map(function (t) { return t.trim().replace(/^#/, ""); }).filter(Boolean);
  }
  function showBatchTag() {
    var box = document.createElement("div"); box.className = "mdn-input-dialog";
    var selAll = document.createElement("button"); selAll.className = "mini"; selAll.textContent = "全选";
    var filter = document.createElement("input"); filter.className = "input"; filter.type = "text"; filter.placeholder = "筛选笔记…";
    var selList = document.createElement("div");
    selList.className = "modal-list batch-list"; selList.style.maxHeight = "260px"; selList.style.overflowY = "auto";
    var addTag = document.createElement("input"); addTag.className = "input"; addTag.type = "text"; addTag.placeholder = "添加标签（逗号分隔）…";
    var rmTag = document.createElement("input"); rmTag.className = "input"; rmTag.type = "text"; rmTag.placeholder = "删除标签（逗号分隔）…";
    var counts = document.createElement("div"); counts.className = "mdn-hint";
    var bar = document.createElement("div"); bar.className = "batch-progress"; bar.style.display = "none";
    var barFill = document.createElement("div"); barFill.className = "batch-progress-fill"; barFill.style.width = "0%";
    bar.appendChild(barFill);
    var err = document.createElement("div"); err.className = "mdn-err";
    var actions = document.createElement("div"); actions.className = "mdn-actions";
    var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
    var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "批量执行";
    actions.appendChild(cancel); actions.appendChild(ok);
    box.appendChild(selAll); box.appendChild(filter); box.appendChild(selList);
    box.appendChild(addTag); box.appendChild(rmTag); box.appendChild(counts); box.appendChild(bar);
    box.appendChild(err); box.appendChild(actions);
    var closeM = openModal("批量打标签", box, { wide: true, dismissible: true });
    var all = [], checked = {};
    function render() {
      var f = filter.value.toLowerCase();
      selList.innerHTML = "";
      all.filter(function (n) { return n.toLowerCase().indexOf(f) !== -1; }).forEach(function (n) {
        var label = document.createElement("label");
        label.className = "batch-item";
        var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!checked[n];
        cb.addEventListener("change", function () {
          if (cb.checked) checked[n] = true; else delete checked[n];
          counts.textContent = "已选 " + Object.keys(checked).length + " 篇";
        });
        var sp = document.createElement("span"); sp.textContent = n;
        label.appendChild(cb); label.appendChild(sp);
        selList.appendChild(label);
      });
      counts.textContent = "已选 " + Object.keys(checked).length + " 篇";
    }
    filter.addEventListener("input", render);
    selAll.addEventListener("click", function () { all.forEach(function (n) { checked[n] = true; }); render(); });
    call("list_note_names").then(function (names) { all = names || []; render(); });
    cancel.addEventListener("click", closeM);
    ok.addEventListener("click", function () {
      var addTags = parseTags(addTag.value), rmTags = parseTags(rmTag.value);
      if (!addTags.length && !rmTags.length) { err.textContent = "请填写要添加或删除的标签"; return; }
      var paths = Object.keys(checked);
      if (!paths.length) { err.textContent = "请至少勾选一篇笔记"; return; }
      ok.disabled = true; bar.style.display = "block";
      var idx = 0, fail = 0;
      (function next() {
        if (idx >= paths.length) {
          barFill.style.width = "100%";
          toast("批量处理完成：成功 " + (paths.length - fail) + (fail ? "，失败 " + fail : ""), fail ? "warn" : "ok");
          closeM(); loadTree(); loadTags();
          return;
        }
        var rel = paths[idx++];
        call("batch_tag", [rel], addTags, rmTags).then(function (r) {
          if (!r || !r.success || !(r.ok || 0)) fail++;
          barFill.style.width = Math.round((idx / paths.length) * 100) + "%";
          next();
        });
      })();
    });
  }
  function showTagRename() {
    call("list_tags").then(function (tags) {
      var arr = Object.keys(tags || {});
      if (!arr.length) { messageBox({ title: "提示", message: "暂无标签" }); return; }
      var box = document.createElement("div"); box.className = "mdn-input-dialog";
      var sel = document.createElement("select"); sel.className = "input";
      arr.forEach(function (t) {
        var o = document.createElement("option"); o.value = t; o.textContent = "#" + t + "（" + tags[t] + " 篇）"; sel.appendChild(o);
      });
      var field = document.createElement("input"); field.className = "input"; field.type = "text"; field.placeholder = "新标签名（不带 #）";
      var err = document.createElement("div"); err.className = "mdn-err";
      var actions = document.createElement("div"); actions.className = "mdn-actions";
      var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
      var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "重命名";
      actions.appendChild(cancel); actions.appendChild(ok);
      box.appendChild(sel); box.appendChild(field); box.appendChild(err); box.appendChild(actions);
      var closeM = openModal("标签重命名", box);
      ok.addEventListener("click", function () {
        var newT = field.value.trim().replace(/^#/, "");
        if (!newT) { err.textContent = "标签不能为空"; return; }
        call("tag_rename", sel.value, newT).then(function (r) {
          if (r && r.success) { closeM(); toast("已重命名，更新 " + r.count + " 篇", "ok"); loadTags(); loadTree(); loadRecent(); }
          else messageBox({ title: "重命名失败", message: (r && r.error) || "失败" });
        });
      });
      cancel.addEventListener("click", closeM);
    });
  }

  // ── 阶段4+：自定义快捷键 & 会话恢复 & 搜索范围 ───────────
  function keyAction(id) {
    var A = {
      "save": function () { doSave(); },
      "bold": function () { wrapSelection("**", "**"); },
      "italic": function () { wrapSelection("*", "*"); },
      "strikethrough": function () { wrapSelection("~~", "~~"); },
      "inline_code": function () { wrapSelection("`", "`"); },
      "command_palette": function () { showCommandPalette(); },
      "quick_switch": function () { showQuickSwitcher(); },
      "focus": function () { toggleFocusMode(); },
      "daily": function () { openDailyNote(); },
      "flash": function () { openFlashNote(); },
      "preview": function () { togglePreview(); },
      "outline": function () { toggleOutline(); },
      "export_png": function () { exportNotePng(); },
      "encrypt": function () { showEncryptDialog(); }
    };
    return A[id] || null;
  }
  function applyKeymap(map) {
    if (!editor) return;
    map = map || {};
    var extra = {};
    Object.keys(map).forEach(function (combo) {
      var fn = keyAction(map[combo]);
      if (combo && fn) extra[combo] = fn;
    });
    if (Object.keys(extra).length) editor.addKeyMap(extra);
    state._appliedKeymap = extra;
  }
  var KEYMAP_ACTIONS = [
    { id: "save", label: "保存", def: "Ctrl-S" },
    { id: "bold", label: "加粗", def: "Ctrl-B" },
    { id: "italic", label: "斜体", def: "Ctrl-I" },
    { id: "strikethrough", label: "删除线", def: "" },
    { id: "inline_code", label: "行内代码", def: "" },
    { id: "command_palette", label: "命令面板", def: "Ctrl-K" },
    { id: "quick_switch", label: "快速切换", def: "Ctrl-P" },
    { id: "focus", label: "聚焦模式", def: "Ctrl-." },
    { id: "daily", label: "每日笔记", def: "Ctrl-D" },
    { id: "flash", label: "闪念笔记", def: "Ctrl-Shift-N" },
    { id: "preview", label: "切换预览", def: "" },
    { id: "outline", label: "切换大纲", def: "" },
    { id: "export_png", label: "导出PNG", def: "" },
    { id: "encrypt", label: "加解密笔记", def: "" }
  ];
  function showKeymapSettings() {
    var box = document.createElement("div"); box.className = "mdn-input-dialog";
    var note = document.createElement("div"); note.className = "mdn-hint";
    note.textContent = "设定 Ctrl/Alt/Shift 组合键；清空则用默认。留空不覆盖默认快捷键。";
    var list = document.createElement("div"); list.className = "modal-list keymap-list";
    KEYMAP_ACTIONS.forEach(function (a) {
      var row = document.createElement("label"); row.className = "cfg-row keymap-row";
      var sp = document.createElement("span"); sp.textContent = a.label;
      var inp = document.createElement("input"); inp.className = "input"; inp.type = "text";
      inp.value = state.keymap[a.id] || a.def; inp.dataset.id = a.id; inp.placeholder = "默认";
      row.appendChild(sp); row.appendChild(inp); list.appendChild(row);
    });
    var err = document.createElement("div"); err.className = "mdn-err";
    var actions = document.createElement("div"); actions.className = "mdn-actions";
    var cancel = document.createElement("button"); cancel.className = "btn"; cancel.textContent = "取消";
    var ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "保存";
    actions.appendChild(cancel); actions.appendChild(ok);
    box.appendChild(note); box.appendChild(list); box.appendChild(err); box.appendChild(actions);
    var closeM = openModal("自定义快捷键", box, { wide: true });
    ok.addEventListener("click", function () {
      var keymap = {};
      list.querySelectorAll("input").forEach(function (inp) {
        var v = inp.value.trim();
        if (v) keymap[inp.dataset.id] = v;
      });
      var patch = { editor: { keymap: keymap } };
      call("update_config", patch).then(function (r) {
        if (r && r.success) {
          state.keymap = keymap;
          // 撤销旧 keymap，再应用新的
          editor.removeKeyMap(state._appliedKeymap || {});
          var extra = {};
          Object.keys(keymap).forEach(function (c) { var fn = keyAction(keymap[c]); if (c && fn) extra[c] = fn; });
          editor.addKeyMap(extra); state._appliedKeymap = extra;
          closeM(); toast("快捷键已更新", "ok");
        } else { err.textContent = (r && r.error) || "保存失败"; }
      });
    });
    cancel.addEventListener("click", closeM);
  }
  function restoreSessionOnStartup() {
    if (!state.restoreSession) return;
    call("list_recent_files", 1).then(function (list) {
      if (list && list.length) openNote(list[0]);
    });
  }
  function populateSearchScope() {
    var sel = el("search-scope");
    if (!sel) return;
    call("list_note_names").then(function (names) {
      var dirs = {};
      (names || []).forEach(function (n) {
        var i = n.lastIndexOf("/");
        if (i > 0) dirs[n.slice(0, i)] = true;
      });
      Object.keys(dirs).sort().forEach(function (d) {
        var o = document.createElement("option"); o.value = d; o.textContent = "📁 " + d; sel.appendChild(o);
      });
      sel.addEventListener("change", doSearch);
    });
  }

  // ── 阶段4：历史版本 ─────────────────────────────────────
  function showHistory() {
    if (!state.currentRel) { messageBox({ title: "提示", message: "请先打开一篇笔记" }); return; }
    call("list_history", state.currentRel).then(function (list) {
      if (!list || !list.length) { messageBox({ title: "历史版本", message: "暂无历史版本" }); return; }
      var rows = list.map(function (h) {
        var t = h.timestamp || "";
        var bt = '<button class="mini restore-h" data-ts="' + esc(t) + '" data-rp="' +
          esc(state.currentRel) + '">恢复</button>';
        var vt = '<button class="mini view-h" data-ts="' + esc(t) + '" data-rp="' +
          esc(state.currentRel) + '">查看</button>';
        var dt = '<button class="mini diff-h" data-ts="' + esc(t) + '" data-rp="' +
          esc(state.currentRel) + '">与当前对比</button>';
        return '<li class="modal-item"><span>🕑 ' + esc(h.time || h.timestamp) +
          ' <span class="tip">(' + (h.timestamp || "").slice(0, 7) + ')</span></span>' +
          '<span class="row-btns">' + vt + dt + bt + '</span></li>';
      }).join("");
      var wrap = document.createElement("div");
      var listBox = document.createElement("ul");
      listBox.className = "modal-list";
      listBox.innerHTML = rows;
      wrap.appendChild(listBox);
      var closeM = openModal("历史版本 · " + state.currentRel.split("/").pop(), wrap, { wide: true });
      Array.prototype.forEach.call(document.querySelectorAll(".restore-h"), function (b) {
        b.addEventListener("click", function () {
          confirmBox({ title: "恢复历史版本", message: "确定恢复到该版本？当前内容会先自动存档。", onOk: function () {
            call("restore_history", b.dataset.rp, b.dataset.ts).then(function (r) {
              if (r && r.success) { closeM(); openNote(b.dataset.rp); toast("已恢复历史版本", "ok"); }
              else messageBox({ title: "恢复失败", message: "恢复失败：" + ((r && r.error) || "未知") });
            });
          } });
        });
      });
      Array.prototype.forEach.call(document.querySelectorAll(".view-h"), function (b) {
        b.addEventListener("click", function () {
          call("read_history", b.dataset.rp, b.dataset.ts).then(function (r) {
            if (r && r.success) openModal("查看历史 · " + b.dataset.ts,
              "<pre class='history-view'>" + esc(r.content) + "</pre>");
            else messageBox({ title: "读取失败", message: "读取失败：" + ((r && r.error) || "未知") });
          });
        });
      });
      Array.prototype.forEach.call(document.querySelectorAll(".diff-h"), function (b) {
        b.addEventListener("click", function () { renderVersionDiff(closeM, b.dataset.rp, b.dataset.ts); });
      });
    });
  }
  // 版本 diff：旧版本 -> 当前内容
  function renderVersionDiff(closeCurrent, relPath, tsA) {
    call("diff_versions", relPath, tsA, "").then(function (r) {
      if (!r || !r.success) { messageBox({ title: "对比失败", message: (r && r.error) || "对比失败" }); return; }
      var wrap = document.createElement("div");
      var head = document.createElement("div");
      head.className = "diff-summary";
      head.textContent = r.identical ? "两个版本内容完全一致" :
        ("+ " + r.added + " 行新增 · - " + r.removed + " 行删除（旧 → 当前）");
      wrap.appendChild(head);
      var pre = document.createElement("pre");
      pre.className = "diff-view";
      (r.diff || "").split("\n").forEach(function (line) {
        var span = document.createElement("span");
        if (line.indexOf("+") === 0) span.className = "diff-add";
        else if (line.indexOf("-") === 0) span.className = "diff-del";
        else if (line.indexOf("@@") === 0) span.className = "diff-hunk";
        else if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) span.className = "diff-file";
        else span.className = "diff-ctx";
        span.textContent = line || " ";
        pre.appendChild(span);
        pre.appendChild(document.createTextNode("\n"));
      });
      wrap.appendChild(pre);
      if (closeCurrent) closeCurrent();
      openModal("版本对比", wrap, { wide: true });
    });
  }

  // ── 阶段4：同步（WebDAV） ───────────────────────────────
  function showSync() {
    call("webdav_sync_status").then(function (w) {
      w = w || {};
      var html =
        '<div class="sync-sec"><h3>🌐 WebDAV 同步</h3>' +
        '<label class="cfg-row"><span>启用</span><input type="checkbox" id="wd-en" ' + (w.enabled ? "checked" : "") + '></label>' +
        '<label class="cfg-row"><span>地址</span><input type="text" id="wd-url" class="input" placeholder="https://dav.example.com" value="' + esc(w.url || "") + '"></label>' +
        '<label class="cfg-row"><span>用户名</span><input type="text" id="wd-user" class="input" value="' + esc(w.username || "") + '"></label>' +
        '<label class="cfg-row"><span>密码</span><input type="password" id="wd-pass" class="input" placeholder="留空=不修改"></label>' +
        '<label class="cfg-row"><span>远程目录</span><input type="text" id="wd-dir" class="input" value="' + esc(w.remote_dir || "/mdnotes") + '"></label>' +
        '<label class="cfg-row checkbox"><span>定时自动备份</span><input type="checkbox" id="wd-auto"' + (w.auto_sync ? " checked" : "") + '></label>' +
        '<label class="cfg-row"><span>备份间隔（分钟）</span><input type="number" id="wd-interval" class="input" min="5" value="' + esc(String(w.auto_sync_interval_min || 30)) + '"></label>' +
        '<div class="row-btns"><button class="btn" id="wd-save">保存配置</button>' +
        '<button class="btn" id="wd-sync">立即同步</button>' +
        '<button class="btn" id="wd-restore">恢复…</button></div>' +
        '<div class="tip" id="wd-msg"></div></div>';
      openModal("WebDAV 同步", html);
      el("wd-save").addEventListener("click", function () {
        var patch = { webdav: { enabled: el("wd-en").checked, url: el("wd-url").value,
          username: el("wd-user").value, remote_dir: el("wd-dir").value,
          auto_sync: el("wd-auto").checked,
          auto_sync_interval_min: parseInt(el("wd-interval").value, 10) || 30 } };
        var pw = el("wd-pass").value;
        if (pw) patch.webdav.password = pw;
        call("update_config", patch).then(function (r) { el("wd-msg").textContent = r && r.success ? "已保存" : "失败：" + ((r && r.error) || ""); });
      });
      el("wd-sync").addEventListener("click", function () {
        el("wd-msg").textContent = "同步中…";
        call("webdav_sync_now").then(function (r) { el("wd-msg").textContent = (r && r.success) ? ("成功，合并 " + (r.merged || 0) + " 处") : ("失败：" + ((r && r.error) || "")); });
      });
      el("wd-restore").addEventListener("click", webdavRestore);
    });
  }

  // WebDAV 恢复：预检差异 → 逐文件选择「保留本地/使用远程/合并/删除」→ 执行
  function webdavRestore() {
    confirmBox({
      title: "WebDAV 恢复",
      message: "将从 WebDAV 拉取远程内容覆盖本地同名文件。先预检差异、逐文件选择处理方式，覆盖前不会写入。",
      okText: "开始预检",
      onOk: function () {
        call("webdav_preview_restore").then(function (r) {
          if (!r || !r.success) { messageBox({ title: "预检失败", message: (r && r.error) || "预检失败" }); return; }
          var conflicts = r.conflicts || [];
          if (!conflicts.length) { messageBox({ title: "WebDAV 恢复", message: "本地与远程没有差异，无需恢复。" }); return; }
          showRestoreDiff(conflicts);
        });
        return true;
      }
    });
  }

  // 渲染差异选择列表；conflicts 结构见 webapi.webdav_preview_restore
  function showRestoreDiff(conflicts) {
    var wrap = document.createElement("div");
    var statusText = { added: "远程新增", modified: "已修改", deleted: "本地独有" };
    var list = document.createElement("div");
    list.className = "modal-list restore-list";
    conflicts.forEach(function (c) {
      var row = document.createElement("li");
      row.className = "modal-item restore-item";
      var info = document.createElement("span");
      info.className = "restore-info";
      info.textContent = "[" + (statusText[c.status] || c.status) + "] " + c.path;
      var sel = document.createElement("select");
      sel.className = "input restore-sel";
      if (c.status === "added") {
        sel.options.add(new Option("下载（覆盖/新建本地）", "remote"));
        sel.options.add(new Option("忽略", "local"));
      } else if (c.status === "deleted") {
        sel.options.add(new Option("保留本地", "local"));
        sel.options.add(new Option("删除本地", "delete"));
      } else { // modified
        sel.options.add(new Option("使用远程（覆盖本地）", "remote"));
        sel.options.add(new Option("保留本地", "local"));
        if (c.mergeable) sel.options.add(new Option("合并（插入冲突标记）", "merge"));
      }
      sel.dataset.path = c.path;
      row.appendChild(info); row.appendChild(sel);
      list.appendChild(row);
    });
    var hint = document.createElement("div");
    hint.className = "mdn-hint";
    hint.textContent = "共 " + conflicts.length + " 处差异。合并仅对文本文件可用，冲突处会插入 <<<<<<< / >>>>>>> 标记。";
    var actions = document.createElement("div");
    actions.className = "mdn-actions";
    var cancel = document.createElement("button");
    cancel.className = "btn"; cancel.textContent = "取消";
    var ok = document.createElement("button");
    ok.className = "btn primary"; ok.textContent = "确认执行恢复";
    actions.appendChild(cancel); actions.appendChild(ok);
    wrap.appendChild(list); wrap.appendChild(hint); wrap.appendChild(actions);
    var closeM = openModal("WebDAV 恢复差异选择", wrap, { wide: true });
    cancel.addEventListener("click", closeM);
    ok.addEventListener("click", function () {
      var resolutions = {};
      list.querySelectorAll("select.restore-sel").forEach(function (s) { resolutions[s.dataset.path] = s.value; });
      ok.disabled = true; ok.textContent = "执行中…";
      call("webdav_resolve_restore", resolutions).then(function (r) {
        closeM();
        if (!r || !r.success) { messageBox({ title: "恢复失败", message: (r && r.error) || "恢复失败" }); return; }
        toast("恢复完成：下载 " + (r.downloaded || 0) + "，保留 " + (r.kept || 0) +
          "，合并 " + (r.merged || 0) + "，删除 " + (r.deleted || 0) +
          (r.failed ? "，失败 " + r.failed : ""), r.failed ? "warn" : "ok");
        if (state.currentRel) { refresh(); loadTree(); }
      });
    });
  }

  // ── 阶段4：设置 ──────────────────────────────────────────
  function showSettings() {
    call("get_config").then(function (cfg) {
      cfg = cfg || {};
      var img = cfg.image || {};
      var ed = cfg.editor || {};
      var html =
        '<div class="sync-sec"><h3>⚙ 常规</h3>' +
        '<label class="cfg-row"><span>笔记目录</span><div class="input-with-btn"><input type="text" id="set-notesdir" class="input" value="' + esc(cfg.notes_dir || "") + '"><button class="btn" id="set-browse-notesdir">浏览…</button></div></label>' +
        '<label class="cfg-row"><span>字体大小</span><input type="number" id="set-font" class="input" value="' + esc(String(cfg.font_size || 16)) + '"></label>' +
        '<label class="cfg-row"><span>主题</span><select id="set-theme" class="input">' +
        listThemes().map(function (t) { return '<option value="' + t.id + '"' + ((cfg.theme || "genshin") === t.id ? " selected" : "") + '>' + escapeHtml(t.name) + '</option>'; }).join("") +
        '</select></label>' +
        '<label class="cfg-row checkbox"><span>自动保存</span><input type="checkbox" id="set-autosave"' + (ed.auto_save !== false ? " checked" : "") + '></label>' +
        '<label class="cfg-row checkbox"><span>打字机滚动</span><input type="checkbox" id="set-typewriter"' + (ed.typewriter_scroll !== false ? " checked" : "") + '></label>' +
        '<label class="cfg-row checkbox"><span>滚动联动</span><input type="checkbox" id="set-scrolllinked"' + (ed.scroll_linked !== false ? " checked" : "") + '></label>' +
        '<label class="cfg-row checkbox"><span>启动恢复上次会话</span><input type="checkbox" id="set-restore"' + (ed.restore_session !== false ? " checked" : "") + '></label>' +
        '<div class="cfg-row"><span>自定义快捷键</span><button class="btn" id="set-keymap">配置…</button></div>' +
        '</div><div class="sync-sec"><h3>🖼 图片压缩</h3>' +
        '<label class="cfg-row"><span>最大宽度</span><input type="number" id="set-imgw" class="input" value="' + esc(String(img.max_width || 1600)) + '"></label>' +
        '<label class="cfg-row"><span>质量</span><input type="number" id="set-imgq" class="input" value="' + esc(String(img.quality || 85)) + '"></label>' +
        '</div><div class="row-btns"><button class="btn" id="set-save">保存</button></div>' +
        '<div class="tip" id="set-msg"></div>';
      openModal("设置", html);
      function bindDirBrowse(btnId, inputId) {
        el(btnId).addEventListener("click", function () {
          pickFolder(el(inputId).value).then(function (path) {
            if (path) el(inputId).value = path;
          });
        });
      }
      bindDirBrowse("set-browse-notesdir", "set-notesdir");
      el("set-save").addEventListener("click", function () {
        var themeSel = el("set-theme").value;
        var patch = {
          notes_dir: el("set-notesdir").value,
          font_size: parseInt(el("set-font").value, 10) || 16,
          theme: themeSel,
          image: { max_width: parseInt(el("set-imgw").value, 10) || 1600,
                   quality: parseInt(el("set-imgq").value, 10) || 85 },
          editor: { typewriter_scroll: el("set-typewriter").checked,
                    scroll_linked: el("set-scrolllinked").checked,
                    auto_save: el("set-autosave").checked,
                    restore_session: el("set-restore").checked },
        };
        // 主题并入 update_config 一次写入，避免与独立 set_theme 并发写 config 互相覆盖
        call("update_config", patch).then(function (r) {
          el("set-msg").textContent = r && r.success ? "已保存（笔记目录需重启生效）" : ("失败：" + ((r && r.error) || ""));
          if (r && r.success) {
            state.typewriterScroll = patch.editor.typewriter_scroll;
            state.scrollLinked = patch.editor.scroll_linked;
            state.autoSave = patch.editor.auto_save;
            state.restoreSession = patch.editor.restore_session;
            applyTheme(themeSel);
          }
        });
      });
      el("set-keymap").addEventListener("click", showKeymapSettings);
    });
  }

  // ── 阶段4：导出 / 导入 ZIP ───────────────────────────────
  function exportZip() {
    var filename = "mdnotes_export_" + new Date().toISOString().slice(0, 10) + ".zip";
    pickSaveFile(filename).then(function (path) {
      if (!path) return;
      toast("正在导出…", "ok");
      call("export_zip_to", path).then(function (r) {
        if (!r || !r.success) { messageBox({ title: "导出失败", message: "导出失败：" + ((r && r.error) || "") }); return; }
        toast("已导出到：" + path, "ok");
      });
    });
  }
  function exportSite() {
    var defaultName = "mdnotes_site";
    pickFolder(defaultName).then(function (path) {
      if (!path) return;
      toast("正在生成静态站点…", "ok");
      call("export_site", path).then(function (r) {
        if (!r || !r.success) { messageBox({ title: "导出失败", message: "导出失败：" + ((r && r.error) || "") }); return; }
        var msg = "已生成静态站点（" + (r.files || 0) + " 篇笔记）\n" + r.path +
          "\n\n可双击 index.html 直接浏览，或推送到 GitHub Pages 分享。";
        confirmBox({
          title: "导出完成",
          message: msg,
          okText: "打开文件夹",
          onOk: function () { call("open_folder", r.path); },
        });
      });
    });
  }
  function exportPdf() {
    if (!state.currentRel) { messageBox({ title: "提示", message: "请先打开一篇笔记再导出 PDF" }); return; }
    // 触发浏览器/WebView2 打印对话框，用户可选「另存为 PDF」
    window.print();
  }
  function onImportFile(e) {
    var f = e.target.files && e.target.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      var b64 = reader.result.split(",")[1];
      confirmBox({ title: "导入 ZIP", message: "确认导入 ZIP？同名文件将被覆盖。", onOk: function () {
        call("import_zip", b64).then(function (r) {
          if (r && r.success) { loadTree(); toast("导入成功，共 " + (r.copied || 0) + " 个文件", "ok"); }
          else messageBox({ title: "导入失败", message: "导入失败：" + ((r && r.error) || "") });
        });
      } });
      el("import-file").value = "";
    };
    reader.readAsDataURL(f);
  }

  // ── P2-2：文件夹 / 语雀 / Notion 导入 ──────────────────
  function importFrom(kind) {
    var labelMap = { folder: "文件夹（含子目录与图片）", yuque: "语雀导出的知识库（文件夹或单个 .md）", notion: "Notion 导出的知识库文件夹" };
    pickFolder(state.notesDir || "").then(function (path) {
      if (!path) return;
      var apiMap = { folder: "import_from_folder", yuque: "import_yuque", notion: "import_notion" };
      toast("正在导入 " + (labelMap[kind] || "") + " …", "ok");
      var args = (kind === "folder") ? [path, ""] : [path];
      call(apiMap[kind], args[0], args[1]).then(function (r) {
        if (!r || !r.success) {
          messageBox({ title: "导入失败", message: "导入失败：" + ((r && r.error) || "") });
          return;
        }
        loadTree();
        var msg = "已导入 " + (r.copied || 0) + " 篇笔记";
        if (r.images) msg += "、" + r.images + " 张图片";
        if (r.mode === "split") msg += "（按标题拆分为多篇）";
        toast(msg, "ok");
      });
    });
  }

  // ── P3：模板笔记 ─────────────────────────────────────
  function loadTemplates() {
    call("list_templates").then(function (list) {
      list = list || [];
      var box = el("template-list");
      if (!list.length) {
        box.innerHTML = '<div class="modal-empty">暂无模板。点击「管理」新建一个吧。</div>';
        return;
      }
      box.innerHTML = list.map(function (t) {
        return '<div class="tpl-item"><span class="tpl-name">' + escapeHtml(t.name) +
          '</span><span class="row-btns">' +
          '<button class="mini tpl-new" data-n="' + esc(t.name) + '">新建</button>' +
          '<button class="mini tpl-edit" data-n="' + esc(t.name) + '">编辑</button>' +
          '<button class="mini tpl-del" data-n="' + esc(t.name) + '">删除</button>' +
          '</span></div>';
      }).join("");
      Array.prototype.forEach.call(box.querySelectorAll(".tpl-new"), function (b) {
        b.addEventListener("click", function () { createFromTemplate(b.dataset.n); });
      });
      Array.prototype.forEach.call(box.querySelectorAll(".tpl-edit"), function (b) {
        b.addEventListener("click", function () { editTemplate(b.dataset.n); });
      });
      Array.prototype.forEach.call(box.querySelectorAll(".tpl-del"), function (b) {
        b.addEventListener("click", function () {
          confirmBox({ title: "删除模板", message: "删除模板「" + b.dataset.n + "」？", onOk: function () {
            call("delete_template", b.dataset.n).then(function () { loadTemplates(); });
          } });
        });
      });
    });
  }
  function createFromTemplate(name) {
    inputDialog({ title: "从模板新建", message: "新建笔记名称：", placeholder: "笔记名（含子目录用 / 分隔）",
      okText: "创建", onOk: function (val) {
        var parts = (val || name).split("/");
        var dest = parts.pop();
        var parent = parts.join("/");
        call("create_from_template", name, dest, parent).then(function (r) {
          if (r && r.success) { loadTree(); openNote(r.rel_path); toast("已创建：" + r.rel_path, "ok"); }
          else messageBox({ title: "失败", message: "创建失败：" + ((r && r.error) || "") });
        });
      } });
  }
  function editTemplate(name) {
    call("list_templates").then(function (list) {
      var t = (list || []).find(function (x) { return x.name === name; });
      var content = t ? t.content : "";
      var box = document.createElement("div");
      box.innerHTML = '<textarea id="tpl-content" class="input tpl-area" placeholder="支持变量：{{date}} {{time}} {{datetime}} {{year}} {{month}} {{day}}">' +
        escapeHtml(content) + '</textarea><div class="tip">变量会在新建时自动替换为当前日期/时间</div>';
      var closeM = openModal("编辑模板 · " + name, box, { wide: true });
      setTimeout(function () { el("tpl-content").focus(); }, 30);
      // 在模态底部加保存按钮
      var bar = document.createElement("div");
      bar.className = "row-btns";
      bar.innerHTML = '<button class="btn" id="tpl-save">保存模板</button>';
      el("modal-body").appendChild(bar);
      el("tpl-save").addEventListener("click", function () {
        call("save_template", name, el("tpl-content").value).then(function (r) {
          if (r && r.success) { closeM(); toast("模板已保存", "ok"); loadTemplates(); }
          else messageBox({ title: "失败", message: "保存失败：" + ((r && r.error) || "") });
        });
      });
    });
  }
  function showNewFromTemplate() {
    call("list_templates").then(function (list) {
      list = list || [];
      if (!list.length) { messageBox({ title: "无模板", message: "尚未创建任何模板，点击「管理」新建一个。" }); return; }
      var rows = list.map(function (t, i) {
        return '<li class="modal-item"><span>📋 ' + escapeHtml(t.name) + '</span>' +
          '<span class="row-btns"><button class="mini tp-pick" data-n="' + esc(t.name) + '">用此新建</button></span></li>';
      }).join("");
      var closeM = openModal("从模板新建笔记", "<ul class='modal-list'>" + rows + "</ul>");
      Array.prototype.forEach.call(document.querySelectorAll(".tp-pick"), function (b) {
        b.addEventListener("click", function () { closeM(); createFromTemplate(b.dataset.n); });
      });
    });
  }
  function showManageTemplates() {
    call("list_templates").then(function (list) {
      list = list || [];
      var rows = list.length
        ? list.map(function (t) {
            return '<li class="modal-item"><span>📋 ' + escapeHtml(t.name) + '</span>' +
              '<span class="row-btns"><button class="mini tm-edit" data-n="' + esc(t.name) + '">编辑</button>' +
              '<button class="mini tm-del" data-n="' + esc(t.name) + '">删除</button></span></li>';
          }).join("")
        : '<li class="modal-item tip">暂无模板</li>';
      var box = document.createElement("div");
      box.innerHTML = '<div class="row-btns"><button class="btn" id="tm-new">＋ 新建模板</button></div>' +
        '<ul class="modal-list">' + rows + '</ul>';
      openModal("模板管理", box);
      el("tm-new").addEventListener("click", function () {
        inputDialog({ title: "新建模板", message: "模板名称：", placeholder: "如：会议纪要 / 周报",
          okText: "创建", onOk: function (val) {
            if (!val) return;
            call("save_template", val, "# " + val + "\n\n").then(function (r) {
              if (r && r.success) { showManageTemplates(); editTemplate(val); }
              else messageBox({ title: "失败", message: "创建失败：" + ((r && r.error) || "") });
            });
          } });
      });
      Array.prototype.forEach.call(document.querySelectorAll(".tm-edit"), function (b) {
        b.addEventListener("click", function () { editTemplate(b.dataset.n); });
      });
      Array.prototype.forEach.call(document.querySelectorAll(".tm-del"), function (b) {
        b.addEventListener("click", function () {
          confirmBox({ title: "删除模板", message: "删除模板「" + b.dataset.n + "」？", onOk: function () {
            call("delete_template", b.dataset.n).then(function () { showManageTemplates(); });
          } });
        });
      });
    });
  }

  // ── P3：字数热力图 ───────────────────────────────────
  function loadHeatmap() {
    var days = state.heatDays || 120;
    call("get_wordcount_stats", days).then(function (r) {
      if (!r || !r.success) { el("heat-stat").textContent = "统计失败"; return; }
      el("heat-stat").textContent = "总计 " + (r.total || 0).toLocaleString() + " 字";
      var daily = r.daily || {};
      var max = 0;
      Object.keys(daily).forEach(function (k) { if (daily[k] > max) max = daily[k]; });
      // 按周排列：每行 7 天（周日~周六）
      var dates = Object.keys(daily).sort();
      var grid = el("heatmap-grid");
      grid.innerHTML = "";
      grid.className = "heatmap-grid";
      // 补齐到整周（前面补空白）
      var first = dates.length ? new Date(dates[0] + "T00:00:00") : new Date();
      var lead = first.getDay();
      for (var i = 0; i < lead; i++) {
        var sp = document.createElement("div"); sp.className = "heat-cell empty"; grid.appendChild(sp);
      }
      dates.forEach(function (d) {
        var v = daily[d];
        var lvl = max > 0 ? Math.ceil((v / max) * 4) : 0;
        if (v === 0) lvl = 0;
        var cell = document.createElement("div");
        cell.className = "heat-cell lvl-" + lvl;
        cell.title = d + "：" + v.toLocaleString() + " 字";
        cell.addEventListener("click", function () {
          el("heat-detail").textContent = d + " 书写 " + v.toLocaleString() + " 字" + (v === 0 ? "（无记录）" : "");
        });
        grid.appendChild(cell);
      });
      // Top 笔记
      var top = (r.top_notes || []).slice(0, 8).map(function (n) {
        return '<li class="modal-item"><span>📄 ' + escapeHtml(n.rel_path.split("/").pop()) +
          '</span><span class="tip">' + (n.chars || 0).toLocaleString() + ' 字</span></li>';
      }).join("");
      var detail = el("heat-detail");
      detail.innerHTML = (top ? '<div class="heat-top-title">📌 字数最多</div><ul class="modal-list heat-top">' + top + '</ul>' : '');
    });
  }

  // ── P3：主题切换 ─────────────────────────────────────
  // 当前主题是暗色还是亮色（用于 CodeMirror / Mermaid 跟随）
  function isDarkTheme() {
    var t = state.theme || "genshin";
    return t === "genshin" || t === "midnight" || t === "forest";
  }
  function applyTheme(themeId) {
    themeId = themeId || "genshin";
    state.theme = themeId;
    document.body.setAttribute("data-theme", themeId);
    // 同步 CodeMirror 主题
    if (editor) {
      var cmTheme = isDarkTheme() ? "material-darker" : "";
      editor.setOption("theme", cmTheme);
    }
    // 预览内容（Mermaid 等）跟随主题：清空缓存并强制重渲染
    try {
      _lastPreviewSrc = null;
      if (state.currentRel || (el("preview") && el("preview").innerHTML.length > 0)) {
        schedulePreview(true);
      }
    } catch (e) { /* 忽略 */ }
  }
  function listThemes() {
    return [
      { id: "genshin", name: "原神（默认暗色）" },
      { id: "light", name: "晨曦（亮色）" },
      { id: "sakura", name: "樱粉（亮色）" },
      { id: "midnight", name: "深蓝（暗色）" },
      { id: "forest", name: "森野（暗色）" },
    ];
  }

  // ── P3：图片 OCR ─────────────────────────────────────
  function ocrCurrentImage(relPath) {
    if (!relPath) { messageBox({ title: "提示", message: "请先选择一张笔记图片" }); return; }
    toast("正在识别图片文字…", "ok");
    call("ocr_image", relPath).then(function (r) {
      if (!r || !r.success) {
        if (r && r.available === false) {
          messageBox({ title: "OCR 不可用", message: r.error || "未安装 Tesseract OCR" });
        } else {
          messageBox({ title: "识别失败", message: "识别失败：" + ((r && r.error) || "") });
        }
        return;
      }
      var text = r.text || "";
      // 把识别结果插入当前笔记末尾或光标处
      if (state.currentRel) {
        var insert = "\n\n> 🔍 OCR 识别结果：\n\n" + text + "\n";
        if (editor) {
          editor.replaceSelection(insert);
          schedulePreview();
        }
        toast("已插入 OCR 结果", "ok");
      } else {
        openModal("OCR 识别结果", "<pre class='history-view'>" + escapeHtml(text) + "</pre>");
      }
    });
  }

  // ── 阶段4：启动时草稿恢复检测 ────────────────────────────
  function checkDraftsOnStartup() {
    call("list_drafts").then(function (list) {
      if (!list || !list.length) return;
      var html = list.map(function (d) {
        return '<li class="modal-item"><span>📝 ' + esc(d.rel_path.split("/").pop()) +
          ' <span class="tip">(' + (d.size || 0) + ' 字符)</span></span>' +
          '<span class="row-btns"><button class="mini rd" data-rp="' + esc(d.rel_path) +
          '">恢复</button><button class="mini cl" data-rp="' + esc(d.rel_path) + '">丢弃</button></span></li>';
      }).join("");
      var closeDrafts = openModal("检测到未保存草稿 (" + list.length + ")", "<ul class='modal-list'>" + html + "</ul>");
      Array.prototype.forEach.call(document.querySelectorAll(".rd"), function (b) {
        b.addEventListener("click", function () {
          call("restore_draft", b.dataset.rp).then(function (r) {
            if (r && r.success) { openNote(b.dataset.rp); if (r.content != null) editor.setValue(r.content); closeDrafts(); toast("已恢复草稿", "ok"); }
            else messageBox({ title: "恢复失败", message: "恢复失败" });
          });
        });
      });
      Array.prototype.forEach.call(document.querySelectorAll(".cl"), function (b) {
        b.addEventListener("click", function () {
          call("clear_draft", b.dataset.rp).then(function () { b.closest(".modal-item").remove(); });
        });
      });
    });
  }

  // 托盘「闪念笔记」：主进程通过 IPC 通知，弹出闪念输入框
  if (typeof window.desktop !== "undefined" && window.desktop.onFlashNote) {
    window.desktop.onFlashNote(function () {
      if (document.body.classList.contains("app-locked")) return;
      openFlashNote();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootstrap);
  else bootstrap();
})();
