"""端到端调用示例（可直接运行）：python examples/demo.py

流程：
1. 生成一张带 EXIF Orientation=6 的 JPEG（存储 200x100，旋正后应为 100x200）；
2. 上传 → 标注 → 用过期修订号修改（被 409 拒绝）；
3. 发布 → 公开读取，校验遮盖像素、元数据剥离与摘要固化；
4. 再改标注再发布，验证旧成品字节不变；
5. 演示空矩形、越界、解码失败、未知发布版本的明确报错；
6. 无标注发布，得到仅去元数据的 PNG。

仅依赖：httpx、Pillow。
"""
from __future__ import annotations

import io
import os
import sys
import hashlib

import httpx
from PIL import Image, ImageDraw

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def build_oriented_jpeg() -> tuple[bytes, tuple[int, int]]:
    """构造 200x100、EXIF Orientation=6（显示时顺时针 90°）的 JPEG。"""
    im = Image.new("RGB", (200, 100), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([8, 18, 12, 22], fill=(255, 0, 0))  # 标记点

    # 手工构造最小 EXIF（小端 TIFF，Orientation tag=0x0112，值 6）
    tiff = (
        b"II" + (0x002A).to_bytes(2, "little") + (8).to_bytes(4, "little")
        + (1).to_bytes(2, "little")                       # IFD 条目数
        + (0x0112).to_bytes(2, "little")                 # tag: Orientation
        + (3).to_bytes(2, "little")                      # type: SHORT
        + (1).to_bytes(4, "little")                      # count
        + (6).to_bytes(2, "little") + b"\x00\x00"        # value 6 + 填充
        + (0).to_bytes(4, "little")                      # next IFD = 0
    )
    exif = b"Exif\x00\x00" + tiff

    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    return buf.getvalue(), (100, 200)  # 旋正后期望尺寸


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expect(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        sys.exit(1)


def main() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=30)

    print("1) 健康检查")
    expect(client.get("/health").json()["status"] == "ok", "GET /health = ok")

    print("2) 上传带 EXIF 方向的 JPEG")
    jpeg_bytes, expected_size = build_oriented_jpeg()
    r = client.post(
        "/documents",
        files={"file": ("sample.jpg", jpeg_bytes, "image/jpeg")},
    )
    expect(r.status_code == 201, f"上传成功（201），实际 {r.status_code}")
    doc = r.json()
    doc_id = doc["document_id"]
    expect(
        (doc["width"], doc["height"]) == expected_size,
        f"尺寸已按 EXIF 旋正：{doc['width']}x{doc['height']}（期望 {expected_size[0]}x{expected_size[1]}）",
    )
    expect(doc["revision"] == 0 and doc["boxes"] == [], "初始修订号为 0、无标注")
    expect(doc["source_sha256"] == sha256(jpeg_bytes), "返回原图 SHA-256 与上传字节一致")

    print("3) 在旋正后的坐标系上标注敏感矩形")
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={
            "expected_revision": 0,
            "boxes": [
                {"x": 77, "y": 8, "width": 6, "height": 6, "reason": "红色密件标记"},
                {"x": 0, "y": 0, "width": 10, "height": 10, "reason": "档号"},
            ],
        },
    )
    expect(r.status_code == 200, f"标注写入成功，实际 {r.status_code} {r.text}")
    expect(r.json()["revision"] == 1, "修订号推进到 1")

    print("4) 过期修订号必须被拒绝")
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={
            "expected_revision": 0,
            "boxes": [{"x": 0, "y": 0, "width": 1, "height": 1, "reason": "过期提交"}],
        },
    )
    expect(r.status_code == 409, f"过期修订号返回 409，实际 {r.status_code}")
    expect(r.json()["error"]["code"] == "revision_conflict", "错误码 revision_conflict")

    print("5) 发布（按修订 1 固化）")
    r = client.post(f"/documents/{doc_id}/releases")
    expect(r.status_code == 201, f"发布成功，实际 {r.status_code} {r.text}")
    rel = r.json()
    rel_id, rev1 = rel["release_id"], rel["revision"]
    expect(rev1 == 1, f"发布记录绑定修订号 {rev1}")
    expect(rel["source_sha256"] == sha256(jpeg_bytes), "记录中固化原图摘要")
    expect(rel["box_count"] == 2, f"遮盖矩形数 {rel['box_count']}（期望 2）")

    print("6) 公开读取：元信息 + 成品 PNG，校验像素、元数据、摘要")
    meta = client.get(f"/releases/{rel_id}").json()
    expect(meta["revision"] == 1 and meta["source_sha256"] == sha256(jpeg_bytes),
           "公开元信息含修订号与原图摘要、坐标原因，不含原图字节")

    r = client.get(f"/releases/{rel_id}/content")
    expect(r.status_code == 200 and r.headers["content-type"] == "image/png",
           "成品以 image/png 返回")
    png_bytes = r.content
    expect(sha256(png_bytes) == rel["png_sha256"] == meta["png_sha256"],
           "成品字节摘要与记录一致（固化）")

    out = Image.open(io.BytesIO(png_bytes))
    expect(out.format == "PNG" and out.size == expected_size, "独立 PNG，尺寸为旋正后尺寸")
    expect(out.info == {}, "PNG 不含任何元数据（EXIF/ICC/文本块均剥离）")
    px = out.load()
    expect(px[80, 10] == (0, 0, 0), "敏感标记位置已被纯黑不可逆涂实")
    expect(all(v > 240 for v in px[50, 50]), "非遮盖区域仍为原始白色内容")

    print("7) 发布后修改标注再发布，旧成品不得改变")
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={
            "expected_revision": 1,
            "boxes": [{"x": 0, "y": 0, "width": 100, "height": 200, "reason": "整页涂黑重发"}],
        },
    )
    expect(r.status_code == 200 and r.json()["revision"] == 2, "标注更新到修订 2")
    r = client.post(f"/documents/{doc_id}/releases")
    rel2 = r.json()
    expect(r.status_code == 201 and rel2["revision"] == 2 and rel2["release_id"] != rel_id,
           "第二次发布生成独立的新成品（修订 2）")
    old_bytes = client.get(f"/releases/{rel_id}/content").content
    expect(old_bytes == png_bytes, "旧发布成品字节完全不变")

    r = client.post(f"/documents/{doc_id}/releases")
    expect(r.status_code == 200 and r.json()["release_id"] == rel2["release_id"],
           "同一修订重复发布幂等，返回既有成品")

    print("8) 明确报错：空矩形 / 越界 / 解码失败 / 未知版本")
    bad_cases = [
        ("空矩形", {"expected_revision": 2,
                   "boxes": [{"x": 0, "y": 0, "width": 0, "height": 5, "reason": "空"}]}),
        ("越界", {"expected_revision": 2,
                  "boxes": [{"x": 90, "y": 190, "width": 20, "height": 20, "reason": "越界"}]}),
        ("原因为空", {"expected_revision": 2,
                     "boxes": [{"x": 0, "y": 0, "width": 1, "height": 1, "reason": "   "}]}),
    ]
    for label, body in bad_cases:
        r = client.put(f"/documents/{doc_id}/annotations", json=body)
        code = r.json()["error"]["code"]
        expect(r.status_code == 422 and code in ("invalid_box", "validation_error"),
               f"{label} → 422 {code}")

    r = client.post("/documents", files={"file": ("x.png", b"not an image", "image/png")})
    expect(r.status_code == 422 and r.json()["error"]["code"] == "decode_failed",
           "解码失败 → 422 decode_failed")

    r = client.get("/releases/rel_does_not_exist")
    expect(r.status_code == 404 and r.json()["error"]["code"] == "release_not_found",
           "未知发布版本 → 404 release_not_found")

    print("9) 无标注也可发布：仅去元数据的 PNG")
    blank = io.BytesIO()
    Image.new("RGB", (32, 16), (10, 20, 30)).save(blank, format="PNG")
    r = client.post(
        "/documents",
        files={"file": ("blank.png", blank.getvalue(), "image/png")},
    )
    expect(r.status_code == 201, f"无标注 PNG 上传成功，实际 {r.status_code}")
    d2 = r.json()
    r = client.post(f"/documents/{d2['document_id']}/releases")
    expect(r.status_code == 201 and r.json()["box_count"] == 0,
           "无标注发布成功，box_count=0")
    rel3 = r.json()
    out = Image.open(io.BytesIO(client.get(rel3["content_url"]).content))
    expect(out.info == {} and out.size == (32, 16), "无标注发布版同样去元数据、尺寸正确")

    print("\n全部通过 ✅  服务工作在", BASE_URL)


if __name__ == "__main__":
    main()
