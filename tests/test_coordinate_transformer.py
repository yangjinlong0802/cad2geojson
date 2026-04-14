# -*- coding: utf-8 -*-
"""
coordinate_transformer 模块的单元测试

测试坐标转换功能，包括：
    - 单点坐标转换
    - GeoJSON 几何对象的坐标转换
    - 不转换模式
    - 无效坐标系处理
"""

import pytest

from src.coordinate_transformer import (
    CoordinateTransformer,
    create_transformer,
    transform_point,
    transform_geometry,
)


class TestCoordinateTransformer:
    """CoordinateTransformer 类的测试"""

    def test_no_transform_mode(self):
        """测试不转换模式：坐标原样返回"""
        ct = CoordinateTransformer(no_transform=True)
        geometry = {
            "type": "Point",
            "coordinates": (500000.0, 3000000.0),
        }
        result = ct.transform(geometry)
        # 不转换模式下，坐标应该不变
        assert result["coordinates"] == (500000.0, 3000000.0)

    def test_no_source_crs_defaults_to_no_transform(self):
        """测试未指定源坐标系时默认不转换"""
        ct = CoordinateTransformer()
        assert ct.no_transform is True

    def test_transform_point_geometry(self):
        """测试 Point 几何对象的坐标转换"""
        # 使用 UTM 50N → WGS84 进行测试
        ct = CoordinateTransformer(source_crs="EPSG:32650")
        geometry = {
            "type": "Point",
            "coordinates": (500000.0, 4000000.0),
        }
        result = ct.transform(geometry)
        # 转换后应该得到经纬度坐标
        lon, lat = result["coordinates"]
        # UTM 50N 中心经度为 117°，500000 是中央经线处
        assert 116.0 < lon < 118.0
        assert 35.0 < lat < 37.0

    def test_transform_linestring_geometry(self):
        """测试 LineString 几何对象的坐标转换"""
        ct = CoordinateTransformer(source_crs="EPSG:32650")
        geometry = {
            "type": "LineString",
            "coordinates": [
                (500000.0, 4000000.0),
                (500100.0, 4000100.0),
            ],
        }
        result = ct.transform(geometry)
        assert result["type"] == "LineString"
        assert len(result["coordinates"]) == 2
        # 每个坐标都应该被转换
        for coord in result["coordinates"]:
            assert 116.0 < coord[0] < 118.0
            assert 35.0 < coord[1] < 37.0

    def test_transform_polygon_geometry(self):
        """测试 Polygon 几何对象的坐标转换"""
        ct = CoordinateTransformer(source_crs="EPSG:32650")
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                (500000.0, 4000000.0),
                (500100.0, 4000000.0),
                (500100.0, 4000100.0),
                (500000.0, 4000100.0),
                (500000.0, 4000000.0),
            ]],
        }
        result = ct.transform(geometry)
        assert result["type"] == "Polygon"
        # 外环应该有 5 个点
        assert len(result["coordinates"][0]) == 5


class TestInvalidCRS:
    """无效坐标系处理测试"""

    def test_invalid_epsg_code(self):
        """测试无效的 EPSG 编码应该抛出异常"""
        with pytest.raises(Exception):
            CoordinateTransformer(source_crs="EPSG:999999")


class TestTransformPoint:
    """单点坐标转换测试"""

    def test_transform_single_point(self):
        """测试单个坐标点的转换"""
        transformer = create_transformer("EPSG:32650")
        lon, lat = transform_point(transformer, 500000.0, 4000000.0)
        assert isinstance(lon, float)
        assert isinstance(lat, float)
        # 确认结果在合理范围内
        assert -180 <= lon <= 180
        assert -90 <= lat <= 90
