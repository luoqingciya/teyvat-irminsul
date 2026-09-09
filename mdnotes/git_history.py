"""基于本地 git 仓库的历史版本实现（无远程）。

替代原先的 .history 目录文件快照方案：
每次保存前把当前（旧）内容提交进笔记目录的本地 git 仓库作为一条版本，
版本不再受数量限制，可用 git 按提交精确回滚、查看差异。

约定：
- 仓库就是笔记目录本身（.git 位于 notes_dir/ 下），.gitignore 已排除
  .trash/.history/.drafts/.attachments 等元数据目录，只跟踪用户内容。
- 每次只 add + commit 某一篇被保存的笔记，一条提交即一个历史版本。
- 需要本机安装 git；不可用时历史版本自动降级为空（不影响保存）。
"""

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 最小快照间隔：5 分钟内不重复为同一篇笔记建档，避免自动保存把版本刷爆
MIN_GAP_SECONDS = 300

# 合法提交号：短哈希（7-40 位十六进制）
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def git_available() -> bool:
    return shutil.which("git") is not None


def _git_run(cwd: Path, *args: str) -> tuple[int, str, str]:
    """在 cwd 下执行 git 命令，返回 (exit_code, stdout, stderr)。

    Windows 上用 CREATE_NO_WINDOW 隐藏子进程控制台，避免打包成 exe 后闪黑框；
    关闭交互式凭据/密码提示，避免无 TTY 环境下卡住。
    """
    git = shutil.which("git")
    if not git:
        return 1, "", "未找到 git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [git, "-c", "credential.helper=", "-c", "core.quotePath=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def ensure_repo(notes_dir: Path) -> bool:
    """确保笔记目录已初始化为 git 仓库，并配置好用户信息。失败返回 False。"""
    if not (notes_dir / ".git").is_dir():
        code, _, _ = _git_run(notes_dir, "init", "-b", "main")
        if code != 0:
            return False
    _git_run(notes_dir, "config", "user.email", "mdnotes@local")
    _git_run(notes_dir, "config", "user.name", "MDNotes")
    return True


def _last_commit_time(notes_dir: Path, rel: str) -> int | None:
    """返回 <rel> 最近一次提交的作者时间（unix 秒）；从未提交返回 None。"""
    code, out, _ = _git_run(notes_dir, "log", "-1", "--format=%at", "--", rel)
    if code != 0 or not out.strip():
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def commit_pre_save(notes_dir: Path, rel: str, force: bool = False) -> None:
    """保存前为当前（旧）内容建档：提交到本地 git 仓库。

    force=True 时跳过最小间隔检查（用于「恢复前建档」等必须保留现场的场景）。
    git 不可用或提交失败时静默降级，不影响保存流程。
    """
    if not git_available():
        return
    try:
        if not ensure_repo(notes_dir):
            return
        if not force:
            last = _last_commit_time(notes_dir, rel)
            if last is not None and (time.time() - last) < MIN_GAP_SECONDS:
                return
        code, _, _ = _git_run(notes_dir, "add", "--", rel)
        if code != 0:
            return
        _git_run(notes_dir, "commit", "-m", f"版本 {rel}")
    except Exception:  # noqa: BLE001
        return


def list_versions(notes_dir: Path, rel: str) -> list[dict[str, Any]]:
    """列出 <rel> 的历史版本（新→旧），无法使用时返回空列表。"""
    if not git_available():
        return []
    ensure_repo(notes_dir)
    code, out, _ = _git_run(
        notes_dir, "log", "--format=%H%x09%at", "--", rel
    )
    if code != 0 or not out.strip():
        return []
    versions: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        commit, at = parts[0], parts[1]
        try:
            ts = int(at)
            human = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            ts, human = 0, at
        versions.append({
            "timestamp": commit,
            "commit": commit,
            "mtime": ts,
            "time": human,
        })
    return versions


def read_version(notes_dir: Path, rel: str, commit: str) -> dict[str, Any]:
    """读取 <rel> 指定提交（版本）的内容。"""
    if not _COMMIT_RE.match(commit or ""):
        return {"success": False, "error": "非法版本号"}
    code, out, err = _git_run(notes_dir, "show", f"{commit}:{rel}")
    if code != 0:
        return {"success": False, "error": "版本不存在"}
    return {"success": True, "content": out, "timestamp": commit}


def restore_version(notes_dir: Path, rel: str, commit: str) -> dict[str, Any]:
    """将 <rel> 的指定版本还原到原笔记路径。

    覆盖前先为当前版本强制建档（跳 5 分钟间隔），避免恢复后无法回退。
    """
    if not _COMMIT_RE.match(commit or ""):
        return {"success": False, "error": "非法版本号"}
    code, out, _ = _git_run(notes_dir, "show", f"{commit}:{rel}")
    if code != 0:
        return {"success": False, "error": "版本不存在"}
    dest = notes_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 覆盖前先为当前版本强制建档
    commit_pre_save(notes_dir, rel, force=True)
    try:
        dest.write_text(out, encoding="utf-8")
    except OSError as exc:
        return {"success": False, "error": f"写入失败: {exc}"}
    return {"success": True, "path": rel}