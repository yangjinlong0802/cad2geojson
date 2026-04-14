# -*- coding: utf-8 -*-
"""
主转换流程编排模块

负责串联整个 CAD → GeoJSON 的转换流程：
    1. DWG → DXF（如果输入是 DWG 文件）
    2. DXF 解析 → 提取实体
    3. 实体几何映射 → GeoJSON 几何类型
    4. 坐标系转换 → WGS84
    5. 组装 GeoJSON FeatureCollection
    6. 输出 GeoJSON 文件

各步骤对应的模块：
    dwg_to_dxf.py → dxf_parser.py → geometry_mapper.py →
    coordinate_transformer.py → geojson_builder.py
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .dwg_to_dxf import convert_dwg_to_dxf, cleanup_temp_dir
from .dxf_parser import parse_dxf, ParsedEntity, EntityTypeStats
from .geometry_mapper import map_entity_to_geometry
from .coordinate_transformer import CoordinateTransformer
from .geojson_builder import (
    build_feature,
    build_feature_collection,
    save_geojson,
    save_geojson_by_layers,
    validate_geojson,
)
from .gdal_parser import parse_dxf_with_gdal, _check_fiona_available

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


class ConversionConfig:
    """
    转换配置类。

    集中管理转换过程中的所有可配置参数，避免在函数间传递大量参数。

    属性:
        input_file:     输入文件路径（DWG 或 DXF）
        output_file:    输出 GeoJSON 文件路径
        source_crs:     源坐标系 EPSG 编码
        no_transform:   是否禁用坐标转换
        split_layers:   是否按图层分别输出
        arc_segments:   弧线离散化的分段数
        expand_blocks:  是否展开块引用
        oda_path:       ODA File Converter 路径
        layers:         只转换的图层列表
        exclude_layers: 排除的图层列表
        engine:         解析引擎 ("ezdxf" / "gdal" / "auto")
    """

    def __init__(
        self,
        input_file: str,
        output_file: str = None,
        source_crs: str = None,
        no_transform: bool = False,
        split_layers: bool = False,
        arc_segments: int = 64,
        expand_blocks: bool = True,
        oda_path: str = None,
        layers: List[str] = None,
        exclude_layers: List[str] = None,
        engine: str = "auto",
    ):
        """
        初始化转换配置。

        参数:
            input_file:     输入文件路径
            output_file:    输出文件路径（默认为输入文件名 + .geojson）
            source_crs:     源坐标系 EPSG 编码（如 EPSG:2437）
            no_transform:   是否禁用坐标转换
            split_layers:   是否按图层分别输出
            arc_segments:   弧线离散化的分段数
            expand_blocks:  是否展开块引用
            oda_path:       ODA File Converter 的安装路径
            layers:         只转换的图层列表（逗号分隔的字符串或列表）
            exclude_layers: 排除的图层列表（逗号分隔的字符串或列表）
            engine:         解析引擎 ("ezdxf" / "gdal" / "auto")
                            auto 模式下两个引擎都跑，取 Feature 数更多的结果
        """
        self.input_file = input_file
        self.source_crs = source_crs
        self.no_transform = no_transform
        self.split_layers = split_layers
        self.arc_segments = arc_segments
        self.expand_blocks = expand_blocks
        self.oda_path = oda_path
        self.engine = engine.lower() if engine else "auto"

        # 处理图层过滤参数（支持字符串和列表两种输入）
        self.layers = self._parse_layer_list(layers)
        self.exclude_layers = self._parse_layer_list(exclude_layers)

        # 如果未指定输出路径，默认使用输入文件名加 .geojson 后缀
        if output_file:
            self.output_file = output_file
        else:
            input_path = Path(input_file)
            self.output_file = str(input_path.with_suffix(".geojson"))

    @staticmethod
    def _parse_layer_list(layers) -> Optional[List[str]]:
        """
        解析图层列表参数。

        支持两种输入格式：
            - 逗号分隔的字符串: "图层1,图层2,图层3"
            - Python 列表: ["图层1", "图层2", "图层3"]

        参数:
            layers: 图层列表（字符串或列表，或 None）

        返回:
            图层名称列表，或 None（表示不过滤）
        """
        if layers is None:
            return None
        if isinstance(layers, str):
            # 按逗号分隔，去除每个图层名的首尾空格
            return [l.strip() for l in layers.split(",") if l.strip()]
        if isinstance(layers, (list, tuple)):
            return list(layers)
        return None


@dataclass
class ConversionResult:
    """
    转换结果，包含输出路径和诊断统计信息。

    属性:
        output_path:  输出的 GeoJSON 文件路径
        diagnostics:  实体类型转换诊断统计（仅 ezdxf/auto 引擎有值）
    """
    output_path: str
    diagnostics: Optional[EntityTypeStats] = None


def convert(config: ConversionConfig) -> ConversionResult:
    """
    执行 CAD → GeoJSON 的完整转换流程。

    这是整个转换流程的主入口函数，按顺序执行以下步骤：
        1. DWG → DXF 转换（如果需要）
        2. DXF 文件解析
        3. 几何类型映射
        4. 坐标系转换
        5. GeoJSON 组装与输出

    参数:
        config: ConversionConfig 转换配置对象

    返回:
        ConversionResult 对象，包含输出路径和诊断统计

    异常:
        FileNotFoundError: 输入文件不存在
        RuntimeError: 转换过程中发生错误
    """
    logger.info(f"========== 开始转换 ==========")
    logger.info(f"输入文件: {config.input_file}")

    temp_dir = None  # 记录可能需要清理的临时目录

    try:
        # ===== 第 1 步：DWG → DXF 转换 =====
        dxf_file = _step_dwg_to_dxf(config)

        # 如果进行了 DWG 转换，记录临时目录以便后续清理
        if dxf_file != str(Path(config.input_file).resolve()):
            temp_dir = str(Path(dxf_file).parent)

        # ===== 第 2 步 + 第 3 步：解析 DXF + 构建 Feature =====
        # 根据 engine 配置选择解析引擎，同时收集诊断统计
        features, diagnostics = _step_parse_and_build(dxf_file, config)

        if not features:
            logger.warning("未解析到任何有效实体，输出将为空的 FeatureCollection")

        # ===== 第 4 步：输出 GeoJSON =====
        output_path = _step_output_geojson(features, config)

        logger.info(f"========== 转换完成 ==========")
        logger.info(f"输出文件: {output_path}")

        return ConversionResult(output_path=output_path, diagnostics=diagnostics)

    finally:
        # 清理 DWG → DXF 转换产生的临时目录
        if temp_dir:
            cleanup_temp_dir(temp_dir)


def _step_dwg_to_dxf(config: ConversionConfig) -> str:
    """
    第 1 步：DWG → DXF 转换。

    如果输入文件是 DWG 格式，调用 ODA File Converter 转换为 DXF。
    如果输入文件已经是 DXF 格式，直接返回路径。

    参数:
        config: 转换配置

    返回:
        DXF 文件路径
    """
    logger.info("[步骤 1/4] 检查输入文件格式...")

    dxf_file = convert_dwg_to_dxf(
        config.input_file,
        oda_path=config.oda_path,
    )

    return dxf_file


def _step_parse_and_build(
    dxf_file: str, config: ConversionConfig
) -> Tuple[list, Optional[EntityTypeStats]]:
    """
    第 2+3 步：解析 DXF 文件 + 构建 GeoJSON Feature。

    根据 engine 参数选择解析引擎：
    - "ezdxf": 使用 ezdxf 解析 → 几何映射 → Feature 构建
    - "gdal":  使用 fiona/GDAL 直接输出 GeoJSON Feature
    - "auto":  两个引擎都跑，取 Feature 数更多的结果

    参数:
        dxf_file: DXF 文件路径
        config:   转换配置

    返回:
        元组 (GeoJSON Feature 列表, 诊断统计信息)
        GDAL 引擎不返回按实体类型的诊断统计，此时 diagnostics 为 None
    """
    engine = config.engine

    if engine == "auto":
        return _parse_auto(dxf_file, config)
    elif engine == "gdal":
        # GDAL 引擎没有按实体类型的细粒度统计
        return _parse_with_gdal(dxf_file, config), None
    else:
        # 默认使用 ezdxf
        return _parse_with_ezdxf(dxf_file, config)


def _parse_with_ezdxf(
    dxf_file: str, config: ConversionConfig
) -> Tuple[list, EntityTypeStats]:
    """
    使用 ezdxf 引擎解析 DXF 并构建 Feature 列表。

    流程：ezdxf 解析 → 几何映射 → 坐标转换 → Feature 构建
    同时收集每种实体类型的转换成功/失败统计。

    参数:
        dxf_file: DXF 文件路径
        config:   转换配置

    返回:
        元组 (GeoJSON Feature 列表, EntityTypeStats 统计信息)
    """
    logger.info("[步骤 2/4] 解析 DXF 文件（引擎: ezdxf）...")

    # 解析 DXF，获取实体列表和解析阶段的统计
    entities, stats = parse_dxf(
        dxf_file,
        layers=config.layers,
        exclude_layers=config.exclude_layers,
        expand_blocks=config.expand_blocks,
    )

    logger.info("[步骤 3/4] 执行几何映射和坐标转换...")

    # 创建坐标转换器
    coord_transformer = CoordinateTransformer(
        source_crs=config.source_crs,
        no_transform=config.no_transform,
    )

    features = []
    success_count = 0
    fail_count = 0

    for entity in entities:
        # 几何类型映射
        geometry = map_entity_to_geometry(entity, config.arc_segments)
        if geometry is None:
            fail_count += 1
            continue

        # 坐标系转换
        geometry = coord_transformer.transform(geometry)

        # 构建 Feature
        feature = build_feature(entity, geometry)
        if feature is not None:
            features.append(feature)
            success_count += 1
        else:
            fail_count += 1

    logger.info(f"ezdxf 引擎: 成功 {success_count} 个, 失败 {fail_count} 个")
    return features, stats


def _parse_with_gdal(dxf_file: str, config: ConversionConfig) -> list:
    """
    使用 GDAL/fiona 引擎解析 DXF 并构建 Feature 列表。

    GDAL 直接输出标准 GeoJSON Feature，只需做坐标转换。

    参数:
        dxf_file: DXF 文件路径
        config:   转换配置

    返回:
        GeoJSON Feature 对象列表
    """
    logger.info("[步骤 2-3/4] 解析 DXF 文件（引擎: GDAL/fiona）...")

    # GDAL 直接输出 Feature 列表
    features = parse_dxf_with_gdal(
        dxf_file,
        layers=config.layers,
        exclude_layers=config.exclude_layers,
    )

    # 如果需要坐标转换，对 GDAL 输出的 Feature 进行转换
    if config.source_crs and not config.no_transform:
        coord_transformer = CoordinateTransformer(
            source_crs=config.source_crs,
            no_transform=config.no_transform,
        )
        for feature in features:
            if feature.get("geometry"):
                feature["geometry"] = coord_transformer.transform(
                    feature["geometry"]
                )

    logger.info(f"GDAL 引擎: 成功 {len(features)} 个 Feature")
    return features


def _parse_auto(
    dxf_file: str, config: ConversionConfig
) -> Tuple[list, EntityTypeStats]:
    """
    自动模式：双引擎按图层合并，取每个图层的最优结果。

    策略：
    1. 分别用 ezdxf 和 GDAL 解析同一个 DXF 文件
    2. 将两个引擎的 Feature 按图层名分组
    3. 对每个图层，取 Feature 数更多的引擎的结果
    4. 合并所有图层的最优结果

    这样能结合两个引擎的优势：
    - ezdxf 擅长块引用展开、复合实体分解
    - GDAL 擅长 DIMENSION 标注、某些特殊实体的原生支持

    参数:
        dxf_file: DXF 文件路径
        config:   转换配置

    返回:
        元组 (合并后的 Feature 列表, ezdxf 引擎的诊断统计)
    """
    logger.info("[自动模式] 双引擎按图层合并，取每层最优结果...")

    # 先用 ezdxf（同时获取诊断统计）
    ezdxf_features, stats = _parse_with_ezdxf(dxf_file, config)

    # 再用 GDAL（如果可用）
    if _check_fiona_available():
        try:
            gdal_features = _parse_with_gdal(dxf_file, config)
        except Exception as e:
            logger.warning(f"GDAL 引擎解析失败，仅使用 ezdxf 结果: {e}")
            return ezdxf_features, stats
    else:
        logger.info("fiona 未安装，仅使用 ezdxf 引擎")
        return ezdxf_features, stats

    # 如果某个引擎没有结果，直接返回另一个
    if not ezdxf_features:
        return gdal_features, stats
    if not gdal_features:
        return ezdxf_features, stats

    # 按图层分组
    from collections import defaultdict
    ezdxf_by_layer = defaultdict(list)
    gdal_by_layer = defaultdict(list)

    for feat in ezdxf_features:
        layer = feat.get("properties", {}).get("layer", "0")
        ezdxf_by_layer[layer].append(feat)

    for feat in gdal_features:
        layer = feat.get("properties", {}).get("layer", "0")
        gdal_by_layer[layer].append(feat)

    # 合并所有图层名
    all_layers = set(list(ezdxf_by_layer.keys()) + list(gdal_by_layer.keys()))

    # 按图层取最优结果
    merged_features = []
    ezdxf_wins = 0    # ezdxf 胜出的图层数
    gdal_wins = 0     # GDAL 胜出的图层数

    for layer in sorted(all_layers):
        e_feats = ezdxf_by_layer.get(layer, [])
        g_feats = gdal_by_layer.get(layer, [])
        e_count = len(e_feats)
        g_count = len(g_feats)

        if g_count > e_count:
            # GDAL 在该图层找到了更多 Feature
            merged_features.extend(g_feats)
            gdal_wins += 1
            if e_count > 0:
                logger.debug(
                    f"  图层 \"{layer}\": GDAL 胜出 ({g_count} > {e_count})"
                )
        else:
            # ezdxf 在该图层找到了更多或相同数量的 Feature
            merged_features.extend(e_feats)
            ezdxf_wins += 1
            if g_count > 0 and g_count < e_count:
                logger.debug(
                    f"  图层 \"{layer}\": ezdxf 胜出 ({e_count} > {g_count})"
                )

    logger.info(
        f"[自动模式] 合并完成: 共 {len(merged_features)} 个 Feature, "
        f"{len(all_layers)} 个图层 "
        f"(ezdxf 胜出 {ezdxf_wins} 层, GDAL 胜出 {gdal_wins} 层)"
    )

    return merged_features, stats


def _step_output_geojson(features: list, config: ConversionConfig) -> str:
    """
    第 4 步：输出 GeoJSON 文件。

    根据配置决定输出方式：
        - 所有实体输出到一个文件
        - 按图层分别输出到多个文件

    参数:
        features: GeoJSON Feature 对象列表
        config:   转换配置

    返回:
        输出路径（单文件模式返回文件路径，分层模式返回目录路径）
    """
    logger.info("[步骤 4/4] 输出 GeoJSON 文件...")

    if config.split_layers:
        # 按图层分别输出
        output_dir = str(Path(config.output_file).parent)
        base_name = Path(config.output_file).stem
        output_files = save_geojson_by_layers(
            features, output_dir, base_name
        )
        return output_dir
    else:
        # 所有实体输出到一个文件
        collection = build_feature_collection(features)

        # 校验 GeoJSON 合法性
        validate_geojson(collection)

        # 保存到文件
        save_geojson(collection, config.output_file)
        return config.output_file
