# -*- coding: utf-8 -*-
"""
大图分块策略（策略 C）

将大体量 ProcessedData 按图层分块，每块控制在目标 token 预算内，
然后多轮调用 LLM，最后将多个 SVG 片段合并为完整图形。

分块算法：
    1. 按图层语义权重排序（结构/墙体等重要图层优先）
    2. 贪心装箱：累积图层直到接近目标字节上限，提交一块
    3. 最终合并：调用 LLM Merge Prompt 将多个 <svg> 片段拼合

合并策略：
    - 若片段数 ≤ 3：直接用 prompt_builder.build_merge_prompt 合并
    - 若片段数 > 3：先两两合并（归并树），再最终合并（避免超出上下文窗口）
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from .semantic_labeler import get_description

logger = logging.getLogger(__name__)

# 语义重要性排序权重（越小越重要，最先渲染）
_SEMANTIC_PRIORITY = {
    "boundary": 1,
    "structure": 2,
    "wall": 3,
    "column": 4,
    "stair": 5,
    "door": 6,
    "window": 7,
    "furniture": 8,
    "equipment": 9,
    "road": 10,
    "water": 11,
    "vegetation": 12,
    "axis": 13,
    "dimension": 14,
    "text": 15,
    "unknown": 99,
}

# 策略 C 每块目标大小（字节）——控制在 20KB 以内，避免大负载超时
DEFAULT_CHUNK_TARGET = 20 * 1024


def make_chunks(
    processed_data: Dict[str, Any],
    target_bytes: int = DEFAULT_CHUNK_TARGET,
) -> List[Dict[str, Any]]:
    """
    将 ProcessedData 按图层语义权重排序后，贪心分块。
    若单个图层超出 target_bytes，则在图层内部按 Feature 数均等切分。

    参数:
        processed_data: 预处理层输出
        target_bytes:   每块目标字节数

    返回:
        ProcessedData 子块列表（每个子块含 viewport + 一批图层/子图层）
    """
    viewport = processed_data.get("viewport", {})
    layers = processed_data.get("layers", {})

    # 计算每个图层大小 + 语义权重
    layer_items: List[Tuple[str, Any, int, int]] = []
    for name, data in layers.items():
        size = len(json.dumps({name: data}, ensure_ascii=False).encode("utf-8"))
        priority = _SEMANTIC_PRIORITY.get(data.get("semantic", "unknown"), 99)
        layer_items.append((name, data, size, priority))

    # 按语义优先级排序（重要的先处理）
    layer_items.sort(key=lambda x: (x[3], -x[2]))

    # 贪心装箱，超大图层做内部切分
    chunks: List[Dict[str, Any]] = []
    current_layers: Dict[str, Any] = {}
    current_size = 0

    for name, data, size, _ in layer_items:
        if size > target_bytes:
            # 超大图层：先提交当前积累块，再对该图层做子切分
            if current_layers:
                chunks.append(_build_chunk(viewport, current_layers, processed_data))
                current_layers = {}
                current_size = 0
            sub_chunks = _split_large_layer(name, data, size, target_bytes, viewport, processed_data)
            chunks.extend(sub_chunks)
        elif current_layers and current_size + size > target_bytes:
            chunks.append(_build_chunk(viewport, current_layers, processed_data))
            current_layers = {name: data}
            current_size = size
        else:
            current_layers[name] = data
            current_size += size

    if current_layers:
        chunks.append(_build_chunk(viewport, current_layers, processed_data))

    logger.info(f"大图分块完成: {len(layers)} 个图层 → {len(chunks)} 块")
    return chunks


def _split_large_layer(
    name: str,
    data: Dict[str, Any],
    size: int,
    target_bytes: int,
    viewport: Dict,
    original: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    对超大单图层按 Feature 数量均等切分为多个子块。
    每个子块共享同一图层 metadata，只是 features 列表不同。
    """
    features = data.get("features", [])
    if not features:
        return [_build_chunk(viewport, {name: data}, original)]

    # 估算每个 Feature 平均大小，计算每块 Feature 数
    avg_bytes_per_feature = size / max(len(features), 1)
    features_per_chunk = max(1, int(target_bytes / avg_bytes_per_feature))

    sub_chunks = []
    for i in range(0, len(features), features_per_chunk):
        sub_features = features[i:i + features_per_chunk]
        sub_data = {
            **data,
            "features": sub_features,
            "feature_count": len(sub_features),
        }
        chunk_name = f"{name}[{i // features_per_chunk + 1}]"   # 如 "3[1]", "3[2]"
        sub_chunks.append(_build_chunk(viewport, {chunk_name: sub_data}, original))

    logger.debug(f"图层 '{name}' ({size/1024:.1f}KB) 切分为 {len(sub_chunks)} 个子块")
    return sub_chunks


