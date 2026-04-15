# -*- coding: utf-8 -*-
"""
体积评估与策略路由模块

根据预处理后 GeoJSON 的压缩体积，选择合适的 LLM 输入策略：

    策略 A （直接喂）
        条件: compressed_byte_size < 20 KB
        做法: 将完整 ProcessedData 序列化为 JSON 直接放入 Prompt
        适用: 简单平面图、小型 DXF

    策略 B （精简 JSON）
        条件: 20 KB ≤ compressed_byte_size < 100 KB
        做法: 保留几何 + 图层摘要，省略冗余属性；对多边形只传外环
        适用: 中等复杂度平面图

    策略 C （分块逐层）
        条件: 100 KB ≤ compressed_byte_size < 500 KB
        做法: 按图层拆分，每批 ≤ 50 KB，多轮调用 LLM，合并 SVG 片段
        适用: 较复杂建筑平面图（多图层）

    策略 D （RAG 检索增强）
        条件: compressed_byte_size ≥ 500 KB
        做法: 将每个图层向量化，用空间查询检索相关区块喂给 LLM
        适用: 超大复杂图纸（总平面、详细施工图）
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List


class RenderStrategy(str, Enum):
    """渲染策略枚举"""
    A = "A"   # 直接喂完整数据
    B = "B"   # 精简 JSON
    C = "C"   # 分块逐层
    D = "D"   # RAG 检索增强


@dataclass
class StrategyDecision:
    """策略决策结果"""
    strategy: RenderStrategy        # 选定策略
    compressed_kb: float            # 压缩后大小 (KB)
    chunk_count: int                # 预估分块数（策略 C/D 有效）
    reason: str                     # 决策原因说明（供日志/调试）


# ── 阈值常量（单位：字节）────────────────────────────────────────────────────
_THRESHOLD_A = 20 * 1024       # 20 KB
_THRESHOLD_B = 100 * 1024      # 100 KB
_THRESHOLD_C = 500 * 1024      # 500 KB

# 策略 C 每块目标大小
_CHUNK_TARGET = 50 * 1024      # 50 KB


def assess_size(processed_data: Dict[str, Any]) -> StrategyDecision:
    """
    根据 ProcessedData 的体积信息决定渲染策略。

    参数:
        processed_data: 预处理层输出的 ProcessedData 字典

    返回:
        StrategyDecision 对象
    """
    compressed = processed_data.get("compressed_byte_size", 0)
    kb = compressed / 1024

    if compressed < _THRESHOLD_A:
        return StrategyDecision(
            strategy=RenderStrategy.A,
            compressed_kb=kb,
            chunk_count=1,
            reason=f"数据量 {kb:.1f} KB < 20 KB，直接喂入大模型",
        )
    elif compressed < _THRESHOLD_B:
        return StrategyDecision(
            strategy=RenderStrategy.B,
            compressed_kb=kb,
            chunk_count=1,
            reason=f"数据量 {kb:.1f} KB 在 20-100 KB 区间，使用精简 JSON",
        )
    elif compressed < _THRESHOLD_C:
        # 估算分块数
        chunks = max(1, int(compressed / _CHUNK_TARGET) + 1)
        return StrategyDecision(
            strategy=RenderStrategy.C,
            compressed_kb=kb,
            chunk_count=chunks,
            reason=f"数据量 {kb:.1f} KB 在 100-500 KB 区间，按图层分块（约 {chunks} 块）",
        )
    else:
        # 策略 D：每个图层作为一个检索单元
        chunks = len(processed_data.get("layers", {}))
        return StrategyDecision(
            strategy=RenderStrategy.D,
            compressed_kb=kb,
            chunk_count=max(chunks, 1),
            reason=f"数据量 {kb:.1f} KB ≥ 500 KB，启用 RAG 检索增强（{chunks} 个图层块）",
        )


def split_into_chunks(
    processed_data: Dict[str, Any],
    target_bytes: int = _CHUNK_TARGET,
) -> List[Dict[str, Any]]:
    """
    将 ProcessedData 按图层拆分为多个子块，每块大小 ≤ target_bytes。

    拆分规则：
        - 以图层为最小单位（不拆分单个图层内部）
        - 贪心装箱：将图层按体积降序排列，依次填入当前块
        - 若单个图层超过 target_bytes，单独成一块

    参数:
        processed_data: 预处理层输出
        target_bytes:   每块目标字节数

    返回:
        ProcessedData 字典列表，每个元素包含 viewport + 该批图层
    """
    import json

    viewport = processed_data.get("viewport", {})
    layers = processed_data.get("layers", {})

    # 计算每个图层的序列化大小
    layer_sizes = []
    for name, data in layers.items():
        size = len(json.dumps({name: data}, ensure_ascii=False).encode("utf-8"))
        layer_sizes.append((name, data, size))

    # 按大小降序排列（便于贪心装箱）
    layer_sizes.sort(key=lambda x: x[2], reverse=True)

    chunks: List[Dict[str, Any]] = []
    current_chunk_layers: Dict[str, Any] = {}
    current_chunk_size = 0

    for name, data, size in layer_sizes:
        # 若当前块加入后超出目标大小，先提交当前块
        if current_chunk_layers and current_chunk_size + size > target_bytes:
            chunks.append(_make_chunk(viewport, current_chunk_layers, processed_data))
            current_chunk_layers = {}
            current_chunk_size = 0

        current_chunk_layers[name] = data
        current_chunk_size += size

    # 提交最后一块
    if current_chunk_layers:
        chunks.append(_make_chunk(viewport, current_chunk_layers, processed_data))

    return chunks


def _make_chunk(
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
    }
