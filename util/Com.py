from PyQt5.QtWidgets import QWidget, QPushButton


def print_all_controls(widget, indent=0):
    """
    递归遍历所有控件，打印按钮的对象名、文本、类型
    :param widget: 起始父控件
    :param indent: 缩进（用于区分控件层级，更直观）
    """
    # 定义缩进符（区分层级，便于查看嵌套关系）
    indent_str = "  " * indent

    # 1. 若是 QPushButton（按钮类型），优先打印详细信息
    if isinstance(widget, QPushButton):
        btn_name = widget.objectName()  # 按钮对象名（关键）
        btn_text = widget.text().strip()  # 按钮显示文本
        btn_class = widget.__class__.__name__  # 按钮类型
        print(f"{indent_str}【按钮】对象名：{btn_name}，文本：{btn_text}，类型：{btn_class}")

    # 2. 打印其他控件（可选，若只需按钮，可注释）
    else:
        widget_name = widget.objectName()
        widget_class = widget.__class__.__name__
        if hasattr(widget, "text"):
            widget_text = widget.text().strip()
            print(f"{indent_str}【其他控件】对象名：{widget_name}，文本：{widget_text}，类型：{widget_class}")
        else:
            print(f"{indent_str}【其他控件】对象名：{widget_name}，类型：{widget_class}")

    # 3. 递归遍历子控件（深层嵌套）
    for child in widget.children():
        if isinstance(child, QWidget):  # 仅遍历控件类型，避免非控件对象
            print_all_controls(child, indent + 1)