import asyncio
import difflib
import json
import mimetypes
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests

from . import config as cfg
from . import notes_ops


SKIP_DIRS = notes_ops._meta_dirs() | {".attachments"}


# 文本文件扩展名，支持合并
_TEXT_EXTENSIONS = {".md", ".txt", ".markdown"}


def _state_path() -> Path:
    root = cfg.get_project_root()
    state_dir = root / ".mdnotes"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "webdav_state.json"


def _load_state() -> dict[str, float]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.items()}
    except Exception:
        return {}


def _save_state(state: dict[str, float]) -> None:
    path = _state_path()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class WebDAVClient:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        timeout: int = 30,
    ):
        self.base_url = url.rstrip("/")
        parsed = urlparse(self.base_url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("WebDAV URL 格式不正确，需要 http(s)://host")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers["User-Agent"] = "MDNotes-WebDAV/0.1"

    def _encode_path(self, path: str) -> str:
        # 先解码再编码，避免对已经 URL 编码的路径（如 123 网盘返回的 href）二次编码
        return "/".join(quote(unquote(segment), safe="") for segment in path.split("/"))

    def _full_url(self, path: str) -> str:
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + self._encode_path(path)

    def _normalize_dir(self, path: str) -> str:
        path = path.replace("\\", "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        return path.rstrip("/")

    def request(self, method: str, path: str, **kwargs):
        url = self._full_url(path)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, **kwargs)

    def propfind(self, path: str, depth: str = "1"):
        resp = self.request(
            "PROPFIND",
            path,
            headers={"Depth": depth, "Content-Type": "text/xml; charset=utf-8"},
            data='<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
        )
        return resp.status_code, resp

    def mkcol(self, path: str):
        resp = self.request("MKCOL", path)
        return resp.status_code

    def ensure_collection(self, path: str) -> bool:
        path = self._normalize_dir(path)
        if path in ("", "/"):
            return True
        code, _ = self.propfind(path, depth="0")
        if code == 207:
            return True
        if code == 404:
            parent = str(PurePosixPath(path).parent)
            if parent and parent != path:
                self.ensure_collection(parent)
            code2 = self.mkcol(path)
            return code2 in (201, 204, 200)
        return False

    def upload_file(self, local_path: Path, remote_path: str) -> dict[str, str | bool]:
        local_path = Path(local_path)
        if not local_path.is_file():
            return {"success": False, "error": f"本地文件不存在: {local_path}"}
        remote_path = self._normalize_dir(remote_path)
        parent = str(PurePosixPath(remote_path).parent)
        if parent and parent != "/" and not self.ensure_collection(parent):
            return {"success": False, "error": f"无法创建远程目录: {parent}"}

        content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        try:
            with open(local_path, "rb") as f:
                resp = self.request(
                    "PUT",
                    remote_path,
                    data=f,
                    headers={"Content-Type": content_type},
                )
        except Exception as exc:
            return {"success": False, "error": f"上传失败: {exc}"}
        if resp.status_code in (201, 204, 200):
            return {"success": True, "message": f"已上传 {remote_path}"}
        return {"success": False, "error": f"上传失败 HTTP {resp.status_code}: {resp.text[:200]}"}

    def sync_dir(
        self,
        local_dir: str | Path,
        remote_dir: str,
        incremental: bool = True,
    ) -> dict[str, Any]:
        local_dir = Path(local_dir)
        remote_dir = self._normalize_dir(remote_dir)
        if not self.ensure_collection(remote_dir):
            return {"success": False, "error": f"无法创建远程目录: {remote_dir}"}

        state = _load_state() if incremental else {}
        results = []
        uploaded = 0
        skipped = 0
        failed = 0
        for file_path in local_dir.rglob("*"):
            if not file_path.is_file():
                continue
            rel_path = file_path.relative_to(local_dir)
            parts = rel_path.parts
            if any(part.startswith(".") for part in parts):
                continue
            if SKIP_DIRS.intersection(parts):
                continue
            rel = rel_path.as_posix()
            mtime = file_path.stat().st_mtime
            if incremental and rel in state and state[rel] == mtime:
                skipped += 1
                continue
            target = f"{remote_dir}/{rel}"
            res = self.upload_file(file_path, target)
            results.append(res)
            if res.get("success"):
                uploaded += 1
                state[rel] = mtime
            else:
                failed += 1

        _save_state(state)

        return {
            "success": failed == 0,
            "uploaded": uploaded,
            "skipped": skipped,
            "failed": failed,
            "details": results,
        }

    def download_file(self, remote_path: str, local_path: Path) -> dict[str, str | bool]:
        """从 WebDAV 下载单个文件到本地。"""
        local_path = Path(local_path)
        remote_path = self._normalize_dir(remote_path)
        try:
            resp = self.request("GET", remote_path, stream=True)
        except Exception as exc:
            return {"success": False, "error": f"下载失败: {exc}"}
        if resp.status_code != 200:
            return {"success": False, "error": f"下载失败 HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as exc:
            return {"success": False, "error": f"写入本地文件失败: {exc}"}
        return {"success": True, "message": f"已下载 {remote_path}"}

    def _read_remote_text(self, remote_path: str) -> str | None:
        """读取远程文本文件内容，失败返回 None。"""
        try:
            resp = self.request("GET", self._normalize_dir(remote_path))
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return None

    def preview_sync_from_webdav(
        self,
        local_dir: str | Path,
        remote_dir: str,
    ) -> dict[str, Any]:
        """预检从 WebDAV 恢复到本地会产生哪些差异，不实际写入。"""
        local_dir = Path(local_dir)
        remote_dir = self._normalize_dir(remote_dir)

        try:
            remote_files = self.list_remote_files(remote_dir)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        conflicts = []
        remote_paths = set()
        for item in remote_files:
            rel = item["path"]
            remote_paths.add(rel)
            remote_path = f"{remote_dir}/{rel}"
            local_path = local_dir / rel

            if ".." in Path(rel).parts:
                continue
            try:
                local_path.resolve().relative_to(local_dir.resolve())
            except ValueError:
                continue

            local_exists = local_path.is_file()
            local_mtime = local_path.stat().st_mtime if local_exists else None
            local_size = local_path.stat().st_size if local_exists else 0
            remote_size = item.get("size", 0)

            if not local_exists:
                status = "added"
            elif local_size != remote_size:
                status = "modified"
            else:
                # 大小相同不一定无差异（同尺寸不同内容）。远程 mtime 明显更新时
                # 也视为有修改；2 秒容差兼容 FAT/WebDAV 服务器的时间精度。
                remote_mtime = item.get("mtime")
                if remote_mtime is not None and local_mtime is not None and remote_mtime > local_mtime + 2:
                    status = "modified"
                else:
                    continue

            conflicts.append({
                "path": rel,
                "status": status,
                "local_mtime": local_mtime,
                "local_size": local_size,
                "remote_size": remote_size,
                "remote_mtime": item.get("mtime"),
                "mergeable": Path(rel).suffix.lower() in _TEXT_EXTENSIONS,
            })

        # 检查本地有但远程没有的文件（删除）
        for local_path in local_dir.rglob("*"):
            if not local_path.is_file():
                continue
            rel = local_path.relative_to(local_dir).as_posix()
            parts = Path(rel).parts
            if any(part.startswith(".") for part in parts):
                continue
            if SKIP_DIRS.intersection(parts):
                continue
            if rel in remote_paths:
                continue
            conflicts.append({
                "path": rel,
                "status": "deleted",
                "local_mtime": local_path.stat().st_mtime,
                "local_size": local_path.stat().st_size,
                "remote_size": 0,
                "mergeable": False,
            })

        return {
            "success": True,
            "conflicts": conflicts,
        }

    def resolve_sync_from_webdav(
        self,
        local_dir: str | Path,
        remote_dir: str,
        resolutions: dict[str, str],
    ) -> dict[str, Any]:
        """根据用户决策执行 WebDAV 恢复/合并。resolutions: {rel_path: 'local'|'remote'|'merge'}"""
        local_dir = Path(local_dir)
        remote_dir = self._normalize_dir(remote_dir)
        downloaded = 0
        kept = 0
        merged = 0
        deleted = 0
        failed = 0
        details = []

        for rel, decision in resolutions.items():
            local_path = local_dir / rel
            remote_path = f"{remote_dir}/{rel}"

            if ".." in Path(rel).parts:
                failed += 1
                details.append({"path": rel, "success": False, "error": "非法路径"})
                continue
            try:
                local_path.resolve().relative_to(local_dir.resolve())
            except ValueError:
                failed += 1
                details.append({"path": rel, "success": False, "error": "路径越界"})
                continue

            if decision == "local":
                kept += 1
                details.append({"path": rel, "success": True, "action": "kept"})
                continue

            if decision == "remote":
                res = self.download_file(remote_path, local_path)
                if res.get("success"):
                    downloaded += 1
                else:
                    failed += 1
                details.append({"path": rel, **res, "action": "downloaded"})
                continue

            if decision == "merge":
                if not local_path.is_file():
                    # 本地不存在时合并退化为下载
                    res = self.download_file(remote_path, local_path)
                    if res.get("success"):
                        downloaded += 1
                    else:
                        failed += 1
                    details.append({"path": rel, **res, "action": "downloaded"})
                    continue

                remote_text = self._read_remote_text(remote_path)
                if remote_text is None:
                    failed += 1
                    details.append({"path": rel, "success": False, "error": "读取远程文件失败"})
                    continue

                try:
                    local_text = local_path.read_text(encoding="utf-8")
                except Exception as exc:
                    failed += 1
                    details.append({"path": rel, "success": False, "error": f"读取本地文件失败: {exc}"})
                    continue

                merged_text = merge_text_files(local_text, remote_text, rel)
                try:
                    local_path.write_text(merged_text, encoding="utf-8")
                    merged += 1
                    details.append({"path": rel, "success": True, "action": "merged"})
                except Exception as exc:
                    failed += 1
                    details.append({"path": rel, "success": False, "error": f"写入合并文件失败: {exc}"})
                continue

            if decision == "delete":
                try:
                    if local_path.is_file():
                        local_path.unlink()
                    deleted += 1
                    details.append({"path": rel, "success": True, "action": "deleted"})
                except Exception as exc:
                    failed += 1
                    details.append({"path": rel, "success": False, "error": f"删除失败: {exc}"})
                continue

            failed += 1
            details.append({"path": rel, "success": False, "error": f"未知决策: {decision}"})

        return {
            "success": failed == 0,
            "downloaded": downloaded,
            "kept": kept,
            "merged": merged,
            "deleted": deleted,
            "failed": failed,
            "details": details,
        }

    def list_remote_files(self, remote_dir: str) -> list[dict[str, Any]]:
        """递归列出远程目录下的所有文件（非目录）。

        使用 Depth: 1 迭代遍历，而不是 Depth: infinity——后者很多 WebDAV
        服务器（如果壳云、Nutstore 等）不支持，会导致请求挂起或超时。
        """
        remote_dir = self._normalize_dir(remote_dir)
        request_url = self._full_url(remote_dir)
        base_path = unquote(urlparse(request_url).path)
        if not base_path.endswith("/"):
            base_path += "/"

        body = '''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>'''

        ns = {"d": "DAV:"}
        files: list[dict[str, Any]] = []
        # 待扫描目录队列，元素为相对于 remote_dir 的路径（空字符串表示根目录）
        dirs_to_scan = [""]

        while dirs_to_scan:
            rel_dir = dirs_to_scan.pop(0)
            current_dir = remote_dir if not rel_dir else f"{remote_dir}/{rel_dir}"
            try:
                resp = self.request(
                    "PROPFIND",
                    current_dir,
                    headers={"Depth": "1", "Content-Type": "text/xml; charset=utf-8"},
                    data=body,
                )
            except Exception as exc:
                raise Exception(f"PROPFIND 请求失败 ({current_dir}): {exc}") from exc

            if resp.status_code == 404:
                continue
            if resp.status_code != 207:
                raise Exception(f"PROPFIND 失败 HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError as exc:
                raise Exception(f"解析 WebDAV 响应失败: {exc}") from exc

            for response in root.findall("d:response", ns):
                href_el = response.find("d:href", ns)
                if href_el is None or not href_el.text:
                    continue
                href = unquote(href_el.text.strip())
                # 部分服务器（如 Nextcloud 反向代理部署）PROPFIND 返回完整 URL，
                # 统一取 path 部分再与 base_path 匹配
                href_path = urlparse(href).path or href

                if not href_path.startswith(base_path):
                    continue
                rel = href_path[len(base_path):].lstrip("/")
                if not rel:
                    # 当前目录本身
                    continue
                if rel.rstrip("/") == rel_dir.rstrip("/"):
                    continue

                resourcetype = response.find(".//d:resourcetype", ns)
                is_collection = resourcetype is not None and resourcetype.find("d:collection", ns) is not None

                if is_collection:
                    dirs_to_scan.append(rel.rstrip("/"))
                    continue

                rel_path = Path(rel)
                if any(part.startswith(".") for part in rel_path.parts):
                    continue
                if SKIP_DIRS.intersection(rel_path.parts):
                    continue
                if rel_path.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                    continue

                size = 0
                size_el = response.find(".//d:getcontentlength", ns)
                if size_el is not None and size_el.text:
                    try:
                        size = int(size_el.text)
                    except ValueError:
                        pass

                mtime: float | None = None
                mtime_el = response.find(".//d:getlastmodified", ns)
                if mtime_el is not None and mtime_el.text:
                    try:
                        mtime = parsedate_to_datetime(mtime_el.text).timestamp()
                    except Exception:
                        pass

                files.append({"path": rel, "size": size, "mtime": mtime})

        return files

    def sync_from_webdav(
        self,
        local_dir: str | Path,
        remote_dir: str,
        incremental: bool = True,
    ) -> dict[str, Any]:
        """从 WebDAV 下载文件到本地，用于恢复备份。"""
        local_dir = Path(local_dir)
        remote_dir = self._normalize_dir(remote_dir)

        try:
            remote_files = self.list_remote_files(remote_dir)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        downloaded = 0
        skipped = 0
        failed = 0
        details = []

        for item in remote_files:
            rel = item["path"]
            remote_path = f"{remote_dir}/{rel}"
            local_path = local_dir / rel

            if ".." in Path(rel).parts:
                failed += 1
                details.append({"success": False, "error": f"非法路径: {rel}"})
                continue
            try:
                local_path.resolve().relative_to(local_dir.resolve())
            except ValueError:
                failed += 1
                details.append({"success": False, "error": f"路径越界: {rel}"})
                continue

            if incremental and local_path.exists():
                local_mtime = local_path.stat().st_mtime
                remote_mtime = item.get("mtime")
                if remote_mtime is not None and local_mtime >= remote_mtime:
                    skipped += 1
                    continue
                local_size = local_path.stat().st_size
                remote_size = item.get("size", 0)
                if local_size == remote_size and remote_size > 0 and remote_mtime is None:
                    skipped += 1
                    continue

            res = self.download_file(remote_path, local_path)
            details.append(res)
            if res.get("success"):
                downloaded += 1
            else:
                failed += 1

        return {
            "success": failed == 0,
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "message": f"已下载 {downloaded} 个文件，跳过 {skipped} 个，失败 {failed} 个",
            "details": details,
        }


async def sync_to_webdav(
    local_dir: str | Path,
    url: str,
    username: str,
    password: str,
    remote_dir: str = "/mdnotes",
    incremental: bool = True,
) -> dict[str, Any]:
    """在线程中执行 WebDAV 同步。"""
    def _run():
        client = WebDAVClient(url, username, password)
        return client.sync_dir(local_dir, remote_dir, incremental=incremental)

    return await asyncio.to_thread(_run)


async def sync_from_webdav(
    local_dir: str | Path,
    url: str,
    username: str,
    password: str,
    remote_dir: str = "/mdnotes",
    incremental: bool = True,
) -> dict[str, Any]:
    """在线程中执行 WebDAV 下载恢复。"""
    def _run():
        client = WebDAVClient(url, username, password)
        return client.sync_from_webdav(local_dir, remote_dir, incremental=incremental)

    return await asyncio.to_thread(_run)


async def preview_sync_from_webdav(
    local_dir: str | Path,
    url: str,
    username: str,
    password: str,
    remote_dir: str = "/mdnotes",
) -> dict[str, Any]:
    """在线程中预检 WebDAV 恢复到本地的差异。"""
    def _run():
        client = WebDAVClient(url, username, password)
        return client.preview_sync_from_webdav(local_dir, remote_dir)

    return await asyncio.to_thread(_run)


async def resolve_sync_from_webdav(
    local_dir: str | Path,
    url: str,
    username: str,
    password: str,
    remote_dir: str,
    resolutions: dict[str, str],
) -> dict[str, Any]:
    """在线程中根据用户决策执行 WebDAV 恢复/合并。"""
    def _run():
        client = WebDAVClient(url, username, password)
        return client.resolve_sync_from_webdav(local_dir, remote_dir, resolutions)

    return await asyncio.to_thread(_run)


def merge_text_files(local_text: str, remote_text: str, path: str = "") -> str:
    """对两个文本做简单行级合并，冲突处插入标记。"""
    if local_text == remote_text:
        return local_text

    local_lines = local_text.splitlines(keepends=True) or [""]
    remote_lines = remote_text.splitlines(keepends=True) or [""]
    sm = difflib.SequenceMatcher(None, local_lines, remote_lines)
    result: list[str] = []
    has_conflict = False

    def _ensure_newline():
        if result and not result[-1].endswith("\n"):
            result[-1] += "\n"

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            result.extend(local_lines[i1:i2])
        elif tag == "replace":
            has_conflict = True
            _ensure_newline()
            result.append(f"<<<<<<< 本地 ({path})\n")
            result.extend(local_lines[i1:i2])
            _ensure_newline()
            result.append("=======\n")
            result.extend(remote_lines[j1:j2])
            _ensure_newline()
            result.append(">>>>>>> 远程\n")
        elif tag == "delete":
            has_conflict = True
            _ensure_newline()
            result.append(f"<<<<<<< 本地 ({path})\n")
            result.extend(local_lines[i1:i2])
            _ensure_newline()
            result.append("=======\n")
            result.append(">>>>>>> 远程\n")
        elif tag == "insert":
            has_conflict = True
            _ensure_newline()
            result.append(f"<<<<<<< 本地 ({path})\n")
            result.append("=======\n")
            result.extend(remote_lines[j1:j2])
            _ensure_newline()
            result.append(">>>>>>> 远程\n")

    merged = "".join(result)
    if has_conflict:
        merged += "\n\n<!-- 冲突标记说明：以上内容包含本地与远程的冲突，请手动编辑后删除标记行 -->\n"
    return merged
