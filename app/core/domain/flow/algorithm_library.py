"""流程编排：节点模板库 + QGIS 处理算法桥接。

- :data:`BUILTIN_TEMPLATES`：内置节点模板（数据源 / 常用算法 / 空节点），
  纯数据，不依赖 QGIS，可离线单测。
- :func:`describe_processing_algorithm`：从 QGIS 处理工具箱注册表读取算法，
  自动生成节点模板（输入 / 输出 / 调用代码），供「选择处理算法」下拉使用。

模板代码运行在 :class:`app.core.domain.flow.executor.FlowExecutor` 提供的
命名空间里，可直接引用 ``inputs``（本节点输入字典）、``outputs``（输出字典，
请赋值）、``processing`` / ``context`` / ``feedback`` 及常用 QGIS 类。
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import PortType

#: 默认节点尺寸（画布上新建节点的初始位置由调用方决定）
DEFAULT_W = 200
DEFAULT_H = 140


@dataclass(frozen=True)
class TemplateInput:
    """模板输入参数定义。"""

    name: str
    type: PortType
    default: str = ""
    required: bool = True


@dataclass(frozen=True)
class TemplateOutput:
    """模板输出端口定义。"""

    name: str
    type: PortType
    write_temp: bool = True
    load_to_canvas: bool = True


@dataclass(frozen=True)
class NodeTemplate:
    """一个可实例化的节点模板。"""

    key: str
    name: str
    group: str
    description: str
    code: str
    inputs: tuple[TemplateInput, ...] = ()
    outputs: tuple[TemplateOutput, ...] = ()


# ------------------------------------------------------------ 内置模板

def _t(key: str, name: str, group: str, code: str, inputs=(), outputs=(), description: str = "") -> NodeTemplate:
    return NodeTemplate(
        key=key,
        name=name,
        group=group,
        description=description,
        code=code.strip(),
        inputs=tuple(inputs),
        outputs=tuple(outputs),
    )


_BUILTIN: list[NodeTemplate] = [
    _t(
        "load_vector",
        "加载矢量图层",
        "数据源",
        """# 加载矢量图层
layer = QgsVectorLayer(inputs["图层路径"], "加载矢量", "ogr")
if not layer.isValid():
    raise RuntimeError("无法加载矢量图层：" + str(inputs["图层路径"]))
outputs["图层"] = layer""",
        inputs=[TemplateInput("图层路径", PortType.FILE, required=True)],
        outputs=[TemplateOutput("图层", PortType.VECTOR, load_to_canvas=True)],
        description="从文件加载矢量图层（shp / gpkg / gdb 等）",
    ),
    _t(
        "load_raster",
        "加载栅格图层",
        "数据源",
        """# 加载栅格图层
layer = QgsRasterLayer(inputs["图层路径"], "加载栅格")
if not layer.isValid():
    raise RuntimeError("无法加载栅格图层：" + str(inputs["图层路径"]))
outputs["图层"] = layer""",
        inputs=[TemplateInput("图层路径", PortType.FILE, required=True)],
        outputs=[TemplateOutput("图层", PortType.RASTER, load_to_canvas=True)],
        description="从文件加载栅格图层（tif / img 等）",
    ),
    _t(
        "constant",
        "常量数值",
        "数据源",
        """# 常量数值：直接透传给输出
outputs["数值"] = inputs["值"]""",
        inputs=[TemplateInput("值", PortType.NUMBER, default="0", required=False)],
        outputs=[TemplateOutput("数值", PortType.NUMBER, load_to_canvas=False)],
        description="输出一个常量数值",
    ),
    _t(
        "buffer",
        "缓冲区",
        "矢量分析",
        """# 缓冲区（native:buffer）
