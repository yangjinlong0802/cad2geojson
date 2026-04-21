# -*- coding: utf-8 -*-
"""
渲染主流程编排模块

RenderPipeline 类整合全部子模块，实现完整的数据流：

    GeoJSON ──► 预处理 ──► 策略路由 ──► Prompt构建 ──► LLM调用 ──► SVG校验 ──► 输出

调用方式（命令行）：
    from src.renderer import RenderPipeline
    pipeline = RenderPipeline()
    result = pipeline.run_file("output/test.geojson")
    with open("output/test.svg", "w") as f:
        f.write(result.svg)

调用方式（Web）：
    pipeline = RenderPipeline()
    result = pipeline.run(geojson_dict)
    return jsonify({"svg": result.svg, "warnings": result.warnings})
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import preprocessor as _pre
from . import prompt_builder as _prompt
from .chunker import make_chunks, merge_svg_parts
from .llm_client import LLMClient
from .size_assessor import RenderStrategy, assess_size
from .svg_validator import validate_svg

logger = logging.getLogger(__name__)


@dataclass
class RenderResult:
    """渲染管线输出结果"""
    svg: str                                    # 最终 SVG 代码
    strategy: str                               # 使用的策略（A/B/C/D）
    is_valid: bool                              # SVG 是否通过校验
    warnings: List[str] = field(default_factory=list)   # 校验警告
    errors: List[str] = field(default_factory=list)     # 校验错误
    elapsed_sec: float = 0.0                    # 总耗时（秒）
    token_hint: str = ""                        # Token 用量提示（可选）


class RenderPipeline:
    """
    GeoJSON → SVG 渲染管线。

    参数:
        model:         Claude 模型 ID
        api_key:       Anthropic API Key（默认从环境变量读取）
        viewbox_size:  SVG 视口尺寸（正方形，默认 1000）
        simplify_tol:  D-P 简化容差（0 = 自动）
        max_tokens:    LLM 最大输出 token
        temperature:   LLM 温度
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key: Optional[str] = None,
        viewbox_size: int = 1000,
        simplify_tol: float = 0.0,
        max_tokens: int = 8192,
        temperature: float = 0.1,
    ):
        self.viewbox_size = viewbox_size
        self.simplify_tol = simplify_tol
        self._llm = LLMClient(
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  公共接口
    # ──────────────────────────────────────────────────────────────────────────

    def run_file(self, geojson_path: str) -> RenderResult:
        """
        从文件路径读取 GeoJSON，执行完整渲染管线。

        参数:
            geojson_path: GeoJSON 文件路径（支持相对路径和绝对路径）

        返回:
            RenderResult 对象
        """
        path = Path(geojson_path)
        if not path.exists():
            raise FileNotFoundError(f"GeoJSON 文件不存在: {geojson_path}")

        with open(path, "r", encoding="utf-8") as f:
            geojson_data = json.load(f)

        logger.info(f"读取 GeoJSON 文件: {path} ({path.stat().st_size / 1024:.1f} KB)")
        return self.run(geojson_data)

    def run(self, geojson_data: Dict[str, Any]) -> RenderResult:
        """
        对内存中的 GeoJSON 字典执行完整渲染管线。

        参数:
            geojson_data: GeoJSON FeatureCollection 字典

        返回:
            RenderResult 对象
        """
        t0 = time.time()

        # ── 阶段 1：预处理 ────────────────────────────────────────────
        logger.info("=== 阶段 1/4：预处理 ===")
        processed = _pre.preprocess(
            geojson_data,
            simplify_tolerance=self.simplify_tol,
            viewbox_size=self.viewbox_size,
        )

        if processed["total_features"] == 0:
            return RenderResult(
                svg="<svg></svg>",
                strategy="none",
                is_valid=False,
                errors=["GeoJSON 中没有有效 Feature"],
                elapsed_sec=time.time() - t0,
            )

        # ── 阶段 2：策略路由 ──────────────────────────────────────────
        logger.info("=== 阶段 2/4：策略路由 ===")
        decision = assess_size(processed)
        logger.info(f"策略决策: {decision.strategy.value} — {decision.reason}")

        # ── 阶段 3：LLM 调用 ──────────────────────────────────────────
        logger.info("=== 阶段 3/4：LLM 调用 ===")
        viewport = processed["viewport"]
        raw_svg = self._call_llm(processed, decision)

        # ── 阶段 4：SVG 校验 ──────────────────────────────────────────
        logger.info("=== 阶段 4/4：SVG 校验 ===")
        vr = validate_svg(
            raw_svg,
            viewport_width=viewport.get("width", 800),
            viewport_height=viewport.get("height", 600),
        )

        elapsed = time.time() - t0
        logger.info(
            f"渲染完成: 策略={decision.strategy.value}，"
            f"SVG 大小={len(vr.svg_code)} 字节，"
            f"耗时={elapsed:.1f}s，"
            f"校验={'通过' if vr.is_valid else '失败'}"
        )

        return RenderResult(
            svg=vr.svg_code,
            strategy=decision.strategy.value,
            is_valid=vr.is_valid,
            warnings=vr.warnings,
            errors=vr.errors,
            elapsed_sec=elapsed,
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  内部 LLM 调用分发
    # ──────────────────────────────────────────────────────────────────────────

    def _call_llm(
        self,
        processed: Dict[str, Any],
        decision,
    ) -> str:
        """根据策略分发到对应的 LLM 调用方法。"""
        strategy = decision.strategy

        if strategy in (RenderStrategy.A, RenderStrategy.B):
            return self._call_strategy_ab(processed, strategy)
        elif strategy == RenderStrategy.C:
            return self._call_strategy_c(processed)
        else:
            return self._call_strategy_d(processed)

    def _call_strategy_ab(
        self,
        processed: Dict[str, Any],
        strategy: RenderStrategy,
    ) -> str:
        """
        策略 A/B：单次调用，直接喂入全部（或精简）数据。
        """
        messages = _prompt.build_messages(processed, strategy)
        logger.info(f"策略 {strategy.value}：单次 LLM 调用")
        return self._llm.generate(messages)

    def _call_strategy_c(self, processed: Dict[str, Any]) -> str:
        """
        策略 C：按图层分块，多轮调用，最后合并。
        """
        chunks = make_chunks(processed)
        total = len(chunks)
        logger.info(f"策略 C：共 {total} 块，逐块调用 LLM")

        svg_parts = []
        for i, chunk in enumerate(chunks):
            messages = _prompt.build_messages(
                chunk,
                RenderStrategy.C,
                chunk_index=i,
                total_chunks=total,
            )
            logger.info(f"  块 {i + 1}/{total}：图层 {list(chunk.get('layer_names', chunk['layers'].keys()))}")
            part = self._llm.generate(messages)
            svg_parts.append(part)

        if len(svg_parts) == 1:
            return svg_parts[0]

        # 合并所有片段
        logger.info(f"策略 C：合并 {len(svg_parts)} 个 SVG 片段")
        return merge_svg_parts(
            svg_parts,
            processed["viewport"],
            self._llm,
            _prompt,
        )

    def _call_strategy_d(self, processed: Dict[str, Any]) -> str:
        """
        策略 D：RAG 模式 —— 当前实现为按语义重要性分批调用
        （完整 RAG 向量检索需要 embedding 服务，此处用贪心分批替代）。

        TODO: 接入向量数据库（如 ChromaDB）实现真正的 RAG
        """
        logger.info("策略 D：超大图，使用 RAG 分批模式（当前版本为贪心分批替代）")
        # 策略 D 当前实现与策略 C 相同，后续可替换为向量检索
        return self._call_strategy_c(processed)
