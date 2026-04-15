# -*- coding: utf-8 -*-
"""
坐标转换模块

使用 pyproj 库实现 CAD 工程坐标系到 WGS84 (EPSG:4326) 的坐标转换。
GeoJSON 规范要求坐标使用 WGS84 经纬度。

关键注意事项：
    - CAD 中的坐标通常是 (x, y) = (easting, northing)
    - GeoJSON 中的坐标是 [longitude, latitude]
    - pyproj v2+ 默认按 CRS 定义的轴序，需使用 always_xy=True 确保一致性
    - 使用 always_xy=True 后，输入和输出统一为 (x/经度, y/纬度) 顺序
"""

import json
import logging
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from pyproj import Transformer, CRS
from pyproj.exceptions import CRSError

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)

# GeoJSON 标准目标坐标系：WGS84
TARGET_CRS = "EPSG:4326"

# 坐标系配置文件路径（相对于项目根目录）
CONFIG_DIR = Path(__file__).parent.parent / "config"
COORD_SYSTEMS_FILE = CONFIG_DIR / "coordinate_systems.json"


def load_coordinate_systems() -> Dict[str, Dict[str, str]]:
    """
    从配置文件加载预定义的坐标系映射表。

    配置文件格式为 JSON，包含常用的坐标系名称和对应的 EPSG 编码。
    这样用户可以通过友好的名称（如 "北京54-3度带-39带"）来指定坐标系。

    返回:
        坐标系配置字典，键为坐标系名称，值为包含 EPSG 编码等信息的字典
    """
    if not COORD_SYSTEMS_FILE.exists():
        logger.debug(f"坐标系配置文件不存在: {COORD_SYSTEMS_FILE}")
        return {}

    try:
        with open(COORD_SYSTEMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"已加载 {len(data)} 个预定义坐标系")
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"加载坐标系配置文件失败: {e}")
        return {}


def create_transformer(source_crs: str) -> Transformer:
    """
    创建坐标转换器。

    使用 pyproj.Transformer 建立从源坐标系到 WGS84 的转换器。
    设置 always_xy=True 确保输入输出都是 (x/经度, y/纬度) 顺序，
    避免不同 CRS 定义的轴序差异导致的混乱。

    参数:
        source_crs: 源坐标系标识符（EPSG 编码，如 "EPSG:2437"）

    返回:
        pyproj.Transformer 转换器对象

    异常:
        CRSError: 无效的坐标系标识符
    """
    try:
        transformer = Transformer.from_crs(
            source_crs,
            TARGET_CRS,
            always_xy=True,  # 关键参数：确保输入输出统一为 (x, y) / (lon, lat) 顺序
        )
        logger.info(f"已创建坐标转换器: {source_crs} → {TARGET_CRS}")
        return transformer
    except CRSError as e:
        logger.error(f"无效的坐标系: {source_crs}")
        raise CRSError(f"无法识别坐标系 '{source_crs}'，请检查 EPSG 编码是否正确: {e}")


def transform_point(
    transformer: Transformer,
    x: float,
    y: float,
) -> Tuple[float, float]:
    """
    转换单个坐标点。

    参数:
        transformer: pyproj.Transformer 转换器
        x:           源坐标 X（easting / 经度）
        y:           源坐标 Y（northing / 纬度）

    返回:
        转换后的坐标 (longitude, latitude)
    """
    lon, lat = transformer.transform(x, y)
    return (lon, lat)


def transform_coordinates(
    coords: List[Tuple[float, float]],
    transformer: Transformer,
) -> List[Tuple[float, float]]:
    """
    批量转换坐标点列表。

    参数:
        coords:      坐标点列表 [(x1, y1), (x2, y2), ...]
        transformer: pyproj.Transformer 转换器

    返回:
        转换后的坐标点列表 [(lon1, lat1), (lon2, lat2), ...]
    """
    transformed = []
    for x, y in coords:
        lon, lat = transformer.transform(x, y)
        transformed.append((lon, lat))
    return transformed


