from PyQt5.QtWidgets import QFileDialog
from component.Logger import setup_logger

logger = setup_logger(__name__)


def open_file_dir(parent, is_dir=False, title="选择文件夹", init_path="", **kwargs):
    """

    :param parent:
    :param is_dir:
    :param title:
    :param init_path:
    :param kwargs: filter
    :return:
    """
    if is_dir:
        folder_path = QFileDialog.getExistingDirectory(
            parent,  # 父窗口
            title,  # 标题
            init_path,  # 初始目录（可设为上次路径）
            QFileDialog.ShowDirsOnly  # 仅显示目录（可省略，此函数默认只选文件夹）
        )
        return folder_path
    else:
        # 选择单个文件
        file_dia = QFileDialog(parent, "选择路径", "", kwargs["filter"])
        file_dia.setModal(True)
        file_dia.setFileMode(QFileDialog.ExistingFile)
        if file_dia.exec_() == QFileDialog.Accepted:
            selected_files = file_dia.selectedFiles()
            return selected_files[0]


def write_data(file_path, data):
    try:
        with open(file_path, "w", encoding="UTF-8") as f:
            f.write(data)
        return True
    except Exception as e:
        logger.error(f"写入文件发生错误：{file_path}")
        return False

