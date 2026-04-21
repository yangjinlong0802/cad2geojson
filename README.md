# cad2geojson

Python CLI + Web 工具，支持 CAD (DWG/DXF) 与 GeoJSON 之间的双向转换，并可通过 LLM 将 GeoJSON 渲染为 SVG 图形。

## 功能特性

**CAD → GeoJSON**
- 支持 DWG 和 DXF 文件输入
- DWG 文件通过 ODA File Converter 自动转换为 DXF
- 双解析引擎：ezdxf（精细控制）+ GDAL/fiona（兼容性好），auto 模式按图层合并取最优
- 支持丰富的 CAD 实体类型（LINE, POLYLINE, CIRCLE, ARC, HATCH, INSERT 块引用等）
- 支持坐标系转换（工程坐标 → WGS84 经纬度）
- 支持按图层过滤和分层输出
- 弧线、曲线自动离散化

**GeoJSON → DXF/DWG（反向导出）**
- 所有 GeoJSON 几何类型 → DXF 实体（Point/LineString/Polygon/Multi*/GeometryCollection）
- 按 `properties.layer` 属性自动分图层
- 支持坐标反向转换（WGS84 → 工程坐标系）
- 支持直接导出为 DWG（需要 ODA File Converter）

**GeoJSON → SVG**
- 通过 Claude LLM 将 GeoJSON 渲染为矢量 SVG 图形
- 自动按数据量选择渲染策略（A/B/C/D 四档）

**Web 界面**
- 基于 Flask，支持上传 CAD 文件、配置参数、一键转换
- 转换完成后展示图层统计，支持下载 GeoJSON、导出 DXF/DWG

## 安装

```bash
# 安装依赖到虚拟环境
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

### DWG 支持（可选）

如需转换 DWG 文件，需安装 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（免费）。

## 快速开始

### Web 界面（推荐）

```bash
.venv/Scripts/python -m web.app
# 浏览器打开 http://localhost:5000
```

### 命令行

```bash
# CAD → GeoJSON
.venv/Scripts/python -m src.main convert input.dxf -o output.geojson

# GeoJSON → DXF（反向导出）
.venv/Scripts/python -m src.main export output.geojson -o output.dxf

# GeoJSON → SVG（需要 ANTHROPIC_API_KEY）
export ANTHROPIC_API_KEY=sk-ant-...
.venv/Scripts/python -m src.main render output.geojson
```

## 子命令详情

### `convert` — CAD → GeoJSON

```bash
.venv/Scripts/python -m src.main convert <input_file> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input_file` | 输入文件（.dwg 或 .dxf） | 必需 |
| `-o, --output` | 输出 GeoJSON 路径 | 同名 .geojson |
| `--source-crs` | 源坐标系 EPSG 编码（如 EPSG:4526） | 不转换 |
| `--no-transform` | 禁用坐标转换，使用原始坐标 | False |
| `--split-layers` | 按图层分别输出多个文件 | False |
| `--arc-segments` | 弧线离散化分段数 | 64 |
| `--expand-blocks` | 展开块引用（INSERT） | True |
| `--engine` | 解析引擎：auto / ezdxf / gdal | auto |
| `--layers` | 只转换指定图层（逗号分隔） | 全部 |
| `--exclude-layers` | 排除指定图层（逗号分隔） | 无 |
| `--oda-path` | ODA File Converter 路径 | 自动检测 |
| `-v, --verbose` | 详细日志 | False |

### `export` — GeoJSON → DXF/DWG

```bash
.venv/Scripts/python -m src.main export <geojson_file> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `geojson_file` | 输入 GeoJSON 文件 | 必需 |
| `-o, --output` | 输出文件路径 | 同名 .dxf/.dwg |
| `--format` | 输出格式：dxf / dwg | dxf |
| `--target-crs` | 目标坐标系（WGS84→工程坐标，如 EPSG:4526） | 不转换 |
| `--default-layer` | 无 layer 属性时的默认图层名 | 0 |
| `--oda-path` | ODA File Converter 路径（DWG 输出时使用） | 自动检测 |
| `-v, --verbose` | 详细日志 | False |

示例：

```bash
# 导出为 DXF（默认）
.venv/Scripts/python -m src.main export output/test.geojson

# 导出为 DWG
.venv/Scripts/python -m src.main export output/test.geojson --format dwg -o output/test.dwg

# 反向坐标转换（WGS84 → 工程坐标系）
.venv/Scripts/python -m src.main export output/test.geojson --target-crs EPSG:4526
```

