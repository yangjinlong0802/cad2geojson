# -*- coding: utf-8 -*-
"""
几何类型映射模块

将 CAD 实体的几何数据映射为 GeoJSON 兼容的几何类型。
主要处理：
    - 直接映射（POINT → Point, LINE → LineString）
    - 曲线离散化（CIRCLE → Polygon, ARC → LineString）
    - LWPOLYLINE 中弧线段（bulge 值）的插值处理
    - 几何合法性校验与修复

映射规则：
    POINT           → Point
    LINE            → LineString
    LWPOLYLINE(开)  → LineString
    LWPOLYLINE(闭)  → Polygon
    POLYLINE(开)    → LineString
    POLYLINE(闭)    → Polygon
    CIRCLE          → Polygon（离散化为正多边形）
    ARC             → LineString（离散化为折线）
    ELLIPSE(闭)     → Polygon
    ELLIPSE(开)     → LineString
    SPLINE          → LineString
    TEXT/MTEXT      → Point
    HATCH           → Polygon / MultiPolygon
    SOLID           → Polygon（2D 填充四边形/三角形）
    3DFACE          → Polygon（投影到 XY 平面）
    DIMENSION       → 分解为基本图元后分别映射
    MULTILEADER     → 分解为基本图元后分别映射
    ARC_DIMENSION   → 分解为基本图元后分别映射
    RAY/XLINE       → Point（起点/基点，无限线段退化为点）
    TOLERANCE/SHAPE/ACAD_TABLE → Point（插入点）
    IMAGE           → Polygon（图像边界四边形）
    PDFUNDERLAY/PDFREFERENCE   → Point（插入点）
    HELIX           → Polygon（螺旋线投影为圆）
    MESH            → MultiPolygon（网格面投影到 XY 平面）
    其他未知类型     → 通过 ezdxf.addons.geo.proxy() 自动转换（fallback）
"""

import math
import logging
from typing import List, Tuple, Dict, Any, Optional

from shapely.geometry import (
    Point,
    LineString,
    Polygon,
    MultiPolygon,
    mapping,
    shape,
)
from shapely.validation import make_valid

from .dxf_parser import ParsedEntity

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)

# 默认弧线离散化分段数
DEFAULT_ARC_SEGMENTS = 64


def bulge_to_arc_points(
    start: Tuple[float, float],
    end: Tuple[float, float],
    bulge: float,
    segments: int = 8,
) -> List[Tuple[float, float]]:
    """
    将 LWPOLYLINE 中的弧线段（由 bulge 值定义）转换为离散点序列。

    Bulge 值（凸度）的含义：
        bulge = tan(弧线段所对圆心角的 1/4)
        bulge > 0：逆时针弧（左凸）
        bulge < 0：顺时针弧（右凸）
        bulge = 0：直线段
        |bulge| = 1：半圆弧

    计算步骤：
        1. 由 bulge 值计算圆心角和半径
        2. 确定圆心位置
        3. 在弧线上均匀采样生成离散点

    参数:
        start:    弧线起点坐标 (x, y)
        end:      弧线终点坐标 (x, y)
        bulge:    凸度值
        segments: 弧线离散化的分段数

    返回:
        弧线上的离散点列表（不包含起点，包含终点）
    """
    # bulge 为 0 表示直线段，无需插值
    if abs(bulge) < 1e-10:
        return [end]

    # 计算起点到终点的弦长和中点
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    chord_length = math.sqrt(dx * dx + dy * dy)

    # 弦长为 0 时退化为点，直接返回
    if chord_length < 1e-10:
        return [end]

    # 由 bulge 值计算弓形高度（sagitta）
    # sagitta = |bulge| * chord_length / 2
    sagitta = abs(bulge) * chord_length / 2.0

    # 计算半径
    # R = (chord_length^2 / 4 + sagitta^2) / (2 * sagitta)
    radius = (chord_length * chord_length / 4.0 + sagitta * sagitta) / (2.0 * sagitta)

    # 计算圆心角（弧线所对应的总角度）
    # 圆心角 = 4 * atan(|bulge|)
    included_angle = 4.0 * math.atan(abs(bulge))

    # 计算弦的中点
    mid_x = (sx + ex) / 2.0
    mid_y = (sy + ey) / 2.0

    # 计算弦的单位法向量（垂直于弦方向）
    # 法向量方向取决于 bulge 的正负
    nx = -dy / chord_length  # 法向量 x 分量
    ny = dx / chord_length   # 法向量 y 分量

    # 计算圆心到弦中点的距离
    # d = R - sagitta（当 bulge > 0 时，圆心在弦的左侧）
    d = radius - sagitta

    # 确定圆心位置
    if bulge > 0:
        # 逆时针弧：圆心在弦的左侧
        cx = mid_x + d * nx
        cy = mid_y + d * ny
    else:
        # 顺时针弧：圆心在弦的右侧
        cx = mid_x - d * nx
        cy = mid_y - d * ny

    # 计算起点和终点相对于圆心的角度
    start_angle = math.atan2(sy - cy, sx - cx)
    end_angle = math.atan2(ey - cy, ex - cx)

    # 根据 bulge 正负确定弧线扫过方向
    # bulge > 0 时弧线在弦的左侧（逆时针），需要角度递减方向扫过
    # bulge < 0 时弧线在弦的右侧（顺时针），需要角度递增方向扫过
    if bulge > 0:
        # 逆时针弧：从起点到终点角度应递减
        if end_angle > start_angle:
            end_angle -= 2.0 * math.pi
    else:
        # 顺时针弧：从起点到终点角度应递增
        if end_angle < start_angle:
            end_angle += 2.0 * math.pi

    # 在弧线上均匀采样生成离散点
    points = []
    for i in range(1, segments + 1):
        t = i / segments
        angle = start_angle + (end_angle - start_angle) * t
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append((x, y))

    return points


