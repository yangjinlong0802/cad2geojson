# -*- coding: utf-8 -*-
"""
GDAL/fiona DXF 解析模块

使用 fiona（GDAL/OGR 的 Python 封装）读取 DXF 文件，直接输出 GeoJSON Feature 列表。
与 ezdxf 解析器相比，GDAL 的优势是：
    - 对 DXF 格式的兼容性更好（GDAL 的 OGR DXF 驱动非常成熟）
    - 直接输出标准 GeoJSON 几何，无需手写几何映射
    - 自动处理曲线离散化、块引用展开等

劣势：
    - 属性信息较少（只有 Layer, EntityHandle, Text 等基本字段）
    - 无法获取 CAD 颜色值
    - 对 HATCH 等复杂实体支持一般

本模块输出与 ezdxf 解析器相同的 GeoJSON Feature 列表格式，
可以直接传给 coordinate_transformer 和 geojson_builder 使用。
"""

import logging
from typing import List, Dict, Any, Optional

from geojson import Feature

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


def _check_fiona_available() -> bool:
    """
    检查 fiona 库是否可用。

    返回:
        True 如果 fiona 可以正常导入，否则 False
    """
    try:
        import fiona
        return True
    except ImportError:
        return False


def parse_dxf_with_gdal(
    file_path: str,
    layers: List[str] = None,
    exclude_layers: List[str] = None,
) -> List[Feature]:
    """
    使用 fiona/GDAL 解析 DXF 文件，直接输出 GeoJSON Feature 列表。

    GDAL 的 OGR DXF 驱动会自动：
    - 将所有 DXF 实体转换为 GeoJSON 几何类型
    - 处理块引用展开
    - 离散化曲线和弧线
    - 提取图层、文字等基本属性

    参数:
        file_path:      DXF 文件路径
        layers:         只解析指定图层的实体（为 None 则解析所有图层）
        exclude_layers: 排除指定图层的实体

    返回:
        GeoJSON Feature 对象列表

    异常:
        ImportError: fiona 库未安装
        RuntimeError: DXF 文件读取失败
    """
    try:
        import fiona
    except ImportError:
        raise ImportError(
            "GDAL 引擎需要安装 fiona 库。请执行: pip install fiona"
        )

    logger.info(f"正在使用 GDAL/fiona 读取 DXF 文件: {file_path}")
    logger.info(f"fiona 版本: {fiona.__version__}, GDAL 版本: {fiona.gdal_version}")

    try:
        features = []
        total_count = 0      # 总实体数
        parsed_count = 0     # 成功解析的实体数
        skipped_count = 0    # 跳过的实体数

        with fiona.open(file_path) as src:
            logger.info(f"DXF 文件打开成功，CRS: {src.crs}")

            for fiona_feat in src:
                total_count += 1

                # 提取属性
                props = fiona_feat.get("properties", {})
                entity_layer = props.get("Layer", "0")

                # 图层过滤
                if layers and entity_layer not in layers:
                    skipped_count += 1
                    continue

                # 图层排除
                if exclude_layers and entity_layer in exclude_layers:
                    skipped_count += 1
                    continue

                # 获取几何数据
                geometry = fiona_feat.get("geometry")
                if geometry is None:
                    skipped_count += 1
                    continue

                # 跳过空几何
                geom_type = geometry.get("type", "")
                coords = geometry.get("coordinates")
                if not coords:
                    skipped_count += 1
                    continue

                # 处理 3D 坐标：投影到 2D（移除 Z 坐标）
                geometry = _flatten_to_2d(geometry)

                # 处理 GeometryCollection：拆分为独立 Feature
                if geom_type == "GeometryCollection":
                    sub_features = _explode_geometry_collection(
                        geometry, props, entity_layer
                    )
                    features.extend(sub_features)
                    parsed_count += len(sub_features)
                    continue

                # 构建标准 GeoJSON Feature
                feature_props = _build_properties(props, entity_layer)
                feature = Feature(
                    geometry=geometry,
                    properties=feature_props,
                )
                features.append(feature)
                parsed_count += 1

        logger.info(
            f"GDAL 解析完成: 总实体 {total_count} 个, "
            f"成功解析 {parsed_count} 个, "
            f"跳过 {skipped_count} 个"
        )

        return features

    except Exception as e:
        logger.error(f"GDAL/fiona 读取 DXF 失败: {e}")
        raise RuntimeError(f"GDAL 解析 DXF 文件失败: {e}")


