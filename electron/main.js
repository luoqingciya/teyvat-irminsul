/* Teyvat Irminsul · Electron 主进程
 *
 * 职责：
 *  1. 拉起 Python 后端 sidecar（run_backend.py / 打包后的 Backend.exe），
 *     从 stdout 解析随机端口
 *  2. 创建主窗口（加载 mdnotes/webui/index.html），关闭时最小化到托盘
 *  3. 系统托盘：显示 / 隐藏 / 闪念笔记 / 退出
 *  4. 单实例锁 + 二次启动唤起窗口
 *  5. 通过 preload 桥接原生能力（对话框、开机自启、闪念监听）
 */

const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, nativeImage, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');

const IS_DEV = !app.isPackaged;

// 随机访问令牌：随环境变量传给后端，前端经 preload 取回并放在 X-Auth-Token 请求头。
// 本地恶意网页拿不到该令牌，从而无法读写笔记（防 DNS rebinding / CSRF 类攻击）。
const apiToken = crypto.randomBytes(32).toString('hex');

// 便携式数据布局：打包后所有数据都落在应用可执行文件所在目录，整个文件夹拷走即完整便携。
//  - Chromium 状态（Local Storage / 缓存 / 首选项）→ <exe 目录>/userData
//  - 后端数据（config.json / .env / notes / logs / 搜索索引）→ <exe 目录>
// 必须在单实例锁与任何 userData 读取之前设置。
const appDataDir = path.dirname(process.execPath);
if (!IS_DEV) {
  app.setPath('userData', path.join(appDataDir, 'userData'));
}

let backendPort = null;
let backendProc = null;
let mainWindow = null;
let tray = null;
let quitting = false;

// ── 单实例：已有实例时唤起旧窗口并退出 ──────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showMainWindow();
  });
}

// ── 路径 ────────────────────────────────────────────────

// 前端资源根目录（webui/ 与 static/ 同级）。
// 开发：直接指向源码 mdnotes/；打包：指向 resources/（extraResources 复制）。
function frontendBase() {
  return IS_DEV ? path.join(__dirname, '..', 'mdnotes') : process.resourcesPath;
}

// Python 后端启动命令
function backendCommand() {
  if (IS_DEV) {
    return {
      cmd: path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe'),
      args: [path.join(__dirname, '..', 'run_backend.py')],
    };
  }
  return {
    cmd: path.join(process.resourcesPath, 'backend', 'TeyvatIrminsulBackend.exe'),
    args: [],
  };
}

// ── 后端进程 ────────────────────────────────────────────

function spawnBackend() {
  return new Promise((resolve, reject) => {
    const { cmd, args } = backendCommand();
    let buf = '';
    let proc;
    const env = { ...process.env, MDNOTES_API_TOKEN: apiToken };
    // 打包后后端位于只读的 resources/backend/，数据目录指向应用可执行文件所在目录（便携式）
    if (!IS_DEV) env.MDNOTES_DATA_DIR = appDataDir;
    try {
      proc = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true, env });
    } catch (err) {
      reject(new Error('后端启动失败: ' + err.message));
      return;
    }
    backendProc = proc;
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      const m = buf.match(/PORT=(\d+)/);
      if (m && !backendPort) {
        backendPort = parseInt(m[1], 10);
        resolve(backendPort);
      }
    });
    proc.stderr.on('data', (d) => {
      const s = d.toString().trim();
      if (s) console.error('[backend]', s);
    });
    proc.on('error', (err) => reject(new Error('后端启动失败: ' + err.message)));
    proc.on('exit', (code) => {
      if (!backendPort) reject(new Error('后端提前退出, code=' + code));
      else if (!quitting && code !== 0) {
        // 运行期间后端异常退出：提示用户，避免静默失联
        dialog.showErrorBox('后端异常退出', '后端进程已退出（code=' + code + '）。\n请关闭应用后重新启动。');
      }
    });
  });
}

function killBackend() {
  return new Promise((resolve) => {
    const proc = backendProc;
    if (!proc || proc.killed) { resolve(); return; }
    const killer = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) { /* 忽略 */ }
      resolve();
    }, 3000);
    proc.once('exit', () => { clearTimeout(killer); resolve(); });
    try { proc.kill(); } catch (_) { /* 忽略 */ }
  });
}

// ── 窗口 / 托盘 ─────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 720,
    minHeight: 520,
    title: 'Teyvat Irminsul · 提瓦特世界树',
    icon: path.join(frontendBase(), 'static', 'icon.ico'),
    backgroundColor: '#0f1520',
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  mainWindow.loadFile(path.join(frontendBase(), 'webui', 'index.html'));
  mainWindow.once('ready-to-show', () => { mainWindow.show(); });

  // 安全：外链一律交给系统浏览器，禁止在应用窗口内导航（防应用被"导航走"）
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (/^https?:/i.test(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  // 关闭窗口 → 最小化到托盘（不退出进程）
  mainWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function showMainWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  try {
    const icon = nativeImage.createFromPath(path.join(frontendBase(), 'static', 'icon.ico'));
    tray = new Tray(icon);
    tray.setToolTip('Teyvat Irminsul · 提瓦特世界树');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: '显示窗口', click: showMainWindow },
      { label: '隐藏到托盘', click: () => mainWindow && mainWindow.hide() },
      {
        label: '✏ 闪念笔记',
        click: () => {
          showMainWindow();
          if (mainWindow) mainWindow.webContents.send('flash-note');
        },
      },
      { type: 'separator' },
      { label: '退出', click: () => { quitting = true; app.quit(); } },
    ]));
    tray.on('click', showMainWindow);
  } catch (err) {
    console.error('托盘创建失败:', err);
  }
}

// ── IPC（渲染进程桥接） ─────────────────────────────────

ipcMain.handle('api-base', () => {
  if (!backendPort) return null; // 后端未就绪（正常时序不会发生）
  return 'http://127.0.0.1:' + backendPort;
});
ipcMain.handle('api-token', () => apiToken);

ipcMain.handle('dialog-folder', async (_e, initial) => {
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory', 'createDirectory'],
    defaultPath: initial || undefined,
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});

ipcMain.handle('dialog-save', async (_e, name) => {
  const r = await dialog.showSaveDialog(mainWindow, {
    defaultPath: name || 'mdnotes_export.zip',
  });
  return r.canceled || !r.filePath ? null : r.filePath;
});

// 开机自启（打包后指向当前安装的 exe）
ipcMain.handle('startup-enabled', () => app.getLoginItemSettings().openAtLogin);
ipcMain.handle('startup-toggle', () => {
  const cur = app.getLoginItemSettings().openAtLogin;
  app.setLoginItemSettings({ openAtLogin: !cur });
  return !cur;
});

ipcMain.on('app-quit', () => { quitting = true; app.quit(); });

// ── 生命周期 ────────────────────────────────────────────

app.whenReady().then(async () => {
  try {
    await spawnBackend();
  } catch (err) {
    dialog.showErrorBox('后端启动失败', String((err && err.message) || err));
    app.quit();
    return;
  }
  createWindow();
  createTray();
});

// 退出前等待后端进程真正退出（带超时），避免残留进程
let backendStopped = false;
app.on('before-quit', (e) => {
  if (backendProc && !backendProc.killed && !backendStopped) {
    e.preventDefault();
    backendStopped = true;
    killBackend().then(() => app.quit());
  }
});

// 托盘常驻：窗口关闭不退出应用
app.on('window-all-closed', () => { /* no-op */ });
