import json
import os
import sys
import uuid

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTableWidgetItem, QWidget, QHBoxLayout, QCheckBox, QLabel, \
    QTableWidget, QHeaderView, QAbstractItemView, QLineEdit, QPushButton, QFileDialog, QMenu, QAction, QComboBox, \
    QRadioButton, QTextEdit
from qgis._core import QgsApplication, Qgis

from .Logger import setup_logger
from .check.CheckParam import CheckParam
from .file import *
from .check.DataCheck import *
from .util.QGISUtil import *

logger = setup_logger(__name__)
check_types = [
    "值域范围",
    "非空（必填）校验",
    "逻辑校验",
    "属性质检",
]


class BaseDialog(QDialog):
    def __init__(self, parent, title=""):
        super().__init__(parent)
        self.title = title
        self.setWindowIcon(QIcon("resource/png/app_icon.png"))
        self.setWindowTitle(self.title)
        self.set_ui()

    def set_ui(self):
        self.setMinimumSize(800, 600)


class JsonTreeView(BaseDialog):
    def __init__(self, parent, json_data, file_path=None, title="质检"):
        super().__init__(parent, title)
        self.parent = parent
        self.display_items = None
        self.table = None
        self.show_failed_cb = None
        self.show_detail_cb = None
        self.all_items = None
        self.json_data = None
        self.file_path = r"D:\系统默认\桌面\质检2.json"
        self.path = r"D:\系统默认\桌面"
        self.proj_layers = parent.project.mapLayers().values()
        self.proj_layers_name = [item.name() for item in self.proj_layers]

        # layout = QVBoxLayout()
        # with open(self.file_path, mode='r', encoding="UTF-8", errors=None) as f:
        #     self.json_data = json.load(f)
        self.setWindowTitle("数据质检：林草湿更新成果")
        self.resize(900, 600)
        self.setup_ui()
        # self.load_data()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        # 标题
        file_layout = QHBoxLayout()
        label = QLabel("质检文件：")
        file_input = QLineEdit()
        file_layout.addWidget(label)
        file_layout.addWidget(file_input)
        file_layout.setStretchFactor(file_input, 1)  # 输入框拉伸
        but = QPushButton("选择文件")
        but.clicked.connect(lambda: self.open_file(file_input))

        file_layout.addWidget(but)
        main_layout.addLayout(file_layout)  # 添加到主布局

        data_source_layout = QHBoxLayout()
        data_source_label = QLabel("数据源：")
        self.data_source_input = QComboBox()
        self.data_source_input.addItems(self.proj_layers_name)
        self.data_source_input.setCurrentIndex(-1)

        data_source_layout.addWidget(data_source_label)
        data_source_layout.addWidget(self.data_source_input)
        data_source_layout.setStretchFactor(self.data_source_input, 1)

        main_layout.addLayout(data_source_layout)

        # 控制按钮
        control_layout = QHBoxLayout()
        self.show_detail_cb = QCheckBox("显示详情")
        self.show_detail_cb.setChecked(True)
        self.show_failed_cb = QCheckBox("显示未通过项")

        self.def_but = QPushButton("加载默认")

        self.def_but.clicked.connect(lambda: self.load_default_data(f"resource/质检2.json"))
        self.show_detail_cb.stateChanged.connect(self.refresh_display_detail)
        self.show_failed_cb.stateChanged.connect(self.refresh_display_fail)

        control_layout.addWidget(self.show_detail_cb)
        control_layout.addWidget(self.show_failed_cb)
        control_layout.addWidget(self.def_but)
        control_layout.addStretch()

        main_layout.addLayout(control_layout)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["质检名称", "质检状态", "错误数"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)

        main_layout.addWidget(self.table)

        var2 = QHBoxLayout()
        but2 = QPushButton("导出")
        var2.addWidget(but2)
        but3 = QPushButton("确认")
        var2.addWidget(but3)
        but3.clicked.connect(self.start_check)
        but4 = QPushButton("取消")
        var2.addWidget(but4)
        var2.addStretch()

        but2.clicked.connect(self.export_data)

        main_layout.addLayout(var2)
        self.setLayout(main_layout)

    def start_check(self):
        check_param = []
        self._get_check_param(check_param, self.json_data["items"])
        for item in check_param:
            logger.info(f"当前进行的质检项目:{item['checkItemName']}")
            layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
            var = json.loads(item["checkParam"])
            param = CheckParam(layer=layer, check_name=item["checkItemName"],
                               field_name=var["fieldName"], where=var["where"], check_id=item["id"])
            features_ = check_service[item["checkType"]].check(param)
            features = list(features_)

            item["noPassCount"] = len(features)
            if len(features) > 0:
                pass
            item["isPass"] = len(features) == 0
        self.load_data()

    def _get_check_param(self, check_param, items):
        if not items:
            return
        for item in items:
            if "items" not in item:
                check_param.append(item)
            else:
                self._get_check_param(check_param, item["items"])

    def export_data(self):
        path = open_file_dir(self.parent, True)
        fileName = f"导出结果-{uuid.uuid4().hex}.json"
        file_path = os.path.join(path, fileName)
        write_data(file_path, json.dumps(self.json_data, ensure_ascii=False, indent=4))

    def show_context_menu(self, position):
        """显示右键菜单"""
        try:
            # 获取点击位置的索引
            logger.info(f"当前选择的数据源：{self.data_source_input.currentText()}")
            if not self.data_source_input.currentText():
                return
            layer = self.parent.project.mapLayersByName(self.data_source_input.currentText())[0]
            fields = layer.fields()
            self.field_name = [item.name() for item in fields]
            logger.info(f"获取到数组{self.field_name}")

            index = self.table.indexAt(position)

            # 创建菜单
            menu = QMenu(self.table)

            if index.isValid():
                # 表格单元格上右键
                row = index.row()
                current_data = self.display_items[row]

                edit_action = QAction("编辑当前规则")
                edit_action.triggered.connect(lambda: self.create_check_item(False, item=current_data))
                menu.addAction(edit_action)
                del_action = QAction("删除")
                # edit_action.triggered.connect(lambda: self.view_cell(row, col))
                if not current_data["pass"]:
                    show_data_action = QAction("查看错误数据")
                    item = current_data["original_item"]
                    var = json.loads(item["checkParam"])
                    param = CheckParam(layer=layer, check_name=item["checkItemName"],
                                       field_name=var["fieldName"], where=var["where"], check_id=item["id"])
                    check_service[item["checkType"]]._build_sql(param)
                    show_data_action.triggered.connect(lambda: self.parent.open_attribute_table(layer=layer,
                                                                                                filter_str=param.where))

                    menu.addAction(show_data_action)

                menu.addAction(del_action)

            else:
                edit_action = QAction("新建规则")
                edit_action.triggered.connect(self.add_new_check_item)
                menu.addAction(edit_action)

            menu.exec_(self.table.viewport().mapToGlobal(position))
        except Exception as e:
            logger.error(e)

    # def edit_current(self, is_add, check_vo=None):
    #     dia = CheckConfigDialog(is_add, check_vo)
    #     dia.exec_()

    def add_new_check_item(self):
        sendMessageToTopParent(2, "功能待开发！", 5)
        self.close()

    def create_check_item(self, is_add, item=None):
        logger.info(f"当前编辑元素：{item}")
        item = item["original_item"]
        check_vo = CheckVo(checkItemName=item["checkItemName"], checkType=item["checkType"], id=item["id"],
                           isApply=item["isApply"], items=item.get("items", None),
                           checkParam=item.get("checkParam", None), pId=None
                           , isPass=None, noPassCount=None)
        self.check_dia = CheckConfigDialog(is_add, self.parent, columns=self.field_name, check_vo=check_vo)
        self.check_dia.save_edit.connect(self.edit_save)

        self.check_dia.exec_()

    def load_data(self):
        """解析 JSON 并构建扁平化数据列表"""
        self.all_items = []
        self.all_items.append({
            "name": self.json_data["checkItemName"],
            "level": 0,
            "checked": self.json_data.get("isApply", 0) == 1,
            "has_children": self.json_data.get("items") is not None and len(self.json_data["items"]) > 0,
            "original_item": self.json_data,
            "pass": self.json_data["isPass"] if "isPass" in self.json_data else True,
            "noPassCount": self.json_data["noPassCount"] if "noPassCount" in self.json_data else 0,
            "isCheckItem": "items" not in self.json_data
        })

        self._parse_json_items(self.json_data.get("items", []), level=1)
        self.refresh_display()

    def _parse_json_items(self, items, level=0):
        """递归解析 JSON 项"""
        if not items:
            return

        for item in items:
            # if level == 3:
            #     cp = json.loads(item["checkParam"])
            #     cp["where"] = ",".join(cp["domainValues"]) if item["checkType"] == 0 else cp["where"]
            #     cp["where"] = cp["logicalSql"] if item["checkType"] == 2 else cp["where"]
            #     item["checkParam"] = json.dumps(cp, ensure_ascii=False, indent=4)

            # 主项
            self.all_items.append({
                "name": item["checkItemName"],
                "level": level,
                "checked": item.get("isApply", 0) == 1,
                "has_children": item.get("items") is not None and len(item["items"]) > 0,
                "original_item": item,
                "pass": item["isPass"] if "isPass" in item else True,
                "noPassCount": item["noPassCount"] if "noPassCount" in item else 0,
                "isCheckItem": "items" not in item
            })

            # 子项
            if item.get("items"):
                self._parse_json_items(item["items"], level + 1)

    def refresh_display_detail(self):
        show_detail = self.show_detail_cb.isChecked()
        self.show_failed_cb.setChecked(not show_detail)
        self.refresh_display()

    def refresh_display_fail(self):
        show_failed = self.show_failed_cb.isChecked()
        self.show_detail_cb.setChecked(not show_failed)
        self.refresh_display()

    def refresh_display(self):
        show_detail = self.show_detail_cb.isChecked()
        show_failed = self.show_failed_cb.isChecked()
        # 没有加载质检文件 直接返回
        if self.all_items is None:
            return
        # 过滤数据
        self.display_items = []
        for item in self.all_items:
            # 如果不显示详情，跳过子项（level > 0）
            # if not show_detail and item["level"] > 0:
            #     continue
            self.display_items.append(item)
        # 筛选失败的
        if show_failed:
            # for row, item in enumerate(self.display_items):
            #     print((item.get("pass") is False and item["isCheckItem"]) or not item["isCheckItem"])
            self.display_items = [item for row, item in enumerate(self.display_items)
                                  if (item.get("pass") is False and item["isCheckItem"]) or not item["isCheckItem"]]
        # 更新表格
        self.table.setRowCount(len(self.display_items))

        for row, item in enumerate(self.display_items):
            # 质检名称（带复选框和缩进）
            name_widget = QWidget()
            name_layout = QHBoxLayout()
            name_layout.setContentsMargins(5, 0, 5, 0)
            name_layout.setSpacing(5)

            checkbox = QCheckBox()
            checkbox.setChecked(item["checked"])
            checkbox.setEnabled(False)  # 只读显示

            # 缩进：每级2个中文空格
            indent = "　　" * item["level"]
            name_label = QLabel(f"{indent}{item['name']}")

            name_layout.addWidget(checkbox)
            name_layout.addWidget(name_label)
            name_layout.addStretch()
            name_widget.setLayout(name_layout)

            self.table.setCellWidget(row, 0, name_widget)

            # 质检状态（从实际质检结果获取，这里临时显示）
            status = "通过" if item.get("pass", True) else "不通过"
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, status_item)

            # 错误数（从实际质检结果获取，这里临时显示）
            error_count = item["noPassCount"]
            error_item = QTableWidgetItem(str(error_count))
            error_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, error_item)

    def _get_error_count(self, item):
        """获取错误数 - 需要根据实际质检结果实现"""
        # 这里是示例逻辑
        check_name = item.get("checkItemName", "")
        if "地类" in check_name:
            return 0
        elif "校验" in check_name:
            return 2
        else:
            return 0

    def open_file(self, var):
        file_dia = QFileDialog(self, "选择质检文件", self.path, "json(*.json)")
        file_dia.setModal(True)
        file_dia.setFileMode(QFileDialog.ExistingFile)
        if file_dia.exec_() == QFileDialog.Accepted:
            selected_files = file_dia.selectedFiles()
            self.file_path = selected_files[0]
            var.setText(self.file_path)

            # 解析质检文件
            with open(self.file_path, mode='r', encoding="UTF-8", errors=None) as f:
                self.json_data = json.load(f)
                self.load_data()

    def load_default_data(self, path):
        with open(path, mode='r', encoding="UTF-8", errors=None) as f:
            self.json_data = json.load(f)
            self.load_data()

    def edit_save(self, data):
        try:
            c_item = self.find_check_obj(data.id, self.json_data["items"])
            c_item["checkItemName"] = data.checkItemName
            c_item["checkType"] = data.checkType
            c_item["isApply"] = data.isApply
            c_item["checkParam"] = data.checkParam
            self.load_data()
            self.check_dia.close()
        except Exception as e:
            logger.error(e)

    def find_check_obj(self, id, items):
        if items is None:
            return None

        for item in items:
            if item["id"] == id:
                return item
            if "items" in item:
                child_result = self.find_check_obj(id, item["items"])
                if child_result is not None:
                    return child_result
        return None


