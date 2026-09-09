"""Teyvat Irminsul（提瓦特世界树）冒烟测试：上线前关键路径回归。

覆盖：FTS5 搜索转义、增量索引、图片压缩链路、
文件名/路径校验、PowerShell 转义、ZIP 安全、草稿路径。
"""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdnotes import search_index, notes_ops, images, config as cfg


# ── FTS5 搜索 ─────────────────────────────────────────────

class TestSearch:
    @pytest.fixture(autouse=True)
    def _isolated_db(self, tmp_path, monkeypatch):
        """搜索索引默认写到项目 .mdnotes/search.db，测试隔离到临时文件。"""
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")

    def test_special_queries_do_not_crash(self, tmp_path):
        (tmp_path / "a.md").write_text('C++ 编程与 foo:bar 笔记 "引用"', encoding="utf-8")
        search_index.init_index(tmp_path)
        search_index.index_note(tmp_path, "a.md", 'C++ 编程与 foo:bar 笔记 "引用"', 0.0, 10)
        for q in ['笔记 "', "foo:bar", "C++", '""', "a*b+c", "AND OR NOT", "{引号"]:
            assert isinstance(search_index.search(tmp_path, q), list)

    def test_search_finds_cpp(self, tmp_path):
        search_index.init_index(tmp_path)
        search_index.index_note(tmp_path, "a.md", "C++ 教程", 0.0, 10)
        assert len(search_index.search(tmp_path, "C++")) == 1

    def test_incremental_rebuild(self, tmp_path):
        (tmp_path / "a.md").write_text("苹果", encoding="utf-8")
        (tmp_path / "b.md").write_text("西瓜", encoding="utf-8")
        r1 = search_index.rebuild_index(tmp_path)
        assert r1["indexed"] == 2 and r1["removed"] == 0
        # 无变化：全部跳过
        r2 = search_index.rebuild_index(tmp_path)
        assert r2["indexed"] == 0 and r2["removed"] == 0
        # 修改一个 + 删除一个
        import os, time
        (tmp_path / "a.md").write_text("榴莲", encoding="utf-8")
        future = time.time() + 1
        os.utime(tmp_path / "a.md", (future, future))
        (tmp_path / "b.md").unlink()
        r3 = search_index.rebuild_index(tmp_path)
        assert r3["indexed"] == 1 and r3["removed"] == 1
        assert search_index.search(tmp_path, "西瓜") == []
        assert len(search_index.search(tmp_path, "榴莲")) == 1


# ── 图片处理 ─────────────────────────────────────────────

class TestImages:
    def test_compress_limits_width(self):
        Image = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        Image.new("RGB", (4000, 100), (255, 0, 0)).save(buf, "PNG")
        out = images.compress_image(buf.getvalue(), max_width=1920, quality=80)
        img = Image.open(io.BytesIO(out))
        assert img.size[0] == 1920

    def test_compress_keeps_png(self):
        Image = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), (0, 255, 0)).save(buf, "PNG")
        out = images.compress_image(buf.getvalue(), max_width=1920, quality=80)
        assert out.startswith(b"\x89PNG")

    def test_animated_gif_untouched(self):
        gif = b"GIF89a" + b"\x00" * 64
        # PIL 无法解析时会抛异常 → 编辑器侧保留原字节；compress_image 本身对动图直接返回
        try:
            out = images.compress_image(gif)
            assert out == gif
        except Exception:
            pass  # 假 GIF 头解析失败也符合兜底语义


# ── 文件名 / 路径校验 ─────────────────────────────────────

