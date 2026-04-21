# -*- coding: utf-8 -*-
"""
GeoJSON → DXF/DWG 反向导出模块

将 GeoJSON FeatureCollection 导出为 DXF 文件，可选进一步转换为 DWG。
支持所有标准 GeoJSON 几何类型，按 properties.layer 属性分图层写入。

GeoJSON 几何类型 → DXF 实体映射：
    Point             → POINT
    LineString        → LWPOLYLINE（开放）
    Polygon           → LWPOLYLINE（闭合）× N（外环 + 各内环）
    MultiPoint        → 多个 POINT
    MultiLineString   → 多个 LWPOLYLINE（开放）
    MultiPolygon      → 多个 LWPOLYLINE（闭合）
    GeometryCollection → 递归处理子几何对象

使用方式：
    from src.geojson_to_dxf import ExportConfig, export_geojson_to_dxf

    config = ExportConfig(
        input_file="output/test.geojson",
        output_file="output/test.dxf",
    )
    result_path = export_geojson_to_dxf(geojson_data, config)
"""

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import ezdxf
from ezdxf.document import Drawing

from .dwg_to_dxf import find_oda_converter

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


@dataclass
class ExportConfig:
    """GeoJSON → DXF/DWG 导出配置"""

    input_file: str
    """输入 GeoJSON 文件路径（用于自动生成输出文件名）"""

    output_file: Optional[str] = None
    """输出文件路径（DXF 或 DWG）；None 时自动生成同目录同名文件"""

    target_crs: Optional[str] = None
    """目标坐标系 EPSG 编码（WGS84→工程坐标反向转换），如 'EPSG:4526'；
    None 则不进行坐标转换，直接使用 GeoJSON 原始坐标"""

    format: str = "dxf"
    """输出格式：'dxf'（默认）或 'dwg'（需要 ODA File Converter）"""

    default_layer: str = "0"
    """Feature 无 layer 属性时使用的默认图层名"""

    oda_path: Optional[str] = None
    """ODA File Converter 可执行文件路径；None 时自动查找"""


