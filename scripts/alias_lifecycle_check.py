"""别名生命周期自检(在真实 QGIS 里跑, 对**老 .db 后缀**的 gpkg 内容库逐阶段观察).

用法:
    python-qgis-ltr.bat scripts/alias_lifecycle_check.py "D:/你的数据目录/老库.db"

**仅老 .db 兼容路径需要别名**: GDAL(3.11)按文件名后缀裁决驱动, 老 .db 的
GPKG 内容会被 SQLite 驱动抢先认领。**新规本体即 .gpkg 的库不走别名** ——
``gpkg_open_path`` 对 .gpkg 原样直开, 目录永远只有库文件(影像直开也不写
.aux.xml, 见 local_layer_loader._open_gdal_raster_no_pam), 无需跑本脚本。

它对给定 .db 依次执行完整生命周期, 每个阶段打印目录里 .gpkg/.db 的:
    时间  大小  inode  nlink  名字
并标注该 .gpkg 是「硬链接(同 inode, 零拷贝)」还是「真拷贝(异 inode,
磁盘翻倍——说明硬链接建不了, 落了 copyfile 回退)」。

阶段: A 加载前 → B 加载后(别名出现) → C 双图层(矢量+影像)挂工程 → D 移除
图层后(别名应消失) → E 启动清扫(模拟重启兜底)。结束前显示磁盘剩余空间
变化, 判断到底占不占双份。
"""
import sys
import os
import time

DB = sys.argv[1] if len(sys.argv) > 1 else ""

sys.modules["pysqlite3"] = __import__("sqlite3")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctypes  # noqa: E402
from pathlib import Path  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject,
    QgsApplication,
    QgsCoordinateReferenceSystem,
)
from qgis.PyQt.QtCore import QTimer  # noqa: E402
from app.core.util.local_db_util import (  # noqa: E402
    gpkg_open_path,
    release_gpkg_alias,
    sweep_startup_gpkg_aliases,
)

if not DB or not Path(DB).is_file():
    print(
        "用法: python-qgis-ltr.bat scripts/alias_lifecycle_check.py \"老库.db\"\n"
        "需要一个真实存在的**老 .db 后缀 gpkg 内容库**(别名兼容演示); 新规\n"
        ".gpkg 本体库不走别名(gpkg_open_path 原样直开), 无需跑本脚本。"
    )
    raise SystemExit(2)

_DIR = Path(DB).parent


def _free() -> int:
    v = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(_DIR), None, None, ctypes.byref(v))
    return v.value


def _list_files(tag: str) -> None:
    print(f"\n--- {tag} ---")
    for f in sorted(_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() not in (".gpkg", ".db") or not f.is_file():
            continue
        st = f.stat()
        note = ""
        if f.suffix.lower() == ".gpkg":
            db = f.with_suffix(".db")
            if db.is_file() and db.stat().st_ino == st.st_ino:
                note = "  ← 硬链接别名(同 inode, 零拷贝)"
            else:
                note = "  ← 真拷贝/独立文件(异 inode, 磁盘双份!)"
        print(f"{time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}  "
              f"{st.st_size/1e6:8.1f}MB  ino={st.st_ino}  n={st.st_nlink}  {f.name}{note}")
    print(f"磁盘剩余: {_free()/1e9:.2f}GB")


app = QgsApplication([], False)
app.initQgis()
proj = QgsProject.instance()
proj.setCrs(QgsCoordinateReferenceSystem("EPSG:4490"))

_list_files("A 加载前")

alias = gpkg_open_path(DB)
_list_files("B 加载后(别名应在此出现)")

lv = QgsVectorLayer(f"{alias}|layername=ldsp_ldtb", "v", "ogr")
li = QgsRasterLayer(alias, "i", "gdal")
print("\n图层 valid: 矢量", lv.isValid(), "| 影像", li.isValid())
lid = proj.addMapLayer(lv, False).id()
iid = proj.addMapLayer(li, False).id()
_list_files("C 双图层挂工程(图层在用 → 别名必须存在)")

state = {"released": False}


def _poll(n: int = 1):
    if release_gpkg_alias(alias):
        state["released"] = True
        _list_files("D 图层移除后 ~0.5-3s(别名应消失)")
        QTimer.singleShot(200, _simulate_restart)
    elif n < 10:
        QTimer.singleShot(500, lambda: _poll(n + 1))
    else:
        print("\n!! 5s 内未能释放别名(可能还有句柄占用), 继续走 E 阶段")
        QTimer.singleShot(200, _simulate_restart)


def _simulate_restart():
    # 模拟残留 + 启动清扫(真正重启时由 main_window 2s 后自动执行)
    gpkg_open_path(DB)  # 重建一个"残留"
    _list_files("E-1 模拟上一会话残留")
    n = sweep_startup_gpkg_aliases()
    _list_files(f"E-2 启动清扫(应删除残留 {n} 个)")
    app.exit(0)


def _step():
    proj.removeMapLayer(lid)
    proj.removeMapLayer(iid)
    # 与真实 _remove_layer 一致: 不清 python 引用, 由 QGIS 异步析构图层
    # (同步 del 栅格层可能撞 GDAL 析构线程竞争而卡死主线程)
    QTimer.singleShot(150, _poll)


QTimer.singleShot(300, _step)
QTimer.singleShot(20000, lambda: app.exit(0))
app.exec()
print("\n检查要点: B/C 阶段别名必在(图层用着, 物理必须); D/E 后应只剩 .db;")
print("任何阶段的 .gpkg 若标了「真拷贝」→ 你的环境建不了硬链接(网络盘/exFAT), 需换盘或改方案。")
