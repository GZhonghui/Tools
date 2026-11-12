#!/usr/bin/env python3
"""递归压缩图片，保留目录结构和文件名主体，仅修改扩展名。Python 3.10+。

安装依赖：python3 -m pip install Pillow
用法：python3 Image/compress_images.py 输入目录 输出目录
覆盖配置：python3 Image/compress_images.py 输入目录 输出目录 --format webp --max-edge 1600 --max-kb 300

小图不放大；JPEG/WebP 先降低质量，仍超限时打印警告并继续等比例缩小。
PNG 使用无损编码，超限时只能继续缩小。动画、多页图片只处理第一帧。
自动纠正 EXIF 方向，输出不保留 EXIF 等元数据；JPEG 的透明区域填充白色。
重复运行会覆盖同路径输出；输入中有同名转码冲突时，在写入前报错。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    raise SystemExit("缺少 Pillow，请先执行：python3 -m pip install Pillow")


# ---- 配置：按需修改，也可用命令行覆盖格式、分辨率和体积上限 ----
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TARGET_FORMAT = "jpeg"  # 支持 jpeg（或 jpg）、webp、png
MAX_LONG_EDGE = 1200  # 像素；只缩小，不放大
MAX_FILE_SIZE_KB = 240  # 1 KB = 1024 字节
MIN_QUALITY = 1
MAX_QUALITY = 95
JPEG_BACKGROUND = (255, 255, 255)

FORMAT_SUFFIXES = {"JPEG": ".jpeg", "WEBP": ".webp", "PNG": ".png"}


def collect_images(input_root: Path, output_root: Path) -> list[Path]:
    """不跟随符号链接；输出在输入目录内时，跳过整个输出子树。"""
    extensions = {"." + extension.lower().lstrip(".") for extension in IMAGE_EXTENSIONS}
    images = []

    def walk_error(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(input_root, onerror=walk_error):
        current = Path(directory)
        dirs[:] = sorted(
            name for name in dirs
            if not (current / name).is_symlink() and current / name != output_root
        )
        for name in sorted(files):
            path = current / name
            if path.suffix.lower() in extensions and not path.is_symlink() and path.is_file():
                images.append(path)
    return images


def plan_outputs(
    sources: list[Path], input_root: Path, output_root: Path, output_format: str,
) -> list[tuple[Path, Path]]:
    jobs = []
    owners: dict[Path, Path] = {}
    for source in sources:
        relative = source.relative_to(input_root).with_suffix(FORMAT_SUFFIXES[output_format])
        destination = output_root / relative
        if destination in owners:
            raise ValueError(
                f"转码后文件名冲突：{owners[destination]} 和 {source} 都将输出到 {destination}"
            )
        owners[destination] = source
        jobs.append((source, destination))
    return jobs


def prepare_image(source: Path, output_format: str, max_edge: int) -> Image.Image:
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        if output_format == "JPEG" and has_alpha:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, JPEG_BACKGROUND)
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGBA" if has_alpha and output_format != "JPEG" else "RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    # 方向已应用到像素；删除元数据也可避免元数据本身占满体积预算。
    image.info.clear()
    return image


def encode_image(image: Image.Image, output_format: str, quality: int | None = None) -> bytes:
    with BytesIO() as buffer:
        if output_format == "JPEG":
            image.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=2)
        elif output_format == "WEBP":
            image.save(buffer, format="WEBP", quality=quality, method=6)
        else:
            image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def fit_quality(image: Image.Image, output_format: str, max_bytes: int) -> tuple[bytes, int | None]:
    """二分查找可满足体积限制的质量；最低质量仍超限时返回它用于后续缩小。"""
    if output_format == "PNG":
        return encode_image(image, output_format), None

    data = encode_image(image, output_format, MAX_QUALITY)
    if len(data) <= max_bytes:
        return data, MAX_QUALITY

    best = encode_image(image, output_format, MIN_QUALITY)
    if len(best) > max_bytes:
        return best, MIN_QUALITY

    best_quality = MIN_QUALITY
    low, high = MIN_QUALITY + 1, MAX_QUALITY - 1
    while low <= high:
        quality = (low + high) // 2
        candidate = encode_image(image, output_format, quality)
        if len(candidate) <= max_bytes:
            best, best_quality = candidate, quality
            low = quality + 1
        else:
            high = quality - 1
    return best, best_quality


def compress_image(
    source: Path, output_format: str, max_edge: int, max_bytes: int,
) -> tuple[bytes, tuple[int, int], int | None]:
    base = prepare_image(source, output_format, max_edge)
    image = base
    edge = max(base.size)
    while True:
        data, quality = fit_quality(image, output_format, max_bytes)
        if len(data) <= max_bytes:
            return data, image.size, quality
        reason = f"最低质量 {quality}" if quality is not None else "无损编码"
        if edge == 1:
            raise ValueError(
                f"{output_format} {reason}且缩小到 1×1 后仍超过 {max_bytes} 字节，无法满足体积上限"
            )

        # 质量无法再降低时按体积比估算下一轮尺寸，并保证每轮都缩小。
        previous_size = image.size
        scale = min(0.9, math.sqrt(max_bytes / len(data)) * 0.9)
        edge = max(1, min(edge - 1, int(edge * scale)))
        image = base.copy()
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        print(
            f"[警告] {source}：{output_format} {reason}下仍超限"
            f"（{len(data) / 1024:.2f} KB > {max_bytes / 1024:g} KB）；"
            f"继续等比例缩小：{previous_size[0]}×{previous_size[1]} -> {image.width}×{image.height}。",
            file=sys.stderr, flush=True,
        )


def write_output(destination: Path, output_root: Path, data: bytes) -> None:
    if not destination.resolve().is_relative_to(output_root):
        raise ValueError(f"输出路径通过符号链接指向输出目录之外：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".compress-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_path", type=Path, help="输入图片目录，递归扫描")
    parser.add_argument("output_path", type=Path, help="输出目录，保留原目录结构")
    parser.add_argument("--format", default=TARGET_FORMAT, help="目标格式：jpeg / jpg / webp / png（默认 %(default)s）")
    parser.add_argument("--max-edge", type=int, default=MAX_LONG_EDGE, help="长边像素上限（默认 %(default)s）")
    parser.add_argument("--max-kb", type=float, default=MAX_FILE_SIZE_KB, help="文件体积上限，1 KB = 1024 字节（默认 %(default)s）")
    args = parser.parse_args(argv)

    output_format = args.format.upper()
    if output_format == "JPG":
        output_format = "JPEG"
    if output_format not in FORMAT_SUFFIXES:
        parser.error("目标格式必须为 jpeg、jpg、webp 或 png")
    if args.max_edge <= 0:
        parser.error("长边上限必须是正整数")
    if not math.isfinite(args.max_kb) or args.max_kb <= 0:
        parser.error("体积上限必须是有限正数")
    max_bytes = int(args.max_kb * 1024)
    if max_bytes < 1:
        parser.error("体积上限不能小于 1 字节")
    if not 1 <= MIN_QUALITY <= MAX_QUALITY <= 95:
        parser.error("质量配置必须满足 1 <= MIN_QUALITY <= MAX_QUALITY <= 95")

    input_root = args.input_path.expanduser().resolve()
    output_root = args.output_path.expanduser().resolve()
    if not input_root.is_dir():
        parser.error(f"输入路径不存在或不是目录：{input_root}")
    if output_root == input_root or input_root.is_relative_to(output_root):
        parser.error("输出目录不能等于输入目录或是其上级目录，以免覆盖源文件")
    if output_root.exists() and not output_root.is_dir():
        parser.error(f"输出路径不是目录：{output_root}")
    try:
        sources = collect_images(input_root, output_root)
        jobs = plan_outputs(sources, input_root, output_root, output_format)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if not jobs:
        print("没有找到匹配扩展名的图片。")
        return 0

    print(f"找到 {len(jobs)} 张图片；输出 {output_format}，长边 ≤ {args.max_edge}px，体积 ≤ {args.max_kb:g} KB。", flush=True)
    succeeded = failed = 0
    for index, (source, destination) in enumerate(jobs, 1):
        try:
            data, size, quality = compress_image(source, output_format, args.max_edge, max_bytes)
            write_output(destination, output_root, data)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            failed += 1
            print(f"[{index}/{len(jobs)}] 失败 {source.relative_to(input_root)}：{error}", file=sys.stderr, flush=True)
            continue
        succeeded += 1
        quality_label = f"，质量 {quality}" if quality is not None else ""
        print(
            f"[{index}/{len(jobs)}] {source.relative_to(input_root)} -> {destination.relative_to(output_root)}"
            f"：{size[0]}×{size[1]}，{len(data) / 1024:.2f} KB{quality_label}",
            flush=True,
        )
    print(f"完成：成功 {succeeded}，失败 {failed}。输出目录：{output_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
