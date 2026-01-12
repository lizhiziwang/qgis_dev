from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QListWidgetItem, QTextEdit, QPushButton, QLabel, QLineEdit,
    QGroupBox, QMessageBox, QSplitter, QGridLayout
)
from PyQt5.QtCore import Qt, pyqtSignal, QVariant
from qgis.core import (
    QgsVectorLayer, QgsFeatureRequest, QgsField, QgsExpression
)
from qgis.gui import QgsFieldComboBox

# 自定义查询构建器对话框（继承 QDialog）
class QueryBuilder(QDialog):
    # 定义信号，用于传递最终的 SQL 条件（可选）
    sqlConfirmed = pyqtSignal(str)

    def __init__(self, layer: QgsVectorLayer, parent=None):
        super().__init__(parent)
        # 初始化核心参数
        self.layer = layer  # 目标矢量图层
        self.valid_fields = [field.name() for field in layer.fields()]  # 图层所有字段
        self.current_field = ""  # 当前选中字段
        self.current_values = []  # 当前字段的所有值

        # 对话框基础设置
        self.setWindowTitle("属性查询器")
        self.setMinimumSize(720, 500)
        self.init_ui()  # 初始化界面
        self.init_signals()  # 初始化信号与槽

    def init_ui(self):
        """初始化对话框界面布局"""
        # 1. 整体布局（垂直布局）
        main_layout = QVBoxLayout(self)

        # 2. 顶部拆分器（字段列表 + 字段值列表）
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        # ---- 2.1 左侧：字段组（QGroupBox + QListWidget/QgsFieldComboBox）
        self.field_group = QGroupBox("图层字段")
        field_layout = QVBoxLayout(self.field_group)

        # 字段下拉框（更友好，替代 ListWidget）
        self.field_combo = QgsFieldComboBox()
        self.field_combo.setLayer(self.layer)
        self.field_combo.setFocusPolicy(Qt.NoFocus)
        field_layout.addWidget(QLabel("选中字段："))

        self.field_combo.fieldChanged.connect(self.set_current_field)

        field_layout.addWidget(self.field_combo)

        # 字段值列表（预览选中字段的值）
        self.value_list = QListWidget()
        self.value_list.setFocusPolicy(Qt.NoFocus)
        field_layout.addWidget(QLabel("字段值预览："))
        field_layout.addWidget(self.value_list)
        def get_field_type(value):
            try:
                field_name = self.field_combo.currentText()
                filed = [item for item in self.layer.fields() if field_name == item.name()][0]
                f_type = filed.type()
                if f_type == QVariant.String:
                    return f"'{value}'"
                elif f_type == QVariant.Int:
                    return f'{value}'
                elif f_type == QVariant.Double:
                    return f'{value}'
                else:
                    return f'{value}'
            except Exception as e:
                print(e)

        self.value_list.itemDoubleClicked.connect(lambda e:self.insert_text(get_field_type(e.text()) ))

        # 加载字段值按钮组
        value_btn_layout = QHBoxLayout()
        self.sample_btn = QPushButton("加载采样值")
        self.all_btn = QPushButton("加载全部值")
        self.sample_btn.setFocusPolicy(Qt.NoFocus)
        self.all_btn.setFocusPolicy(Qt.NoFocus)

        value_btn_layout.addWidget(self.sample_btn)
        value_btn_layout.addWidget(self.all_btn)
        field_layout.addLayout(value_btn_layout)

        splitter.addWidget(self.field_group)

        # ---- 2.2 右侧：SQL 编辑组
        self.sql_group = QGroupBox("SQL 筛选条件")
        sql_layout = QVBoxLayout(self.sql_group)
        but_layout = QGridLayout()
        operator_str = ["=", "<", ">", "LIKE", "%", "IN", "NOT IN", "<=", ">=", "!=", "ILIKE", "AND", "OR", "NOT"]
        row, col = 0, 0
        col_count = 5  # 每行显示 5 个按钮
        for s in operator_str:
            var = QPushButton(s)
            var.setFocusPolicy(Qt.NoFocus)
            var.setMinimumSize(60, 30)
            var.clicked.connect(lambda _, text=var.text(): self.insert_text(text))
            # 绑定点击事件，插入运算符到 QTextEdit
            # var.clicked.connect(lambda checked, op=s: self.insert_operator_to_sql(op))
            but_layout.addWidget(var, row, col)
            col += 1
            if col >= col_count:
                col = 0
                row += 1
        # 将运算符布局添加到 SQL 编辑组
        sql_layout.addLayout(but_layout)

        # SQL 编辑框
        self.sql_edit = QTextEdit()
        self.sql_edit.setPlaceholderText("请输入 SQL 筛选条件（如：县代码 = '360202'）")
        sql_layout.addWidget(self.sql_edit)
        self.sql_edit.setReadOnly(False)  # 显式设置为可编辑（默认也是 False，此处为强制锁定）

        # 2. 默认激活输入焦点（对话框弹出后，直接可输入）
        self.sql_edit.setFocusPolicy(Qt.StrongFocus)  # 允许获取焦点
        self.sql_edit.setFocus()

        # 语法提示标签
        # self.tip_label = QLabel("提示：支持 QGIS 表达式语法，字段名含特殊字符请用双引号包裹")
        # self.tip_label.setStyleSheet("color: #666; font-size: 10px;")
        # sql_layout.addWidget(self.tip_label) processing.run("native:shortestpathpointtopoint", {})

        splitter.addWidget(self.sql_group)

        # 3. 底部：功能按钮组
        btn_layout = QHBoxLayout()
        self.test_btn = QPushButton("测试条件")
        self.clear_btn = QPushButton("清除条件")
        self.ok_btn = QPushButton("确定应用")
        self.cancel_btn = QPushButton("取消")

        btn_layout.addWidget(self.test_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()  # 拉伸占位，让确定/取消靠右
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)

        main_layout.addLayout(btn_layout)

        # 调整拆分器比例
        splitter.setSizes([300, 420])
    def insert_text(self, text):
        cursor = self.sql_edit.textCursor()
        cursor.insertText(text)
        self.sql_edit.setTextCursor(cursor)

    def set_current_field(self, event=None):
        self.insert_text(f' "{event}" ')

    def init_signals(self):
        """初始化信号与槽绑定"""
        # 1. 字段下拉框切换：加载对应字段的值
        self.field_combo.fieldChanged.connect(self.on_field_changed)

        # 2. 加载值按钮
        self.sample_btn.clicked.connect(lambda: self.load_field_values(sample=True))
        self.all_btn.clicked.connect(lambda: self.load_field_values(sample=False))

        # 3. 功能按钮
        self.test_btn.clicked.connect(self.test_sql_condition)
        self.clear_btn.clicked.connect(self.clear_sql_condition)
        self.ok_btn.clicked.connect(self.on_ok_clicked)
        self.cancel_btn.clicked.connect(self.reject)

        # 4. 字段值列表双击：自动拼接 SQL 条件
        self.value_list.itemDoubleClicked.connect(self.on_value_item_double_clicked)

    def on_field_changed(self, field_name: str):
        """字段切换事件"""
        self.current_field = field_name
        self.value_list.clear()
        self.current_values = []

    def load_field_values(self, sample: bool = True, sample_count: int = 50):
        """加载选中字段的值（采样/全部）"""
        if not self.current_field or not self.layer.isValid():
            QMessageBox.warning(self, "警告", "无效字段或图层，无法加载值！")
            return

        self.value_list.clear()
        self.current_values = []

        # 1. 构建要素请求，获取字段值
        request = QgsFeatureRequest()
        # 仅加载当前字段，提升效率
        request.setSubsetOfAttributes([self.current_field], self.layer.fields())

        # 2. 遍历要素，提取字段值
        features = self.layer.getFeatures(request)
        for idx, feat in enumerate(features):
            # 采样模式：仅加载前 sample_count 个值
            if sample and idx >= sample_count:
                break
            field_value = feat.attribute(self.current_field)
            if field_value is None:
                continue
            # 去重（避免重复值）
            if field_value not in self.current_values:
                self.current_values.append(field_value)
                # 添加到值列表
                item = QListWidgetItem(str(field_value))
                item.setData(Qt.UserRole, field_value)  # 存储原始值
                self.value_list.addItem(item)

        # 3. 提示加载结果
        # QMessageBox.information(self, "提示", f"成功加载 {len(self.current_values)} 个值（{'采样' if sample else '全部'}）")

    def on_value_item_double_clicked(self, item: QListWidgetItem):
        """双击值列表项，自动拼接 SQL 条件"""
        if not self.current_field:
            return

        # 1. 获取字段值和字段类型
        field_value = item.data(Qt.UserRole)
        field = self.layer.fields().field(self.current_field)
        field_type = field.type()

        # 2. 拼接 SQL 条件（处理字符串/数值类型差异）
        # if field_type in [QgsField.String, QgsField.Text]:
        #     # 字符串类型：值用单引号包裹
        #     sql_segment = f"{self.current_field} = '{str(field_value).replace(\"'\", \"''\")}'"
        # else:
        #     # 数值类型：直接拼接
        #     sql_segment = f"{self.current_field} = {field_value}"

        # 3. 写入 SQL 编辑框（覆盖原有内容，可改为追加）
        current_sql = self.sql_edit.toPlainText().strip()
        # if not current_sql:
        #     self.sql_edit.setText(sql_segment)
        # else:
        #     # 追加条件（默认 AND 连接）
        #     self.sql_edit.setText(f"{current_sql} AND {sql_segment}")

    def test_sql_condition(self):
        """测试 SQL 条件的合法性，并统计匹配要素数"""
        sql_condition = self.sql_edit.toPlainText().strip()
        if not sql_condition:
            QMessageBox.warning(self, "警告", "请输入有效的 SQL 筛选条件！")
            return

        # 1. 验证 SQL 表达式合法性
        expression = QgsExpression(sql_condition)
        if expression.hasParserError():
            QMessageBox.critical(self, "测试失败", f"SQL 语法错误！\n{expression.parserErrorString()}")
            return

        # 2. 验证字段有效性
        for field_name in expression.referencedColumns():
            if field_name not in self.valid_fields:
                QMessageBox.critical(self, "测试失败", f"无效字段：{field_name}（图层中不存在该字段）")
                return

        # 3. 统计匹配要素数
        try:
            request = QgsFeatureRequest().setFilterExpression(sql_condition)
            match_count = sum(1 for _ in self.layer.getFeatures(request))
            QMessageBox.information(self, "测试成功", f"SQL 条件合法！\n匹配要素数量：{match_count} 个")
        except Exception as e:
            QMessageBox.critical(self, "测试失败", f"查询执行异常！\n{str(e)}")

    def clear_sql_condition(self):
        """清除 SQL 编辑框的内容"""
        self.sql_edit.clear()
        self.value_list.clear()
        self.current_values = []

    def on_ok_clicked(self):
        """确定应用 SQL 条件到图层"""
        sql_condition = self.sql_edit.toPlainText().strip()

        # 1. 空条件处理：清空图层筛选
        if not sql_condition:
            self.layer.setSubsetString("")
            self.sqlConfirmed.emit("")
            QMessageBox.information(self, "操作成功", "已清空图层筛选条件，显示所有要素")
            self.accept()
            return

        # 2. 验证条件合法性（重复测试逻辑，避免无效条件）
        expression = QgsExpression(sql_condition)
        if expression.hasParserError():
            QMessageBox.critical(self, "应用失败", f"SQL 语法错误！\n{expression.parserErrorString()}")
            return

        # 3. 应用筛选条件到图层
        try:

            self.layer.setSubsetString(sql_condition)
            feature_count = self.layer.featureCount()
            self.sqlConfirmed.emit(sql_condition)
            QMessageBox.information(self, "操作成功", f"已应用 SQL 筛选条件！\n图层剩余要素：{feature_count} 个")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "应用失败", f"条件应用异常！\n{str(e)}")
