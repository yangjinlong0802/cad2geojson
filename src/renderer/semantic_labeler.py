# -*- coding: utf-8 -*-
"""
语义标签注入模块

根据 CAD 图层名称的命名惯例，推断图层的建筑/工程语义，
并生成人类可读的语义标签，供 LLM Prompt 中使用。

支持的语义类型（可扩展）：
    wall        墙体
    door        门
    window      窗
    column      柱
    dimension   尺寸标注
    axis        轴线/定位线
    text        文字注释
    furniture   家具/室内设施
    stair       楼梯
    boundary    边界/用地红线
    road        道路
    water       水体
    vegetation  绿化/植被
    equipment   设备/机电
    structure   结构构件
    unknown     未能识别
"""

import re
from typing import List

# ── 关键词映射表 ──────────────────────────────────────────────────────────────
# 优先级：列表中的 (模式, 语义) 按顺序匹配，先匹配先得
# 模式支持正则（大小写不敏感）
_KEYWORD_RULES = [
    # ── 墙体 ──
    (r"wall|qiang|墙|WALL", "wall"),
    # ── 门 ──
    (r"door|men|门|DOOR", "door"),
    # ── 窗 ──
    (r"window|chuang|窗|WIN", "window"),
    # ── 柱 ──
    (r"column|col|pillar|柱|ZHU", "column"),
    # ── 轴线 ──
    (r"axis|axe|zhouline|轴线|轴|AXIS|ZX", "axis"),
    # ── 尺寸标注 ──
    (r"dim|标注|尺寸|DIM", "dimension"),
    # ── 文字/注释 ──
    (r"text|anno|note|label|文字|注释|TXT", "text"),
    # ── 家具 ──
    (r"furn|furniture|家具|内装|FURN", "furniture"),
    # ── 楼梯 ──
    (r"stair|梯|STAIR", "stair"),
    # ── 边界/红线 ──
    (r"boundary|border|outline|红线|边界|BOUN", "boundary"),
    # ── 道路 ──
    (r"road|street|路|道路|ROAD", "road"),
    # ── 水体 ──
    (r"water|lake|river|水|WATER", "water"),
    # ── 绿化 ──
    (r"green|plant|tree|veg|绿|植|TREE|GRND", "vegetation"),
    # ── 设备 ──
    (r"equip|mech|elec|pipe|设备|机电|管|EQUIP|MEP", "equipment"),
    # ── 结构 ──
    (r"struct|beam|slab|梁|板|结构|STR", "structure"),
]

# ── 语义标签的中文描述（供 Prompt 中使用）────────────────────────────────────
SEMANTIC_DESC = {
    "wall":       "墙体/隔墙",
    "door":       "门洞/门",
    "window":     "窗户/窗洞",
    "column":     "结构柱",
    "axis":       "建筑轴线",
    "dimension":  "尺寸标注线",
    "text":       "文字注释",
    "furniture":  "家具/室内设施",
    "stair":      "楼梯",
    "boundary":   "用地边界/红线",
    "road":       "道路",
    "water":      "水体",
    "vegetation": "绿化/植被",
    "equipment":  "机电设备",
    "structure":  "结构构件",
    "unknown":    "未知图层",
}

# ── 语义标签的 SVG 默认样式建议（颜色 + 线宽）────────────────────────────────
SEMANTIC_STYLE_HINT = {
    "wall":       {"stroke": "#333333", "stroke-width": 2, "fill": "#e0e0e0"},
    "door":       {"stroke": "#8b4513", "stroke-width": 1.5, "fill": "none"},
    "window":     {"stroke": "#4fc3f7", "stroke-width": 1, "fill": "#e3f2fd"},
    "column":     {"stroke": "#555555", "stroke-width": 2, "fill": "#9e9e9e"},
    "axis":       {"stroke": "#ff7043", "stroke-width": 0.5, "fill": "none", "stroke-dasharray": "8,4"},
    "dimension":  {"stroke": "#0288d1", "stroke-width": 0.5, "fill": "none"},
    "text":       {"stroke": "none", "fill": "#212121", "font-size": 10},
    "furniture":  {"stroke": "#795548", "stroke-width": 0.8, "fill": "#efebe9"},
    "stair":      {"stroke": "#607d8b", "stroke-width": 1, "fill": "none"},
    "boundary":   {"stroke": "#d32f2f", "stroke-width": 2, "fill": "none", "stroke-dasharray": "12,4"},
    "road":       {"stroke": "#ffc107", "stroke-width": 3, "fill": "#fff9c4"},
    "water":      {"stroke": "#0277bd", "stroke-width": 1, "fill": "#b3e5fc"},
    "vegetation": {"stroke": "#388e3c", "stroke-width": 1, "fill": "#c8e6c9"},
    "equipment":  {"stroke": "#6a1b9a", "stroke-width": 0.8, "fill": "none"},
    "structure":  {"stroke": "#37474f", "stroke-width": 2.5, "fill": "#cfd8dc"},
    "unknown":    {"stroke": "#bdbdbd", "stroke-width": 1, "fill": "none"},
}


def label_layer(layer_name: str, geometry_types: List[str] = None) -> str:
    """
    根据图层名称推断语义标签。

    参数:
        layer_name:     CAD 图层名称（如 "WALL", "门窗"，"A-DIM"）
        geometry_types: 该图层包含的几何类型列表（辅助判断），如 ["Polygon", "LineString"]

    返回:
        语义标签字符串（如 "wall", "dimension", "unknown"）
    """
    name_upper = layer_name.upper().strip()

    # 遍历规则表，正则匹配图层名
    for pattern, tag in _KEYWORD_RULES:
        if re.search(pattern, name_upper, re.IGNORECASE):
            return tag

    # ── 几何类型辅助推断 ──────────────────────────────────────────────
    # 若关键词匹配失败，利用几何类型做粗略判断
    if geometry_types:
        types_set = set(geometry_types)
        if "Point" in types_set and len(types_set) == 1:
            # 只有点的图层很可能是文字注释图层
            return "text"
        if "Polygon" in types_set and "LineString" not in types_set:
            # 只有面的图层可能是填充/房间
            return "structure"

    return "unknown"


def get_style_hint(semantic_tag: str) -> dict:
    """
    根据语义标签返回 SVG 样式建议字典。

    参数:
        semantic_tag: 语义标签字符串

    返回:
        包含 stroke / fill / stroke-width 等的字典
    """
    return SEMANTIC_STYLE_HINT.get(semantic_tag, SEMANTIC_STYLE_HINT["unknown"]).copy()


def get_description(semantic_tag: str) -> str:
    """返回语义标签的中文描述，用于 Prompt 文本拼接。"""
    return SEMANTIC_DESC.get(semantic_tag, "未知图层")
