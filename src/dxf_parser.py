# -*- coding: utf-8 -*-
"""
DXF 文件解析模块

使用 ezdxf 库读取 DXF 文件，遍历模型空间（modelspace）中的所有实体，
提取实体类型、图层、颜色、几何数据和属性信息。

支持的实体类型：
    基本图元: LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, POINT, TEXT, MTEXT
    高级图元: ELLIPSE, SPLINE, HATCH, SOLID, 3DFACE
    块引用:   INSERT（使用 explode() 自动展开）
    复合实体: DIMENSION, MULTILEADER, LEADER, MLINE（使用 virtual_entities() 分解）
    其他类型: 通过 ezdxf.addons.geo.proxy() 自动 fallback 转换
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import ezdxf
from ezdxf import recover
from ezdxf.addons import geo as ezdxf_geo
from ezdxf.entities import DXFEntity

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


@dataclass
class ParsedEntity:
    """
    解析后的 CAD 实体数据结构。

    将 ezdxf 的实体对象统一转换为此数据类，方便后续的几何映射和 GeoJSON 构建。

    属性:
        entity_type:  实体类型名称（如 LINE, CIRCLE 等）
        layer:        所在图层名称
        color:        颜色值（CAD 颜色索引号）
        geometry_data: 几何数据字典，不同实体类型包含不同的键值
        attributes:   块引用的属性键值对（仅 INSERT 实体有值）
        block_name:   块引用的块名称（仅 INSERT 实体有值）
        text_content: 文字内容（仅 TEXT/MTEXT 实体有值）
    """
    entity_type: str
    layer: str = ""
    color: int = 0
    geometry_data: Dict[str, Any] = field(default_factory=dict)
    attributes: Dict[str, str] = field(default_factory=dict)
    block_name: str = ""
    text_content: str = ""


def read_dxf_file(file_path: str) -> ezdxf.document.Drawing:
    """
    读取 DXF 文件并返回 ezdxf 的文档对象。

    优先使用 ezdxf.recover.readfile() 进行容错读取，能处理：
    - 编码问题（如德语变音字符 ä/ö/ü、中文等）
    - 轻微损坏或不规范的 DXF 文件
    - 缺少必要结构的 DXF 文件
    如果 recover 也失败，回退到标准 ezdxf.readfile()。

    参数:
        file_path: DXF 文件路径

    返回:
        ezdxf.document.Drawing 文档对象

    异常:
        FileNotFoundError: 文件不存在
        ezdxf.DXFError: DXF 文件格式错误或不支持的版本
    """
    logger.info(f"正在读取 DXF 文件: {file_path}")
    try:
        # 优先使用 recover 模式读取，容错性更强
        # recover 会自动修复常见的 DXF 结构问题和编码问题
        doc, auditor = recover.readfile(file_path)

        # 检查 auditor 报告的问题并记录
        if auditor.has_errors:
            logger.warning(
                f"DXF 文件存在 {len(auditor.errors)} 个错误，已自动修复"
            )
            for error in auditor.errors:
                logger.debug(f"  修复的错误: {error}")
        if auditor.has_fixes:
            logger.info(
                f"DXF 文件已自动修复 {len(auditor.fixes)} 个问题"
            )

        logger.info(
            f"DXF 文件读取成功（recover 模式），版本: {doc.dxfversion}, "
            f"图层数: {len(doc.layers)}"
        )
        return doc
    except Exception as recover_err:
        # recover 模式失败时，回退到标准读取
        logger.debug(f"recover 模式读取失败: {recover_err}，尝试标准模式")
        try:
            doc = ezdxf.readfile(file_path)
            logger.info(
                f"DXF 文件读取成功（标准模式），版本: {doc.dxfversion}, "
                f"图层数: {len(doc.layers)}"
            )
            return doc
        except ezdxf.DXFError as e:
            logger.error(f"DXF 文件解析失败: {e}")
            raise
        except IOError as e:
            logger.error(f"无法读取文件: {e}")
            raise FileNotFoundError(f"DXF 文件不存在或无法读取: {file_path}")


def get_entity_color(entity: DXFEntity) -> int:
    """
    获取实体的颜色值。

    CAD 中颜色有多种来源：实体自身颜色、图层颜色、块颜色等。
    这里优先取实体自身的颜色，如果是 BYLAYER（256）则使用图层颜色。

    参数:
        entity: ezdxf 实体对象

    返回:
        CAD 颜色索引号（ACI, AutoCAD Color Index）
    """
    try:
        color = entity.dxf.color
        # 256 表示 BYLAYER（随层），需要从图层获取颜色
        if color == 256:
            # 尝试从文档图层表中获取图层颜色
            try:
                doc = entity.doc
                if doc:
                    layer = doc.layers.get(entity.dxf.layer)
                    if layer:
                        return layer.color
            except Exception:
                pass
        return color
    except AttributeError:
        # 某些实体可能没有颜色属性，返回默认值 0
        return 0


def parse_line(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 LINE 实体的几何数据。

    LINE 实体由两个端点定义。

    参数:
        entity: LINE 类型的 ezdxf 实体

    返回:
        包含起点和终点坐标的字典
    """
    start = entity.dxf.start
    end = entity.dxf.end
    return {
        "type": "LINE",
        "start": (start.x, start.y),  # 起点坐标 (x, y)
        "end": (end.x, end.y),         # 终点坐标 (x, y)
    }


