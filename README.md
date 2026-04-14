# cad2geojson

CAD (DWG/DXF) 文件转 GeoJSON 格式的 Python 命令行工具。

## 功能特性

- 支持 DWG 和 DXF 文件输入
- DWG 文件通过 ODA File Converter 自动转换为 DXF
- 支持多种 CAD 实体类型（LINE, POLYLINE, CIRCLE, ARC, TEXT 等）
- 支持坐标系转换（工程坐标 → WGS84 经纬度）
- 支持按图层过滤和分层输出
- 支持块引用（INSERT）递归展开
- 弧线和曲线自动离散化

## 安装

```bash
# 安装依赖
pip install -r requirements.txt

# 或以开发模式安装
pip install -e .
```

### DWG 支持（可选）

如需转换 DWG 文件，需安装 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（免费）。

## 使用方法

```bash
# 基本用法：转换 DXF 文件
python -m src.main input.dxf

# 指定输出路径
python -m src.main input.dxf -o output.geojson

# 指定源坐标系进行坐标转换
python -m src.main input.dxf --source-crs EPSG:4526

# 按图层分别输出
python -m src.main input.dxf --split-layers

# 只转换指定图层
python -m src.main input.dxf --layers "道路,建筑,绿化"

# 排除指定图层
python -m src.main input.dxf --exclude-layers "标注,辅助线"

# 调整弧线精度
python -m src.main input.dxf --arc-segments 128

# DWG 文件转换（需要 ODA File Converter）
python -m src.main input.dwg --oda-path "C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe"

# 详细日志输出
python -m src.main input.dxf -v
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input_file` | 输入文件路径（.dwg 或 .dxf） | 必需 |
| `-o, --output` | 输出 GeoJSON 文件路径 | 输入文件名.geojson |
| `--source-crs` | 源坐标系 EPSG 编码 | 不转换 |
| `--no-transform` | 不进行坐标转换 | False |
| `--split-layers` | 按图层分别输出 | False |
| `--arc-segments` | 弧线离散化分段数 | 64 |
| `--expand-blocks` | 展开块引用 | True |
| `--oda-path` | ODA File Converter 路径 | 自动检测 |
| `--layers` | 只转换指定图层（逗号分隔） | 全部 |
| `--exclude-layers` | 排除指定图层（逗号分隔） | 无 |
| `-v, --verbose` | 详细日志输出 | False |

## 支持的 CAD 实体类型

| CAD 实体 | GeoJSON 类型 | 状态 |
|----------|-------------|------|
| POINT | Point | ✅ |
| LINE | LineString | ✅ |
| LWPOLYLINE | LineString / Polygon | ✅ |
| POLYLINE | LineString / Polygon | ✅ |
| CIRCLE | Polygon | ✅ |
| ARC | LineString | ✅ |
| ELLIPSE | Polygon / LineString | ✅ |
| SPLINE | LineString | ✅ |
| TEXT | Point | ✅ |
| MTEXT | Point | ✅ |
| HATCH | Polygon / MultiPolygon | ✅ |
| INSERT（块引用） | 递归展开 | ✅ |

## 运行测试

```bash
pytest tests/ -v
```

## 项目结构

```
cad2geojson/
├── config/
│   └── coordinate_systems.json    # 预定义坐标系映射表
├── src/
│   ├── main.py                    # CLI 入口
│   ├── converter.py               # 主转换流程编排
│   ├── dwg_to_dxf.py             # DWG → DXF 转换
│   ├── dxf_parser.py             # DXF 文件解析
│   ├── geometry_mapper.py        # 几何类型映射
│   ├── coordinate_transformer.py # 坐标系转换
│   └── geojson_builder.py        # GeoJSON 组装输出
├── tests/                         # 单元测试
├── output/                        # 默认输出目录
├── requirements.txt
└── setup.py
```

## 技术栈

- Python 3.10+
- ezdxf — DXF 文件解析
- pyproj — 坐标系转换
- shapely — 几何对象处理
- geojson — GeoJSON 生成与校验
- click — 命令行参数解析
- ODA File Converter — DWG → DXF 转换（外部工具）