class CheckConfigDialog(BaseDialog):
    save_edit = pyqtSignal(object)

    def __init__(self, is_add, parent, columns=None, check_vo=None):
        super().__init__(parent, "新增质检配置" if is_add else "编辑质检配置")
        self.filter_button = None
        self.option_combo = None
        self.apply_radio = None
        self.name_input = None
        self.filter_input = None
        self.check_vo = check_vo
        self.coulmns = columns
        try:
            if self.check_vo is None:
                self.check_vo = CheckVo(checkItemName=None, checkType=None, id=None
                                        , isApply=None, items=None, checkParam=None, pId=None, isPass=None,
                                        noPassCount=None)
            self.is_add = is_add
            # self.resize(900, 600)
            self.init_ui()
        except Exception as e:
            print(str(e))

    def init_ui(self):
        try:
            self.main_layout = QVBoxLayout()
            self.main_layout.setSpacing(15)  # 增加整体间距
            self.main_layout.setContentsMargins(20, 20, 20, 20)  # 边距

            # 标题区域
            title_label = QLabel("质检项配置信息")
            title_label.setStyleSheet("font-size: 14px; color: #333;")
            self.main_layout.addWidget(title_label)

            # 分隔线（可选）
            # main_layout.addSpacing(10)

            # 名称行
            name_layout = QHBoxLayout()
            name_label = QLabel("名称:")
            name_label.setFixedWidth(60)  # 固定标签宽度，对齐美观
            self.name_input = QLineEdit()
            self.name_input.textEdited.connect(self.check_item_name_change)
            if self.check_vo is not None:
                self.name_input.setText(self.check_vo.checkItemName)
            self.name_input.setPlaceholderText("请输入质检项名称")
            self.name_input.setMinimumWidth(200)
            name_layout.addWidget(name_label)
            name_layout.addWidget(self.name_input)
            name_layout.addStretch()  # 右侧留白
            self.main_layout.addLayout(name_layout)

            # 质检选项 + 是否应用 行
            option_layout = QHBoxLayout()
            option_layout.setSpacing(15)

            # 是否应用
            self.apply_radio = QRadioButton("是否应用")
            self.apply_radio.clicked.connect(self.check_item_apply_change)
            if self.check_vo is not None:
                self.apply_radio.setChecked(self.check_vo.isApply if self.check_vo.isApply is not None else True)
            else:
                self.apply_radio.setChecked(True)
            # self.apply_radio.setFixedWidth(100)  # 控制宽度，避免拉伸

            # 质检选项
            option_label = QLabel("质检选项:")
            option_label.setFixedWidth(60)
            self.option_combo = QComboBox()
            self.option_combo.addItems(check_types)

            self.option_combo.setMinimumWidth(150)
            option_layout.addWidget(self.apply_radio)
            option_layout.addWidget(option_label)
            option_layout.addWidget(self.option_combo)
            option_layout.addStretch()  # 右侧留白
            self.option_combo.currentIndexChanged.connect(self.check_item_type_change)

            self.main_layout.addLayout(option_layout)

            # 过滤条件行
            filter_layout = QVBoxLayout()
            self.filter_label = QLabel("过滤条件（选填）:")
            # self.filter_label.setFixedWidth(120)
            self.filter_input = QTextEdit()
            self.filter_input.setFixedHeight(50)
            if self.check_vo is not None:
                c_param = json.loads(self.check_vo.checkParam)
                self.filter_input.setText(f"{c_param['where']}")
            else:
                self.filter_input.setPlaceholderText("支持表达式，如 field > 10")
            self.filter_input.textChanged.connect(self.check_item_where_change)

            filter_layout.addWidget(self.filter_label)
            filter_layout.addWidget(self.filter_input)
            # filter_layout.addWidget(self.filter_button)
            filter_layout.addStretch()  # 右侧留白
            self.main_layout.addLayout(filter_layout)

            # 添加字段
            self.build_custom_ui(self.coulmns)

            # 底部添加一个弹性空间，防止控件贴底
            self.main_layout.addStretch(1)
            # 下拉框移到最后
            if self.check_vo is not None:
                index = self.check_vo.checkType if self.check_vo.checkType is not None else 0
                self.option_combo.setCurrentText(check_types[index])
                self.option_combo.setCurrentIndex(index)
                self.check_item_type_change()

            var2 = QHBoxLayout()
            but3 = QPushButton("确认")
            var2.addWidget(but3)
            but4 = QPushButton("取消")
            var2.addWidget(but4)
            var2.addStretch()
            but3.clicked.connect(lambda: self.save_edit.emit(self.check_vo))
            but4.clicked.connect(lambda: self.close())
            self.main_layout.addLayout(var2)
            self.setLayout(self.main_layout)
        except Exception as e:
            logger.error(f"组件初始化发生错误：{e}")

    """
     0-> 值域
     1-> 非空
     2-> 逻辑
    """

    def build_custom_ui(self, columns, check_type: int = 0):
        # if check_type == 0:
        self.table_widget = QTableWidget()
        self.table_widget.setRowCount(len(columns))
        self.table_widget.setColumnCount(2)

        self.table_widget.setHorizontalHeaderLabels(["是否应用", "质检字段"])
        self.table_widget.setSelectionMode(QTableWidget.ExtendedSelection)
        # 选择行为：SelectRows选整行；SelectItems选单元格；SelectColumns选整列
        self.table_widget.setSelectionBehavior(QTableWidget.SelectRows)
        # 取消单元格编辑功能（纯展示+选择）
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        j = json.loads(self.check_vo.checkParam)
        for row in range(len(columns)):
            check_item = QTableWidgetItem()
            # check_item.setCheckState(Qt.Unchecked)  # 默认未选中
            # 让复选框居根据数据动态渲染
            check_item.setCheckState(Qt.Checked if columns[row] in j["fieldName"] else Qt.Unchecked)
            check_item.setTextAlignment(Qt.AlignCenter)
            self.table_widget.setItem(row, 0, check_item)

            name_item = QTableWidgetItem(columns[row])
            self.table_widget.setItem(row, 1, name_item)

        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_widget.cellClicked.connect(self.select_columns)
        # 最后一列拉伸填充剩余空间
        self.table_widget.horizontalHeader().setStretchLastSection(True)
        self.table_widget.setFixedHeight(300)  # 固定高度为400px
        v_l = QVBoxLayout()

        label = QLabel("选择字段")
        v_l.addWidget(label)
        v_l.addWidget(self.table_widget)
        v_l.addStretch(1)

        self.main_layout.addLayout(v_l)

    def select_columns(self, row, col):
        try:
            if col == 0:
                j = json.loads(self.check_vo.checkParam)

                check_item = self.table_widget.item(row, 0)
                field_item = self.table_widget.item(row, 1)
                if check_item.checkState() == Qt.Checked:
                    j["fieldName"].append(field_item.text())
                else:
                    j["fieldName"].remove(field_item.text())
                self.check_vo.checkParam = json.dumps(j)
        except Exception as e:
            logger.error(e)

    def check_item_name_change(self):
        self.check_vo.checkItemName = self.name_input.text()

    def check_item_apply_change(self):
        is_apply = 1 if self.apply_radio.isEnabled() else 0
        self.check_vo.isApply = is_apply

    def check_item_type_change(self):
        # current_index = [index for index, item in check_types if item == self.option_combo.currentText][0]
        current_index = [index for index, item in enumerate(check_types) if item == self.option_combo.currentText()][0]
        # if current_index == 0:
        self.filter_label.setText("过滤条件（选填）:" if not current_index == 0 else "值域数据（使用,分割）")
        self.filter_input.setPlaceholderText(
            "支持表达式，如 field > 10" if not current_index == 0 else "'value1','value2','value3'......")

        self.check_vo.checkType = current_index

    def check_item_where_change(self):
        v = self.filter_input.toPlainText()
        j = json.loads(self.check_vo.checkParam)
        j["where"] = v
        self.check_vo.checkParam = json.dumps(j)


class CheckVo:
    def __init__(self, **kwargs):
        self.checkItemName = kwargs["checkItemName"]
        self.checkType = kwargs["checkType"]
        self.id = kwargs["id"]
        self.isApply = kwargs["isApply"]
        self.items = kwargs["items"]
        self.checkParam = kwargs["checkParam"]
        self.pId = kwargs["pId"]
        self.isPass = kwargs["isPass"]
        self.noPassCount = kwargs["noPassCount"]