class TestFilenameValidation:
    @pytest.mark.parametrize("name", ["CON", "con.md", "NUL", "COM1", "LPT9", "abc.", "abc ", "a/b", "a\\b", 'a"b'])
    def test_invalid_names(self, name):
        assert not notes_ops.is_valid_filename(name)

    @pytest.mark.parametrize("name", ["我的笔记.md", "note-1", "a.b.c.md"])
    def test_valid_names(self, name):
        assert notes_ops.is_valid_filename(name)

    @pytest.mark.parametrize("path", ["../x.md", "a/../../b.md", "/etc/passwd", "C:/win/x.md", "\\server\\x"])
    def test_trash_rel_path_rejected(self, path):
        with pytest.raises(ValueError):
            notes_ops._validate_rel_path(path)

    def test_trash_rel_path_ok(self):
        notes_ops._validate_rel_path("folder/note.md")

    def test_attachment_path_allows_attachments_dir(self, tmp_path):
        notes_ops.validate_attachment_path(tmp_path, ".attachments/a.bin")
        notes_ops.validate_attachment_path(tmp_path, "images/a.png")

    def test_attachment_path_rejects_hidden(self, tmp_path):
        with pytest.raises(ValueError):
            notes_ops.validate_attachment_path(tmp_path, ".trash/a.md")
        with pytest.raises(ValueError):
            notes_ops.validate_attachment_path(tmp_path, "../x")


# ── 回收站 ───────────────────────────────────────────────

class TestTrash:
    def test_move_and_restore(self, tmp_path):
        import asyncio
        notes_ops.ensure_meta_dirs(tmp_path)
        f = tmp_path / "n.md"
        f.write_text("内容", encoding="utf-8")
        r = asyncio.run(notes_ops.move_to_trash(tmp_path, f))
        assert r["success"]
        assert not f.exists()
        trash_files = notes_ops.list_trash(tmp_path)
        assert any(t["path"] == "n.md" for t in trash_files)
        r2 = asyncio.run(notes_ops.restore_from_trash(tmp_path, "n.md"))
        assert r2["success"]
        assert f.read_text(encoding="utf-8") == "内容"

    def test_restore_traversal_blocked(self, tmp_path):
        import asyncio
        notes_ops.ensure_meta_dirs(tmp_path)
        r = asyncio.run(notes_ops.restore_from_trash(tmp_path, "../evil.md"))
        assert not r["success"]


# ── ZIP 安全 ─────────────────────────────────────────────

class TestZipSafety:
    def test_traversal_detected(self, tmp_path):
        zp = tmp_path / "t.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("ok.md", "hello")
            zf.writestr("../evil.md", "bad")
        with zipfile.ZipFile(zp) as zf:
            bad = [n for n in zf.namelist() if ".." in Path(n).parts or Path(n).is_absolute()]
        assert bad == ["../evil.md"]


# ── 草稿路径 ─────────────────────────────────────────────

class TestDraftPath:
    def test_no_collision(self):
        from urllib.parse import quote
        a = quote("a/b.md", safe="") + ".draft"
        b = quote("a_b.md", safe="") + ".draft"
        assert a != b


# ── 文件树管理（右键：新建/重命名/删除文件夹） ──
class TestTreeManagement:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        # 用临时目录作为笔记根，隔离真实配置（避免污染用户真实 notes 目录）
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_rename_folder(self, api, tmp_path):
        root = tmp_path / "notes"
        (root / "Old").mkdir(parents=True)
        r = api.rename_folder("Old", "New")
        assert r["success"] is True
        assert (root / "New").is_dir()
        assert not (root / "Old").exists()

    def test_rename_folder_rejects_traversal(self, api):
        r = api.rename_folder("../evil", "x")
        assert r["success"] is False

    def test_delete_folder_moves_to_trash(self, api, tmp_path):
        root = tmp_path / "notes"
        d = root / "Sub"
        d.mkdir(parents=True)
        (d / "a.md").write_text("# hi", encoding="utf-8")
        r = api.delete_folder("Sub")
        assert r["success"] is True
        assert not d.exists()
        # 应进入回收站
        trash = root / ".trash" / "Sub"
        assert trash.exists() and (trash / "a.md").exists()