def merge_svg_parts(
    svg_parts: List[str],
    viewport: Dict[str, Any],
    llm_client=None,
    prompt_builder=None,
) -> str:
    """
    将多个 SVG 片段合并为完整 SVG。

    策略：直接用 XML 解析提取各片段的 <g> 分组，
    拼装到统一的 <svg> 根元素下，不再调用 LLM（避免超 token 截断）。

    参数:
        svg_parts:     各块生成的 SVG 字符串列表
        viewport:      视口信息
        llm_client:    保留参数（不再使用）
        prompt_builder: 保留参数（不再使用）

    返回:
        合并后的完整 SVG 字符串
    """
    import re
    import xml.etree.ElementTree as ET

    if len(svg_parts) == 1:
        return svg_parts[0]

    width  = viewport.get("width", 800)
    height = viewport.get("height", 600)

    # 收集所有 <g> 分组和 <defs> 内容
    all_g_blocks: List[str] = []
    all_defs_content: List[str] = []

    for i, part in enumerate(svg_parts):
        # 清理 markdown 代码块标记
        part = re.sub(r"```(?:svg|xml)?\s*", "", part)
        part = re.sub(r"```\s*$", "", part, flags=re.MULTILINE)

        # 修复截断的标签（LLM 输出 token 上限导致最后一行不完整）
        part = _fix_truncated_tag(part)

        # 提取 <defs> 内容
        defs_match = re.search(r"<defs[^>]*>([\s\S]*?)</defs>", part, re.IGNORECASE)
        if defs_match and defs_match.group(1).strip():
            all_defs_content.append(defs_match.group(1).strip())

        # 提取所有 <g ...>...</g> 块（支持嵌套）
        g_blocks = _extract_g_blocks(part)
        if g_blocks:
            all_g_blocks.extend(g_blocks)
        else:
            # 若无 <g> 块，提取 <svg> 内部全部内容
            inner = _extract_svg_inner(part)
            if inner:
                all_g_blocks.append(f"<!-- 片段 {i+1} -->\n{inner}")

        logger.debug(f"片段 {i+1}: 提取到 {len(g_blocks)} 个 <g> 块")

    # 组装最终 SVG
    defs_section = ""
    if all_defs_content:
        defs_section = f"  <defs>\n    {'    '.join(all_defs_content)}\n  </defs>\n"

    g_section = "\n".join(all_g_blocks)

    merged = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'{defs_section}'
        f'{g_section}\n'
        f'</svg>'
    )

    logger.info(f"SVG 合并完成: {len(svg_parts)} 个片段 → {len(merged)} 字节")
    return merged


def _fix_truncated_tag(svg_text: str) -> str:
    """
    修复 LLM 输出 token 上限导致的最后一行标签截断问题。

    策略：
        1. 找到最后一个完整的自闭合标签（如 <polyline .../> 或 <line .../>）
           或完整的闭合标签对（如 <text>...</text>）
        2. 截断到该位置
        3. 补全未闭合的 </g> 标签
    """
    import re

    # 找最后一个完整的元素结束位置
    # 匹配：自闭合标签 .../> 或 </xxx> 闭合标签
    last_complete = -1
    for m in re.finditer(r"(?:/>|</\w+\s*>)", svg_text):
        last_complete = m.end()

    if last_complete == -1:
        return svg_text

    # 截断到最后一个完整元素之后
    truncated = svg_text[:last_complete]

    # 统计未闭合的 <g> 标签数量，补全 </g>
    open_g  = len(re.findall(r"<g\b", truncated, re.IGNORECASE))
    close_g = len(re.findall(r"</g\s*>", truncated, re.IGNORECASE))
    missing = open_g - close_g
    if missing > 0:
        truncated += "\n" + "</g>" * missing

    return truncated


