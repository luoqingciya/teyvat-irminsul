# Teyvat Irminsul · 提瓦特世界树

> 一款基于 **Electron + Python 混合架构**的原生桌面 Markdown 笔记工具。界面采用原神二次元风格，所有静态资源完全本地化，**无需任何 CDN，断网也能用**。
>
> UI 层使用 Web 技术栈（HTML / CSS / JavaScript + CodeMirror 编辑器），由 Electron（Chromium 内核）承载渲染；业务逻辑与数据层由 Python 后端提供，通过本地 HTTP 服务（随机端口）通信。前后端各司其职：**Electron 负责窗口/托盘/系统集成，Python 负责笔记、搜索、Git 历史、WebDAV、加密等核心能力**。

[![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue)](https://www.python.org/)
[![Electron](https://img.shields.io/badge/Electron-31%2B-informational)](https://www.electronjs.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 核心亮点

| 亮点 | 说明 |
|------|------|
| **混合架构** | Electron 主进程拉起 Python 后端 sidecar（PyInstaller 打包的独立 exe），后端在 `127.0.0.1` **随机端口**起 HTTP 服务，前端通过 `POST /api/<method>` 调用，并用**随机令牌（X-Auth-Token）**鉴权，本机恶意网页无法读写笔记；打包后数据落在 **exe 所在目录**（便携式布局），整个文件夹拷走即完整便携。 |
| **完全离线** | KaTeX、Mermaid 全部内置到 `static/vendor/`，没有任何外部 CDN 依赖。 |
| **实时预览** | 左侧 Markdown 编辑器（CodeMirror 语法高亮），右侧 Web 实时渲染；支持原神/晨曦/樱粉/深蓝/森野五套明暗主题。 |
| **多标签页** | 支持同时打开多个笔记，标签页切换/关闭，未保存时以 ● 标记。 |
| **双链笔记** | 输入 `[[` 自动补全已有笔记链接（支持子目录笔记，如 `[[子目录/笔记名]]`），快速构建知识网络；侧边栏显示反向链接。 |
| **反向链接** | 侧边栏「反向链接」查看引用了当前笔记的其他笔记。 |
| **未关联笔记** | 一键查看未被任何双链引用的笔记，方便补齐知识网络。 |
| **标签补全** | 输入 `#` 自动弹出已有标签列表，快速插入标签。 |
| **代码块语言** | 输入 ` ``` ` 自动弹出 42 种编程语言列表，一键选择高亮语言。 |
| **大纲面板** | 自动解析 Markdown 标题生成层级目录，点击跳转到对应位置。 |
| **快速切换器** | Ctrl+P 打开模糊搜索，输入关键词快速跳转到任意笔记（支持子目录笔记，候选显示相对路径）。 |
| **笔记模板** | 侧边栏「📋」标签管理与使用模板；支持 `{{date}} {{time}} {{datetime}} {{year}} {{month}} {{day}}` 变量，新建时自动替换为当前日期时间；模板存于 `.templates/`，可新建/编辑/删除。 |
| **图片拖拽粘贴** | 截图/图片直接粘贴或拖入编辑器，自动保存到 `images/` 并插入 Markdown 链接。 |
| **导出 PDF** | 通过浏览器打印（`window.print()`）一键导出当前笔记为 PDF。 |
| **历史版本** | 基于本地 Git 仓库：每次保存前自动为旧内容建档，版本不受数量限制，支持对比与一键回退（需本机安装 Git）。 |
| **回收站** | 删除的笔记先进入回收站，可随时恢复或彻底清空。 |
| **草稿恢复** | 编辑内容自动保存草稿到 `.drafts/`，打开笔记时检测未保存变更并提示恢复。 |
| **保存状态指示** | 状态栏实时显示 ● 未保存 / 已保存状态，避免数据丢失。 |
| **附件管理** | 图片/附件自动保存到 `notes/images/` 和 `notes/.attachments/`。 |
| **笔记属性** | 侧边栏显示当前笔记的文件名、大小、创建/修改时间、字数统计。 |
| **全文搜索** | 基于 SQLite FTS5 的全文索引，搜索标题与正文内容（中文按字分词，子串可命中）；侧边栏标签云快速筛选。 |
| **WebDAV 同步** | 增量上传到任意 WebDAV 服务（已兼容 123 网盘等路径编码特殊的厂商）。 |
| **差异恢复** | 从 WebDAV 恢复时先预检本地/远程差异，逐文件选择「保留本地 / 使用远程 / 合并」（文本文件），确认后执行。 |
| **聚焦模式** | Ctrl+. 进入沉浸式书写画布：隐藏侧栏/大纲/预览面板，仅保留居中限宽的编辑器，Esc 退出。 |
| **预览全屏** | 预览工具栏「⛶」按钮可将预览面板铺满整个窗口，单独阅读渲染结果；全屏预览左侧悬浮大纲，点击标题可快速跳转，Esc 退出。 |
| **三栏自由拉伸** | 编辑区、大纲、预览之间的分隔条可自由拖拽调整宽度，适应不同写作习惯。 |
| **滚动联动** | 编辑区与预览区按滚动比例双向同步（在设置中可关闭），编辑/预览任一侧滚动，另一侧自动跟随。 |
| **原生风弹窗** | 新建笔记/文件夹、关于、历史版本、回收站、导入导出、设置等操作均使用统一的原神风格自建模态框，不再出现系统原生 prompt/alert。 |
| **单实例运行** | 已限制重复启动：再次双击 exe 会唤起已运行的窗口，而非开第二个进程。 |
| **系统托盘** | 关闭窗口时最小化到系统托盘（Electron Tray 实现）；**左键单击托盘图标直接显示主窗口**，右键菜单可显示/隐藏窗口、退出、撰写闪念笔记；支持开机自启。 |
| **ZIP 导入导出** | 导出 ZIP 时弹出保存路径选择框，导出完成 toast 提示具体位置；从 ZIP 导入恢复，自动校验路径安全并拒绝 zip bomb。 |
| **质量兜底** | 56 项 pytest 冒烟测试覆盖搜索、索引、回收站、压缩、打包入口、路径安全、文件对话框、笔记属性、收藏、关系图谱、静态站点导出、导入、模板、字热统计、主题、OCR 等关键路径；ruff 零告警。 |
| **全局日志** | 运行日志按天轮转输出到 `logs/`，自动清理 30 天前的旧日志。 |
| **每日笔记** | Ctrl+D 一键创建/打开当日笔记（`Daily/YYYY-MM-DD.md`），自动填充日期模板。 |
| **闪念笔记** | Ctrl+Shift+N 或托盘「✏ 闪念笔记」弹出极简输入框，回车即存为 `inbox/<时间戳>.md`。 |
| **最近文件** | 侧边栏「🕒」标签查看最近打开的笔记；Ctrl+1~9 快速跳转到对应的最近文件。 |
| **收藏** | 侧边栏「⭐」标签集中管理收藏笔记；文件树右键或工具栏「⭐」按钮即可收藏/取消，已收藏笔记按钮高亮。 |
| **编辑工具栏** | 编辑器顶部一排格式化按钮：加粗/斜体/标题/列表/任务清单/引用/代码块/表格/链接/图片，一键插入 Markdown 语法；任务清单复选框可在预览中直接勾选并写回源码。 |
| **命令面板** | Ctrl+K 唤出全局命令面板：模糊搜索跳转笔记、执行命令（新建/保存/聚焦/预览全屏/同步/设置/图谱…）、按 #标签 筛选，支持上下键 + 回车。 |
| **关系图谱** | 侧边栏「🕸」标签查看双链关系力导向图：节点=笔记（大小随连接数、黄色=带标签），连线=[[引用]]方向；点击节点打开笔记，hover 高亮邻域，滚轮缩放、拖拽布局；带 mtime 缓存，可一键重建。 |
| **标签层级** | `#parent/child` 层级标签在标签云中树状展示；点击父标签可筛选其所有子标签笔记。 |
| **搜索命中高亮** | 侧栏搜索结果片段对纯文本关键词高亮（`<mark>`），点击结果直接定位打开笔记；支持 `#标签1 #标签2` 多标签交集检索。 |
| **笔记别名** | YAML frontmatter `aliases:` 定义的别名可在 `[[别名]]` 双链与补全中定位到对应笔记。 |
| **孤儿附件清理** | 工具栏「🧹」扫描 `images/` 中未被任何笔记引用的图片，可逐一删除释放空间。 |
| **新建文件夹** | 侧边栏「＋」或目录节点上的「＋」在任意层级创建子目录，组织笔记结构。 |
| **文件树右键管理** | 在侧栏文件/文件夹上右键，可新建笔记、新建子文件夹、重命名、删除、查看属性（移入回收站可恢复）。 |
| **静态站点导出** | 工具栏「🌐 网站」将整个知识库导出为纯静态网站（含 `index.html` 笔记列表 + 实时搜索 + 关系图谱），双击即开、无需服务器，亦可推 GitHub Pages 分享——语雀「分享页」的隐私可控平替；`[[双链]]` 自动转为站点内跳转，图片随包复制；导出 HTML 经过白名单消毒，可安全发布到公网。 |
| **多源导入** | 工具栏「⋯」菜单提供四种导入：📁 文件夹（含子目录与图片）、🌿 语雀（文件夹或单个 .md，按 H1 自动拆分为多篇）、📝 Notion（文件夹，自动抽取 .md 与图片）、⬆ ZIP；导入后自动重建索引。 |
| **字数热力图** | 侧边栏「📊」标签按日历热力图展示写作活跃度（近 120/365 天可切换），颜色越深代表当天书写越多，并列出字数最多的笔记。 |
| **多主题** | 设置面板「主题」可切换原神（暗）/晨曦（亮）/樱粉（亮）/深蓝（暗）/森野（暗）五套配色，即时生效并持久化；CodeMirror 编辑器与 Mermaid 图表同步明暗。 |
| **工具栏二级菜单** | 常用功能（同步/每日/闪念/历史/设置）直接外露在工具栏；其余按「导入/导出/工具」分组折叠在「⋯」二级菜单中，侧栏 Tab 也仅保留常用 4 个、其余收进「⋮」下拉，界面不拥挤。 |
| **图片 OCR** | 预览中右键或双击图片即可对图片做文字识别（需本地安装 Tesseract-OCR 并含 `chi_sim` 中文包），识别结果自动插入当前笔记；无 OCR 引擎时给出友好提示而非崩溃。 |
| **Callout 提示块** | 支持 Obsidian 风格 `> [!type]` 引用块，渲染为带图标与配色的提示卡片（info/note/tip/warning/danger/success/question/quote/example）。 |
| **文字高亮** | 使用 `==文字==` 包裹的选中文字在预览中渲染为黄色高亮 `<mark>`。 |
| **应用锁** | 设置应用锁密码后，启动进入锁屏需输入密码解锁，保护整个应用访问。 |
| **单篇笔记加密** | 对单篇笔记用密码派生密钥（AES-256-GCM）加密存储，磁盘上为密文，打开/保存时透明解密，防止明文泄露。 |
| **导出 PNG** | 工具栏「⋯」→ 导出 →「🖼 导出PNG」把当前笔记渲染为图片 PNG 并下载，主题样式一并保留。 |
| **标签管理** | 标签面板支持整体重命名标签（跨全部笔记更新）；「工具→批量标签」先勾选任意笔记、填写添加/删除标签，按篇实时进度完成批量打标签。 |
| **搜索范围过滤** | 侧栏搜索框下可选择按目录（子目录）限定搜索范围，结果仅保留目标文件夹内的笔记。 |
| **WebDAV 定时备份** | 同步面板可开启定时自动备份，按设定间隔（分钟）增量上传到 WebDAV，无需手动同步。 |
| **自定义快捷键** | 设置面板「配置快捷键」可为保存、加粗、聚焦、每日/闪念、导出 PNG 等动作绑定任意组合键，即时生效。 |
| **启动恢复会话** | 开启后，启动自动重开上次浏览的笔记，续写无缝衔接。 |

---

## 系统架构

```
┌────────────────────────────────────────────────────────────┐
│                      Electron 主进程 (main.js)              │
│  · 拉起 Python 后端 sidecar（stdout 解析 PORT=）            │
│  · 创建窗口 / 系统托盘 / 单实例锁 / 开机自启                 │
│  · 关闭窗口 → 最小化到托盘，进程常驻                         │
└───────────────┬───────────────────────────┬────────────────┘
                │ spawn（打包后经 MDNOTES_DATA_DIR 指定数据目录）
                ▼                           │ 加载
┌───────────────────────────┐               ▼
│  Python 后端 (sidecar)     │   ┌──────────────────────────┐
│  mdnotes/electron_server   │   │  webui/（HTML/CSS/JS）     │
│  127.0.0.1 随机端口 HTTP    │◄──┤  index.html + app.js      │
│  POST /api/<method>        │   │  渲染进程 fetch 调用        │
│  WebAPI → notes/search/…   │   │  原生能力经 preload.js 桥接 │
└───────────────────────────┘   └──────────────────────────┘
```

- **通信**：后端仅监听 `127.0.0.1` 随机端口（避免端口被占用），前端统一走 `POST /api/<method>`，`body={"args":[...]}`。
- **安全鉴权**：主进程启动时生成 32 字节随机令牌，经 `MDNOTES_API_TOKEN` 环境变量传给后端；前端经 preload 取回后放在每次请求的 `X-Auth-Token` 请求头。本机其他进程/恶意网页拿不到该令牌，无法读写笔记（防 DNS rebinding / CSRF 类攻击）。后端异常退出时主进程弹窗提示。
- **数据隔离**：开发模式下数据落在项目根目录（`config.json` / `notes/`）；打包后 Electron 通过 `MDNOTES_DATA_DIR` 环境变量把数据目录指向 **exe 所在目录**（便携式布局），Chromium 状态（Local Storage / 缓存）落 `<exe 目录>/userData`，后端所在 `resources/backend/` 保持只读，整个文件夹拷走即完整数据。
- **资源加载**：`webui/` 与 `static/` 通过 electron-builder 的 `extraResources` 复制到 `resources/`，渲染进程以 `file://` 加载，完全离线。

---

## 快速开始

### 环境要求

- Python >= 3.10 + [UV](https://docs.astral.sh/uv/) 包管理器
- Node.js >= 18 + npm
- Git（历史版本功能需要；未安装则历史版本不可用，其余功能不受影响）

### 安装与启动（开发模式）

```powershell
# 1. 安装 Python 后端依赖
uv sync

# 2. 安装 Electron 依赖（首次需下载 Electron 二进制，国内建议走镜像）
cd electron
npm install        # 如下载慢：$env:ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/"
npm start          # 自动拉起后端 + 打开窗口
```

`npm start` 会以开发模式启动：Electron 主进程直接调用 `.venv\Scripts\python.exe run_backend.py`，读取 stdout 中的随机端口后加载 `../mdnotes/webui/index.html`。

首次启动会自动生成默认 `config.json` 与 `notes/` 目录。

### 更新离线资源

如果 `static/vendor/` 缺失，公式、流程图可能无法渲染：

```powershell
uv run python scripts/download_vendor.py
```

### 打包为可执行文件

打包分两步：先用 PyInstaller 把 Python 后端打成独立 sidecar exe，再用 electron-builder 打包整个应用（内含 Electron + 前端 + 后端）。

```powershell
# 1. 打包 Python 后端（依赖 pyinstaller，可先 uv sync --extra dev）
uv run python scripts/build_backend.py
#    → dist/TeyvatIrminsulBackend/TeyvatIrminsulBackend.exe

# 2. 打包 Electron 应用（产出 win-unpacked 便携版 + NSIS 安装包）
cd electron
npm run dist
#    → electron/release/win-unpacked/（双击 TeyvatIrminsul.exe 即可运行）
#    → electron/release/TeyvatIrminsul Setup 1.0.0.exe（安装包）
```

**打包说明**

| 项目 | 说明 |
|------|------|
| 后端打包脚本 | `scripts/build_backend.py`（PyInstaller `--onedir`，收集 pymdownx/markdown/pygments/PIL 等动态导入模块） |
| 后端入口 | `run_backend.py` → `mdnotes.electron_server:main`（stdout 打印 `PORT=<port>`） |
| 应用打包 | `electron/package.json` 的 `build.extraResources` 将 `webui/`、`static/`、后端 exe 复制进 `resources/` |
| 安装包 | NSIS 一键安装（`TeyvatIrminsul Setup <ver>.exe`），安装目录按 package.json 的 `name`（`teyvat-irminsul`，小写）命名；同版本号已安装时会跳过，需先卸载旧版 |
| 数据目录 | 打包版（便携版/安装版）统一落在 **exe 所在目录**（`config.json` / `notes/` / `logs/` / 搜索索引 / `.env`），Chromium 状态在 `<exe 目录>/userData`；整个文件夹拷走即完整便携 |

> **注意**：旧的 pywebview 版打包入口（`build_web.py`、`app_web.py`）与 Qt 打包脚本（`build_exe.py`）已随架构迁移移除，请使用上述 Electron 流程。

---

## 目录结构

```
Teyvat-Irminsul/
├── mdnotes/                        # Python 后端包
│   ├── electron_server.py          # 本地 HTTP 服务（127.0.0.1 随机端口，CORS，/api/<method> 封装）
│   ├── webapi.py                   # 前后端桥接 API 层（暴露给 JS 调用的 Python 方法）
│   ├── config.py                   # 配置加载/保存、环境变量替换、MDNOTES_DATA_DIR 支持
│   ├── auth.py                     # 令牌哈希等认证工具
│   ├── notes_ops.py                # 笔记 CRUD、任务扫描、批量标签、模板
│   ├── images.py                   # 图片压缩、缩放、保存
│   ├── git_history.py              # 基于本地 Git 仓库的历史版本（建档/查询/恢复）
│   ├── webdav_sync.py              # WebDAV 同步与差异恢复
│   ├── markdown_render.py          # Markdown 渲染（KaTeX / Mermaid / 代码高亮 / 白名单消毒）
│   ├── search_index.py             # SQLite FTS5 全文索引（中文按字分词、启动增量重建）
│   ├── note_crypto.py              # 单篇笔记 AES-256-GCM 加密
│   ├── logger.py                   # 全局日志系统（按天轮转，自动清理）
│   ├── webui/                      # 前端 UI（HTML/CSS/JS，编辑器用 CodeMirror 5）
│   │   ├── index.html              # 多栏布局主页面
│   │   ├── css/                    # 原神风格主题、布局、预览样式（含 PDF 打印样式）
│   │   └── js/app.js               # 前端控制器（HTTP 调用、多标签、补全、大纲、模态等）
│   └── static/                     # 前端静态资源
│       ├── favicon.svg             # 应用图标
│       ├── icon.ico                # Windows 应用/托盘图标
│       └── vendor/                 # CodeMirror / KaTeX / Mermaid / Cytoscape（离线可用）
├── electron/                       # Electron 壳
│   ├── main.js                     # 主进程（拉起后端、窗口、托盘、单实例、退出清理）
│   ├── preload.js                  # 原生能力桥接（contextBridge：API 基址/鉴权令牌/对话框/开机自启/闪念） |
│   ├── package.json                # Electron 依赖与 electron-builder 配置
│   └── release/                    # electron-builder 产物（便携版 + 安装包，已 gitignore）
├── scripts/
│   ├── build_backend.py            # 后端 sidecar PyInstaller 打包脚本
│   ├── download_vendor.py          # 下载/更新 CodeMirror/KaTeX/Mermaid/Cytoscape（版本已钉死）
│   └── _gen_icon.py                # 应用图标生成脚本
├── tests/
│   └── test_smoke.py               # pytest 冒烟测试套件（56 项）
├── run_backend.py                  # 后端启动引导入口（源码运行 / PyInstaller 共用）
├── notes/                          # Markdown 笔记（开发模式下的用户数据，gitignore）
│   ├── images/                     # 笔记中的图片附件
│   ├── .attachments/               # 其他附件（非图片）
│   ├── .trash/                     # 回收站
│   ├── .history/                   # 历史版本相关状态
│   ├── .drafts/                    # 未保存的草稿
│   └── .templates/                 # 笔记模板
├── .mdnotes/                       # 运行时数据（搜索索引等，首次运行后生成）
├── logs/                           # 运行日志（按天轮转，保留 30 天）
├── config.json                     # 运行时配置（首次启动自动生成，gitignore）
├── .env                            # 敏感信息（首次手动创建，参考 .env.example）
├── .env.example                    # 环境变量模板
├── pyproject.toml                  # 后端依赖、ruff 配置与元数据
├── uv.lock                         # 依赖锁定文件（构建可复现）
└── README.md                       # 本说明文件
```

> **提示**：开发模式下所有运行时生成的数据（日志、索引、同步状态、配置）均保存在项目根目录下，不会写入系统目录；打包后（便携版/安装版）统一落在 **exe 所在目录**（`config.json` / `notes/` / `logs/` / 搜索索引），Chromium 状态在 `<exe 目录>/userData`。

### 文件可删除性

| 目录/文件 | 能否删除 | 说明 |
|-----------|----------|------|
| `mdnotes/`（Python 包） | ❌ | 核心后端源码，删除后无法运行。 |
| `electron/`（不含 release） | ❌ | Electron 壳源码，删除后无法运行。 |
| `notes/` | ⚠️ 可删但丢数据 | 开发模式下的笔记与图片。 |
| `notes/images/` / `notes/.attachments/` | ⚠️ 可删但丢数据 | 笔记中的图片和附件。 |
| `notes/.trash/` / `notes/.history/` / `notes/.drafts/` | ⚠️ 可删但丢数据 | 回收站、历史版本、草稿。 |
| `mdnotes/static/vendor/` | ✅ | 运行 `scripts/download_vendor.py` 自动重建。 |
| `.venv/` | ✅ | `uv sync` 自动重建。 |
| `electron/node_modules/` | ✅ | `npm install` 自动重建。 |
| `electron/release/` | ✅ | `npm run dist` 重新打包。 |
| `dist/` | ✅ | 后端 sidecar 产物，`scripts/build_backend.py` 重新打包。 |
| `tests/` / `uv.lock` | ✅ | 测试与锁文件，删除不影响运行（但锁文件缺失时 `uv sync` 会重新解析）。 |
| `.mdnotes/` | ✅ | 搜索索引，删除后自动重建（需重新索引）。 |
| `logs/` | ✅ | 运行日志，可随时清理。 |
| `config.json` | ✅ | 删除后恢复默认配置。 |
| `.env` | ✅ 但建议保留 | 存放 WebDAV 密码（开发时在项目根，打包后在 exe 同级目录）。 |

---

## 配置说明

首次启动会自动生成 `config.json`，也可以在应用内 **设置** 对话框中修改。设置中涉及目录（如笔记目录）的输入框提供「浏览…」按钮，直接调用系统文件夹选择对话框，无需手动粘贴路径。

### config.json

```json
{
  "notes_dir": "notes",
  "server": { "host": "0.0.0.0", "port": 8000, "auth_token": "", "lan_enabled": true },
  "webdav": {
    "enabled": false,
    "url": "https://dav.jianguoyun.com/dav",
    "username": "",
    "password": "",
    "remote_dir": "/mdnotes",
    "incremental": true,
    "auto_sync": false,
    "auto_sync_interval_min": 30
  },
  "image": { "max_width": 1920, "quality": 85, "compress": true },
  "editor": {
    "typewriter_scroll": true,
    "scroll_linked": true,
    "auto_save": true,
    "auto_save_delay_ms": 2000,
    "restore_session": true,
    "keymap": {}
  },
  "app_lock": { "enabled": false, "password": "" },
  "encrypt": { "key": "", "notes": {} },
  "tags": { "colors": {} },
  "saved_searches": []
}
```

### 环境变量（敏感信息）

推荐将敏感信息放在 `.env` 中，避免写入 `config.json`：

```bash
MDNOTES_WEBDAV_PASSWORD=你的WebDAV密码
```

| 变量 | 作用 |
|------|------|
| `MDNOTES_WEBDAV_PASSWORD` | WebDAV 密码（覆盖 config.json，避免明文落盘） |
| `MDNOTES_DATA_DIR` | 打包后由 Electron 自动注入为 exe 所在目录，指定数据目录（一般无需手动设置） |
| `MDNOTES_CONFIG` | 指定 config.json 的绝对路径（可选） |
| `MDNOTES_API_TOKEN` | 打包后由 Electron 自动注入的随机鉴权令牌（一般无需手动设置） |

应用启动时会自动加载 **exe 同级目录**下的 `.env`（源码运行时为项目根目录）。

---

## 使用指南

### 编辑与预览

- **多标签页**：支持同时打开多个笔记，标签页切换、未保存时以 ● 标记；关闭时自动提示保存未保存的更改。
- **实时预览**：左侧编辑，右侧自动渲染（150ms 防抖），可隐藏/显示预览面板。
- **语法高亮**：CodeMirror 编辑器提供 Markdown 语法高亮、当前行高亮、自动补全括号/引号、Tab 缩进。
- **自动保存**：停止输入约 2 秒后自动保存（可在设置中开关），状态栏显示 ● 未保存 / 已保存 / 自动保存状态；意外关闭后可恢复本地草稿。
- **字体缩放**：Ctrl+= / Ctrl+-（或 Ctrl+滚轮）调整字体大小，适应不同屏幕。
- **插入图片**：截图或图片直接粘贴/拖入编辑器，自动保存到 `images/` 并插入 Markdown 链接；大图自动压缩；也可用编辑工具栏「🖼 图片」按钮选择本地图片。
- **编辑工具栏**：编辑器顶部一排格式化按钮（加粗/斜体/标题/列表/任务清单/引用/代码块/表格/链接/图片），点击即插入对应 Markdown 语法；任务清单的复选框在预览中可直接勾选，改动会写回源码。
- **目录 / 公式 / 流程图**：大纲面板点击标题快速跳转；支持 `$$LaTeX$$` 公式和 `mermaid` 代码块。
- **快捷键增强**：Ctrl+Shift+K 插入代码块；输入 ` ``` ` 弹出语言选择；行内 `[[` 触发双链补全、`#` 触发标签补全。

### 已支持的快捷键

| 快捷键 | 功能 | 作用范围 |
|--------|------|----------|
| `Ctrl + S` | 保存当前笔记 | 编辑器内 |
| `Ctrl + B` | 加粗选中文字 | 编辑器内 |
| `Ctrl + I` | 斜体选中文字 | 编辑器内 |
| `Ctrl + K` | 打开全局命令面板（跳转笔记 / 执行命令 / 按 #标签 筛选） | 全局 |
| `Ctrl + Shift + K` | 插入/包裹代码块 | 编辑器内 |
| `Ctrl + =` | 放大字体 | 全局 |
| `Ctrl + -` | 缩小字体 | 全局 |
| `Ctrl + P` | 快速切换器（模糊搜索跳转笔记） | 全局 |
| `Ctrl + D` | 打开/创建每日笔记 | 全局 |
| `Ctrl + Shift + N` | 闪念笔记（弹窗速记） | 全局 |
| `Ctrl + 1` ~ `Ctrl + 9` | 打开最近文件列表中的第 N 个 | 全局 |
| `Ctrl + .` | 进入 / 退出聚焦模式（沉浸式书写） | 全局 |
| `Esc` | 退出聚焦模式 / 关闭弹窗 / 取消补全 | 全局 |
| `[[` | 触发双链笔记 / 别名自动补全 | 编辑器内 |
| `#` | 触发标签自动补全 | 编辑器内 |
| ` ``` ` | 触发代码块语言选择 | 编辑器内 |

### 文件管理

- **新建笔记**：工具栏「+ 笔记」按钮，或输入 `[[新笔记名]]` 创建后点击跳转。
- **新建文件夹**：侧边栏「+ 文件夹」在根目录新建，或鼠标悬停目录节点右侧的「＋」在任意层级新建子目录。
- **每日笔记**：Ctrl+D 一键创建/打开当日笔记（`Daily/YYYY-MM-DD.md`），自动填充日期模板。
- **闪念笔记**：Ctrl+Shift+N 或托盘菜单「✏ 闪念笔记」弹出极简输入框，回车即存为 `inbox/<时间戳>.md`。
- **最近文件**：侧边栏「🕒」标签查看最近打开的笔记；Ctrl+1~9 快速跳转到第 N 个最近文件。
- **快速切换**：Ctrl+P 打开模糊搜索，输入关键词快速跳转到任意笔记。
- **历史版本**：基于本地 Git 仓库，每次保存前自动为旧内容建档（版本不受数量限制），可查看各版本内容并一键回退；需本机安装 Git。
- **回收站**：删除的笔记进入回收站，可恢复或彻底删除。
- **搜索与标签**：侧边栏「搜索」全文搜索，「标签」按标签树筛选；点击父标签 `#parent` 会同时筛选其所有子标签 `#parent/child` 笔记；输入 `#` 触发标签自动补全。
- **双链与别名**：输入 `[[` 触发双链笔记与别名补全；侧边栏「反向链接」查看引用了当前笔记的其他笔记（`[[别名]]` 同样会被计入反向链接）。
- **孤儿附件清理**：工具栏「🧹」扫描 `images/` 中未被任何笔记引用的图片，可逐一删除释放空间。
- **笔记属性**：工具栏「ℹ 属性」或文件树右键「属性」查看当前笔记的完整信息，包括文件名、相对/绝对路径、大小、创建/修改时间、字符数、字数、行数、标签等。
- **导入内容**：工具栏「⋯」菜单提供多源导入——📁 文件夹（递归复制 .md 与图片）、🌿 语雀（文件夹或单个 .md，单文件含多个 H1 时会按标题拆分为多篇笔记）、📝 Notion（文件夹，自动抽取页面与图片）、⬆ ZIP。导入后自动重建搜索索引。导入前会弹确认，同名文件将被覆盖。
- **模板笔记**：侧边栏「📋」标签进入模板中心，可新建/编辑/删除模板；模板支持 `{{date}} {{time}} {{datetime}} {{year}} {{month}} {{day}}` 变量，从模板新建笔记时自动替换为当前日期时间。模板文件持久化于 `.templates/`。
- **字数热力图**：侧边栏「📊」标签按日历热力图展示写作活跃度，颜色越深代表当天书写字数越多；点击某天格子查看当日总量，下方列出字数最多的笔记；按钮可切换近 120 / 365 天范围。
- **主题切换**：设置面板「主题」下拉切换原神（暗）/晨曦（亮）/樱粉（亮）/深蓝（暗）/森野（暗）五套配色，即时生效并持久化到 `config.json`，重启后保持。
- **图片 OCR**：在预览中右键或双击图片，选择「识别图片文字（OCR）」，需本地已安装 Tesseract-OCR 并含 `chi_sim` 中文语言包；识别结果自动插入当前笔记末尾。未安装 OCR 引擎时弹窗提示而非崩溃。

### 备份与恢复

**WebDAV**：增量上传变更文件，第二次同步极快。恢复时先预检本地/远程差异，逐文件选择「保留本地 / 使用远程 / 合并」（合并仅限文本文件，冲突处插入 `<<<<<<<` / `>>>>>>>` 标记），确认后执行。

**历史版本**：历史版本由本地 Git 仓库管理，保存变更自动建档，版本不限数量，可随时在历史版本面板查看旧内容并回退任意版本（需本机安装 Git）。

**ZIP**：导出全部笔记为 ZIP 时先弹出保存路径选择框，导出完成后 toast 提示具体位置；从 ZIP 导入恢复时自动校验路径安全并跳过垃圾文件。

---

## 常见问题

### WebDAV 同步失败 401

- 检查用户名/密码；若使用 `.env`，确保应用正确加载了环境变量。
- 部分网盘 WebDAV 路径已 URL 编码，程序会自动处理。

### 恢复后本地笔记被覆盖

- 恢复语义是「用备份替换当前内容」，会覆盖本地同名文件；恢复前请确认已备份重要内容。

### 历史版本不可用（Git 未安装）

- 历史版本依赖本地 Git 仓库。若本机未安装 Git，历史版本功能将不可用，但其余编辑、搜索、WebDAV 同步等功能不受影响。安装 Git 后重启应用即可。

### 打包后数据存在哪里？

- 打包版（安装包/便携版）的数据统一存放在 **exe 所在目录**（含 `config.json`、`notes/`、`logs/`、搜索索引、`.env`），Chromium 状态在 `<exe 目录>/userData`。整个文件夹拷走即完整数据，实现真便携；安装版数据也在安装目录内，卸载时会提示是否保留数据。

### 端口被占用 / 启动冲突

- 后端每次启动自动选取**随机空闲端口**并只监听 `127.0.0.1`，不会与 8000 等固定端口冲突；多实例由 Electron 单实例锁拦截。

### 移动项目后启动异常

- 若移动过项目文件夹，删除 `.venv/` 目录后重新运行 `uv sync` 即可重建虚拟环境；Electron 依赖若丢失，在 `electron/` 下重新 `npm install`。

### 公式/流程图无法渲染

- 确认 `static/vendor/katex/` 和 `static/vendor/mermaid/` 存在；若缺失，运行 `scripts/download_vendor.py` 重新下载。

### npm install 下载 Electron 失败（国内网络）

- 设置国内镜像后重装：
  ```powershell
  $env:ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/"
  npm install
  ```

---

## 开发与测试

```powershell
# 安装开发依赖（pytest / ruff / pyinstaller）
uv sync --extra dev

# 运行 pytest 冒烟套件
uv run pytest -q

# 运行 ruff 静态检查
uv run ruff check mdnotes scripts run_backend.py tests

# Electron 壳：开发模式启动（先 cd electron && npm install）
npm start

# Electron 壳：打包
npm run dist
```

当前已固化一批冒烟测试，覆盖：FTS5 特殊查询、增量索引重建、图片压缩、URL 掩码、文件名安全、路径穿越校验、PowerShell 引号转义、ZIP 路径安全、回收站恢复、文件对话框、ZIP 导出到指定路径、笔记属性等。

---

## 开源协议

MIT