def export_geojson_to_dxf(geojson_data: dict, config: ExportConfig) -> str:
    """
    主转换函数：GeoJSON FeatureCollection → DXF/DWG 文件。

    流程：
        1. 验证 GeoJSON 格式，统一为 Feature 列表
        2. 创建新 DXF 文档（R2010 版本）
        3. 遍历所有 Feature，按几何类型调用对应写入函数
        4. 按 properties.layer 属性分图层
        5. 若指定 target_crs，对每个坐标执行 WGS84→目标CRS 反向转换
        6. 保存 DXF；若 format='dwg'，再调用 ODA File Converter 转为 DWG

    参数:
        geojson_data: GeoJSON FeatureCollection 或 Feature 字典
        config:       导出配置（ExportConfig）

    返回:
        输出文件的绝对路径字符串（DXF 或 DWG）

    异常:
        ValueError: GeoJSON 格式不正确，或目标坐标系无效
        RuntimeError: DWG 转换失败或超时
    """
    # ── 验证输入格式 ──────────────────────────────────────────────────────────
    if not isinstance(geojson_data, dict):
        raise ValueError("geojson_data 必须是字典类型")

    geojson_type = geojson_data.get("type")
    if geojson_type not in ("FeatureCollection", "Feature"):
        raise ValueError(
            f"不支持的 GeoJSON 类型: {geojson_type!r}，"
            f"需要 FeatureCollection 或 Feature"
        )

    # 统一为 Feature 列表
    if geojson_type == "FeatureCollection":
        features = geojson_data.get("features") or []
    else:
        features = [geojson_data]

    logger.info(f"开始导出 {len(features)} 个 Feature → {config.format.upper()}")

    # ── 确定输出路径 ───────────────────────────────────────────────────────────
    output_path = _resolve_output_path(config)

    # ── 创建坐标反向转换器（WGS84 → 目标 CRS） ─────────────────────────────
    transformer = None
    if config.target_crs:
        transformer = _create_reverse_transformer(config.target_crs)

    # ── 确定 DXF 写入路径（DWG 时先写临时 DXF）──────────────────────────────
    is_dwg = config.format.lower() == "dwg"
    if is_dwg:
        # 在系统临时目录生成临时 DXF，避免路径冲突
        tmp_dxf_name = f"cad2geojson_{uuid.uuid4().hex[:8]}.dxf"
        dxf_write_path = Path(tempfile.gettempdir()) / tmp_dxf_name
    else:
        dxf_write_path = output_path

    # ── 创建 DXF 文档并写入 Feature ────────────────────────────────────────
    doc = _create_dxf_doc()
    msp = doc.modelspace()

    entity_count = 0  # 统计写入的实体总数

    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            logger.warning(f"跳过非 Feature 对象: {type(feature).__name__}")
            continue

        # 从 properties.layer 读取图层名，缺失时使用默认图层
        props = feature.get("properties") or {}
        layer_name = str(props.get("layer") or config.default_layer).strip()
        if not layer_name:
            layer_name = config.default_layer

        # 确保图层在 DXF 文档中存在
        _ensure_layer(doc, layer_name)

        # 读取几何体
        geometry = feature.get("geometry")
        if geometry is None:
            logger.debug(f"Feature 无几何体，跳过（layer={layer_name}）")
            continue

        # 写入几何体到 modelspace（传入 props 以支持文字实体还原）
        count = _write_feature(msp, geometry, transformer, layer_name, props)
        entity_count += count

    # ── 保存 DXF 文件 ─────────────────────────────────────────────────────
    dxf_write_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dxf_write_path))
    logger.info(f"DXF 已写入: {dxf_write_path}，共 {entity_count} 个实体")

    # ── DWG 转换（如需） ──────────────────────────────────────────────────
    if is_dwg:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _convert_dxf_to_dwg(dxf_write_path, output_path, config.oda_path)
        finally:
            # 无论成功与否，删除临时 DXF 文件
            try:
                dxf_write_path.unlink(missing_ok=True)
                logger.debug(f"已删除临时 DXF: {dxf_write_path}")
            except OSError as e:
                logger.warning(f"删除临时 DXF 失败: {e}")

    return str(output_path)


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────


def _resolve_output_path(config: ExportConfig) -> Path:
    """
    根据配置确定输出文件的绝对路径。

    若用户指定了 output_file，直接使用；否则根据 input_file 同目录同名生成。

    参数:
        config: 导出配置

    返回:
        输出文件路径（Path 对象，已 resolve()）
    """
    if config.output_file:
        return Path(config.output_file).resolve()

    # 自动生成：输入文件同目录同名，换后缀
    ext = ".dwg" if config.format.lower() == "dwg" else ".dxf"
    return Path(config.input_file).with_suffix(ext).resolve()


def _create_dxf_doc() -> Drawing:
    """
    创建新的 DXF 文档。

    使用 R2010 版本（ACAD2010），兼容性好，支持所有常见 DXF 实体类型。
    设置公制单位（$MEASUREMENT=1）。

    返回:
        ezdxf.document.Drawing 对象
    """
    doc = ezdxf.new(dxfversion="R2010")
    # 设置测量单位为公制（0=英制，1=公制）
    doc.header["$MEASUREMENT"] = 1
    # 创建支持中文字符的文字样式，优先使用宋体 TTF 字体
    # R2010 版本支持 TrueType 字体，宋体（simsun.ttf）是 Windows 内置中文字体
    for font_name in ("simsun.ttf", "msyh.ttf", "simhei.ttf"):
        try:
            doc.styles.add("CHINESE", font=font_name)
            logger.debug(f"已创建中文文字样式 CHINESE，字体: {font_name}")
            break
        except Exception:
            continue
    return doc


