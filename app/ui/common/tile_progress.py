"""影像切片进度文字：把算法日志换算成「元瓦片 n/m + 缩放级别」。

``native:tilesxyzdirectory`` / ``native:tilesxyzmbtiles`` 的进度条只按
**元瓦片**（``METATILESIZE`` × ``METATILESIZE`` 个瓦片一批）推进，默认
10×10 时一次小任务可能只有 0→100 两个事件。算法日志里则明确给出了每个
缩放级别的元瓦片数：

    INFO 1 metatiles (2 tiles) will be created for zoom level 8
    INFO A total of 7 metatiles (123 tiles) will be created

本模块解析这两类日志，结合 ``progressChanged`` 百分比给出可读进度
（如「元瓦片 3/7 · 缩放级别 9 · 43%」），并保留完整日志供任务日志框显示。
解析同时兼容英文与中文译文；没有日志时不臆造数字。
"""

from __future__ import annotations

import re
import time

#: 单个缩放级别的日志（英文 / 中文）
_ZOOM_EN = re.compile(
    r"(\d+)\s*metatiles?\s*\((\d+)\s*tiles?\).*?zoom\s*level\s*(\d+)",
    re.IGNORECASE,
)
_ZOOM_ZH = re.compile(
    r"缩放级别\s*(\d+).*?(\d+)\s*个?\s*(?:元瓦片|元切片).*?(\d+)\s*个?\s*(?:瓦片|切片)"
)
#: 中文译文的另一种语序：「1 个元瓦片（2 个瓦片）… 缩放级别 8」
_ZOOM_ZH_REV = re.compile(
    r"(\d+)\s*个?\s*(?:元瓦片|元切片).*?(\d+)\s*个?\s*(?:瓦片|切片).*?缩放级别\s*(\d+)"
)

#: 全部缩放级别合计（英文 / 中文）
_TOTAL_EN = re.compile(
    r"total\s+of\s*(\d+)\s*metatiles?\s*\((\d+)\s*tiles?\)",
    re.IGNORECASE,
)
#: 中文合计；QGIS 4.2 的实际译文是「将共创建5元切片 (5切片)」，
#: 同时兼容「总共将创建 7 个元瓦片（123 个瓦片）」等旧译文。
_TOTAL_ZH = re.compile(
    r"共[^\d\n]{0,16}(\d+)\s*个?\s*(?:元瓦片|元切片)[^\d\n]{0,8}(\d+)\s*个?\s*(?:瓦片|切片)"
)


class MetatileProgressTracker:
    """从算法日志累计元瓦片计划，并把百分比换算成可读状态。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """开始新一轮任务前清空计划。"""
        #: [(缩放级别, 元瓦片数)]，按日志出现顺序
        self.zoom_plan: list[tuple[int, int]] = []
        self.total_metatiles = 0
        self.total_tiles = 0
        self.last_progress_text = ""
        #: 任务开始时间（``time.monotonic()``）；未开始为 ``None``
        self._started_at: float | None = None

    def start_clock(self) -> None:
        """开始计时：状态文字会附带「已用 mm:ss」。

        大范围影像切片的第一个元瓦片可能渲染数分钟，进度回调停留在 0%。
        显示已用时间能让用户确认任务仍在推进，而不是卡死。
        """
        self._started_at = time.monotonic()

    def stop_clock(self) -> None:
        self._started_at = None

    @property
    def elapsed_seconds(self) -> int:
        if self._started_at is None:
            return 0
        return max(0, int(time.monotonic() - self._started_at))

    @staticmethod
    def format_elapsed(seconds: int) -> str:
        """``75`` → ``01:15``；超过 1 小时 → ``1:02:03``。"""
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    # ------------------------------------------------------------ 日志解析

    def feed_log(self, _level: str, message: str) -> bool:
        """喂一行算法日志；识别到计划行返回 ``True``。"""
        text = str(message or "").strip()
        if not text:
            return False

        match = _TOTAL_EN.search(text) or _TOTAL_ZH.search(text)
        if match:
            self.total_metatiles = int(match.group(1))
            self.total_tiles = int(match.group(2))
            return True

        match = _ZOOM_EN.search(text)
        if match:
            metatiles, tiles, zoom = (int(g) for g in match.groups())
        else:
            match = _ZOOM_ZH.search(text)
            if match:
                zoom, metatiles, tiles = (int(g) for g in match.groups())
            else:
                match = _ZOOM_ZH_REV.search(text)
                if not match:
                    return False
                metatiles, tiles, zoom = (int(g) for g in match.groups())

        if all(z != zoom for z, _count in self.zoom_plan):
            self.zoom_plan.append((zoom, metatiles))
        # 合计行缺失时用逐级数据兜底
        if not self.total_metatiles:
            self.total_metatiles = sum(count for _z, count in self.zoom_plan)
        if not self.total_tiles:
            self.total_tiles += tiles
        return True

    def feed_progress_text(self, text: str) -> None:
        """记录算法 ``setProgressText`` 的文字（当前算法未使用）。"""
        self.last_progress_text = str(text or "").strip()

    # ------------------------------------------------------------ 展示

    def describe(self, value: int) -> str:
        """把 0~100 的进度值描述成「元瓦片 n/m · 缩放级别 z · p%」。"""
        value = max(0, min(100, int(value)))
        if self.total_metatiles <= 0:
            text = f"正在准备瓦片… {value}%" if value else "正在准备瓦片…"
            return self._with_elapsed(text)

        done = min(
            self.total_metatiles,
            round(value / 100.0 * self.total_metatiles),
        )
        parts = [f"元瓦片 {done}/{self.total_metatiles}"]
        zoom = self.zoom_for(done)
        if zoom is not None:
            parts.append(f"缩放级别 {zoom}")
        parts.append(f"{value}%")
        return self._with_elapsed(" · ".join(parts))

    def _with_elapsed(self, text: str) -> str:
        if self._started_at is None:
            return text
        return f"{text} · 已用 {self.format_elapsed(self.elapsed_seconds)}"

    def zoom_for(self, done_metatiles: int) -> int | None:
        """已完成 ``done`` 个元瓦片时，当前处理的缩放级别。"""
        if not self.zoom_plan:
            return None
        completed = 0
        for zoom, count in self.zoom_plan:
            if done_metatiles <= completed + count:
                return zoom
            completed += count
        return self.zoom_plan[-1][0]

    def tooltip(self) -> str:
        """状态标签的悬停说明：总数与逐级计划。"""
        if self.total_metatiles <= 0:
            return "等待算法报告瓦片计划"
        text = f"共 {self.total_metatiles} 个元瓦片"
        if self.total_tiles:
            text += f"（{self.total_tiles} 个瓦片）"
        if self.zoom_plan:
            plan = "，".join(f"{z} 级 {count} 个" for z, count in self.zoom_plan)
            text += f"\n按缩放级别：{plan}"
        return text


__all__ = ["MetatileProgressTracker"]
