# logger_config.py
import logging
from logging.handlers import RotatingFileHandler
import os

# 确保日志目录存在
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# 全局集合，用于跟踪哪些 logger 名称已经被配置过
_CONFIGURED_LOGGERS = set()

LOG_LEVEL_STYLE_MAP = {
    "INFO": {
        "color": "#008000",  # 绿色
        "bold": False
    },
    "ERROR": {
        "color": "#ff0000",  # 红色
        "bold": True
    },
    "WARNING": {
        "color": "#ff8c00",  # 橙色
        "bold": False
    },
    "DEBUG": {
        "color": "#4169e1",  # 蓝色
        "bold": False
    },
    "CRITICAL": {
        "color": "#8b0000",  # 深红色
        "bold": False
    }
}
def log_to_html(log_str):
    """
    将原始日志字符串（按 formatter 格式输出）转换为 HTML 字符串
    :param log_str: 原始日志字符串（如 "2025-12-31 16:00:00 - __INFO__ - 程序启动"）
    :return: HTML 格式字符串
    """
    # 提取日志级别（根据你的 formatter 格式，级别被包裹在 __xxx__ 中）
    level_start = log_str.find("__") + 2
    level_end = log_str.rfind("__")
    log_level = log_str[level_start:level_end].upper() if (level_start > 1 and level_end > level_start) else "INFO"

    # 获取对应样式
    style_info = LOG_LEVEL_STYLE_MAP.get(log_level, LOG_LEVEL_STYLE_MAP["INFO"])
    font_color = style_info["color"]
    is_bold = style_info["bold"]

    # 替换原始日志中的 __级别__ 为带样式的 HTML 标签（可选，优化视觉效果）
    # 方案1：保留所有字段，仅为整个日志行设置样式
    html_line = f'<p style="line-height: 1.5; margin: 2px 0;">'
    if is_bold:
        html_line += f'<font color="{font_color}"><b>{log_str}</b></font>'
    else:
        html_line += f'<font color="{font_color}">{log_str}</font>'
    html_line += '</p>'

    # 方案2：精细化样式（不同字段不同样式，更美观，推荐）
    # 拆分日志字段（按你的 formatter 格式拆分，可根据实际情况调整）
    try:
        parts = log_str.split(" - ")
        if len(parts) >= 5:
            log_time = parts[0]
            log_name = parts[1]
            log_pid_thread = parts[2]
            log_level_raw = parts[3]
            log_msg = " - ".join(parts[4:])

            # 拼接精细化 HTML
            html_line = f'<p style="line-height: 1.5; margin: 2px 0;">'
            # 时间：灰色
            html_line += f'<font color="#696969">{log_time}</font> - '
            # 模块名：深灰色
            html_line += f'<font color="#2f4f4f">{log_name}</font> - '
            # 进程/线程ID：浅灰色
            html_line += f'<font color="#778899">{log_pid_thread}</font> - '
            # 级别：对应颜色+加粗
            if is_bold:
                html_line += f'<font color="{font_color}"><b>{log_level_raw}</b></font> - '
            else:
                html_line += f'<font color="{font_color}">{log_level_raw}</font> - '
            # 消息：默认黑色
            html_line += f'<font color="#000000">{log_msg}</font>'
            html_line += '</p>'
    except:
        # 拆分失败时，使用方案1的兜底样式
        pass

    return html_line

def setup_logger(name=None):
    """
    配置项目日志，支持保留模块名称。

    Args:
        name (str, optional): logger 的名称。如果为 None 或 "__main__"，则使用默认名称 "Application"。
                             通常在模块中调用时传入 `__name__`。

    Returns:
        logging.Logger: 配置好的 logger 实例。
    """
    # 处理 name 为 None 或 "__main__" 的情况，给一个统一的默认名
    if name is None or name == "__main__":
        name = "Application"

    # 获取 logger 实例
    logger = logging.getLogger(name)

    # 关键：检查这个特定名称的 logger 是否已经配置过
    if name in _CONFIGURED_LOGGERS:
        return logger

    # 标记这个名称的 logger 为已配置
    _CONFIGURED_LOGGERS.add(name)

    # 配置 logger
    logger.setLevel(logging.DEBUG)

    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(process)d:%(thread)d - __%(levelname)s__ - %(message)s",

        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # 2. 错误日志处理器
    error_file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(formatter)

    # 3. 全部日志处理器
    all_file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    all_file_handler.setLevel(logging.DEBUG)
    all_file_handler.setFormatter(formatter)

    # 添加处理器
    logger.addHandler(console_handler)
    logger.addHandler(error_file_handler)
    logger.addHandler(all_file_handler)

    # 阻止日志向上级传播，避免重复
    logger.propagate = False

    return logger