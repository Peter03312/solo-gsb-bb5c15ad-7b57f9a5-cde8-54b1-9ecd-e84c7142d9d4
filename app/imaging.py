"""影像处理：解码、EXIF 旋正、标注校验、不可逆涂实与去元数据 PNG 输出。

所有标注坐标均定义在「按 EXIF 方向旋正之后」的像素坐标系上。
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .config import settings

# 全局像素上限，解压时拦截解压炸弹
Image.MAX_IMAGE_PIXELS = settings.max_image_pixels

DECODED_FORMAT_BY_CONTENT_TYPE = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
}

MAX_BOXES = 1000
MAX_REASON_LEN = 500


class ImageError(ValueError):
    """上传影像无法解码或不满足格式/尺寸要求。"""


class BoxValidationError(ValueError):
    """标注矩形不合法，errors 给出逐条原因。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class DecodedImage:
    image: Image.Image
    width: int
    height: int
    decoded_format: str


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def decode_original(data: bytes, declared_content_type: str) -> DecodedImage:
    """解码上传字节并按 EXIF Orientation 旋正。失败抛 ImageError。"""
    if not data:
        raise ImageError("上传内容为空")

    expected_format = DECODED_FORMAT_BY_CONTENT_TYPE.get(declared_content_type)
    if expected_format is None:
        raise ImageError(
            f"不支持的 Content-Type: {declared_content_type!r}，仅接受 image/png 与 image/jpeg"
        )

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except UnidentifiedImageError:
        raise ImageError("图片解码失败：无法识别的文件格式，仅支持有效的 PNG/JPEG")
    except (Image.DecompressionBombError,):
        raise ImageError(
            f"图片像素数超过上限 {settings.max_image_pixels:,}，已拒绝处理"
        )
    except (OSError, ValueError) as exc:
        raise ImageError(f"图片解码失败：{exc}")

    if im.format not in ("PNG", "JPEG"):
        raise ImageError(f"仅支持 PNG/JPEG，实际解码格式为 {im.format!r}")
    if im.format != expected_format:
        raise ImageError(
            f"声明类型 {declared_content_type} 与实际解码格式 {im.format} 不一致"
        )

    # 按 EXIF Orientation 旋正；后续所有宽高与坐标都基于旋正后的图像
    im = ImageOps.exif_transpose(im)

    width, height = im.size
    if width <= 0 or height <= 0:
        raise ImageError("图片尺寸非法")
    if width * height > settings.max_image_pixels:
        raise ImageError(
            f"图片像素数 {width * height:,} 超过上限 {settings.max_image_pixels:,}"
        )
    return DecodedImage(image=im, width=width, height=height, decoded_format=im.format)


def validate_boxes(boxes: list[dict], width: int, height: int) -> list[dict]:
    """在给定（旋正后的）画布尺寸上校验标注，返回规范化列表。"""
    errors: list[str] = []
    normalized: list[dict] = []

    if len(boxes) > MAX_BOXES:
        raise BoxValidationError([f"矩形数量超过上限 {MAX_BOXES}"])

    for i, box in enumerate(boxes):
        where = f"boxes[{i}]"
        box_errors: list[str] = []
        try:
            x = box["x"]
            y = box["y"]
            w = box["width"]
            h = box["height"]
            reason = box["reason"]
        except (KeyError, TypeError):
            errors.append(f"{where} 必须是包含 x/y/width/height/reason 的对象")
            continue

        ints_ok = True
        for name, value in (("x", x), ("y", y), ("width", w), ("height", h)):
            if isinstance(value, bool) or not isinstance(value, int):
                box_errors.append(f"{where}.{name} 必须是整数")
                ints_ok = False
        if not isinstance(reason, str):
            box_errors.append(f"{where}.reason 必须是字符串")
        else:
            reason = reason.strip()
            if reason == "":
                box_errors.append(f"{where}.reason 不能为空")
            elif len(reason) > MAX_REASON_LEN:
                box_errors.append(f"{where}.reason 长度不能超过 {MAX_REASON_LEN}")

        if ints_ok:
            if w < 1 or h < 1:
                box_errors.append(
                    f"{where} 为空矩形（width/height 必须 ≥ 1，实际 {w}×{h}）"
                )
            if x < 0 or y < 0:
                box_errors.append(f"{where} 坐标不能为负（实际 x={x}, y={y}）")
            elif w >= 1 and h >= 1 and (x + w > width or y + h > height):
                box_errors.append(
                    f"{where} 越界：画布 {width}×{height}，"
                    f"矩形覆盖至 ({x + w}, {y + h})"
                )

        errors.extend(box_errors)
        if not box_errors:
            normalized.append(
                {"x": x, "y": y, "width": w, "height": h, "reason": reason}
            )

    if errors:
        raise BoxValidationError(errors)
    return normalized


def render_published(
    original_data: bytes, declared_content_type: str, boxes: list[dict]
) -> tuple[bytes, int, int]:
    """从不可变原图字节独立渲染发布 PNG。

    - 重新解码并 EXIF 旋正；
    - 将标注矩形用纯色不可逆涂实（不保留任何原始像素）；
    - 输出独立 PNG，剥离全部元数据（EXIF / ICC / comment 等）。
    """
    decoded = decode_original(original_data, declared_content_type)
    im = decoded.image

    # 统一转 RGB；im.copy()/convert 会拷贝 info，显式清空，确保不带任何元数据
    published = im.convert("RGB") if im.mode != "RGB" else im.copy()
    published.info = {}

    draw = ImageDraw.Draw(published)
    color = settings.redaction_color
    for box in boxes:
        x, y = box["x"], box["y"]
        x2, y2 = x + box["width"] - 1, y + box["height"] - 1
        draw.rectangle([x, y, x2, y2], fill=color)

    buf = io.BytesIO()
    # 不传 pnginfo/exif/icc_profile，且 info 已清空 → 输出只含图像像素
    published.save(buf, format="PNG", optimize=False)
    return buf.getvalue(), decoded.width, decoded.height
