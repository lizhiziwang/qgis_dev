"""客户端自动更新链路（检查 → 验签 → 下载 → 安装）。

模块划分（全部 Qt-free，方便单测与无界面调用）：

- :mod:`app.update.checker`：拉取清单并用内置公钥验签
- :mod:`app.update.downloader`：支持 HTTP Range 断点续传的包下载
- :mod:`app.update.state_store`：每用户更新状态（最后一次检查 / 跳过版本 / 待安装包）
- :mod:`app.update.launcher`：把安装任务交给独立更新器进程
- :mod:`app.update.service`：把上面几块串成 UI 可直接调用的一步操作

UI 入口在 :mod:`app.ui.update`（后台线程 + 对话框）。
"""
