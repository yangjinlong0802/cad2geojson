# -*- coding: utf-8 -*-
"""
SVG 语法与坐标范围校验模块

对 LLM 输出的 SVG 代码执行两层校验：

    第一层：语法校验
        - 提取 <svg> 标签（清理 LLM 可能添加的 markdown 代码块标记）
        - 使用 xml.etree.ElementTree 解析 XML 结构
        - 检查根元素是否为 <svg>
        - 确保 xmlns 属性存在

    第二层：坐标范围校验
        - 解析 SVG 中所有坐标属性（points / d / cx/cy/x/y）
        - 检测是否有坐标值大幅超出 viewBox 范围（越界 > 10%）
        - 记录越界警告（不强制拒绝，允许有少量溢出的合法 SVG）

修复操作（自动）：
    - 清理 markdown 代码块标记 (``` svg...``` → 提取内部)
    - 如果缺少 xmlns 属性，自动补全
    - 如果根元素不是 <svg>，在外层包裹一个 <svg>
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# SVG 命名空间
SVG_NS = "http://www.w3.org/2000/svg"


@dataclass
class ValidationResult:
    """SVG 校验结果"""
    is_valid: bool                        # 是否通过（语法正确且可渲染）
    svg_code: str                         # 清理/修复后的 SVG 代码
    errors: List[str] = field(default_factory=list)     # 严重错误
    warnings: List[str] = field(default_factory=list)   # 警告（不阻断渲染）


def validate_svg(
    raw_output: str,
    viewport_width: int = 800,
    viewport_height: int = 600,
    overflow_tolerance: float = 0.1,
) -> ValidationResult:
    """
    校验并修复 LLM 输出的 SVG 代码。

    参数:
        raw_output:          LLM 原始输出文本（可能包含 markdown 标记等噪声）
        viewport_width:      期望的 SVG 宽度（用于越界检测）
        viewport_height:     期望的 SVG 高度  viewport_height:     期望的 SVG 高度
        overflow_tolerance:  允许的坐标越界比例（默认 10%）

    返回:
        ValidationResult 对象
    """
    errors: List[str] = []
    warnings: List[str] = []

    # ── 第一步：提取 SVG 代码 ─────────────────────────────────────────
    svg_code = _extract_svg(raw_output)
    if not svg_code:
        errors.append("未能从 LLM 输出中提取到有效的 SVG 代码块")
        return ValidationResult(is_valid=False, svg_code=raw_output, errors=errors)

    # ── 第二步：补全 xmlns ────────────────────────────────────────────
    svg_code = _ensure_xmlns(svg_code)

    # ── 第三步：XML 语法解析 ──────────────────────────────────────────
    try:
        root = ET.fromstring(svg_code)
    except ET.ParseError as e:
        errors.append(f"SVG XML 语法错误: {e}")
        # 尝试简单修复（截断非法尾部）
        svg_code = _try_fix_broken_xml(svg_code)
        try:
            root = ET.fromstring(svg_code)
        except ET.ParseError:
            return ValidationResult(is_valid=False, svg_code=svg_code, errors=errors)

    # ── 第四步：检查根元素 ────────────────────────────────────────────
    tag_local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if tag_local != "svg":
        warnings.append(f"根元素不是 <svg>，而是 <{tag_local}>，自动包裹")
        svg_code = _wrap_in_svg(svg_code, viewport_width, viewport_height)

    # ── 第五步：坐标范围校验 ──────────────────────────────────────────
    overflow_warns = _check_coordinate_overflow(
        svg_code, viewport_width, viewport_height, overflow_tolerance
    )
    warnings.extend(overflow_warns)

    is_valid = len(errors) == 0
    if is_valid:
        logger.info("SVG 校验通过" + (f"（{len(warnings)} 条警告）" if warnings else ""))
    else:
        logger.warning(f"SVG 校验失败: {errors}")

    return ValidationResult(
        is_valid=is_valid,
        svg_code=svg_code,
        errors=errors,
        warnings=warnings,
    )


# ─────────────────────────────── 内部工具 ────────────────────────────────────

def _extract_svg(text: str) -> Optional[str]:
    """
    从原始文本中提取 <svg>...</svg> 片段。
    处理情况：
        1. 纯 SVG 输出
        2. markdown 代码块包裹：```svg ... ``` 或 ``` ... ```
        3. SVG 前后有解释文字
    """
    # 清理 markdown 代码块标记
    text = re.sub(r"```(?:svg|xml)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

    # 查找 <svg...>...</svg>
    match = re.search(r"(<svg[\s\S]*?</svg>)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # 尝试宽松匹配（大写 SVG 标签等）
    match = re.search(r"<[Ss][Vv][Gg][\s\S]*?</[Ss][Vv][Gg]>", text)
    if match:
        return match.group(0).strip()

    return None


def _ensure_xmlns(svg_code: str) -> str:
    """若 <svg> 缺少 xmlns 属性，自动补全。"""
    if 'xmlns' not in svg_code[:200]:
        svg_code = svg_code.replace(
            "<svg",
            f'<svg xmlns="{SVG_NS}"',
            1,
        )
    return svg_code


def _wrap_in_svg(content: str, width: int, height: int) -> str:
    """在内容外层包裹标准 <svg> 根元素。"""
    return (
        f'<svg xmlns="{SVG_NS}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'{content}\n'
        f'</svg>'
    )


def _try_fix_broken_xml(svg_code: str) -> str:
    """
    简单修复：找到最后一个完整的 </g> 或 </svg> 截断。
    这是最后手段，用于处理 LLM 输出截断的情况。
    """
    # 找最后一个完整的闭合标签
    for tag in ("</svg>", "</g>", "</path>"):
        pos = svg_code.rfind(tag)
        if pos != -1:
            truncated = svg_code[:pos + len(tag)]
            if tag != "</svg>":
                truncated += "\n</svg>"
            return truncated
    return svg_code


def _check_coordinate_overflow(
    svg_code: str,
    width: int,
    height: int,
    tolerance: float,
) -> List[str]:
    """
    检测 SVG 中的坐标是否大幅超出视口范围。

    解析以下属性中的数字：
        points="x1,y1 x2,y2 ..."
        d="M x y L x y ..."
        cx/cy/x/y/x1/y1/x2/y2

    返回警告信息列表。
    """
    warnings = []
    x_limit = width * (1 + tolerance)
    y_limit = height * (1 + tolerance)
    x_min_limit = -width * tolerance
    y_min_limit = -height * tolerance

    # 提取所有数字对（x, y）—— 粗略扫描
    # points 属性
    points_matches = re.findall(r'points="([^"]+)"', svg_code)
    for match in points_matches:
        coords = re.findall(r"[-\d.]+", match)
        numbers = [float(c) for c in coords if _is_number(c)]
        xs = numbers[0::2]
        ys = numbers[1::2]
        _check_ranges(xs, ys, x_min_limit, x_limit, y_min_limit, y_limit, "points", warnings)

    # cx/cy 属性（圆心）
    cx_vals = [float(m) for m in re.findall(r'cx="([-\d.]+)"', svg_code) if _is_number(m)]
    cy_vals = [float(m) for m in re.findall(r'cy="([-\d.]+)"', svg_code) if _is_number(m)]
    _check_ranges(cx_vals, cy_vals, x_min_limit, x_limit, y_min_limit, y_limit, "cx/cy", warnings)

    if warnings:
        logger.debug(f"坐标越界警告: {warnings}")

    return warnings


def _check_ranges(
    xs: List[float],
    ys: List[float],
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    attr_name: str,
    warnings: List[str],
) -> None:
    """检查坐标列表是否有超出范围的值，将警告追加到 warnings。"""
    overflow_x = [x for x in xs if x < xmin or x > xmax]
    overflow_y = [y for y in ys if y < ymin or y > ymax]
    if overflow_x:
        warnings.append(f"{attr_name} 中有 {len(overflow_x)} 个 X 坐标越界: 示例={overflow_x[:3]}")
    if overflow_y:
        warnings.append(f"{attr_name} 中有 {len(overflow_y)} 个 Y 坐标越界: 示例={overflow_y[:3]}")


def _is_number(s: str) -> bool:
    """判断字符串是否可转为 float。"""
    try:
        float(s)
        return True
    except ValueError:
        return False