def parse_lwpolyline(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 LWPOLYLINE（轻量多段线）实体的几何数据。

    LWPOLYLINE 是 CAD 中最常见的实体类型之一，由多个顶点组成。
    每个顶点可能包含 bulge 值（凸度），用于表示弧线段。
    bulge = tan(弧线段所对应圆心角的 1/4)，正值为逆时针弧，负值为顺时针弧。

    参数:
        entity: LWPOLYLINE 类型的 ezdxf 实体

    返回:
        包含顶点列表、凸度列表和闭合状态的字典
    """
    # get_points() 返回格式为 (x, y, start_width, end_width, bulge) 的元组列表
    points_data = list(entity.get_points(format="xyseb"))

    # 提取坐标点和 bulge 值
    vertices = []   # 顶点坐标列表
    bulges = []     # 对应的凸度值列表
    for point in points_data:
        x, y = point[0], point[1]
        bulge = point[4] if len(point) > 4 else 0.0  # bulge 是第 5 个元素
        vertices.append((x, y))
        bulges.append(bulge)

    # 判断多段线是否闭合
    is_closed = entity.closed

    return {
        "type": "LWPOLYLINE",
        "vertices": vertices,  # 顶点坐标列表 [(x1,y1), (x2,y2), ...]
        "bulges": bulges,      # 每个顶点对应的凸度值
        "is_closed": is_closed,  # 是否闭合
    }


def parse_polyline(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 POLYLINE（旧版多段线）实体的几何数据。

    POLYLINE 是旧版的多段线实体，与 LWPOLYLINE 类似但数据结构不同。
    通过 virtual_entities() 或 points() 方法获取顶点信息。

    参数:
        entity: POLYLINE 类型的 ezdxf 实体

    返回:
        包含顶点列表、凸度列表和闭合状态的字典
    """
    vertices = []
    bulges = []

    # 遍历 POLYLINE 的顶点实体（VERTEX）
    for vertex in entity.vertices:
        location = vertex.dxf.location
        vertices.append((location.x, location.y))
        # 获取凸度值，默认为 0（直线段）
        bulge = vertex.dxf.get("bulge", 0.0)
        bulges.append(bulge)

    # 判断是否闭合
    is_closed = entity.is_closed

    return {
        "type": "POLYLINE",
        "vertices": vertices,
        "bulges": bulges,
        "is_closed": is_closed,
    }


def parse_circle(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 CIRCLE 实体的几何数据。

    CIRCLE 由圆心和半径定义。

    参数:
        entity: CIRCLE 类型的 ezdxf 实体

    返回:
        包含圆心坐标和半径的字典
    """
    center = entity.dxf.center
    return {
        "type": "CIRCLE",
        "center": (center.x, center.y),  # 圆心坐标
        "radius": entity.dxf.radius,     # 半径
    }


def parse_arc(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 ARC（圆弧）实体的几何数据。

    ARC 由圆心、半径、起始角度和终止角度定义。
    角度以度为单位，逆时针方向为正。

    参数:
        entity: ARC 类型的 ezdxf 实体

    返回:
        包含圆心、半径和角度范围的字典
    """
    center = entity.dxf.center
    return {
        "type": "ARC",
        "center": (center.x, center.y),       # 圆心坐标
        "radius": entity.dxf.radius,           # 半径
        "start_angle": entity.dxf.start_angle, # 起始角度（度）
        "end_angle": entity.dxf.end_angle,     # 终止角度（度）
    }


def parse_point(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 POINT 实体的几何数据。

    参数:
        entity: POINT 类型的 ezdxf 实体

    返回:
        包含点坐标的字典
    """
    location = entity.dxf.location
    return {
        "type": "POINT",
        "location": (location.x, location.y),  # 点坐标
    }


def parse_text(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 TEXT 实体的几何数据和文本内容。

    TEXT 是单行文字实体，包含插入点和文字内容。

    DXF TEXT 有两个坐标属性：
    - insert (group 10)：第一定义点（左对齐时即文字起点）
    - align_point (group 11)：第二定义点（非左对齐时的对齐锚点）

    对齐方式 (halign):
        0=LEFT（左，默认）, 1=CENTER（中）, 2=RIGHT（右）
        3=ALIGNED（两端对齐）, 4=MIDDLE（正中）, 5=FIT（适合）
    垂直对齐 (valign):
        0=BASELINE（基线，默认）, 1=BOTTOM（下）, 2=MIDDLE（中）, 3=TOP（上）

    非左对齐时，align_point 才是真正的定位参考点，用它作为 GeoJSON 的 Point
    坐标，可以在反向导出时以相同对齐方式还原到正确位置。

    参数:
        entity: TEXT 类型的 ezdxf 实体

    返回:
        包含插入点坐标和文字内容的字典
    """
    insert = entity.dxf.insert
    halign = entity.dxf.get("halign", 0)  # 水平对齐方式（0=左对齐，1=居中，2=右对齐…）
    valign = entity.dxf.get("valign", 0)  # 垂直对齐方式（0=基线，1=下，2=中，3=上）

    # 非左/基线对齐时，优先使用 align_point 作为定位锚点
    # align_point 是文字的对齐参考坐标，比 insert 更能反映文字的视觉位置
    pos = (insert.x, insert.y)
    if halign != 0 or valign != 0:
        try:
            ap = entity.dxf.align_point
            pos = (ap.x, ap.y)
        except Exception:
            pass  # align_point 不存在时回退使用 insert

    return {
        "type": "TEXT",
        "insert": pos,                                 # 定位参考坐标（align_point 优先）
        "text": entity.dxf.text,                      # 文字内容
        "height": entity.dxf.height,                  # 文字高度
        "rotation": entity.dxf.get("rotation", 0.0),  # 旋转角度（度），竖向文字通常为 90/270
        "halign": halign,                              # 水平对齐方式（回写时还原）
        "valign": valign,                              # 垂直对齐方式（回写时还原）
    }


def parse_mtext(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 MTEXT（多行文字）实体的几何数据和文本内容。

    MTEXT 支持多行文本、格式化等功能。
    字高属性为 char_height（区别于 TEXT 的 height）。
    旋转角属性为 rotation，竖向文字通常为 90° 或 270°。

    参数:
        entity: MTEXT 类型的 ezdxf 实体

    返回:
        包含插入点坐标、文字内容、字高和旋转角的字典
    """
    insert = entity.dxf.insert
    # MTEXT 的文本内容可能包含格式化标记，使用 plain_text() 获取纯文本
    plain_text = entity.plain_text()
    return {
        "type": "MTEXT",
        "insert": (insert.x, insert.y),               # 文字插入点坐标
        "text": plain_text,                            # 纯文本内容（去除格式化标记）
        "height": entity.dxf.get("char_height", 2.5), # 字符高度（MTEXT 用 char_height）
        "rotation": entity.dxf.get("rotation", 0.0),  # 旋转角度（度），竖向文字通常为 90/270
    }


def parse_ellipse(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 ELLIPSE（椭圆）实体的几何数据。

    椭圆由中心点、长轴端点（相对于中心）、短长轴比和参数范围定义。
    参数范围从 start_param 到 end_param（弧度），完整椭圆为 0 到 2π。

    参数:
        entity: ELLIPSE 类型的 ezdxf 实体

    返回:
        包含椭圆参数的字典
    """
    center = entity.dxf.center
    major_axis = entity.dxf.major_axis  # 长轴端点（相对于中心的偏移向量）

    return {
        "type": "ELLIPSE",
        "center": (center.x, center.y),                    # 中心点坐标
        "major_axis": (major_axis.x, major_axis.y),        # 长轴方向向量
        "ratio": entity.dxf.ratio,                         # 短轴与长轴的比值
        "start_param": entity.dxf.start_param,             # 起始参数（弧度）
        "end_param": entity.dxf.end_param,                 # 终止参数（弧度）
        "is_closed": abs(entity.dxf.end_param - entity.dxf.start_param - math.pi * 2) < 1e-6,
    }


def parse_spline(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 SPLINE（样条曲线）实体的几何数据。

    使用 ezdxf 的 flattening() 方法将样条曲线离散化为折线点序列。
    这样可以避免手动计算 B 样条插值，由 ezdxf 内部处理精度。

    参数:
        entity: SPLINE 类型的 ezdxf 实体

    返回:
        包含离散化后的点列表的字典
    """
    # flattening() 方法将样条曲线离散化为直线段
    # distance 参数控制最大弦高偏差（精度）
    try:
        points = [(p.x, p.y) for p in entity.flattening(distance=0.01)]
    except Exception as e:
        # 某些异常样条曲线可能无法离散化，降级使用控制点
        logger.warning(f"样条曲线离散化失败，使用控制点代替: {e}")
        points = [(p.x, p.y) for p in entity.control_points]

    return {
        "type": "SPLINE",
        "points": points,          # 离散化后的点列表
        "is_closed": entity.closed,  # 是否闭合
    }


def parse_hatch(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 HATCH（填充）实体的几何数据。

    HATCH 实体包含一个或多个边界路径，每个边界路径可以是：
    - 由线段和弧线组成的边缘路径（EdgePath）
    - 由多段线定义的多段线路径（PolylinePath）

    参数:
        entity: HATCH 类型的 ezdxf 实体

    返回:
        包含所有边界路径的字典
    """
    boundaries = []  # 存储所有边界路径

    for boundary in entity.paths:
        path_points = []

        # 判断边界路径类型
        if hasattr(boundary, "vertices"):
            # 多段线路径：直接提取顶点
            for vertex in boundary.vertices:
                path_points.append((vertex[0], vertex[1]))
            # 确保闭合
            if path_points and boundary.is_closed:
                if path_points[0] != path_points[-1]:
                    path_points.append(path_points[0])
        elif hasattr(boundary, "edges"):
            # 边缘路径：遍历每条边（线段、弧线、椭圆弧等）提取点
            for edge in boundary.edges:
                edge_type = type(edge).__name__
                if hasattr(edge, "start") and hasattr(edge, "end") and edge_type == "LineEdge":
                    # 线段边缘
                    path_points.append((edge.start.x, edge.start.y))
                    path_points.append((edge.end.x, edge.end.y))
                elif hasattr(edge, "center") and hasattr(edge, "radius"):
                    # 圆弧边缘（ArcEdge）：用 radius 离散化
                    if hasattr(edge, "start_angle") and hasattr(edge, "end_angle"):
                        center = edge.center
                        radius = edge.radius
                        start_angle = math.radians(edge.start_angle)
                        end_angle = math.radians(edge.end_angle)
                        # 离散化弧线为若干点
                        if end_angle < start_angle:
                            end_angle += 2 * math.pi
                        num_segments = max(8, int(abs(end_angle - start_angle) / (math.pi / 16)))
                        for i in range(num_segments + 1):
                            angle = start_angle + (end_angle - start_angle) * i / num_segments
                            x = center.x + radius * math.cos(angle)
                            y = center.y + radius * math.sin(angle)
                            path_points.append((x, y))
                elif hasattr(edge, "center") and hasattr(edge, "major_axis"):
                    # 椭圆弧边缘（EllipseEdge）：用参数方程离散化
                    try:
                        center = edge.center
                        major_axis = edge.major_axis
                        ratio = edge.ratio  # 短轴/长轴比
                        start_param = edge.start_param
                        end_param = edge.end_param
                        # 长轴长度和角度
                        major_len = math.sqrt(major_axis.x**2 + major_axis.y**2)
                        minor_len = major_len * ratio
                        rotation = math.atan2(major_axis.y, major_axis.x)
                        cos_r = math.cos(rotation)
                        sin_r = math.sin(rotation)
                        # 参数范围处理
                        if end_param < start_param:
                            end_param += 2 * math.pi
                        num_segments = max(16, int(abs(end_param - start_param) / (math.pi / 16)))
                        for i in range(num_segments + 1):
                            t = start_param + (end_param - start_param) * i / num_segments
                            # 椭圆参数方程
                            ex = major_len * math.cos(t)
                            ey = minor_len * math.sin(t)
                            # 旋转
                            x = center.x + ex * cos_r - ey * sin_r
                            y = center.y + ex * sin_r + ey * cos_r
                            path_points.append((x, y))
                    except Exception as e:
                        logger.debug(f"HATCH 椭圆弧边缘离散化失败: {e}")
                elif hasattr(edge, "control_points"):
                    # 样条曲线边缘（SplineEdge）
                    try:
                        for pt in edge.control_points:
                            path_points.append((pt.x, pt.y))
                    except Exception:
                        pass

        if path_points:
            boundaries.append(path_points)

    return {
        "type": "HATCH",
        "boundaries": boundaries,  # 边界路径列表，每个路径是点列表
    }


def parse_solid(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 SOLID（2D 填充实体）的几何数据。

    SOLID 是 CAD 中的 2D 填充区域，由 3 或 4 个顶点定义。
    注意：SOLID 的顶点顺序是特殊的 —— 第 3 和第 4 个顶点是交叉存储的：
        vtx0 → vtx1 → vtx3 → vtx2（而非 vtx0 → vtx1 → vtx2 → vtx3）

    参数:
        entity: SOLID 类型的 ezdxf 实体

    返回:
        包含顶点列表的字典，顶点已按正确顺序排列
    """
    # 获取 3 或 4 个顶点
    vtx0 = entity.dxf.vtx0
    vtx1 = entity.dxf.vtx1
    vtx2 = entity.dxf.vtx2
    vtx3 = entity.dxf.get("vtx3", None)

    # SOLID 的顶点存储顺序是交叉的：0, 1, 3, 2
    # 需要重新排列为正确的多边形顺序
    if vtx3 is not None and (vtx3.x != vtx2.x or vtx3.y != vtx2.y):
        # 四边形：按 0→1→3→2→0 的顺序（SOLID 的特殊顶点顺序）
        vertices = [
            (vtx0.x, vtx0.y),
            (vtx1.x, vtx1.y),
            (vtx3.x, vtx3.y),
            (vtx2.x, vtx2.y),
        ]
    else:
        # 三角形：只有 3 个有效顶点
        vertices = [
            (vtx0.x, vtx0.y),
            (vtx1.x, vtx1.y),
            (vtx2.x, vtx2.y),
        ]

    return {
        "type": "SOLID",
        "vertices": vertices,
        "is_closed": True,  # SOLID 始终是闭合的填充区域
    }


def parse_3dface(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 3DFACE（三维面）实体的几何数据。

    3DFACE 由 3 或 4 个三维顶点定义一个平面面片。
    转换时忽略 Z 坐标，投影到 XY 平面生成 2D 多边形。

    参数:
        entity: 3DFACE 类型的 ezdxf 实体

    返回:
        包含顶点列表的字典（Z 坐标被忽略）
    """
    vtx0 = entity.dxf.vtx0
    vtx1 = entity.dxf.vtx1
    vtx2 = entity.dxf.vtx2
    vtx3 = entity.dxf.get("vtx3", None)

    # 收集顶点，投影到 XY 平面（忽略 Z 坐标）
    vertices = [
        (vtx0.x, vtx0.y),
        (vtx1.x, vtx1.y),
        (vtx2.x, vtx2.y),
    ]

    # 如果第 4 个顶点存在且与第 3 个不同，则是四边形
    if vtx3 is not None and (vtx3.x != vtx2.x or vtx3.y != vtx2.y):
        vertices.append((vtx3.x, vtx3.y))

    return {
        "type": "3DFACE",
        "vertices": vertices,
        "is_closed": True,  # 3DFACE 始终是闭合面
    }


def parse_insert(
    entity: DXFEntity,
    doc: ezdxf.document.Drawing,
) -> List[ParsedEntity]:
    """
    解析 INSERT（块引用）实体，使用 ezdxf 内置的 explode() 方法展开。

    块引用是 CAD 中复用图形的核心机制。INSERT 实体引用一个块定义（Block），
    块定义中可能包含任意类型的子实体，甚至可能嵌套其他 INSERT。

    使用 ezdxf 的 explode() 方法替代手写递归展开 + 坐标变换，优点：
    - ezdxf 内部自动处理坐标变换（平移、旋转、缩放、镜像）
    - 自动递归展开嵌套块引用
    - 正确处理非等比缩放下的圆弧、椭圆等复杂变换
    - 处理 OCS（对象坐标系）到 WCS（世界坐标系）的转换

    如果 explode() 失败（某些复杂块可能不支持），回退到 virtual_entities()。

    参数:
        entity:    INSERT 类型的 ezdxf 实体
        doc:       ezdxf 文档对象（用于查找块定义）

    返回:
        展开后的 ParsedEntity 列表
    """
    block_name = entity.dxf.name
    logger.debug(f"展开块引用: {block_name}")

    # 提取块引用的 ATTRIB 属性（显示在图中的属性文字）
    attrib_dict = {}
    if hasattr(entity, "attribs"):
        for attrib in entity.attribs:
            tag = attrib.dxf.tag
            value = attrib.dxf.text
            attrib_dict[tag] = value

    # 使用 explode() 将块引用炸开为独立实体
    # explode() 会自动处理坐标变换和嵌套展开
    exploded_entities = []
    try:
        exploded_entities = list(entity.explode())
    except Exception as e:
        logger.debug(f"块引用 '{block_name}' explode() 失败: {e}，尝试 virtual_entities()")
        # 回退方案：使用 virtual_entities() 获取虚拟实体
        # virtual_entities() 不会修改原始文档，更安全但可能不够完整
        try:
            exploded_entities = list(entity.virtual_entities())
        except Exception as e2:
            logger.warning(f"块引用 '{block_name}' 展开完全失败: {e2}")
            return []

    # 解析炸开后的每个子实体
    parsed_entities = []
    for sub_entity in exploded_entities:
        sub_type = sub_entity.dxftype()

        if sub_type == "INSERT":
            # 嵌套的块引用，递归展开
            sub_parsed = parse_insert(sub_entity, doc)
        else:
            # 普通实体，直接解析（坐标已经被 explode 变换到世界坐标系）
            result = parse_single_entity(sub_entity)
            sub_parsed = [result] if result else []

        # 记录块名和属性信息
        for parsed in sub_parsed:
            if not parsed.block_name:
                parsed.block_name = block_name
            if attrib_dict and not parsed.attributes:
                parsed.attributes = attrib_dict
            parsed_entities.append(parsed)

    return parsed_entities


# 不适合用 geo.proxy() 转换的实体类型（非几何数据）
_GEO_PROXY_SKIP_TYPES = frozenset({
    "LIGHT",       # 灯光信息，不含几何
    "OLE2FRAME",   # 嵌入的 OLE 对象（图片、Excel 等）
    "OLEFRAME",    # 旧版 OLE 对象
    "WIPEOUT",     # 遮罩对象
    "ACAD_PROXY_ENTITY",  # 代理实体（第三方应用创建）
})


def _fallback_geo_proxy(entity: DXFEntity, entity_type: str) -> Optional[ParsedEntity]:
    """
    使用 ezdxf.addons.geo.proxy() 作为 fallback 转换未知实体类型。

    geo.proxy() 是 ezdxf 内置的通用转换器，能将大部分 DXF 实体转为
    __geo_interface__ 兼容的几何数据（Point/LineString/Polygon 等）。
    适用于我们没有专门解析函数的实体类型。

    参数:
        entity:      ezdxf 实体对象
        entity_type: 实体类型名称

    返回:
        ParsedEntity 对象，如果转换失败则返回 None
    """
    # 跳过明确不含几何信息的实体类型
    if entity_type in _GEO_PROXY_SKIP_TYPES:
        logger.debug(f"跳过非几何实体类型: {entity_type}")
        return None

    try:
        # distance 参数控制曲线离散化精度（单位与 CAD 坐标单位一致）
        # force_line_string=True 强制弧线类实体输出为 LineString 而非曲线
        proxy = ezdxf_geo.proxy(entity, distance=0.1, force_line_string=True)
        geo_json = proxy.__geo_interface__

        geo_type = geo_json.get("type", "")
        coords = geo_json.get("coordinates", [])

        if not coords:
            logger.debug(f"geo.proxy() 转换 {entity_type} 结果为空")
            return None

        # 安全获取图层名
        try:
            entity_layer = entity.dxf.get("layer", "0")
        except Exception:
            entity_layer = "0"

        # 将 geo.proxy() 返回的 GeoJSON 几何数据封装为 ParsedEntity
        # geometry_data 中存储 geo.proxy() 直接输出的 GeoJSON 几何
        # 标记为 GEO_PROXY 类型，在 geometry_mapper 中直接透传
        logger.debug(f"geo.proxy() 成功转换 {entity_type} → {geo_type}")
        return ParsedEntity(
            entity_type=entity_type,
            layer=entity_layer,
            color=get_entity_color(entity),
            geometry_data={
                "type": "GEO_PROXY",
                "geo_type": geo_type,             # GeoJSON 几何类型
                "coordinates": coords,            # GeoJSON 坐标数据
                "original_type": entity_type,      # 原始 CAD 实体类型
            },
        )
    except Exception as e:
        # geo.proxy() 也无法处理，最终跳过
        try:
            skip_layer = entity.dxf.get('layer', '未知')
        except Exception:
            skip_layer = '未知'
        logger.debug(f"跳过不支持的实体类型: {entity_type}（图层: {skip_layer}，原因: {e}）")
        return None


def parse_single_entity(entity: DXFEntity) -> Optional[ParsedEntity]:
    """
    解析单个 CAD 实体，根据实体类型调用对应的解析函数。

    参数:
        entity: ezdxf 实体对象

    返回:
        ParsedEntity 对象，如果实体类型不支持则返回 None
    """
    entity_type = entity.dxftype()

    # DIMENSION 和 MULTILEADER 是复合实体，需要特殊处理（分解为基本图元）
    # 这里返回 None，在 parse_dxf() 主循环中单独处理
    # （因为它们会分解出多个子实体，无法用单个 ParsedEntity 表示）

    # 根据实体类型分派到对应的解析函数
    parser_map = {
        "LINE": parse_line,
        "LWPOLYLINE": parse_lwpolyline,
        "POLYLINE": parse_polyline,
        "CIRCLE": parse_circle,
        "ARC": parse_arc,
        "POINT": parse_point,
        "TEXT": parse_text,
        "MTEXT": parse_mtext,
        "ELLIPSE": parse_ellipse,
        "SPLINE": parse_spline,
        "HATCH": parse_hatch,
        "SOLID": parse_solid,
        "3DFACE": parse_3dface,
    }

    parse_func = parser_map.get(entity_type)
    if parse_func is None:
        # parser_map 中没有匹配的解析函数，尝试使用 ezdxf.addons.geo.proxy() 作为 fallback
        # geo.proxy() 是 ezdxf 内置的通用转换器，能自动将大部分 DXF 实体转为 GeoJSON 几何
        return _fallback_geo_proxy(entity, entity_type)

    try:
        geometry_data = parse_func(entity)
    except Exception as e:
        # 解析单个实体失败不应中断整个流程
        logger.warning(f"解析实体失败（类型: {entity_type}）: {e}")
        return None

    # 提取文字内容（TEXT 和 MTEXT 实体）
    text_content = ""
    if entity_type in ("TEXT", "MTEXT"):
        text_content = geometry_data.get("text", "")

    # 安全获取图层名称，兼容不支持 layer 属性的特殊实体
    try:
        entity_layer = entity.dxf.get("layer", "0")
    except Exception:
        entity_layer = "0"

    return ParsedEntity(
        entity_type=entity_type,
        layer=entity_layer,
        color=get_entity_color(entity),
        geometry_data=geometry_data,
        text_content=text_content,
    )


def parse_dxf(
    file_path: str,
    layers: List[str] = None,
    exclude_layers: List[str] = None,
    expand_blocks: bool = True,
) -> List[ParsedEntity]:
    """
    解析 DXF 文件，提取所有实体信息。

    这是本模块的主入口函数，负责：
    1. 读取 DXF 文件
    2. 遍历模型空间中的所有实体
    3. 根据图层过滤条件筛选实体
    4. 解析每个实体的几何和属性数据
    5. 展开块引用（如果启用）

    参数:
        file_path:      DXF 文件路径
        layers:         只解析指定图层的实体（为 None 则解析所有图层）
        exclude_layers: 排除指定图层的实体
        expand_blocks:  是否展开块引用（默认为 True）

    返回:
        ParsedEntity 对象列表
    """
    # 读取 DXF 文件
    doc = read_dxf_file(file_path)
    modelspace = doc.modelspace()

    # 统计信息
    total_count = 0       # 总实体数
    parsed_count = 0      # 成功解析的实体数
    skipped_count = 0     # 跳过的实体数（图层过滤或不支持的类型）
    parsed_entities = []  # 结果列表

    for entity in modelspace:
        total_count += 1

        # 安全获取图层名称
        # 某些特殊实体（如 PLANESURFACE）不支持 layer 属性，需要容错处理
        try:
            entity_layer = entity.dxf.get("layer", "0")
        except Exception:
            entity_layer = "0"

        # 图层过滤：如果指定了 layers 参数，只保留指定图层的实体
        if layers and entity_layer not in layers:
            skipped_count += 1
            continue

        # 图层排除：如果指定了 exclude_layers 参数，排除指定图层的实体
        if exclude_layers and entity_layer in exclude_layers:
            skipped_count += 1
            continue

        entity_type = entity.dxftype()

        # 处理块引用
        if entity_type == "INSERT" and expand_blocks:
            block_entities = parse_insert(entity, doc)
            parsed_entities.extend(block_entities)
            parsed_count += len(block_entities)
            continue

        # 处理复合实体：使用 virtual_entities() 分解为基本图元后逐个解析
        # DIMENSION: 标注（尺寸线 + 箭头 + 文字）
        # MULTILEADER: 多重引线
        # LEADER: 引线（引出线 + 箭头）
        # MLINE: 多线（平行线组）
        if entity_type in ("DIMENSION", "MULTILEADER", "LEADER", "MLINE"):
            try:
                sub_entities = list(entity.virtual_entities())
            except Exception as e:
                logger.debug(f"{entity_type} 分解失败: {e}")
                sub_entities = None

            if sub_entities:
                # 对分解出的每个子图元进行解析
                sub_count = 0
                for sub_entity in sub_entities:
                    sub_parsed = parse_single_entity(sub_entity)
                    if sub_parsed:
                        # 继承父实体的图层信息
                        sub_parsed.layer = entity_layer
                        parsed_entities.append(sub_parsed)
                        sub_count += 1
                parsed_count += sub_count
                logger.debug(f"{entity_type} 分解为 {sub_count} 个子实体")
            else:
                skipped_count += 1
            continue

        # 解析普通实体
        parsed = parse_single_entity(entity)
        if parsed:
            parsed_entities.append(parsed)
            parsed_count += 1
        else:
            skipped_count += 1

    # 输出统计信息
    logger.info(
        f"DXF 解析完成: 总实体 {total_count} 个, "
        f"成功解析 {parsed_count} 个, "
        f"跳过 {skipped_count} 个"
    )

    return parsed_entities
