"""基于 SQLite 的本地全文搜索索引。

优先使用 FTS5；当前环境不支持时回退到普通表 + LIKE 查询。
索引文件位于运行目录 .mdnotes/search.db。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import config as cfg

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
# CJK 单字正则：用于把连续汉字拆成单字空格，让 FTS5 unicode61 能按字索引（中文子串搜索）
_CJK = re.compile(r"([\u4e00-\u9fff])")

# FTS5 支持探测结果缓存（避免每次搜索都 CREATE/DROP 临时表）
_fts5_support: bool | None = None


def _space_cjk(text: str) -> str:
    """在每个汉字两侧加空格，使 unicode61 将连续汉字拆为单字 token。

    例如「今天天气」→「今 天 天 气」，配合查询词同样拆字，
    中文子串即可被 FTS5 短语匹配命中。
    """
    if not text:
        return ""
    return _CJK.sub(r" \1 ", text)


def _join_cjk(text: str) -> str:
    """把单个汉字之间的空格合并回去（摘要显示还原为正常中文）。"""
    if not text:
        return text
    return re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)


def _index_path() -> Path:
    root = cfg.get_project_root()
    meta = root / ".mdnotes"
    meta.mkdir(parents=True, exist_ok=True)
    return meta / "search.db"


def _connect() -> sqlite3.Connection:
    """建立 SQLite 连接并启用 WAL 模式以提升并发性能。"""
    db = _index_path()
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _has_fts5(conn: sqlite3.Connection) -> bool:
    global _fts5_support
    if _fts5_support is not None:
        return _fts5_support
    try:
        conn.execute("CREATE VIRTUAL TABLE __fts5_test USING fts5(a)")
        conn.execute("DROP TABLE __fts5_test")
        _fts5_support = True
    except sqlite3.OperationalError:
        _fts5_support = False
    return _fts5_support


def init_index(notes_dir: Path) -> None:
    """初始化索引数据库。如果表结构已存在则跳过创建。"""
    conn = _connect()
    try:
        if _has_fts5(conn):
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
                    path UNINDEXED,
                    title,
                    content,
                    mtime UNINDEXED,
                    size UNINDEXED,
                    tokenize='porter unicode61'
                )
                """
            )
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, title TEXT, content TEXT, mtime REAL, size INTEGER)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_content ON notes(content)")
        conn.commit()
    finally:
        conn.close()


async def init_index_async(notes_dir: Path) -> None:
    await asyncio.to_thread(init_index, notes_dir)


