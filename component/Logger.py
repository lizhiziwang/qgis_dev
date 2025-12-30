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
        "%(asctime)s - %(name)s - %(process)d:%(thread)d - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # 2. 错误日志处理器
    error_file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "error.log"),
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