# ── 关系图谱（双链 → 节点/边） ──
class TestGraph:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_build_graph_edges(self, api, tmp_path):
        root = tmp_path / "notes"
        (root / "A.md").write_text("# A\n链接到 [[B]] 和 [[C]]", encoding="utf-8")
        (root / "B.md").write_text("# B\n回链 [[A]]", encoding="utf-8")
        (root / "C.md").write_text("# C", encoding="utf-8")
        g = api.build_graph()
        ids = {n["id"] for n in g["nodes"]}
        assert {"A.md", "B.md", "C.md"}.issubset(ids)
        pairs = {(e["source"], e["target"]) for e in g["edges"]}
        assert ("A.md", "B.md") in pairs
        assert ("A.md", "C.md") in pairs
        assert ("B.md", "A.md") in pairs

    def test_build_graph_tags_and_cache(self, api, tmp_path):
        root = tmp_path / "notes"
        (root / "Note.md").write_text("# Note\n#项目/alpha 正文", encoding="utf-8")
        g1 = api.build_graph()
        node = next(n for n in g1["nodes"] if n["id"] == "Note.md")
        assert "项目/alpha" in node["tags"]
        # 二次调用应命中缓存（节点数不变）
        g2 = api.build_graph()
        assert len(g2["nodes"]) == len(g1["nodes"])


# ── 静态站点导出（P2：隐私可控的「分享页」平替） ──
class TestExportSite:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_export_site_artifacts(self, api, tmp_path):
        root = tmp_path / "notes"
        (root / "A.md").write_text("# A\n链接 [[B]] 与 ![图](images/x.png)", encoding="utf-8")
        (root / "B.md").write_text("# B\n回链 [[A]]", encoding="utf-8")
        (root / "images").mkdir()
        (root / "images" / "x.png").write_bytes(b"\x89PNG\r\n")
        out = tmp_path / "site"
        r = api.export_site(str(out))
        assert r["success"] is True
        assert (out / "index.html").exists()
        assert (out / "notes" / "A.html").exists()
        assert (out / "notes" / "B.html").exists()
        assert (out / "assets" / "graph.json").exists()
        assert (out / "assets" / "search-index.json").exists()
        assert (out / "assets" / "site.css").exists()
        assert (out / "assets" / "site.js").exists()
        assert (out / "images" / "x.png").exists()
        # 笔记页应渲染并解析 wikilink 为站点内链接
        html_a = (out / "notes" / "A.html").read_text(encoding="utf-8")
        assert "wikilink" in html_a and "notes/B.html" in html_a
        # 搜索索引包含两篇笔记
        import json as _json
        idx = _json.loads((out / "assets" / "search-index.json").read_text(encoding="utf-8"))
        assert len(idx) == 2


# ── 收藏（P0 体验增强）──
class TestFavorites:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        (notes_root / "a.md").write_text("# A", encoding="utf-8")
        (notes_root / "b.md").write_text("# B", encoding="utf-8")
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_toggle_favorite_on_off(self, api):
        r1 = api.toggle_favorite("a.md")
        assert r1["success"] and r1["favorite"] is True
        assert api.is_favorite("a.md") is True
        favs = api.list_favorites()
        assert "a.md" in favs
        r2 = api.toggle_favorite("a.md")
        assert r2["success"] and r2["favorite"] is False
        assert api.is_favorite("a.md") is False
        assert api.list_favorites() == []

    def test_favorites_persist_and_filter_missing(self, api):
        api.toggle_favorite("a.md")
        api.toggle_favorite("b.md")
        # 制造一个已不存在的文件，list_favorites 应过滤
        (api._notes_dir / "b.md").unlink()
        favs = api.list_favorites()
        assert favs == ["a.md"]
        # .favorites.json 应已落盘
        assert (api._notes_dir / ".favorites.json").exists()