### `render` — GeoJSON → SVG

```bash
.venv/Scripts/python -m src.main render <geojson_file> [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `geojson_file` | 输入 GeoJSON 文件 | 必需 |
| `-o, --output` | 输出 SVG 路径 | 同名 .svg |
| `--api-key` | Anthropic API Key（或设置环境变量） | 环境变量 |
| `--model` | Claude 模型 ID | claude-sonnet-4-6 |
| `--viewbox` | SVG 视口像素尺寸 | 1000 |
| `-v, --verbose` | 详细日志 | False |

## 支持的 CAD 实体类型

| CAD 实体 | GeoJSON 类型 |
|----------|-------------|
| POINT | Point |
| LINE | LineString |
| LWPOLYLINE | LineString / Polygon |
| POLYLINE | LineString / Polygon |
| CIRCLE | Polygon |
| ARC | LineString |
| ELLIPSE | Polygon / LineString |
| SPLINE | LineString |
| TEXT / MTEXT | Point |
| HATCH | Polygon / MultiPolygon |
| SOLID / 3DFACE | Polygon |
| INSERT（块引用） | 递归展开 |
| DIMENSION / MULTILEADER / LEADER / MLINE | virtual_entities 分解 |

## GeoJSON → DXF 几何映射

| GeoJSON 类型 | DXF 实体 |
|---|---|
| Point | POINT |
| LineString | LWPOLYLINE（开放） |
| Polygon | LWPOLYLINE（闭合）× N（外环 + 各内环） |
| MultiPoint | 多个 POINT |
| MultiLineString | 多个 LWPOLYLINE（开放） |
| MultiPolygon | 多个 LWPOLYLINE（闭合） |
| GeometryCollection | 递归展开处理 |

## Web API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web 界面首页 |
| `/convert` | POST | 上传 CAD 文件 → 返回 GeoJSON + 统计信息 |
| `/export` | POST | GeoJSON → DXF/DWG 文件下载 |
| `/render` | POST | GeoJSON → SVG（通过 LLM） |
| `/download/<task_id>` | GET | 下载转换结果 GeoJSON |

`POST /export` 请求体示例：
```json
{
  "geojson": { "type": "FeatureCollection", "features": [...] },
  "format": "dxf",
  "target_crs": "EPSG:4526"
}
```

## 运行测试

```bash
.venv/Scripts/python -m pytest tests/ -v
```

目前共 56 个单元测试，全部通过。

## 项目结构

```
cad2geojson/
├── src/
│   ├── main.py                    # CLI 入口（convert / export / render 子命令）
│   ├── converter.py               # CAD→GeoJSON 主流程编排
│   ├── dwg_to_dxf.py             # DWG → DXF（ODA File Converter）
│   ├── dxf_parser.py             # DXF 解析 — ezdxf 引擎
│   ├── gdal_parser.py            # DXF 解析 — GDAL/fiona 引擎
│   ├── geojson_to_dxf.py         # GeoJSON → DXF/DWG 反向导出
│   ├── geometry_mapper.py        # CAD 实体 → GeoJSON 几何类型映射
│   ├── coordinate_transformer.py # 坐标系转换（pyproj）
│   ├── geojson_builder.py        # GeoJSON 组装与输出
│   └── renderer/                 # GeoJSON → SVG 渲染管线（LLM）
│       ├── pipeline.py
│       ├── preprocessor.py
│       ├── semantic_labeler.py
│       ├── size_assessor.py
│       ├── prompt_builder.py
│       ├── llm_client.py
│       ├── svg_validator.py
│       └── chunker.py
├── web/
│   ├── app.py                    # Flask Web 服务
│   └── templates/index.html      # Web 前端页面
├── tests/                        # 单元测试（pytest）
├── config/
│   └── coordinate_systems.json   # 预定义坐标系映射表
├── samples/                      # 测试用 CAD 文件
└── output/                       # 默认输出目录
```

## 技术栈

- Python 3.12
- ezdxf — DXF 文件解析与生成
- GDAL / fiona — DXF 解析（备用引擎）
- pyproj — 坐标系转换
- shapely — 几何对象处理
- geojson — GeoJSON 生成与校验
- click — 命令行参数解析
- Flask — Web 服务
- anthropic — Claude LLM API（SVG 渲染）
- ODA File Converter — DWG ↔ DXF 转换（外部工具，免费）
