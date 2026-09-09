"""MDNotes 后端 API（供 Electron 渲染进程通过本地 HTTP 服务调用）。

替代原 Qt 版本中 MainWindow 的所有 Signal/Slot 交互。
前端通过 ``fetch`` 调用 ``/api/<method>``（见 electron_server.py）调用本类方法。

设计原则：
- 纯 Python 核心逻辑（notes_ops / markdown_render / config / search_index 等）
  零改动复用，本类只做「接口编排 + 文件读写 + 路径安全校验」。
- 所有公开方法返回可 JSON 序列化的 dict / list / str / None。
"""

from __future__ import annotations

import os
import re
import json
import time
import asyncio
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config as cfg
from . import markdown_render
from . import notes_ops
from . import search_index
from . import images
from . import webdav_sync
from . import note_crypto
from . import git_history
from .logger import log

import html as _html


def _do_decrypt_note(target: Path, key_b64: str) -> bool:
    """解密一篇笔记文件并写回明文；失败返回 False。"""
    try:
        raw = target.read_bytes()
        plain = note_crypto.decrypt_bytes(raw, key_b64)
        if not plain or not note_crypto.is_encrypted(raw):
            return False
        target.write_bytes(plain)
        return True
    except Exception:  # noqa: BLE001
        return False


def escape_html(s: Any) -> str:
    """转义 HTML 文本（用于站点导出模板）。"""
    return _html.escape(str(s), quote=False)


def escape_attr(s: Any) -> str:
    """转义 HTML 属性值（含引号）。"""
    return _html.escape(str(s), quote=True)

# 预览渲染时把相对图片/资源路径改写为笔记目录绝对 file:/// URI。
# 预编译正则，避免 render_markdown 每次调用都重新编译（高频热路径）。
_RE_IMG_SRC = re.compile(r'(src=")([^"]+)(")')
_RE_IMG_URL = re.compile(r'(url\()([^)]+)(\))')


