import html as _html
import html.parser
import markdown
import re
import threading
from markdown.extensions.codehilite import CodeHiliteExtension
from pygments.formatters.html import HtmlFormatter
from pymdownx.arithmatex import ArithmatexExtension

# ===========================================================
# 安全消毒：基于 html.parser 的白名单解析器
# 只保留 Markdown 渲染产物的安全标签与属性，其余一律剥离。
# 相比正则方案，可正确识别 HTML 实体编码/大小写混淆的危险 URL，
# 用于实时预览与静态站点导出（推公网也安全）。
# ===========================================================

# 连同内容一起剥离的危险标签（script 内部、style 内容等不可作为文本展示）
_DROP_CONTENT_TAGS = {
    "script", "style", "iframe", "object", "embed", "link", "meta", "form",
    "base", "frame", "frameset", "template", "noscript", "svg", "math",
    "audio", "video", "source", "track",
}

# 允许保留的标签（白名单）
_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "div", "dl",
    "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "input",
    "kbd", "label", "li", "mark", "ol", "p", "pre", "q", "s", "small", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
}

# 需要校验 URL 协议的属性
_URL_ATTRS = {"href", "src"}

# 任意元素都放行的安全属性（class 仅影响样式；id/data-* 用于 JS 钩子）
_GENERIC_ATTRS = {"class", "id", "title", "loading", "decoding"}