def _ensure_layer(doc: Drawing, layer_name: str, color: int = 7) -> None:
    """
    确保指定图层存在，不存在则创建。

    DXF 中写入实体前必须先确保图层已定义，否则 ezdxf 会报错或写入到默认图层。

    参数:
        doc:        DXF 文档对象
        layer_name: 图层名称
        color:      图层颜色（ACI 颜色码，7=白色/黑色默认）
    """
    if layer_name not in doc.layers:
        doc.layers.add(name=layer_name, color=color)
        logger.debug(f"创建新图层: {layer_name!r}")


def _create_reverse_transformer(target_crs: str):
    """
    创建 WGS84 → 目标坐标系的反向坐标转换器。

    GeoJSON 标准使用 WGS84（EPSG:4326），而工程图纸通常使用本地坐标系
    （如 CGCS2000 高斯-克吕格投影）。此函数建立反向转换器。

    参数:
        target_crs: 目标坐标系 EPSG 编码（如 'EPSG:4526'）

    返回:
        pyproj.Transformer 对象

    异常:
        ValueError: 无效的 EPSG 编码
    """
    from pyproj import Transformer
    from pyproj.exceptions import CRSError

    try:
        transformer = Transformer.from_crs(
            "EPSG:4326",  # GeoJSON 标准坐标系（经纬度，WGS84）
            target_crs,   # 目标工程坐标系
            always_xy=True,  # 统一 (x/经度, y/纬度) 顺序，避免轴序歧义
        )
        logger.info(f"已创建反向坐标转换器: EPSG:4326 → {target_crs}")
        return transformer
    except CRSError as e:
        raise ValueError(f"无效的目标坐标系 '{target_crs}': {e}")


def _transform_coord(coord: list, transformer) -> Tuple[float, float]:
    """
    对单个 GeoJSON 坐标点执行坐标系转换。

    GeoJSON 坐标格式为 [longitude, latitude]（或 [longitude, latitude, z]），
    始终以 (x=lon, y=lat) 顺序存储。

    参数:
        coord:       GeoJSON 坐标点，格式 [lon, lat] 或 [lon, lat, z]
        transformer: pyproj 转换器；None 则原样返回 (x, y)

    返回:
        转换后的坐标 (x, y) 元组
    """
    x, y = float(coord[0]), float(coord[1])
    if transformer is None:
        return (x, y)
    tx, ty = transformer.transform(x, y)
    return (tx, ty)


def _transform_coords(coords: list, transformer) -> List[Tuple[float, float]]:
    """
    批量转换 GeoJSON 坐标点列表。

    参数:
        coords:      GeoJSON 坐标列表，格式 [[lon, lat], ...]
        transformer: pyproj 转换器；None 则原样返回

    返回:
        转换后的坐标列表，格式 [(x, y), ...]
    """
    return [_transform_coord(c, transformer) for c in coords]


