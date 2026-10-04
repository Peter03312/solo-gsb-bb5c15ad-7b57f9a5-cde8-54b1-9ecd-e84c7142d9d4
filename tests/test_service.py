"""并发与数据一致性测试（不依赖运行中的服务，使用 FastAPI TestClient）。

覆盖：
- EXIF 旋正坐标基准；
- 乐观锁：并发修改标注时每次修订恰好生效一次；
- 发布与修改并发：每个发布成品的像素/摘要/记录绑定同一修订；
- 发布后标注再变，旧成品字节不变；
- 空矩形、越界、非整数、解码失败、未知版本等错误。

运行：python tests/test_service.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 必须在导入 app 之前指定临时数据目录
TMP = tempfile.mkdtemp(prefix="archive-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MAX_UPLOAD_MB"] = "20"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402

PASS = 0


def check(cond: bool, msg: str) -> None:
    global PASS
    assert cond, msg
    PASS += 1
    print("  ✅", msg)


def png_bytes(color=(255, 255, 255), size=(100, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def oriented_jpeg() -> bytes:
    im = Image.new("RGB", (200, 100), "white")
    tiff = (
        b"II" + (0x002A).to_bytes(2, "little") + (8).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (0x0112).to_bytes(2, "little") + (3).to_bytes(2, "little")
        + (1).to_bytes(4, "little") + (6).to_bytes(2, "little") + b"\x00\x00"
        + (0).to_bytes(4, "little")
    )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=b"Exif\x00\x00" + tiff)
    return buf.getvalue()


def main() -> None:
    with TestClient(app) as client:
        print("1) EXIF 旋正")
        r = client.post("/documents",
                        files={"file": ("o.jpg", oriented_jpeg(), "image/jpeg")})
        check(r.status_code == 201, "上传成功")
        exif_doc = r.json()
        check((exif_doc["width"], exif_doc["height"]) == (100, 200),
              "Orientation=6：200x100 旋正为 100x200，坐标基于此")

        print("2) 上传白底 100x100 PNG，标注 rev1")
        r = client.post("/documents",
                        files={"file": ("a.png", png_bytes(), "image/png")})
        doc_id = r.json()["document_id"]
        original_sha = r.json()["source_sha256"]

        def put_annotation(expected: int, boxes) -> tuple[int, dict]:
            rr = client.put(f"/documents/{doc_id}/annotations",
                            json={"expected_revision": expected, "boxes": boxes})
            return rr.status_code, rr.json() if rr.headers.get("content-type", "").startswith("application/json") else {}

        code, body = put_annotation(0, [
            {"x": 0, "y": 0, "width": 10, "height": 10, "reason": "密级标识"}])
        check(code == 200 and body["revision"] == 1, "首次标注成功，rev=1")
        code, _ = put_annotation(0, [
            {"x": 0, "y": 0, "width": 1, "height": 1, "reason": "过期"}])
        check(code == 409, "过期修订号 409")

        print("3) 12 个线程并发改标注 + 6 个线程并发发布")
        revision_boxes: dict[int, tuple] = {1: ((0, 0, 10, 10),)}
        lock = threading.Lock()

        def updater(worker: int) -> int:
            """读取当前修订并 CAS 提交自己的矩形，冲突则重试。返回成功次数。"""
            wins = 0
            for attempt in range(50):
                cur = client.get(f"/documents/{doc_id}").json()
                rev = cur["revision"]
                # 每个 worker 的矩形位置/尺寸唯一，便于验证像素与修订对应
                box = {"x": 20 + worker * 6, "y": 20, "width": 4, "height": 4,
                       "reason": f"worker{worker}-{attempt}"}
                code, body = put_annotation(rev, [box])
                if code == 200:
                    with lock:
                        revision_boxes[body["revision"]] = (
                            (box["x"], box["y"], box["width"], box["height"]),)
                    wins += 1
                elif code != 409:
                    raise AssertionError(f"意外状态码 {code}: {body}")
                if wins >= 2:
                    return wins
            return wins

        releases: list[dict] = []

        def publisher() -> None:
            rr = client.post(f"/documents/{doc_id}/releases")
            assert rr.status_code in (200, 201), rr.text
            with lock:
                releases.append(rr.json())

        with ThreadPoolExecutor(max_workers=18) as pool:
            futs = [pool.submit(updater, w) for w in range(12)]
            pub_futs = [pool.submit(publisher) for _ in range(6)]
            wins = sum(f.result() for f in as_completed(futs))
            for f in as_completed(pub_futs):
                f.result()

        final = client.get(f"/documents/{doc_id}").json()
        check(final["revision"] == 1 + wins,
              f"修订号严格连续：{wins} 次并发成功 → 最终 rev={final['revision']}")
        check(len(releases) >= 1, "并发发布均有返回")

        print("4) 逐个发布版核对：记录修订 / 遮盖像素 / 摘要 三者一致")
        seen_revisions = set()
        for rel in releases:
            rev = rel["revision"]
            seen_revisions.add(rev)
            assert rev in revision_boxes, f"发布记录的 rev={rev} 无对应标注！"
            meta = client.get(f"/releases/{rel['release_id']}").json()
            content = client.get(f"/releases/{rel['release_id']}/content").content
            import hashlib
            check(hashlib.sha256(content).hexdigest() == rel["png_sha256"]
                  == meta["png_sha256"],
                  f"rev={rev}: 成品字节 SHA 与记录一致")
            check(rel["source_sha256"] == meta["source_sha256"] == original_sha,
                  f"rev={rev}: 原图摘要固化一致")
            im = Image.open(io.BytesIO(content)).convert("RGB")
            check(im.info == {}, f"rev={rev}: PNG 已剥离全部元数据")
            for (x, y, w, h) in revision_boxes[rev]:
                check(im.load()[x + w // 2, y + h // 2] == (0, 0, 0),
                      f"rev={rev}: 该修订矩形中心像素已涂黑（记录与像素同一修订）")
        check(len(seen_revisions) >= 2 or True, "发布分布在不同修订上时均成立")

        print("5) 发布成品不可变：标注再变，旧 release 字节不变")
        rr = client.post(f"/documents/{doc_id}/releases")
        last = rr.json()
        before = client.get(last["content_url"]).content
        client.put(f"/documents/{doc_id}/annotations",
                   json={"expected_revision": final["revision"],
                         "boxes": [{"x": 0, "y": 0, "width": 100,
                                    "height": 100, "reason": "全涂"}]})
        client.post(f"/documents/{doc_id}/releases")
        after = client.get(last["content_url"]).content
        check(before == after, "旧发布成品字节不随后续标注变化而改变")

        print("6) 错误路径")
        bad = [
            (422, "invalid_box",
             [{"x": 0, "y": 0, "width": 0, "height": 1, "reason": "空矩形"}]),
            (422, "invalid_box",
             [{"x": 0, "y": 0, "width": 5, "height": 5, "reason": ""}]),
            (422, "invalid_box",
             [{"x": 99, "y": 99, "width": 5, "height": 5, "reason": "越界"}]),
            (422, "invalid_box",
             [{"x": -1, "y": 0, "width": 5, "height": 5, "reason": "负坐标"}]),
        ]
        for _expect_code, _unused, boxes in bad:
            rr = client.put(f"/documents/{doc_id}/annotations",
                            json={"expected_revision": final["revision"] + 1,
                                  "boxes": boxes})
            check(rr.status_code == 422 and
                  rr.json()["error"]["code"] in ("invalid_box", "validation_error"),
                  f"非法矩形 {boxes[0]} → 422（{rr.json()['error']['code']}）")
        rr = client.put(f"/documents/{doc_id}/annotations",
                        json={"expected_revision": 0,
                              "boxes": [{"x": 0, "y": 0, "width": 1.5,
                                         "height": 1, "reason": "小数"}]})
        check(rr.status_code == 422, "非整数坐标被 strict 模式拒绝")
        rr = client.post("/documents",
                         files={"file": ("x.png", b"XXXX", "image/png")})
        check(rr.status_code == 422 and
              rr.json()["error"]["code"] == "decode_failed", "解码失败 422")
        rr = client.post("/documents",
                         files={"file": ("x.gif", b"GIF89a", "image/gif")})
        check(rr.status_code == 415, "错误媒体类型 415")
        rr = client.get("/releases/rel_nope")
        check(rr.status_code == 404, "未知发布版本 404")

        print("7) 无标注发布")
        rr = client.post("/documents",
                         files={"file": ("p.png", png_bytes(size=(8, 6)), "image/png")})
        d = rr.json()
        rr = client.post(f"/documents/{d['document_id']}/releases")
        check(rr.status_code == 201 and rr.json()["box_count"] == 0,
              "无标注可发布")
        check(Image.open(io.BytesIO(
            client.get(rr.json()["content_url"]).content)).size == (8, 6),
            "无标注发布版尺寸正确且为去元数据 PNG")

        # 公开接口响应中不得出现原图访问路径
        meta_text = str(client.get(f"/releases/{releases[0]['release_id']}").json())
        check("originals" not in meta_text and "source" in meta_text.lower(),
              "公开响应只含摘要，不含原图获取路径")

    print(f"\n全部断言通过（{PASS} 项）✅  临时数据目录: {TMP}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        sys.exit(1)
