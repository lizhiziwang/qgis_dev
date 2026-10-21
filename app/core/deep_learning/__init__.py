"""深度学习统一依赖目录（标注 / 训练 / 推理）。"""

from app.core.deep_learning.environment import (
    ALL_USAGES,
    DEEP_LEARNING_DEPENDENCIES,
    USAGE_TITLES,
    build_install_commands,
    build_probe_command,
    parse_probe_output,
    probe_script,
    spec_by_key,
    specs_for_usage,
)

__all__ = [
    "ALL_USAGES",
    "DEEP_LEARNING_DEPENDENCIES",
    "USAGE_TITLES",
    "build_install_commands",
    "build_probe_command",
    "parse_probe_output",
    "probe_script",
    "spec_by_key",
    "specs_for_usage",
]
