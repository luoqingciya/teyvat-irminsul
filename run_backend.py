"""Teyvat Irminsul（提瓦特世界树）Electron 混合架构后端 sidecar 入口。

PyInstaller 的 __main__ 上下文下，包内相对导入会失败；
本模块在正确的包路径下调用 electron_server.main()，避免该问题。

Electron 主进程拉起本程序后，从 stdout 读取 ``PORT=<port>`` 建立连接。
"""

from mdnotes.electron_server import main

if __name__ == "__main__":
    main()