class WebAPI:
    """暴露给前端的核心 API。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config = cfg.load_config()
        # 用 realpath 统一解析符号链接 / Windows 8.3 短路径，避免
        # self._notes_dir 与子路径 resolve 形式不一致（导致 relative_to 失败）
        self._notes_dir = Path(os.path.realpath(self._config["notes_dir"]))
        notes_ops.ensure_meta_dirs(self._notes_dir)
        # 预建搜索索引表（IF NOT EXISTS，幂等），避免首次搜索/保存时建表竞态
        try:
            search_index.init_index(self._notes_dir)
        except Exception:  # noqa: BLE001
            log.warning("初始化搜索索引失败")
        # 预览渲染时，把相对 images/xxx 改写为笔记目录绝对 file:/// 路径
        self._notes_dir_uri = self._notes_dir.as_uri() + "/"
        # 自动同步后台线程的停止标记
        self._auto_sync_stop = threading.Event()

    def start_webdav_auto_sync(self) -> None:
        """后台守护线程：按配置间隔自动增量备份到 WebDAV。

        仅当 webdav.enabled 且 webdav.url 非空、auto_sync=True 时执行；
        每次等待 interval 后读取最新配置并触发一次上传。配置变更即时生效。
        """
        def _loop() -> None:
            while not self._auto_sync_stop.is_set():
                time.sleep(15)  # 每 15s 检查一次，配置变更无需重启即生效
                if self._auto_sync_stop.is_set():
                    return
                # 配置由 HTTP 请求线程并发修改（整块替换 self._config），
                # 读取与回写 _last_auto_sync 都加锁避免读-改-写竞态
                with self._lock:
                    wd = self._config.get("webdav", {})
                    auto_sync = bool(wd.get("auto_sync")) and bool(wd.get("url"))
                    interval = int(wd.get("auto_sync_interval_min", 30) or 0)
                    last = wd.get("_last_auto_sync")
                if not auto_sync or interval <= 0:
                    continue
                now = time.time()
                if last and (now - last) < interval * 60:
                    continue
                try:
                    self.webdav_sync_now()
                except Exception as exc:  # noqa: BLE001
                    log.warning("WebDAV 自动备份失败: %s", exc)
                    continue
                with self._lock:
                    self._config.setdefault("webdav", {})["_last_auto_sync"] = time.time()

        t = threading.Thread(target=_loop, name="webdav-auto-sync", daemon=True)
        t.start()

    @staticmethod
    def _run_async(coro_fn, *args, **kwargs):
        """在独立线程中运行 async 协程（业务层函数多为 async）。"""
        return asyncio.run(coro_fn(*args, **kwargs))

    # ── 内部工具 ──────────────────────────────────────────

    def _safe_rel(self, rel_path: str) -> Path:
        """把前端传入的相对路径解析为笔记目录内的安全绝对路径。

        防止 ``../`` 越权访问笔记目录之外。
        """
        if not rel_path:
            raise ValueError("空路径")
        # 注意：pathlib 对盘符相对路径（如 C:evil.md）与根相对路径（\evil）
        # 的拼接行为特殊，先用 is_absolute / drive / root 明确拒绝，再做 parents 判定。
        user_path = Path(rel_path)
        if user_path.is_absolute() or user_path.drive or user_path.root:
            raise ValueError(f"非法路径: {rel_path}")
        target = Path(os.path.realpath(str(self._notes_dir / rel_path)))
        if self._notes_dir not in target.parents and target != self._notes_dir:
            raise ValueError(f"非法路径: {rel_path}")
        return target

    def _safe_img_rel(self, imgpath: str) -> str | None:
        """校验图片相对路径：拒绝 ``..`` 分量/盘符/空段，返回清理后的相对路径。

        预览渲染会把相对路径拼成 ``file://`` URI，导出 PNG 时又会按相对路径
        读取本地文件——两者都必须防止 ``../`` 逃出笔记目录。
        """
        imgpath = imgpath.lstrip("/").split("?")[0].split("#")[0]
        if not imgpath:
            return None
        parts = imgpath.replace("\\", "/").split("/")
        if any(p in ("", "..") for p in parts):
            return None
        if ":" in parts[0]:
            return None
        return imgpath

    def _tree_node(self, path: Path, is_dir: bool, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "path": self._rel(path).as_posix(),
            "is_dir": is_dir,
        }

    def _rel(self, absolute_path: Path) -> Path:
        """把绝对路径转为相对笔记目录的 Path。

        使用 os.path.relpath 而非 Path.relative_to，避免 Windows 8.3
        短路径（如 LUOQIN~1）与长路径（Luoqingci）大小写不一致导致的
        ValueError。返回 Path 以兼容调用方对 .parts / .as_posix() 的使用。
        """
        base = str(self._notes_dir)
        return Path(os.path.relpath(str(absolute_path), base).replace("\\", "/"))

    # ── 文件树 ────────────────────────────────────────────

    def list_tree(self) -> list[dict[str, Any]]:
        """返回笔记目录的文件树（两层结构：children 递归）。"""
        skip_dirs = notes_ops._meta_dirs() | {"images", ".attachments"}

        def build(directory: Path) -> list[dict[str, Any]]:
            nodes: list[dict[str, Any]] = []
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError:
                return nodes
            for p in entries:
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    if p.name in skip_dirs:
                        continue
                    node = self._tree_node(p, True, p.name)
                    node["children"] = build(p)
                    nodes.append(node)
                elif p.suffix == ".md":
                    nodes.append(self._tree_node(p, False, p.stem))
            return nodes

        return build(self._notes_dir)

    # ── 笔记读写 ──────────────────────────────────────────

    def _note_enc_key(self, rel_path: str) -> str | None:
        """返回该笔记的加密密钥（若被加密）；否则 None。"""
        return self._config.get("encrypt", {}).get("notes", {}).get(rel_path)

    def _read_note_text(self, fp: Path, rel_path: str) -> str:
        """读取笔记文本，透明解密（若该笔记已加密）。"""
        raw = fp.read_bytes()
        enc_key = self._note_enc_key(rel_path)
        if enc_key:
            plain = note_crypto.decrypt_bytes(raw, enc_key)
            if not plain and note_crypto.is_encrypted(raw):
                raise RuntimeError("解密失败：密码错误或文件已损坏")
            return plain.decode("utf-8")
        return raw.decode("utf-8")

    def _write_note_text(self, fp: Path, rel_path: str, content: str) -> None:
        """写入笔记文本；若该笔记已加密则先加密再落盘。

        原子写：先写同目录临时文件再 os.replace 覆盖，
        避免写入中断（断电/崩溃）留下残缺文件。
        """
        enc_key = self._note_enc_key(rel_path)
        if enc_key:
            data = note_crypto.encrypt_bytes(content.encode("utf-8"), enc_key)
        else:
            data = content.encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(dir=str(fp.parent), prefix=f".{fp.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, fp)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def open_note(self, rel_path: str) -> dict[str, Any]:
        """读取一篇笔记的内容与元信息。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": f"文件不存在: {rel_path}"}
        try:
            content = self._read_note_text(fp, rel_path)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"读取失败: {exc}"}
        stat = fp.stat()
        return {
            "success": True,
            "rel_path": rel_path,
            "content": content,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }

    def get_note_properties(self, rel_path: str) -> dict[str, Any]:
        """返回笔记的详细属性（大小、时间、字数、标签等）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": f"文件不存在: {rel_path}"}
        try:
            content = fp.read_text(encoding="utf-8")
            stat = fp.stat()
            # 标签：行内 #tag 形式，排除代码块中的内容
            tags = sorted(set(re.findall(r"(?<![\w#])#([\w\u4e00-\u9fa5][\w\-/]*)", content)))
            return {
                "success": True,
                "name": fp.stem,
                "rel_path": rel_path,
                "absolute_path": str(fp.resolve()),
                "size": stat.st_size,
                "created": stat.st_ctime,
                "modified": stat.st_mtime,
                "char_count": len(content),
                "word_count": len(content.replace("\n", "").replace(" ", "").replace("\t", "")),
                "line_count": content.count("\n") + 1,
                "tags": tags,
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"读取失败: {exc}"}

    # ── 笔记属性编辑（frontmatter） + 总览 ────────────────
    def get_note_frontmatter(self, rel_path: str) -> dict[str, Any]:
        """返回笔记的 frontmatter 字段（键值对，数组字段为 list）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": f"文件不存在: {rel_path}"}
        try:
            content = self._read_note_text(fp, rel_path)
            data, _ = notes_ops.parse_frontmatter(content)
            return {"success": True, "rel_path": rel_path, "fields": data}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"读取失败: {exc}"}

    def save_note_properties(self, rel_path: str, fields: dict[str, Any]) -> dict[str, Any]:
        """把 fields 合并写入笔记 frontmatter（None 值表示删除该字段）。

        注意：tags 字段走既有 set_tags 逻辑（含层级匹配），其余字段用
        merge_frontmatter 写回。
        """
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": f"文件不存在: {rel_path}"}
        fields = fields or {}
        try:
            content = self._read_note_text(fp, rel_path)
            tags = fields.pop("tags", None)
            if tags is not None:
                if isinstance(tags, str):
                    tags = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
                current = notes_ops.extract_tags(content)
                remove = [t for t in current if t not in tags]
                add = [t for t in tags if t not in current]
                if notes_ops.set_tags(self._notes_dir, rel_path, add, remove):
                    content = self._read_note_text(fp, rel_path)
            new_content = notes_ops.merge_frontmatter(content, fields)
            if new_content != content:
                # 保存前为当前（旧）内容建档，保证属性编辑也进入历史版本
                try:
                    notes_ops.create_history_version_sync(self._notes_dir, fp)
                except Exception:  # noqa: BLE001
                    pass
                self._write_note_text(fp, rel_path, new_content)
            # 同步索引
            try:
                st = fp.stat()
                self._run_async(
                    search_index.index_note_async,
                    self._notes_dir, rel_path, new_content, st.st_mtime, st.st_size,
                )
            except Exception:  # noqa: BLE001
                pass
            return {"success": True, "rel_path": rel_path}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存失败: {exc}"}

    def list_all_notes_meta(self) -> list[dict[str, Any]]:
        """返回全库笔记元信息（供总览表格/看板）：路径、修改时间、大小、标签、
        文件夹与 frontmatter 常用字段（status / priority / due / type）。"""
        out: list[dict[str, Any]] = []
        skip = notes_ops._meta_dirs()
        if not self._notes_dir.exists():
            return out
        try:
            for p in self._notes_dir.rglob("*.md"):
                rel = self._rel(p)
                if any(part.startswith(".") for part in rel.parts) or skip.intersection(rel.parts):
                    continue
                rel_str = rel.as_posix()
                try:
                    content = self._read_note_text(p, rel_str)
                    stat = p.stat()
                except Exception:  # noqa: BLE001
                    continue
                data, _ = notes_ops.parse_frontmatter(content)
                folder = rel_str.rsplit("/", 1)[0] if "/" in rel_str else ""
                out.append({
                    "path": rel_str,
                    "title": p.stem,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "folder": folder,
                    "tags": notes_ops.extract_tags(content),
                    "status": str(data.get("status") or ""),
                    "priority": str(data.get("priority") or ""),
                    "due": str(data.get("due") or ""),
                    "type": str(data.get("type") or ""),
                })
            out.sort(key=lambda n: -(n.get("mtime") or 0))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("列举笔记元信息失败: %s", exc)
            return out

    def save_note(self, rel_path: str, content: str) -> dict[str, Any]:
        """保存笔记内容（覆盖写）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        try:
            # 覆盖前先为当前版本建档（与原 Qt 版行为一致）
            if fp.exists():
                try:
                    notes_ops.create_history_version_sync(self._notes_dir, fp)
                except Exception:  # noqa: BLE001
                    log.warning("历史建档失败（不影响保存）")
            fp.parent.mkdir(parents=True, exist_ok=True)
            self._write_note_text(fp, rel_path, content)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"写入失败: {exc}"}
        # 同步更新全文索引（与原 Qt 版行为一致）
        try:
            st = fp.stat()
            self._run_async(
                search_index.index_note_async,
                self._notes_dir, rel_path, content, st.st_mtime, st.st_size,
            )
        except Exception:  # noqa: BLE001
            log.warning("索引更新失败（不影响保存）")
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0
        return {"success": True, "rel_path": rel_path, "size": size}

    def create_note(self, name: str, parent_dir: str = "") -> dict[str, Any]:
        """在指定目录（相对路径）下新建笔记。"""
        name = (name or "").strip()
        if not name:
            return {"success": False, "error": "文件名不能为空"}
        if not name.endswith(".md"):
            name += ".md"
        if not notes_ops.is_valid_filename(name):
            return {"success": False, "error": "文件名不合法"}
        try:
            base = self._safe_rel(parent_dir) if parent_dir else self._notes_dir
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        target = (base / name).resolve()
        if target.exists():
            return {"success": False, "error": "文件已存在"}
        try:
            target.write_text("", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"创建失败: {exc}"}
        rel = self._rel(target).as_posix()
        return {"success": True, "rel_path": rel}

    def delete_note(self, rel_path: str) -> dict[str, Any]:
        """将笔记移入回收站（与原 Qt 版一致，可恢复）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": "文件不存在"}
        try:
            self._run_async(notes_ops.move_to_trash, self._notes_dir, fp)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除失败: {exc}"}
        try:
            self._run_async(search_index.remove_note_async, self._notes_dir, rel_path)
        except Exception:  # noqa: BLE001
            pass
        return {"success": True, "rel_path": rel_path}

    def rename_note(self, rel_path: str, new_name: str) -> dict[str, Any]:
        """重命名笔记（new_name 不含目录）。"""
        new_name = (new_name or "").strip()
        if not new_name:
            return {"success": False, "error": "新名称不能为空"}
        if not new_name.endswith(".md"):
            new_name += ".md"
        if not notes_ops.is_valid_filename(new_name):
            return {"success": False, "error": "文件名不合法"}
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        new = fp.parent / new_name
        if new.exists():
            return {"success": False, "error": "目标已存在"}
        try:
            fp.rename(new)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"重命名失败: {exc}"}
        old_rel = rel_path
        new_rel = self._rel(new).as_posix()
        return {"success": True, "old_rel_path": old_rel, "rel_path": new_rel}

    # ── 预览渲染 ──────────────────────────────────────────

    def render_markdown(self, text: str) -> dict[str, Any]:
        """将 Markdown 渲染为 HTML（含安全消毒、双链改写）。

        预览中相对 images/xxx 的图片路径会被改写为笔记目录的绝对
        file:/// URI，确保本地图片正确加载。
        """
        try:
            html = markdown_render.render(text)

            def _abs(match: "re.Match") -> str:
                prefix, imgpath, suffix = match.group(1), match.group(2), match.group(3)
                if imgpath.startswith(("http://", "https://", "file://", "data:")):
                    return match.group(0)
                # 去掉前导斜杠，避免 notes_dir_uri 末尾 / 叠加成 //
                imgpath = imgpath.lstrip("/")
                return f"{prefix}{self._notes_dir_uri}{imgpath}{suffix}"

            html = _RE_IMG_SRC.sub(_abs, html)
            html = _RE_IMG_URL.sub(_abs, html)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"渲染失败: {exc}"}
        return {"success": True, "html": html}

    def export_note_png_html(self, rel_path: str) -> dict[str, Any]:
        """返回一篇笔记的渲染 HTML，图片内联为 data URI，供前端转 PNG（避免 canvas 污染）。

        返回 { html }（外层已含 .markdown-body 包裹与内联样式）。若图片读取失败则留空 alt。
        """
        try:
            fp = self._safe_rel(rel_path)
            if not fp.is_file():
                return {"success": False, "error": "笔记不存在"}
            content = self._read_note_text(fp, rel_path)
            html = markdown_render.render(content)

            def _inline(match: "re.Match") -> str:
                prefix, imgpath, suffix = match.group(1), match.group(2), match.group(3)
                if imgpath.startswith(("http://", "https://", "data:")):
                    return match.group(0)
                imgpath = self._safe_img_rel(imgpath)
                if imgpath is None:
                    return f"{prefix}{suffix}"
                candidate = (self._notes_dir / imgpath).resolve()
                if candidate.is_file():
                    try:
                        uri = self._image_data_uri(candidate)
                        if uri:
                            return f"{prefix}{uri}{suffix}"
                    except Exception:  # noqa: BLE001
                        pass
                return f"{prefix}{suffix}"

            html = _RE_IMG_SRC.sub(_inline, html)
            html = _RE_IMG_URL.sub(_inline, html)
            styled = (
                '<div class="mdn-png-root"><div class="markdown-body">' + html + "</div></div>"
            )
            return {"success": True, "html": styled}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导出失败: {exc}"}

    def _image_data_uri(self, fp: Path) -> str | None:
        import base64
        import mimetypes
        try:
            data = fp.read_bytes()
            if not data:
                return None
            if self._config.get("image", {}).get("compress", True):
                try:
                    data = images.compress_image(data, max_width=1920, quality=85)
                except Exception:  # noqa: BLE001
                    pass
            mime = mimetypes.guess_type(fp.name)[0] or "image/png"
            return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        except Exception:  # noqa: BLE001
            return None

    # ── 配置 ──────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        """返回当前配置（用于前端初始化）。"""
        try:
            from importlib.metadata import version as _pkg_version
            pkg_version = _pkg_version("mdnotes")
        except Exception:  # noqa: BLE001
            pkg_version = "1.0.0"
        return {
            "notes_dir": str(self._notes_dir),
            "theme": self._config.get("theme", "genshin"),
            "image": self._config.get("image", {}),
            "editor": self._config.get("editor", {}),
            "version": pkg_version,
        }

    def get_notes_dir(self) -> str:
        """返回笔记目录绝对路径。"""
        return str(self._notes_dir)

    def open_folder(self, path: str) -> dict[str, Any]:
        """在系统文件管理器中打开指定目录（导出完成后便于定位）。"""
        try:
            import webbrowser
            target = Path(path).expanduser().resolve()
            if not target.exists():
                return {"success": False, "error": "目录不存在"}
            webbrowser.open(target.as_uri()) if target.is_dir() else webbrowser.open(target.parent.as_uri())
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            log.warning("打开目录失败: %s", exc)
            return {"success": False, "error": str(exc)}

    # ── 资源读取（图片等） ────────────────────────────────

    def read_note_image(self, rel_path: str) -> dict[str, Any] | None:
        """读取笔记目录下图片，返回 base64 data URI 供前端 <img> 使用。

        主要用于规避 file:/// 跨目录访问限制（预览内相对路径已处理，
        此接口备用）。
        """
        try:
            fp = self._safe_rel(rel_path)
        except ValueError:
            return None
        if not fp.is_file():
            return None
        import base64
        import mimetypes

        try:
            data = fp.read_bytes()
        except OSError as exc:
            log.warning("读取图片失败: %s", exc)
            return None
        mime = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
        b64 = base64.b64encode(data).decode("ascii")
        return {"success": True, "data_uri": f"data:{mime};base64,{b64}"}

    # ── 占位：搜索 / 标签 / 反向链接（阶段 2/3 接入） ─────

    def search(
        self,
        query: str,
        scope: str = "",
        tag: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> list[dict[str, Any]]:
        """全文搜索（委托 search_index）。

        scope      目录前缀过滤（相对路径）
        tag        #标签过滤（匹配含该标签的笔记；层级标签匹配子级）
        date_from / date_to  按修改日期过滤（YYYY-MM-DD），闭区间
        """
        try:
            results = self._run_async(search_index.search_async, self._notes_dir, query)
        except Exception as exc:  # noqa: BLE001
            log.warning("搜索失败: %s", exc)
            return []
        results = results or []

        def in_scope(path: str) -> bool:
            p = (path or "").strip("/").replace("\\", "/")
            return p == scope or p.startswith(scope + "/")

        if scope:
            scope = scope.strip("/").replace("\\", "/")
            results = [r for r in results if in_scope(r.get("path", ""))]

        # 日期过滤：解析 YYYY-MM-DD 为时间戳闭区间
        date_from_ts = self._date_to_ts(date_from)
        date_to_ts = self._date_to_ts(date_to, end_of_day=True)
        if date_from_ts is not None or date_to_ts is not None:
            filtered = []
            for r in results:
                mt = r.get("mtime") or 0
                if date_from_ts is not None and mt < date_from_ts:
                    continue
                if date_to_ts is not None and mt > date_to_ts:
                    continue
                filtered.append(r)
            results = filtered

        # 标签过滤：逐篇读取标签判断（结果已限 200 条，开销可控）
        if tag:
            tag = tag.strip().lstrip("#").strip()
            tagged = []
            for r in results:
                rel = r.get("path", "")
                if not rel:
                    continue
                try:
                    fp = self._notes_dir / rel
                    content = fp.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                tags = notes_ops.extract_tags(content)
                if tag in tags or any(t.startswith(tag + "/") for t in tags):
                    tagged.append(r)
            results = tagged
        return results

    @staticmethod
    def _date_to_ts(date_str: str, end_of_day: bool = False) -> float | None:
        """把 YYYY-MM-DD 转为时间戳；无效/空返回 None。"""
        if not date_str:
            return None
        try:
            d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59)
            return d.timestamp()
        except (ValueError, OSError):
            return None

    # ── 保存搜索（搜索增强） ────────────────────────────────
    def list_saved_searches(self) -> list[dict[str, Any]]:
        """返回已保存的搜索列表（名称 -> 查询参数）。"""
        try:
            saved = self._config.get("saved_searches", []) or []
            return [dict(s) for s in saved]
        except Exception as exc:  # noqa: BLE001
            log.warning("读取保存搜索失败: %s", exc)
            return []

    def save_search(
        self, name: str, query: str, scope: str = "", tag: str = ""
    ) -> dict[str, Any]:
        """保存一条搜索（重名覆盖）。"""
        name = (name or "").strip()
        if not name or not (query or "").strip():
            return {"success": False, "error": "名称与关键词不能为空"}
        try:
            with self._lock:
                saved = [s for s in self._config.get("saved_searches", []) or [] if s.get("name") != name]
                saved.append({"name": name, "query": query, "scope": scope or "", "tag": (tag or "").lstrip("#").strip()})
                self._config["saved_searches"] = saved
                cfg.save_config(self._config)
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存失败: {exc}"}

    def delete_saved_search(self, name: str) -> dict[str, Any]:
        """删除一条保存的搜索。"""
        try:
            with self._lock:
                saved = [s for s in self._config.get("saved_searches", []) or [] if s.get("name") != name]
                self._config["saved_searches"] = saved
                cfg.save_config(self._config)
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除失败: {exc}"}


    def list_tags(self) -> dict[str, int]:
        """返回标签云数据。"""
        try:
            return notes_ops.list_tags(self._notes_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("标签统计失败: %s", exc)
            return {}

    def tag_rename(self, old_tag: str, new_tag: str) -> dict[str, Any]:
        """把 #old_tag 重命名为 #new_tag（跨全部笔记）。"""
        old_tag = (old_tag or "").lstrip("#").strip()
        new_tag = (new_tag or "").lstrip("#").strip()
        if not old_tag or not new_tag:
            return {"success": False, "error": "标签不能为空"}
        if old_tag == new_tag:
            return {"success": False, "error": "新旧标签相同"}
        try:
            count = notes_ops.rename_tag(self._notes_dir, old_tag, new_tag)
            self._reindex_all()
            return {"success": True, "count": count}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"重命名失败: {exc}"}

    # ── 标签视觉管理：颜色 + 拖拽整理 ─────────────────────
    def get_tag_colors(self) -> dict[str, str]:
        """返回标签颜色映射（标签 -> 颜色值）。"""
        try:
            return dict(self._config.get("tags", {}).get("colors", {}) or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("读取标签颜色失败: %s", exc)
            return {}

    def set_tag_color(self, tag: str, color: str) -> dict[str, Any]:
        """设置单个标签的颜色（空颜色则清除）。"""
        tag = (tag or "").lstrip("#").strip()
        color = (color or "").strip()
        if not tag:
            return {"success": False, "error": "标签不能为空"}
        try:
            with self._lock:
                colors = dict(self._config.get("tags", {}).get("colors", {}) or {})
                if color:
                    colors[tag] = color
                else:
                    colors.pop(tag, None)
                self._config.setdefault("tags", {})["colors"] = colors
                cfg.save_config(self._config)
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"设置失败: {exc}"}

    def tag_move(self, source: str, dest: str = "") -> dict[str, Any]:
        """拖拽整理：把标签 source 移动到目标父标签 dest 下（dest 为空表示移到根）。

        实现为全局重命名：source -> dest/source（或 source 末段当移到根），
        同时移动其全部层级子标签。
        """
        source = (source or "").lstrip("#").strip()
        dest = (dest or "").lstrip("#").strip("/")
        if not source:
            return {"success": False, "error": "标签不能为空"}
        if source == dest:
            return {"success": False, "error": "目标与来源相同"}
        # 禁止移到自己的后代下（会形成循环）
        if dest.startswith(source + "/"):
            return {"success": False, "error": "不能移动到自己的子标签下"}
        if dest:
            new_tag = dest + "/" + source
        else:
            new_tag = source.split("/")[-1]
        if new_tag == source:
            return {"success": False, "error": "标签已在该位置"}
        try:
            count = notes_ops.rename_tag(self._notes_dir, source, new_tag)
            self._reindex_all()
            # 同步迁移颜色
            with self._lock:
                colors = dict(self._config.get("tags", {}).get("colors", {}) or {})
                for k, v in list(colors.items()):
                    if k == source or k.startswith(source + "/"):
                        colors.pop(k, None)
                        colors[k.replace(source, new_tag, 1)] = v
                self._config.setdefault("tags", {})["colors"] = colors
                cfg.save_config(self._config)
            return {"success": True, "count": count, "new_tag": new_tag}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"移动失败: {exc}"}

    # ── 任务视图 ──────────────────────────────────────────
    def scan_tasks(self, done_filter: str = "") -> list[dict[str, Any]]:
        """扫描全库任务清单项（done_filter: "" 全部 / open / done）。"""
        try:
            return notes_ops.scan_tasks(self._notes_dir, done_filter or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描任务失败: %s", exc)
            return []


    def batch_tag(self, paths: list[str], add_tags: list[str], remove_tags: list[str]) -> dict[str, Any]:
        """为一批笔记批量添加/删除标签。paths 为相对路径列表。"""
        paths = paths or []
        if not paths:
            return {"success": False, "error": "未选择笔记"}
        try:
            ok, failed = 0, 0
            for rel in paths:
                if notes_ops.set_tags(self._notes_dir, rel, add_tags or [], remove_tags or []):
                    ok += 1
                else:
                    failed += 1
            self._last_batch_success = ok
            self._reindex_all()
            return {"success": True, "ok": ok, "failed": failed}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"批量打标签失败: {exc}"}

    # ── 双链 / 代码语言补全候选 ──────────────────────────

    def list_note_names(self) -> list[str]:
        """返回所有笔记的相对路径（不含 .md 扩展名），供快速切换/双链补全。

        子目录笔记返回形如 `子目录/笔记名`，保证能从补全/双链直接打开。
        """
        names: list[str] = []
        try:
            for p in self._notes_dir.rglob("*.md"):
                rel = self._rel(p)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if notes_ops._meta_dirs().intersection(rel.parts):
                    continue
                rel_str = rel.as_posix()
                names.append(rel_str[:-3] if rel_str.endswith(".md") else rel_str)
        except Exception as exc:  # noqa: BLE001
            log.warning("列举笔记名失败: %s", exc)
        return sorted(set(names))

    @staticmethod
    def list_code_languages() -> list[str]:
        """返回常用代码语言候选（与原 Qt 版一致）。"""
        return [
            "python", "javascript", "typescript", "java", "cpp", "c", "csharp", "go",
            "rust", "ruby", "php", "swift", "kotlin", "scala", "perl", "lua",
            "bash", "shell", "powershell", "cmd", "bat",
            "html", "css", "scss", "less", "xml", "json", "yaml", "toml", "ini",
            "sql", "graphql", "protobuf",
            "markdown", "latex", "dockerfile", "makefile", "cmake", "nginx",
            "diff", "text", "plaintext",
        ]

    # ── 图片保存（拖拽 / 粘贴） ──────────────────────────

    def save_image(self, data_b64: str, src_ext: str = "") -> dict[str, Any]:
        """保存前端传来的图片（base64），返回相对路径 images/xxx。

        压缩参数取自 config.image。
        """
        import base64
        import uuid

        try:
            data = base64.b64decode(data_b64)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"解码失败: {exc}"}
        if not data:
            return {"success": False, "error": "空数据"}
        try:
            img_cfg = self._config.get("image", {})
            if img_cfg.get("compress", True):
                try:
                    data = images.compress_image(
                        data,
                        max_width=img_cfg.get("max_width", 1920),
                        quality=img_cfg.get("quality", 85),
                    )
                except Exception:  # noqa: BLE001
                    pass
            # 识别真实扩展名
            ext = src_ext or ".png"
            if ext.startswith("."):
                ext = ext[1:]
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                ext = "png"
            elif data[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                ext = "gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ext = "webp"
            images_dir = self._notes_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            uid = uuid.uuid4().hex[:6]
            filename = f"{ts}_{uid}.{ext}"
            (images_dir / filename).write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存失败: {exc}"}
        return {"success": True, "rel_path": f"images/{filename}"}

    # ── 反向链接 / 未关联 / 回收站（阶段3） ───────────────

    def list_backlinks(self, rel_path: str) -> list[dict[str, Any]]:
        """返回引用了 rel_path 的其他笔记（双链反向链接）。"""
        import re
        stem = Path(rel_path).stem
        backlink_re = re.compile(rf"\[\[{re.escape(stem)}(\|[^\]]*)?\]\]")
        results: list[dict[str, Any]] = []
        try:
            for p in self._notes_dir.rglob("*.md"):
                r = self._rel(p)
                if r.as_posix() == rel_path:
                    continue
                if any(part.startswith(".") for part in r.parts):
                    continue
                if notes_ops._meta_dirs().intersection(r.parts):
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                m = backlink_re.search(content)
                if m:
                    idx = m.start()
                    start = max(0, idx - 40)
                    end = min(len(content), m.end() + 40)
                    snippet = content[start:end].replace("\n", " ").strip()
                    if start > 0:
                        snippet = "…" + snippet
                    if end < len(content):
                        snippet += "…"
                    results.append({"path": r.as_posix(), "snippet": snippet})
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描反向链接失败: %s", exc)
        return results

    def list_unlinked(self) -> list[str]:
        """返回未被任何其他笔记引用的笔记。"""
        try:
            return notes_ops.find_unlinked_notes(self._notes_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描未关联笔记失败: %s", exc)
            return []

    _WIKILINK_RE = re.compile(r"(?<!\[)\[\[([^\]|\n]+)(?:\|[^\]]*)?\]\]")
    _TAG_RE = re.compile(r"(?<![\w/])#([\w\u4e00-\u9fa5][\w\u4e00-\u9fa5\-/]*)(?![\w/])")
    _H1_RE = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$", re.MULTILINE)

    def build_graph(self) -> dict[str, Any]:
        """构建双链关系图谱：返回节点（笔记）与边（[[链接]] 引用）。

        节点: {id, title, path, tags[]}
        边:   {source, target}  —— source 通过 [[target]] 引用了 target
        带简单缓存（按文件 mtime 摘要），避免每次重建。
        """
        cache_file = self._notes_dir / ".graph_cache.json"
        files: list[Path] = []
        mtimes: dict[str, float] = {}
        try:
            for p in self._notes_dir.rglob("*.md"):
                rel = self._rel(p)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if notes_ops._meta_dirs().intersection(rel.parts):
                    continue
                files.append(p)
                mtimes[rel.as_posix()] = p.stat().st_mtime
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描图谱文件失败: %s", exc)
            return {"nodes": [], "edges": []}

        # 缓存命中判断：文件集合与 mtime 完全一致
        try:
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached.get("_mtimes") == mtimes and "_mtimes" in cached:
                    return {"nodes": cached["nodes"], "edges": cached["edges"]}
        except Exception:  # noqa: BLE001
            pass

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        edge_seen: set[tuple[str, str]] = set()
        titles: dict[str, str] = {}

        # 先收集标题，便于 target 解析后回填显示名
        for p in files:
            rel = self._rel(p).as_posix()
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                text = ""
            h1 = self._H1_RE.search(text)
            title = h1.group(1).strip() if h1 else p.stem
            titles[rel] = title
            if rel not in seen_paths:
                seen_paths.add(rel)
                tags = sorted(set(self._TAG_RE.findall(text)))
                nodes.append({"id": rel, "title": title, "path": rel, "tags": tags})

        for p in files:
            src = self._rel(p).as_posix()
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            for m in self._WIKILINK_RE.finditer(text):
                target = m.group(1).strip()
                if not target:
                    continue
                dst = self.resolve_wikilink(target)
                if not dst or dst == src:
                    continue
                key = (src, dst)
                if key in edge_seen:
                    continue
                edge_seen.add(key)
                edges.append({"source": src, "target": dst})
                # 确保 target 也在节点中（即便它还没被扫描到标题）
                if dst not in seen_paths:
                    seen_paths.add(dst)
                    nodes.append({"id": dst, "title": titles.get(dst, Path(dst).stem), "path": dst, "tags": []})

        try:
            cache_file.write_text(
                json.dumps({"_mtimes": mtimes, "nodes": nodes, "edges": edges}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        return {"nodes": nodes, "edges": edges}

    def list_trash(self) -> list[dict[str, Any]]:
        """返回回收站中的笔记列表。"""
        try:
            return notes_ops.list_trash(self._notes_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("列举回收站失败: %s", exc)
            return []

    def restore_trash(self, rel_path: str) -> dict[str, Any]:
        """从回收站恢复笔记。"""
        try:
            return self._run_async(notes_ops.restore_from_trash, self._notes_dir, rel_path)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"恢复失败: {exc}"}

    def delete_trash(self, rel_path: str) -> dict[str, Any]:
        """从回收站彻底删除。"""
        try:
            return self._run_async(notes_ops.delete_from_trash, self._notes_dir, rel_path)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除失败: {exc}"}

    def empty_trash(self) -> dict[str, Any]:
        """清空回收站。"""
        try:
            return self._run_async(notes_ops.empty_trash, self._notes_dir)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"清空失败: {exc}"}

    # ── 设置 ───────────────────────────────────────────────
    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """更新配置（只写允许修改的顶层键）。"""
        try:
            allowed = {
                "notes_dir", "image", "webdav",
                "font_size", "theme", "editor",
                "app_lock", "encrypt",
            }
            # theme 白名单校验（与 list_themes 一致），防止非法值写坏配置
            if "theme" in patch:
                valid_themes = {t["id"] for t in self.list_themes()}
                if patch["theme"] not in valid_themes:
                    return {"success": False, "error": f"未知主题: {patch['theme']}"}
            with self._lock:
                data = cfg.load_config()
                for k, v in patch.items():
                    if k in allowed:
                        if isinstance(v, dict) and isinstance(data.get(k), dict):
                            data[k].update(v)
                        else:
                            data[k] = v
                cfg.save_config(data)
                # 立即刷新内存中的配置，否则同步/图片等后续读取到旧值
                self._config = data
            # 若修改了笔记目录，刷新内部引用
            if "notes_dir" in patch:
                new_dir = Path(patch["notes_dir"]).expanduser().resolve()
                if new_dir != self._notes_dir:
                    self._notes_dir = new_dir
                    notes_ops.ensure_meta_dirs(self._notes_dir)
                    self._notes_dir_uri = self._notes_dir.as_uri() + "/"
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存配置失败: {exc}"}

    # ── 应用锁（启动密码） ────────────────────────────────
    def app_lock_enabled(self) -> bool:
        """是否已启用应用锁。"""
        try:
            return bool(self._config.get("app_lock", {}).get("enabled"))
        except Exception:  # noqa: BLE001
            return False

    def app_lock_set(self, password: str) -> dict[str, Any]:
        """设置/修改应用锁密码并启用。密码非空则启用。"""
        if not password:
            return {"success": False, "error": "密码不能为空"}
        try:
            with self._lock:
                data = cfg.load_config()
                data.setdefault("app_lock", {})["password"] = cfg.auth.hash_token(password)
                data["app_lock"]["enabled"] = True
                cfg.save_config(data)
                self._config = data
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"设置失败: {exc}"}

    def app_lock_verify(self, password: str) -> bool:
        """校验应用锁密码是否正确。"""
        try:
            stored = self._config.get("app_lock", {}).get("password", "")
            return bool(stored and cfg.auth.verify_token(password, stored))
        except Exception:  # noqa: BLE001
            return False

    def app_lock_disable(self, password: str) -> dict[str, Any]:
        """校验密码后关闭应用锁。"""
        if not self.app_lock_verify(password):
            return {"success": False, "error": "密码错误"}
        try:
            with self._lock:
                data = cfg.load_config()
                data.setdefault("app_lock", {})["enabled"] = False
                cfg.save_config(data)
                self._config = data
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"关闭失败: {exc}"}

    # ── 单篇笔记加密 ────────────────────────────────────
    def encrypt_status(self, rel_path: str) -> dict[str, Any]:
        """返回某篇笔记是否已加密。"""
        try:
            notes = self._config.get("encrypt", {}).get("notes", {})
            return {"success": True, "encrypted": bool(notes.get(rel_path))}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    def encrypt_set_password(self, password: str) -> dict[str, Any]:
        """设置/更换加密主密码（据此派生全局密钥，已加密笔记自动重加密）。"""
        if not note_crypto.available():
            return {"success": False, "error": "缺少 cryptography 依赖，无法加密"}
        if not password:
            return {"success": False, "error": "密码不能为空"}
        try:
            with self._lock:
                data = cfg.load_config()
                new_key = note_crypto.derive_key(password)
                enc = data.setdefault("encrypt", {})["notes"] = dict(data.get("encrypt", {}).get("notes", {}))
                old_key = data.get("encrypt", {}).get("key")
                if old_key:
                    for rel, key in enc.items():
                        fp = self._safe_rel(rel)
                        if fp.is_file():
                            raw = fp.read_bytes()
                            if note_crypto.is_encrypted(raw):
                                fp.write_bytes(note_crypto.rekey_bytes(raw, key, new_key))
                                enc[rel] = new_key
                data["encrypt"]["key"] = new_key
                cfg.save_config(data)
                self._config = data
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"设置失败: {exc}"}

    def encrypt_toggle(self, rel_path: str, password: str, encrypt: bool) -> dict[str, Any]:
        """加密或解密一篇笔记。

        加密：用主密码派生的密钥（或已有全局密钥）加密笔记，计入 encrypt.notes；
        解密：用该笔记存储的密钥解出明文并落盘，从 encrypt.notes 移除。
        """
        try:
            target = self._safe_rel(rel_path)
            if not target.is_file():
                return {"success": False, "error": "笔记不存在"}
            with self._lock:
                data = cfg.load_config()
                enc = data.setdefault("encrypt", {})["notes"] = dict(data.get("encrypt", {}).get("notes", {}))
                is_enc = bool(enc.get(rel_path))
                key = data.get("encrypt", {}).get("key") or (note_crypto.derive_key(password) if password else "")
                if encrypt and not is_enc:
                    if not key:
                        return {"success": False, "error": "请先在设置中设置加密主密码，或提供密码"}
                    if not note_crypto.available():
                        return {"success": False, "error": "缺少 cryptography 依赖，无法加密"}
                    enc[rel_path] = key
                elif not encrypt and is_enc:
                    if not _do_decrypt_note(target, enc[rel_path]):
                        return {"success": False, "error": "解密失败：密码错误或文件已损坏"}
                    enc.pop(rel_path, None)
                cfg.save_config(data)
                self._config = data
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"操作失败: {exc}"}

    # ── 历史版本（基于本地 git 仓库） ───────────────────────
    def list_history(self, rel_path: str) -> list[dict[str, Any]]:
        """列出某笔记的历史版本。"""
        try:
            fp = self._safe_rel(rel_path)
            rel = self._rel(fp)
            return notes_ops.list_history_by_path(self._notes_dir, rel.as_posix())
        except Exception as exc:  # noqa: BLE001
            log.warning("读取历史失败: %s", exc)
            return []

    def read_history(self, rel_path: str, timestamp: str) -> dict[str, Any]:
        """读取指定历史版本内容。"""
        try:
            fp = self._safe_rel(rel_path)
            rel = self._rel(fp)
            return notes_ops.read_history_by_path(self._notes_dir, rel.as_posix(), timestamp)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"读取失败: {exc}"}

    def restore_history(self, rel_path: str, timestamp: str) -> dict[str, Any]:
        """恢复指定历史版本。"""
        try:
            fp = self._safe_rel(rel_path)
            rel = self._rel(fp)
            return notes_ops.restore_history_by_path(self._notes_dir, rel.as_posix(), timestamp)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"恢复失败: {exc}"}

    def diff_versions(self, rel_path: str, ts_a: str, ts_b: str = "") -> dict[str, Any]:
        """对比两个历史版本（或版本与当前内容）的差异。

        ts_a / ts_b 为 git 提交号；ts_b 为空时与当前磁盘内容对比。
        返回统一 diff 文本（行）与增删行统计。
        """
        import difflib

        try:
            fp = self._safe_rel(rel_path)
            rel = self._rel(fp)
            rel_str = rel.as_posix()
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        def _content(ts: str) -> str | None:
            if not ts:
                try:
                    return self._read_note_text(fp, rel_str)
                except Exception:  # noqa: BLE001
                    return None
            res = git_history.read_version(self._notes_dir, rel_str, ts)
            if not res.get("success"):
                return None
            return res.get("content", "")

        a = _content(ts_a)
        b = _content(ts_b)
        if a is None or b is None:
            return {"success": False, "error": "版本不存在或读取失败"}

        a_lines = a.splitlines(keepends=True)
        b_lines = b.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=f"{rel_str} @{ts_a[:7] if ts_a else '当前'}",
            tofile=f"{rel_str} @{ts_b[:7] if ts_b else '当前'}",
            lineterm="\n",
        ))
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
        return {
            "success": True,
            "diff": "".join(diff_lines),
            "added": added,
            "removed": removed,
            "identical": not diff_lines,
        }

    # ── WebDAV 同步 ────────────────────────────────────────
    def webdav_sync_status(self) -> dict[str, Any]:
        """返回 WebDAV 同步配置概览（不含密码）。"""
        try:
            w = self._config.get("webdav", {})
            return {
                "enabled": bool(w.get("enabled")),
                "url": w.get("url", ""),
                "username": w.get("username", ""),
                "remote_dir": w.get("remote_dir", "/mdnotes"),
                "auto_sync": bool(w.get("auto_sync")),
                "has_password": bool(w.get("password")),
            }
        except Exception as exc:  # noqa: BLE001
            return {"enabled": False, "error": str(exc)}

    def webdav_sync_now(self) -> dict[str, Any]:
        """立即同步到 WebDAV。"""
        try:
            w = self._config.get("webdav", {})
            if not w.get("url"):
                return {"success": False, "error": "未配置 WebDAV 地址"}
            res = self._run_async(
                webdav_sync.sync_to_webdav,
                self._notes_dir,
                w["url"],
                w.get("username", ""),
                w.get("password", ""),
                w.get("remote_dir", "/mdnotes"),
            )
            if not res.get("success"):
                return {"success": False, "error": res.get("error") or "同步失败"}
            uploaded = res.get("uploaded", 0)
            skipped = res.get("skipped", 0)
            failed = res.get("failed", 0)
            return {
                "success": True,
                "merged": uploaded,
                "uploaded": uploaded,
                "skipped": skipped,
                "failed": failed,
                "message": f"上传 {uploaded} 个，跳过 {skipped} 个，失败 {failed} 个",
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"同步失败: {exc}"}

    def webdav_preview_restore(self) -> dict[str, Any]:
        """预检从 WebDAV 恢复到本地会产生的差异，不实际写入。"""
        try:
            w = self._config.get("webdav", {})
            if not w.get("url"):
                return {"success": False, "error": "未配置 WebDAV 地址"}
            return self._run_async(
                webdav_sync.preview_sync_from_webdav,
                self._notes_dir,
                w["url"],
                w.get("username", ""),
                w.get("password", ""),
                w.get("remote_dir", "/mdnotes"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"预检失败: {exc}"}

    def webdav_resolve_restore(self, resolutions: dict[str, str]) -> dict[str, Any]:
        """根据用户逐文件决策执行 WebDAV 恢复/合并。resolutions: {rel: 'local'|'remote'|'merge'|'delete'}"""
        try:
            w = self._config.get("webdav", {})
            if not w.get("url"):
                return {"success": False, "error": "未配置 WebDAV 地址"}
            res = self._run_async(
                webdav_sync.resolve_sync_from_webdav,
                self._notes_dir,
                w["url"],
                w.get("username", ""),
                w.get("password", ""),
                w.get("remote_dir", "/mdnotes"),
                resolutions,
            )
            # 恢复实际改动了文件时，重建搜索索引
            if res.get("success") and (res.get("downloaded") or res.get("merged") or res.get("deleted")):
                self._reindex_all()
            return res
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"恢复失败: {exc}"}

    # ── ZIP 导入导出 ───────────────────────────────────────
    def export_zip(self) -> dict[str, Any]:
        """导出笔记目录为 ZIP（返回 base64 字节）。"""
        try:
            data = notes_ops.export_zip(self._notes_dir)
            import base64
            return {"success": True, "data": base64.b64encode(data).decode("ascii")}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导出失败: {exc}"}

    def export_zip_to(self, target_path: str) -> dict[str, Any]:
        """导出笔记目录为 ZIP 并写入指定路径。"""
        try:
            target = Path(target_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            data = notes_ops.export_zip(self._notes_dir)
            target.write_bytes(data)
            return {"success": True, "path": str(target)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导出失败: {exc}"}

    # ── 静态站点导出（P2：语雀「分享页」的隐私可控平替） ──────
    _WIKILINK_EXPORT_RE = re.compile(r'<a class="wikilink" data-target="([^"]+)" href="#">([^<]*)</a>')

    def export_site(self, target_path: str) -> dict[str, Any]:
        """把整个知识库导出为带搜索 + 图谱的静态网站（纯 HTML，可本地打开或推 GitHub Pages）。

        产物结构：
          <target>/index.html            首页（笔记列表 + 搜索 + 图谱）
          <target>/note.html             笔记查看器（?p=相对路径.html 读取预渲染数据）
          <target>/assets/site.css
          <target>/assets/site.js        搜索（search-index.json）+ 图谱（graph.json）
          <target>/assets/search-index.json
          <target>/assets/graph.json
          <target>/notes/<rel>.html      每篇笔记渲染结果（保持目录层级）
          <target>/images/*              复制的图片资源
        """
        import shutil
        try:
            target = Path(target_path).expanduser().resolve()
            (target / "notes").mkdir(parents=True, exist_ok=True)
            (target / "assets").mkdir(parents=True, exist_ok=True)
            (target / "images").mkdir(parents=True, exist_ok=True)

            files: list[Path] = []
            for p in self._notes_dir.rglob("*.md"):
                rel = self._rel(p)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if notes_ops._meta_dirs().intersection(rel.parts):
                    continue
                files.append(p)

            search_index_data: list[dict[str, Any]] = []
            written = 0
            for p in files:
                rel = self._rel(p).as_posix()
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                html = markdown_render.render(text)
                # 把 wikilink 的 data-target 解析为站点内相对链接
                html = self._WIKILINK_EXPORT_RE.sub(
                    lambda m: self._export_wikilink(m, rel), html
                )
                h1 = self._H1_RE.search(text)
                title = h1.group(1).strip() if h1 else Path(rel).stem
                tags = sorted(set(self._TAG_RE.findall(text)))
                # 笔记页相对站点根的路径（notes/ 下一级，去掉 .md 后缀）
                note_html_rel = "notes/" + rel[:-3] + ".html" if rel.endswith(".md") else "notes/" + rel + ".html"
                note_html_path = target / note_html_rel
                note_html_path.parent.mkdir(parents=True, exist_ok=True)
                note_html_path.write_text(
                    self._note_page_html(rel, title, tags, html), encoding="utf-8"
                )
                written += 1
                # 纯文本用于搜索索引（去 markdown 符号）
                plain = re.sub(r"[#>*_`\[\]()!-]", " ", text)
                plain = re.sub(r"\s+", " ", plain).strip()
                search_index_data.append({
                    "path": note_html_rel,
                    "rel": rel,
                    "title": title,
                    "tags": tags,
                    "text": plain[:2000],
                })

            # 图谱数据（复用 build_graph）
            graph = self.build_graph()
            (target / "assets" / "graph.json").write_text(
                json.dumps(graph, ensure_ascii=False), encoding="utf-8"
            )
            (target / "assets" / "search-index.json").write_text(
                json.dumps(search_index_data, ensure_ascii=False), encoding="utf-8"
            )

            # 复制图片
            src_images = self._notes_dir / "images"
            if src_images.exists():
                for img in src_images.iterdir():
                    if img.is_file():
                        try:
                            shutil.copy2(img, target / "images" / img.name)
                        except Exception:  # noqa: BLE001
                            pass

            # 首页 + 资源
            (target / "index.html").write_text(
                self._site_index_html(search_index_data, graph), encoding="utf-8"
            )
            (target / "assets" / "site.css").write_text(_SITE_CSS, encoding="utf-8")
            (target / "assets" / "site.js").write_text(_SITE_JS, encoding="utf-8")

            return {"success": True, "path": str(target), "files": written}
        except Exception as exc:  # noqa: BLE001
            log.warning("导出站点失败: %s", exc)
            return {"success": False, "error": f"导出失败: {exc}"}

    def _export_wikilink(self, m: "re.Match", from_rel: str) -> str:
        target = m.group(1).strip()
        label = m.group(2) or target
        dst = self.resolve_wikilink(target)
        if not dst:
            # 目标笔记不存在：渲染为普通文本 + 标题属性
            return f'<span class="wikilink-missing" title="未找到笔记：{escape_attr(target)}">{escape_html(label)}</span>'
        # 当前页在 notes/<from_rel>.html，目标在 notes/<dst>.html
        # 计算从当前页到目标页的相对路径
        from_parts = Path(from_rel).parts
        depth = len(from_parts) - 1  # notes/ 下一级的子目录层数
        prefix = "../" * depth if depth > 0 else ""
        dst_rel = dst[:-3] + ".html" if dst.endswith(".md") else dst + ".html"
        href = prefix + "notes/" + dst_rel
        return f'<a class="wikilink" href="{href}">{escape_html(label)}</a>'

    def _note_page_html(self, rel: str, title: str, tags: list[str], body_html: str) -> str:
        tag_html = "".join(f'<span class="tag">#{escape_html(t)}</span>' for t in tags)
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape_html(title)} · MDNotes</title>
<link rel="stylesheet" href="../assets/site.css">
</head><body class="note-page">
<header class="np-head"><a class="np-home" href="../index.html">← 返回首页</a>
<span class="np-path">{escape_html(rel)}</span></header>
<article class="markdown-body">
<h1 class="np-title">{escape_html(title)}</h1>
<div class="np-tags">{tag_html}</div>
{body_html}
</article>
<footer class="np-foot">由 MDNotes 导出 · 本地优先 · 隐私可控</footer>
</body></html>"""

    def _site_index_html(self, index: list[dict[str, Any]], graph: dict[str, Any]) -> str:
        notes_li = "\n".join(
            f'<li><a href="{escape_attr(n["path"])}">{escape_html(n["title"])}</a>'
            f'<span class="li-path">{escape_html(n["rel"])}</span></li>'
            for n in index
        )
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MDNotes 知识库</title>
<link rel="stylesheet" href="assets/site.css">
</head><body>
<header class="site-head">
  <h1>📚 MDNotes 知识库</h1>
  <input type="search" id="site-search" placeholder="搜索笔记内容 / 标题 / #标签…" autocomplete="off">
</header>
<main class="site-main">
  <section class="site-list">
    <h2>笔记（{len(index)}）</h2>
    <ul id="note-list">{notes_li}</ul>
  </section>
  <aside class="site-graph">
    <h2>关系图谱</h2>
    <div id="graph-canvas"></div>
    <p class="graph-tip">点击节点在下方列表定位 · 拖拽 · 滚轮缩放</p>
  </aside>
</main>
<script src="assets/site.js"></script>
<script>MDNotesSite.init({{count:{len(index)}}});</script>
</body></html>"""

    def import_zip(self, data_b64: str) -> dict[str, Any]:
        """从 ZIP（base64）导入笔记，安全校验后写入笔记目录。"""
        try:
            import base64
            import io
            import zipfile
            raw = base64.b64decode(data_b64)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                infos = zf.infolist()
                if len(infos) > 20000:
                    return {"success": False, "error": "文件数量过多"}
                total = sum(i.file_size for i in infos)
                if total > 500 * 1024 * 1024:
                    return {"success": False, "error": "解压体积过大"}
                for name in zf.namelist():
                    pp = Path(name)
                    # 拦截：.. 穿越、绝对路径、Windows 盘符相对（C:evil）与根相对（\evil）
                    if (
                        ".." in pp.parts
                        or pp.is_absolute()
                        or pp.drive
                        or pp.root
                        or name.startswith("/")
                        or (len(name) >= 2 and name[1] == ":")
                    ):
                        return {"success": False, "error": f"非法路径: {name}"}
                skip = notes_ops._meta_dirs() | {"__MACOSX"}
                copied = 0
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    rel = Path(name)
                    if any(p.startswith(".") for p in rel.parts):
                        continue
                    if skip.intersection(rel.parts):
                        continue
                    dest = self._notes_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
                    copied += 1
            return {"success": True, "copied": copied}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导入失败: {exc}"}

    # ── 草稿 ───────────────────────────────────────────────
    def list_drafts(self) -> list[dict[str, Any]]:
        """列出所有未保存草稿。"""
        try:
            drafts_dir = self._notes_dir / ".drafts"
            if not drafts_dir.exists():
                return []
            from urllib.parse import unquote
            out = []
            for p in drafts_dir.glob("*.draft"):
                rel = unquote(p.name[: -len(".draft")])
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:
                    content = ""
                out.append({"rel_path": rel, "size": len(content)})
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("列出草稿失败: %s", exc)
            return []

    def restore_draft(self, rel_path: str) -> dict[str, Any]:
        """读取某笔记的草稿内容。"""
        try:
            from urllib.parse import quote
            drafts_dir = self._notes_dir / ".drafts"
            p = drafts_dir / (quote(rel_path, safe="") + ".draft")
            if not p.exists():
                return {"success": False, "error": "无草稿"}
            return {"success": True,
                    "content": p.read_text(encoding="utf-8")}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"读取草稿失败: {exc}"}

    def clear_draft(self, rel_path: str) -> dict[str, Any]:
        """删除某笔记的草稿。"""
        try:
            from urllib.parse import quote
            drafts_dir = self._notes_dir / ".drafts"
            p = drafts_dir / (quote(rel_path, safe="") + ".draft")
            if p.exists():
                p.unlink()
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"清除草稿失败: {exc}"}

    # ── 新建文件夹 ───────────────────────────────────────
    def create_folder(self, parent_dir: str, name: str) -> dict[str, Any]:
        """在指定目录下新建子文件夹。"""
        name = (name or "").strip().strip("/")
        if not name:
            return {"success": False, "error": "文件夹名不能为空"}
        if not notes_ops.is_valid_filename(name) or "/" in name or "\\" in name:
            return {"success": False, "error": "文件夹名不合法"}
        try:
            base = self._safe_rel(parent_dir) if parent_dir else self._notes_dir
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        target = (base / name).resolve()
        if target.exists():
            return {"success": False, "error": "文件夹已存在"}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"创建失败: {exc}"}
        rel = self._rel(target).as_posix()
        return {"success": True, "rel_path": rel}

    def delete_folder(self, rel_path: str) -> dict[str, Any]:
        """将文件夹（含其内容）移入回收站（与原 Qt 版一致，可恢复）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_dir():
            return {"success": False, "error": "目录不存在"}
        try:
            self._run_async(notes_ops.move_to_trash, self._notes_dir, fp)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除失败: {exc}"}
        # 同步清理搜索索引中属于该目录的所有笔记
        try:
            self._run_async(search_index.remove_folder_async, self._notes_dir, rel_path)
        except Exception:  # noqa: BLE001
            pass
        return {"success": True, "rel_path": rel_path}

    def rename_folder(self, rel_path: str, new_name: str) -> dict[str, Any]:
        """重命名文件夹（new_name 不含目录，仅末级名称）。"""
        new_name = (new_name or "").strip().strip("/")
        if not new_name:
            return {"success": False, "error": "新名称不能为空"}
        if not notes_ops.is_valid_filename(new_name) or "/" in new_name or "\\" in new_name:
            return {"success": False, "error": "文件夹名不合法"}
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_dir():
            return {"success": False, "error": "目录不存在"}
        new = fp.parent / new_name
        if new.exists():
            return {"success": False, "error": "目标已存在"}
        try:
            fp.rename(new)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"重命名失败: {exc}"}
        old_rel = rel_path
        new_rel = self._rel(new).as_posix()
        return {"success": True, "old_rel_path": old_rel, "rel_path": new_rel}

    # ── 每日笔记 ─────────────────────────────────────────
    def open_daily_note(self) -> dict[str, Any]:
        """创建/打开当日笔记（notes/Daily/YYYY-MM-DD.md）。"""
        from datetime import date
        daily_dir = self._notes_dir / "Daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().strftime("%Y-%m-%d")
        target = daily_dir / f"{today}.md"
        rel = self._rel(target).as_posix()
        content = ""
        if not target.exists():
            content = (
                f"# {today}\n\n"
                f"> 每日笔记 · 创建于 {datetime.now().strftime('%H:%M:%S')}\n\n"
                "## 今日计划\n\n- \n\n## 今日记录\n\n"
            )
            try:
                target.write_text(content, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"创建失败: {exc}"}
        return {"success": True, "rel_path": rel}

    # ── 闪念笔记 ─────────────────────────────────────────
    def create_flash_note(self, text: str) -> dict[str, Any]:
        """保存一条闪念笔记到 inbox/<时间戳>.md。"""
        from datetime import datetime as _dt
        text = (text or "").strip()
        if not text:
            return {"success": False, "error": "内容为空"}
        inbox_dir = self._notes_dir / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        # 用首行前 20 字作为文件名，过滤非法字符
        first_line = text.split("\n", 1)[0][:20].strip() or "note"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in first_line)
        target = inbox_dir / f"{ts}_{safe}.md"
        try:
            target.write_text(text + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存失败: {exc}"}
        rel = self._rel(target).as_posix()
        return {"success": True, "rel_path": rel}

    # ── 最近文件 ─────────────────────────────────────────
    def list_recent_files(self, limit: int = 12) -> list[str]:
        """返回最近打开过的笔记路径（基于 .recent 记录文件）。"""
        try:
            recent_file = self._notes_dir / ".recent"
            if not recent_file.exists():
                return []
            lines = recent_file.read_text(encoding="utf-8").splitlines()
            out = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                fp = self._notes_dir / line
                if fp.is_file():
                    out.append(line)
            return out[:limit]
        except Exception as exc:  # noqa: BLE001
            log.warning("读取最近文件失败: %s", exc)
            return []

    def push_recent_file(self, rel_path: str) -> None:
        """将 rel_path 记录为最近打开（去重 + 置顶）。"""
        try:
            recent_file = self._notes_dir / ".recent"
            existing = []
            if recent_file.exists():
                existing = [l.strip() for l in recent_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            if rel_path in existing:
                existing.remove(rel_path)
            existing.insert(0, rel_path)
            recent_file.write_text("\n".join(existing[:20]) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("记录最近文件失败: %s", exc)

    # ── 收藏 ────────────────────────────────────────────
    def _favorites_file(self) -> Path:
        return self._notes_dir / ".favorites.json"

    def _read_favorites(self) -> list[str]:
        fp = self._favorites_file()
        if not fp.exists():
            return []
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def list_favorites(self) -> list[str]:
        """返回收藏的笔记路径（仅保留仍存在的文件）。"""
        out = []
        for rel in self._read_favorites():
            if (self._notes_dir / rel).is_file():
                out.append(rel)
        return out

    def is_favorite(self, rel_path: str) -> bool:
        return rel_path in self._read_favorites()

    def toggle_favorite(self, rel_path: str) -> dict[str, Any]:
        """切换收藏状态，返回 {success, favorite}。"""
        try:
            favs = self._read_favorites()
            if rel_path in favs:
                favs.remove(rel_path)
                now = False
            else:
                favs.append(rel_path)
                now = True
            self._favorites_file().write_text(json.dumps(favs, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"success": True, "favorite": now, "rel_path": rel_path}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"收藏失败: {exc}"}

    # ── 标签层级 ─────────────────────────────────────────
    def list_tags_hierarchical(self) -> list[dict[str, Any]]:
        """返回层级化标签树（#parent/child 形式）。

        返回结构：[{name, count, children:[...]}]，其中 name 为完整路径。
        """
        try:
            flat = notes_ops.list_tags(self._notes_dir)
            root = {}
            for tag, count in flat.items():
                parts = tag.split("/")
                node = root
                for i, part in enumerate(parts):
                    key = "/".join(parts[: i + 1])
                    if key not in node:
                        node[key] = {"name": key, "count": 0, "children": {}}
                    node[key]["count"] += count
                    node = node[key]["children"]
            def build(d):
                items = []
                for key in sorted(d.keys()):
                    child = d[key]
                    items.append({
                        "name": child["name"],
                        "count": child["count"],
                        "children": build(child["children"]),
                    })
                return items
            return build(root)
        except Exception as exc:  # noqa: BLE001
            log.warning("层级标签失败: %s", exc)
            return []

    def search_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """返回含有指定标签（含层级子标签）的笔记列表。"""
        try:
            results = []
            for p in self._notes_dir.rglob("*.md"):
                r = self._rel(p)
                if any(part.startswith(".") for part in r.parts):
                    continue
                if notes_ops._meta_dirs().intersection(r.parts):
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                # 匹配 #tag 或 #parent/child（tag 为父级时匹配其所有子级）
                import re
                pattern = re.compile(r"(?m)^#+\s.*$" if False else r"#" + re.escape(tag) + r"(\b|/)")
                if pattern.search(content):
                    results.append({"path": r.as_posix()})
            return results
        except Exception as exc:  # noqa: BLE001
            log.warning("按标签搜索失败: %s", exc)
            return []

    # ── 笔记别名（frontmatter aliases） ─────────────────
    @staticmethod
    def _parse_aliases(content: str) -> list[str]:
        """从 YAML frontmatter 中解析 aliases 列表。

        支持两种写法：
            aliases: [a, b]
            aliases:
              - a
              - b
        """
        import re
        # 用字符串分割提取 frontmatter（比脆弱正则更稳健）
        if not content.startswith("---"):
            return []
        parts = content.split("---", 2)
        if len(parts) < 3:
            return []
        block = parts[1]
        out: list[str] = []
        lines = block.splitlines()
        for i, line in enumerate(lines):
            lm = re.match(r"^aliases:\s*\[(.*)\]\s*$", line)
            if lm:
                # 行内数组形式
                for item in lm.group(1).split(","):
                    item = item.strip().strip("'\"")
                    if item:
                        out.append(item)
                return out
            if re.match(r"^aliases:\s*$", line):
                # 下方缩进列表形式：收集后续以 "- " 开头的列表项
                j = i + 1
                while j < len(lines):
                    item_m = re.match(r"^\s*-\s+(.+)$", lines[j])
                    if not item_m:
                        break
                    item = item_m.group(1).strip().strip("'\"")
                    if item:
                        out.append(item)
                    j += 1
                return out
        return out

    def list_note_aliases(self) -> list[dict[str, str]]:
        """返回所有笔记的 {stem: [aliases...]} 映射（供双链补全）。"""
        out = []
        try:
            for p in self._notes_dir.rglob("*.md"):
                r = self._rel(p)
                if any(part.startswith(".") for part in r.parts):
                    continue
                if notes_ops._meta_dirs().intersection(r.parts):
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                aliases = self._parse_aliases(content)
                if aliases:
                    out.append({"path": r.as_posix(), "aliases": aliases})
        except Exception as exc:  # noqa: BLE001
            log.warning("列举别名失败: %s", exc)
        return out

    def resolve_wikilink(self, target: str) -> str | None:
        """把 [[目标]] 解析为笔记相对路径；支持完整路径/文件名/别名匹配。"""
        target = (target or "").strip().lstrip("./")
        if target.lower().endswith(".md"):
            target = target[:-3]
        if not target:
            return None
        names = self.list_note_names()
        # 1) 完整相对路径精确匹配（含子目录，如 "子目录/笔记"）
        if target in names:
            return target + ".md"
        # 2) 文件名（不含目录）匹配：命中唯一时直接返回
        base = target.rsplit("/", 1)[-1]
        hits = [n for n in names if n.rsplit("/", 1)[-1] == base]
        if len(hits) == 1:
            return hits[0] + ".md"
        # 3) 别名匹配（含子目录笔记）
        for item in self.list_note_aliases():
            if target in item["aliases"]:
                return item["path"]
        return None

    # ── 附件管理器 ───────────────────────────────────────
    def list_attachments_all(self) -> list[dict[str, Any]]:
        """列出全部附件（非 .md 文件），标记是否被笔记引用（orphan）。

        返回 [{rel_path, name, size, mtime, referenced}]，按是否被引用分组，
        未引用（孤儿）排前，便于清理。
        """
        try:
            attachments = notes_ops.list_attachments(self._notes_dir)
            orphans = {o["path"] for o in notes_ops.find_orphan_attachments(self._notes_dir)}
            out = []
            for a in attachments:
                path = a["path"]
                out.append({
                    "rel_path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "size": a.get("size", 0),
                    "mtime": a.get("mtime", 0),
                    "referenced": path not in orphans,
                })
            out.sort(key=lambda x: (x["referenced"], -(x.get("mtime") or 0)))
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("列举附件失败: %s", exc)
            return []

    # ── 孤儿附件清理 ─────────────────────────────────────
    def list_orphan_attachments(self) -> list[dict[str, Any]]:
        """扫描 images/ 下未被任何笔记引用的图片。"""
        try:
            images_dir = self._notes_dir / "images"
            if not images_dir.exists():
                return []
            referenced = set()
            for p in self._notes_dir.rglob("*.md"):
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                for m in re.findall(r"(images/[^\s)\"']+)", content):
                    referenced.add(m.replace("\\", "/"))
            orphans = []
            for img in sorted(images_dir.iterdir()):
                if img.is_file() and img.name.lower() != "readme.md":
                    rel = ("images/" + img.name).replace("\\", "/")
                    if rel not in referenced:
                        orphans.append({
                            "rel_path": rel,
                            "size": img.stat().st_size,
                            "name": img.name,
                        })
            return orphans
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描孤儿附件失败: %s", exc)
            return []

    def delete_attachment(self, rel_path: str) -> dict[str, Any]:
        """删除指定附件（用于清理孤儿附件）。"""
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": "文件不存在"}
        try:
            fp.unlink()
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除失败: {exc}"}
        return {"success": True, "rel_path": rel_path}

    # ── 导入（P2-2）：文件夹 / 语雀 / Notion ───────────────
    def _import_markdown_dir(self, src_dir: Path, into_dir: str = "") -> dict[str, int]:
        """把 src_dir 下所有 .md（及图片）复制到笔记目录。

        返回 {copied, images} 计数。图片相对路径会被尽量保留。
        """
        base = self._safe_rel(into_dir) if into_dir else self._notes_dir
        copied = 0
        images = 0
        skip = notes_ops._meta_dirs() | {"__MACOSX"}
        for p in src_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if skip.intersection(rel.parts):
                continue
            dest = base / rel
            if p.suffix.lower() in (".md", ".markdown", ".txt"):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(p.read_bytes())
                copied += 1
            elif p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
                # 图片放入笔记目录 images/（去重，文件名冲突加后缀）
                images_dir = self._notes_dir / "images"
                images_dir.mkdir(parents=True, exist_ok=True)
                target = images_dir / p.name
                if target.exists():
                    target = images_dir / f"{p.stem}_{abs(hash(p.name)) & 0xffff}{p.suffix}"
                target.write_bytes(p.read_bytes())
                images += 1
        return {"copied": copied, "images": images}

    def import_from_folder(self, src_dir: str, into_dir: str = "") -> dict[str, Any]:
        """从本地文件夹导入 Markdown 笔记（含子目录与图片）。"""
        try:
            src = Path(src_dir).expanduser().resolve()
            if not src.is_dir():
                return {"success": False, "error": "源目录不存在"}
            if src.resolve() == self._notes_dir.resolve():
                return {"success": False, "error": "源目录不能是笔记目录本身"}
            stats = self._import_markdown_dir(src, into_dir)
            if stats["copied"]:
                self._reindex_all()
            return {"success": True, **stats}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导入失败: {exc}"}

    def import_yuque(self, src_dir: str) -> dict[str, Any]:
        """导入语雀导出的知识库。

        语雀导出（Markdown 或 Doc）典型结构：
          - 每个文档一个 .md 文件（文件名即标题）
          - 图片在 images/ 或 assets/ 子目录
          - 部分导出为单个大 .md（内部以 # 标题分隔多篇）
        这里支持「目录导入」与「单文件（按 H1 切分）」两种。
        """
        try:
            src = Path(src_dir).expanduser().resolve()
            if not src.exists():
                return {"success": False, "error": "源不存在"}
            # 单文件模式：是一个 .md 且内部有多个 H1
            if src.is_file() and src.suffix.lower() in (".md", ".markdown"):
                text = src.read_text(encoding="utf-8", errors="ignore")
                chunks = self._split_yuque_doc(text)
                if len(chunks) <= 1:
                    # 整篇作为单笔记导入
                    dest = self._notes_dir / (self._safe_name(src.stem) + ".md")
                    dest.write_text(text, encoding="utf-8")
                    self._reindex_all()
                    return {"success": True, "copied": 1, "images": 0, "mode": "single"}
                # 按 H1 切分为多篇
                copied = 0
                for title, body in chunks:
                    fname = self._safe_name(title) or f"yuque_{copied + 1}"
                    dest = self._notes_dir / (fname + ".md")
                    dest.write_text(f"# {title}\n\n{body}".strip() + "\n", encoding="utf-8")
                    copied += 1
                self._reindex_all()
                return {"success": True, "copied": copied, "images": 0, "mode": "split"}
            # 目录模式
            if not src.is_dir():
                return {"success": False, "error": "请传入语雀导出的文件夹或单个 .md 文件"}
            stats = self._import_markdown_dir(src)
            if stats["copied"]:
                self._reindex_all()
            return {"success": True, **stats, "mode": "dir"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导入语雀失败: {exc}"}

    def import_notion(self, src_dir: str) -> dict[str, Any]:
        """导入 Notion 导出的知识库。

        Notion 导出默认是每个页面一个 .md，图片在各自页面的
        子目录里，且会生成 CSV（数据库）。我们只导入 .md 与图片。
        """
        try:
            src = Path(src_dir).expanduser().resolve()
            if not src.is_dir():
                return {"success": False, "error": "请传入 Notion 导出的文件夹"}
            stats = self._import_markdown_dir(src)
            if stats["copied"]:
                self._reindex_all()
            return {"success": True, **stats, "mode": "dir"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"导入 Notion 失败: {exc}"}

    @staticmethod
    def _safe_name(name: str) -> str:
        """把任意标题转为安全文件名（保留中文）。"""
        name = (name or "").strip().replace("\n", " ").replace("\r", " ")
        # 去除文件系统非法字符
        bad = '<>:"/\\|?*'
        for c in bad:
            name = name.replace(c, "_")
        name = name.strip().strip(".")
        return name[:120] or "note"

    @staticmethod
    def _split_yuque_doc(text: str) -> list[tuple[str, str]]:
        """按 H1 切分单文件语雀文档为多篇文章。返回 [(标题, 正文), ...]。"""
        import re
        lines = text.splitlines()
        chunks: list[tuple[str, str]] = []
        cur_title = None
        buf: list[str] = []
        h1_re = re.compile(r"^#\s+(.+?)\s*#*\s*$")
        for line in lines:
            m = h1_re.match(line)
            if m:
                if cur_title is not None:
                    chunks.append((cur_title, "\n".join(buf).strip()))
                cur_title = m.group(1).strip()
                buf = []
            else:
                buf.append(line)
        if cur_title is not None:
            chunks.append((cur_title, "\n".join(buf).strip()))
        return chunks

    def _reindex_all(self) -> None:
        """全量重建搜索索引（导入后调用）。"""
        try:
            self._run_async(search_index.rebuild_index_async, self._notes_dir)
        except Exception:  # noqa: BLE001
            log.warning("全量重建索引失败")

    # ── 模板笔记（P3） ───────────────────────────────────
    def _templates_dir(self) -> Path:
        d = self._notes_dir / ".templates"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_templates(self) -> list[dict[str, Any]]:
        """返回模板列表 [{name, content}]。"""
        try:
            d = self._templates_dir()
            out = []
            for p in sorted(d.glob("*.md")):
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    content = ""
                out.append({"name": p.stem, "content": content})
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("列举模板失败: %s", exc)
            return []

    def save_template(self, name: str, content: str) -> dict[str, Any]:
        """保存/更新一个模板。"""
        name = (name or "").strip()
        if not name:
            return {"success": False, "error": "模板名不能为空"}
        try:
            safe = self._safe_name(name)
            p = self._templates_dir() / (safe + ".md")
            p.write_text(content or "", encoding="utf-8")
            return {"success": True, "name": safe}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"保存模板失败: {exc}"}

    def delete_template(self, name: str) -> dict[str, Any]:
        """删除一个模板。"""
        try:
            p = self._templates_dir() / (self._safe_name(name) + ".md")
            if p.exists():
                p.unlink()
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"删除模板失败: {exc}"}

    @staticmethod
    def _render_template(content: str, variables: dict[str, str]) -> str:
        """替换模板变量 {{key}}，未知变量留空。"""
        import re
        def repl(m: "re.Match") -> str:
            key = m.group(1).strip()
            return variables.get(key, "")
        return re.sub(r"\{\{\s*([\w\-]+)\s*\}\}", repl, content or "")

    def create_from_template(self, template_name: str, dest_name: str, parent_dir: str = "") -> dict[str, Any]:
        """用模板创建一篇新笔记。

        dest_name 为空时用模板名；变量 {{date}} {{time}} {{datetime}} {{year}} 自动填充。
        """
        try:
            tpl_path = self._templates_dir() / (self._safe_name(template_name) + ".md")
            if not tpl_path.exists():
                return {"success": False, "error": f"模板不存在: {template_name}"}
            content = tpl_path.read_text(encoding="utf-8")
            now = datetime.now()
            variables = {
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "year": now.strftime("%Y"),
                "month": now.strftime("%m"),
                "day": now.strftime("%d"),
            }
            rendered = self._render_template(content, variables)
            base = self._safe_rel(parent_dir) if parent_dir else self._notes_dir
            dest_name = (dest_name or template_name or "untitled").strip()
            if not dest_name.endswith(".md"):
                dest_name += ".md"
            target = (base / self._safe_name(dest_name)).with_suffix(".md").resolve()
            if target.exists():
                return {"success": False, "error": "目标笔记已存在"}
            target.write_text(rendered, encoding="utf-8")
            rel = self._rel(target).as_posix()
            return {"success": True, "rel_path": rel}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"创建失败: {exc}"}

    # ── 字数热力图（P3） ─────────────────────────────────
    def get_wordcount_stats(self, days: int = 120) -> dict[str, Any]:
        """返回写作活跃度数据：按日期聚合字数。

        返回 {days: N, daily: {YYYY-MM-DD: chars}, total: int,
              top_notes: [{rel_path, chars}], by_ext 暂略}。
        统计口径为所有 .md 当前内容的字数（轻量，不读历史）。
        """
        try:
            from collections import Counter
            daily: Counter = Counter()
            total = 0
            note_chars: list[tuple[str, int]] = []
            for p in self._notes_dir.rglob("*.md"):
                rel = self._rel(p)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if notes_ops._meta_dirs().intersection(rel.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
                chars = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))
                total += chars
                note_chars.append((rel.as_posix(), chars))
                # 用文件修改日期归到对应天
                day = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
                daily[day] += chars
            # 填充最近 N 天（无数据补 0）
            today = datetime.now().date()
            filled: dict[str, int] = {}
            for i in range(days - 1, -1, -1):
                d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                filled[d] = daily.get(d, 0)
            top = sorted(note_chars, key=lambda x: x[1], reverse=True)[:10]
            return {
                "success": True,
                "days": days,
                "daily": filled,
                "total": total,
                "top_notes": [{"rel_path": r, "chars": c} for r, c in top],
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"统计失败: {exc}"}

    # ── 主题（P3） ──────────────────────────────────────
    def list_themes(self) -> list[dict[str, str]]:
        """返回可用主题列表。"""
        return [
            {"id": "genshin", "name": "原神（默认暗色）", "mode": "dark"},
            {"id": "light", "name": "晨曦（亮色）", "mode": "light"},
            {"id": "sakura", "name": "樱粉（亮色）", "mode": "light"},
            {"id": "midnight", "name": "深蓝（暗色）", "mode": "dark"},
            {"id": "forest", "name": "森野（暗色）", "mode": "dark"},
        ]

    def set_theme(self, theme_id: str) -> dict[str, Any]:
        """切换主题（持久化到 config.theme）。"""
        valid = {t["id"] for t in self.list_themes()}
        if theme_id not in valid:
            return {"success": False, "error": f"未知主题: {theme_id}"}
        try:
            with self._lock:
                cfg_data = cfg.load_config()
                cfg_data["theme"] = theme_id
                cfg.save_config(cfg_data)
                self._config["theme"] = theme_id
            return {"success": True, "theme": theme_id}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"切换失败: {exc}"}

    # ── 图片 OCR（P3） ──────────────────────────────────
    def _find_tesseract(self) -> str | None:
        """探测系统中可用的 tesseract 可执行文件。"""
        import shutil
        candidate = shutil.which("tesseract")
        if candidate:
            return candidate
        # Windows 常见安装路径
        for p in [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]:
            if Path(p).exists():
                return p
        return None

    def ocr_image(self, rel_path: str) -> dict[str, Any]:
        """对笔记图片执行 OCR，返回识别文字。

        依赖本地 tesseract（需含中文包 chi_sim）。无 OCR 引擎时返回
        available=False 与友好提示，前端可不崩溃。
        """
        try:
            fp = self._safe_rel(rel_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not fp.is_file():
            return {"success": False, "error": "图片不存在"}
        tess = self._find_tesseract()
        if not tess:
            return {
                "success": False,
                "available": False,
                "error": "未检测到 Tesseract OCR 引擎。请安装 Tesseract-OCR 并加入 PATH（中文需 chi_sim 语言包）。",
            }
        try:
            import subprocess
            # 使用图片绝对路径；-l chi_sim+eng 支持中英文
            proc = subprocess.run(
                [tess, str(fp), "stdout", "-l", "chi_sim+eng"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                return {"success": False, "available": True, "error": f"OCR 失败: {proc.stderr[:200]}"}
            text = proc.stdout.strip()
            return {"success": True, "available": True, "text": text}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "available": True, "error": f"OCR 执行失败: {exc}"}


# ═══════════════════════════════════════════════════════════════
# 静态站点导出用的自包含前端资源（写入 <target>/assets/）
# 纯原生 JS/CSS，无构建步骤；双击 index.html 即可使用（无需服务器）。
# ═══════════════════════════════════════════════════════════════

_SITE_CSS = """
:root { --bg:#0f1620; --bg2:#16202e; --fg:#e6ebf2; --fg2:#aab4c5; --fg3:#6b7689;
  --acc:#4cc9f0; --acc2:#f6c453; --border:#2a3550; --code:#1b2433; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
a { color:var(--acc); text-decoration:none; }
a:hover { text-decoration:underline; }
.site-head { padding:28px 32px 18px; border-bottom:1px solid var(--border); }
.site-head h1 { margin:0 0 14px; font-size:22px; }
#site-search { width:100%; max-width:560px; padding:11px 14px; border-radius:9px;
  border:1px solid var(--border); background:var(--bg2); color:var(--fg); font-size:15px; }
