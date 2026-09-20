"""应用启动流程（bootstrap）。

把"读配置 → 装配路径 → 装配日志 → 装配 QGIS runtime → 登录校验 →
启动 QGIS → 注册插件 → 显示主窗口"这一线性流程集中到一处。所有跨模块
依赖只在这里组合一次，业务模块不需要知道 QGIS 路径从哪里来。

启动流程：

1. 读 :class:`app.env.config.AppConfig`
2. 装配 :class:`app.infra.fs.paths.Paths`
3. 装配 :class:`app.infra.logging.LogConfig` 并触发 ``_RootSink.configure``
4. 按 ``env`` 字段选择 :class:`QgisRuntime.for_dev` / ``for_prod``，
   调 :meth:`apply_env`（仅设环境变量，**不**初始化 QGIS）
5. 创建 :class:`QgsApplication`（**不**调 ``initQgis``），弹出登录对话框
   （每次启动都要登，账号密码提交到授权服务器换 token，不落盘）；
   此时**不**显示初始化状态页，否则两个居中浮窗会叠在一起
6. 登录成功才弹初始化状态页，并调 ``initQgis``、加载翻译、初始化 processing 插件（仅 dev）
7. 实例化主窗口、显示、依次执行启动回调（加载底图、弹出“打开地图文档”）、``exec_``
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from app.bootstrap.main_window import build_window
from app.core.auth import (
    AuthClient,
    AuthError,
    AuthResult,
    AuthServerConfig,
    fetch_current_user,
    load_auth_server_config,
)
from app.core.domain.basemap import add_base_tile_layer
from app.core.util.service_client import set_session_token
from app.env.config import AppConfig
from app.env.qgis_runtime import QgisRuntime
from app.infra.fs.paths import Paths
from app.infra.logging import get_logger, install_crash_logging
from app.infra.logging.config import LogConfig
from app.infra.logging.sink import _RootSink

logger = get_logger(__name__)

# 启动完成后的回调（签名 ``fn(window)``）：主窗口显示后统一执行。
_startup_callbacks: list = []


def register_startup_callback(fn) -> None:
    """注册一个启动回调；``main`` 显示主窗口后依次调用。"""
    _startup_callbacks.append(fn)


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


def main(argv: Optional[list[str]] = None) -> int:
    """应用入口。返回 ``app.exec()`` 的退出码。"""
    # 打包后直接双击 app.exe 也能工作：自动探测便携包并设环境变量
    _ensure_portable_env()
    config_path = _resolve_config_path()
    cfg = AppConfig.from_file(config_path)
    paths = _build_paths(cfg)
    paths.ensure()

    # 注册启动完成回调：按配置加载天地图 XYZ 底图
    register_startup_callback(_make_base_tile_callback(cfg))

    # 注册启动完成回调：主窗口显示后立即弹“打开地图文档”，让用户选择要打开的 .pgs
    register_startup_callback(_make_open_project_callback())

    _configure_logging(cfg, paths)

    # 崩溃日志：装配在日志目录确定之后、QGIS 初始化之前。
    # 之后任何未捕获 Python 异常（含 Qt 槽内）都会写 logs/crash.log，
    # 原生崩溃由 faulthandler 写 logs/crash_faulthandler.log。
    install_crash_logging(paths.logs)

    logger.info("应用启动 env=%s crs=%s", cfg.env, cfg.crs)
    logger.debug("Paths: %s", paths)

    qgis = _build_qgis_runtime(cfg)
    qgis.apply_env()
    logger.info("QGIS runtime applied: prefix=%s", qgis.qgis_prefix)

    # 创建 QApplication（不 initQgis，等登录通过再做）
    app = _ensure_qgis_application(qgis)

    # 先弹登录窗口：此时**不**显示初始化状态页。两者都是居中浮窗，
    # 一起开就会叠在一起（登录框上面压着 splash），体验很怪；而且登录
    # 阶段本来就短（一次 POST），登录框自己就有"正在登录"反馈。
    auth_result = _run_login_dialog(cfg)
    if auth_result is None:
        logger.info("登录取消，退出")
        return 0
    logger.info("登录成功 username=%s", auth_result.username)

    # 登录通过才弹初始化状态页：覆盖 initQgis → 翻译 → 插件注册 → 主窗口构建，
    # 主窗口显示时由 _run_qgis_app 关闭。
    splash = _start_splash(cfg.app_name)
    try:
        return _run_qgis_app(cfg, qgis, app, splash, auth_result=auth_result)
    finally:
        # 所有出口（含异常）都关掉初始化状态页，避免残留
        splash.close()


# --------------------------------------------------------------------- 私有


def _resolve_config_path() -> Path:
    """优先环境变量，其次仓库内 ``config/app.yaml``。"""
    env = os.environ.get("QGIS_DEV_CONFIG")
    if env:
        return Path(env)
    return _DEFAULT_CONFIG_PATH


def _ensure_portable_env() -> None:
    """打包后直接双击 ``app.exe`` 也能工作。

    PyInstaller 冻结态下，可执行文件所在目录即便携包根（``qgis/`` + ``proj.db``
    + ``etc/`` 等都在根下）。这里自动探测并设置 ``QGIS_PREFIX`` /
    ``QGIS_DEV_*``，已由 launcher 设置过则不覆盖（``setdefault``）。
    """
    if not getattr(sys, "frozen", False):
        return
    bundle = Path(sys.executable).resolve().parent
    # 便携包特征校验，避免误判
    if not (bundle / "qgis").is_dir() or not (bundle / "proj.db").is_file():
        return
    os.environ.setdefault("QGIS_PREFIX", str(bundle))
    os.environ.setdefault("QGIS_DEV_CONFIG", str(bundle / "config" / "app.yaml"))
    os.environ.setdefault("QGIS_DEV_PATHS_PROJECT_ROOT", str(bundle))
    os.environ.setdefault("QGIS_DEV_PATHS_RESOURCE", str(bundle / "resource"))
    os.environ.setdefault("QGIS_DEV_PATHS_LOG", str(bundle / "logs"))
    os.environ.setdefault("QGIS_DEV_PATHS_TMP", str(bundle / ".tmp"))
    os.environ.setdefault("QGIS_DEV_PATHS_CONFIG", str(bundle / "config"))


def _build_paths(cfg: AppConfig) -> Paths:
    """根据配置构造 :class:`Paths``，env 变量再次覆盖。"""
    base = Paths.default()
    if cfg.paths.resource:
        base = Paths(
            project_root=base.project_root,
            resources=cfg.paths.resource or base.resources,
            logs=cfg.paths.log or base.logs,
            tmp=cfg.paths.tmp or base.tmp,
            config=base.config,
        )
    return Paths.from_env(base)


def _configure_logging(cfg: AppConfig, paths: Paths) -> None:
    """写一份新 :class:`LogConfig` 并强制装配。"""

    log_cfg = LogConfig(
        log_dir=paths.logs,
        console_level=logging.getLevelName(cfg.logging.level),
        root_level=logging.getLevelName(cfg.logging.file_level),
        file_max_bytes=cfg.logging.rotation_mb * 1024 * 1024,
        file_backup_count=cfg.logging.backup_count,
    )
    # 强制让 sink 用新配置
    os.environ["QGIS_DEV_LOG_DIR"] = str(paths.logs)
    _RootSink.reset()
    _RootSink.configure(log_cfg)
    logger.info("日志装配完成 dir=%s", paths.logs)


def _build_qgis_runtime(cfg: AppConfig) -> QgisRuntime:
    # 便携打包：launcher 设置 QGIS_PREFIX 指向扁平 dist/app（或完整 QGIS 安装）
    prefix = os.environ.get("QGIS_PREFIX")
    if prefix:
        base = Path(prefix)
        if (base / "apps" / "qgis").exists():
            return QgisRuntime.for_prod()          # 完整 QGIS 安装布局（OSGeo4W）
        if (base / "proj.db").is_file():
            return QgisRuntime.for_portable(base)  # 扁平便携包
        if (base / "share" / "qgis").is_dir():
            return QgisRuntime.for_system(base)    # conda/apt 标准 Linux 布局
        # 无法识别布局：退回 dev 配置（含报错路径），保持旧行为
        logger.warning("QGIS_PREFIX 无法识别布局: %s", base)
    if cfg.env == "prod":
        return QgisRuntime.for_prod()
    if cfg.qgis.install_path is None or not cfg.qgis.install_path.exists():
        # Linux 上 config 的 install_path 是 Windows 路径（D:/... 不存在）
        # → 自动探测 conda/系统 QGIS，免去手设环境变量
        detected = _detect_linux_qgis()
        if detected is not None:
            logger.info(
                "config qgis.install_path 无效（%s），自动探测到 QGIS: %s",
                cfg.qgis.install_path,
                detected.qgis_base,
            )
            return detected
    if cfg.qgis.install_path is None:
        raise RuntimeError(
            "dev 模式需要 config/app.yaml 里 qgis.install_path"
        )
    return QgisRuntime.for_dev(cfg.qgis.install_path)


def _detect_linux_qgis() -> Optional[QgisRuntime]:
    """Linux 下自动探测 QGIS（conda 环境优先，其次系统 apt 安装）。

    只在 config 的 install_path 无效（如 Windows 路径）且未设 QGIS_PREFIX
    时调用；探测成功返回 :class:`QgisRuntime`（标准 Linux 布局），否则 None。
    """
    if os.name == "nt":
        return None
    candidates: list[Path] = []
    env = os.environ.get("CONDA_PREFIX")
    if env:
        candidates.append(Path(env))
    for base in (
        Path.home() / "miniconda3" / "envs",
        Path.home() / "anaconda3" / "envs",
        Path("/opt/conda/envs"),
        Path("/opt/miniconda3/envs"),
        Path("/opt/anaconda3/envs"),
    ):
        candidates.append(base / "qgis_new")
    candidates.append(Path("/usr"))  # apt 安装的系统 QGIS
    seen: set[Path] = set()
    for base in candidates:
        if base in seen or not base.is_dir():
            continue
        seen.add(base)
        # 标准 Linux 布局校验：share/qgis（python 包）+ share/proj（proj.db）
        if (base / "share" / "qgis").is_dir() and (
            base / "share" / "proj" / "proj.db"
        ).is_file():
            return QgisRuntime.for_system(base)
    return None


def _ensure_qgis_application(qgis: QgisRuntime):
    """创建（复用） :class:`QgsApplication`，但不调 ``initQgis``。

    登录阶段只需要一个能弹出 :class:`QDialog` 的 Qt 应用；QGIS 提供商
    注册等重活在登录成功后才执行。
    """
    from qgis.core import QgsApplication

    _argv = [a.encode("utf-8") if isinstance(a, str) else a for a in sys.argv]
    app = QgsApplication.instance()
    if app is None:
        app = QgsApplication(_argv, True)
    app.setPrefixPath(str(qgis.qgis_prefix), True)

    # 全局控件配色：弹窗 / 右键菜单 / ToolTip 等顶层窗口只有 QApplication 级
    # QSS 能覆盖。这里提前实例化 ThemeManager，把保存的主题设到全局，
    # 让登录弹窗等出现在主窗口之前的顶层窗口也能用上主题。
    from app.ui.common.theme import ThemeManager

    ThemeManager.instance()
    return app


def _run_login_dialog(cfg: AppConfig) -> AuthResult | None:
    """弹登录对话框；返回登录结果，或 ``None``（取消 / 关闭窗口）。

    账号校验走**授权服务器**（地址按 ``app.yaml`` 的 ``env`` 选段，见
    :data:`app.core.auth.config.PROFILES`）；token 只在本次运行内存里，
    **不写本地凭证**——每次启动都要登录。

    登录通过后会再用 token 拉一次当前用户信息（姓名 / 单位 / 部门，主界面标题栏
    要显示），见 :func:`_with_current_user`；同时把 token 登记给业务接口工具
    :mod:`app.core.util.service_client`，此后业务请求自动带 ``unitkey`` 头。
    """
    from qgis.PyQt.QtWidgets import QDialog

    from app.ui.dialogs.login import LoginDialog

    # 服务器地址在这里解析一次（不依赖对话框内部默认值），日志里能直接看到打到了哪台机
    server = load_auth_server_config(cfg.env)
    logger.info("授权服务器（环境 %s）：%s", cfg.env, server.describe())
    client = AuthClient(server)
    dlg = LoginDialog(app_name=cfg.app_name, auth_client=client)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        logger.info("用户取消登录")
        return None
    result = dlg.auth_result()
    if result is None:  # 理论上不会：对话框接受就有结果
        logger.warning("登录对话框已接受但没拿到登录结果，按取消处理")
        return None
    logger.info("授权服务器校验通过 username=%s", dlg.username())
    # 登记给业务接口工具（app.core.util.service_client.request()），后续自动带 token 头
    set_session_token(result.token)
    return _with_current_user(server, result)


def _with_current_user(server: AuthServerConfig, result: AuthResult) -> AuthResult:
    """登录成功后补拉当前用户信息（``currentUserByToken``，走业务接口工具）。

    **拉不到不拦登录**：只记一条 warning，标题栏回退显示登录账号（见
    :meth:`app.ui.main_window.mixins.chrome.ChromeMixin._apply_user_info_to_titlebar`）。
    这里同步调用（一次 GET，超时与登录一致），拿到的信息随 :class:`AuthResult`
    一路传给主窗口，不另外落盘。
    """
    try:
        user = fetch_current_user(result.token, config=server)
    except AuthError as exc:
        logger.warning("获取当前用户信息失败，标题栏回退显示登录账号：%s", exc)
        return result
    logger.info(
        "当前用户：%s（账号 %s，%s）",
        user.display_name(),
        user.account or result.username,
        user.organize_line() or "未返回单位/部门",
    )
    if user.need_reset_password:
        logger.warning("服务端提示该账号需要重置密码：%s", user.account or result.username)
    # AuthResult 是 frozen 的，用 replace 挂上用户信息（其余字段原样保留）
    return replace(result, user=user)


def _start_splash(app_name: str):
    """创建并显示启动初始化状态页，返回实例。

    **登录成功之后才调**（见 :func:`main`）：登录框与状态页都是居中浮窗，
    同时开场会叠在一起。这里只覆盖登录之后的 ``initQgis`` / 翻译 /
    插件注册 / 主窗口构建。

    :param app_name: 应用名（``app.yaml`` → ``app.name``），用作 splash 标题。
    """
    from app.ui.common.splash import StartupSplash

    splash = StartupSplash(app_name=app_name)
    splash.center_on_screen()
    splash.show()
    return splash


def _run_qgis_app(
    cfg: AppConfig,
    qgis: QgisRuntime,
    app,
    splash=None,
    auth_result: AuthResult | None = None,
) -> int:
    """登录成功后才真正初始化 QGIS + 主窗口 + exec_。

    :param auth_result: 本次登录结果（用户名 / token），传给主窗口显示账号信息。
    """
    # 关键：initQgis 必须在登录通过之后
    if splash is not None:
        splash.set_status("正在初始化核心程序")
    app.initQgis()

    # 翻译：仅在 dev 模式装（prod 走 launcher 内置资源）
    if cfg.env == "dev":
        if splash is not None:
            splash.set_status("正在加载语言")
        from qgis.PyQt.QtCore import QLibraryInfo, QTranslator

        translator = QTranslator()
        translator.load("qgis_zh-Hans.qm", app.i18nPath())
        app.installTranslator(translator)

        # Qt 内置标准按钮（保存/丢弃/取消/是/否/确定…）走 qtbase 翻译；
        # qgis_zh-Hans 只管 QGIS 自带文案，不翻译这些按钮，不装的话弹窗里
        # 全是英文（如流程窗口关闭时的 Save / Discard / Cancel）。
        qt_translator = QTranslator()
        # Qt 6：QLibraryInfo.path(LibraryPath.TranslationsPath)；
        # Qt 5：QLibraryInfo.location(TranslationsPath)。qgis.PyQt 不提供 path shim，需双写。
        try:
            qt_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        except AttributeError:  # PyQt5 / Qt5
            qt_path = QLibraryInfo.location(QLibraryInfo.TranslationsPath)
        if qt_translator.load("qtbase_zh_CN.qm", qt_path):
            app.installTranslator(qt_translator)

    # processing 插件：dev / prod 都需要（瓦片、缓冲等工具依赖 processing.run）。
    # 必须在 initQgis 之后调用；打包环境漏掉会导致 run 是 stub，
    # 报 "Processing plugin has not been loaded"。
    if splash is not None:
        splash.set_status("正在加载处理工具箱")
    qgis.init_plugins()

    # 用户自定义表达式函数（QGIS 函数编辑器里写的 @qgsfunction）：本应用不走
    # QGIS 启动器，qgis.user 不会被自动 import，不补这步表达式构建器里就
    # 看不到也用不了这些函数。详见 app/env/user_expressions.py。
    from app.env.user_expressions import load_user_expression_functions

    load_user_expression_functions()

    # 4) 主窗口（阶段 4 实现新窗口；阶段 1-3 临时仍用旧类）

    if splash is not None:
        splash.set_status("正在构建主窗口")
    window = build_window(app_name=cfg.app_name, auth_result=auth_result)

    # 主窗口就绪：先关掉初始化状态页再显示，避免浮窗盖在主窗口上
    if splash is not None:
        splash.close()
    window.show()
    _run_startup_callbacks(window)
    return app.exec()


def _make_base_tile_callback(cfg: AppConfig):
    """构造"启动完成后加载配置底图"的回调。"""

    def _cb(window) -> None:

        layer = add_base_tile_layer(cfg.map)
        if layer is None:
            logger.warning("未加载配置底图（未启用或图层无效）")
        else:
            logger.info("已加载配置底图：%s", layer.name())

    return _cb


def _make_open_project_callback():
    """构造“主窗口显示后立即弹出打开地图文档对话框”的启动回调。

    登录校验通过、主窗口 ``show()`` 后由 :func:`_run_startup_callbacks` 执行。
    弹 ArcMap 风格的 :class:`~app.ui.dialogs.startup_project.StartupProjectDialog`
    ——最近工程列表 + 浏览 + 新建，替代系统文件选择框：选中已有工程走主窗口
    :meth:`ProjectOpsMixin.open_project_path`，选“新建工程”走
    :meth:`ProjectOpsMixin._on_new_project`，取消则停留在空工作区（先前回调
    已加载的底图不受影响）。回调本身失败只记日志，不阻断启动。
    """

    def _cb(window) -> None:
        from qgis.PyQt.QtWidgets import QDialog

        from app.ui.dialogs.startup_project import StartupProjectDialog

        opener = getattr(window, "open_project_path", None)
        creator = getattr(window, "_on_new_project", None)
        if opener is None and creator is None:  # 旧窗口无这些动作时静默跳过
            logger.warning("主窗口缺少工程操作动作，跳过启动选择工程")
            return

        logger.info("启动完成，弹出“打开地图文档”对话框")
        dlg = StartupProjectDialog(
            window, app_name=getattr(window, "_app_name", None) or "qgis_dev"
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            logger.info("启动未选择工程，停留在空工作区")
            return
        if dlg.create_new():
            if creator is None:
                logger.warning("主窗口缺少 _on_new_project，跳过新建工程")
                return
            logger.info("启动选择：新建工程")
            creator()
            return
        path = dlg.selected_path()
        if not path:
            return
        if opener is None:
            logger.warning("主窗口缺少 open_project_path，跳过打开工程")
            return
        logger.info("启动选择：打开工程 %s", path)
        opener(path)

    return _cb


def _run_startup_callbacks(window) -> None:
    """依次执行已注册的启动回调；单个失败不影响主流程。"""
    for fn in _startup_callbacks:
        try:
            fn(window)
        except Exception:  # noqa: BLE001
            logger.exception("启动回调执行失败：%s", getattr(fn, "__name__", repr(fn)))


__all__ = ["main", "register_startup_callback"]
