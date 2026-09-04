"""「编辑连接」Dialog：在线底图 Dock 叶子右键编辑一条在线连接。

复用「添加自定义底图」（:class:`CustomBasemapDialog`）的全部字段/校验/类型页：
名称、分组、服务类型、服务地址、类型参数（xyz 页含**反盗链认证 Referer** ——
天地图"浏览器端" key 校验要求带白名单 Referer 头，见 basemap 模块坑 2）。
与新增对话框的差异有三处：

- 进入时按现有清单条目**预填**所有字段（含类型页参数）；
- 标题「编辑在线底图」；
- 额外一行「API Key」——仅当连接 URL 引用集中 key 占位符（``{tk}`` /
  ``{gaode_key}``）或老配置把真实 tk 内嵌在 URL 里（无占位符时代遗留，与
  ``{tk}`` 判等归一，见 :func:`basemap.canonical_basemap_url`）时显示：预填当前
  真值（占位符 → 传入 :class:`MapKeysConfig` 的集中值；内嵌 tk → 提取出来并把
  URL 归一成 ``{tk}`` 占位符形态），确定后真值经 :meth:`keys_override` 交给
  MainWindow 写回 ``map.keys`` —— 换 key 仍是「集中管理、改一处全站生效」，
  这里只是给单条连接一个就近编辑入口，URL 落盘恒为占位符。

只依赖 ``qgis.PyQt`` + :class:`OnlineBasemapDef` / :class:`MapKeysConfig`，不做
config I/O（由 MainWindow 统一写回独立清单文件与 map.keys）。
"""

from __future__ import annotations

import re
from typing import Optional

from qgis.PyQt.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from app.env.config import MapKeysConfig, OnlineBasemapDef
from app.ui.dialogs.custom_basemap import CustomBasemapDialog

#: URL 里可引用的集中 key 占位符：(占位符, map.keys 字段名)
_KEY_PLACEHOLDERS = (
    ("{tk}", "tianditu_tk"),
    ("{gaode_key}", "gaode_key"),
)
#: 老配置内嵌在 URL 里的真实天地图 tk（无占位符时代遗留；编辑时顺手迁到 {tk}）
_EMBEDDED_TK = re.compile(r"tk=[^&\s]+")

#: 表单行号（CustomBasemapDialog 的通用区共 4 行：名称/分组/类型/服务地址）
_KEY_ROW = 4
_HINT_ROW = 5

_HINT_TEMPLATE = (
    "此 URL 引用 {placeholder}：真实 key 集中管理（app.yaml map.keys.{field}）。"
    "保存即写回，引用同一占位符的连接一并生效；已加载底图需重载后生效。"
)


def detect_key_slot(url: str) -> Optional[str]:
    """url 引用的集中 key 槽位（``map.keys`` 字段名）；未引用返回 ``None``。

    优先认占位符（``{tk}`` / ``{gaode_key}``）；其次认老配置内嵌的真实
    ``tk=…``（与 basemap 模块 :func:`canonical_basemap_url` 的归一口径一致——
    内嵌 tk 与 ``{tk}`` 判等，故也视为天地图槽位）。
    """
    for placeholder, field in _KEY_PLACEHOLDERS:
        if placeholder in url:
            return field
    if _EMBEDDED_TK.search(url):
        return "tianditu_tk"
    return None