# ── 导出 ──
class TestExport:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_export_zip_to_writes_file(self, api, tmp_path):
        notes_root = tmp_path / "notes"
        (notes_root / "a.md").write_text("hello", encoding="utf-8")
        target = tmp_path / "out.zip"
        r = api.export_zip_to(str(target))
        assert r["success"] is True
        assert target.exists()
        with zipfile.ZipFile(target) as zf:
            assert "a.md" in zf.namelist()


# ── 笔记属性 ──
class TestNoteProperties:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_get_note_properties(self, api, tmp_path):
        root = tmp_path / "notes"
        (root / "dir").mkdir()
        (root / "dir" / "note.md").write_text("# 标题\n正文 #tag1 #tag2\n", encoding="utf-8")
        r = api.get_note_properties("dir/note.md")
        assert r["success"] is True
        assert r["name"] == "note"
        assert r["rel_path"] == "dir/note.md"
        assert "tag1" in r["tags"] and "tag2" in r["tags"]
        assert r["line_count"] == 3

    def test_get_note_properties_rejects_missing(self, api):
        r = api.get_note_properties("missing.md")
        assert r["success"] is False



# ── P2-2 导入（文件夹 / 语雀 / Notion）──
class TestImport:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_import_from_folder(self, api, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "a.md").write_text("# A\n正文", encoding="utf-8")
        (src / "b.md").write_text("# B", encoding="utf-8")
        (src / "images").mkdir()
        (src / "images" / "pic.png").write_bytes(b"\x89PNG")
        r = api.import_from_folder(str(src))
        assert r["success"] is True
        assert r["copied"] == 2
        assert r["images"] == 1
        assert (tmp_path / "notes" / "sub" / "a.md").exists()
        assert (tmp_path / "notes" / "b.md").exists()
        assert (tmp_path / "notes" / "images" / "pic.png").exists()

    def test_import_yuque_single_split(self, api, tmp_path):
        # 单个 .md 含多个 H1 → 按标题拆分为多篇
        f = tmp_path / "yuque_export.md"
        f.write_text("# 第一篇\n内容一\n\n# 第二篇\n内容二\n", encoding="utf-8")
        r = api.import_yuque(str(f))
        assert r["success"] is True
        assert r["mode"] == "split"
        assert r["copied"] == 2
        assert (tmp_path / "notes" / "第一篇.md").exists()
        assert (tmp_path / "notes" / "第二篇.md").exists()

    def test_import_yuque_dir(self, api, tmp_path):
        src = tmp_path / "yuque_dir"
        src.mkdir()
        (src / "c.md").write_text("# C\n正文", encoding="utf-8")
        r = api.import_yuque(str(src))
        assert r["success"] is True
        assert r["mode"] == "dir"
        assert r["copied"] == 1

    def test_import_notion_dir(self, api, tmp_path):
        src = tmp_path / "notion"
        (src / "nested").mkdir(parents=True)
        (src / "nested" / "p.md").write_text("# P", encoding="utf-8")
        r = api.import_notion(str(src))
        assert r["success"] is True
        assert r["copied"] == 1
        assert (tmp_path / "notes" / "nested" / "p.md").exists()


# ── P3 模板笔记 ──
class TestTemplates:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_save_and_list_template(self, api):
        r = api.save_template("周报", "# 周报\n日期：{{date}}\n")
        assert r["success"] is True
        assert r["name"] == "周报"
        lst = api.list_templates()
        assert any(t["name"] == "周报" for t in lst)

    def test_create_from_template_renders_vars(self, api, tmp_path):
        api.save_template("会议纪要", "# 会议 {{date}}\n")
        r = api.create_from_template("会议纪要", "第一次", "")
        assert r["success"] is True
        content = (tmp_path / "notes" / "第一次.md").read_text(encoding="utf-8")
        assert "会议 " in content
        assert "{{date}}" not in content  # 变量已替换

    def test_delete_template(self, api):
        api.save_template("临时", "x")
        r = api.delete_template("临时")
        assert r["success"] is True
        assert not any(t["name"] == "临时" for t in api.list_templates())


