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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

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


# ezdxf 原生支持的实体类型（有专门解析函数的）
_NATIVE_ENTITY_TYPES = frozenset({
    "LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "POINT",
    "TEXT", "MTEXT", "ATTDEF", "ELLIPSE", "SPLINE", "HATCH", "SOLID", "3DFACE",
    # 新增原生解析：位置类实体（退化为 Point 或 Polygon）
    "RAY", "XLINE", "TOLERANCE", "SHAPE", "ACAD_TABLE",
    "IMAGE", "HELIX", "MESH", "PDFUNDERLAY", "PDFREFERENCE",
})

# 复合实体类型（通过 virtual_entities() 分解处理）
_COMPOUND_ENTITY_TYPES = frozenset({
    "DIMENSION", "MULTILEADER", "LEADER", "MLINE",
    "ARC_DIMENSION",  # 弧长标注，与 DIMENSION 类似，使用 virtual_entities() 分解
})

# 需要 ACIS 几何内核的实体类型（当前无法转换，显式跳过以给出准确诊断原因）
_ACIS_ENTITY_TYPES = frozenset({
    "3DSOLID",         # 三维实体（ACIS SAT/SAB 格式）
    "REGION",          # 二维面域（ACIS SAT/SAB 格式）
    "EXTRUDEDSURFACE", # 拉伸曲面
    "LOFTEDSURFACE",   # 放样曲面
    "REVOLVEDSURFACE", # 旋转曲面
    "SWEPTSURFACE",    # 扫掠曲面
    "PLANESURFACE",    # 平面曲面
})


