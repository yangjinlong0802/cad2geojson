# -*- coding: utf-8 -*-
"""
DWG 转 DXF 模块

通过 subprocess 调用 ODA File Converter（免费的预编译程序）将 DWG 文件转换为 DXF 文件。
ODA File Converter 需要用户自行下载安装，本模块负责找到它并调用。

典型调用方式：
    ODAFileConverter "输入目录" "输出目录" ACAD2018 DXF 0 1 "*.dwg"
"""

import os
import logging
import subprocess
import tempfile
import uuid
import shutil
from pathlib import Path

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)

# ODA File Converter 在不同操作系统上的默认安装路径
# 用户也可以通过环境变量 ODA_CONVERTER_PATH 或命令行参数指定
DEFAULT_ODA_PATHS = [
    # 当前开发机安装路径
    r"E:\ODAConvert\ODAFileConverter.exe",
    # Windows 默认安装路径
    r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    r"C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe",
    # Linux 默认安装路径
    "/usr/bin/ODAFileConverter",
    "/usr/local/bin/ODAFileConverter",
]


def find_oda_converter(custom_path: str = None) -> str:
    """
    查找 ODA File Converter 的可执行文件路径。

    查找优先级：
        1. 用户通过参数传入的自定义路径
        2. 环境变量 ODA_CONVERTER_PATH
        3. 系统默认安装路径列表

    参数:
        custom_path: 用户指定的 ODA File Converter 路径（可选）

    返回:
        ODA File Converter 的完整可执行文件路径

    异常:
        FileNotFoundError: 找不到 ODA File Converter 时抛出
    """
    # 优先使用用户指定的路径
    if custom_path:
        if os.path.isfile(custom_path):
            logger.info(f"使用用户指定的 ODA File Converter 路径: {custom_path}")
            return custom_path
        else:
            raise FileNotFoundError(
                f"指定的 ODA File Converter 路径不存在: {custom_path}"
            )

    # 其次检查环境变量
    env_path = os.environ.get("ODA_CONVERTER_PATH")
    if env_path and os.path.isfile(env_path):
        logger.info(f"通过环境变量找到 ODA File Converter: {env_path}")
        return env_path

    # 最后尝试默认安装路径
    for default_path in DEFAULT_ODA_PATHS:
        if os.path.isfile(default_path):
            logger.info(f"在默认路径找到 ODA File Converter: {default_path}")
            return default_path

    # 所有路径都找不到，给出详细的错误提示
    raise FileNotFoundError(
        "未找到 ODA File Converter。请确保已安装并配置正确的路径。\n"
        "您可以通过以下方式指定路径：\n"
        "  1. 命令行参数: --oda-path <路径>\n"
        "  2. 环境变量: ODA_CONVERTER_PATH=<路径>\n"
        "  3. 将 ODA File Converter 安装到默认路径\n"
        "下载地址: https://www.opendesign.com/guestfiles/oda_file_converter"
    )