def _extract_g_blocks(svg_text: str) -> List[str]:
    """
    从 SVG 文本中提取所有顶层 <g> 块（含内容，支持嵌套）。
    使用栈式匹配，正确处理 <g> 嵌套。
    提取后对每个块内部修复截断的标签。
    """
    import re
    blocks = []
    i = 0
    n = len(svg_text)

    while i < n:
        # 找下一个 <g 开始
        m = re.search(r"<g\b", svg_text[i:], re.IGNORECASE)
        if not m:
            break
        start = i + m.start()
        depth = 0
        pos = start

        # 栈式扫描，找匹配的 </g>
        found = False
        while pos < n:
            open_m  = re.search(r"<g\b",    svg_text[pos:], re.IGNORECASE)
            close_m = re.search(r"</g\s*>", svg_text[pos:], re.IGNORECASE)

            if not close_m:
                break

            open_pos  = (pos + open_m.start())  if open_m  else n
            close_pos = (pos + close_m.start()) if close_m else n

            if open_pos < close_pos:
                depth += 1
                pos = open_pos + 2
            else:
                if depth == 1:
                    end = pos + close_m.start() + len(close_m.group(0))
                    block = svg_text[start:end]
                    # 修复块内部截断的标签（最后一个不完整的开放标签）
                    block = _fix_incomplete_tag_in_block(block)
                    blocks.append(block)
                    i = end
                    found = True
                    break
                depth -= 1
                pos = close_pos + len(close_m.group(0))

        if not found:
            i = start + 2

    return blocks


def _fix_incomplete_tag_in_block(block: str) -> str:
    """
    修复 <g> 块内部最后一个不完整的开放标签。
    例如：<polygon points="..." 没有 /> 结尾，直接被下一个标签打断。

    策略：
        1. 找到最后一个完整的元素（以 /> 或 </xxx> 结尾）
        2. 检查其后是否有不完整的标签（非 </g> 的内容）
        3. 若有，截断到最后一个完整元素之后
        4. 补全缺失的 </g> 标签（保持原有的 <g> 嵌套层级）
    """
    import re

    # 找最后一个完整元素的结束位置
    last_complete = -1
    for m in re.finditer(r"(?:/>|</\w[\w.-]*\s*>)", block):
        last_complete = m.end()

    if last_complete == -1:
        return block

    # 检查 last_complete 之后是否有不完整的标签
    remainder = block[last_complete:].strip()
    # 如果余下内容只有 </g> 标签（或为空），不需要截断
    if not remainder or re.match(r"^(</g\s*>)+\s*$", remainder, re.IGNORECASE):
        return block

    # 余下内容有不完整的标签，截断到 last_complete
    truncated = block[:last_complete]

    # 统计截断后未闭合的 <g> 数量，补全 </g>
    open_g  = len(re.findall(r"<g\b", truncated, re.IGNORECASE))
    close_g = len(re.findall(r"</g\s*>", truncated, re.IGNORECASE))
    missing = open_g - close_g
    if missing > 0:
        truncated += "\n" + "</g>" * missing

    return truncated


def _extract_svg_inner(svg_text: str) -> str:
    """提取 <svg> 标签内部的全部内容（去掉根标签）。"""
    import re
    m = re.search(r"<svg[^>]*>([\s\S]*)</svg>", svg_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return svg_text.strip()


def _build_chunk(
    viewport: Dict,
    layers: Dict[str, Any],
    original: Dict[str, Any],
) -> Dict[str, Any]:
    """构建单个分块的 ProcessedData 结构。"""
    return {
        "viewport": viewport,
        "layers": layers,
        "total_features": sum(v["feature_count"] for v in layers.values()),
        "original_byte_size": original.get("original_byte_size", 0),
        "compressed_byte_size": original.get("compressed_byte_size", 0),
        "layer_names": list(layers.keys()),     # 便于日志/调试
    }
