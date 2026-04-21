# -*- coding: utf-8 -*-
"""
预处理层

负责对原始 GeoJSON 进行四步预处理，为后续 LLM 输入做准备：
  1. 坐标归一化 —— 将 WGS84 经纬度映射到 SVG 视口坐标 [0, viewbox_size]
  2. 几何简化   —— Douglas-Peucker 算法（shapely.simplify）降点减量
  3. 图层分组   —— 按 properties.layer 汇聚，统计每层实体数/几何类型分布
  4. 语义标签注入—— 由 semantic_labeler 给每个图层打上建筑语义 Tag

输出结构 ProcessedData：
    {
        "viewport": {"width": int, "height": int, "bbox": [minx,miny,maxx,maxy]},
        "layers": {
            "<layer_name>": {
                "semantic": str,             # 语义标签
                "feature_count": int,
                "geometry_types": [str],
                "features": [simplified GeoJSON feature, ...]
            }
        },
        "total_features": int,
        "original_byte_size": int,          # 序列化后字节数（用于策略决策）
        "compressed_byte_size": int,
    }
"""

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

from .semantic_labeler import label_layer

logger = logging.getLogger(__name__)

# SVG 视口默认尺寸（像素）
DEFAULT_VIEWBOX = 1000


def preprocess(
    geojson_data: Dict[str, Any],
    simplify_tolerance: float = 0.0,     # 0 表示自动计算
    viewbox_size: int = DEFAULT_VIEWBOX,
) -> Dict[str, Any]:
    """
    对原始 GeoJSON FeatureCollection 执行完整预处理流程。

    参数:
        geojson_data:       原始 GeoJSON FeatureCollection 字典
        simplify_tolerance: D-P 简化容差（SVG 坐标系下的像素值），0 = 自动
        viewbox_size:       SVG 视口宽高（正方形）

    返回:
        ProcessedData 字典（见模块文档）
    """
    features: List[Dict] = geojson_data.get("features", [])
    if not features:
        logger.warning("GeoJSON 中没有 Feature，返回空结果")
        return _empty_result()

    # ── 步骤 1：计算原始字节体积 ──────────────────────────────────────
    original_bytes = len(json.dumps(geojson_data, ensure_ascii=False).encode("utf-8"))
    logger.info(f"原始 GeoJSON 大小: {original_bytes / 1024:.1f} KB，共 {len(features)} 个 Feature")

    # ── 步骤 2：计算地理包围盒 ────────────────────────────────────────
    bbox = _calc_bbox(features)
    if bbox is None:
        logger.error("无法计算 GeoJSON 包围盒")
        return _empty_result()
    geo_minx, geo_miny, geo_maxx, geo_maxy = bbox

    # ── 步骤 3：构建归一化变换函数 ────────────────────────────────────
    # 将地理坐标映射到 [0, viewbox_size]，Y 轴翻转（SVG Y 轴向下）
    geo_w = geo_maxx - geo_minx or 1e-9
    geo_h = geo_maxy - geo_miny or 1e-9
    scale = viewbox_size / max(geo_w, geo_h)       # 等比缩放，保持纵横比
    svg_w = int(geo_w * scale)
    svg_h = int(geo_h * scale)

    def coord_normalize(x: float, y: float) -> Tuple[float, float]:
        """地理坐标 → SVG 坐标（Y 轴翻转）"""
        sx = round((x - geo_minx) * scale, 2)
        sy = round(svg_h - (y - geo_miny) * scale, 2)   # Y 翻转
        return sx, sy

    # ── 步骤 4：自动计算简化容差 ──────────────────────────────────────
    if simplify_tolerance == 0.0:
        # 地理坐标系下等效约 0.5 像素
        simplify_tolerance = 0.5 / scale
    logger.debug(f"D-P 简化容差 = {simplify_tolerance:.6f}（地理坐标系）")

    # ── 步骤 5：按图层分组并逐 Feature 处理 ──────────────────────────
    layer_buckets: Dict[str, List[Dict]] = defaultdict(list)
    for feat in features:
        layer = feat.get("properties", {}).get("layer") or "0"
        layer_buckets[layer].append(feat)

    processed_layers: Dict[str, Any] = {}
    for layer_name, layer_feats in layer_buckets.items():
        simplified = []
        geom_types = set()

        for feat in layer_feats:
            geom = feat.get("geometry")
            if not geom:
                continue

            # D-P 简化
            try:
                shp = shape(geom)
                if not shp.is_valid:
                    from shapely.validation import make_valid
                    shp = make_valid(shp)
                if simplify_tolerance > 0:
                    shp = shp.simplify(simplify_tolerance, preserve_topology=True)
                geom_simplified = mapping(shp)
            except Exception as e:
                logger.debug(f"几何简化失败，使用原始几何: {e}")
                geom_simplified = geom

            # 坐标归一化
            geom_normalized = _normalize_geometry(geom_simplified, coord_normalize)
            geom_types.add(geom_normalized.get("type", "Unknown"))

            # 构建精简 Feature（只保留必要属性）
            props = feat.get("properties", {})
            slim_props = {k: props[k] for k in ("layer", "entity_type", "text") if k in props}
            simplified.append({"type": "Feature", "geometry": geom_normalized, "properties": slim_props})

        # 注入语义标签
        semantic_tag = label_layer(layer_name, list(geom_types))

        processed_layers[layer_name] = {
            "semantic": semantic_tag,
            "feature_count": len(simplified),
            "geometry_types": sorted(geom_types),
            "features": simplified,
        }

    # ── 步骤 6：计算压缩后体积 ────────────────────────────────────────
    compressed_bytes = len(json.dumps(processed_layers, ensure_ascii=False).encode("utf-8"))
    logger.info(
        f"预处理完成: {len(processed_layers)} 个图层，"
        f"压缩后 {compressed_bytes / 1024:.1f} KB "
        f"（压缩比 {compressed_bytes / original_bytes:.1%}）"
    )

    return {
        "viewport": {
            "width": svg_w,
            "height": svg_h,
            "bbox": list(bbox),
        },
        "layers": processed_layers,
        "total_features": sum(v["feature_count"] for v in processed_layers.values()),
        "original_byte_size": original_bytes,
        "compressed_byte_size": compressed_bytes,
    }


