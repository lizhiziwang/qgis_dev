# GDAL 对话框参数面板增加 QScrollArea 内部滚动

## Context（为什么改）

用户反馈：「栅格计算器的窗口依旧高度有问题，如果窗口内的内容太多的，
内部可以用滚动条」。

承接上一轮「dialog 最大高度对齐 mainwindow」修复：当时把 dialog
maxHeight 钳到 `mainwindow.height()`（默认 800），但栅格计算器
（`gdal:rastercalculator`）有 13+ 参数行，加上高级分组展开后，sizeHint
约 900+ px。maxHeight = 800 会让 QFormLayout 被压扁、wrapper 显示不完整。

正确做法：**外层 dialog 保持 maxHeight = mainwindow 高度**，**参数面板
内部用 QScrollArea 承载超出部分**——内容超长时出滚动条，按需访问，
不会被压扁。

## 探针 / 根因

[generic_alg.py](app/ui/dialogs/generic_alg.py) 上一轮修复后：

```python
def _init_ui(self):
    layout = QVBoxLayout()
    main = QHBoxLayout()
    param_panel = QWidget()
    self._init_parameters_panel(param_panel)
    main.addWidget(param_panel, stretch=3)   # ← 直接 addWidget，无滚动
    main.addWidget(make_description_browser(self._algo_id), stretch=2)
    layout.addLayout(main, stretch=1)
```

参数面板是 QWidget（含 VBoxLayout），直接 addWidget 到 QHBoxLayout。
当 content sizeHint 大于 maxHeight 时，layout 不会出滚动条，而是把
所有 widget 压扁以塞下；用户看到「下拉框宽度变 26px / 控件高度变小」。

## 方案

`app/ui/dialogs/generic_alg.py`：

1. **导入 `QScrollArea`**：

   ```python
   from PyQt6.QtWidgets import (
       ...,
       QScrollArea,
       ...
   )
   ```

2. **`_init_ui` 把参数面板包进 QScrollArea**：

   ```python
   param_panel = QWidget()
   self._init_parameters_panel(param_panel)
   param_scroll = QScrollArea()
   param_scroll.setWidgetResizable(True)         # 关键：让 inner widget 跟随 viewport
   param_scroll.setFrameShape(QScrollArea.Shape.NoFrame)  # 不画边框，与 dialog 风格一致
   param_scroll.setWidget(param_panel)
   main.addWidget(param_scroll, stretch=3)
   ```

- `setWidgetResizable(True)` 让 inner widget 自动适应 QScrollArea viewport
  大小；超出时 `verticalScrollBar.maximum()` > 0，Qt 自动显示滚动条
- `setFrameShape(NoFrame)` 与 QGroupBox / QFormLayout 视觉融合，不显突兀
- 描述浏览器（右侧）独立保留不动——QTextBrowser 自带滚动，不需要再包

3. **配套回归测试** `tests/ui/docks/test_gdal_desc_and_height.py` 新增：
   `test_generic_alg_dialog_param_panel_scrolls_when_tall`
   - 构造一个 600px 高的 QMainWindow（模拟比默认 800 更紧）
   - 构造 `gdal:rastercalculator` dialog，验证：
     1. `findChildren(QScrollArea)` 必须至少有一个
     2. `QScrollArea.widgetResizable() == True`
     3. 把 inner widget `setMinimumHeight(sizeHint.height() + 1500)`
        强制超出 viewport，`vbar.maximum() > 0` 证明有滚动能力
     4. `dlg.height() <= win.height() + 80` —— maxHeight 仍起约束
     5. `vbar.setValue(vbar.maximum() // 2)` 后 value > 0，验证
        实际可滚动

## 关键文件

- 改 `app/ui/dialogs/generic_alg.py`：
    - `from PyQt6.QtWidgets` 增 `QScrollArea`
    - `_init_ui` 把参数面板包到 `QScrollArea`（`setWidgetResizable(True)`
      + `setFrameShape(NoFrame)`）
- 改 `tests/ui/docks/test_gdal_desc_and_height.py`：
    - 新增 `test_generic_alg_dialog_param_panel_scrolls_when_tall`

## 验证

- AST parse：2 个改文件全部 OK
- `pytest tests/ui/docks/test_gdal_desc_and_height.py -m gui -v` —— **4 passed**
  （3 原有 + 1 新增）
- `pytest tests/ui/docks/ tests/ui/main_window/ tests/ui/dialogs/ -m gui` ——
  **58 passed**（54 历史 + 4 新增），无回归

## 关键 API 备忘

- **`QScrollArea.setWidgetResizable(True)`**：让 inner widget 跟随 viewport，
  这是「内容超出时自动出滚动条」的前提；不设的话，inner 会保持 sizeHint 不动
- **`QScrollArea.setFrameShape(NoFrame)`**：去掉默认的 box 边框，避免与
  周围的 QFormLayout / QGroupBox 视觉冲突
- **`vbar.maximum() > 0` vs `vbar.isVisible()`**：Qt 在内容 ≤ viewport 时
  把滚动条 setVisible(False)，这是默认行为（不是 bug）。测试要断言
  `maximum() > 0` 而非 `isVisible()`，前者证明滚动机制就绪即可

## 风险与限制

- **`gdal:rastercalculator` 等极长表单**：现在 13+ 参数行也不会撑爆 dialog，
  内容过多时由滚动条承载；用户首次打开能直接看到「顶部 + 底部按钮 + 进度条」
  全可见，中间的参数面板在 QScrollArea 内上下滚动
- **描述浏览器不同步滚动（保持现状）**：QTextBrowser 自身已带滚动，
  没必要再嵌套一层 QScrollArea；这也意味着两个区域独立滚动——更符合
  「参数 vs 文档」语义分离
- **测试 `processing_ready` fixture**：必须放在 test 函数签名里，不能
  在模块顶层调 `Processing.initialize()`（那时 `qgis_app` fixture 还没
  执行 initQgis，processing registry 是空的，连 `native:buffer` 都查不到）
- **`QScrollArea` 测试里的 `setMinimumHeight`**：模拟「内容超长」需要
  主动把 inner widget 顶上去；否则 13+ 行栅格计算器在 600px viewport 下
  不一定真超（实际 viewport 可能滚动条不出现）