def convert_dwg_to_dxf(
    dwg_file_path: str,
    output_dir: str = None,
    oda_path: str = None,
    dxf_version: str = "ACAD2018",
) -> str:
    """
    将 DWG 文件转换为 DXF 文件。

    使用 ODA File Converter 命令行工具进行转换。
    如果输入文件本身就是 DXF 格式，则直接返回该文件路径，跳过转换。

    参数:
        dwg_file_path: 输入的 DWG 文件路径
        output_dir:    输出目录（可选，默认使用临时目录）
        oda_path:      ODA File Converter 的安装路径（可选）
        dxf_version:   输出的 DXF 版本（默认 ACAD2018）

    返回:
        转换后的 DXF 文件路径

    异常:
        FileNotFoundError: DWG 文件不存在或 ODA File Converter 未安装
        RuntimeError: 转换过程中发生错误
    """
    dwg_path = Path(dwg_file_path).resolve()

    # 检查输入文件是否存在
    if not dwg_path.is_file():
        raise FileNotFoundError(f"输入文件不存在: {dwg_path}")

    # 如果输入文件已经是 DXF 格式，直接返回，无需转换
    if dwg_path.suffix.lower() == ".dxf":
        logger.info(f"输入文件已经是 DXF 格式，跳过转换: {dwg_path}")
        return str(dwg_path)

    # 检查文件扩展名是否为 DWG
    if dwg_path.suffix.lower() != ".dwg":
        raise ValueError(f"不支持的文件格式: {dwg_path.suffix}，仅支持 .dwg 和 .dxf")

    # 查找 ODA File Converter
    converter_path = find_oda_converter(oda_path)

    # 确定输出目录：用户指定或创建临时目录
    # 使用 uuid 保证目录名唯一，避免并发冲突
    use_temp_dir = output_dir is None
    if use_temp_dir:
        output_dir = os.path.join(tempfile.gettempdir(), f"cad2geojson_{uuid.uuid4().hex[:8]}")

    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"输出目录: {output_dir}")

    # 构建 ODA File Converter 命令行参数
    # 参数说明：
    #   输入目录  输出目录  DXF版本  输出格式  递归(0=否)  审计(1=是)  文件过滤
    input_dir = str(dwg_path.parent)
    input_filename = dwg_path.name

    cmd = [
        converter_path,
        input_dir,       # 输入目录
        output_dir,      # 输出目录
        dxf_version,     # 输出 DXF 版本，如 ACAD2018
        "DXF",           # 输出格式为 DXF
        "0",             # 不递归子目录
        "1",             # 开启审计修复（自动修复文件中的错误）
        input_filename,  # 只转换指定的 DWG 文件
    ]

    logger.info(f"执行转换命令: {' '.join(cmd)}")

    try:
        # 调用 ODA File Converter，设置超时 300 秒（大文件可能需要较长时间）
        result = subprocess.run(
            cmd,
            capture_output=True,  # 捕获标准输出和错误输出
            text=True,            # 以文本模式读取输出
            timeout=300,          # 超时时间 300 秒
            check=False,          # 不自动抛出异常，手动检查返回码
        )

        # 记录命令输出（用于调试）
        if result.stdout:
            logger.debug(f"ODA 标准输出: {result.stdout}")
        if result.stderr:
            logger.warning(f"ODA 错误输出: {result.stderr}")

        # 检查返回码
        if result.returncode != 0:
            raise RuntimeError(
                f"ODA File Converter 转换失败，返回码: {result.returncode}\n"
                f"错误信息: {result.stderr or '无'}"
            )

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ODA File Converter 转换超时（超过 300 秒），文件可能过大: {dwg_path}"
        )
    except OSError as e:
        raise RuntimeError(f"无法执行 ODA File Converter: {e}")

    # 查找转换后的 DXF 文件
    # ODA File Converter 会将 .dwg 后缀替换为 .dxf
    expected_dxf_name = dwg_path.stem + ".dxf"
    dxf_file_path = os.path.join(output_dir, expected_dxf_name)

    if not os.path.isfile(dxf_file_path):
        raise RuntimeError(
            f"转换完成但未找到输出文件: {dxf_file_path}\n"
            f"请检查 ODA File Converter 是否正常工作"
        )

    logger.info(f"DWG 转 DXF 成功: {dxf_file_path}")
    return dxf_file_path


def cleanup_temp_dir(dir_path: str) -> None:
    """
    清理转换过程中创建的临时目录。

    参数:
        dir_path: 需要清理的临时目录路径
    """
    try:
        if os.path.isdir(dir_path) and "cad2geojson_" in dir_path:
            shutil.rmtree(dir_path)
            logger.debug(f"已清理临时目录: {dir_path}")
    except OSError as e:
        # 清理失败不应阻断主流程，仅记录警告
        logger.warning(f"清理临时目录失败: {dir_path}, 原因: {e}")