def _write_feature(
    msp, geometry: dict, transformer, layer: str, props: dict = None
) -> int:
    """
    将单个 GeoJSON 几何对象写入 DXF modelspace。

    根据 geometry.type 分发到对应的写入函数，不支持的类型会记录警告并跳过。
    当 props.entity_type 为 TEXT/MTEXT 且 props.text 存在时，Point 几何会被写入
    为 TEXT 实体而非 POINT 实体，从而保留原始文字内容（包括中文）。

    参数:
        msp:         ezdxf Modelspace 对象
        geometry:    GeoJSON 几何对象字典（含 type 和 coordinates/geometries）
        transformer: pyproj 坐标转换器（可为 None）
        layer:       目标图层名称
        props:       Feature 的 properties 字典（可为 None）

    返回:
        成功写入的 DXF 实体数量
    """
    if geometry is None:
        return 0

    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    props = props or {}

    if geom_type is None:
        logger.warning("几何对象缺少 type 字段，跳过")
        return 0

    try:
        if geom_type == "Point":
            if coords is None:
                return 0
            # 检查是否为文字实体（entity_type 为 TEXT 或 MTEXT 且有文字内容）
            entity_type = props.get("entity_type", "")
            text_content = props.get("text", "")
            if entity_type in ("TEXT", "MTEXT") and text_content:
                # 写入 TEXT 实体，保留文字内容（含中文）、旋转角和对齐方式
                _write_text(
                    msp,
                    coords,
                    text_content,
                    layer,
                    transformer,
                    height=props.get("text_height", 2.5),
                    rotation=props.get("text_rotation", 0.0),
                    halign=props.get("text_halign", 0),
                    valign=props.get("text_valign", 0),
                )
            else:
                # 普通点 → POINT 实体
                _write_point(msp, coords, layer, transformer)
            return 1

        elif geom_type == "LineString":
            # 折线 → 开放 LWPOLYLINE
            if coords is None:
                return 0
            _write_linestring(msp, coords, layer, transformer, closed=False)
            return 1

        elif geom_type == "Polygon":
            # 多边形 → 多个闭合 LWPOLYLINE（外环 + 各内环）
            if coords is None:
                return 0
            return _write_polygon(msp, coords, layer, transformer)

        elif geom_type == "MultiPoint":
            # 多点 → 多个 POINT
            if coords is None:
                return 0
            for pt_coords in coords:
                _write_point(msp, pt_coords, layer, transformer)
            return len(coords)

        elif geom_type == "MultiLineString":
            # 多条折线 → 多个开放 LWPOLYLINE
            if coords is None:
                return 0
            for line_coords in coords:
                _write_linestring(msp, line_coords, layer, transformer, closed=False)
            return len(coords)

        elif geom_type == "MultiPolygon":
            # 多个多边形 → 多组闭合 LWPOLYLINE
            if coords is None:
                return 0
            count = 0
            for poly_rings in coords:
                count += _write_polygon(msp, poly_rings, layer, transformer)
            return count

        elif geom_type == "GeometryCollection":
            # 几何集合 → 递归处理每个子几何对象
            count = 0
            for sub_geom in geometry.get("geometries") or []:
                count += _write_feature(msp, sub_geom, transformer, layer)
            return count

        else:
            logger.warning(f"不支持的几何类型: {geom_type!r}，跳过")
            return 0

    except Exception as e:
        logger.warning(f"写入几何对象失败（type={geom_type}，layer={layer}）: {e}")
        return 0


def _write_point(msp, coords: list, layer: str, transformer) -> None:
    """
    将 GeoJSON Point 坐标写入 DXF POINT 实体。

    POINT 实体使用三维坐标，Z 值取 GeoJSON 坐标的第三维（无则为 0）。

    参数:
        msp:         ezdxf Modelspace
        coords:      GeoJSON Point 坐标，[lon, lat] 或 [lon, lat, z]
        layer:       目标图层名
        transformer: 坐标转换器（可为 None）
    """
    x, y = _transform_coord(coords, transformer)
    # 取 Z 值（GeoJSON 第三维），无则为 0
    z = float(coords[2]) if len(coords) > 2 else 0.0
    msp.add_point((x, y, z), dxfattribs={"layer": layer})