def _title_from_path(path: str) -> str:
    name = path.split("/")[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name


def _extract_wikilinks(content: str) -> str:
    """把 [[目标]] 展开为空格分隔文本，便于通过目标标题搜索到关联笔记。"""
    return " ".join(_WIKILINK.findall(content))


def index_note(notes_dir: Path, rel_path: str, content: str, mtime: float, size: int) -> None:
    """索引或更新单条笔记。"""
    init_index(notes_dir)  # 确保表存在（全新安装首次保存时）
    conn = _connect()
    try:
        title = _title_from_path(rel_path)
        extra = _extract_wikilinks(content)
        full = _space_cjk(f"{title}\n{content}\n{extra}")
        # FTS5 不支持主键约束，REPLACE INTO 会产生重复行，因此先删除旧记录
        conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
        conn.execute(
            "REPLACE INTO notes (path, title, content, mtime, size) VALUES (?, ?, ?, ?, ?)",
            (rel_path, title, full, mtime, size),
        )
        conn.commit()
    finally:
        conn.close()


async def index_note_async(notes_dir: Path, rel_path: str, content: str, mtime: float, size: int) -> None:
    await asyncio.to_thread(index_note, notes_dir, rel_path, content, mtime, size)


def remove_note(notes_dir: Path, rel_path: str) -> None:
    """从索引中删除笔记（删除/移入回收站时调用）。"""
    init_index(notes_dir)
    conn = _connect()
    try:
        conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
        conn.commit()
    finally:
        conn.close()


async def remove_note_async(notes_dir: Path, rel_path: str) -> None:
    await asyncio.to_thread(remove_note, notes_dir, rel_path)


def remove_folder(notes_dir: Path, rel_path: str) -> None:
    """从索引中删除某个文件夹下的全部笔记（删除文件夹时调用）。"""
    init_index(notes_dir)
    prefix = rel_path.rstrip("/") + "/"
    conn = _connect()
    try:
        conn.execute("DELETE FROM notes WHERE path = ? OR path LIKE ?", (rel_path, prefix + "%"))
        conn.commit()
    finally:
        conn.close()


async def remove_folder_async(notes_dir: Path, rel_path: str) -> None:
    await asyncio.to_thread(remove_folder, notes_dir, rel_path)


def _is_fts5(notes_dir: Path) -> bool:
    conn = _connect()
    try:
        return _has_fts5(conn)
    finally:
        conn.close()


def _snippet(text: str, query_terms: list[str], radius: int = 60) -> str:
    text = _join_cjk(text or "")  # 索引里是拆字存储，先还原为正常中文再定位摘要
    lower = text.lower()
    best = -1
    for term in query_terms:
        idx = lower.find(term)
        if idx != -1:
            best = idx
            break
    if best == -1:
        best = 0
    start = max(0, best - radius)
    end = min(len(text), best + max(len(t) for t in query_terms) + radius)
    snippet = text[start:end].replace("\n", " ")
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def search(notes_dir: Path, query: str) -> list[dict[str, Any]]:
    """搜索笔记，返回带摘要的结果列表。"""
    if not query.strip():
        return []
    init_index(notes_dir)  # 确保索引表存在（全新安装首次搜索）
    conn = _connect()
    try:
        terms = [t.strip().lower() for t in query.split() if t.strip()]
        if not terms:
            return []
        # 中文查询词拆成单字（与索引侧 _space_cjk 对应），保证子串可命中
        space_terms = [_space_cjk(t).strip() for t in terms]
        if _has_fts5(conn):
            # FTS5 支持：按查询词相关性排序。
            # 每个词包装为带引号的短语，避免 C++、foo:bar、引号等触发 FTS5 语法错误。
            q = " ".join('"' + t.replace('"', '""') + '"' for t in space_terms)
            try:
                cur = conn.execute(
                    """
                    SELECT path, title, content, mtime, size, rank
                    FROM notes
                    WHERE notes MATCH ?
                    ORDER BY rank
                    LIMIT 200
                    """,
                    (q,),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # 兜底：FTS5 查询失败时退化为 LIKE，保证搜索可用
                rows = []
                cond = " AND ".join(["content LIKE ?" for _ in terms])
                params = [f"%{t}%" for t in terms]
                for row in conn.execute(
                    f"SELECT path, title, content, mtime, size FROM notes WHERE {cond} LIMIT 200",
                    params,
                ):
                    rows.append((*row, 0))
            results = []
            for row in rows:
                path, title, content, mtime, size, _ = row
                results.append(
                    {
                        "path": path,
                        "mtime": mtime,
                        "size": size,
                        "snippet": _snippet(content or title or "", terms),
                    }
                )
            return results
        else:
            # 回退：使用 LIKE 逐个匹配
            cond = " AND ".join(["content LIKE ?" for _ in terms])
            params = [f"%{t}%" for t in terms]
            cur = conn.execute(
                f"SELECT path, title, content, mtime, size FROM notes WHERE {cond} LIMIT 200",
                params,
            )
            results = []
            for row in cur.fetchall():
                path, title, content, mtime, size = row
                results.append(
                    {
                        "path": path,
                        "mtime": mtime,
                        "size": size,
                        "snippet": _snippet(content or title or "", terms),
                    }
                )
            return results
    finally:
        conn.close()


async def search_async(notes_dir: Path, query: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(search, notes_dir, query)


def rebuild_index(notes_dir: Path) -> dict[str, int]:
    """增量重建索引：只处理新增/mtime 变化的文件，并清除已删除文件的条目。

    全量 DROP 重建在大型笔记库下启动开销大（且每个文件一次连接），
    这里改用单连接 + 按 mtime 比对，未变化的文件直接跳过。
    """
    init_index(notes_dir)
    from . import notes_ops

    skip = notes_ops._meta_dirs()
    conn = _connect()
    indexed = 0
    removed = 0
    try:
        existing: dict[str, float] = {}
        try:
            for path, mtime in conn.execute("SELECT path, mtime FROM notes"):
                existing[path] = float(mtime or 0)
        except sqlite3.OperationalError:
            existing = {}

        seen: set[str] = set()
        to_index: list[tuple[str, str, float, int]] = []
        if notes_dir.exists():
            for p in notes_dir.rglob("*.md"):
                rel_parts = p.relative_to(notes_dir)
                if notes_ops._is_hidden(rel_parts) or skip.intersection(rel_parts.parts):
                    continue
                rel = rel_parts.as_posix()
                seen.add(rel)
                try:
                    st = p.stat()
                except OSError:
                    continue
                # mtime 未变化：跳过（REAL 往返存取留少量容差）
                if rel in existing and abs(existing[rel] - st.st_mtime) < 1e-4:
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                to_index.append((rel, content, st.st_mtime, st.st_size))

        # 索引里存在但磁盘已消失的文件：清除
        for rel in existing.keys() - seen:
            conn.execute("DELETE FROM notes WHERE path = ?", (rel,))
            removed += 1

        for rel, content, mtime, size in to_index:
            title = _title_from_path(rel)
            extra = _extract_wikilinks(content)
            full = _space_cjk(f"{title}\n{content}\n{extra}")
            conn.execute("DELETE FROM notes WHERE path = ?", (rel,))
            conn.execute(
                "REPLACE INTO notes (path, title, content, mtime, size) VALUES (?, ?, ?, ?, ?)",
                (rel, title, full, mtime, size),
            )
            indexed += 1
        conn.commit()
    finally:
        conn.close()
    return {"indexed": indexed, "removed": removed}


async def rebuild_index_async(notes_dir: Path) -> dict[str, int]:
    return await asyncio.to_thread(rebuild_index, notes_dir)