class EntityTypeStats:
    """
    实体类型统计信息收集器。

    跟踪每种 CAD 实体类型的解析成功/失败/跳过情况，
    用于生成转换诊断报告，帮助用户了解哪些实体类型没有被正确转换。

    统计维度：
        - 按实体类型: 每种类型的 总数/成功/失败
        - 按处理方式: 原生解析 / geo.proxy() fallback / 复合实体分解 / 块展开
        - 失败原因归类: proxy失败 / 几何为空 / 解析异常 / 图层过滤
    """

    def __init__(self):
        # 每种实体类型的计数: {type: {"total": n, "success": n, "failed": n}}
        self._type_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"total": 0, "success": 0, "failed": 0}
        )
        # 每种实体类型的处理方式: {type: "native"/"proxy"/"compound"/"block"/"skip"}
        self._type_method: Dict[str, str] = {}
        # 失败原因记录: {type: {reason: count}}
        self._fail_reasons: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 图层过滤跳过的实体数
        self.layer_filtered: int = 0

    def record_success(self, entity_type: str, method: str = "native"):
        """
        记录一个实体解析成功。

        参数:
            entity_type: CAD 实体类型名称
            method:      处理方式 (native/proxy/compound/block)
        """
        self._type_counts[entity_type]["total"] += 1
        self._type_counts[entity_type]["success"] += 1
        # 记录处理方式（取第一次出现的方式）
        if entity_type not in self._type_method:
            self._type_method[entity_type] = method

    def record_failure(self, entity_type: str, reason: str = "未知"):
        """
        记录一个实体解析失败。

        参数:
            entity_type: CAD 实体类型名称
            reason:      失败原因描述
        """
        self._type_counts[entity_type]["total"] += 1
        self._type_counts[entity_type]["failed"] += 1
        self._fail_reasons[entity_type][reason] += 1
        # 记录处理方式（根据实体所属的集合判断）
        if entity_type not in self._type_method:
            if entity_type in _NATIVE_ENTITY_TYPES:
                self._type_method[entity_type] = "native"
            elif entity_type in _COMPOUND_ENTITY_TYPES:
                self._type_method[entity_type] = "compound"
            elif entity_type in _GEO_PROXY_SKIP_TYPES:
                self._type_method[entity_type] = "skip"
            elif entity_type in _ACIS_ENTITY_TYPES:
                self._type_method[entity_type] = "acis"
            else:
                self._type_method[entity_type] = "proxy"

    def record_layer_filtered(self):
        """记录一个实体因图层过滤被跳过。"""
        self.layer_filtered += 1

    def get_method_label(self, entity_type: str) -> str:
        """
        获取实体类型的处理方式标签（中文）。

        参数:
            entity_type: CAD 实体类型名称

        返回:
            处理方式的中文标签
        """
        method = self._type_method.get(entity_type, "unknown")
        labels = {
            "native": "原生解析",
            "proxy": "proxy兜底",
            "compound": "分解子实体",
            "block": "块展开",
            "skip": "非几何跳过",
            "acis": "ACIS不支持",  # 需要 ACIS 几何内核（3DSOLID、REGION 等）
            "unknown": "未知",
        }
        return labels.get(method, method)

    def to_dict(self) -> Dict[str, Any]:
        """
        将统计信息导出为字典，方便序列化（Web API 返回 / JSON 输出）。

        返回:
            包含完整统计信息的字典
        """
        total_all = sum(c["total"] for c in self._type_counts.values())
        success_all = sum(c["success"] for c in self._type_counts.values())
        failed_all = sum(c["failed"] for c in self._type_counts.values())

        # 按总数降序排列实体类型
        sorted_types = sorted(
            self._type_counts.items(),
            key=lambda x: x[1]["total"],
            reverse=True,
        )

        entity_details = []
        for etype, counts in sorted_types:
            rate = (counts["success"] / counts["total"] * 100) if counts["total"] > 0 else 0
            detail = {
                "entity_type": etype,
                "total": counts["total"],
                "success": counts["success"],
                "failed": counts["failed"],
                "success_rate": round(rate, 1),
                "method": self.get_method_label(etype),
            }
            # 如果有失败，附上失败原因
            if counts["failed"] > 0 and etype in self._fail_reasons:
                detail["fail_reasons"] = dict(self._fail_reasons[etype])
            entity_details.append(detail)

        return {
            "total": total_all,
            "success": success_all,
            "failed": failed_all,
            "success_rate": round(success_all / total_all * 100, 1) if total_all > 0 else 0,
            "layer_filtered": self.layer_filtered,
            "entity_details": entity_details,
        }

    def format_report(self) -> str:
        """
        生成格式化的诊断报告文本（用于 CLI 输出）。

        返回:
            带表格的诊断报告字符串
        """
        data = self.to_dict()
        lines = []
        lines.append("")
        lines.append("=" * 78)
        lines.append("  转换诊断报告")
        lines.append("=" * 78)

        # 表头
        header = f"  {'实体类型':<20s} {'总数':>6s} {'成功':>6s} {'失败':>6s} {'成功率':>8s}  {'处理方式':<12s}"
        lines.append(header)
        lines.append("  " + "-" * 74)

        # 按成功率升序排列（失败多的排前面，更醒目）
        details = data["entity_details"]
        details_sorted = sorted(details, key=lambda x: (x["success_rate"], -x["total"]))

        for d in details_sorted:
            # 成功率低于 100% 的用标记醒目提示
            rate_str = f"{d['success_rate']:>6.1f}%"
            if d["success_rate"] < 100 and d["failed"] > 0:
                rate_str = f"{d['success_rate']:>6.1f}% !"
            line = f"  {d['entity_type']:<20s} {d['total']:>6d} {d['success']:>6d} {d['failed']:>6d} {rate_str:>8s}  {d['method']:<12s}"
            lines.append(line)

            # 如果有失败原因，缩进显示
            if "fail_reasons" in d:
                for reason, count in d["fail_reasons"].items():
                    lines.append(f"    └─ {reason} ({count}个)")

        lines.append("  " + "-" * 74)

        # 汇总行
        total_rate = f"{data['success_rate']:.1f}%"
        lines.append(
            f"  {'合计':<20s} {data['total']:>6d} {data['success']:>6d} "
            f"{data['failed']:>6d} {total_rate:>8s}"
        )

        if data["layer_filtered"] > 0:
            lines.append(f"  另有 {data['layer_filtered']} 个实体因图层过滤被跳过")

        lines.append("=" * 78)
        lines.append("")

        return "\n".join(lines)


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

    参数:
        entity: TEXT 类型的 ezdxf 实体

    返回:
        包含插入点坐标和文字内容的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "TEXT",
        "insert": (insert.x, insert.y),  # 文字插入点坐标
        "text": entity.dxf.text,          # 文字内容
        "height": entity.dxf.height,      # 文字高度
        "rotation": entity.dxf.get("rotation", 0.0),  # 旋转角度（度）
    }


