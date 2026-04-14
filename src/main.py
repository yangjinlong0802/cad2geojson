# -*- coding: utf-8 -*-
"""
CLI 入口模块

使用 click 库实现命令行参数解析，提供用户友好的命令行接口。

使用方式：
    python -m src.main <input_file> [选项]

示例：
    # 基本转换（DXF 文件，不做坐标转换）
    python -m src.main input.dxf

    # DWG 文件转换，指定源坐标系
    python -m src.main input.dwg --source-crs EPSG:2437

    # 按图层分别输出，指定弧线精度
    python -m src.main input.dxf --split-layers --arc-segments 128

    # 只转换指定图层
    python -m src.main input.dxf --layers "道路,建筑,绿化"
"""

import logging
import sys
from pathlib import Path

import click

from .converter import ConversionConfig, convert

# 版本号
__version__ = "0.1.0"


def setup_logging(verbose: bool) -> None:
    """
    配置日志系统。

    根据 verbose 参数设置日志级别：
        - verbose=True:  DEBUG 级别，输出详细调试信息
        - verbose=False: INFO 级别，只输出关键步骤信息

    参数:
        verbose: 是否启用详细日志模式
    """
    level = logging.DEBUG if verbose else logging.INFO

    # 日志格式：时间 - 模块名 - 级别 - 消息
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),  # 输出到控制台
        ],
    )

    # 降低第三方库的日志级别，避免过多噪音
    logging.getLogger("ezdxf").setLevel(logging.WARNING)
    logging.getLogger("pyproj").setLevel(logging.WARNING)


@click.command(
    help="CAD (DWG/DXF) 文件转 GeoJSON 格式的命令行工具。\n\n"
         "将 CAD 文件中的图形实体转换为标准的 GeoJSON 格式，"
         "支持坐标系转换、图层过滤、弧线精度控制等功能。"
)
@click.argument(
    "input_file",
    type=click.Path(exists=True, readable=True),
)
@click.option(
    "-o", "--output",
    type=click.Path(),
    default=None,
    help="输出 GeoJSON 文件路径（默认: 输入文件名.geojson）",
)
@click.option(
    "--source-crs",
    type=str,
    default=None,
    help="源坐标系 EPSG 编码（如 EPSG:2437）。如不指定，将直接使用原始坐标。",
)
@click.option(
    "--no-transform",
    is_flag=True,
    default=False,
    help="不进行坐标转换，直接使用 CAD 中的原始坐标。",
)
@click.option(
    "--split-layers",
    is_flag=True,
    default=False,
    help="按图层分别输出 GeoJSON 文件，每个图层一个文件。",
)
@click.option(
    "--arc-segments",
    type=int,
    default=64,
    show_default=True,
    help="弧线和圆的离散化分段数。数值越大越精细，但文件也越大。",
)
@click.option(
    "--expand-blocks/--no-expand-blocks",
    default=True,
    show_default=True,
    help="是否展开块引用（INSERT 实体）中的子实体。",
)
@click.option(
    "--oda-path",
    type=click.Path(),
    default=None,
    help="ODA File Converter 的安装路径（仅转换 DWG 文件时需要）。",
)
@click.option(
    "--layers",
    type=str,
    default=None,
    help="只转换指定图层，多个图层用逗号分隔（如: 道路,建筑,绿化）。",
)
@click.option(
    "--exclude-layers",
    type=str,
    default=None,
    help="排除指定图层，多个图层用逗号分隔。",
)
@click.option(
    "--engine",
    type=click.Choice(["auto", "ezdxf", "gdal"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="DXF 解析引擎。auto 模式下双引擎都跑，取 Feature 数更多的结果。",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="启用详细日志输出（DEBUG 级别）。",
)
@click.version_option(version=__version__, prog_name="cad2geojson")
def cli(
    input_file: str,
    output: str,
    source_crs: str,
    no_transform: bool,
    split_layers: bool,
    arc_segments: int,
    expand_blocks: bool,
    oda_path: str,
    layers: str,
    exclude_layers: str,
    engine: str,
    verbose: bool,
):
    """
    CAD (DWG/DXF) 文件转 GeoJSON 工具的 CLI 入口函数。

    接收并解析命令行参数，创建转换配置，调用转换流程。
    """
    # 配置日志系统
    setup_logging(verbose)

    logger = logging.getLogger(__name__)
    logger.info(f"cad2geojson v{__version__}")

    try:
        # 创建转换配置对象
        config = ConversionConfig(
            input_file=input_file,
            output_file=output,
            source_crs=source_crs,
            no_transform=no_transform,
            split_layers=split_layers,
            arc_segments=arc_segments,
            expand_blocks=expand_blocks,
            oda_path=oda_path,
            layers=layers,
            exclude_layers=exclude_layers,
            engine=engine,
        )

        # 执行转换
        result = convert(config)

        # 输出诊断报告（如果有的话）
        if result.diagnostics:
            click.echo(result.diagnostics.format_report())

        # 输出结果
        click.echo(f"\n[OK] 转换成功！")
        click.echo(f"  输出文件: {result.output_path}")

    except FileNotFoundError as e:
        # 文件未找到错误（输入文件不存在、ODA 未安装等）
        logger.error(str(e))
        click.echo(f"\n[FAIL] 错误: {e}", err=True)
        sys.exit(1)

    except ValueError as e:
        # 参数值错误（不支持的文件格式、无效的坐标系等）
        logger.error(str(e))
        click.echo(f"\n[FAIL] 参数错误: {e}", err=True)
        sys.exit(1)

    except RuntimeError as e:
        # 运行时错误（转换失败、ODA 调用失败等）
        logger.error(str(e))
        click.echo(f"\n[FAIL] 转换失败: {e}", err=True)
        sys.exit(1)

    except Exception as e:
        # 未预期的异常
        logger.exception(f"发生未预期的错误: {e}")
        click.echo(f"\n[FAIL] 未预期的错误: {e}", err=True)
        click.echo("请使用 -v 参数查看详细日志", err=True)
        sys.exit(1)


# 支持 python -m src.main 方式运行
if __name__ == "__main__":
    cli()
