"""下载 KaTeX 与 Mermaid 到本地 static/vendor，避免依赖 CDN。"""

import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
KATEX_DIR = ROOT / "mdnotes" / "static" / "vendor" / "katex"
MERMAID_DIR = ROOT / "mdnotes" / "static" / "vendor" / "mermaid"

KATEX_VERSION = "0.16.9"
# 钉死具体版本保证构建可复现（此前为浮动大版本 "10"，每次解析可能不同）
MERMAID_VERSION = "10.9.6"


def download(url: str, dest: Path) -> None:
    print(f"下载 {url} -> {dest}")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)


def main() -> int:
    KATEX_DIR.mkdir(parents=True, exist_ok=True)
    (KATEX_DIR / "fonts").mkdir(exist_ok=True)
    MERMAID_DIR.mkdir(parents=True, exist_ok=True)

    base = f"https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist"

    # CSS
    css_url = f"{base}/katex.min.css"
    css_path = KATEX_DIR / "katex.min.css"
    download(css_url, css_path)

    # 解析并下载字体
    css = css_path.read_text(encoding="utf-8")
    for match in re.finditer(r"url\(fonts/([^)]+)\)", css):
        font = match.group(1)
        font_url = f"{base}/fonts/{font}"
        font_path = KATEX_DIR / "fonts" / font
        if not font_path.exists():
            download(font_url, font_path)
        else:
            print(f"已存在 {font_path}")

    # JS
    js_url = f"{base}/katex.min.js"
    download(js_url, KATEX_DIR / "katex.min.js")

    # Mermaid
    mermaid_url = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
    download(mermaid_url, MERMAID_DIR / "mermaid.min.js")

    print("本地 Vendor 下载完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())