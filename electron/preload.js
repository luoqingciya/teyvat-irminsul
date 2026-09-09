/* Teyvat Irminsul · 预加载脚本
 *
 * 在渲染进程通过 contextBridge 暴露最小化的原生能力：
 *  - apiBase:        返回后端 HTTP 服务地址
 *  - pickFolder:     选择文件夹（设置笔记目录 / 导入 / 导出站点）
 *  - pickSaveFile:   选择保存位置（导出 ZIP）
 *  - isStartupEnabled / toggleStartup: 开机自启
 *  - onFlashNote:    托盘「闪念笔记」事件监听
 *  - quit:           请求退出应用
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  apiBase: () => ipcRenderer.invoke('api-base'),
  apiToken: () => ipcRenderer.invoke('api-token'),
  pickFolder: (initial) => ipcRenderer.invoke('dialog-folder', initial || ''),
  pickSaveFile: (name) => ipcRenderer.invoke('dialog-save', name || ''),
  isStartupEnabled: () => ipcRenderer.invoke('startup-enabled'),
  toggleStartup: () => ipcRenderer.invoke('startup-toggle'),
  onFlashNote: (cb) => {
    ipcRenderer.removeAllListeners('flash-note');
    ipcRenderer.on('flash-note', () => cb());
  },
  quit: () => ipcRenderer.send('app-quit'),
});
