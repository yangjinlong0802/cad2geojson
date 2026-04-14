# -*- coding: utf-8 -*-
"""
GeoJSON 构建模块

将解析并映射后的 CAD 实体组装为标准的 GeoJSON FeatureCollection。
每个 CAD 实体映射为一个 GeoJSON Feature，包含几何信息和属性信息。

支持两种图层组织方式：
    1. 所有实体放在一个 FeatureCollection 中，通过 properties.layer 区分
    2. 按图层分别输出为独立的 GeoJSON 文件

输出格式符合 GeoJSON 规范 (RFC 7946)，使用 WGS84 坐标系。
"""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Any, Optional

import geojson
from geojson import Feature, FeatureCollection

from .dxf_parser import ParsedEntity

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


def build_feature(
    entity: ParsedEntity,
    geometry: Dict[str, Any],
) -> Optional[Feature]:
    """
    将单个 CAD 实体构建为 GeoJSON Feature。

    Feature 包含两部分：
        - geometry: 几何形状（Point, LineString, Polygon 等）
        - properties: 属性信息（图层、颜色、原始实体类型、文字内容等）

    参数:
        entity:   解析后的 CAD 实体对象
        geometry: GeoJSON 几何对象字典（已完成坐标转换）

    返回:
        GeoJSON Feature 对象，如果几何数据为空则返回 None
    """
    if geometry is None:
        return None

    # 构建属性字典
    properties = {
        "layer": entity.layer,              # 图层名称
        "entity_type": entity.entity_type,  # 原始 CAD 实体类型（LINE, CIRCLE 等）
        "color": entity.color,              # CAD 颜色索引值
    }

    # 文字实体：添加文字内容到属性中
    if entity.text_content:
        properties["text"] = entity.text_content

    # ATTDEF 实体：添加属性标签和提示信息
    if entity.entity_type == "ATTDEF":
        geo = entity.geometry_data
        if geo.get("tag"):
            properties["attdef_tag"] = geo["tag"]
        if geo.get("prompt"):
            properties["attdef_prompt"] = geo["prompt"]

    # 块引用实体：添加块名和属性信息
    if entity.block_name:
        properties["block_name"] = entity.block_name

    # 块的 ATTRIB 属性（键值对）
    if entity.attributes:
        properties["attributes"] = entity.attributes

    # 创建 GeoJSON Feature
    feature = Feature(
        geometry=geometry,
        properties=properties,
    )

    return feature


def build_feature_collection(
    features: List[Feature],
) -> FeatureCollection:
    """
    将 Feature 列表组装为 GeoJSON FeatureCollection。

    FeatureCollection 是 GeoJSON 的顶层容器，包含一组 Feature。

    参数:
        features: GeoJSON Feature 对象列表

    返回:
        GeoJSON FeatureCollection 对象
    """
    # 过滤掉 None 值
    valid_features = [f for f in features if f is not None]

    collection = FeatureCollection(valid_features)

    logger.info(f"已构建 FeatureCollection，包含 {len(valid_features)} 个 Feature")
    return collection


def group_features_by_layer(
    features: List[Feature],
) -> Dict[str, List[Feature]]:
    """
    按图层对 Feature 进行分组。

    用于"按图层分别输出"模式，将属于同一图层的 Feature 归到一组。

    参数:
        features: GeoJSON Feature 对象列表

    返回:
        字典，键为图层名称，值为该图层的 Feature 列表
    """
    layer_groups = defaultdict(list)

    for feature in features:
        if feature is None:
            continue
        layer_name = feature.get("properties", {}).get("layer", "default")
        layer_groups[layer_name].append(feature)

    logger.info(f"Feature 已按 {len(layer_groups)} 个图层分组")
    return dict(layer_groups)


def validate_geojson(data: Any) -> bool:
    """
    校验 GeoJSON 数据的合法性。

    使用 geojson 库的验证功能检查数据是否符合 GeoJSON 规范。

    参数:
        data: GeoJSON 数据对象（FeatureCollection 或 Feature）

    返回:
        True 表示合法，False 表示不合法
    """
    # geojson 库的 is_valid 属性返回验证结果
    if hasattr(data, "is_valid"):
        valid = data.is_valid
        if not valid:
            # 获取详细的验证错误信息
            errors = data.errors()
            if errors:
                logger.warning(f"GeoJSON 校验失败: {errors}")
            return False
        return True

    # 如果没有 is_valid 属性，尝试手动检查
    logger.debug("无法进行 GeoJSON 格式校验")
    return True


def save_geojson(
    data: Any,
    output_path: str,
    indent: int = 2,
) -> None:
    """
    将 GeoJSON 数据保存到文件。

    输出时使用 ensure_ascii=False 以支持中文字符，
    使用缩进格式化以提高可读性。

    参数:
        data:        GeoJSON 数据对象
        output_path: 输出文件路径
        indent:      JSON 缩进空格数（默认 2）
    """
    output = Path(output_path)

    # 确保输出目录存在
    output.parent.mkdir(parents=True, exist_ok=True)

    # 将 GeoJSON 对象序列化为 JSON 字符串并写入文件
    with open(output, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,  # 支持中文等非 ASCII 字符
            indent=indent,       # 缩进格式化，提高可读性
        )

    # 计算输出文件大小
    file_size = output.stat().st_size
    size_str = _format_file_size(file_size)

    logger.info(f"GeoJSON 已保存: {output} ({size_str})")


def save_geojson_by_layers(
    features: List[Feature],
    output_dir: str,
    base_name: str = "output",
    indent: int = 2,
) -> List[str]:
    """
    按图层分别输出 GeoJSON 文件。

    每个图层生成一个独立的 GeoJSON 文件，文件名格式为：
        {base_name}_{layer_name}.geojson

    参数:
        features:   GeoJSON Feature 对象列表
        output_dir: 输出目录路径
        base_name:  输出文件名前缀
        indent:     JSON 缩进空格数

    返回:
        生成的文件路径列表
    """
    # 按图层分组
    layer_groups = group_features_by_layer(features)

    output_files = []
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    for layer_name, layer_features in layer_groups.items():
        # 清理图层名称中不适合做文件名的字符
        safe_layer_name = _sanitize_filename(layer_name)
        file_name = f"{base_name}_{safe_layer_name}.geojson"
        file_path = output_path / file_name

        # 构建该图层的 FeatureCollection
        collection = build_feature_collection(layer_features)

        # 保存到文件
        save_geojson(collection, str(file_path), indent)
        output_files.append(str(file_path))

    logger.info(f"已按图层输出 {len(output_files)} 个 GeoJSON 文件到: {output_dir}")
    return output_files


def _sanitize_filename(name: str) -> str:
    """
    清理字符串，使其适合用作文件名。

    将文件系统不允许的字符替换为下划线，并限制长度。

    参数:
        name: 原始字符串

    返回:
        清理后的安全文件名字符串
    """
    # 替换文件名中不允许的字符
    invalid_chars = '<>:"/\\|?*'
    result = name
    for char in invalid_chars:
        result = result.replace(char, "_")

    # 去除首尾空格和点号
    result = result.strip(" .")

    # 限制长度（避免超出文件系统限制）
    if len(result) > 100:
        result = result[:100]

    # 如果清理后为空字符串，使用默认名称
    return result or "unnamed_layer"


def _format_file_size(size_bytes: int) -> str:
    """
    将字节数格式化为人类可读的文件大小字符串。

    参数:
        size_bytes: 文件大小（字节）

    返回:
        格式化后的字符串（如 "1.5 MB"）
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
