"""节点编辑（顶点级几何编辑）工具。

编辑工具条「节点编辑」按钮激活的交互工具：**点击矢量要素即可编辑其几何图形**
（顶点级编辑，与旧 :class:`component.map_tool.FindFeatureTool` 无关）：

- **左键单击**要素 → 载入该要素几何：红色描边 + 青色顶点手柄；
  多边形的闭合重复顶点不显示手柄（避免与首顶点重复）；
- **左键按住拖动顶点** → 实时预览几何形变，松开鼠标把几何写回要素
  （一次拖拽 = 一个可整体撤销的编辑命令 ``changeGeometry``）；
- **左键双击边** → 在点击位置插入一个新顶点；
- **右键点击顶点** → 删除该顶点（线至少保留 2 个顶点；面环至少保留
  4 个顶点、含闭合点，防止删成非法环）；
- **点击空白处** → 退出当前要素编辑（清空高亮与手柄）；
- **Esc** → 退出工具；
- **Delete** → 删除当前已载入（选中）的要素（需处于编辑态；整体一个可撤销命令）。

支持点 / 线 / 面（含多部件、洞），几何坐标一律以**图层 CRS** 为基准：
命中测试在画布像素空间做（容差 12px），落点换算成图层坐标后再改几何。

安全约束（仓库既定结论，见 ``swipe.py`` docstring）：不在 Qt 事件栈内调
``canvas.refresh()`` / ``unsetMapTool``，一律 ``QTimer.singleShot(0, ...)``
推迟出栈。顶点手柄是 :class:`QgsVertexMarker`（继承 ``QgsMapCanvasItem``
→ ``QGraphicsItem``，**非 QObject**、没有 ``deleteLater()``），释放时先
``hide()`` 并从场景移除，再 ``sip.delete``。

几何修改走编辑缓冲（要求图层处于编辑态）：``beginEditCommand`` 把每次
``changeGeometry`` 包成**一个**撤销步骤，可整体撤销。写回始终针对**原要素**
（同一 fid、属性不变，绝不新建要素/几何）；且每次手势前都从图层编辑缓冲
重读该要素**当前**几何作为基准（见 :meth:`_sync_from_layer`），外部撤销/重做
或其它工具改过几何后，以图层现状为准——不把本地累积的过期快照覆盖回原要素。
"""

from __future__ import annotations

import math
from collections.abc import Callable

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsVertexId,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint, QPointF, Qt, QTimer
from qgis.PyQt.QtGui import QColor

from app.core.domain.check.query import intersect_features
from app.core.domain.check.space_index import clear_spatial_index
from app.infra.logging import get_logger

logger = get_logger(__name__)


