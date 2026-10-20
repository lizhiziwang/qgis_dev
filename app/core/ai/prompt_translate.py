"""中文提示词 → 英文提示词（SAM3 文本编码器只支持英文）。

SAM3 的文本编码器使用 CLIP BPE 词表，中文会被拆成字节级 token，实测中文提示
返回 0 个掩膜。这里用一个**内置地物术语表**做离线转换：

- 最长匹配：``高层建筑`` 优先于 ``建筑``；
- 忽略“请 / 帮我 / 分割 / 图中的 / 所有 / 的”等指令性词语；
- 保留英文单词，支持中英混写；
- 未收录的中文片段会放进 ``unknown``，调用方应提示用户改用英文，而不是把
  无法识别的文字硬塞给模型。

术语表按常见遥感 / 地物类别组织，直接往 ``ZH_EN_TERMS`` 里加条目即可扩展。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CJK = re.compile(r"[㐀-䶿一-鿿]+")

#: 中文术语 → 英文提示。键越具体越好；匹配时按长度倒序。
ZH_EN_TERMS: dict[str, str] = {
    # ---- 建筑与设施
    "高层建筑": "high-rise building",
    "施工工地": "construction site",
    "建筑工地": "construction site",
    "施工围挡": "construction fence",
    "光伏电站": "solar farm",
    "太阳能电池板": "solar panel",
    "太阳能板": "solar panel",
    "光伏板": "solar panel",
    "风力发电机": "wind turbine",
    "风力发电": "wind farm",
    "输电塔": "transmission tower",
    "通信塔": "communication tower",
    "电视塔": "TV tower",
    "冷却塔": "cooling tower",
    "购物中心": "shopping mall",
    "体育场": "stadium",
    "游泳池": "swimming pool",
    "建筑物": "building",
    "建筑": "building",
    "房屋": "building",
    "房子": "building",
    "大楼": "tall building",
    "楼房": "tall building",
    "住宅": "residential building",
    "居民楼": "residential building",
    "厂房": "factory building",
    "工厂": "factory",
    "仓库": "warehouse",
    "学校": "school",
    "医院": "hospital",
    "商场": "shopping mall",
    "球场": "sports field",
    "屋顶": "roof",
    "围墙": "wall",
    "墙": "wall",
    "栅栏": "fence",
    "围栏": "fence",
    "大门": "gate",
    "塔": "tower",
    "烟囱": "chimney",
    "桥梁": "bridge",
    "桥": "bridge",
    "立交桥": "overpass",
    "隧道": "tunnel",
    "大坝": "dam",
    "水坝": "dam",
    "水库": "reservoir",
    "港口": "port",
    "码头": "dock",
    "机场": "airport",
    "跑道": "runway",
    "停机坪": "apron",
    "停车场": "parking lot",
    "广场": "plaza",
    "工地": "construction site",
    "堆场": "storage yard",
    "塑料大棚": "greenhouse",
    "大棚": "greenhouse",
    "温室": "greenhouse",
    "风机": "wind turbine",
    "风车": "wind turbine",
    "储罐": "storage tank",
    "油罐": "oil tank",
    "集装箱": "container",
    "广告牌": "billboard",
    "塔吊": "crane",
    "起重机": "crane",
    "挖掘机": "excavator",
    "推土机": "bulldozer",
    "脚手架": "scaffolding",
    "围挡": "construction fence",
    # ---- 道路与交通
    "高速公路": "highway",
    "人行道": "sidewalk",
    "斑马线": "crosswalk",
    "收费站": "toll station",
    "服务区": "service area",
    "道路": "road",
    "公路": "road",
    "马路": "road",
    "路面": "road",
    "街道": "street",
    "街": "street",
    "小路": "path",
    "小径": "path",
    "铁路": "railway",
    "铁道": "railway",
    "地铁": "subway",
    # ---- 植被与土地覆盖
    "植被": "vegetation",
    "森林": "forest",
    "林地": "forest",
    "树林": "forest",
    "树木": "tree",
    "树": "tree",
    "灌木": "shrub",
    "草地": "grassland",
    "草坪": "lawn",
    "农田": "cropland",
    "耕地": "cropland",
    "水田": "paddy field",
    "梯田": "terraced field",
    "果园": "orchard",
    "茶园": "tea plantation",
    "苗圃": "nursery",
    "裸地": "bare land",
    "裸土": "bare soil",
    "沙地": "sand",
    "沙滩": "beach",
    "沙丘": "dune",
    "岩石": "rock",
    "石头": "rock",
    "山地": "mountain",
    "山": "mountain",
    "丘陵": "hill",
    "悬崖": "cliff",
    "火山": "volcano",
    "积雪": "snow",
    "雪": "snow",
    "冰川": "glacier",
    "冰": "ice",
    "湿地": "wetland",
    "沼泽": "swamp",
    "水体": "water",
    "水域": "water",
    "水面": "water",
    "河流": "river",
    "河": "river",
    "小溪": "stream",
    "溪流": "stream",
    "湖泊": "lake",
    "湖": "lake",
    "池塘": "pond",
    "水塘": "pond",
    "海洋": "ocean",
    "海": "ocean",
    "海滩": "beach",
    "云层": "cloud",
    "云": "cloud",
    "雾": "fog",
    "阴影": "shadow",
    "烟雾": "smoke",
    "烟": "smoke",
    "火焰": "flame",
    "火": "fire",
    # ---- 车辆与船舶
    "车辆": "vehicle",
    "车": "vehicle",
    "汽车": "car",
    "小汽车": "car",
    "轿车": "car",
    "卡车": "truck",
    "货车": "truck",
    "公交车": "bus",
    "巴士": "bus",
    "客车": "bus",
    "火车": "train",
    "列车": "train",
    "摩托车": "motorcycle",
    "自行车": "bicycle",
    "飞机": "airplane",
    "直升机": "helicopter",
    "无人机": "drone",
    "船舶": "ship",
    "轮船": "ship",
    "货船": "cargo ship",
    "渔船": "fishing boat",
    "快艇": "boat",
    "小艇": "boat",
    "船": "ship",
    # ---- 人 / 动物
    "行人": "pedestrian",
    "人群": "crowd",
    "人": "person",
    "动物": "animal",
    "牛": "cow",
    "羊": "sheep",
    "马": "horse",
    "猪": "pig",
    "狗": "dog",
    "猫": "cat",
    "鸟": "bird",
    "鱼": "fish",
    # ---- 颜色
    "红色": "red",
    "红": "red",
    "蓝色": "blue",
    "蓝": "blue",
    "绿色": "green",
    "绿": "green",
    "黄色": "yellow",
    "黄": "yellow",
    "白色": "white",
    "白": "white",
    "黑色": "black",
    "黑": "black",
    "灰色": "gray",
    "灰": "gray",
    "棕色": "brown",
    "棕": "brown",
    "橙色": "orange",
    "橙": "orange",
    "紫色": "purple",
    "紫": "purple",
    # ---- 形状
    "正方形": "square",
    "长方形": "rectangle",
    "矩形": "rectangle",
    "方形": "square",
    "方块": "square",
    "圆形": "circle",
    "圆": "circle",
    "三角形": "triangle",
    "条状": "strip",
    "长条": "strip",
}

#: 指令性词语：不参与转换，也不视为未识别。按长度倒序匹配。
_FILLERS: tuple[str, ...] = tuple(
    sorted(
        (
            "分割出",
            "标注出",
            "提取出",
            "识别出",
            "每一个",
            "影像中",
            "影像里",
            "影像上",
            "图片中",
            "画面中",
            "图中",
            "图上",
            "帮我",
            "帮忙",
            "给我",
            "所有",
            "全部",
            "每个",
            "那些",
            "这些",
            "一些",
            "分割",
            "标注",
            "提取",
            "识别",
            "找出",
            "找到",
            "圈出",
            "勾出",
            "选出",
            "标记",
            "地物",
            "目标",
            "区域",
            "部分",
            "请",
            "把",
            "将",
            "出",
            "中",
            "的",
        ),
        key=len,
        reverse=True,
    )
)

#: 连接词（长的先替换）。
_CONNECTORS: tuple[tuple[str, str], ...] = (
    ("以及", "and"),
    ("或者", "or"),
    ("和", "and"),
    ("与", "and"),
    ("及", "and"),
    ("或", "or"),
)

#: 中文标点 → 英文标点。
_PUNCTUATION: tuple[tuple[str, str], ...] = (
    ("，", ", "),
    ("、", ", "),
    ("；", ", "),
    ("：", ": "),
    ("。", ". "),
    ("（", "("),
    ("）", ")"),
    ("　", " "),
)

_TERM_PATTERN = re.compile(
    "|".join(re.escape(term) for term in sorted(ZH_EN_TERMS, key=len, reverse=True))
)
_FILLER_PATTERN = re.compile("|".join(re.escape(term) for term in _FILLERS))


@dataclass(frozen=True)
class PromptTranslation:
    """翻译结果：``english`` 是实际应发送给 SAM3 的提示。"""

    original: str
    english: str
    unknown: tuple[str, ...]

    @property
    def has_chinese(self) -> bool:
        return bool(_CJK.search(self.original))

    @property
    def translated(self) -> bool:
        return self.has_chinese and bool(self.english)

    @property
    def usable(self) -> bool:
        """没有未识别中文且存在有效英文时才适合发送。"""
        return bool(self.english) and not self.unknown


def _normalize_english(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # 未识别中文被删掉后可能留下孤立的连接词，如 “量子计算机和道路” → “and road”
    text = re.sub(r"^(?:and|or)\s+", "", text)
    text = re.sub(r"\s+(?:and|or)$", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])(?:\s*[,.;:!?])+", r"\1", text)
    return text.strip(" ,.;:!?").lower()


def translate_prompt(text: str) -> PromptTranslation:
    """把中文地物描述转换为英文提示；未收录的中文片段放入 ``unknown``。"""
    original = (text or "").strip()
    if not original:
        return PromptTranslation(original="", english="", unknown=())

    working = _TERM_PATTERN.sub(lambda match: f" {ZH_EN_TERMS[match.group(0)]} ", original)
    for chinese, english in _CONNECTORS:
        working = working.replace(chinese, f" {english} ")
    working = _FILLER_PATTERN.sub(" ", working)
    for chinese, english in _PUNCTUATION:
        working = working.replace(chinese, english)

    unknown = tuple(dict.fromkeys(_CJK.findall(working)))
    english = _normalize_english(_CJK.sub(" ", working))
    if not re.search(r"[a-z]", english):
        english = ""
    return PromptTranslation(original=original, english=english, unknown=unknown)


__all__ = ["ZH_EN_TERMS", "PromptTranslation", "translate_prompt"]
