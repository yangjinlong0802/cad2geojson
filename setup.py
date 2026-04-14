# -*- coding: utf-8 -*-
"""
cad2geojson 安装配置

用于将项目安装为可执行的 Python 包，支持 pip install 方式安装。
"""

from setuptools import setup, find_packages

# 读取 requirements.txt 中的依赖列表
with open("requirements.txt", "r", encoding="utf-8") as f:
    # 过滤掉注释行和空行
    requirements = [
        line.strip()
        for line in f
        if line.strip() and not line.startswith("#")
    ]

# 读取 README 作为长描述
with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="cad2geojson",
    version="0.1.0",
    description="CAD (DWG/DXF) 文件转 GeoJSON 格式的命令行工具",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="cad2geojson",
    python_requires=">=3.10",
    packages=find_packages(),  # 自动发现所有 Python 包
    install_requires=requirements,  # 从 requirements.txt 读取依赖
    entry_points={
        # 注册命令行入口点，安装后可直接使用 cad2geojson 命令
        "console_scripts": [
            "cad2geojson=src.main:cli",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