#site-search:focus { outline:none; border-color:var(--acc); box-shadow:0 0 0 2px rgba(76,201,240,.18); }
.site-main { display:grid; grid-template-columns: 1fr 420px; gap:24px; padding:24px 32px; }
@media (max-width:860px){ .site-main { grid-template-columns:1fr; } }
.site-list h2, .site-graph h2 { font-size:14px; color:var(--fg2); margin:0 0 12px; }
#note-list { list-style:none; margin:0; padding:0; }
#note-list li { padding:9px 12px; border:1px solid var(--border); border-radius:9px;
  margin-bottom:8px; background:var(--bg2); transition:border-color .15s; }
#note-list li:hover { border-color:var(--acc); }
.li-path { display:block; font-size:11px; color:var(--fg3); margin-top:3px; }
.site-graph { position:sticky; top:24px; }
#graph-canvas { width:100%; height:360px; background:
  radial-gradient(circle at 30% 20%, rgba(76,201,240,.06), transparent 60%),
  radial-gradient(circle at 70% 80%, rgba(246,196,83,.05), transparent 55%), var(--bg2);
  border:1px solid var(--border); border-radius:12px; }
.graph-tip { font-size:11px; color:var(--fg3); text-align:center; margin-top:8px; }
.note-page { max-width:820px; margin:0 auto; padding:32px 24px 60px; }
.np-head { display:flex; justify-content:space-between; align-items:center;
  border-bottom:1px solid var(--border); padding-bottom:14px; margin-bottom:22px; font-size:13px; }
