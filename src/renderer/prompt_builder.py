# -*- coding: utf-8 -*-
"""
三段式 Prompt 构建模块

将预处理后的 ProcessedData 组装为三段式 LLM 输入：
    1. System Prompt  —— 角色定义 + SVG 规则 + 约束
    2. Data Prompt    —— 几何数据 + 语义标签 + 样式建议
    3. Output Prompt  —— 输出格式要求（纯 SVG，无 markdown）

不同策略下 Data Prompt 的密度不同：
    策略 A: 完整 ProcessedData JSON（含所有 Feature）
    策略 B: 精简版：只传图层摘要 + 关键 Feature（省略密集点列）
    策略 C/D: 单块 ProcessedData JSON（由 chunker / rag_indexer 提供）
"""

import json
from typing import Any, Dict, List

from .semantic_labeler import get_description, get_style_hint
from .size_assessor import RenderStrategy


# ═══════════════════════════════════════════════════════════════════════════════
#  System Prompt 模板
# ═══════════════════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = """\
你是一名专业的 CAD 图形渲染专家，精通 SVG 矢量图形规范。
你的任务是：根据我提供的 GeoJSON 几何数据，生成一段语义清晰、视觉美观的 SVG 代码，
准确还原原始 CAD 图纸的布局和形状。

## SVG 生成规则
1. 严格使用标准 SVG 1.1 语法，输出必须可直接在浏览器中渲染。
2. 使用 `<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">` 作为根元素。
3. 每个图层对应一个 `<g id="layer-LAYERNAME" data-semantic="SEMANTIC">` 分组。
4. 几何类型映射规则：
   - Point        → `<circle>` 或 `<text>`（文字实体）
   - LineString   → `<polyline>` 或 `<path>`
   - Polygon      → `<polygon>` 或 `<path>`（含 fill）
   - MultiPolygon → 多个 `<path>` 用同一 `<g>` 包裹
5. 坐标系：输入坐标已归一化到 SVG 视口，直接使用，无需换算。
6. 线条颜色、线宽、填充色按图层语义样式建议设置（见 Data Prompt）。
7. 文字实体（entity_type = TEXT/MTEXT）渲染为 `<text>` 标签，保留 properties.text。
8. 尺寸标注图层（semantic = dimension）使用蓝色细线 + 箭头标记。
9. 轴线图层（semantic = axis）使用橙红色虚线。
10. 禁止生成 JavaScript 或外部资源引用，只允许内联 SVG。

## 输出约束
- 只输出 SVG 代码，不要任何 markdown 代码块标记，不要解释文字。
- 输出以 `<svg` 开头，以 `</svg>` 结尾。
- 图层按语义重要性排序（结构/墙体最先，文字/标注最后）。
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  公共接口
# ═══════════════════════════════════════════════════════════════════════════════