# ─────────────────────────────── 内部工具 ────────────────────────────────────

def _calc_bbox(features: List[Dict]) -> Tuple[float, float, float, float]:
    """遍历所有 Feature 求最小包围盒 (minx, miny, maxx, maxy)。"""
    xs, ys = [], []
    for feat in features:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            shp = shape(geom)
            b = shp.bounds      # (minx, miny, maxx, maxy)
            if any(v != v for v in b):   # NaN 检测
                continue
            xs.extend([b[0], b[2]])
            ys.extend([b[1], b[3]])
        except Exception:
            pass
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _normalize_geometry(geom: Dict, fn) -> Dict:
    """
    递归地将几何对象中所有坐标对通过变换函数 fn 转换。

    fn 签名: (x: float, y: float) -> (sx: float, sy: float)
    支持 Point / MultiPoint / LineString / MultiLineString /
           Polygon / MultiPolygon / GeometryCollection。
    """
    gtype = geom.get("type")

    def transform_coord(coord):
        """单个坐标点变换（支持 2D / 3D，忽略 Z）"""
        x, y = coord[0], coord[1]
        sx, sy = fn(x, y)
        return [sx, sy]

    def transform_ring(ring):
        return [transform_coord(c) for c in ring]

    if gtype == "Point":
        return {"type": "Point", "coordinates": transform_coord(geom["coordinates"])}
    elif gtype == "MultiPoint":
        return {"type": "MultiPoint", "coordinates": [transform_coord(c) for c in geom["coordinates"]]}
    elif gtype == "LineString":
        return {"type": "LineString", "coordinates": transform_ring(geom["coordinates"])}
    elif gtype == "MultiLineString":
        return {"type": "MultiLineString", "coordinates": [transform_ring(r) for r in geom["coordinates"]]}
    elif gtype == "Polygon":
        return {"type": "Polygon", "coordinates": [transform_ring(r) for r in geom["coordinates"]]}
    elif gtype == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[transform_ring(r) for r in poly] for poly in geom["coordinates"]]}
    elif gtype == "GeometryCollection":
        return {"type": "GeometryCollection", "geometries": [_normalize_geometry(g, fn) for g in geom.get("geometries", [])]}
    else:
        return geom


def _empty_result() -> Dict[str, Any]:
    return {
        "viewport": {"width": 0, "height": 0, "bbox": []},
        "layers": {},
        "total_features": 0,
        "original_byte_size": 0,
        "compressed_byte_size": 0,
    }