def transform_geometry(
    geometry: Dict[str, Any],
    transformer: Transformer,
) -> Dict[str, Any]:
    """
    对 GeoJSON 几何对象中的所有坐标进行坐标系转换。

    递归处理 GeoJSON 几何对象的坐标数组，将每个坐标点从源坐标系转换到 WGS84。
    支持所有 GeoJSON 几何类型：Point, LineString, Polygon, Multi* 等。

    参数:
        geometry:    GeoJSON 几何对象字典（包含 type 和 coordinates）
        transformer: pyproj.Transformer 转换器

    返回:
        坐标转换后的 GeoJSON 几何对象字典
    """
    if geometry is None:
        return None

    geom_type = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if geom_type is None or coordinates is None:
        logger.warning("无效的 GeoJSON 几何对象：缺少 type 或 coordinates 字段")
        return geometry

    # 根据几何类型递归转换坐标
    transformed_coords = _transform_coords_recursive(geom_type, coordinates, transformer)

    # 返回新的几何对象（不修改原对象）
    return {
        "type": geom_type,
        "coordinates": transformed_coords,
    }


def _transform_coords_recursive(
    geom_type: str,
    coordinates: Any,
    transformer: Transformer,
) -> Any:
    """
    递归转换 GeoJSON 坐标。

    GeoJSON 不同几何类型的坐标嵌套层数不同：
        Point:        一个坐标 [x, y]
        LineString:   坐标数组 [[x,y], [x,y], ...]
        Polygon:      环数组的数组 [[[x,y], ...], [[x,y], ...]]
        MultiPolygon: 多边形数组的数组 [[[[x,y], ...]], ...]

    参数:
        geom_type:   几何类型
        coordinates: 坐标数据（嵌套列表）
        transformer: pyproj.Transformer 转换器

    返回:
        转换后的坐标数据
    """
    if geom_type == "Point":
        # Point: 单个坐标 [x, y]
        x, y = coordinates[0], coordinates[1]
        lon, lat = transformer.transform(x, y)
        return (lon, lat)

    elif geom_type in ("LineString", "MultiPoint"):
        # LineString/MultiPoint: 坐标列表 [[x,y], ...]
        return [
            transformer.transform(coord[0], coord[1])
            for coord in coordinates
        ]

    elif geom_type in ("Polygon", "MultiLineString"):
        # Polygon/MultiLineString: 环/线列表 [[[x,y], ...], ...]
        return [
            [transformer.transform(coord[0], coord[1]) for coord in ring]
            for ring in coordinates
        ]

    elif geom_type == "MultiPolygon":
        # MultiPolygon: 多边形列表 [[[[x,y], ...]], ...]
        return [
            [
                [transformer.transform(coord[0], coord[1]) for coord in ring]
                for ring in polygon
            ]
            for polygon in coordinates
        ]

    else:
        # 未知类型，原样返回
        logger.warning(f"未知的几何类型，坐标未转换: {geom_type}")
        return coordinates


class CoordinateTransformer:
    """
    坐标转换器封装类。

    提供统一的接口来管理坐标转换的配置和执行。
    支持"不转换"模式（直接使用原始坐标）。

    使用示例:
        # 创建转换器（指定源坐标系）
        ct = CoordinateTransformer(source_crs="EPSG:2437")

        # 转换 GeoJSON 几何对象
        transformed_geom = ct.transform(geojson_geometry)

        # 不转换模式
        ct = CoordinateTransformer(no_transform=True)
        same_geom = ct.transform(geojson_geometry)  # 原样返回
    """

    def __init__(
        self,
        source_crs: str = None,
        no_transform: bool = False,
    ):
        """
        初始化坐标转换器。

        参数:
            source_crs:   源坐标系 EPSG 编码（如 "EPSG:2437"）
            no_transform: 如果为 True，则不进行坐标转换，直接使用原始坐标
        """
        self.source_crs = source_crs
        self.no_transform = no_transform
        self._transformer = None  # 延迟创建 pyproj 转换器

        if no_transform:
            logger.info("坐标转换已禁用，将使用原始坐标")
        elif source_crs:
            # 立即创建转换器，验证坐标系是否有效
            self._transformer = create_transformer(source_crs)
        else:
            # 未指定源坐标系且未禁用转换，默认假设输入已是 WGS84
            logger.info(
                "未指定源坐标系，假设 CAD 坐标已经是 WGS84 经纬度。"
                "如需坐标转换，请使用 --source-crs 参数指定源坐标系"
            )
            self.no_transform = True

    def transform(self, geometry: Dict[str, Any]) -> Dict[str, Any]:
        """
        对 GeoJSON 几何对象执行坐标转换。

        参数:
            geometry: GeoJSON 几何对象字典

        返回:
            坐标转换后的 GeoJSON 几何对象字典
        """
        if self.no_transform or self._transformer is None:
            return geometry

        return transform_geometry(geometry, self._transformer)
