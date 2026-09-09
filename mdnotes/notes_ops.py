"""笔记相关的增删改查、搜索、回收站、模板、历史版本、导出、附件管理。"""

import asyncio
import io
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import git_history


def _is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def _meta_dirs() -> set[str]:
    return {".trash", ".history", ".templates", ".drafts", ".git", "__pycache__", ".venv", ".uv_cache"}


# Markdown 图片/链接目标提取，用于孤儿附件检测
_LINK_DEST_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

# 双链笔记链接提取，用于未关联笔记检测与反向链接
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

# 行内 #tag 匹配，支持层级标签如 #project/aaa
_TAG_RE = re.compile(r"#([\u4e00-\u9fa5\w\-/]+)")

# Windows 保留文件名与非法字符
_INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def is_valid_filename(name: str) -> bool:
    """校验文件名是否合法（Windows 规则）：非法字符、保留名、尾部空格/点。"""
    if not name or not name.strip():
        return False
    if _INVALID_FILENAME_CHARS_RE.search(name):
        return False
    stem = name.split(".")[0].strip().upper()
    if stem in WINDOWS_RESERVED_NAMES:
        return False
    if name.endswith((" ", ".")):
        return False
    return True


def _extract_referenced_paths(text: str) -> set[str]:
    """从 Markdown 文本中提取所有链接/图片的本地目标路径（已 URL 解码、去掉 API 前缀）。"""
    refs: set[str] = set()
    for m in _LINK_DEST_RE.finditer(text):
        dest = m.group(1)
        if not dest:
            continue
        dest = unquote(dest.split("?")[0].split("#")[0]).strip("/")
        if dest.startswith("api/files/"):
            dest = dest[len("api/files/"):]
        elif dest.startswith("/api/files/"):
            dest = dest[len("/api/files/"):]
        refs.add(dest)
    return refs


async def list_notes(notes_dir: Path, tag: str | None = None) -> list[dict[str, Any]]:
    files = []
    skip = _meta_dirs()
    if notes_dir.exists():
        for p in sorted(notes_dir.rglob("*.md")):
            rel = p.relative_to(notes_dir)
            if _is_hidden(rel) or skip.intersection(rel.parts):
                continue
            content = ""
            if tag:
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                tags = extract_tags(content)
                # 层级标签：点击 parent 同时匹配 parent 与 parent/child
                if not (tag in tags or any(t.startswith(tag + "/") for t in tags)):
                    continue
            stat = p.stat()
            files.append(
                {
                    "path": rel.as_posix(),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "tags": extract_tags(content) if tag else [],
                }
            )
    return files


def extract_tags(content: str) -> list[str]:
    """从 YAML frontmatter 或文本中 #tag 提取标签。"""
    tags: set[str] = set()
    # YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            fm = content[3:end].strip()
            lines = fm.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.lower().startswith("tags:"):
                    val = line.split(":", 1)[1].strip()
                    if val.startswith("["):
                        tags.update(t.strip().strip('"\'') for t in val.strip("[]").split(","))
                    elif val:
                        tags.update(t.strip().strip('"\'') for t in val.split(","))
                    else:
                        # 多行列表风格：tags: 下一行开始是缩进 - item
                        j = i + 1
                        while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
                            tags.add(lines[j].strip()[2:].strip().strip("'\""))
                            j += 1
                i += 1
    # 行内 #tag（支持 #parent/child 层级）
    for m in _TAG_RE.finditer(content):
        tag = m.group(1).rstrip("/")
        if tag:
            tags.add(tag)
    return sorted(t for t in tags if t)