# 各标签额外的允许属性
_EXTRA_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "loading", "decoding"},
    "input": {"type", "checked", "disabled"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

_VOID_TAGS = {"br", "hr", "img", "input"}


class _Sanitizer(html.parser.HTMLParser):
    """白名单消毒器：剥离危险标签/属性，保留文本与安全结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._drop_depth = 0  # 处于危险标签内部时 >0，其内容整体丢弃

    def _filter_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        allowed: list[tuple[str, str]] = []
        for key, value in attrs:
            k = (key or "").lower()
            if k.startswith("on"):  # 事件处理器
                continue
            if k == "style":  # CSS 注入面
                continue
            if k in _URL_ATTRS and tag in _EXTRA_ATTRS and k in _EXTRA_ATTRS[tag]:
                # URL 属性：先解码实体再判断协议，防止 &#106;avascript: 绕过
                if _dangerous_url(value or ""):
                    continue
            # 白名单属性判定
            ok_attr = k in _GENERIC_ATTRS or k.startswith("data-")
            if not ok_attr and tag in _EXTRA_ATTRS:
                ok_attr = k in _EXTRA_ATTRS[tag]
            if ok_attr:
                allowed.append((k, value or ""))
        return allowed

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if t not in _ALLOWED_TAGS:
            return  # 未知标签：剥离标签本身，内容保留为文本
        cleaned = self._filter_attrs(t, attrs)
        attr_html = "".join(f' {k}="{_html.escape(v, quote=True)}"' for k, v in cleaned)
        self._out.append(f"<{t}{attr_html}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS or t not in _ALLOWED_TAGS or t not in _VOID_TAGS:
            return
        cleaned = self._filter_attrs(t, attrs)
        attr_html = "".join(f' {k}="{_html.escape(v, quote=True)}"' for k, v in cleaned)
        self._out.append(f"<{t}{attr_html}/>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if t in _ALLOWED_TAGS:
            self._out.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._drop_depth == 0:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_depth == 0:
            self._out.append(f"&#{name};")

    def get_result(self) -> str:
        return "".join(self._out)


def _sanitize(html_text: str) -> str:
    parser = _Sanitizer()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 解析异常时保守降级为纯文本
        return _html.escape(html_text or "")
    return parser.get_result()


def _dangerous_url(value: str) -> bool:
    decoded = _html.unescape(value).strip().lower()
    # 允许 data:image/... 的图片 src，其余 data: 一律视为危险
    if decoded.startswith("data:image/"):
        return False
    return decoded.startswith(("javascript:", "vbscript:", "data:", "file:"))


_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_IMG_TAG = re.compile(r"<img\b([^>]*?)(/?>)", re.IGNORECASE)


def _add_lazy_loading(html: str) -> str:
    """为图片添加懒加载属性，减少大文档渲染时的瞬时资源占用。"""

    def _patch(match: re.Match) -> str:
        attrs = match.group(1)
        closing = match.group(2)
        if 'loading=' not in attrs.lower():
            attrs += ' loading="lazy"'
        if 'decoding=' not in attrs.lower():
            attrs += ' decoding="async"'
        return f"<img{attrs}{closing}"

    return _IMG_TAG.sub(_patch, html)


def _mermaid_format(source, language, css_class, options, md, **kwargs):
    """把 mermaid 代码块输出为前端可直接渲染的 div。"""
    return f'<div class="mermaid">{md.htmlStash.store(source)}</div>'


_EXTENSIONS = [
    "nl2br",
    "tables",
    "fenced_code",
    "toc",
    CodeHiliteExtension(guess_lang=False, css_class="codehilite"),
    "pymdownx.highlight",
    "pymdownx.tasklist",
    ArithmatexExtension(generic=True),
]

_EXTENSION_CONFIGS = {
    "pymdownx.superfences": {
        "custom_fences": [
            {
                "name": "mermaid",
                "class": "mermaid",
                "format": _mermaid_format,
            }
        ]
    },
    "pymdownx.tasklist": {
        "custom_checkbox": True,
        "clickable_checkbox": True,
    },
}

# Markdown 实例：每次 render 都重建实例（含所有扩展初始化）开销很大，因此复用。
# 但实例带可变状态（reset/convert 交替），多线程并发复用会串扰，
# 故按线程隔离（thread-local），既避免锁串行化，也保证并发安全。
_THREAD_LOCAL = threading.local()


def _md_instance() -> "markdown.Markdown":
    inst = getattr(_THREAD_LOCAL, "md", None)
    if inst is None:
        inst = markdown.Markdown(
            extensions=_EXTENSIONS + ["pymdownx.superfences"],
            extension_configs=_EXTENSION_CONFIGS,
        )
        _THREAD_LOCAL.md = inst
    return inst


def render(text: str) -> str:
    """将 Markdown 文本渲染为 HTML，并做安全消毒。"""
    text = _highlight_equals(text)
    inst = _md_instance()
    inst.reset()
    html = inst.convert(text)
    # [[双链]] -> 可点击链接，由前端解析目标笔记（标题转义，防止属性逃逸）
    html = _WIKILINK.sub(
        lambda m: '<a class="wikilink" data-target="%s" href="#">%s</a>' % (
            _html.escape(m.group(1), quote=True),
            _html.escape(m.group(1), quote=True),
        ),
        html,
    )
    html = _inject_task_line_numbers(html, text)
    html = _transform_callouts(html)
    html = _add_lazy_loading(html)
    return _sanitize(html)


def _highlight_equals(text: str) -> str:
    """把 ``==文字==`` 转成 ``<mark>文字</mark>``（删除线两侧），跳过围栏代码块。

    仅匹配同一行内的 `==...==`，不处理跨行、连续等号（如数学 `==`）。
    """
    _EQ_RE = re.compile(r"==([^=\n<].*?)==(?![=])")
    out_lines = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if not in_fence and not stripped.startswith("    "):
            line = _EQ_RE.sub(r"<mark>\1</mark>", line)
        out_lines.append(line)
    return "\n".join(out_lines)


# ── Callout 提示块（Obsidian 风格 `> [!info]`） ────────────
_CALLOUT_ICONS = {
    "info": "💡", "note": "🗒", "tip": "🎯", "hint": "🎯",
    "warning": "⚠", "warn": "⚠", "danger": "🔥", "error": "🔥",
    "success": "✅", "question": "❓", "faq": "❓",
    "quote": "💬", "abstract": "💬", "example": "✒",
}
_CALLOUT_ALIAS = {
    "note": "info", "hint": "tip", "warn": "warning", "error": "danger", "faq": "question",
    "abstract": "quote",
}
_CALLOUT_RE = re.compile(r"\[!(note|info|tip|hint|warning|warn|danger|error|success|question|faq|quote|abstract|example)\](.*)", re.IGNORECASE)


def _transform_callouts(html: str) -> str:
    """把 `> [!type] title` 形式的 blockquote 转成高亮 callout 容器。

    只处理顶级且首段即 `[!x]` 的引用块；内联普通引用不受影响。
    """
    # 匹配整个 blockquote（含嵌套需留到 .*? 与首段判断）
    def _replace(match: re.Match) -> str:
        whole = match.group(0)
        body = match.group(1)
        # 取首个 <p>...</p>
        mp = re.match(r"(?s)^\s*<p>(.*?)</p>", body)
        if not mp:
            return whole  # 无段落，原样保留
        first_p = mp.group(1)
        mh = _CALLOUT_RE.match(first_p)
        if not mh:
            return whole  # 非 callout
        type_ = mh.group(1).lower()
        canon = _CALLOUT_ALIAS.get(type_, type_)
        title = mh.group(2).split("<br")[0].strip()
        if not title:
            title = {"info": "信息", "note": "备注", "tip": "提示", "warning": "警告",
                     "danger": "危险", "success": "成功", "question": "提问", "quote": "引用",
                     "example": "示例"}.get(canon, "提示")
        rest = re.sub(r"^\s*<br\s*/?>\s*", "", first_p[mh.end():])
        inner = "<p>" + rest + "</p>" if rest.strip() else ""
        inner += body[mp.end():]
        title_html = "%s %s" % (_CALLOUT_ICONS.get(canon, "💬"), _html.escape(title, quote=True))
        return (
            '<div class="callout callout-%s"><div class="callout-title">%s</div>%s</div>'
            % (canon, title_html, inner)
        )

    return re.sub(r"(?s)<blockquote>(.*?)</blockquote>", _replace, html)


# 任务清单复选框需要能写回源码（勾选 [ ] <-> [x]）。
# markdown 渲染后丢失了源码行号，这里在渲染前扫描源码统计任务行顺序，
# 渲染后按顺序给每个 <input type="checkbox"> 注入对应的源码行号 data-line。
_TASK_LINE_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+")


def _inject_task_line_numbers(html: str, source: str) -> str:
    task_lines = []
    for idx, line in enumerate(source.split("\n")):
        if _TASK_LINE_RE.match(line):
            task_lines.append(idx)
    if not task_lines:
        return html
    count = [0]  # 闭包计数，保持与 task_lines 顺序一致

    def _patch(match: re.Match) -> str:
        i = count[0]
        count[0] += 1
        if i < len(task_lines):
            return f'<input type="checkbox" data-line="{task_lines[i]}"{match.group(1)}>'
        return match.group(0)

    return re.sub(r'<input type="checkbox"([^>]*)>', _patch, html)


def get_pygments_stylesheet(style: str = "monokai") -> str:
    """返回 Pygments 代码高亮的 CSS。"""
    formatter = HtmlFormatter(style=style)
    return formatter.get_style_defs(".codehilite")