def parse_attdef(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 ATTDEF（块属性定义）实体的几何数据和文本内容。

    ATTDEF 是块定义中的属性模板，定义了块引用时可以填写的属性字段。
    它有插入点、默认文本值、标签名(tag)和提示文字(prompt)。
    在几何上与 TEXT 实体类似，作为 Point 处理。

    参数:
        entity: ATTDEF 类型的 ezdxf 实体

    返回:
        包含插入点坐标和属性信息的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "ATTDEF",
        "insert": (insert.x, insert.y),            # 属性插入点坐标
        "text": entity.dxf.get("text", ""),         # 默认文本值
        "tag": entity.dxf.get("tag", ""),           # 属性标签名（如 "编号"、"名称"）
        "prompt": entity.dxf.get("prompt", ""),     # 属性输入提示文字
        "height": entity.dxf.get("height", 2.5),    # 文字高度
        "rotation": entity.dxf.get("rotation", 0.0), # 旋转角度（度）
    }


def parse_mtext(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 MTEXT（多行文字）实体的几何数据和文本内容。

    MTEXT 支持多行文本、格式化等功能。

    参数:
        entity: MTEXT 类型的 ezdxf 实体

    返回:
        包含插入点坐标和文字内容的字典
    """
    insert = entity.dxf.insert
    # MTEXT 的文本内容可能包含格式化标记，使用 plain_text() 获取纯文本
    plain_text = entity.plain_text()
    return {
        "type": "MTEXT",
        "insert": (insert.x, insert.y),  # 文字插入点坐标
        "text": plain_text,               # 纯文本内容（去除格式化标记）
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


def parse_ray(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 RAY（半无限射线）实体的几何数据。

    RAY 由一个起点和方向向量定义，向一个方向延伸至无穷远。
    GeoJSON 中无法表示无限长线段，故退化为起点的 Point。

    参数:
        entity: RAY 类型的 ezdxf 实体

    返回:
        包含起点坐标的字典
    """
    start = entity.dxf.start
    return {
        "type": "RAY",
        "start": (start.x, start.y),  # 射线起点（投影到 XY 平面）
    }


def parse_xline(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 XLINE（无限构造线）实体的几何数据。

    XLINE 是双向无限延伸的构造辅助线，由基点和方向向量定义。
    GeoJSON 无法表示无限线，故退化为基点的 Point。

    注意：ezdxf 中 XLINE 的基点属性名为 "start"（对应 DXF group code 10），
    与 RAY 实体相同，不要与 DXF 规范文档中的 "point" 混淆。

    参数:
        entity: XLINE 类型的 ezdxf 实体

    返回:
        包含基点坐标的字典
    """
    # ezdxf 中 XLINE 的基点属性名是 start（DXF group code 10）
    start = entity.dxf.start
    return {
        "type": "XLINE",
        "point": (start.x, start.y),  # 构造线基点（投影到 XY 平面）
    }


def parse_tolerance(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 TOLERANCE（形位公差框）实体的几何数据。

    TOLERANCE 是 GD&T（几何尺寸和公差）标注框，含有插入点。
    几何上表示为插入点的 Point。

    参数:
        entity: TOLERANCE 类型的 ezdxf 实体

    返回:
        包含插入点坐标的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "TOLERANCE",
        "insert": (insert.x, insert.y),  # 公差框的插入点
    }


def parse_shape(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 SHAPE（形状/符号字体）实体的几何数据。

    SHAPE 是基于形状文件（.shx）定义的符号，类似于单字符的 TEXT。
    几何上表示为插入点的 Point。

    参数:
        entity: SHAPE 类型的 ezdxf 实体

    返回:
        包含插入点坐标的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "SHAPE",
        "insert": (insert.x, insert.y),  # 形状的插入点
    }


def parse_acad_table(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 ACAD_TABLE（AutoCAD 表格）实体的几何数据。

    ACAD_TABLE 是 AutoCAD 的表格对象，含有插入点和尺寸信息。
    几何上表示为插入点的 Point（表格左上角）。

    参数:
        entity: ACAD_TABLE 类型的 ezdxf 实体

    返回:
        包含插入点坐标的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "ACAD_TABLE",
        "insert": (insert.x, insert.y),  # 表格插入点（左上角）
    }


def parse_image(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 IMAGE（光栅图像引用）实体的几何数据。

    IMAGE 实体引用外部光栅图像文件，由插入点和像素尺寸向量定义其在图中的位置和大小。
    几何上表示为图像边界的 Polygon（四边形）。

    图像边界的四个角点计算：
        P0 = insert（左下角）
        P1 = insert + u_pixel * 图像宽度（右下角）
        P2 = insert + u_pixel * 宽度 + v_pixel * 高度（右上角）
        P3 = insert + v_pixel * 高度（左上角）

    参数:
        entity: IMAGE 类型的 ezdxf 实体

    返回:
        包含插入点和边界多边形的字典
    """
    insert = entity.dxf.insert
    try:
        # u_pixel/v_pixel 是每个像素在 CAD 坐标系中的大小向量
        u_pixel = entity.dxf.u_pixel
        v_pixel = entity.dxf.v_pixel
        # image_size 返回 (宽度像素数, 高度像素数) 的元组
        width, height = entity.image_size
        # 计算图像四个角点坐标（顺时针方向）
        p0 = (insert.x, insert.y)
        p1 = (insert.x + u_pixel.x * width, insert.y + u_pixel.y * width)
        p2 = (
            insert.x + u_pixel.x * width + v_pixel.x * height,
            insert.y + u_pixel.y * width + v_pixel.y * height,
        )
        p3 = (insert.x + v_pixel.x * height, insert.y + v_pixel.y * height)
        # 闭合多边形（首尾重合）
        boundary = [p0, p1, p2, p3, p0]
    except Exception as e:
        # 无法计算边界时退化为仅保留插入点
        logger.debug(f"IMAGE 边界计算失败，退化为插入点: {e}")
        boundary = []

    return {
        "type": "IMAGE",
        "insert": (insert.x, insert.y),  # 图像左下角坐标
        "boundary": boundary,             # 图像边界多边形（空时退化为 Point）
    }


def parse_helix(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 HELIX（螺旋线）实体的几何数据。

    HELIX 是绕轴旋转的三维螺旋曲线，由轴基点、半径和圈数定义。
    投影到 XY 平面后为圆形，几何上表示为圆（Polygon）。

    参数:
        entity: HELIX 类型的 ezdxf 实体

    返回:
        包含圆心和半径的字典（XY 平面投影）
    """
    base = entity.dxf.axis_base_point
    try:
        # 优先使用 DXF 属性中的 radius 字段
        radius = entity.dxf.radius
    except AttributeError:
        # 无 radius 属性时，用起始点到轴基点的水平距离估算
        try:
            start = entity.dxf.start_point
            radius = math.sqrt(
                (start.x - base.x) ** 2 + (start.y - base.y) ** 2
            )
        except AttributeError:
            radius = 1.0  # 兜底：使用单位半径

    return {
        "type": "HELIX",
        "center": (base.x, base.y),  # 轴基点（投影到 XY 平面作为圆心）
        "radius": max(radius, 1e-6),  # 螺旋半径（确保非零）
    }


def parse_underlay(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 PDFUNDERLAY/PDFREFERENCE（PDF/DWF 参考底图）实体的几何数据。

    底图实体引用外部 PDF/DWF 文件，将其叠加显示在 CAD 图中。
    几何上表示为插入点的 Point。

    参数:
        entity: PDFUNDERLAY 或 PDFREFERENCE 类型的 ezdxf 实体

    返回:
        包含插入点坐标的字典
    """
    insert = entity.dxf.insert
    return {
        "type": "UNDERLAY",
        "insert": (insert.x, insert.y),  # 底图插入点（左下角）
    }


def parse_mesh(entity: DXFEntity) -> Dict[str, Any]:
    """
    解析 MESH（多边形网格）实体的几何数据。

    MESH 是自由形式的 3D 网格，由顶点列表和面（顶点索引序列）定义。
    将所有顶点投影到 XY 平面，每个面转为一个多边形。

    参数:
        entity: MESH 类型的 ezdxf 实体

    返回:
        包含顶点列表和面列表的字典（Z 坐标被丢弃）
    """
    # 将所有顶点投影到 XY 平面（忽略 Z 坐标）
    vertices = [(v.x, v.y) for v in entity.vertices]

    # 遍历所有面，每个面是顶点索引的序列
    faces = []
    for face_indices in entity.faces:
        # 根据索引取出对应顶点坐标
        face_verts = [vertices[i] for i in face_indices if i < len(vertices)]
        if len(face_verts) >= 3:
            faces.append(face_verts)

    return {
        "type": "MESH",
        "vertices": vertices,  # 所有顶点的 XY 坐标
        "faces": faces,        # 每个面的顶点坐标列表
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

    # 跳过需要 ACIS 几何内核的实体类型（无法在 ezdxf 中直接提取几何）
    if entity_type in _ACIS_ENTITY_TYPES:
        logger.debug(f"跳过 ACIS 实体类型: {entity_type}（需要 ACIS 几何内核，暂不支持）")
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
        # 基本图元
        "LINE": parse_line,
        "LWPOLYLINE": parse_lwpolyline,
        "POLYLINE": parse_polyline,
        "CIRCLE": parse_circle,
        "ARC": parse_arc,
        "POINT": parse_point,
        "TEXT": parse_text,
        "MTEXT": parse_mtext,
        "ATTDEF": parse_attdef,
        "ELLIPSE": parse_ellipse,
        "SPLINE": parse_spline,
        "HATCH": parse_hatch,
        "SOLID": parse_solid,
        "3DFACE": parse_3dface,
        # 新增：构造线/射线（退化为 Point）
        "RAY": parse_ray,
        "XLINE": parse_xline,
        # 新增：标注/符号类实体（退化为 Point）
        "TOLERANCE": parse_tolerance,
        "SHAPE": parse_shape,
        "ACAD_TABLE": parse_acad_table,
        # 新增：参考/引用类实体
        "IMAGE": parse_image,            # 光栅图像边界 → Polygon
        "PDFUNDERLAY": parse_underlay,   # PDF 参考底图 → Point
        "PDFREFERENCE": parse_underlay,  # PDF 引用 → Point
        # 新增：几何体类实体
        "HELIX": parse_helix,            # 螺旋线 → 圆 Polygon（XY 平面投影）
        "MESH": parse_mesh,              # 多边形网格 → MultiPolygon（XY 平面投影）
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

    # 提取文字内容（TEXT、MTEXT、ATTDEF 实体）
    text_content = ""
    if entity_type in ("TEXT", "MTEXT", "ATTDEF"):
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
) -> Tuple[List[ParsedEntity], EntityTypeStats]:
    """
    解析 DXF 文件，提取所有实体信息。

    这是本模块的主入口函数，负责：
    1. 读取 DXF 文件
    2. 遍历模型空间中的所有实体
    3. 根据图层过滤条件筛选实体
    4. 解析每个实体的几何和属性数据
    5. 展开块引用（如果启用）
    6. 收集每种实体类型的解析统计信息

    参数:
        file_path:      DXF 文件路径
        layers:         只解析指定图层的实体（为 None 则解析所有图层）
        exclude_layers: 排除指定图层的实体
        expand_blocks:  是否展开块引用（默认为 True）

    返回:
        元组 (ParsedEntity 列表, EntityTypeStats 统计信息)
    """
    # 读取 DXF 文件
    doc = read_dxf_file(file_path)
    modelspace = doc.modelspace()

    # 实体类型统计收集器
    stats = EntityTypeStats()
    parsed_entities = []  # 结果列表

    for entity in modelspace:
        # 安全获取图层名称
        # 某些特殊实体（如 PLANESURFACE）不支持 layer 属性，需要容错处理
        try:
            entity_layer = entity.dxf.get("layer", "0")
        except Exception:
            entity_layer = "0"

        # 图层过滤：如果指定了 layers 参数，只保留指定图层的实体
        if layers and entity_layer not in layers:
            stats.record_layer_filtered()
            continue

        # 图层排除：如果指定了 exclude_layers 参数，排除指定图层的实体
        if exclude_layers and entity_layer in exclude_layers:
            stats.record_layer_filtered()
            continue

        entity_type = entity.dxftype()

        # 处理块引用（INSERT）
        if entity_type == "INSERT" and expand_blocks:
            block_entities = parse_insert(entity, doc)
            if block_entities:
                parsed_entities.extend(block_entities)
                stats.record_success("INSERT", method="block")
                # 统计块展开后的子实体类型
                for be in block_entities:
                    stats.record_success(f"  (块内){be.entity_type}", method="block")
            else:
                stats.record_failure("INSERT", reason="块展开结果为空")
            continue

        # 处理复合实体：使用 virtual_entities() 分解为基本图元后逐个解析
        # DIMENSION: 标注（尺寸线 + 箭头 + 文字）
        # MULTILEADER: 多重引线
        # LEADER: 引线（引出线 + 箭头）
        # MLINE: 多线（平行线组）
        if entity_type in _COMPOUND_ENTITY_TYPES:
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
                if sub_count > 0:
                    stats.record_success(entity_type, method="compound")
                else:
                    stats.record_failure(entity_type, reason="分解后子实体全部解析失败")
                logger.debug(f"{entity_type} 分解为 {sub_count} 个子实体")
            else:
                stats.record_failure(entity_type, reason="virtual_entities()分解失败")
            continue

        # 解析普通实体
        parsed = parse_single_entity(entity)
        if parsed:
            parsed_entities.append(parsed)
            # 判断处理方式：如果 geometry_data 里有 GEO_PROXY 标记，说明是 fallback
            if parsed.geometry_data.get("type") == "GEO_PROXY":
                stats.record_success(entity_type, method="proxy")
            else:
                stats.record_success(entity_type, method="native")
        else:
            # 根据实体类型判断失败原因，给出更精确的诊断信息
            if entity_type in _GEO_PROXY_SKIP_TYPES:
                stats.record_failure(entity_type, reason="非几何实体，主动跳过")
            elif entity_type in _ACIS_ENTITY_TYPES:
                # ACIS 实体需要专用几何内核（Open Design Alliance ACIS），ezdxf 不支持
                stats.record_failure(entity_type, reason="需要ACIS几何内核，暂不支持")
            elif entity_type in _NATIVE_ENTITY_TYPES:
                stats.record_failure(entity_type, reason="解析异常或几何为空")
            else:
                stats.record_failure(entity_type, reason="geo.proxy()转换失败")

    # 输出统计摘要
    stats_data = stats.to_dict()
    logger.info(
        f"DXF 解析完成: 总实体 {stats_data['total']} 个, "
        f"成功解析 {stats_data['success']} 个, "
        f"跳过 {stats_data['failed']} 个"
    )

    return parsed_entities, stats