def build_messages(
    processed_data: Dict[str, Any],
    strategy: RenderStrategy,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> List[Dict[str, str]]:
    """
    构建完整的 messages 列表（OpenAI / Anthropic 格式均适用）。

    参数:
        processed_data: 预处理层输出的 ProcessedData（或分块后的子块）
        strategy:       当前使用的渲染策略
        chunk_index:    当前块序号（策略 C/D 分块时使用，从 0 开始）
        total_chunks:   总块数（策略 C/D）

    返回:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    vp = processed_data.get("viewport", {})
    width = vp.get("width", 800)
    height = vp.get("height", 600)

    system_content = _SYSTEM_PROMPT.format(
        width=width,
        height=height,
    )

    # 构建用户消息
    data_section = _build_data_section(processed_data, strategy)
    output_section = _build_output_section(width, height, chunk_index, total_chunks)

    user_content = data_section + "\n\n" + output_section

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def build_merge_prompt(
    svg_parts: List[str],
    viewport: Dict[str, Any],
) -> List[Dict[str, str]]:
    """
    策略 C 合并阶段：将多个 SVG 片段合并为完整 SVG 的 Prompt。

    参数:
        svg_parts: 各分块生成的 SVG 片段列表
        viewport:  视口信息

    返回:
        messages 列表
    """
    width = viewport.get("width", 800)
    height = viewport.get("height", 600)

    parts_text = "\n\n".join(
        f"<!-- 片段 {i + 1} -->\n{part}" for i, part in enumerate(svg_parts)
    )

    user_content = f"""\
以下是按图层分块生成的 {len(svg_parts)} 个 SVG 片段，每个片段是一个完整的 <svg> 块。
请将它们合并为一个统一的 SVG 文件，规则如下：
1. 根元素使用 <svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">
2. 提取每个片段中所有 <g> 分组，按图层语义重要性重新排序后放入根元素
3. 去除重复的 <defs> 定义，合并为一个 <defs> 块
4. 只输出合并后的完整 SVG 代码

SVG 片段如下：
{parts_text}
"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT.format(width=width, height=height)},
        {"role": "user", "content": user_content},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  内部构建函数
# ═══════════════════════════════════════════════════════════════════════════════

def _build_data_section(
    processed_data: Dict[str, Any],
    strategy: RenderStrategy,
) -> str:
    """根据策略构建 Data Prompt 部分。"""
    layers = processed_data.get("layers", {})
    vp = processed_data.get("viewport", {})

    # ── 图层元信息摘要（所有策略都有）────────────────────────────────
    layer_meta_lines = ["## 图层信息摘要"]
    for layer_name, layer_data in layers.items():
        semantic = layer_data.get("semantic", "unknown")
        desc = get_description(semantic)
        style_hint = get_style_hint(semantic)
        style_str = "; ".join(f"{k}:{v}" for k, v in style_hint.items())
        layer_meta_lines.append(
            f"- 图层 `{layer_name}` → 语义: {desc}"
            f"  | 实体数: {layer_data.get('feature_count', 0)}"
            f"  | 几何类型: {', '.join(layer_data.get('geometry_types', []))}"
            f"  | 样式建议: {style_str}"
        )
    layer_meta = "\n".join(layer_meta_lines)

    # ── 视口信息 ──────────────────────────────────────────────────────
    vp_info = (
        f"## 视口\n"
        f"width={vp.get('width')}, height={vp.get('height')}, "
        f"原始坐标范围={vp.get('bbox')}"
    )

    # ── 几何数据（策略不同，详略不同）────────────────────────────────
    if strategy == RenderStrategy.A:
        # 完整数据
        geo_json_str = json.dumps(
            {"layers": layers},
            ensure_ascii=False,
            separators=(",", ":"),   # 紧凑格式
        )
        geo_section = f"## 完整几何数据（JSON）\n```json\n{geo_json_str}\n```"

    elif strategy == RenderStrategy.B:
        # 精简：只保留多边形外环 + 折线点列（截断超长的）
        slim_layers = _slim_layers(layers, max_points_per_geom=50)
        geo_json_str = json.dumps(
            {"layers": slim_layers},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        geo_section = f"## 几何数据（精简版）\n```json\n{geo_json_str}\n```"

    else:
        # 策略 C/D：单块数据，也用完整格式（因为已经分块控制体积）
        geo_json_str = json.dumps(
            {"layers": layers},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        geo_section = f"## 几何数据（分块）\n```json\n{geo_json_str}\n```"

    return f"{vp_info}\n\n{layer_meta}\n\n{geo_section}"


def _build_output_section(
    width: int,
    height: int,
    chunk_index: int,
    total_chunks: int,
) -> str:
    """构建 Output Prompt 部分。"""
    if total_chunks > 1:
        chunk_note = (
            f"注意：这是第 {chunk_index + 1}/{total_chunks} 块分块请求。\n"
            f"请只为本块中的图层生成 SVG `<g>` 分组，"
            f"但仍需使用完整的 `<svg>` 根元素包裹（便于后续合并）。\n"
        )
    else:
        chunk_note = ""

    return (
        f"## 输出要求\n"
        f"{chunk_note}"
        f"请生成宽 {width} × 高 {height} 的 SVG 代码。\n"
        f"直接输出 SVG，从 `<svg` 开始，以 `</svg>` 结束，不要任何其他文字。"
    )


def _slim_layers(
    layers: Dict[str, Any],
    max_points_per_geom: int = 50,
) -> Dict[str, Any]:
    """
    策略 B 下的几何精简：
    - Polygon 只保留外环（第一个 ring）
    - LineString / Polygon 坐标序列超过 max_points 时均匀降采样
    - 删除 properties 中非关键字段
    """
    slim = {}
    for layer_name, layer_data in layers.items():
        slim_features = []
        for feat in layer_data.get("features", []):
            geom = feat.get("geometry", {})
            geom_slim = _slim_geometry(geom, max_points_per_geom)
            props = feat.get("properties", {})
            slim_props = {k: props[k] for k in ("text",) if k in props}
            slim_features.append({"type": "Feature", "geometry": geom_slim, "properties": slim_props})

        slim[layer_name] = {
            "semantic": layer_data.get("semantic"),
            "feature_count": layer_data.get("feature_count"),
            "features": slim_features,
        }
    return slim


def _slim_geometry(geom: Dict, max_pts: int) -> Dict:
    """对单个几何对象降采样。"""
    gtype = geom.get("type")
    if not gtype:
        return geom

    def downsample(coords):
        if len(coords) <= max_pts:
            return coords
        # 均匀降采样，保留首尾点
        step = len(coords) / max_pts
        indices = set([0, len(coords) - 1])
        indices.update(int(i * step) for i in range(1, max_pts - 1))
        return [coords[i] for i in sorted(indices)]

    if gtype == "LineString":
        return {"type": "LineString", "coordinates": downsample(geom.get("coordinates", []))}
    elif gtype == "Polygon":
        # 只取外环
        rings = geom.get("coordinates", [[]])
        return {"type": "Polygon", "coordinates": [downsample(rings[0])]}
    elif gtype == "MultiPolygon":
        polys = geom.get("coordinates", [])
        return {
            "type": "MultiPolygon",
            "coordinates": [[downsample(poly[0])] for poly in polys if poly],
        }
    return geom