def discretize_circle(
    center: Tuple[float, float],
    radius: float,
    segments: int = DEFAULT_ARC_SEGMENTS,
) -> List[Tuple[float, float]]:
    """
    将圆离散化为正多边形的点序列。

    圆在 GeoJSON 中没有直接表示方式，需要用正多边形近似。
    默认使用 64 段，足够平滑的同时控制数据量。

    参数:
        center:   圆心坐标 (x, y)
        radius:   半径
        segments: 分段数（默认 64）

    返回:
        正多边形的顶点列表（首尾重合，满足 GeoJSON Polygon 要求）
    """
    cx, cy = center
    points = []

    for i in range(segments):
        angle = 2.0 * math.pi * i / segments
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append((x, y))

    # GeoJSON 的 Polygon 要求首尾点重合
    points.append(points[0])

    return points


def discretize_arc(
    center: Tuple[float, float],
    radius: float,
    start_angle: float,
    end_angle: float,
    segments: int = DEFAULT_ARC_SEGMENTS,
) -> List[Tuple[float, float]]:
    """
    将圆弧离散化为折线点序列。

    圆弧在 GeoJSON 中用 LineString 表示，需要离散化为多个点。

    参数:
        center:      圆心坐标 (x, y)
        radius:      半径
        start_angle: 起始角度（度）
        end_angle:   终止角度（度）
        segments:    分段数

    返回:
        圆弧上的离散点列表
    """
    cx, cy = center

    # 将角度转换为弧度
    sa = math.radians(start_angle)
    ea = math.radians(end_angle)

    # 确保弧线方向正确（CAD 中圆弧默认逆时针）
    if ea <= sa:
        ea += 2.0 * math.pi

    points = []
    for i in range(segments + 1):
        t = i / segments
        angle = sa + (ea - sa) * t
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append((x, y))

    return points


