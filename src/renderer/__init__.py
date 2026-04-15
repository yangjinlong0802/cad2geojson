# -*- coding: utf-8 -*-
"""
renderer 子包 - GeoJSON → SVG 渲染管线

将 CAD→GeoJSON 转换结果通过大模型理解语义后还原为 SVG 图形。
数据流：
    GeoJSON
      └─ preprocessor    坐标压缩 / 几何简化 / 图层分组 / 语义标注 / 体积评估
           └─ size_assessor  决定策略 A/B/C/D
                └─ prompt_builder  构建三段式 Prompt
                     └─ llm_client  调用大模型
                          └─ svg_validator  语法 + 坐标范围校验
                               └─ SVG 输出
"""

from .pipeline import RenderPipeline
from .preprocessor import preprocess
from .size_assessor import assess_size, RenderStrategy

__all__ = ["RenderPipeline", "preprocess", "assess_size", "RenderStrategy"]
