"""在线底图。

在线底图的定义读取、新增（模板 / 自定义）、编辑、删除与 Key 管理，
以及配置文件的持久化。

由 :class:`~app.ui.main_window.main_window.MainWindow` 多继承组合，方法只依赖
``self`` 上的共享状态。
"""

from __future__ import annotations

from dataclasses import replace

from qgis.core import Qgis
from qgis.PyQt.QtWidgets import (
    QDialog,
)

from app.core.domain.basemap import (
    build_online_basemap_layer,
    canonical_basemap_url,
    install_referer_preprocessor,
)
from app.env.config import (
    add_online_basemap_group_to_yaml,
    append_online_basemap_to_yaml,
    load_online_basemap_groups,
    load_online_basemaps,
    reassign_online_basemap_group_in_basemaps,
    remove_online_basemap_from_yaml,
    remove_online_basemap_group_from_yaml,
    reorder_online_basemap_groups_in_yaml,
    set_map_keys,
    update_online_basemap_in_yaml,
)
from app.infra.logging import get_logger
from app.ui.common.message_box import MessageBox
from app.ui.dialogs.custom_basemap import CustomBasemapDialog
from app.ui.dialogs.edit_basemap import EditBasemapDialog
from app.ui.dialogs.manage_keys import ManageKeysDialog
from app.ui.docks.online_basemap_dock import OnlineBasemapDock

logger = get_logger(__name__)