def discretize_ellipse(
    center: Tuple[float, float],
    major_axis: Tuple[float, float],
    ratio: float,
    start_param: float,
    end_param: float,
    segments: int = DEFAULT_ARC_SEGMENTS,
) -> List[Tuple[float, float]]:
    """
    将椭圆（或椭圆弧）离散化为点序列。

    椭圆的参数化方程：
        P(t) = center + cos(t) * major_axis + sin(t) * minor_axis
    其中 minor_axis = ratio * rotate90(major_axis)

    参数:
        center:      中心点坐标 (x, y)
        major_axis:  长轴方向向量 (dx, dy)
        ratio:       短轴与长轴的比值
        start_param: 起始参数（弧度）
        end_param:   终止参数（弧度）
        segments:    分段数

    返回:
        离散点列表
    """
    cx, cy = center
    mx, my = major_axis

    # 计算短轴方向向量（长轴逆时针旋转 90°，乘以比值）
    minor_x = -my * ratio
    minor_y = mx * ratio

    # 确保参数范围正确
    if end_param <= start_param:
        end_param += 2.0 * math.pi

    points = []
    for i in range(segments + 1):
        t = start_param + (end_param - start_param) * i / segments
        x = cx + math.cos(t) * mx + math.sin(t) * minor_x
        y = cy + math.cos(t) * my + math.sin(t) * minor_y
        points.append((x, y))

    return points


def process_polyline_with_bulge(
    vertices: List[Tuple[float, float]],
    bulges: List[float],
    is_closed: bool,
    arc_segments: int = 8,
) -> List[Tuple[float, float]]:
    """
    处理包含弧线段（bulge 值）的多段线，生成完整的点序列。

    遍历每对相邻顶点，如果之间的 bulge 不为 0，则插入弧线离散化后的点。
    这是处理 LWPOLYLINE 的关键函数。

    参数:
        vertices:     顶点坐标列表
        bulges:       每个顶点对应的 bulge 值
        is_closed:    是否闭合
        arc_segments: 每个弧线段的离散化分段数

    返回:
        处理后的完整点序列
    """
    if not vertices:
        return []

    points = [vertices[0]]  # 从第一个顶点开始
    num_vertices = len(vertices)

    # 遍历每对相邻顶点
    for i in range(num_vertices - 1):
        bulge = bulges[i] if i < len(bulges) else 0.0
        start = vertices[i]
        end = vertices[i + 1]

        if abs(bulge) > 1e-10:
            # 有 bulge 值，需要做弧线插值
            arc_points = bulge_to_arc_points(start, end, bulge, arc_segments)
            points.extend(arc_points)
        else:
            # 直线段，直接添加终点
            points.append(end)

    # 处理闭合段：最后一个顶点到第一个顶点
    if is_closed and num_vertices > 1:
        bulge = bulges[-1] if bulges else 0.0
        if abs(bulge) > 1e-10:
            arc_points = bulge_to_arc_points(vertices[-1], vertices[0], bulge, arc_segments)
            points.extend(arc_points)
        else:
            # 确保闭合（首尾点重合）
            if points[0] != points[-1]:
                points.append(points[0])

    return points


def validate_and_fix_geometry(geom) -> Optional[Any]:
    """
    校验 Shapely 几何对象的合法性，不合法时尝试修复。

    GeoJSON 规范要求几何对象合法（不自交、不重叠等）。
    使用 Shapely 的 is_valid 检查，不合法时调用 make_valid() 尝试修复。

    参数:
        geom: Shapely 几何对象

    返回:
        合法的几何对象，如果无法修复则返回 None
    """
    if geom is None or geom.is_empty:
        logger.debug("几何对象为空，跳过")
        return None

    if geom.is_valid:
        return geom

    # 尝试修复不合法的几何对象
    logger.debug(f"几何对象不合法，尝试修复: {geom.geom_type}")
    try:
        fixed = make_valid(geom)
        if fixed.is_valid and not fixed.is_empty:
            logger.debug("几何对象修复成功")
            return fixed
        else:
            logger.warning("几何对象修复后仍然不合法或为空")
            return None
    except Exception as e:
        logger.warning(f"几何对象修复失败: {e}")
        return None


def map_entity_to_geometry(
    entity: ParsedEntity,
    arc_segments: int = DEFAULT_ARC_SEGMENTS,
) -> Optional[Dict[str, Any]]:
    """
    将解析后的 CAD 实体映射为 GeoJSON 几何对象。

    这是本模块的核心函数，根据实体类型调用对应的映射逻辑，
    生成 Shapely 几何对象，校验合法性后转换为 GeoJSON 格式的字典。

    参数:
        entity:       ParsedEntity 对象
        arc_segments: 弧线离散化的分段数

    返回:
        GeoJSON 几何对象的字典表示，如果映射失败则返回 None
    """
    geo = entity.geometry_data
    entity_type = geo.get("type", "")

    try:
        geom = _create_shapely_geometry(entity_type, geo, arc_segments)
    except Exception as e:
        logger.warning(f"创建几何对象失败（类型: {entity_type}）: {e}")
        return None

    # 校验并修复几何对象
    geom = validate_and_fix_geometry(geom)
    if geom is None:
        return None

    # 将 Shapely 几何对象转换为 GeoJSON 字典
    return mapping(geom)