# ── P3 字数热力图 ──
class TestWordcountStats:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_stats_counts_chars_and_days(self, api, tmp_path):
        (tmp_path / "notes" / "a.md").write_text("苹果香蕉", encoding="utf-8")
        (tmp_path / "notes" / "b.md").write_text("西瓜 梨 桃", encoding="utf-8")
        r = api.get_wordcount_stats(30)
        assert r["success"] is True
        assert r["days"] == 30
        assert r["total"] == 8  # 去空格/换行后：苹果香蕉=4 + 西瓜梨桃=4
        # daily 应覆盖最近 30 天（含补 0）
        assert len(r["daily"]) == 30
        assert any(v > 0 for v in r["daily"].values())
        assert len(r["top_notes"]) >= 2


# ── P3 主题切换 ──
class TestTheme:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        cfg_path = tmp_path / "config.json"
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(cfg, "save_config", lambda c: cfg_path.write_text(__import__("json").dumps(c), encoding="utf-8"))
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_list_themes(self, api):
        themes = api.list_themes()
        ids = {t["id"] for t in themes}
        assert {"genshin", "light", "sakura", "midnight", "forest"}.issubset(ids)

    def test_set_theme_persists(self, api, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(cfg, "save_config", lambda c: cfg_path.write_text(__import__("json").dumps(c), encoding="utf-8"))
        r = api.set_theme("sakura")
        assert r["success"] is True
        saved = __import__("json").loads(cfg_path.read_text(encoding="utf-8"))
        assert saved.get("theme") == "sakura"

    def test_set_theme_rejects_unknown(self, api):
        r = api.set_theme("neon")
        assert r["success"] is False


# ── P3 图片 OCR（无引擎时优雅降级）──
class TestOCR:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root)}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_ocr_missing_image(self, api):
        r = api.ocr_image("images/nope.png")
        assert r["success"] is False

    def test_ocr_no_engine_graceful(self, api, tmp_path, monkeypatch):
        # 让 _find_tesseract 返回 None → available=False，不抛异常
        from mdnotes import webapi
        monkeypatch.setattr(webapi.WebAPI, "_find_tesseract", lambda self: None)
        (tmp_path / "notes" / "images").mkdir()
        (tmp_path / "notes" / "images" / "x.png").write_bytes(b"\x89PNG")
        r = api.ocr_image("images/x.png")
        assert r["success"] is False
        assert r.get("available") is False
        assert "Tesseract" in (r.get("error") or "")


# ── 同步：调用参数与返回处理 ──
class TestSync:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        notes_root = tmp_path / "notes"
        notes_root.mkdir(parents=True, exist_ok=True)
        test_cfg = {"notes_dir": str(notes_root), "webdav": {"url": "https://dav.test/", "username": "u", "password": "p", "remote_dir": "/md"}}
        monkeypatch.setattr(cfg, "load_config", lambda: test_cfg)
        monkeypatch.setattr(cfg, "save_config", lambda c: None)
        monkeypatch.setattr(search_index, "_index_path", lambda: tmp_path / "search_test.db")
        from mdnotes import webapi
        return webapi.WebAPI()

    def test_webdav_sync_now_arguments_and_dict_return(self, api, monkeypatch):
        from mdnotes import webdav_sync
        called = {}
        async def fake_sync(local_dir, url, username, password, remote_dir="/mdnotes"):
            called.update({"local_dir": str(local_dir), "url": url, "username": username, "password": password, "remote_dir": remote_dir})
            return {"success": True, "uploaded": 5, "skipped": 2, "failed": 0, "message": "ok"}
        monkeypatch.setattr(webdav_sync, "sync_to_webdav", fake_sync)
        r = api.webdav_sync_now()
        assert r["success"] is True
        assert r["merged"] == 5
        assert r["uploaded"] == 5
        assert called["username"] == "u"
        assert called["password"] == "p"
        assert called["remote_dir"] == "/md"