.np-path { color:var(--fg3); }
.np-title { font-size:28px; margin:0 0 10px; }
.np-tags .tag { display:inline-block; background:var(--code); color:var(--acc2);
  border-radius:20px; padding:2px 10px; font-size:12px; margin:0 6px 6px 0; }
.np-foot { margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
  color:var(--fg3); font-size:12px; text-align:center; }
.markdown-body { line-height:1.75; font-size:15px; }
.markdown-body h1,.markdown-body h2,.markdown-body h3 { margin:1.4em 0 .6em; }
.markdown-body pre { background:var(--code); padding:14px 16px; border-radius:10px;
  overflow:auto; border:1px solid var(--border); }
.markdown-body code { background:var(--code); padding:2px 6px; border-radius:5px; font-size:13px; }
.markdown-body pre code { background:none; padding:0; }
.markdown-body blockquote { border-left:3px solid var(--acc); margin:1em 0; padding:.2em 1em;
  color:var(--fg2); background:var(--bg2); border-radius:0 8px 8px 0; }
.markdown-body table { border-collapse:collapse; width:100%; margin:1em 0; }
.markdown-body th,.markdown-body td { border:1px solid var(--border); padding:8px 12px; }
.markdown-body img { max-width:100%; border-radius:8px; }
.markdown-body input[type=checkbox] { margin-right:6px; }
.wikilink-missing { color:#e57373; border-bottom:1px dotted #e57373; }
.empty-hint { color:var(--fg3); padding:20px; text-align:center; }
"""

_SITE_JS = """
window.MDNotesSite = {
  index: [],
  init: function(opts) {
    opts = opts || {};
    fetch('assets/search-index.json').then(function(r){return r.json();}).then(function(d){
      MDNotesSite.index = d || [];
      MDNotesSite._bindSearch();
      MDNotesSite._renderList(MDNotesSite.index);
    }).catch(function(e){ console.warn('load index failed', e); });
    fetch('assets/graph.json').then(function(r){return r.json();}).then(function(g){
      MDNotesSite._renderGraph(g);
    }).catch(function(e){ console.warn('load graph failed', e); });
  },
  _bindSearch: function() {
    var box = document.getElementById('site-search');
    if (!box) return;
    box.addEventListener('input', function() {
      var q = box.value.trim().toLowerCase();
      if (!q) { MDNotesSite._renderList(MDNotesSite.index); return; }
      var parts = q.split(/\\s+/);
      var res = MDNotesSite.index.filter(function(n){
        var hay = (n.title + ' ' + n.text + ' ' + (n.tags||[]).join(' ')).toLowerCase();
        // 每个词都要命中（多标签交集语义）
        return parts.every(function(p){ return hay.indexOf(p) >= 0; });
      });
      MDNotesSite._renderList(res);
    });
  },
  _renderList: function(list) {
    var ul = document.getElementById('note-list');
    if (!ul) return;
    if (!list.length) { ul.innerHTML = '<li class="empty-hint">无匹配笔记</li>'; return; }
    ul.innerHTML = list.map(function(n){
      return '<li><a href="' + encodeURI(n.path) + '">' + MDNotesSite._esc(n.title) +
        '</a><span class="li-path">' + MDNotesSite._esc(n.rel) + '</span></li>';
    }).join('');
  },
  _renderGraph: function(g) {
    var canvas = document.getElementById('graph-canvas');
    if (!canvas) return;
    if (typeof cytoscape === 'undefined') {
      canvas.innerHTML = '<div class="empty-hint">图谱库需联网加载（Cytoscape.js CDN）</div>';
      return;
    }
    var eles = [];
    (g.nodes||[]).forEach(function(n){
      eles.push({ data: { id:n.id, label:n.title||n.path },
        classes: (n.tags&&n.tags.length)?'tagged':'' });
    });
    (g.edges||[]).forEach(function(e){
      eles.push({ data: { id:'e_'+e.source+'_'+e.target, source:e.source, target:e.target } });
    });
    if (!eles.length) { canvas.innerHTML = '<div class="empty-hint">暂无双链关系</div>'; return; }
    var cy = cytoscape({
      container: canvas, elements: eles,
      style: [
        { selector:'node', style:{ 'background-color':'#4cc9f0','label':'data(label)',
          'color':'#e6ebf2','font-size':'9px','text-valign':'bottom','text-margin-y':3,
          'text-wrap':'wrap','text-max-width':'80px',
          'width':'mapData(degree,0,12,16,42)','height':'mapData(degree,0,12,16,42)',
          'border-width':2,'border-color':'#0f1620' } },
        { selector:'node.tagged', style:{ 'background-color':'#f6c453' } },
        { selector:'edge', style:{ 'width':1.4,'line-color':'#33415c',
          'target-arrow-shape':'triangle','target-arrow-color':'#33415c','opacity':.7,'curve-style':'bezier' } }
      ],
      layout: { name:'cose', animate:true, animationDuration:500, nodeRepulsion:8000, idealEdgeLength:80, padding:20 }
    });
    cy.on('tap','node', function(evt){
      var id = evt.target.id();          // 形如 foo/Bar.md
      var href = 'notes/' + id.replace(/\\.md$/, '') + '.html';
      window.open(href, '_blank');
    });
  },
  _esc: function(s){ var d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }
};
"""
