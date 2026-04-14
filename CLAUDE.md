# CLAUDE.md - cad2geojson 项目指南

## 项目简介
Python CLI 工具，将 CAD (DWG/DXF) 文件转换为 GeoJSON 格式。

## 技术栈
- Python 3.12, ezdxf, pyproj, shapely, geojson, click, flask, fiona(GDAL)
- 双解析引擎: ezdxf(精细控制) + GDAL/fiona(兼容性好), auto 模式自动选最优
- DWG 转换依赖外部工具 ODA File Converter（已安装）
- 测试框架: pytest
- Web 前端: Flask

## 项目结构
```
src/
  main.py                 # CLI 入口 (click)
  converter.py            # 主转换流程编排
  dwg_to_dxf.py           # DWG -> DXF (调用 ODA File Converter)
  dxf_parser.py           # DXF 解析 - ezdxf 引擎
  gdal_parser.py          # DXF 解析 - GDAL/fiona 引擎
  geometry_mapper.py      # CAD实体 -> GeoJSON几何类型映射
  coordinate_transformer.py  # 坐标系转换 (pyproj)
  geojson_builder.py      # GeoJSON 组装与输出
web/
  app.py                  # Flask Web 服务 (后端 API)
  templates/index.html    # Web 前端页面
tests/                    # 单元测试 (pytest)
config/                   # 坐标系配置
samples/                  # 测试用 CAD 文件
output/                   # 默认输出目录
```

## 转换流程
DWG -> DXF (ODA) -> 解析实体 (ezdxf 或 GDAL/fiona) -> 几何映射 (shapely) -> 坐标转换 (pyproj) -> GeoJSON 输出

auto 模式下两个引擎都跑，按图层级别合并，每个图层取 Feature 数更多的引擎结果

## 开发规范
- **代码注释必须用中文**，注释要多写
- 运行测试: `.venv/Scripts/python -m pytest tests/ -v`
- 运行工具: `.venv/Scripts/python -m src.main <input_file> [options]`
- 虚拟环境在 `.venv/`，已安装所有依赖

## 常用命令
```bash
# 启动 Web 界面（推荐）
.venv/Scripts/python -m web.app
# 然后浏览器打开 http://localhost:5000

# 命令行方式（默认 auto 引擎）
.venv/Scripts/python -m src.main samples/test.dxf -o output/test.geojson

# 指定引擎
.venv/Scripts/python -m src.main samples/test.dxf --engine ezdxf
.venv/Scripts/python -m src.main samples/test.dxf --engine gdal

# 运行测试
.venv/Scripts/python -m pytest tests/ -v
```

## 注意事项
- Windows 环境，shell 用 Git Bash (Unix 语法)
- DWG 支持需要安装 ODA File Converter（已安装于 E:\ODAConvert\）
- GeoJSON 坐标系必须是 WGS84 (EPSG:4326)
- 弧线/圆需要离散化为折线/多边形近似

## 开发进度

### 已完成
- 项目基础架构搭建（2026-03-26）
- CLI 入口 (click) + 完整参数体系
- DXF 解析：基本图元(LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, POINT, TEXT, MTEXT, ELLIPSE, SPLINE, HATCH, SOLID, 3DFACE) + INSERT(explode展开) + 复合实体(DIMENSION, MULTILEADER, LEADER, MLINE 用virtual_entities分解) + 未知类型(geo.proxy fallback)
- 几何映射：所有实体类型 → GeoJSON 几何类型，含 bulge 弧线插值、曲线离散化、几何校验修复
- 坐标转换：pyproj 实现工程坐标系 → WGS84
- GeoJSON 输出：单文件 / 按图层分文件输出
- DWG → DXF 转换模块（依赖 ODA File Converter）
- 单元测试 32 个全部通过
- 依赖全部安装到 .venv/
- Web 前端（Flask）：上传文件、配置参数、一键转换下载（2026-03-26）
- ODA File Converter 已安装，DWG→DXF 转换正常工作
- 新增 SOLID, 3DFACE, DIMENSION, MULTILEADER 实体支持（2026-03-27）
- 借鉴 L5IN_task1 项目改进架构（2026-03-27）：
  - 用 ezdxf.recover.readfile() 替代 ezdxf.readfile()，增强容错性
  - 用 ezdxf explode() 替代手写递归展开+坐标变换，更可靠
  - 用 ezdxf.addons.geo.proxy() 作为未知实体类型的 fallback
  - 新增 LEADER, MLINE 复合实体分解支持
- 17 个真实 DWG 样本文件端到端测试全部通过（2026-03-27）
- 双引擎架构：ezdxf + GDAL/fiona（2026-03-27）
  - 新建 gdal_parser.py，使用 fiona 读取 DXF 直接输出 GeoJSON
  - converter.py 支持 engine 参数 (ezdxf/gdal/auto)
  - CLI 添加 --engine 选项，Web 前端添加引擎选择下拉框
  - 修复 HATCH EllipseEdge 缺少 radius 属性的 bug
- 双引擎按图层合并策略（2026-03-27）：
  - auto 模式升级: 不再简单取最优引擎，改为按图层级别合并
  - 每个图层分别对比两个引擎的 Feature 数，取更多的那个
  - 实测 7 个样本文件: 合并 8657 > ezdxf 7932 > GDAL 4485，比最优单引擎多 9.1%
  - architectural 文件提升最显著: 111→180 (+62.2%)

- 转换诊断报告功能（2026-04-07）：
  - 新增 EntityTypeStats 统计类，跟踪每种实体类型的解析成功/失败/原因
  - convert() 返回 ConversionResult 对象（含输出路径 + 诊断统计）
  - CLI 自动输出诊断报告表格（按成功率排序，失败原因缩进显示）
  - Web API 返回 diagnostics 字段（JSON 格式的细粒度统计）
  - 统计维度: 实体类型/总数/成功/失败/成功率/处理方式/失败原因

- 大批量失败实体类型修复（2026-04-08）：
  - 新增原生解析：RAY/XLINE（→Point）、TOLERANCE/SHAPE/ACAD_TABLE（→Point）
  - 新增原生解析：IMAGE（→边界Polygon）、PDFUNDERLAY/PDFREFERENCE（→Point）
  - 新增原生解析：HELIX（→投影圆Polygon）、MESH（→面片MultiPolygon）
  - ARC_DIMENSION 添加到复合实体分解（virtual_entities()），与 DIMENSION 同路径
  - 新增 _ACIS_ENTITY_TYPES 集合（3DSOLID/REGION/EXTRUDEDSURFACE 等）：
    - 从 geo.proxy() 失败改为显式跳过，诊断原因更精确
    - 统计方式标签改为"ACIS不支持"，告知用户需要 ACIS 内核
  - XLINE 属性名修正：ezdxf 中使用 dxf.start 而非 dxf.point

### 当前状态
- CLI + Web 双入口均已完成，支持双解析引擎+按图层合并
- 32 个单元测试全部通过
- 实体支持覆盖率大幅提升（RAY/XLINE/TOLERANCE/SHAPE/IMAGE/HELIX/MESH/ACAD_TABLE 等均已支持）
- ACIS 类实体（3DSOLID/REGION/EXTRUDEDSURFACE 等）明确标注为不支持，诊断更准确

### 待办
- [ ] 样式信息保留（颜色RGB/线宽/线型 → GeoJSON properties）
- [ ] 块结构语义保留（INSERT 信息 + explode 几何同时输出）
- [ ] XRef 外部引用支持
- [ ] 3DSOLID / PLANESURFACE / REGION 支持（需要 ACIS 几何内核，技术难度高）