def list_tags(notes_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    skip = _meta_dirs()
    if not notes_dir.exists():
        return counts
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        try:
            tags = extract_tags(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    return counts


def rename_tag(notes_dir: Path, old_tag: str, new_tag: str) -> int:
    """把笔记中的 #old_tag 统一重命名为 #new_tag（frontmatter 与行内 #tag）。

    返回受影响的笔记数。仅改写，不触碰其它内容。
    """
    inline_re = re.compile(r"(?<!#)#" + re.escape(old_tag) + r"(?=/|(?![-\w\u4e00-\u9fa5]))")
    count = 0
    skip = _meta_dirs()
    if not notes_dir.exists():
        return 0
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if old_tag not in content:
            continue
        changed = inline_re.sub("#" + re.escape(new_tag), content)
        changed = _rename_tag_frontmatter(changed, old_tag, new_tag)
        if changed != content:
            try:
                p.write_text(changed, encoding="utf-8")
                count += 1
            except Exception:  # noqa: BLE001
                pass
    return count


def _rename_tag_frontmatter(content: str, old_tag: str, new_tag: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("---", 3)
    if end == -1:
        return content
    head = content[:end]
    body = content[end:]

    def _rename_val(val: str) -> str:
        return re.sub(r"(?<![\w#])" + re.escape(old_tag) + r"(?![-\w])", new_tag, val)

    lines = head.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().lower().startswith("tags:"):
            val = ln.split(":", 1)[1]
            if val.strip():
                # 内联写法：tags: a, b 或 tags: [a, b]
                out.append(ln.split(":", 1)[0] + ":" + _rename_val(val))
            else:
                # 多行列表写法：tags: 后跟缩进 - item
                out.append(ln)
                j = i + 1
                while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
                    item = lines[j].split("-", 1)
                    out.append(item[0] + "-" + _rename_val(item[1]))
                    j += 1
                i = j
                continue
        else:
            out.append(ln)
        i += 1
    return "".join(out) + body


def set_tags(notes_dir: Path, rel_path: str, add_tags: list[str], remove_tags: list[str]) -> bool:
    """为单篇笔记添加/删除标签（写入 YAML frontmatter，无 frontmatter 则新建）。"""
    target = (notes_dir / rel_path).resolve()
    try:
        content = target.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    add_tags = [t for t in add_tags or [] if t]
    remove_tags = remove_tags or []
    for t in add_tags:
        content = re.sub(r"(?<!#)#" + re.escape(t) + r"(?=/|(?![-\w\u4e00-\u9fa5]))", "", content)
    for t in remove_tags:
        content = re.sub(r"(?<!#)#" + re.escape(t) + r"(?=/|(?![-\w\u4e00-\u9fa5]))", "", content)
    # frontmatter 更新
    has_fm = content.startswith("---") and content.find("---", 3) != -1
    if has_fm:
        end = content.find("---", 3)
        head, body = content[:end], content[end:]
        lines = head.splitlines(keepends=True)
        out, tags_line = [], None
        for ln in lines:
            if ln.strip().lower().startswith("tags:"):
                val = ln.split(":", 1)[1].strip().strip("[]")
                existing = [x.strip().strip("\"'") for x in val.split(",") if x.strip()]
                existing = [e for e in existing if e not in remove_tags]
                existing = list(dict.fromkeys(existing + add_tags))
                tags_line = "tags: " + ", ".join(existing) + "\n"
            else:
                out.append(ln)
        if tags_line is None:
            out.append("tags: " + ", ".join(add_tags) + "\n")
        else:
            out.append(tags_line)
        content = "".join(out) + body
    else:
        content = "---\ntags: " + ", ".join(add_tags) + "\n---\n\n" + content
    try:
        target.write_text(content, encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


# ── YAML frontmatter 解析 / 合并（笔记属性编辑） ─────────
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter 为 dict，并返回正文（去掉 frontmatter 的剩余部分）。

    支持简单标量（key: value）、数组（key: [a, b] 或缩进列表）与注释；
    无 frontmatter 或解析失败时返回 ({}, content)。注意：不做完整 YAML 解析，
    只覆盖常见笔记字段（tags / aliases / status / priority / due / type 等）。
    """
    if not content.startswith("---"):
        return {}, content
    end = content.find("---", 3)
    if end == -1:
        return {}, content
    block = content[3:end]
    body = content[end + 3:].lstrip("\n")
    data: dict[str, Any] = {}
    list_key: str | None = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if list_key and re.match(r"^\s*-\s+", line):
            data.setdefault(list_key, []).append(line.strip()[2:].strip().strip("'\""))
            continue
        list_key = None
        m = re.match(r"^([\w\-]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if not val:
            list_key = key
            data[key] = []
            continue
        if val.startswith("[") and val.endswith("]"):
            data[key] = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
        else:
            data[key] = val.strip("'\"")
    # 把 tags / aliases 等数组字段统一规范为 list（兼容单值标量写法，保证读写一致）
    for key in ("tags", "aliases"):
        val = data.get(key)
        if isinstance(val, str):
            data[key] = [x.strip().strip("'\"") for x in val.split(",") if x.strip()]
    return data, body


def merge_frontmatter(content: str, fields: dict[str, Any]) -> str:
    """把 fields 合并写入 frontmatter；无 frontmatter 时新建。返回新的完整内容。

    fields 中值为 None 的键会被删除；列表值按 YAML 列表书写。
    """
    data, body = parse_frontmatter(content)
    for k, v in fields.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, (list, tuple)):
            if len(v) == 0:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


# ── 任务清单扫描（任务视图） ──────────────────────────────
# 匹配 - [ ] / - [x] / * [X] 等任务清单项
_TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$")


def scan_tasks(notes_dir: Path, done_filter: str = "") -> list[dict[str, Any]]:
    """扫描全库任务清单项并聚合。

    done_filter: "" 全部 / "open" 未完成 / "done" 已完成。
    每项返回 {path, line, text, done, due, mtime}，line 为 1 起始行号；
    due 取自笔记 frontmatter 的 due 字段（可留空）。未完成排前。
    """
    skip = _meta_dirs()
    tasks: list[dict[str, Any]] = []
    if not notes_dir.exists():
        return tasks
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        data, _ = parse_frontmatter(content)
        due = str(data.get("due") or "").strip()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        for i, line in enumerate(content.splitlines()):
            m = _TASK_RE.match(line)
            if not m:
                continue
            done = m.group(1).lower() == "x"
            if done_filter == "open" and done:
                continue
            if done_filter == "done" and not done:
                continue
            tasks.append({
                "path": rel.as_posix(),
                "line": i + 1,
                "text": m.group(2).strip(),
                "done": done,
                "due": due,
                "mtime": mtime,
            })
    tasks.sort(key=lambda t: (t["done"], -(t["mtime"] or 0)))
    return tasks


def extract_aliases(content: str) -> list[str]:
    """从 YAML frontmatter 提取笔记别名（aliases）。"""
    aliases: list[str] = []


def list_aliases(notes_dir: Path) -> dict[str, str]:
    """返回别名到笔记相对路径的映射（别名 -> 笔记路径）。"""
    aliases: dict[str, str] = {}
    skip = _meta_dirs()
    if not notes_dir.exists():
        return aliases
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel_path = rel.as_posix()
        for alias in extract_aliases(content):
            if alias and alias not in aliases:
                aliases[alias] = rel_path
    return aliases


def find_unlinked_notes(notes_dir: Path) -> list[str]:
    """返回未被任何其他笔记通过 [[...]] 引用的笔记相对路径。"""
    skip = _meta_dirs()
    all_notes: dict[str, Path] = {}
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        all_notes[rel.as_posix()] = p

    if not all_notes:
        return []

    # 同时加载别名，避免别名被误当作未关联
    aliases = list_aliases(notes_dir)

    referenced: set[str] = set()
    for rel, p in all_notes.items():
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _WIKI_LINK_RE.finditer(content):
            target = m.group(1).strip()
            referenced.add(target)
            # 兼容相对路径链接
            if not target.endswith(".md"):
                referenced.add(target + ".md")

    unlinked: list[str] = []
    for rel in sorted(all_notes):
        stem = Path(rel).stem
        # 笔记被直接引用
        if stem in referenced or rel in referenced:
            continue
        # 笔记的某个别名被引用
        if any(
            alias in referenced and Path(target_path).stem == stem
            for alias, target_path in aliases.items()
        ):
            continue
        unlinked.append(rel)
    return unlinked


def _trash_dir(notes_dir: Path) -> Path:
    return notes_dir / ".trash"


def _templates_dir(notes_dir: Path) -> Path:
    return notes_dir / ".templates"


def ensure_meta_dirs(notes_dir: Path) -> None:
    for d in (_trash_dir(notes_dir), _templates_dir(notes_dir)):
        d.mkdir(parents=True, exist_ok=True)
    # 本地 git 历史仓库只跟踪用户内容；用 .gitignore 排除元数据目录，
    # 避免回收站/草稿/旧 .history 快照被卷入历史
    gitignore = notes_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".trash/\n.history/\n.drafts/\n.attachments/\n", encoding="utf-8")


async def move_to_trash(notes_dir: Path, target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"success": False, "error": "文件不存在"}
    try:
        rel = target.relative_to(notes_dir)
    except ValueError:
        return {"success": False, "error": "目标不在笔记目录内"}
    trash = _trash_dir(notes_dir)
    dest = trash / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{dest.suffix}")
    await asyncio.to_thread(shutil.move, str(target), str(dest))
    _cleanup_empty_dirs(notes_dir, target.parent)
    return {"success": True, "path": rel.as_posix()}


def _validate_rel_path(path: str) -> None:
    """校验回收站等场景传入的相对路径：禁止绝对路径、盘符/根路径与目录穿越。"""
    p = Path(path)
    # Windows 上 "/etc/passwd" 虽非 is_absolute，但带根分隔符，拼接后可能越出预期目录
    if p.is_absolute() or p.root or p.drive or ".." in p.parts:
        raise ValueError(f"非法路径: {path}")


def list_trash(notes_dir: Path) -> list[dict[str, Any]]:
    trash = _trash_dir(notes_dir)
    files = []
    if trash.exists():
        for p in sorted(trash.rglob("*.md")):
            rel = p.relative_to(trash).as_posix()
            stat = p.stat()
            files.append({"path": rel, "mtime": stat.st_mtime, "size": stat.st_size})
    return files


async def restore_from_trash(notes_dir: Path, path: str) -> dict[str, Any]:
    try:
        _validate_rel_path(path)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    trash = _trash_dir(notes_dir)
    src = trash / path
    if not src.exists():
        return {"success": False, "error": "回收站中不存在"}
    dest = notes_dir / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return {"success": False, "error": "原位置已存在同名笔记"}
    await asyncio.to_thread(shutil.move, str(src), str(dest))
    _cleanup_empty_dirs(notes_dir, src.parent, trash)
    return {"success": True, "path": path}


async def delete_from_trash(notes_dir: Path, path: str) -> dict[str, Any]:
    try:
        _validate_rel_path(path)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    trash = _trash_dir(notes_dir)
    target = trash / path
    if not target.exists():
        return {"success": False, "error": "回收站中不存在"}
    if target.is_dir():
        await asyncio.to_thread(shutil.rmtree, str(target))
    else:
        await asyncio.to_thread(target.unlink)
    _cleanup_empty_dirs(notes_dir, target.parent, trash)
    return {"success": True}


async def empty_trash(notes_dir: Path) -> dict[str, Any]:
    trash = _trash_dir(notes_dir)
    if trash.exists():
        await asyncio.to_thread(shutil.rmtree, str(trash))
    trash.mkdir(parents=True, exist_ok=True)
    return {"success": True}


def _cleanup_empty_dirs(notes_dir: Path, start_dir: Path, stop_dir: Path | None = None) -> None:
    stop = stop_dir or notes_dir
    for parent in [start_dir, *start_dir.parents]:
        if parent == stop or not parent.is_relative_to(stop):
            break
        try:
            parent.rmdir()
        except OSError:
            break


def create_history_version_sync(notes_dir: Path, target: Path, force: bool = False) -> None:
    """保存前为当前（旧）内容建档：提交进本地 git 仓库作为一条版本。

    force=True 时跳过最小间隔检查（用于「恢复前建档」等必须保留现场的场景）。
    git 不可用时静默降级，不影响保存。
    """
    if not target.exists():
        return
    try:
        rel = target.relative_to(notes_dir).as_posix()
    except ValueError:
        return
    git_history.commit_pre_save(notes_dir, rel, force=force)


def list_history_by_path(notes_dir: Path, rel: str) -> list[dict[str, Any]]:
    """列出 <rel> 笔记的历史版本（git 提交记录，新→旧）。"""
    return git_history.list_versions(notes_dir, rel)


def read_history_by_path(notes_dir: Path, rel: str, timestamp: str) -> dict[str, Any]:
    """读取 <rel> 指定 git 提交（版本）的内容。"""
    return git_history.read_version(notes_dir, rel, timestamp)


def restore_history_by_path(notes_dir: Path, rel: str, timestamp: str) -> dict[str, Any]:
    """将 <rel> 的指定版本还原到原笔记路径。"""
    return git_history.restore_version(notes_dir, rel, timestamp)


def list_templates(notes_dir: Path) -> list[dict[str, Any]]:
    tdir = _templates_dir(notes_dir)
    defaults = {
        "diary.md": "# 日记 {date}\n\n## 今日\n\n## 感悟\n\n",
        "todo.md": "# 待办清单\n\n- [ ] \n- [ ] \n- [ ] \n",
        "meeting.md": "# 会议纪要\n\n## 时间\n{date}\n\n## 参会人\n\n## 议题\n\n## 结论\n\n## 待办\n\n",
    }
    ensure_meta_dirs(notes_dir)
    # 如果目录为空，写入默认模板
    existing = list(tdir.glob("*.md")) if tdir.exists() else []
    if not existing:
        for name, content in defaults.items():
            (tdir / name).write_text(content, encoding="utf-8")
    files = []
    for p in sorted(tdir.glob("*.md")):
        files.append({"name": p.name, "mtime": p.stat().st_mtime})
    return files


def read_template(notes_dir: Path, name: str) -> dict[str, Any]:
    tdir = _templates_dir(notes_dir)
    # 防路径穿越：模板名只能是单个文件名
    if not name or Path(name).name != name or name.startswith((".", "/", "\\")) or ".." in name:
        return {"success": False, "error": "非法模板名"}
    target = tdir / name
    if not target.exists():
        return {"success": False, "error": "模板不存在"}
    # 用 replace 而非 format，避免模板正文中的 { }（LaTeX/代码片段）触发 KeyError
    content = target.read_text(encoding="utf-8").replace(
        "{date}", datetime.now().strftime("%Y-%m-%d")
    )
    return {"success": True, "content": content}


def export_zip(notes_dir: Path) -> bytes:
    skip = _meta_dirs()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in notes_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(notes_dir)
            if _is_hidden(rel) or skip.intersection(rel.parts):
                continue
            zf.write(str(p), rel.as_posix())
    buf.seek(0)
    return buf.read()


def list_attachments(notes_dir: Path) -> list[dict[str, Any]]:
    """列出所有非 .md 附件（图片等）。"""
    skip = _meta_dirs()
    files = []
    if not notes_dir.exists():
        return files
    for p in notes_dir.rglob("*"):
        if not p.is_file() or p.suffix == ".md":
            continue
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        stat = p.stat()
        files.append(
            {
                "path": rel.as_posix(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return sorted(files, key=lambda x: x["mtime"], reverse=True)


def find_orphan_attachments(notes_dir: Path) -> list[dict[str, Any]]:
    """找出未被任何笔记引用的附件（孤儿文件），扫描 images/ 和 .attachments/。"""
    skip = _meta_dirs()
    attachments: list[str] = []
    for scan_dir_name in ("images", ".attachments"):
        scan_dir = notes_dir / scan_dir_name
        if not scan_dir.exists():
            continue
        for p in scan_dir.rglob("*"):
            if p.is_file() and not _is_hidden(p.relative_to(notes_dir)):
                attachments.append(p.relative_to(notes_dir).as_posix())
    if not attachments:
        return []
    # 解析 Markdown 中的链接/图片目标，仅当目标路径与附件路径匹配时才计为引用，
    # 避免正文中出现相似子串导致误报。
    indexed: set[str] = set()
    for p in notes_dir.rglob("*.md"):
        rel = p.relative_to(notes_dir)
        if _is_hidden(rel) or skip.intersection(rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        refs = _extract_referenced_paths(text)
        for att in attachments:
            att_norm = att.strip("/")
            if att_norm in refs:
                indexed.add(att)
                continue
            for ref in refs:
                if ref.endswith("/" + att_norm) or ref.endswith(att_norm):
                    indexed.add(att)
                    break
    orphans = sorted(set(attachments) - indexed)
    return [
        {"path": path, "size": (notes_dir / path).stat().st_size} for path in orphans
    ]


def validate_attachment_path(notes_dir: Path, rel: str) -> None:
    """校验附件相对路径：禁止空路径、目录穿越、隐藏目录与越界路径。

    例外：首段允许 ".attachments"（附件专用目录），否则孤儿附件清理
    对该目录下的文件会永远校验失败。
    """
    if not rel or not isinstance(rel, str):
        raise ValueError("路径不能为空")
    rel_path = Path(rel)
    parts = rel_path.parts
    if ".." in parts or rel_path.is_absolute():
        raise ValueError(f"非法路径: {rel}")
    for i, part in enumerate(parts):
        if part.startswith(".") and not (i == 0 and part == ".attachments"):
            raise ValueError(f"非法路径: {rel}")
    target = notes_dir / rel
    try:
        target.resolve().relative_to(notes_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"路径越界: {rel}") from exc


def delete_attachments(notes_dir: Path, paths: list[str]) -> dict[str, Any]:
    """按相对路径（相对于 notes_dir，如 images/xxx.png）删除附件。"""
    removed = 0
    failed = 0
    for rel in paths:
        try:
            validate_attachment_path(notes_dir, rel)
        except ValueError:
            failed += 1
            continue
        target = notes_dir / rel
        if target.is_file():
            target.unlink()
            removed += 1
        else:
            failed += 1
    return {"success": True, "removed": removed, "failed": failed}
