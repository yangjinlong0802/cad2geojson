# -*- coding: utf-8 -*-
"""
geometry_mapper 模块的单元测试

测试 CAD 实体到 GeoJSON 几何类型的映射功能，
包括弧线离散化、bulge 处理和几何合法性校验。
"""

import math

import pytest

from src.dxf_parser import ParsedEntity
from src.geometry_mapper import (
    map_entity_to_geometry,
    bulge_to_arc_points,
    discretize_circle,
    discretize_arc,
    process_polyline_with_bulge,
)


class TestMapPoint:
    """POINT 实体映射测试"""

    def test_point_to_geojson(self):
        """测试 POINT → Point 映射"""
        entity = ParsedEntity(
            entity_type="POINT",
            geometry_data={"type": "POINT", "location": (10.0, 20.0)},
        )
        result = map_entity_to_geometry(entity)
        assert result is not None
        assert result["type"] == "Point"
        assert result["coordinates"] == (10.0, 20.0)


class TestMapLine:
    """LINE 实体映射测试"""

    def test_line_to_linestring(self):
        """测试 LINE → LineString 映射"""
        entity = ParsedEntity(
            entity_type="LINE",
            geometry_data={
                "type": "LINE",
                "start": (0.0, 0.0),
                "end": (10.0, 20.0),
            },
        )
        result = map_entity_to_geometry(entity)
        assert result is not None
        assert result["type"] == "LineString"
        assert len(result["coordinates"]) == 2


class TestMapPolyline:
    """LWPOLYLINE 实体映射测试"""

    def test_open_polyline_to_linestring(self):
        """测试开放多段线 → LineString 映射"""
        entity = ParsedEntity(
            entity_type="LWPOLYLINE",
            geometry_data={
                "type": "LWPOLYLINE",
                "vertices": [(0, 0), (10, 0), (10, 10)],
                "bulges": [0, 0, 0],
                "is_closed": False,
            },
        )
        result = map_entity_to_geometry(entity)
        assert result["type"] == "LineString"

    def test_closed_polyline_to_polygon(self):
        """测试闭合多段线 → Polygon 映射"""
        entity = ParsedEntity(
            entity_type="LWPOLYLINE",
            geometry_data={
                "type": "LWPOLYLINE",
                "vertices": [(0, 0), (10, 0), (10, 10), (0, 10)],
                "bulges": [0, 0, 0, 0],
                "is_closed": True,
            },
        )
        result = map_entity_to_geometry(entity)
        assert result["type"] == "Polygon"


class TestMapCircle:
    """CIRCLE 实体映射测试"""

    def test_circle_to_polygon(self):
        """测试 CIRCLE → Polygon 映射"""
        entity = ParsedEntity(
            entity_type="CIRCLE",
            geometry_data={
                "type": "CIRCLE",
                "center": (5.0, 5.0),
                "radius": 10.0,
            },
        )
        result = map_entity_to_geometry(entity, arc_segments=32)
        assert result["type"] == "Polygon"
        # Polygon 外环的点数 = 分段数 + 1（首尾重合）
        ring = result["coordinates"][0]
        assert len(ring) == 33

    def test_circle_first_last_point_equal(self):
        """测试圆的首尾点是否重合（GeoJSON Polygon 要求）"""
        entity = ParsedEntity(
            entity_type="CIRCLE",
            geometry_data={
                "type": "CIRCLE",
                "center": (0.0, 0.0),
                "radius": 5.0,
            },
        )
        result = map_entity_to_geometry(entity, arc_segments=16)
        ring = result["coordinates"][0]
        # 首尾点应该相同（或非常接近）
        assert abs(ring[0][0] - ring[-1][0]) < 1e-10
        assert abs(ring[0][1] - ring[-1][1]) < 1e-10


class TestMapArc:
    """ARC 实体映射测试"""

    def test_arc_to_linestring(self):
        """测试 ARC → LineString 映射"""
        entity = ParsedEntity(
            entity_type="ARC",
            geometry_data={
                "type": "ARC",
                "center": (0.0, 0.0),
                "radius": 5.0,
                "start_angle": 0.0,
                "end_angle": 90.0,
            },
        )
        result = map_entity_to_geometry(entity, arc_segments=32)
        assert result["type"] == "LineString"
        assert len(result["coordinates"]) == 33  # 32 段 + 1 = 33 个点


class TestMapText:
    """TEXT 实体映射测试"""

    def test_text_to_point(self):
        """测试 TEXT → Point 映射"""
        entity = ParsedEntity(
            entity_type="TEXT",
            text_content="测试文字",
            geometry_data={
                "type": "TEXT",
                "insert": (100.0, 200.0),
                "text": "测试文字",
                "height": 2.5,
                "rotation": 0.0,
            },
        )
        result = map_entity_to_geometry(entity)
        assert result["type"] == "Point"
        assert result["coordinates"] == (100.0, 200.0)


class TestBulgeToArcPoints:
    """bulge 弧线插值测试"""

    def test_zero_bulge(self):
        """测试 bulge=0（直线段）"""
        points = bulge_to_arc_points((0, 0), (10, 0), 0.0)
        assert points == [(10, 0)]

    def test_semicircle_bulge(self):
        """测试 bulge=1（半圆弧）"""
        points = bulge_to_arc_points((0, 0), (10, 0), 1.0, segments=32)
        # 半圆弧的最高点应该在 y ≈ 5 附近
        max_y = max(p[1] for p in points)
        assert max_y > 4.0  # 不要求精确值，确认弧线方向正确即可

    def test_negative_bulge(self):
        """测试负 bulge（顺时针弧）"""
        points = bulge_to_arc_points((0, 0), (10, 0), -1.0, segments=32)
        # 负 bulge 弧线应该朝负 y 方向凸出
        min_y = min(p[1] for p in points)
        assert min_y < -4.0


class TestDiscretizeCircle:
    """圆离散化测试"""

    def test_circle_point_count(self):
        """测试离散化后的点数"""
        points = discretize_circle((0, 0), 5, segments=16)
        # 16 段 + 1 个闭合点 = 17 个点
        assert len(points) == 17

    def test_circle_radius(self):
        """测试离散化后所有点到圆心的距离是否正确"""
        radius = 10.0
        center = (5.0, 5.0)
        points = discretize_circle(center, radius, segments=32)

        for px, py in points:
            dist = math.sqrt((px - center[0]) ** 2 + (py - center[1]) ** 2)
            assert abs(dist - radius) < 1e-10


class TestProcessPolylineWithBulge:
    """多段线 bulge 处理测试"""

    def test_straight_segments(self):
        """测试纯直线段的多段线"""
        vertices = [(0, 0), (10, 0), (10, 10)]
        bulges = [0, 0, 0]
        points = process_polyline_with_bulge(vertices, bulges, is_closed=False)
        assert len(points) == 3

    def test_closed_polyline(self):
        """测试闭合多段线的首尾重合"""
        vertices = [(0, 0), (10, 0), (10, 10), (0, 10)]
        bulges = [0, 0, 0, 0]
        points = process_polyline_with_bulge(vertices, bulges, is_closed=True)
        # 闭合后首尾点应该相同
        assert points[0] == points[-1]