def _write_text(
    msp,
    coords: list,
    text_content: str,
    layer: str,
    transformer,
    height: float = 2.5,
    rotation: float = 0.0,
    halign: int = 0,
    valign: int = 0,
) -> None:
    """
    将文字内容写入 DXF TEXT 实体，支持中文字符。

    优先使用 CHINESE 文字样式（宋体 TTF），该样式在 _create_dxf_doc() 中创建。
    若样式不存在，则使用 STANDARD 样式（可能无法正确显示中文）。

    对齐还原逻辑：
    - GeoJSON 中存储的 Point 坐标（由 dxf_parser.parse_text 决定）：
        * 左对齐（halign=0, valign=0）：等于 DXF insert 点
        * 其他对齐：等于 DXF align_point 点（真实定位锚点）
    - 写入时：将该坐标同时赋给 insert 和 align_point，并设置 halign/valign
      ezdxf 对非左对齐 TEXT 必须同时设置 align_point，否则定位不生效

    参数:
        msp:          ezdxf Modelspace
        coords:       GeoJSON Point 坐标，[lon, lat] 或 [lon, lat, z]
        text_content: 文字内容（支持中文）
        layer:        目标图层名
        transformer:  坐标转换器（可为 None）
        height:       文字高度（默认 2.5）
        rotation:     旋转角度，单位为度（默认 0）；竖向文字通常为 90° 或 270°
        halign:       水平对齐（0=左, 1=居中, 2=右, 3=两端对齐, 4=正中, 5=适合）
        valign:       垂直对齐（0=基线, 1=下, 2=中, 3=上）
    """
    x, y = _transform_coord(coords, transformer)
    z = float(coords[2]) if len(coords) > 2 else 0.0

    # 文字高度不合法时使用默认值
    text_height = height if (height and height > 0) else 2.5

    dxfattribs = {
        "layer": layer,
        "insert": (x, y, z),
        "height": text_height,
        "rotation": rotation or 0.0,
        "halign": halign,
        "valign": valign,
    }

    # 优先使用支持中文的 CHINESE 样式
    try:
        if "CHINESE" in msp.doc.styles:
            dxfattribs["style"] = "CHINESE"
    except Exception:
        pass

    text_entity = msp.add_text(text_content, dxfattribs=dxfattribs)

    # 非左/基线对齐时，必须显式设置 align_point
    # ezdxf 要求：凡 halign != 0 或 valign != 0，都需要 align_point 才能正确定位
    # GeoJSON 中存储的坐标已经是 align_point，直接赋回即可
    if halign != 0 or valign != 0:
        text_entity.dxf.align_point = (x, y, z)


def _write_linestring(
    msp, coords: list, layer: str, transformer, closed: bool = False
) -> None:
    """
    将 GeoJSON LineString 坐标写入 DXF LWPOLYLINE 实体。

    LWPOLYLINE 是 DXF 中最常用的折线实体，支持开放和闭合两种形式。
    Polygon 的环使用 closed=True，LineString 使用 closed=False。

    注意：当 closed=True 且首尾点相同时，自动去除最后一个重复点
    （GeoJSON Polygon 规范要求首尾点相同，但 DXF 闭合多段线不需要）。

    参数:
        msp:         ezdxf Modelspace
        coords:      坐标列表 [[lon, lat], ...]
        layer:       目标图层名
        transformer: 坐标转换器（可为 None）
        closed:      是否闭合（True=Polygon 环，False=LineString）
    """
    if len(coords) < 2:
        logger.debug(f"LineString 点数不足 2，跳过（layer={layer}）")
        return

    # 批量坐标转换
    transformed = _transform_coords(coords, transformer)

    # 闭合时去除重复的首尾点（GeoJSON Polygon 规范与 DXF 闭合方式不同）
    if closed and len(transformed) > 1:
        if transformed[0] == transformed[-1]:
            transformed = transformed[:-1]

    if len(transformed) < 2:
        return

    # 创建 LWPOLYLINE，只传入 (x, y)（LWPOLYLINE 是二维实体）
    polyline = msp.add_lwpolyline(
        points=[(x, y) for x, y in transformed],
        dxfattribs={"layer": layer},
    )
    polyline.closed = closed