def _create_shapely_geometry(
    entity_type: str,
    geo: Dict[str, Any],
    arc_segments: int,
):
    """
    根据实体类型创建 Shapely 几何对象。

    这是内部辅助函数，封装了所有实体类型到 Shapely 对象的映射逻辑。

    参数:
        entity_type:  实体类型字符串
        geo:          几何数据字典
        arc_segments: 弧线离散化的分段数

    返回:
        Shapely 几何对象

    异常:
        ValueError: 不支持的实体类型
    """
    if entity_type == "POINT":
        # POINT → Point：直接映射
        return Point(geo["location"])

    elif entity_type == "LINE":
        # LINE → LineString：两点连线
        return LineString([geo["start"], geo["end"]])

    elif entity_type in ("LWPOLYLINE", "POLYLINE"):
        # 多段线：需要处理 bulge 值（弧线段）
        vertices = geo["vertices"]
        bulges = geo.get("bulges", [0.0] * len(vertices))
        is_closed = geo.get("is_closed", False)

        # 处理弧线段，生成完整点序列
        points = process_polyline_with_bulge(
            vertices, bulges, is_closed,
            arc_segments=max(8, arc_segments // 8),  # 单个弧段用较少的分段数
        )

        if len(points) < 2:
            logger.debug("多段线点数不足，跳过")
            return None

        if is_closed and len(points) >= 4:
            # 闭合多段线 → Polygon（至少需要 4 个点：3 个顶点 + 闭合点）
            # 确保首尾重合
            if points[0] != points[-1]:
                points.append(points[0])
            return Polygon(points)
        else:
            # 开放多段线 → LineString
            return LineString(points)

    elif entity_type == "CIRCLE":
        # CIRCLE → Polygon：离散化为正多边形
        points = discretize_circle(geo["center"], geo["radius"], arc_segments)
        return Polygon(points)

    elif entity_type == "ARC":
        # ARC → LineString：离散化为折线
        points = discretize_arc(
            geo["center"], geo["radius"],
            geo["start_angle"], geo["end_angle"],
            arc_segments,
        )
        if len(points) < 2:
            return None
        return LineString(points)

    elif entity_type == "ELLIPSE":
        # ELLIPSE → Polygon 或 LineString
        points = discretize_ellipse(
            geo["center"], geo["major_axis"], geo["ratio"],
            geo["start_param"], geo["end_param"],
            arc_segments,
        )
        is_closed = geo.get("is_closed", False)
        if is_closed and len(points) >= 4:
            if points[0] != points[-1]:
                points.append(points[0])
            return Polygon(points)
        elif len(points) >= 2:
            return LineString(points)
        return None

    elif entity_type == "SPLINE":
        # SPLINE → LineString：使用已离散化的点
        points = geo["points"]
        is_closed = geo.get("is_closed", False)
        if len(points) < 2:
            return None
        if is_closed and len(points) >= 4:
            if points[0] != points[-1]:
                points.append(points[0])
            return Polygon(points)
        return LineString(points)

    elif entity_type in ("TEXT", "MTEXT", "ATTDEF"):
        # TEXT/MTEXT/ATTDEF → Point：以插入点作为点坐标
        return Point(geo["insert"])

    elif entity_type == "HATCH":
        # HATCH → Polygon 或 MultiPolygon
        boundaries = geo.get("boundaries", [])
        if not boundaries:
            return None

        if len(boundaries) == 1:
            # 单个边界路径 → Polygon
            ring = boundaries[0]
            if len(ring) >= 4:
                # 确保闭合
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                return Polygon(ring)
            return None
        else:
            # 多个边界路径
            # 第一个通常是外环，其余是内环（孔洞）
            # 简化处理：尝试构建带孔洞的多边形
            exterior = boundaries[0]
            if exterior[0] != exterior[-1]:
                exterior.append(exterior[0])

            interiors = []
            for ring in boundaries[1:]:
                if len(ring) >= 4:
                    if ring[0] != ring[-1]:
                        ring.append(ring[0])
                    interiors.append(ring)

            if len(exterior) >= 4:
                return Polygon(exterior, interiors)
            return None

    elif entity_type in ("SOLID", "3DFACE"):
        # SOLID / 3DFACE → Polygon：闭合的填充多边形
        vertices = geo["vertices"]
        if len(vertices) < 3:
            logger.debug(f"{entity_type} 顶点数不足 3 个，跳过")
            return None
        # 确保闭合
        ring = list(vertices)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return Polygon(ring)

    elif entity_type == "GEO_PROXY":
        # geo.proxy() fallback 产生的通用 GeoJSON 几何
        # 使用 shapely.geometry.shape() 从 GeoJSON 格式直接构建几何对象
        geo_type = geo.get("geo_type", "")
        coordinates = geo.get("coordinates", [])
        original_type = geo.get("original_type", "未知")
        try:
            # 构建标准 GeoJSON 几何字典，传给 shapely.shape()
            geojson_geom = {
                "type": geo_type,
                "coordinates": coordinates,
            }
            geom = shape(geojson_geom)
            if geom.is_empty:
                logger.debug(f"geo.proxy() 转换 {original_type} 结果为空几何")
                return None
            # 如果是 3D 坐标，投影到 2D（丢弃 Z 坐标）
            if geom.has_z:
                from shapely.ops import transform as shapely_transform
                geom = shapely_transform(lambda x, y, z=None: (x, y), geom)
            logger.debug(f"geo.proxy() 映射成功: {original_type} → {geo_type}")
            return geom
        except Exception as e:
            logger.debug(f"geo.proxy() 几何构建失败 ({original_type}): {e}")
            return None

    elif entity_type == "RAY":
        # RAY（半无限射线）→ 起点 Point（GeoJSON 无法表示无限线段）
        return Point(geo["start"])

    elif entity_type == "XLINE":
        # XLINE（双向无限构造线）→ 基点 Point
        return Point(geo["point"])

    elif entity_type in ("TOLERANCE", "SHAPE", "ACAD_TABLE", "UNDERLAY"):
        # 这些实体都有插入点，统一表示为 Point
        # TOLERANCE: 形位公差框插入点
        # SHAPE: 符号字体插入点
        # ACAD_TABLE: 表格左上角插入点
        # UNDERLAY: PDF/DWF 底图插入点
        return Point(geo["insert"])

    elif entity_type == "IMAGE":
        # IMAGE（光栅图像引用）→ 边界四边形 Polygon
        # 如果边界点不足，退化为插入点 Point
        boundary = geo.get("boundary", [])
        if len(boundary) >= 4:
            return Polygon(boundary)
        # 退化为插入点
        return Point(geo["insert"])

    elif entity_type == "HELIX":
        # HELIX（螺旋线）→ XY 平面投影为圆 Polygon
        # 以轴基点为圆心，螺旋半径为半径
        points = discretize_circle(geo["center"], geo["radius"], arc_segments)
        return Polygon(points)

    elif entity_type == "MESH":
        # MESH（多边形网格）→ 所有面投影到 XY 平面后合并为 MultiPolygon
        from shapely.geometry import MultiPolygon as ShapelyMultiPolygon
        faces = geo.get("faces", [])
        polygons = []
        for face_verts in faces:
            ring = list(face_verts)
            # 确保多边形闭合（首尾重合）
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            try:
                poly = Polygon(ring)
                if not poly.is_empty:
                    polygons.append(poly)
            except Exception as e:
                logger.debug(f"MESH 面多边形构建失败: {e}")
        if not polygons:
            return None
        if len(polygons) == 1:
            return polygons[0]
        return ShapelyMultiPolygon(polygons)

    else:
        logger.debug(f"不支持的几何映射类型: {entity_type}")
        return None