class EditBasemapDialog(CustomBasemapDialog):
    """编辑一条在线底图连接的对话框（Dock 叶子右键「编辑连接」）。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        def_: Optional[OnlineBasemapDef] = None,
        keys: Optional[MapKeysConfig] = None,
    ) -> None:
        super().__init__(parent, title="编辑在线底图")
        self._central_keys: MapKeysConfig = keys or MapKeysConfig()
        self._last_slot: Optional[str] = None
        self._build_key_row()
        if def_ is not None:
            self._load_def(def_)
        self._refresh_key_row()

    # ============================================================== UI / 预填

    def _build_key_row(self) -> None:
        """在「服务地址」行下追加 API key 行（含提示行），默认隐藏。"""
        self._key_label = QLabel("API Key")
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("真实 key（集中存于 app.yaml 的 map.keys）")
        self._key_hint = QLabel("")
        self._key_hint.setWordWrap(True)
        self._key_hint.setStyleSheet("color: #888;")
        form = self._common_form
        form.insertRow(_KEY_ROW, self._key_label, self._key_edit)
        form.insertRow(_HINT_ROW, "", self._key_hint)
        self.url_edit.textChanged.connect(self._refresh_key_row)

    @staticmethod
    def _set_row_visible(form: QFormLayout, row: int, visible: bool) -> None:
        """QFormLayout 整行显隐（行内 label + 控件一起）。"""
        for role in (
            QFormLayout.ItemRole.LabelRole,
            QFormLayout.ItemRole.FieldRole,
        ):
            item = form.itemAt(row, role)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setVisible(visible)

    def _load_def(self, d: OnlineBasemapDef) -> None:
        """按现有条目预填全部字段（含类型页参数）。"""
        svc = d.type or "xyz"
        idx = self.type_combo.findData(svc)
        self.type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.name_edit.setText(d.name)
        self.group_edit.setText(d.group or "在线底图")

        url = d.url
        embedded_tk: Optional[str] = None
        if not any(ph in url for ph, _ in _KEY_PLACEHOLDERS):
            m = _EMBEDDED_TK.search(url)
            if m:
                # 老配置内嵌真实 tk → 提取到 key 行，URL 归一成 {tk} 占位符
                embedded_tk = m.group(0)[len("tk=") :]
                url = _EMBEDDED_TK.sub("tk={tk}", url)
        self.url_edit.setText(url)

        if svc == "xyz":
            self.referer_edit.setText(d.referer)
            self.zmin_spin.setValue(d.zmin)
            self.zmax_spin.setValue(d.zmax)
        elif svc == "arcgis":
            self.arcgis_layer_edit.setText(d.layers)
        elif svc == "arcgisfeature":
            self.arcgisfeature_layer_edit.setText(d.layers)
            self.arcgisfeature_crs_edit.setText(d.crs)
        elif svc in ("wms", "wmts"):
            for k in ("layers", "styles", "format", "crs"):
                getattr(self, f"{svc}_{k}_edit").setText(getattr(d, k) or "")
            if svc == "wmts":
                self.wmts_matrix_edit.setText(d.tile_matrix_set or "")

        slot = detect_key_slot(self.url_edit.text())
        if slot:
            if slot == "tianditu_tk" and embedded_tk is not None:
                self._key_edit.setText(embedded_tk)  # 迁移保留原真值
            else:
                self._key_edit.setText(self._central_value(slot))

    def _central_value(self, slot: str) -> str:
        return getattr(self._central_keys, slot) or ""

    def _refresh_key_row(self, _text: str = "") -> None:
        """URL 引用集中 key 占位符 / 内嵌 tk → 显示 key 行；否则隐藏。

        textChanged 信号回调（带新 url 字符串）；构造时手动调也兼容（默认空串）。
        只对“新识别出的槽位”自动预填集中真值（用户正在手输时不打断）；换槽位
        （{tk} ↔ {gaode_key}）时清掉旧真值避免串 key。
        """
        slot = detect_key_slot(self.url_edit.text())
        visible = slot is not None
        self._set_row_visible(self._common_form, _KEY_ROW, visible)
        self._set_row_visible(self._common_form, _HINT_ROW, visible)
        if slot is None:
            self._last_slot = None
            return
        self._key_label.setText(f"API Key（{next(ph for ph, f in _KEY_PLACEHOLDERS if f == slot)}）")
        self._key_hint.setText(
            _HINT_TEMPLATE.format(
                placeholder=next(ph for ph, f in _KEY_PLACEHOLDERS if f == slot),
                field=slot,
            )
        )
        if slot != self._last_slot:
            self._key_edit.setText(self._central_value(slot))
        self._last_slot = slot

    # ============================================================== 取值

    def keys_override(self) -> Optional[MapKeysConfig]:
        """API key 相对 ``map.keys`` 集中值有改动 → 返回整份新
        :class:`MapKeysConfig`（供 MainWindow 写回），否则 ``None``。

        URL 未引用任何 key 槽位（改了也不生效）或与集中值一致时不返回——
        避免每次改名都重写 ``app.yaml`` 的 keys 段。内嵌 tk 迁移（提取真值 +
        URL 归一成 ``{tk}``）时若与集中值不同也会在此返回（真值落盘保证该连接
        不因迁移而失效）。
        """
        slot = detect_key_slot(self.url_edit.text())
        if slot is None:
            return None
        value = self._key_edit.text().strip()
        if value == self._central_value(slot):
            return None
        return MapKeysConfig(
            tianditu_tk=value if slot == "tianditu_tk" else self._central_keys.tianditu_tk,
            gaode_key=value if slot == "gaode_key" else self._central_keys.gaode_key,
        )
