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
from .geojson_to_dxf import ExportConfig, export_geojson_to_dxf
from .renderer import RenderPipeline

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


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
@click.version_option(version=__version__, prog_name="cad2geojson")
def cli_group(ctx):
    """cad2geojson —— CAD ↔ GeoJSON 转换工具集。\n\n子命令：\n  convert  CAD 转 GeoJSON（默认）\n  render   GeoJSON 转 SVG（通过 LLM）"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ── convert 子命令（原 cli 逻辑不变）─────────────────────────────────────────
@cli_group.command("convert", help="CAD (DWG/DXF) 文件转 GeoJSON 格式。")
@click.argument("input_file", type=click.Path(exists=True, readable=True))
@click.option("-o", "--output", type=click.Path(), default=None, help="输出 GeoJSON 文件路径")
@click.option("--source-crs", type=str, default=None, help="源坐标系 EPSG 编码（如 EPSG:2437）")
@click.option("--no-transform", is_flag=True, default=False, help="不进行坐标转换，使用原始坐标")
@click.option("--split-layers", is_flag=True, default=False, help="按图层分别输出 GeoJSON 文件")
@click.option("--arc-segments", type=int, default=64, show_default=True, help="弧线离散化分段数")
@click.option("--expand-blocks/--no-expand-blocks", default=True, show_default=True, help="是否展开块引用")
@click.option("--oda-path", type=click.Path(), default=None, help="ODA File Converter 路径（DWG 转换用）")
@click.option("--layers", type=str, default=None, help="只转换指定图层（逗号分隔）")
@click.option("--exclude-layers", type=str, default=None, help="排除指定图层（逗号分隔）")
@click.option("--engine", type=click.Choice(["auto", "ezdxf", "gdal"], case_sensitive=False), default="auto", show_default=True, help="DXF 解析引擎")
@click.option("-v", "--verbose", is_flag=True, default=False, help="详细日志（DEBUG 级别）")
def convert_cmd(input_file, output, source_crs, no_transform, split_layers,
                arc_segments, expand_blocks, oda_path, layers, exclude_layers, engine, verbose):
    """CAD → GeoJSON 转换子命令。"""
    setup_logging(verbose)
    logger = logging.getLogger(__name__)
    logger.info(f"cad2geojson v{__version__}")

    try:
        config = ConversionConfig(
            input_file=input_file, output_file=output, source_crs=source_crs,
            no_transform=no_transform, split_layers=split_layers, arc_segments=arc_segments,
            expand_blocks=expand_blocks, oda_path=oda_path, layers=layers,
            exclude_layers=exclude_layers, engine=engine,
        )
        output_path = convert(config)
        click.echo(f"\n[OK] 转换成功！输出: {output_path}")
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"\n[FAIL] 错误: {e}", err=True); sys.exit(1)
    except Exception as e:
        logger.exception(e); click.echo(f"\n[FAIL] 未预期错误: {e}", err=True); sys.exit(1)


# ── export 子命令（GeoJSON → DXF/DWG）───────────────────────────────────────
@cli_group.command("export", help="GeoJSON → DXF/DWG 文件（反向导出）。")
@click.argument("geojson_file", type=click.Path(exists=True, readable=True))
@click.option("-o", "--output", type=click.Path(), default=None,
              help="输出 DXF/DWG 文件路径（默认同名换后缀）")
@click.option("--target-crs", type=str, default=None,
              help="目标坐标系 EPSG 编码（WGS84→工程坐标反向转换，如 EPSG:4526）")
@click.option("--format", "fmt", type=click.Choice(["dxf", "dwg"], case_sensitive=False),
              default="dxf", show_default=True, help="输出格式")
@click.option("--oda-path", type=click.Path(), default=None,
              help="ODA File Converter 路径（DWG 输出时使用，默认自动查找）")
@click.option("--default-layer", type=str, default="0", show_default=True,
              help="无 layer 属性时使用的默认图层名")
@click.option("-v", "--verbose", is_flag=True, default=False, help="详细日志（DEBUG 级别）")
def export_cmd(geojson_file, output, target_crs, fmt, oda_path, default_layer, verbose):
    """
    GeoJSON → DXF/DWG 反向导出子命令。

    示例：

        # 基本导出为 DXF
        .venv/Scripts/python -m src.main export output/test.geojson

        # 导出为 DWG
        .venv/Scripts/python -m src.main export output/test.geojson --format dwg

        # 反向坐标转换（WGS84 → 工程坐标系）
        .venv/Scripts/python -m src.main export output/test.geojson --target-crs EPSG:4526
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)
    logger.info(f"cad2geojson v{__version__} — export 模式")

    import json as json_module

    try:
        # 读取 GeoJSON 文件
        with open(geojson_file, "r", encoding="utf-8") as f:
            geojson_data = json_module.load(f)

        # 构建导出配置
        config = ExportConfig(
            input_file=geojson_file,
            output_file=output,
            target_crs=target_crs,
            format=fmt,
            default_layer=default_layer,
            oda_path=oda_path,
        )

        out_path = export_geojson_to_dxf(geojson_data, config)
        click.echo(f"\n[OK] 导出成功！输出: {out_path}")

    except (FileNotFoundError, ValueError) as e:
        click.echo(f"\n[FAIL] 错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        logger.exception(e)
        click.echo(f"\n[FAIL] 未预期错误: {e}", err=True)
        sys.exit(1)


# ── render 子命令（GeoJSON → SVG）────────────────────────────────────────────
@cli_group.command("render", help="GeoJSON 文件 → SVG 图形（通过 LLM）。")
@click.argument("geojson_file", type=click.Path(exists=True, readable=True))
@click.option("-o", "--output", type=click.Path(), default=None, help="输出 SVG 文件路径（默认同名 .svg）")
@click.option("--api-key", type=str, default=None, envvar="ANTHROPIC_API_KEY", help="Anthropic API Key（也可用环境变量 ANTHROPIC_API_KEY）")
@click.option("--model", type=str, default="claude-sonnet-4-6", show_default=True, help="Claude 模型 ID")
@click.option("--viewbox", type=int, default=1000, show_default=True, help="SVG 视口尺寸（正方形像素数）")
@click.option("--simplify", type=float, default=0.0, show_default=True, help="D-P 简化容差（0=自动）")
@click.option("-v", "--verbose", is_flag=True, default=False, help="详细日志")
def render_cmd(geojson_file, output, api_key, model, viewbox, simplify, verbose):
    """
    GeoJSON → SVG 渲染子命令。

    示例：
        .venv/Scripts/python -m src.main render output/test.geojson
        .venv/Scripts/python -m src.main render output/test.geojson -o output/test.svg
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    # 确定输出路径
    if output is None:
        output = str(Path(geojson_file).with_suffix(".svg"))

    try:
        pipeline = RenderPipeline(model=model, api_key=api_key, viewbox_size=viewbox, simplify_tol=simplify)
        result = pipeline.run_file(geojson_file)

        # 写入 SVG 文件
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(result.svg, encoding="utf-8")

        status = "通过" if result.is_valid else "有警告"
        click.echo(f"\n[OK] 渲染完成！策略={result.strategy}，校验={status}，耗时={result.elapsed_sec:.1f}s")
        click.echo(f"     输出: {output}")
        if result.warnings:
            for w in result.warnings:
                click.echo(f"     ⚠ {w}", err=True)
        if result.errors:
            for e in result.errors:
                click.echo(f"     ✗ {e}", err=True)
    except FileNotFoundError as e:
        click.echo(f"\n[FAIL] 文件错误: {e}", err=True); sys.exit(1)
    except RuntimeError as e:
        click.echo(f"\n[FAIL] 渲染失败: {e}", err=True); sys.exit(1)
    except Exception as e:
        logger.exception(e); click.echo(f"\n[FAIL] 未预期错误: {e}", err=True); sys.exit(1)


# ── 程序入口 ──────────────────────────────────────────────────────────────────
# 主入口使用 cli_group（支持 convert / render 子命令）
cli = cli_group   # 为了兼容 setup.py entry_points 引用 src.main:cli


if __name__ == "__main__":
    cli_group()
