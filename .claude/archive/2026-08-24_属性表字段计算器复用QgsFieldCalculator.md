# 属性表工具条：字段计算器（复用 QGIS 内置 QgsFieldCalculator）

## Context（为什么改）

用户要求属性表 dock（[attribute_dock.py](app/ui/docks/attribute_dock.py)）工具条加"字段计算器"
按钮，弹出计算框，支持常用 Python/QGIS 函数。用户最终指示：**矢量图层属性窗口"字段管理"里
有现成的字段计算器，直接复用**——不自建对话框。

## 关键 API 探针结论（决定复用路径，避免以后重探）

- `qgis.core.QgsFieldCalculator`、`qgis.gui.QgsFieldCalculatorDialog` 在 QGIS 4.2 **都不存在**。
- `qgis.gui.QgsFieldCalculator`（QDialog）**存在**——正是图层属性窗口字段页里那个字段计算器。
- **必须 `QgsApplication.initQgis()` 后才能构造**；裸 `QgsApplication` 下构造会原生崩溃
  （退出码 127，无 traceback，本次多次踩到）。构造函数 `QgsFieldCalculator(layer, parent)`。
- 自带 `changedAttributeId` 信号；OK 时把计算结果直接应用到图层（必要时自动进入编辑态）。
- 附带发现：**QGIS 表达式引擎对 `if()`/`||`/`CASE WHEN`/多参 `concat` 等惰性节点，在带
  feature 上下文求值时返回 None**（`coalesce`/`length`/算术/`*` 正常）。内置对话框走 C++
  求值路径，按 QGIS 官方行为对待，未做自定义求值。
- 无现成计算器图标（QGIS 主题里也没有 `mActionFieldCalculator`），生成 48×48 PNG。

## 改动

1. `app/ui/docks/attribute_dock.py`
   - 工具条：`filter_btn` 后加 `calc_btn`（objectName=`fieldCalcBtn`，`Icons.field_calc()`，
     toolTip"字段计算器"，26×26，连 `_field_calc`）。
   - `_field_calc`：`QgsFieldCalculator(self.layer, self).exec()` → Accepted 后
     `_sync_edit_buttons()` + `model.reload_fields()`。
   - `_sync_edit_buttons`：按 `layer.isEditable()` 使能 保存/停止 按钮。
   - `PagedAttributeTableModel.reload_fields()`：重读 `field_names`/`header_names`（别名降级，
     同 `__init__` 逻辑）+ 清 `_feature_cache` + `beginResetModel/endResetModel`。
     **不重流 fids**（加字段不改要素数/顺序，500w 图层只重取可视行）。
   - 顶部 `PyQt6.QtWidgets` 导入加 `QDialog`。
2. `app/ui/common/icons.py`：`FIELD_CALC = ("png", "字段计算器.png")` + `field_calc()`。
3. `resource/png/字段计算器.png`：一次性脚本生成 48×48（蓝色圆角方块 + 白 "fx"，主题色
   `#2f5f8f`），脚本用完删。**坑**：QPainter/QFont 绘图必须建 `QApplication`，否则崩。

## 测试

`tests/ui/docks/test_attribute_dock.py` 新增 4 个（`@pytest.mark.gui`）：
1. 工具条含 `fieldCalcBtn`（findChild by objectName）。
2. `reload_fields()` 新字段：`dataProvider().addAttributes` + `updateFields()` 加字段 →
   `columnCount()` +1、新列头正确、fids 保留不重流。
3. `reload_fields()` 值更新：编辑态 `changeAttributeValue` 后重取到编辑缓冲内新值。
4. `_field_calc` 链路：monkeypatch 掉 `qgis.gui.QgsFieldCalculator`（假对话框 accept 时加
   字段）→ `calc_btn.click()` → `columnCount()` +1。

验证：`tests/ui/docks/` 全量 42 个通过；`py_compile` 通过；`Icons.field_calc()` 非空。
跑法（**必须带 QGIS PATH**，否则 QtCore DLL 加载失败）：
```
export PATH="/d/zsh/app/qgis4.2/apps/qt6/bin:/d/zsh/app/qgis4.2/apps/qgis/bin:/d/zsh/app/qgis4.2/apps/gdal/bin:$PATH"
D:/zsh/app/qgis4.2/apps/Python312/python.exe .tmp/run_pytest_qgis.py tests/ui/docks/test_attribute_dock.py -q
```

## 收尾

- 删除一次性图标生成脚本 `.tmp/gen_field_calc_icon.py`。
- 对话框文案跟随 QGIS 语言环境（本机未加载中文翻译则英文）——QGIS 级行为，未改。