def _write_polygon(msp, rings: list, layer: str, transformer) -> int:
    """
    将 GeoJSON Polygon 的所有环写入 DXF LWPOLYLINE 实体。

    GeoJSON Polygon 结构：
        rings[0] = 外环（exterior ring，顺时针或逆时针）
        rings[1:] = 内环（holes/interior rings）

    每个环单独写入一个闭合 LWPOLYLINE，内外环在 DXF 中无法区分，
    用户可通过图层或颜色区分（当前实现均写入同一图层）。

    参数:
        msp:         ezdxf Modelspace
        rings:       Polygon 环列表（外环 + 各内环）
        layer:       目标图层名
        transformer: 坐标转换器（可为 None）

    返回:
        写入的 LWPOLYLINE 实体数量（= 有效环数量）
    """
    count = 0
    for i, ring_coords in enumerate(rings):
        if len(ring_coords) < 3:
            # 有效的环至少需要 3 个点（不计首尾重复点则需要 3 个不同点）
            logger.debug(
                f"Polygon 第 {i} 个环点数不足 3，跳过（layer={layer}）"
            )
            continue
        _write_linestring(msp, ring_coords, layer, transformer, closed=True)
        count += 1
    return count


def _convert_dxf_to_dwg(
    dxf_path: Path, dwg_path: Path, oda_path: Optional[str] = None
) -> None:
    """
    调用 ODA File Converter 将 DXF 文件转换为 DWG 文件。

    复用 dwg_to_dxf.py 中的 find_oda_converter() 查找 ODA 安装路径。
    ODA 命令行格式：
        ODAFileConverter <输入目录> <输出目录> <版本> <格式> <递归> <审计> <文件名>

    参数:
        dxf_path: 输入 DXF 文件路径（Path 对象）
        dwg_path: 输出 DWG 文件路径（Path 对象）
        oda_path: ODA File Converter 可执行文件路径（None 则自动查找）

    异常:
        FileNotFoundError: ODA File Converter 未找到
        RuntimeError: 转换失败或超时
    """
    # 查找 ODA File Converter 可执行文件
    converter_exe = find_oda_converter(oda_path)

    # 使用临时目录接收 ODA 的输出，再移动到目标路径
    with tempfile.TemporaryDirectory(prefix="cad2geojson_dwg_") as tmp_dir:
        # ODA 参数：输入目录 输出目录 DWG版本 输出格式 递归 审计 文件过滤
        cmd = [
            converter_exe,
            str(dxf_path.parent),  # 输入目录（包含临时 DXF 文件的目录）
            tmp_dir,               # 输出目录（临时）
            "ACAD2018",            # DWG 版本（兼容性最好）
            "DWG",                 # 目标格式
            "0",                   # 不递归子目录
            "1",                   # 开启审计修复（自动修复轻微错误）
            dxf_path.name,         # 只转换该文件
        ]

        logger.info(f"执行 DXF→DWG 转换: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 超时 300 秒
                check=False,
            )

            if result.stdout:
                logger.debug(f"ODA 标准输出: {result.stdout}")
            if result.stderr:
                logger.warning(f"ODA 错误输出: {result.stderr}")

            if result.returncode != 0:
                raise RuntimeError(
                    f"ODA File Converter DXF→DWG 失败，"
                    f"返回码: {result.returncode}，"
                    f"错误: {result.stderr or '无'}"
                )

        except subprocess.TimeoutExpired:
            raise RuntimeError("ODA File Converter 转换超时（>300s），文件可能过大")
        except OSError as e:
            raise RuntimeError(f"无法执行 ODA File Converter: {e}")

        # 查找转换后的 DWG 文件（ODA 将后缀从 .dxf 改为 .dwg）
        expected_dwg_name = dxf_path.stem + ".dwg"
        tmp_dwg_path = Path(tmp_dir) / expected_dwg_name

        if not tmp_dwg_path.is_file():
            raise RuntimeError(
                f"ODA 转换完成但未找到输出 DWG 文件: {tmp_dwg_path}\n"
                f"请检查 ODA File Converter 是否正常工作"
            )

        # 将临时 DWG 文件移动到目标路径
        shutil.move(str(tmp_dwg_path), str(dwg_path))
        logger.info(f"DXF→DWG 转换成功: {dwg_path}")
