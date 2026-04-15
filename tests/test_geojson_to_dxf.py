# -*- coding: utf-8 -*-
"""
geojson_to_dxf 模块的单元测试

测试 GeoJSON → DXF 转换功能，包括：
    - Point Feature → POINT 实体
    - LineString Feature → LWPOLYLINE（开放）
    - Polygon Feature（含内环）→ 多个 LWPOLYLINE（闭合）
    - properties.layer 正确映射到 DXF 图层
    - target_crs 坐标反向转换验证
    - Multi* 类型分解写入
    - 往返测试（图层数、实体数）
"""

from pathlib import Path

import ezdxf
import pytest

from src.geojson_to_dxf import ExportConfig, export_geojson_to_dxf


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _make_feature(geom_type: str, coords, layer: str = "TEST") -> dict:
    """创建单个 GeoJSON Feature（用于快速构造测试数据）"""
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": {"layer": layer},
    }


def _make_collection(features: list) -> dict:
    """创建 GeoJSON FeatureCollection"""
    return {"type": "FeatureCollection", "features": features}


# ── Point 测试 ────────────────────────────────────────────────────────────────

class TestPointToDxf:
    """Point Feature → POINT 实体测试"""

    def test_point_creates_point_entity(self, tmp_path):
        """Point Feature 应生成一个 DXF POINT 实体"""
        geojson = _make_collection([
            _make_feature("Point", [120.0, 30.0], layer="POINTS"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        points = [e for e in msp if e.dxftype() == "POINT"]
        assert len(points) == 1

    def test_point_coordinates_preserved(self, tmp_path):
        """Point 坐标应原样写入 DXF（无坐标转换时）"""
        geojson = _make_collection([
            _make_feature("Point", [100.5, 25.3]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        pts = [e for e in msp if e.dxftype() == "POINT"]
        assert len(pts) == 1
        loc = pts[0].dxf.location
        # 坐标精度误差应在 1e-6 以内
        assert abs(loc.x - 100.5) < 1e-6
        assert abs(loc.y - 25.3) < 1e-6

    def test_point_z_coordinate(self, tmp_path):
        """三维 Point 的 Z 值应正确写入"""
        geojson = _make_collection([
            _make_feature("Point", [100.0, 30.0, 50.5]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        pts = [e for e in msp if e.dxftype() == "POINT"]
        assert abs(pts[0].dxf.location.z - 50.5) < 1e-6


# ── LineString 测试 ───────────────────────────────────────────────────────────

class TestLinestringToDxf:
    """LineString Feature → LWPOLYLINE 测试"""

    def test_linestring_creates_open_polyline(self, tmp_path):
        """LineString 应生成开放的 LWPOLYLINE（closed=False）"""
        coords = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
        geojson = _make_collection([
            _make_feature("LineString", coords, layer="LINES"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        lines = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        assert len(lines) == 1
        # LineString 应为开放线段
        assert not lines[0].closed

    def test_linestring_vertex_count(self, tmp_path):
        """LineString 的顶点数应与输入坐标数一致"""
        coords = [[0.0, 0.0], [1.0, 0.0], [2.0, 1.0], [3.0, 0.5]]
        geojson = _make_collection([
            _make_feature("LineString", coords),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        lines = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        assert len(lines) == 1
        assert len(lines[0]) == 4

    def test_linestring_too_few_points_skipped(self, tmp_path):
        """少于 2 个点的 LineString 应被跳过，不生成实体"""
        geojson = _make_collection([
            _make_feature("LineString", [[0.0, 0.0]]),  # 只有 1 个点
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        lines = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        assert len(lines) == 0


# ── Polygon 测试 ──────────────────────────────────────────────────────────────

class TestPolygonToDxf:
    """Polygon Feature → LWPOLYLINE 测试"""

    def test_simple_polygon_is_closed(self, tmp_path):
        """简单多边形应生成一个闭合 LWPOLYLINE"""
        # 正方形外环（GeoJSON Polygon 要求首尾点相同）
        outer = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        geojson = _make_collection([
            _make_feature("Polygon", [outer], layer="POLYS"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        polys = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        assert len(polys) == 1
        # 多边形应为闭合多段线
        assert polys[0].closed

    def test_polygon_with_hole_generates_two_polylines(self, tmp_path):
        """含内环的多边形应生成外环+内环共 2 个 LWPOLYLINE"""
        outer = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
        inner = [[2.0, 2.0], [8.0, 2.0], [8.0, 8.0], [2.0, 8.0], [2.0, 2.0]]
        geojson = _make_collection([
            _make_feature("Polygon", [outer, inner], layer="POLYS"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        polys = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        # 外环 + 内环 = 2 个闭合多段线
        assert len(polys) == 2
        for poly in polys:
            assert poly.closed

    def test_polygon_ring_deduplication(self, tmp_path):
        """Polygon 环的首尾重复点应在 DXF 中去除"""
        # 4 个顶点 + 1 个重复首点 = 5 个坐标点，DXF 应只存 4 个顶点
        outer = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        geojson = _make_collection([
            _make_feature("Polygon", [outer]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        polys = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        # DXF 闭合多段线不需要重复首尾点，应为 4 个顶点
        assert len(polys[0]) == 4


# ── 图层映射测试 ──────────────────────────────────────────────────────────────

class TestLayerAssignment:
    """properties.layer → DXF 图层名映射测试"""

    def test_layer_from_properties(self, tmp_path):
        """Feature 的 properties.layer 应正确创建对应 DXF 图层"""
        geojson = _make_collection([
            _make_feature("Point", [0.0, 0.0], layer="墙体"),
            _make_feature("Point", [1.0, 1.0], layer="门窗"),
            _make_feature("Point", [2.0, 2.0], layer="轴线"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        layer_names = {layer.dxf.name for layer in doc.layers}
        assert "墙体" in layer_names
        assert "门窗" in layer_names
        assert "轴线" in layer_names

    def test_default_layer_when_no_layer_property(self, tmp_path):
        """无 layer 属性的 Feature 应写入 default_layer"""
        geojson = _make_collection([
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                "properties": {},  # 无 layer 字段
            }
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
            default_layer="DEFAULT_LAYER",
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        points = [e for e in msp if e.dxftype() == "POINT"]
        assert len(points) == 1
        assert points[0].dxf.layer == "DEFAULT_LAYER"

    def test_entities_on_correct_layers(self, tmp_path):
        """不同图层的 Feature 应分别写入各自对应的 DXF 图层"""
        geojson = _make_collection([
            _make_feature("Point", [0.0, 0.0], layer="LAYER_A"),
            _make_feature("Point", [1.0, 1.0], layer="LAYER_B"),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        points = [e for e in msp if e.dxftype() == "POINT"]
        layer_set = {p.dxf.layer for p in points}
        assert "LAYER_A" in layer_set
        assert "LAYER_B" in layer_set

    def test_none_layer_uses_default(self, tmp_path):
        """properties.layer 为 None 时应使用 default_layer"""
        geojson = _make_collection([
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                "properties": {"layer": None},
            }
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
            default_layer="0",
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        points = [e for e in msp if e.dxftype() == "POINT"]
        assert points[0].dxf.layer == "0"


# ── 坐标转换测试 ──────────────────────────────────────────────────────────────

class TestCoordinateTransform:
    """target_crs 坐标反向转换（WGS84→工程坐标系）测试"""

    def test_no_transform_preserves_coords(self, tmp_path):
        """不指定 target_crs 时，坐标应原样保留"""
        geojson = _make_collection([
            _make_feature("Point", [120.0, 30.0]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
            target_crs=None,
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        pts = [e for e in msp if e.dxftype() == "POINT"]
        assert abs(pts[0].dxf.location.x - 120.0) < 1e-6
        assert abs(pts[0].dxf.location.y - 30.0) < 1e-6

    def test_with_target_crs_transforms_to_engineering_coords(self, tmp_path):
        """指定 target_crs 时，坐标应转换为工程坐标系（大数值）"""
        # 117°E 30°N 在 UTM 50N（EPSG:32650）中约为 (500000, 3322000)
        geojson = _make_collection([
            _make_feature("Point", [117.0, 30.0]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
            target_crs="EPSG:32650",  # UTM zone 50N
        )
        out_path = export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        pts = [e for e in msp if e.dxftype() == "POINT"]
        assert len(pts) == 1
        loc = pts[0].dxf.location
        # UTM 50N 坐标应在合理范围：x ≈ 500000，y ≈ 3320000
        assert 490000 < loc.x < 510000
        assert 3300000 < loc.y < 3340000

    def test_invalid_target_crs_raises_value_error(self, tmp_path):
        """无效的坐标系编码应抛出 ValueError"""
        geojson = _make_collection([
            _make_feature("Point", [120.0, 30.0]),
        ])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
            target_crs="EPSG:99999999",  # 不存在的 EPSG 编码
        )
        with pytest.raises(ValueError, match="无效的目标坐标系"):
            export_geojson_to_dxf(geojson, config)


# ── 往返测试 ──────────────────────────────────────────────────────────────────

class TestRoundtrip:
    """GeoJSON → DXF 往返验证测试"""

    def test_layer_names_preserved(self, tmp_path):
        """导出后 DXF 中应包含所有原始图层名"""
        geojson = _make_collection([
            _make_feature("Point", [0.0, 0.0], layer="A"),
            _make_feature("LineString", [[0.0, 0.0], [1.0, 1.0]], layer="B"),
            _make_feature(
                "Polygon",
                [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
                layer="C",
            ),
        ])

        out = tmp_path / "round1.dxf"
        config = ExportConfig(input_file="dummy.geojson", output_file=str(out))
        export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(str(out))
        # 排除 DXF 默认图层 "0"，只检查用户定义图层
        user_layers = {l.dxf.name for l in doc.layers if l.dxf.name != "0"}
        assert "A" in user_layers
        assert "B" in user_layers
        assert "C" in user_layers

    def test_entity_count_matches_features(self, tmp_path):
        """导出后实体总数应与 Feature 数一致（简单几何无内环）"""
        geojson = _make_collection([
            _make_feature("Point", [0.0, 0.0]),
            _make_feature("Point", [1.0, 0.0]),
            _make_feature("LineString", [[0.0, 0.0], [1.0, 1.0]]),
        ])

        out = tmp_path / "test.dxf"
        config = ExportConfig(input_file="dummy.geojson", output_file=str(out))
        export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        entities = list(msp)
        # 2 个 POINT + 1 个 LWPOLYLINE = 3 个实体
        assert len(entities) == 3

    def test_multi_geometry_types(self, tmp_path):
        """Multi* 几何类型应正确分解写入多个实体"""
        geojson = _make_collection([
            _make_feature("MultiPoint", [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]),
            _make_feature(
                "MultiLineString",
                [[[0.0, 0.0], [1.0, 1.0]], [[2.0, 2.0], [3.0, 3.0]]],
            ),
        ])

        out = tmp_path / "multi.dxf"
        config = ExportConfig(input_file="dummy.geojson", output_file=str(out))
        export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        points = [e for e in msp if e.dxftype() == "POINT"]
        lines = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        # MultiPoint(3) + MultiLineString(2)
        assert len(points) == 3
        assert len(lines) == 2

    def test_multi_polygon_entity_count(self, tmp_path):
        """MultiPolygon 应写入 polygon 数 * 环数 个 LWPOLYLINE"""
        # 2 个多边形，各 1 个环
        poly1 = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
        poly2 = [[[2.0, 0.0], [3.0, 0.0], [3.0, 1.0], [2.0, 1.0], [2.0, 0.0]]]
        geojson = _make_collection([
            _make_feature("MultiPolygon", [poly1, poly2]),
        ])

        out = tmp_path / "multipoly.dxf"
        config = ExportConfig(input_file="dummy.geojson", output_file=str(out))
        export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        polys = [e for e in msp if e.dxftype() == "LWPOLYLINE"]
        # 2 个多边形各 1 个环 = 2 个 LWPOLYLINE
        assert len(polys) == 2

    def test_geometry_collection(self, tmp_path):
        """GeometryCollection 应递归展开写入各子几何"""
        geojson = _make_collection([
            {
                "type": "Feature",
                "geometry": {
                    "type": "GeometryCollection",
                    "geometries": [
                        {"type": "Point", "coordinates": [0.0, 0.0]},
                        {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]},
                    ],
                },
                "properties": {"layer": "GC_LAYER"},
            }
        ])

        out = tmp_path / "gc.dxf"
        config = ExportConfig(input_file="dummy.geojson", output_file=str(out))
        export_geojson_to_dxf(geojson, config)

        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        entities = list(msp)
        # 1 个 POINT + 1 个 LWPOLYLINE = 2 个实体
        assert len(entities) == 2

    def test_output_path_auto_generated(self, tmp_path):
        """未指定 output_file 时应自动生成输出路径"""
        geojson = _make_collection([
            _make_feature("Point", [0.0, 0.0]),
        ])
        input_path = str(tmp_path / "input.geojson")
        # 写一个占位符文件（ExportConfig 不会实际读取它）
        Path(input_path).write_text("{}")

        config = ExportConfig(
            input_file=input_path,
            output_file=None,  # 不指定输出路径
        )
        out_path = export_geojson_to_dxf(geojson, config)

        # 输出路径应与 input 同目录同名，后缀为 .dxf
        assert out_path.endswith(".dxf")
        assert Path(out_path).is_file()

    def test_invalid_geojson_type_raises_value_error(self, tmp_path):
        """非 FeatureCollection/Feature 类型应抛出 ValueError"""
        geojson = {"type": "Geometry", "coordinates": [0.0, 0.0]}
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "out.dxf"),
        )
        with pytest.raises(ValueError, match="不支持的 GeoJSON 类型"):
            export_geojson_to_dxf(geojson, config)

    def test_empty_feature_collection(self, tmp_path):
        """空 FeatureCollection 应生成有效但空的 DXF 文件"""
        geojson = _make_collection([])
        config = ExportConfig(
            input_file="dummy.geojson",
            output_file=str(tmp_path / "empty.dxf"),
        )
        out_path = export_geojson_to_dxf(geojson, config)

        # 能成功读取，无实体
        doc = ezdxf.readfile(out_path)
        msp = doc.modelspace()
        assert list(msp) == []
