"""Teyvat Irminsul 后端 sidecar（Electron 混合架构）PyInstaller 打包脚本。

把 run_backend.py（本地 HTTP 服务）打包为独立可执行程序，
供 Electron 主进程作为 sidecar 拉起（见 electron/main.js 的 backendCommand()）。

生成 dist/TeyvatIrminsulBackend/TeyvatIrminsulBackend.exe，
由 electron/package.json 的 extraResources 复制到 resources/backend/。

用法：
    python scripts/build_backend.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

# Windows CI（GitHub Actions）默认代码页非 UTF-8，PyInstaller 与脚本的中文输出
# 会触发 UnicodeEncodeError；统一按 UTF-8 输出（本地开发不受影响）。
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent


def _safe_rmtree(path: Path, label: str) -> None:
    """删除目录，文件被占用（exe 运行中）时给出可操作提示而非栈崩溃。"""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError as exc:
        print(f"错误: 无法删除 {label} 目录 {path}", file=sys.stderr)
        print(f"  原因: {exc}", file=sys.stderr)
        print("  请确认相关 exe 未在运行，然后重试。", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"警告: 删除 {label} 目录时出错（已忽略）: {exc}", file=sys.stderr)


def build() -> None:
    """执行 PyInstaller 打包。"""
    dist_dir = ROOT / "dist"
    work_dir = ROOT / "build" / "backend"
    out_dir = dist_dir / "TeyvatIrminsulBackend"

    _safe_rmtree(out_dir, "输出")
    _safe_rmtree(work_dir, "build")

    # 控制台模式：Electron 以 windowsHide 拉起，不会闪现控制台窗口；
    # 保留 stdout 管道保证 PORT= 能被主进程可靠解析。
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "TeyvatIrminsulBackend",
        "--onedir",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        # pymdownx 扩展按名字动态 import，必须 collect-all
        "--collect-all", "pymdownx",
        "--collect-all", "markdown",
        "--collect-all", "pygments",
        "--collect-all", "aiofiles",
        "--collect-all", "PIL",
        "--collect-submodules", "mdnotes",
        "run_backend.py",
    ]

    print("正在打包后端 sidecar...")
    print(f"命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(ROOT))

    if result.returncode != 0:
        print("打包失败！")
        sys.exit(result.returncode)

    # 清理中间文件
    spec_file = ROOT / "TeyvatIrminsulBackend.spec"
    if spec_file.exists():
        spec_file.unlink()
    _safe_rmtree(work_dir, "build")

    exe_path = out_dir / "TeyvatIrminsulBackend.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print("\n打包成功！")
        print(f"  输出目录: {out_dir}")
        print(f"  可执行文件: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("\n打包完成但未找到 exe，请检查输出。")
        sys.exit(1)


if __name__ == "__main__":
    build()