class OnlineBasemapMixin:
    """OnlineBasemapMixin：见模块 docstring。"""

    def _on_online_basemap(self) -> None:
        """在线底图：懒建右侧 Dock，按独立清单文件刷新列表。"""

        dock = getattr(self, "_online_basemap_dock", None)
        if dock is None:
            dock = OnlineBasemapDock(
                self,
                on_add=self._add_online_basemap,
                on_add_template=self._add_online_basemap_template,
                on_add_custom=self._add_custom_basemap,
                on_manage_keys=self._manage_basemap_keys,
                on_edit=self._edit_online_basemap,
                on_delete=self._delete_online_basemap,
                on_add_group=self._add_online_basemap_group,
                on_rename_group=self._rename_online_basemap_group,
                on_delete_group=self._delete_online_basemap_group,
                on_move_to_group=self._move_online_basemap_to_group,
                on_reorder_groups=self._reorder_online_basemap_groups,
            )
            self._online_basemap_dock = dock
            self.place_right_dock(dock)
        dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
        dock.show()
        dock.raise_()

    def _online_basemaps_config_path(self):
        """``online_basemap_config.yaml`` 实际路径（与 app.yaml 同目录）。

        在线底图清单独立于 ``config/app.yaml``（键/底图仍在 app.yaml，清单在此
        固定名文件）；QGIS_DEV_CONFIG 指向哪份 app.yaml，清单就跟到同目录。
        """
        return self._map_config_path().with_name("online_basemap_config.yaml")

    def _online_basemap_defs(self):
        """读取独立清单文件里的在线底图（文件缺失 / 读取失败返回空列表）。"""
        try:

            return load_online_basemaps(self._online_basemaps_config_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取在线底图清单失败：%s", exc, exc_info=True)
            return []

    def _online_basemap_groups(self) -> list[str]:
        """读取独立清单文件里的分组列表（文件缺失 / 读取失败返回空列表）。

        与 :meth:`_online_basemap_defs` 同步供 :meth:`_on_online_basemap` 一次性刷新
        Dock 树：v2 dict schema 的 ``groups:`` 段是 Dock 显示顺序的权威。
        """
        try:
            return load_online_basemap_groups(self._online_basemaps_config_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取在线底图分组失败：%s", exc, exc_info=True)
            return []

    @staticmethod
    def _basemap_conflicts_with(d, others) -> bool:
        """``d`` 与 ``others`` 里任一条同源 → ``True``（添加去重 / 编辑撞名共用）。

        同源判定：``type`` 相同 + ``url`` 经 :func:`canonical_basemap_url` 归一后
        相同 + ``layers`` 相同（两个 WMS 可能共享服务 url 但 layers 不同；xyz
        layers 为空 → 等价于 url 判重）。url 归一把老配置内嵌真实 tk 的 url 与
        ``{tk}`` 模板判等时互认（tk=cb75… == tk={tk}），去重/撞名不失效。
        """

        if not d.url:
            return False
        return any(
            (x.type or "xyz") == (d.type or "xyz")
            and canonical_basemap_url(x.url) == canonical_basemap_url(d.url)
            and x.layers == d.layers
            for x in others
        )

    def _add_online_basemap_template(self, d) -> bool:
        """把一条内置模板/自定义底图写回 ``online_basemap_config.yaml`` 并刷新树。

        右键「添加在线底图」/「添加自定义底图」的回调：Dock 只负责把条目递给
        本方法，真正持久化（文本追加到独立清单文件）与刷新 Dock 树在这里做；
        同源（url 相同）条目已存在则只提示不重复加。返回是否成功。
        """
        try:

            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)  # 缺失/空 = 空清单（可从模板加）
            if self._basemap_conflicts_with(d, current):
                self.messageBar.pushMessage(
                    f"该底图已在在线底图清单中：{d.name}",
                    level=Qgis.MessageLevel.Info,
                    duration=2,
                )
                return True
            append_online_basemap_to_yaml(path, d)
            # 写回后重读并刷新 dock 树（新增条目立刻出现在分组里）
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs())
            self.messageBar.pushMessage(
                f"已添加在线底图：{d.name}（已写入 online_basemap_config.yaml）",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            logger.info("添加在线底图类型：%s", d.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("添加在线底图类型失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"添加在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _add_custom_basemap(self, groups: list[str]) -> bool:
        """Dock 右键「添加在线底图 ▸ 自定义底图...」：弹窗配置后走模板同一条持久化路径。

        ``groups`` 为 Dock 当前 ``_groups_order`` 拷贝，作为 dialog 「分组」字段
        的可编辑下拉预设项（已存在分组一键选 / 键入新名 = "先建组后加条目"连贯
        动作）。弹窗组装好 :class:`OnlineBasemapDef`（type=xyz/arcgis/arcgisfeature/
        wms/wmts），委托 :meth:`_add_online_basemap_template` 写回
        ``online_basemap_config.yaml`` 并刷新 Dock 树（去重 / messageBar / 异常
        处理全复用）。取消返回 ``False``。按已定决策**不**加载画布——自定义底图
        先进树，双击再出图。
        """

        dlg = CustomBasemapDialog(self, groups=groups)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        return self._add_online_basemap_template(dlg.basemap_def())

    def _manage_basemap_keys(self) -> bool:
        """Dock 右键「管理 Key」：弹窗集中管理 provider key，写回 ``map.keys``。

        弹窗预填当前 ``map.keys``（天地图 tk / 高德预留字段），确定后经
        :func:`set_map_keys` **文本写回** ``config/app.yaml`` 的 ``map.keys``
        （只动本段，URL 仍保持 ``{tk}`` 占位，注释/其余排版保留）。取消返回
        False。改 key 只影响**新建/重载**的图层（已加载图层 source 已焊死），
        文案已如实提示。
        """

        cfg = self._load_map_config()
        if cfg is None:
            self.messageBar.pushMessage(
                "地图服务 Key 管理：配置文件读取失败",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        dlg = ManageKeysDialog(self, cfg.map.keys)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        new_keys = dlg.keys()
        try:
            set_map_keys(self._map_config_path(), new_keys)
        except Exception as exc:  # noqa: BLE001
            logger.error("写回 map.keys 失败：%s", exc, exc_info=True)
            self.messageBar.pushMessage(
                f"保存地图服务 Key 失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        self.messageBar.pushMessage(
            "已保存地图服务 Key：新建 / 下次加载的底图即生效（已加载底图需重载）",
            level=Qgis.MessageLevel.Info,
            duration=3,
        )
        logger.info(
            "已更新地图服务 Key：tianditu_tk=%s gaode_key=%s",
            "***" if new_keys.tianditu_tk else "",
            "***" if new_keys.gaode_key else "",
        )
        return True

    def _edit_online_basemap(self, d) -> bool:
        """Dock 叶子右键「编辑连接」：弹窗编辑清单条目，确定后文本写回并刷新树。

        对话框（:class:`EditBasemapDialog`）复用「添加自定义底图」的字段并预填现有
        条目（名称/分组/类型/url/类型参数/反盗链 Referer），就近提供 API key 编辑——
        key 真值仍**集中管理**（url 里 ``{tk}`` / ``{gaode_key}`` 占位符引用
        ``map.keys``，改一处全站生效），确定后分两处落盘：

        - 清单条目经 :func:`update_online_basemap_in_yaml` **原位文本替换**回
          ``online_basemap_config.yaml``（注释/其余条目保留）并刷新 Dock 树；
        - key 有改动（或老配置内嵌 tk 被迁移成占位符）时，真值经
          :func:`set_map_keys` 只动 ``app.yaml`` 的 ``keys`` 段。

        改后的连接下次双击才按新参数出图（工程里已加载的旧图层不受影响，需移除后
        重新「添加到画布」）。与清单里其它条目撞源（同 type+归一 url+layers）则
        拒绝保存只提示。返回是否成功。取消返回 False。
        """
        try:
            cfg = self._load_map_config()
            if cfg is None:
                self.messageBar.pushMessage(
                    "编辑在线底图：配置文件读取失败",
                    level=Qgis.MessageLevel.Critical,
                    duration=5,
                )
                return False
            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)
            if d not in current:
                self.messageBar.pushMessage(
                    "在线底图清单已变化（找不到该连接），请刷新后重试",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False

            dlg = EditBasemapDialog(self, def_=d, keys=cfg.map.keys)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            new_def = dlg.basemap_def()
            key_override = dlg.keys_override()
            if new_def == d and key_override is None:
                self.messageBar.pushMessage("在线底图未做修改", level=Qgis.MessageLevel.Info, duration=2)
                return True
            if new_def != d and self._basemap_conflicts_with(new_def, [x for x in current if x != d]):
                self.messageBar.pushMessage(
                    f"编辑后与清单中已有底图同源：{new_def.name}（未保存）",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False

            # 先写 key（集中真值，可能被 url 占位符引用），再原位替换清单条目
            if key_override is not None:
                set_map_keys(self._map_config_path(), key_override)
            if new_def != d:
                update_online_basemap_in_yaml(path, d, new_def)
                dock = getattr(self, "_online_basemap_dock", None)
                if dock is not None:
                    dock.set_layers(self._online_basemap_defs())

            parts = []
            if new_def != d:
                parts.append(f"已更新在线底图：{new_def.name}")
            if key_override is not None:
                parts.append("服务 Key 已同步（新建 / 重载底图生效）")
            self.messageBar.pushMessage("；".join(parts), level=Qgis.MessageLevel.Info, duration=3)
            logger.info(
                "更新在线底图连接：%s → %s%s",
                d.name,
                new_def.name if new_def != d else d.name,
                "（key 同步）" if key_override is not None else "",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("编辑在线底图失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"编辑在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _delete_online_basemap(self, d) -> bool:
        """Dock 叶子右键「删除连接」：确认后从清单文件移除条目并刷新 Dock 树。

        删除是**破坏性**操作，确认弹窗（Modal MessageBox）归 MainWindow 所有——
        Dock 只把要删的条目递给本方法。只移除 ``online_basemap_config.yaml`` 里的
        配置条目：工程里**已加载**的底图图层不受影响（source 已焊死），但下次无法
        再从列表添加该连接。找不到 ``d``（清单被外部改动）或确认取消都不落盘。
        返回是否成功删除（取消 / 失败为 False）。
        """
        try:

            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)
            if d not in current:
                self.messageBar.pushMessage(
                    "在线底图清单已变化（找不到该连接），请刷新后重试",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False
            ret = MessageBox.question(
                self,
                "删除在线底图",
                f"确定从在线底图清单中删除「{d.name}」吗？\n\n"
                "已加载到工程的图层不受影响，删除后将无法再从列表添加该连接。",
                MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                MessageBox.StandardButton.No,
            )
            if ret != MessageBox.StandardButton.Yes:
                return False

            remove_online_basemap_from_yaml(path, d)
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs())
            self.messageBar.pushMessage(
                f"已删除在线底图：{d.name}",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            logger.info("删除在线底图连接：%s", d.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("删除在线底图失败 %s：%s", getattr(d, "name", d), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"删除在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _add_online_basemap_group(self, name: str) -> bool:
        """Dock 右键「新建分组」：在 ``groups:`` 段追加 ``- name`` 并刷新树。

        校验交给 config 原语（非空 / 长度限制 / 拒绝 YAML 保留字符、重名检测）；
        错误原样弹到 messageBar（不闷拑）。返回是否成功。
        """
        if not name:
            self.messageBar.pushMessage(
                "分组名不能为空",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return False
        try:
            path = self._online_basemaps_config_path()
            add_online_basemap_group_to_yaml(path, name)
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
            self.messageBar.pushMessage(
                f"已新建分组：{name}",
                level=Qgis.MessageLevel.Info,
                duration=2,
            )
            logger.info("新建在线底图分组：%s", name)
            return True
        except ValueError as exc:
            self.messageBar.pushMessage(
                f"新建分组失败：{exc}",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("新建分组失败 %s：%s", name, exc, exc_info=True)
            self.messageBar.pushMessage(
                f"新建分组失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _rename_online_basemap_group(self, old: str, new: str) -> bool:
        """Dock 分组右键「重命名分组」：重命名分两步——先重名（新增 new）、重分配
        所有 ``group: old`` 条目为 ``new``、最后从 ``groups:`` 段删除 ``- old``。

        两步中间出了异常会留下不一致状态——但 config 原语都是文本原子写、调用
        异常多在磁盘层（权限 / 空间），该场景下保持失败比提供一个「事务包装」更
        可观察可调试。原样报 messageBar。重名与重名原名（old==new）也在此拦截。
        """
        if not new or new == old:
            self.messageBar.pushMessage(
                "分组名未变更",
                level=Qgis.MessageLevel.Info,
                duration=2,
            )
            return False
        try:
            path = self._online_basemaps_config_path()
            # 1) 新名补进 groups 段（不重才让走）
            add_online_basemap_group_to_yaml(path, new)
            # 2) 把 basemaps: 段所有 group: old 改为 group: new
            reassign_online_basemap_group_in_basemaps(path, old, new)
            # 3) 从 groups: 段删 - old
            remove_online_basemap_group_from_yaml(path, old, cascade=True)
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
            self.messageBar.pushMessage(
                f"已重命名分组：「{old}」→「{new}」",
                level=Qgis.MessageLevel.Info,
                duration=3,
            )
            logger.info("重命名在线底图分组：%s → %s", old, new)
            return True
        except ValueError as exc:
            # 包括「分组已存在」/「分组名非法」
            self.messageBar.pushMessage(
                f"重命名分组失败：{exc}",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("重命名分组失败 %s→%s：%s", old, new, exc, exc_info=True)
            self.messageBar.pushMessage(
                f"重命名分组失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _delete_online_basemap_group(self, name: str) -> bool:
        """Dock 分组右键「删除分组」：空分组直接删，非空则弹选择 dialog。

        选顶：

        - 选目标分组重分配 → 走 :func:`reassign_online_basemap_group_in_basemaps`
          + :func:`remove_online_basemap_group_from_yaml`（cascade=False 隐含 “已无引用”）；
        - 一并删除该分组下所有底图 → cascade=True 一次性踢出；
        - 取消 → 不动。

        返回是否成功。 messageBar 提示与 日志都走。
        """
        try:
            path = self._online_basemaps_config_path()
            defs = load_online_basemaps(path)
            current = load_online_basemap_groups(path)
            # 是否还有引用
            referenced = [d for d in defs if (d.group or "在线底图") == name]
            if not referenced:
                # 空分组：直接删（cascade=True 以防老版本遗留了引用未及时清理）
                remove_online_basemap_group_from_yaml(path, name, cascade=True)
                dock = getattr(self, "_online_basemap_dock", None)
                if dock is not None:
                    dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
                self.messageBar.pushMessage(
                    f"已删除空分组：{name}",
                    level=Qgis.MessageLevel.Info,
                    duration=2,
                )
                logger.info("删除在线底图分组（空）：%s", name)
                return True

            # 非空：弹选择 dialog
            other_groups = [g for g in current if g != name]
            dlg = _DeleteGroupDialog(self, name=name, other_groups=other_groups, count=len(referenced))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            choice, target = dlg.result()
            if choice == "reassign":
                if not target:
                    return False
                reassign_online_basemap_group_in_basemaps(path, name, target)
                remove_online_basemap_group_from_yaml(path, name, cascade=True)
                self.messageBar.pushMessage(
                    f"已将「{name}」下 {len(referenced)} 条底图移到「{target}」，并删除「{name}」",
                    level=Qgis.MessageLevel.Info,
                    duration=3,
                )
                logger.info("重分配+删除分组：%s → %s，count=%d", name, target, len(referenced))
            elif choice == "cascade":
                # 二次确认：破坏性
                ret = MessageBox.question(
                    self,
                    "删除分组与底图",
                    f"将同时删除「{name}」下 {len(referenced)} 条在线底图配置。\n"
                    "已加载到工程的图层不受影响，但下次无法再从列表添加。\n\n"
                    "确定继续吗？",
                    MessageBox.StandardButton.Yes | MessageBox.StandardButton.No,
                    MessageBox.StandardButton.No,
                )
                if ret != MessageBox.StandardButton.Yes:
                    return False
                remove_online_basemap_group_from_yaml(path, name, cascade=True)
                self.messageBar.pushMessage(
                    f"已连同「{name}」分组及其 {len(referenced)} 条底图一并删除",
                    level=Qgis.MessageLevel.Info,
                    duration=3,
                )
                logger.info("级联删除分组：%s，count=%d", name, len(referenced))
            else:
                return False
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
            return True
        except ValueError as exc:
            self.messageBar.pushMessage(
                f"删除分组失败：{exc}",
                level=Qgis.MessageLevel.Warning,
                duration=3,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("删除分组失败 %s：%s", name, exc, exc_info=True)
            self.messageBar.pushMessage(
                f"删除分组失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _move_online_basemap_to_group(self, d, new_group: str) -> bool:
        """Dock 叶子右键「移动到分组」：把该条目 ``group`` 改写为 ``new_group``
        并文本写回（注释/其他条目保留）。

        “不需重名校验”（Dock 侧 _append_move_to_group_submenu 已排除了本组名）；
        “与理是否实际改了” 在这里拦截（选了本组 = noop，直接返成功不落盘）。
        改完同刷 Dock 树。
        """
        if not new_group or (d.group or "在线底图") == new_group:
            return True
        try:
            path = self._online_basemaps_config_path()
            current = load_online_basemaps(path)
            if d not in current:
                self.messageBar.pushMessage(
                    "在线底图清单已变化（找不到该连接），请刷新后重试",
                    level=Qgis.MessageLevel.Warning,
                    duration=3,
                )
                return False
            new_def = replace(d, group=new_group)
            update_online_basemap_in_yaml(path, d, new_def)
            # 顺便同步 groups 段（目标 group 不存在则补进）
            try:
                add_online_basemap_group_to_yaml(path, new_group)
            except ValueError:
                pass  # 已存在 → 忽略
            dock = getattr(self, "_online_basemap_dock", None)
            if dock is not None:
                dock.set_layers(self._online_basemap_defs(), self._online_basemap_groups())
            self.messageBar.pushMessage(
                f"已将「{d.name}」移到分组「{new_group}」",
                level=Qgis.MessageLevel.Info,
                duration=2,
            )
            logger.info("移动在线底图到分组：%s → %s", d.name, new_group)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("移动在线底图到分组失败 %s→%s：%s", d.name, new_group, exc, exc_info=True)
            self.messageBar.pushMessage(
                f"移动到分组失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False

    def _reorder_online_basemap_groups(self, new_order: list[str]) -> bool:
        """Dock 分组拖拽排序后调本方法：把 ``groups:`` 段按新顺序重写 + 刷新树。

        ``new_order`` 为 Dock 拖拽完成后给的新顺序（与当前 :attr:`_groups_order`
        不同才被回调）。底层调 :func:`reorder_online_basemap_groups_in_yaml`
        写 groups 段，**不动** basemaps 段（basemaps 段是条目列表、顺序不在拖动
        语义范围；Dock 显示顺序由 :meth:`_on_online_basemap` 下次 set_layers
        以新 groups 重传保证）。

        失败 → 弹 messageBar Critical，**不**改 :attr:`_groups_order` 内存状态
        （Dock 内部已变更，下次 set_layers 以磁盘为准重同步）。
        """
        path = self._online_basemaps_config_path()
        if path is None:
            self.messageBar.pushMessage(
                "重排分组失败：找不到独立清单文件路径",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        try:
            new_groups = reorder_online_basemap_groups_in_yaml(path, list(new_order))
        except (ValueError, OSError) as exc:
            logger.error("重排分组失败 %s：%s", new_order, exc, exc_info=True)
            self.messageBar.pushMessage(
                f"重排分组失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        # 刷新树 / memory；新 groups 已透传 dock
        if self._online_basemap_dock is not None:
            self._online_basemap_dock.set_layers(
                self._online_basemap_defs(), new_groups
            )
        self.messageBar.pushMessage(
            f"已重排分组（{len(new_groups)} 个）",
            level=Qgis.MessageLevel.Info,
            duration=2,
        )
        return True

    def _add_online_basemap(self, tile) -> bool:
        """把一条在线底图配置加进工程「参考图层」分组顶部。

        ``type`` 分派：xyz 瓦片 / ArcGIS 影像服务（栅格）/ ArcGIS 要素服务
        （FeatureServer 矢量）/ OGC WMS / WMTS（见
        basemap 模块 :func:`build_online_basemap_layer`）。带 ``referer`` 的
        xyz 行先装全局 Referer 预处理（连接串 referer= 不总是生效，见 basemap
        模块 docstring）；工程里已有同源图层则只提示、不重复加。
        返回是否成功，供 :class:`OnlineBasemapDock` 回调。

        期间状态栏亮起「正在加载图层」指示（含 FeatureServer 矢量整层拉取的
        长等待），try/finally 保证成功 / 失败都熄灭。
        """
        self._begin_layer_loading(f"正在加载图层：{tile.name}")
        try:

            if tile.referer:
                install_referer_preprocessor(tile.referer)
            # 树里叶子 url 仍为 {tk} 占位：在 uri 构造边界读最新 map.keys 替换，
            # 让刚改过的 key 对下一次添加立即生效（config 模型里的 url 恒为占位符）
            cfg = self._load_map_config()
            keys = cfg.map.keys if cfg is not None else None
            layer = build_online_basemap_layer(tile, keys)
            if layer is None:
                self.messageBar.pushMessage(
                    f"在线底图加载失败：{tile.name}",
                    level=Qgis.MessageLevel.Critical,
                    duration=5,
                )
                return False
            # 同源图层已存在（xyz 连接串相同）→ 去重，只提示不重复加
            uri = layer.source()
            for lyr in self.project.mapLayers().values():
                if lyr.source() == uri:
                    self.messageBar.pushMessage(
                        f"该底图已在工程中：{tile.name}",
                        level=Qgis.MessageLevel.Info,
                        duration=2,
                    )
                    return True
            self.project.addMapLayer(layer, False)
            group = getattr(self, "_ref_group", None) or self.project.layerTreeRoot()
            group.insertLayer(0, layer)  # 参考分组顶部 = 默认底图之上、数据之下
            self.canvas.refreshAllLayers()
            self.messageBar.pushMessage(
                f"已添加在线底图：{tile.name}",
                level=Qgis.MessageLevel.Info,
                duration=2,
            )
            logger.info("添加在线底图：%s", tile.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("添加在线底图失败 %s：%s", getattr(tile, "name", tile), exc, exc_info=True)
            self.messageBar.pushMessage(
                f"添加在线底图失败：{exc}",
                level=Qgis.MessageLevel.Critical,
                duration=5,
            )
            return False
        finally:
            self._end_layer_loading()


class _DeleteGroupDialog(QDialog):
    """删除非空分组的选择 dialog：移到其他分组 / 一并删除条目 / 取消。

    不引导到外层 messageBar。Dock 调 :meth:`OnlineBasemapMixin._delete_online_basemap_group`
    调本类；用户取消或关窗 → ``exec()`` 返回 ``Rejected``，不落盘。

    UI：QRadioButton 互斥“移到其他分组”/“一并删除条目”；后者的目标分组下拉框
    (QComboBox) 装可用分组选项。返回 ``(choice, target)``：

    - ``choice == "reassign"`` + ``target != None`` → 走 reassign+remove 路径；
    - ``choice == "cascade"`` + ``target == None`` → 一并删除；
    - ``choice == "reassign"`` 且 ``target == None``（无其他分组可选）→
      禁止 Submit（只有 cascade 能选）；组合框 “无可选目标”。
    """

    def __init__(self, parent, name: str, other_groups: list[str], count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"删除分组「{name}」")
        from qgis.PyQt.QtWidgets import (
            QComboBox,
            QDialogButtonBox,
            QLabel,
            QRadioButton,
            QVBoxLayout,
        )
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"分组「{name}」下还有 {count} 条底图，请选择："
            )
        )
        self._reassign_radio = QRadioButton("移到其他分组：")
        self._cascade_radio = QRadioButton("连同底图一并删除")
        if other_groups:
            self._reassign_radio.setChecked(True)
        else:
            # 没其他分组可分配 → cascade 唯一选项
            self._reassign_radio.setEnabled(False)
            self._cascade_radio.setChecked(True)
        self._target_combo = QComboBox()
        self._target_combo.addItems(other_groups)
        if not other_groups:
            self._target_combo.setEnabled(False)
            self._target_combo.addItem("（无可选目标分组）")
        layout.addWidget(self._reassign_radio)
        layout.addWidget(self._target_combo)
        layout.addWidget(self._cascade_radio)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result(self) -> tuple[str, str | None]:
        """返回 ``(choice, target)``——``choice`` 是 ``"reassign"`` / ``"cascade"``。"""
        if self._cascade_radio.isChecked():
            return "cascade", None
        target = self._target_combo.currentText() if self._target_combo.isEnabled() else None
        return "reassign", target