def _build_properties(
    fiona_props: Dict[str, Any],
    entity_layer: str,
) -> Dict[str, Any]:
    """
    将 fiona 的属性字段映射为与 ezdxf 解析器一致的属性格式。

    fiona/GDAL 输出的 DXF 属性字段：
        - Layer: 图层名
        - PaperSpace: 是否在图纸空间
        - SubClasses: DXF 子类信息
        - Linetype: 线型名称
        - EntityHandle: 实体句柄（唯一标识）
        - Text: 文字内容（TEXT/MTEXT 实体）

    参数:
        fiona_props: fiona 原始属性字典
        entity_layer: 图层名称

    返回:
        标准化的属性字典
    """
    properties = {
        "layer": entity_layer,
        "entity_type": fiona_props.get("SubClasses", ""),  # DXF 子类作为类型参考
        "color": 0,  # GDAL 不直接提供 CAD 颜色索引值
    }

    # 添加文字内容（如果有）
    text = fiona_props.get("Text", "")
    if text:
        properties["text"] = text

    # 添加线型信息
    linetype = fiona_props.get("Linetype", "")
    if linetype:
        properties["linetype"] = linetype

    # 保留实体句柄作为唯一标识
    handle = fiona_props.get("EntityHandle", "")
    if handle:
        properties["entity_handle"] = handle

    return properties


def _flatten_to_2d(geometry: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 3D GeoJSON 几何坐标投影到 2D（移除 Z 坐标）。

    GDAL 输出的 DXF 几何可能包含 Z 坐标，但 GeoJSON 标准要求
    坐标为 [longitude, latitude] 或 [x, y]，多余的 Z 值可能
    干扰后续的坐标转换和可视化。

    参数:
        geometry: GeoJSON 几何对象

    返回:
        移除 Z 坐标后的 GeoJSON 几何对象
    """
    geom_type = geometry.get("type", "")
    coords = geometry.get("coordinates")

    if coords is None:
        return geometry

    try:
        flattened = _flatten_coords(geom_type, coords)
        return {"type": geom_type, "coordinates": flattened}
    except Exception:
        # 如果展平失败，返回原始几何
        return geometry


def _flatten_coords(geom_type: str, coords):
    """
    递归移除坐标中的 Z 值。

    根据 GeoJSON 几何类型的不同嵌套层级递归处理：
        Point:          [x, y, z] → [x, y]
        LineString:     [[x,y,z], ...] → [[x,y], ...]
        Polygon:        [[[x,y,z], ...], ...] → [[[x,y], ...], ...]
        MultiPolygon:   [[[[x,y,z], ...]], ...] → [[[[x,y], ...]], ...]

    参数:
        geom_type: 几何类型
        coords:    坐标数据

    返回:
        展平后的坐标数据
    """
    if geom_type == "Point":
        return tuple(coords[:2])

    elif geom_type in ("LineString", "MultiPoint"):
        return [tuple(c[:2]) for c in coords]

    elif geom_type in ("Polygon", "MultiLineString"):
        return [[tuple(c[:2]) for c in ring] for ring in coords]

    elif geom_type == "MultiPolygon":
        return [
            [[tuple(c[:2]) for c in ring] for ring in polygon]
            for polygon in coords
        ]

    elif geom_type == "GeometryCollection":
        # GeometryCollection 不使用 coordinates，直接返回
        return coords

    else:
        return coords


def _explode_geometry_collection(
    geometry: Dict[str, Any],
    fiona_props: Dict[str, Any],
    entity_layer: str,
) -> List[Feature]:
    """
    将 GeometryCollection 拆分为独立的 Feature 列表。

    GDAL 有时会将复合实体（如包含多种类型子元素的块）输出为
    GeometryCollection。为了后续处理和可视化方便，将其拆分。

    参数:
        geometry:     GeometryCollection 几何对象
        fiona_props:  fiona 原始属性
        entity_layer: 图层名称

    返回:
        拆分后的 Feature 列表
    """
    features = []
    geometries = geometry.get("geometries", [])

    for sub_geom in geometries:
        if sub_geom is None:
            continue

        # 展平子几何的坐标
        sub_geom = _flatten_to_2d(sub_geom)

        # 跳过空几何
        coords = sub_geom.get("coordinates")
        if not coords:
            continue

        feature_props = _build_properties(fiona_props, entity_layer)
        feature = Feature(
            geometry=sub_geom,
            properties=feature_props,
        )
        features.append(feature)

    return features
