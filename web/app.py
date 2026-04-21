# -*- coding: utf-8 -*-
"""
Web 前端服务模块

基于 Flask 提供简单的 Web 界面，用户可以通过浏览器上传 CAD 文件、
选择转换参数、执行转换并下载 GeoJSON 结果。

启动方式：
    python -m web.app
"""

import os
import uuid
import shutil
import logging
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    send_file,
    jsonify,
    after_this_request,
)

# 导入转换模块
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.converter import ConversionConfig, convert
from src.geojson_to_dxf import ExportConfig, export_geojson_to_dxf
from src.renderer import RenderPipeline

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)

# 项目根目录
BASE_DIR = Path(__file__).parent.parent

# Flask 应用实例
app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)

# 上传文件大小限制：100MB
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

# 临时文件目录
UPLOAD_DIR = BASE_DIR / "output" / "uploads"
RESULT_DIR = BASE_DIR / "output" / "results"

# 允许上传的文件扩展名
ALLOWED_EXTENSIONS = {".dxf", ".dwg"}


def allowed_file(filename: str) -> bool:
    """检查文件扩展名是否合法"""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    """首页：渲染上传和参数配置页面"""
    return render_template("index.html")


@app.route("/convert", methods=["POST"])
def convert_file():
    """
    转换接口：接收上传的 CAD 文件和参数，执行转换，返回结果文件。

    表单参数：
        file:          上传的 CAD 文件
        source_crs:    源坐标系 EPSG 编码（可选）
        arc_segments:  弧线分段数（默认 64）
        split_layers:  是否按图层分割（复选框）
        expand_blocks: 是否展开块引用（复选框）
        layers:        只转换的图层（可选）
        exclude_layers: 排除的图层（可选）
    """
    # 检查是否上传了文件
    logger.info(f"收到转换请求，files: {list(request.files.keys())}, form: {list(request.form.keys())}")
    if "file" not in request.files:
        return jsonify({"error": "未选择文件（表单中未找到 file 字段）"}), 400

    file = request.files["file"]
    logger.info(f"文件名: '{file.filename}', content_type: {file.content_type}")
    if file.filename == "":
        return jsonify({"error": "未选择文件（文件名为空）"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"不支持的文件格式 '{Path(file.filename).suffix}'，仅支持 .dxf 和 .dwg"}), 400

    # 创建本次转换的临时目录（用 uuid 隔离）
    task_id = uuid.uuid4().hex[:8]
    upload_dir = UPLOAD_DIR / task_id
    result_dir = RESULT_DIR / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 保存上传文件
        original_name = Path(file.filename).name
        input_path = upload_dir / original_name
        file.save(str(input_path))
        logger.info(f"文件已上传: {input_path}")

        # 读取表单参数
        source_crs = request.form.get("source_crs", "").strip() or None
        arc_segments = int(request.form.get("arc_segments", 64))
        split_layers = request.form.get("split_layers") == "on"
        expand_blocks = request.form.get("expand_blocks", "on") == "on"
        layers = request.form.get("layers", "").strip() or None
        exclude_layers = request.form.get("exclude_layers", "").strip() or None
        engine = request.form.get("engine", "auto").strip() or "auto"
        no_transform = source_crs is None

        # 输出文件路径
        output_name = Path(original_name).stem + ".geojson"
        output_path = str(result_dir / output_name)

        # 创建转换配置
        config = ConversionConfig(
            input_file=str(input_path),
            output_file=output_path,
            source_crs=source_crs,
            no_transform=no_transform,
            split_layers=split_layers,
            arc_segments=arc_segments,
            expand_blocks=expand_blocks,
            layers=layers,
            exclude_layers=exclude_layers,
            engine=engine,
        )

        # 执行转换
        result_path = convert(config)

        # 读取转换结果的 GeoJSON 内容，返回给前端预览
        import json as json_module
        if split_layers:
            # 按图层分割时，读取所有 GeoJSON 文件合并返回预览数据
            all_features = []
            result_dir_path = Path(result_path)
            for geojson_file in result_dir_path.glob("*.geojson"):
                with open(geojson_file, "r", encoding="utf-8") as f:
                    data = json_module.load(f)
                    all_features.extend(data.get("features", []))
            preview_geojson = {
                "type": "FeatureCollection",
                "features": all_features,
            }
        else:
            with open(result_path, "r", encoding="utf-8") as f:
                preview_geojson = json_module.load(f)

        # 统计信息
        feature_count = len(preview_geojson.get("features", []))
        # 按图层统计数量
        layer_stats = {}
        for feat in preview_geojson.get("features", []):
            layer = feat.get("properties", {}).get("layer", "未知")
            layer_stats[layer] = layer_stats.get(layer, 0) + 1
        # 按几何类型统计
        type_stats = {}
        for feat in preview_geojson.get("features", []):
            geom_type = feat.get("geometry", {}).get("type", "未知")
            type_stats[geom_type] = type_stats.get(geom_type, 0) + 1

        return jsonify({
            "success": True,
            "task_id": task_id,
            "filename": original_name,
            "geojson": preview_geojson,
            "stats": {
                "feature_count": feature_count,
                "layer_stats": layer_stats,
                "type_stats": type_stats,
            },
        })

    except FileNotFoundError as e:
        return jsonify({"error": f"文件错误: {str(e)}"}), 400
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400
    except RuntimeError as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500
    except Exception as e:
        logger.exception(f"未预期的错误: {e}")
        return jsonify({"error": f"服务器错误: {str(e)}"}), 500