params = {
    "INPUT": inputs["输入图层"],
    "DISTANCE": inputs["距离"],
    "SEGMENTS": inputs["线段数"],
    "OUTPUT": "memory:缓冲区结果",
}
outputs["缓冲区结果"] = processing.run(
    "native:buffer", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("距离", PortType.NUMBER, default="100", required=True),
            TemplateInput("线段数", PortType.NUMBER, default="8", required=False),
        ],
        outputs=[TemplateOutput("缓冲区结果", PortType.VECTOR, load_to_canvas=True)],
        description="为矢量图层按指定距离生成缓冲区面",
    ),
    _t(
        "clip",
        "裁剪",
        "矢量分析",
        """# 裁剪（native:clip）
params = {
    "INPUT": inputs["输入图层"],
    "OVERLAY": inputs["裁剪图层"],
    "OUTPUT": "memory:裁剪结果",
}
outputs["裁剪结果"] = processing.run(
    "native:clip", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("裁剪图层", PortType.VECTOR, required=True),
        ],
        outputs=[TemplateOutput("裁剪结果", PortType.VECTOR, load_to_canvas=True)],
        description="用裁剪图层裁剪输入矢量图层",
    ),
    _t(
        "reproject",
        "投影转换",
        "矢量分析",
        """# 投影转换（native:reprojectlayer）
params = {
    "INPUT": inputs["输入图层"],
    "TARGET_CRS": inputs["目标坐标系"],
    "OUTPUT": "memory:投影结果",
}
outputs["投影结果"] = processing.run(
    "native:reprojectlayer", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("目标坐标系", PortType.STRING, default="EPSG:4326", required=True),
        ],
        outputs=[TemplateOutput("投影结果", PortType.VECTOR, load_to_canvas=True)],
        description="把矢量图层重投影到目标坐标系",
    ),
    _t(
        "field_calc",
        "字段计算",
        "矢量分析",
        """# 字段计算（native:fieldcalculator，新增字段）
params = {
    "INPUT": inputs["输入图层"],
    "FIELD_NAME": inputs["新字段名"],
    "FIELD_TYPE": 2,
    "FIELD_LENGTH": 50,
    "FIELD_PRECISION": 0,
    "FORMULA": inputs["表达式"],
    "OUTPUT": "memory:字段计算结果",
}
outputs["字段计算结果"] = processing.run(
    "native:fieldcalculator", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("新字段名", PortType.STRING, required=True),
            TemplateInput("表达式", PortType.STRING, default="1", required=True),
        ],
        outputs=[TemplateOutput("字段计算结果", PortType.VECTOR, load_to_canvas=True)],
        description="按 QGIS 表达式为图层新增计算字段",
    ),
    _t(
        "filter_attrs",
        "属性过滤",
        "矢量分析",
        """# 属性过滤（native:extractbyexpression，按 QGIS 表达式过滤，类 SQL）
params = {
    "INPUT": inputs["输入图层"],
    "EXPRESSION": inputs["过滤条件"],
    "OUTPUT": "memory:属性过滤结果",
}
outputs["过滤结果"] = processing.run(
    "native:extractbyexpression", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("过滤条件", PortType.STRING, default='"字段" = \'值\'', required=True),
        ],
        outputs=[TemplateOutput("过滤结果", PortType.VECTOR, load_to_canvas=True)],
        description="按条件过滤矢量图层属性，条件用 QGIS 表达式（类 SQL，如「字段」=『值』）",
    ),
    _t(
        "overlay_intersect",
        "相交",
        "矢量分析",
        """# 相交（native:intersection）：保留两图层几何重叠区，属性合并
params = {
    "INPUT": inputs["输入图层"],
    "OVERLAY": inputs["叠加图层"],
    "OUTPUT": "memory:相交结果",
}
outputs["相交结果"] = processing.run(
    "native:intersection", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("叠加图层", PortType.VECTOR, required=True),
        ],
        outputs=[TemplateOutput("相交结果", PortType.VECTOR, load_to_canvas=True)],
        description="保留输入与叠加图层几何相交部分，属性合并",
    ),
    _t(
        "overlay_union",
        "并集",
        "矢量分析",
        """# 并集（native:union）：两图层全部区域合并，属性合并
params = {
    "INPUT": inputs["输入图层"],
    "OVERLAY": inputs["叠加图层"],
    "OUTPUT": "memory:并集结果",
}
outputs["并集结果"] = processing.run(
    "native:union", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("叠加图层", PortType.VECTOR, required=True),
        ],
        outputs=[TemplateOutput("并集结果", PortType.VECTOR, load_to_canvas=True)],
        description="合并输入与叠加图层的全部区域（重叠部分不重复）",
    ),
    _t(
        "overlay_difference",
        "差集",
        "矢量分析",
        """# 差集（native:difference）：从输入图层擦除与叠加图层重叠的区域
params = {
    "INPUT": inputs["输入图层"],
    "OVERLAY": inputs["擦除图层"],
    "OUTPUT": "memory:差集结果",
}
outputs["差集结果"] = processing.run(
    "native:difference", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("擦除图层", PortType.VECTOR, required=True),
        ],
        outputs=[TemplateOutput("差集结果", PortType.VECTOR, load_to_canvas=True)],
        description="从输入图层中擦除与叠加图层重叠的区域",
    ),
    _t(
        "overlay_symdiff",
        "对称差",
        "矢量分析",
        """# 对称差（native:symmetricaldifference）：保留两图层互不重叠的部分
params = {
    "INPUT": inputs["输入图层"],
    "OVERLAY": inputs["叠加图层"],
    "OUTPUT": "memory:对称差结果",
}
outputs["对称差结果"] = processing.run(
    "native:symmetricaldifference", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[
            TemplateInput("输入图层", PortType.VECTOR, required=True),
            TemplateInput("叠加图层", PortType.VECTOR, required=True),
        ],
        outputs=[TemplateOutput("对称差结果", PortType.VECTOR, load_to_canvas=True)],
        description="保留输入与叠加图层互不重叠的部分（去掉共同区）",
    ),
    _t(
        "reclassify",
        "重分类",
        "栅格分析",
        """# 重分类（native:reclassifybytable，栅格）
params = {
    "INPUT_RASTER": inputs["输入栅格"],
    "RASTER_BAND": 1,
    "TABLE": [[0, 100, 1], [100, 200, 2]],
    "NO_DATA": 0,
    "RANGE_BOUNDARIES": 0,
    "NODATA_FOR_MISSING": True,
    "DATA_TYPE": 5,
    "OUTPUT": "memory:重分类结果",
}
outputs["重分类结果"] = processing.run(
    "native:reclassifybytable", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[TemplateInput("输入栅格", PortType.RASTER, required=True)],
        outputs=[TemplateOutput("重分类结果", PortType.RASTER, load_to_canvas=True)],
        description="按重分类表对栅格值重分类",
    ),
    _t(
        "raster_merge",
        "栅格镶嵌",
        "栅格分析",
        """# 栅格镶嵌（gdal:merge，单输入示例；多输入可把列表填进 inputs["输入栅格"]）
params = {
    "INPUT": [inputs["输入栅格"]],
    "PCT": False,
    "SEPARATE": False,
    "NODATA_INPUT": None,
    "NODATA_OUTPUT": None,
    "OPTIONS": "",
    "EXTRA": "",
    "DATA_TYPE": 5,
    "OUTPUT": "memory:镶嵌结果",
}
outputs["镶嵌结果"] = processing.run(
    "gdal:merge", params, context=context, feedback=feedback
)["OUTPUT"]""",
        inputs=[TemplateInput("输入栅格", PortType.RASTER, required=True)],
        outputs=[TemplateOutput("镶嵌结果", PortType.RASTER, load_to_canvas=True)],
        description="把多个栅格镶嵌为一个",
    ),
    _t(
        "empty",
        "空节点（自定义代码）",
        "通用",
        """# 自定义节点：请编写 PyQGIS 执行代码
# 可用变量：
#   inputs   —— 本节点输入参数字典（按键名取值）
#   outputs  —— 输出字典，请把结果赋值进去
#   processing / context / feedback —— 处理工具箱运行环境
#   QgsVectorLayer / QgsRasterLayer / QgsProject
#   QgsVectorFileWriter / QgsRasterFileWriter
outputs["输出"] = inputs""",
        outputs=[TemplateOutput("输出", PortType.STRING, load_to_canvas=False)],
        description="从零开始的空白节点",
    ),
]

#: 全部内置模板，按 key 索引
BUILTIN_TEMPLATES: dict[str, NodeTemplate] = {t.key: t for t in _BUILTIN}

#: 节点库分组顺序（左侧面板树展示用）
NODE_GROUPS: list[str] = ["数据源", "矢量分析", "栅格分析", "通用"]


def template_by_key(key: str) -> NodeTemplate | None:
    """按 key 取模板（含处理算法动态模板由调用方缓存）。"""
    return BUILTIN_TEMPLATES.get(key)


def instantiate_template(
    graph, template: NodeTemplate, x: float = 0.0, y: float = 0.0
) -> object:
    """把模板实例化成一个节点：建节点 + 按模板建输入/输出端口 + 填入代码。

    供画布（拖模板建节点）与测试共用。
    """
    node = graph.add_node(name=template.name, x=x, y=y, code=template.code, algorithm_id=template.key)
    for ti in template.inputs:
        graph.add_input(node.id, ti.name, ti.type, default=ti.default)
    for to in template.outputs:
        out = graph.add_output(node.id, to.name, to.type)
        out.write_temp = to.write_temp
        out.load_to_canvas = to.load_to_canvas
    return node


# ------------------------------------------------------------ 处理算法桥接


def list_processing_algorithms(filter_text: str = "") -> list[tuple[str, str]]:
    """列出 QGIS 处理工具箱算法，返回 ``[(algo_id, 显示名)]``，按显示名排序。

    需要 QGIS 环境（懒加载）。
    """
    from qgis.core import QgsApplication  # type: ignore

    text = filter_text.strip().lower()
    out: list[tuple[str, str]] = []
    for alg in QgsApplication.processingRegistry().algorithms():
        name = alg.displayName()
        if text and text not in name.lower() and text not in alg.id().lower():
            continue
        out.append((alg.id(), name))
    out.sort(key=lambda item: item[1])
    return out


def describe_processing_algorithm(algo_id: str) -> NodeTemplate | None:
    """从处理工具箱注册表生成算法节点模板。

    把算法参数定义映射为节点输入、输出定义映射为节点输出端口，并生成一段
    调用 ``processing.run`` 的模板代码。任一环节出错（算法不存在 / 映射失败）
    返回 None。
    """
    from qgis.core import (  # type: ignore
        QgsApplication,
        QgsProcessingParameterDefinition,
        QgsProcessingParameterDestination,
    )

    try:
        alg = QgsApplication.processingRegistry().createAlgorithmById(algo_id)
    except Exception:  # noqa: BLE001
        return None
    if alg is None:
        return None

    try:
        inputs: list[TemplateInput] = []
        dest_names: list[str] = []
        for p in alg.parameterDefinitions():
            if isinstance(p, QgsProcessingParameterDestination):
                dest_names.append(p.name())
                continue
            flags = p.flags()
            if flags & QgsProcessingParameterDefinition.Flag.FlagHidden:
                continue
            ptype = _param_to_port_type(p)
            inputs.append(
                TemplateInput(
                    name=p.name(),
                    type=ptype,
                    default=str(p.defaultValue() or ""),
                    required=not bool(flags & QgsProcessingParameterDefinition.Flag.FlagOptional),
                )
            )
        outputs = [
            TemplateOutput(name=o.name(), type=_output_to_port_type(o))
            for o in alg.outputDefinitions()
        ]
        code = _generate_algorithm_code(algo_id, alg, inputs, outputs, dest_names)
        description = getattr(alg, "shortDescription", lambda: "")() or getattr(alg, "longName", lambda: "")()
        return NodeTemplate(
            key=algo_id,
            name=alg.displayName(),
            group="处理算法",
            description=description,
            code=code,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
        )
    except Exception:  # noqa: BLE001
        return None


def _param_to_port_type(param) -> PortType:
    """把 QgsProcessingParameterDefinition 映射为端口类型。"""
    from qgis.core import (  # type: ignore
        QgsProcessingParameterBoolean,
        QgsProcessingParameterCrs,
        QgsProcessingParameterExtent,
        QgsProcessingParameterField,
        QgsProcessingParameterFile,
        QgsProcessingParameterMultipleLayers,
        QgsProcessingParameterNumber,
        QgsProcessingParameterRasterLayer,
        QgsProcessingParameterString,
        QgsProcessingParameterVectorLayer,
    )

    if isinstance(param, QgsProcessingParameterVectorLayer):
        return PortType.VECTOR
    if isinstance(param, QgsProcessingParameterRasterLayer):
        return PortType.RASTER
    if isinstance(param, QgsProcessingParameterNumber):
        return PortType.NUMBER
    if isinstance(param, QgsProcessingParameterBoolean):
        return PortType.BOOLEAN
    if isinstance(param, QgsProcessingParameterField):
        try:
            from qgis.core import QgsProcessingParameterField  # type: ignore

            multiple = param.dataType() == QgsProcessingParameterField.DataType.FieldMultiple
        except Exception:  # noqa: BLE001
            multiple = False
        return PortType.FIELDS if multiple else PortType.STRING
    if isinstance(param, QgsProcessingParameterFile):
        try:
            from qgis.core import QgsProcessingParameterFile  # type: ignore

            is_folder = param.behavior() == QgsProcessingParameterFile.Behavior.Folder
        except Exception:  # noqa: BLE001
            is_folder = False
        return PortType.FOLDER if is_folder else PortType.FILE
    if isinstance(param, (QgsProcessingParameterCrs, QgsProcessingParameterExtent)):
        return PortType.STRING
    if isinstance(param, QgsProcessingParameterString):
        return PortType.STRING
    if isinstance(param, QgsProcessingParameterMultipleLayers):
        return PortType.FILE  # 多图层参数无法用单端口精确表达，按文件路径处理
    return PortType.STRING


def _output_to_port_type(output) -> PortType:
    """把 QgsProcessingOutputDefinition 映射为端口类型。"""
    from qgis.core import (  # type: ignore
        QgsProcessingOutputFile,
        QgsProcessingOutputFolder,
        QgsProcessingOutputNumber,
        QgsProcessingOutputRasterLayer,
        QgsProcessingOutputString,
        QgsProcessingOutputVectorLayer,
    )

    if isinstance(output, QgsProcessingOutputVectorLayer):
        return PortType.VECTOR
    if isinstance(output, QgsProcessingOutputRasterLayer):
        return PortType.RASTER
    if isinstance(output, QgsProcessingOutputNumber):
        return PortType.NUMBER
    if isinstance(output, QgsProcessingOutputString):
        return PortType.STRING
    if isinstance(output, QgsProcessingOutputFile):
        return PortType.FILE
    if isinstance(output, QgsProcessingOutputFolder):
        return PortType.FOLDER
    return PortType.STRING


def _generate_algorithm_code(
    algo_id: str,
    alg,
    inputs: list[TemplateInput],
    outputs: list[TemplateOutput],
    dest_names: list[str],
) -> str:
    """生成调用处理算法的节点代码。"""
    lines = [
        f"# 调用处理算法：{alg.displayName()}（{algo_id}）",
        "params = {",
    ]
    for t in inputs:
        lines.append(f"    {t.name!r}: inputs[{t.name!r}],")
    for idx, name in enumerate(dest_names):
        lines.append(f"    {name!r}: 'memory:结果{idx + 1}',")
    lines.append("}")
    for name in dest_names:
        lines.append(
            f"outputs[{name!r}] = processing.run({algo_id!r}, params, "
            f"context=context, feedback=feedback)[{name!r}]"
        )
    return "\n".join(lines)
