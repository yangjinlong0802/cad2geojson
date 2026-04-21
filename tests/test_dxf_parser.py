# -*- coding: utf-8 -*-
"""
dxf_parser 模块的单元测试

测试 DXF 文件解析功能，包括各种实体类型的解析和图层过滤。
由于测试需要实际的 DXF 文件，部分测试使用 ezdxf 动态创建测试用文件。
"""

import math
import os
import tempfile

import ezdxf
import pytest

from src.dxf_parser import (
    parse_dxf,
    parse_single_entity,
    parse_line,
    parse_lwpolyline,
    parse_circle,
    parse_arc,
    parse_point,
    parse_text,
    ParsedEntity,
)


def create_test_dxf(entities_func) -> str:
    """
    创建一个临时的测试 DXF 文件。

    使用 ezdxf 动态创建 DXF 文件，通过 entities_func 回调函数
    让调用者自定义要添加的实体。

    参数:
        entities_func: 回调函数，接收 modelspace 参数，用于添加实体

    返回:
        临时 DXF 文件的路径
    """
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    # 调用回调函数添加实体
    entities_func(msp)

    # 保存到临时文件
    temp_file = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    temp_file.close()
    doc.saveas(temp_file.name)
    return temp_file.name


class TestParseLine:
    """LINE 实体解析测试"""

    def test_parse_basic_line(self):
        """测试基本直线解析"""
        def add_entities(msp):
            msp.add_line((0, 0), (10, 20))

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            assert len(entities) == 1
            entity = entities[0]
            assert entity.entity_type == "LINE"
            assert entity.geometry_data["start"] == (0.0, 0.0)
            assert entity.geometry_data["end"] == (10.0, 20.0)
        finally:
            os.unlink(dxf_path)

    def test_parse_line_layer(self):
        """测试直线的图层属性"""
        def add_entities(msp):
            msp.add_line((0, 0), (5, 5), dxfattribs={"layer": "测试图层"})

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            assert entities[0].layer == "测试图层"
        finally:
            os.unlink(dxf_path)


class TestParseLwpolyline:
    """LWPOLYLINE 实体解析测试"""

    def test_parse_open_polyline(self):
        """测试开放多段线解析"""
        def add_entities(msp):
            msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)])

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            assert len(entities) == 1
            entity = entities[0]
            assert entity.entity_type == "LWPOLYLINE"
            assert len(entity.geometry_data["vertices"]) == 3
            assert entity.geometry_data["is_closed"] is False
        finally:
            os.unlink(dxf_path)

    def test_parse_closed_polyline(self):
        """测试闭合多段线解析"""
        def add_entities(msp):
            msp.add_lwpolyline(
                [(0, 0), (10, 0), (10, 10), (0, 10)],
                close=True,
            )

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            assert entities[0].geometry_data["is_closed"] is True
        finally:
            os.unlink(dxf_path)


class TestParseCircle:
    """CIRCLE 实体解析测试"""

    def test_parse_circle(self):
        """测试圆的解析"""
        def add_entities(msp):
            msp.add_circle((5, 5), radius=10)

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            entity = entities[0]
            assert entity.entity_type == "CIRCLE"
            assert entity.geometry_data["center"] == (5.0, 5.0)
            assert entity.geometry_data["radius"] == 10.0
        finally:
            os.unlink(dxf_path)


class TestParseArc:
    """ARC 实体解析测试"""

    def test_parse_arc(self):
        """测试圆弧的解析"""
        def add_entities(msp):
            msp.add_arc(
                center=(0, 0),
                radius=5,
                start_angle=0,
                end_angle=90,
            )

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            entity = entities[0]
            assert entity.entity_type == "ARC"
            assert entity.geometry_data["radius"] == 5.0
            assert entity.geometry_data["start_angle"] == 0.0
            assert entity.geometry_data["end_angle"] == 90.0
        finally:
            os.unlink(dxf_path)


class TestParseText:
    """TEXT 实体解析测试"""

    def test_parse_text(self):
        """测试单行文字解析"""
        def add_entities(msp):
            msp.add_text(
                "测试文字",
                dxfattribs={"insert": (10, 20), "height": 2.5},
            )

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            entity = entities[0]
            assert entity.entity_type == "TEXT"
            assert entity.text_content == "测试文字"
            assert entity.geometry_data["insert"] == (10.0, 20.0)
        finally:
            os.unlink(dxf_path)


class TestLayerFilter:
    """图层过滤测试"""

    def test_filter_include_layers(self):
        """测试指定图层过滤（只包含）"""
        def add_entities(msp):
            msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "道路"})
            msp.add_line((0, 0), (2, 2), dxfattribs={"layer": "建筑"})
            msp.add_line((0, 0), (3, 3), dxfattribs={"layer": "绿化"})

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path, layers=["道路", "建筑"])
            assert len(entities) == 2
            layers = {e.layer for e in entities}
            assert layers == {"道路", "建筑"}
        finally:
            os.unlink(dxf_path)

    def test_filter_exclude_layers(self):
        """测试排除图层过滤"""
        def add_entities(msp):
            msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "道路"})
            msp.add_line((0, 0), (2, 2), dxfattribs={"layer": "标注"})
            msp.add_line((0, 0), (3, 3), dxfattribs={"layer": "辅助线"})

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path, exclude_layers=["标注", "辅助线"])
            assert len(entities) == 1
            assert entities[0].layer == "道路"
        finally:
            os.unlink(dxf_path)


class TestMultipleEntities:
    """混合实体解析测试"""

    def test_parse_multiple_entity_types(self):
        """测试同时解析多种实体类型"""
        def add_entities(msp):
            msp.add_line((0, 0), (10, 10))
            msp.add_circle((5, 5), radius=3)
            msp.add_point((1, 1))
            msp.add_text("Hello", dxfattribs={"insert": (0, 0), "height": 1})

        dxf_path = create_test_dxf(add_entities)
        try:
            entities = parse_dxf(dxf_path)
            types = {e.entity_type for e in entities}
            assert "LINE" in types
            assert "CIRCLE" in types
            assert "POINT" in types
            assert "TEXT" in types
        finally:
            os.unlink(dxf_path)