@app.route("/render", methods=["POST"])
def render_geojson():
    """
    GeoJSON → SVG 渲染接口。

    接收：
        JSON body: {"geojson": <FeatureCollection>, "api_key": "...（可选）"}
        或 multipart/form-data: geojson_file 字段上传 .geojson 文件

    返回：
        {"svg": "...", "strategy": "A/B/C/D", "warnings": [...], "elapsed": 1.2}
    """
    import json as json_module

    # ── 从请求中提取 GeoJSON 数据 ─────────────────────────────────────
    geojson_data = None
    api_key = None

    if request.is_json:
        body = request.get_json()
        geojson_data = body.get("geojson")
        api_key = body.get("api_key")
    elif "geojson_file" in request.files:
        f = request.files["geojson_file"]
        geojson_data = json_module.loads(f.read().decode("utf-8"))
        api_key = request.form.get("api_key")
    else:
        return jsonify({"error": "请提供 JSON body（含 geojson 字段）或上传 geojson_file 文件"}), 400

    if not geojson_data:
        return jsonify({"error": "GeoJSON 数据为空"}), 400

    # ── 执行渲染管线 ──────────────────────────────────────────────────
    try:
        pipeline = RenderPipeline(api_key=api_key or None)
        result = pipeline.run(geojson_data)

        return jsonify({
            "svg": result.svg,
            "strategy": result.strategy,
            "is_valid": result.is_valid,
            "warnings": result.warnings,
            "errors": result.errors,
            "elapsed": round(result.elapsed_sec, 2),
        })

    except RuntimeError as e:
        logger.error(f"渲染失败: {e}")
        return jsonify({"error": f"渲染失败: {str(e)}"}), 500
    except Exception as e:
        logger.exception(f"渲染未预期错误: {e}")
        return jsonify({"error": f"服务器错误: {str(e)}"}), 500


@app.route("/export", methods=["POST"])
def export_to_cad():
    """
    GeoJSON → DXF/DWG 导出接口。

    接收（JSON body）：
        {
            "geojson":    <FeatureCollection>,
            "format":     "dxf" | "dwg"（默认 dxf）,
            "target_crs": "EPSG:4526"（可选，WGS84→工程坐标系反向转换）
        }

    返回：
        DXF 或 DWG 文件下载（Content-Disposition: attachment）
    """
    import json as json_module

    if not request.is_json:
        return jsonify({"error": "请提供 JSON body（含 geojson 字段）"}), 400

    body = request.get_json()
    geojson_data = body.get("geojson")
    fmt = (body.get("format") or "dxf").lower()
    target_crs = body.get("target_crs") or None

    if not geojson_data:
        return jsonify({"error": "GeoJSON 数据为空"}), 400

    if fmt not in ("dxf", "dwg"):
        return jsonify({"error": f"不支持的格式: {fmt!r}，仅支持 dxf 或 dwg"}), 400

    # 每次导出使用独立目录，避免并发冲突
    task_id = uuid.uuid4().hex[:8]
    result_dir = RESULT_DIR / task_id
    result_dir.mkdir(parents=True, exist_ok=True)

    ext = ".dwg" if fmt == "dwg" else ".dxf"
    output_path = result_dir / f"export{ext}"

    try:
        config = ExportConfig(
            input_file="upload.geojson",  # 仅用于生成默认输出文件名，不会实际读取
            output_file=str(output_path),
            target_crs=target_crs,
            format=fmt,
        )
        out_path = export_geojson_to_dxf(geojson_data, config)

        # 设置对应的 MIME 类型
        mimetype = "application/acad" if fmt == "dwg" else "application/dxf"
        return send_file(
            out_path,
            as_attachment=True,
            download_name=f"export{ext}",
            mimetype=mimetype,
        )

    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400
    except RuntimeError as e:
        logger.error(f"导出失败: {e}")
        return jsonify({"error": f"导出失败: {str(e)}"}), 500
    except Exception as e:
        logger.exception(f"导出未预期错误: {e}")
        return jsonify({"error": f"服务器错误: {str(e)}"}), 500


@app.route("/download/<task_id>")
def download_file(task_id):
    """
    下载接口：用户预览确认后，通过 task_id 下载对应的 GeoJSON 文件。

    参数:
        task_id: 转换任务的唯一标识
    """
    result_dir = RESULT_DIR / task_id

    if not result_dir.exists():
        return jsonify({"error": "文件不存在或已过期"}), 404

    # 查找结果文件
    geojson_files = list(result_dir.glob("*.geojson"))
    if not geojson_files:
        return jsonify({"error": "未找到转换结果"}), 404

    if len(geojson_files) == 1:
        # 单文件直接下载
        return send_file(
            str(geojson_files[0]),
            as_attachment=True,
            download_name=geojson_files[0].name,
            mimetype="application/geo+json",
        )
    else:
        # 多文件打包为 zip 下载
        zip_path = str(result_dir / "layers")
        shutil.make_archive(zip_path, "zip", str(result_dir))
        return send_file(
            zip_path + ".zip",
            as_attachment=True,
            download_name="layers.zip",
            mimetype="application/zip",
        )


if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    print("=" * 50)
    print("  cad2geojson Web 界面")
    print("  打开浏览器访问: http://localhost:5000")
    print("=" * 50)
    app.run(debug=True, host="0.0.0.0", port=5000)