class FeatureEditTool(QgsMapTool):
    """点击要素 → 编辑其几何的顶点级编辑工具（拖动 / 插入 / 删除顶点）。"""

    #: 命中容差（像素）：顶点 / 边 / 要素拾取共用
    TOLERANCE_PX = 12
    #: 顶点手柄尺寸（像素）
    MARKER_SIZE = 12
    #: 线（含多线每个部件）最少顶点数：删除后不能低于此数
    MIN_LINE_VERTICES = 2
    #: 面环（含闭合点）最少顶点数：删除后不能低于此数
    MIN_RING_VERTICES = 4

    def __init__(
        self,
        canvas,
        editing_layer_getter: Callable[[], QgsVectorLayer | None] = lambda: None,
        hint: Callable[[str], None] | None = None,
        visible_filter_getter=None,
    ) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._layer_getter = editing_layer_getter
        self._hint = hint or (lambda text: None)
        #: 渲染级可见性判定（选择屏蔽 / 范围裁剪）：``(layer) -> callable(fid)``
        #: 或 None。命中后只取当前可见的要素——否则会载入屏幕上根本看不见的
        #: 图形，用户表现为"点了没反应/拖了不更新"。
        self._visible_filter_getter = visible_filter_getter
        #: 正在编辑的图层（锁定；切换图层时清空状态，防跨层 fid 误改）
        self._layer: QgsVectorLayer | None = None
        #: 正在编辑的要素 fid
        self._fid: int | None = None
        #: 编辑中的几何模型（图层 CRS；move/insert/delete 原地修改）
        self._geom: QgsGeometry | None = None
        #: 几何类型（0 点 / 1 线 / 2 面）
        self._geom_type: QgsWkbTypes.GeometryType | None = None
        self._band: QgsRubberBand | None = None
        self._band_type: QgsWkbTypes.GeometryType | None = None
        #: 顶点手柄 [(marker, QgsVertexId), ...]
        self._markers: list[tuple[QgsVertexMarker, QgsVertexId]] = []
        #: 拖拽状态
        self._drag_vid: QgsVertexId | None = None
        self._drag_orig: QgsPoint | None = None
        #: 已挂 geometryChanged 的图层（收尾时断开）
        self._hooked_layer = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -------------------------------------------------- 工具生命周期

    def activate(self) -> None:
        self._init_band()
        super().activate()

    def deactivate(self) -> None:
        self._clear_overlay()
        super().deactivate()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            # 不在键盘事件栈内 unsetMapTool，推迟出栈
            QTimer.singleShot(0, self._quit)
        elif event.key() == Qt.Key.Key_Delete:
            self._delete_loaded_feature()
        else:
            super().keyPressEvent(event)

    def _quit(self) -> None:
        self._clear_overlay()
        try:
            self.canvas.unsetMapTool(self)
        except RuntimeError:
            pass

    # -------------------------------------------------- 交互

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        """左键按下：若命中当前要素的顶点手柄则准备拖拽。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._geom is None:
            return
        # 以图层当前几何为准重读模型（防外部撤销/重做/其它改动后用过期快照）
        if not self._sync_from_layer():
            return
        vid = self._hit_vertex(event)
        if vid is None:
            return
        self._drag_vid = vid
        # 快照原始顶点，松手时据此判断是否有位移
        self._drag_orig = self._geom.constGet().vertexAt(vid)

    def canvasMoveEvent(self, event) -> None:  # noqa: N802
        """拖拽中：实时移动顶点并刷新预览。"""
        if self._drag_vid is None or self._geom is None:
            return
        pt = self._layer_point_of_event(event)
        if pt is None:
            return
        self._move_vertex(self._drag_vid, pt)
        self._refresh_band()
        self._recenter_marker(self._drag_vid)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802
        """左键松开：结束拖拽（位移则提交）或点选要素；右键：删除顶点。"""
        button = event.button()
        if button == Qt.MouseButton.LeftButton:
            if self._drag_vid is not None:
                self._finish_drag()
            else:
                self._handle_click_select(event)
        elif button == Qt.MouseButton.RightButton:
            self._handle_right_click(event)

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802
        """左键双击：在命中的边上插入新顶点（双击顶点手柄不处理）。"""
        if event.button() != Qt.MouseButton.LeftButton or self._geom is None:
            return
        # 双击自己会先触发一次按下/松开（点选载入），此处只需处理落点不在顶点上的双击
        if not self._sync_from_layer():
            return
        if self._hit_vertex(event) is not None:
            return
        hit = self._hit_edge(event)
        if hit is None:
            return
        insert_vid, fraction = hit
        self._insert_vertex(insert_vid, fraction, event)

    # -------------------------------------------------- 点选与载入

    def _handle_click_select(self, event) -> None:
        """左键单击：点中要素则载入其几何；点空白则退出当前要素编辑。"""
        fid = self._pick_fid(event)
        if fid is None:
            if self._fid is not None:
                self._clear_overlay()
                self._hint("已退出节点编辑；点击要素重新载入，Esc 退出工具")
            return
        if self._fid == fid:
            return  # 已在编辑该要素，忽略重复点击
        self._load_feature(fid)

    def _handle_right_click(self, event) -> None:
        """右键：命中顶点手柄则删除该顶点（带最小顶点数保护）。"""
        if self._geom is None:
            return
        if not self._sync_from_layer():
            return
        vid = self._hit_vertex(event)
        if vid is None:
            return
        self._delete_vertex(vid)

    def _load_feature(self, fid: int) -> None:
        """载入要素几何进入编辑状态（替换当前已编辑要素）。"""
        layer = self._current_layer()
        if layer is None:
            return
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法编辑几何")
            return
        feature = layer.getFeature(fid)
        if not feature.isValid():
            return
        geom = feature.geometry()
        if geom is None or geom.isNull() or geom.isEmpty():
            self._hint("该要素没有有效几何，无法编辑")
            return
        self._clear_overlay()
        self._layer = layer
        self._fid = fid
        # 外部改动（撤销 / 重做 / 别的工具动了同一要素）要**立刻**反映到手柄与描边，
        # 不能等用户下次下手势才同步 —— 否则撤销后画布上还停在旧位置
        self._connect_layer_geometry(layer)
        # 拷贝一份作为编辑预览模型：原要素仍在图层里，提交时经 changeGeometry
        # 把修改写回**原 fid**（不新建要素、属性不变）。
        self._geom = QgsGeometry(geom)
        self._geom_type = QgsWkbTypes.GeometryType(layer.geometryType())
        self._init_band()
        self._refresh_overlay()
        try:
            layer.select([fid])
        except RuntimeError:
            pass
        kind = {0: "点", 1: "线", 2: "面"}.get(int(self._geom_type), "要素")
        self._hint(f"已载入{kind}要素几何：拖动顶点编辑，双击边加顶点，右键顶点删除，Esc 退出")

    def _pick_fid(self, event) -> int | None:
        """点击像素 → 命中要素 fid（12px 容差矩形求交；多要素命中取最上层）。"""
        layer = self._current_layer()
        if layer is None:
            return None
        px = self._pixel_of_event(event)
        if px is None:
            return None
        map_pt = self._map_point_of_px(px)
        if map_pt is None:
            return None
        tol = self.TOLERANCE_PX * self.canvas.mapSettings().mapUnitsPerPixel()
        rect = QgsRectangle(
            map_pt.x() - tol, map_pt.y() - tol, map_pt.x() + tol, map_pt.y() + tol
        )
        project = self.canvas.project() or QgsProject.instance()
        fids = intersect_features(
            layer,
            QgsGeometry.fromRect(rect),
            self.canvas.mapSettings().destinationCrs(),
            project,
        )
        # 过滤已删除/失效的要素（空间索引缓存可能含已删 fid）
        fids = [fid for fid in fids if layer.getFeature(fid).isValid()]
        # 选择屏蔽 / 范围裁剪是**渲染级**过滤：图形在数据里但屏幕上不显示。
        # 节点编辑要跟着过滤走，否则会载入看不见的要素（拖拽后像"没更新"）
        if fids and self._visible_filter_getter is not None:
            try:
                keep = self._visible_filter_getter(layer)
            except Exception:  # noqa: BLE001 —— 取过滤失败按不过滤处理
                logger.exception("节点编辑：取图层可见性过滤失败")
                keep = None
            if keep is not None:
                visible = [fid for fid in fids if keep(fid)]
                if not visible:
                    self._hint(
                        "点击的图形被「选择屏蔽 / 范围裁剪」隐藏，无法编辑；"
                        "请先在工具栏取消屏蔽 / 裁剪"
                    )
                fids = visible
        return fids[-1] if fids else None  # 后添加的要素通常绘制在上层

    # -------------------------------------------------- 顶点编辑动作

    def _move_vertex(self, vid: QgsVertexId, pt: QgsPoint) -> None:
        """把顶点移动到新位置（仅改内存模型）。"""
        old = self._geom.constGet().vertexAt(vid)
        if old.is3D():
            pt = QgsPoint(pt.x(), pt.y())
            pt.addZValue(old.z())
        self._geom.get().moveVertex(vid, pt)

    def _finish_drag(self) -> None:
        """松手：拖拽产生位移则提交几何。"""
        vid, orig = self._drag_vid, self._drag_orig
        self._drag_vid = None
        self._drag_orig = None
        if vid is None or orig is None or self._geom is None:
            return
        cur = self._geom.constGet().vertexAt(vid)
        if self._same_point(orig, cur):
            return  # 没有位移（点了一下顶点），不改数据
        # 拖拽是高频连续操作：提交但不弹 info 提示（避免消息条刷屏），
        # 撤销由编辑命令栈负责
        self._commit("移动顶点")

    def _delete_vertex(self, vid: QgsVertexId) -> None:
        """右键删除顶点；触发最小顶点数保护时仅提示不删除。"""
        if self._geom is None or not self._ensure_editable():
            return
        if self._geom_type == QgsWkbTypes.PointGeometry:
            self._hint("点要素没有可删除的顶点，请直接拖动顶点移动")
            return
        absg = self._geom.constGet()
        n = absg.vertexCount(vid.part, vid.ring)
        minimum = self.MIN_RING_VERTICES if self._geom_type == QgsWkbTypes.PolygonGeometry else self.MIN_LINE_VERTICES
        if n - 1 < minimum:
            kind = "面环至少保留 4 个顶点" if self._geom_type == QgsWkbTypes.PolygonGeometry else "线至少保留 2 个顶点"
            self._hint(f"{kind}（含闭合点），无法继续删除")
            return
        # 注意：必须用 get() 改几何——constGet() 返回 const 包装，经它调用
        # deleteVertex 虽能改出结果，但会破坏 QGIS 编辑命令撤销语义（实测：
        # changeGeometry 记录的旧几何会被污染，undo 不还原，甚至波及同进程
        # 后续图层的撤销）。
        if not self._geom.get().deleteVertex(vid):
            self._hint("删除顶点失败")
            return
        if self._commit("删除顶点"):
            self._hint("已删除顶点（可 Ctrl+Z 撤销）")

    def _insert_vertex(self, insert_vid: QgsVertexId, fraction: float, event) -> None:
        """双击边：在点击处插入新顶点。"""
        if self._geom is None or not self._ensure_editable():
            return
        absg = self._geom.constGet()
        # 边的两端（fraction 由 _hit_edge 提供），用于 3D 几何的 z 插值
        n = absg.vertexCount(insert_vid.part, insert_vid.ring)
        if not (0 < insert_vid.vertex <= n):
            return
        end_a = absg.vertexAt(QgsVertexId(insert_vid.part, insert_vid.ring, insert_vid.vertex - 1))
        end_b = absg.vertexAt(insert_vid)
        pt = self._new_inserted_point(event, end_a, end_b, fraction)
        if pt is None:
            return
        if not self._geom.get().insertVertex(insert_vid, pt):
            self._hint("插入顶点失败")
            return
        if self._commit("插入顶点"):
            self._hint("已添加顶点：可继续拖动它调整位置")

    def _commit(self, command_text: str) -> bool:
        """把刚在“原要素几何”上做的修改写回该要素（同一 fid，一个撤销命令）。

        走 QGIS 编辑缓冲 ``changeGeometry``：原要素原地更新（fid、属性均不变，
        不新建要素），undo/redo 由图层编辑命令栈负责。直接传本地模型即可——
        QGIS 在命令创建时自行快照，而该模型此后不再被改动（下次手势前由
        :meth:`_sync_from_layer` 重新从图层读回）。
        """
        layer = self._layer
        if layer is None or self._fid is None:
            return False
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法修改")
            return False
        geom = self._geom
        if geom is None or geom.isNull() or geom.isEmpty():
            self._hint("编辑后的几何无效，修改未保存")
            return False
        layer.beginEditCommand(command_text)
        try:
            ok = layer.changeGeometry(self._fid, geom)
        except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
            ok = False
        if not ok:
            # destroyEditCommand = 丢弃本次改动并关闭命令（PyQGIS 没有
            # cancelEditCommand，写了会在 except 里再抛 AttributeError，
            # 命令悬着 → 图层撤销栈永久失效）
            layer.destroyEditCommand()
            self._hint("修改失败：图层编辑 API 异常")
            return False
        layer.endEditCommand()
        # 几何已变，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        self._refresh_overlay()
        # 编辑命令栈外刷新画布（仓库约束：事件栈内不直接 refresh）
        QTimer.singleShot(0, self._safe_refresh)
        return True

    def _sync_from_layer(self) -> bool:
        """以图层编辑缓冲里该要素的**当前**几何为准，重读本地预览模型。

        手势间可能发生外部改动（应用/工具栏撤销、重做、其它编辑工具、保存
        回滚等）：本地自持的模型若不刷新，会带着过期状态把整块几何写回，
        覆盖原要素的最新修改。因此每次手势（拖拽/插点/删点）前都调用本方法：
        - 要素仍可编辑 → 用图层当前几何重建模型（变了才重建手柄/描边）；
        - 已停止编辑 / 要素已删 / 几何无效 → 清空并退出当前编辑状态。

        :returns: 是否仍持有可继续编辑的要素。
        """
        if self._geom is None or self._fid is None:
            return False
        layer = self._current_layer()
        if layer is None:
            return False
        if not layer.isEditable():
            self._clear_overlay()
            self._hint("图层已停止编辑，节点编辑状态已退出")
            return False
        feature = layer.getFeature(self._fid)
        geom = feature.geometry() if feature.isValid() else None
        if geom is None or geom.isNull() or geom.isEmpty():
            self._clear_overlay()
            self._hint("要素已不存在或没有有效几何，已退出编辑")
            return False
        # QGIS 4.2 起 QgsGeometry.equals() 已弃用，且 Python 侧 == 是对象恒等、
        # 无 isEqual —— 用 asWkb() 逐字节比较判断几何是否变化（含 z/m 维度）。
        changed = self._geom is None or self._geom.asWkb() != geom.asWkb()
        self._geom = QgsGeometry(geom)  # 重新以图层几何为基准（原要素，非新建）
        if changed:
            self._refresh_overlay()
        return True

    # -------------------------------------------------- 删除选中要素

    def _delete_loaded_feature(self) -> None:
        """Delete 键：删除当前已载入（选中）的要素，整体一个撤销步骤。

        与 QGIS 顶点编辑工具一致：编辑态下对已载入的要素按 ``Delete`` 删除
        整个要素（而非顶点）。顶点拖拽过程中忽略 Delete，防止误删。删除后
        清空高亮/手柄、清掉空间索引缓存，并推迟出栈刷新画布。
        """
        if self._drag_vid is not None:
            return  # 正在拖拽顶点，忽略 Delete
        if self._fid is None or self._geom is None:
            return
        layer = self._current_layer()
        if layer is None or not layer.isEditable():
            self._hint("图层未处于编辑态，无法删除要素")
            return
        fid = self._fid
        layer.beginEditCommand("删除要素")
        try:
            ok = layer.deleteFeatures([fid])
        except Exception:  # noqa: BLE001 —— 收宽：任何异常都必须关掉编辑命令
            ok = False
        if not ok:
            layer.destroyEditCommand()
            self._hint("删除要素失败：图层编辑 API 异常")
            return
        layer.endEditCommand()
        self._clear_overlay()
        # 要素已删，空间索引缓存失效 → 清掉让下次命中重建
        clear_spatial_index(layer)
        self._hint("已删除选中要素（可 Ctrl+Z 撤销）")
        # 事件栈内不直接 refresh（仓库约束），推迟出栈
        QTimer.singleShot(0, self._safe_refresh)

    # -------------------------------------------------- 命中测试（画布像素空间）

    def _hit_vertex(self, event) -> QgsVertexId | None:
        """点击是否命中某可编辑顶点手柄（容差 TOLERANCE_PX）。"""
        if self._geom is None:
            return None
        px = self._pixel_of_event(event)
        if px is None:
            return None
        best: QgsVertexId | None = None
        best_d = float("inf")
        for vid, pt in self._iter_vertices():
            screen = self._screen_of_layer_point(pt)
            if screen is None:
                continue
            d = math.hypot(px.x() - screen.x(), px.y() - screen.y())
            if d < best_d:
                best_d = d
                best = vid
        if best is not None and best_d <= self.TOLERANCE_PX:
            return best
        return None

    def _hit_edge(self, event) -> tuple[QgsVertexId, float] | None:
        """点击是否命中某条边；命中返回 (插入位置 QgsVertexId, 沿线比例 t)。"""
        if self._geom is None:
            return None
        px = self._pixel_of_event(event)
        if px is None:
            return None
        absg = self._geom.constGet()
        best: tuple[QgsVertexId, float] | None = None
        best_d = float("inf")
        for part in range(absg.partCount()):
            for ring in range(absg.ringCount(part)):
                n = absg.vertexCount(part, ring)
                if n < 2:
                    continue
                # 收集环上全部顶点（含闭合重复点 → 末段即“闭合段”）
                pts = [absg.vertexAt(QgsVertexId(part, ring, v)) for v in range(n)]
                for k in range(n - 1):
                    a_screen = self._screen_of_layer_point(pts[k])
                    b_screen = self._screen_of_layer_point(pts[k + 1])
                    if a_screen is None or b_screen is None:
                        continue
                    dist, t = self._distance_to_segment(px, a_screen, b_screen)
                    if dist < best_d:
                        best_d = dist
                        # 新顶点插在“后端点”之前
                        best = (QgsVertexId(part, ring, k + 1), t)
        if best is not None and best_d <= self.TOLERANCE_PX:
            return best
        return None

    def _distance_to_segment(self, p: QPoint, a: QPointF, b: QPointF) -> tuple[float, float]:
        """点到线段距离 + 投影比例 t（0~1）。"""
        dx, dy = b.x() - a.x(), b.y() - a.y()
        len2 = dx * dx + dy * dy
        if len2 < 1e-9:
            return math.hypot(p.x() - a.x(), p.y() - a.y()), 0.0
        t = max(0.0, min(1.0, ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / len2))
        proj_x, proj_y = a.x() + t * dx, a.y() + t * dy
        return math.hypot(p.x() - proj_x, p.y() - proj_y), t

    # -------------------------------------------------- 几何遍历 / 顶点手柄

    def _iter_vertices(self):
        """遍历几何上可编辑顶点 → (QgsVertexId, QgsPoint)。

        多边形环的**闭合重复点**（末点 == 首点）跳过：它没有独立意义，
        拖它只会破坏闭合，删除它等同删除首点。
        """
        absg = self._geom.constGet()
        for part in range(absg.partCount()):
            for ring in range(absg.ringCount(part)):
                n = absg.vertexCount(part, ring)
                if n <= 0:
                    continue
                for v in range(n):
                    if v == n - 1 and self._ring_closed(absg, part, ring):
                        continue
                    yield QgsVertexId(part, ring, v), absg.vertexAt(QgsVertexId(part, ring, v))

    def _ring_closed(self, absg, part: int, ring: int) -> bool:
        """环是否闭合（首末顶点 2D 坐标一致，容差 1e-9）。"""
        n = absg.vertexCount(part, ring)
        if n < 2:
            return False
        a = absg.vertexAt(QgsVertexId(part, ring, 0))
        b = absg.vertexAt(QgsVertexId(part, ring, n - 1))
        return abs(a.x() - b.x()) < 1e-9 and abs(a.y() - b.y()) < 1e-9

    def _refresh_overlay(self) -> None:
        """按当前模型重建描边 + 顶点手柄。"""
        if self._geom is None:
            return
        self._refresh_band()
        self._clear_markers()
        for vid, pt in self._iter_vertices():
            center = self._map_of_layer_point(QgsPointXY(pt.x(), pt.y()))
            marker = QgsVertexMarker(self.canvas)
            marker.setIconType(QgsVertexMarker.ICON_BOX)
            marker.setColor(QColor(0, 200, 255))
            marker.setIconSize(self.MARKER_SIZE)
            marker.setPenWidth(2)
            marker.setCenter(center)
            marker.show()
            self._markers.append((marker, vid))

    def _recenter_marker(self, vid: QgsVertexId) -> None:
        """拖拽中只移动被拖顶点的手柄（其余手柄不动，省去整批重建）。"""
        if self._geom is None:
            return
        pt = self._geom.constGet().vertexAt(vid)
        for marker, m_vid in self._markers:
            if m_vid == vid:
                try:
                    marker.setCenter(self._map_of_layer_point(QgsPointXY(pt.x(), pt.y())))
                except RuntimeError:
                    pass
                return

    def _clear_markers(self) -> None:
        """销毁全部顶点手柄。

        QgsVertexMarker 继承链是 ``QgsMapCanvasItem -> QGraphicsItem``
        （非 QObject），没有 ``deleteLater()``；先把它从画布场景移除，
        再用 ``sip.delete`` 释放 C++ 对象。
        """
        for marker, _vid in self._markers:
            try:
                marker.hide()
            except RuntimeError:
                pass
            scene = marker.scene()
            if scene is not None:
                try:
                    scene.removeItem(marker)
                except RuntimeError:
                    pass
            try:
                sip.delete(marker)
            except (RuntimeError, TypeError):
                pass
        self._markers.clear()

    def _refresh_band(self) -> None:
        """把模型几何画成红色描边（点几何只靠顶点手柄，不画边）。"""
        if self._geom is None or self._geom_type == QgsWkbTypes.PointGeometry:
            return
        self._init_band()
        band = self._band
        if band is None:
            return
        band.reset(self._band_type)
        band.addGeometry(self._geom, self._layer.crs() if self._layer is not None else None)
        band.show()

    def _connect_layer_geometry(self, layer) -> None:
        """挂上图层几何变化信号：撤销 / 重做 / 别的工具改了几何，手柄与描边要立刻跟上。"""
        self._disconnect_layer_geometry()
        try:
            layer.geometryChanged.connect(self._on_layer_geometry_changed)
            self._hooked_layer = layer
        except Exception:  # noqa: BLE001 —— 挂不上也不该挡住编辑
            self._hooked_layer = None
            logger.warning("节点编辑：挂接 geometryChanged 失败", exc_info=True)

    def _disconnect_layer_geometry(self) -> None:
        layer = getattr(self, "_hooked_layer", None)
        self._hooked_layer = None
        if layer is None:
            return
        try:
            layer.geometryChanged.disconnect(self._on_layer_geometry_changed)
        except Exception:  # noqa: BLE001 —— 收尾场景属正常
            pass

    def _on_layer_geometry_changed(self, *_args) -> None:
        """图层几何被改了：**推迟一拍**再同步。

        推迟是必须的：``changeGeometry`` 在本工具自己的编辑命令窗口内同步发这个信号，
        当场重读/重建手柄会踩在命令栈上（仓库约束：这类动作一律 ``singleShot(0)``）。
        ``_sync_from_layer`` 自己会用 ``asWkb()`` 判断几何是否真变了，没变就不重建。
        """
        QTimer.singleShot(0, self._sync_from_layer)

    def _clear_overlay(self) -> None:
        """清空当前编辑状态（手柄 + 描边 + 模型 + 图层选中）。"""
        self._disconnect_layer_geometry()
        if self._fid is not None and self._layer is not None:
            try:
                self._layer.deselect([self._fid])
            except RuntimeError:
                pass
        self._drag_vid = None
        self._drag_orig = None
        self._fid = None
        self._geom = None
        self._geom_type = None
        self._clear_markers()
        if self._band is not None:
            try:
                self._band.reset(self._band_type)
                self._band.hide()
            except RuntimeError:
                pass

    def _init_band(self) -> None:
        """按图层几何类型建描边橡皮筋（点不建）。"""
        if self._geom_type == QgsWkbTypes.PointGeometry or self._geom_type is None:
            return
        if self._band is None or self._band_type != self._geom_type:
            self._band_type = self._geom_type
            self._band = QgsRubberBand(self.canvas, self._band_type)
            self._band.setColor(QColor(255, 0, 0, 180))
            self._band.setWidth(2)
            if self._geom_type == QgsWkbTypes.PolygonGeometry:
                self._band.setFillColor(QColor(255, 0, 0, 30))

    # -------------------------------------------------- 图层 / 坐标

    def _current_layer(self) -> QgsVectorLayer | None:
        """当前编辑图层；切换图层时清空编辑状态（防跨层 fid 误改）。"""
        layer = self._layer_getter()
        if layer is None or layer.type() != layer.VectorLayer:
            if self._fid is not None:
                self._clear_overlay()
            return None
        if self._layer is not None and layer is not self._layer:
            self._clear_overlay()
        self._layer = layer
        return layer

    def _ensure_editable(self) -> bool:
        """图层存在且处于编辑态；否则提示并返回 False。"""
        layer = self._current_layer()
        if layer is None:
            return False
        if not layer.isEditable():
            self._hint("图层未处于编辑态，无法修改几何")
            return False
        return True

    def _safe_refresh(self) -> None:
        try:
            self.canvas.refresh()
        except RuntimeError:
            pass

    # -------------------------------------------------- 坐标换算

    def _pixel_of_event(self, event) -> QPoint | None:
        """事件像素坐标（QPointF 测试桩 / 真实 QPoint 都兼容）。"""
        try:
            raw = event.pixelPoint()
        except AttributeError:
            raw = event.pos()
        if isinstance(raw, QPointF):
            return raw.toPoint()
        return QPoint(raw.x(), raw.y())

    def _map_point_of_px(self, px: QPoint) -> QgsPointXY | None:
        """画布像素 → 画布地图坐标（canvas 目标 CRS）。"""
        try:
            return self.canvas.getCoordinateTransform().toMapCoordinates(px)
        except RuntimeError:
            return None

    def _screen_of_layer_point(self, pt: QgsPoint) -> QPointF | None:
        """图层 CRS 坐标 → 画布像素（命中测试 / 手柄定位）。"""
        map_pt = self._map_of_layer_point(QgsPointXY(pt.x(), pt.y()))
        if map_pt is None:
            return None
        try:
            out = self.canvas.getCoordinateTransform().transform(map_pt)
            return QPointF(out.x(), out.y())
        except RuntimeError:
            return None

    def _layer_point_of_event(self, event) -> QgsPoint | None:
        """事件像素 → 图层 CRS 坐标（新顶点 / 拖拽落点）。"""
        px = self._pixel_of_event(event)
        if px is None:
            return None
        map_pt = self._map_point_of_px(px)
        if map_pt is None:
            return None
        xy = self._to_layer_coords(map_pt)
        return QgsPoint(xy)

    def _new_inserted_point(self, event, end_a: QgsPoint, end_b: QgsPoint, fraction: float) -> QgsPoint | None:
        """构造插入点；3D 几何沿线对 z 做线性插值，保持几何维度。"""
        base = self._layer_point_of_event(event)
        if base is None:
            return None
        pt = QgsPoint(base.x(), base.y())
        if end_a.is3D() or end_b.is3D():
            za = end_a.z() if end_a.is3D() else 0.0
            zb = end_b.z() if end_b.is3D() else 0.0
            pt.addZValue(za + (zb - za) * fraction)
        return pt

    def _to_layer_coords(self, map_pt: QgsPointXY) -> QgsPointXY:
        """画布地图坐标 → 图层 CRS（CRS 相同 / 无效时恒等）。"""
        layer = self._layer
        if layer is None:
            return QgsPointXY(map_pt)
        src = self.canvas.mapSettings().destinationCrs()
        dst = layer.crs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return QgsPointXY(map_pt)
        try:
            xform = QgsCoordinateTransform(src, dst, self.canvas.project() or QgsProject.instance())
            return xform.transform(QgsPointXY(map_pt))
        except Exception:
            return QgsPointXY(map_pt)

    def _map_of_layer_point(self, xy: QgsPointXY) -> QgsPointXY | None:
        """图层 CRS 坐标 → 画布地图坐标（反方向换算）。"""
        layer = self._layer
        if layer is None:
            return QgsPointXY(xy)
        src = layer.crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if not src.isValid() or not dst.isValid() or src == dst:
            return QgsPointXY(xy)
        try:
            xform = QgsCoordinateTransform(src, dst, self.canvas.project() or QgsProject.instance())
            return xform.transform(QgsPointXY(xy))
        except Exception:
            return None

    @staticmethod
    def _same_point(a: QgsPoint, b: QgsPoint) -> bool:
        return abs(a.x() - b.x()) < 1e-9 and abs(a.y() - b.y()) < 1e-9


__all__ = ["FeatureEditTool"]
