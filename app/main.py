"""档案影像脱敏发布服务 —— FastAPI 入口。

接口分两组：
- /documents/**：档案员侧（上传原图、读取/修改标注、发布、列出发布版）
- /releases/**：公开只读（只返回发布成品 PNG 与其遮盖坐标/原因，绝不返回原图）
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import database as db
from . import storage
from .config import settings
from .imaging import (
    BoxValidationError,
    ImageError,
    decode_original,
    render_published,
    sha256_hex,
    validate_boxes,
)
from .models import AnnotationsUpdate

app = FastAPI(
    title="档案影像脱敏发布服务",
    version="1.0.0",
    description="上传 PNG/JPEG、在 EXIF 旋正坐标系上标注敏感矩形，"
    "按修订发布不可逆涂实且去元数据的 PNG。",
)

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}


# ---------- 错误处理 ----------

class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    body: dict = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(ImageError)
async def image_error_handler(_: Request, exc: ImageError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "decode_failed", "message": str(exc)}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """统一请求体校验错误信封（坐标/原因等在业务校验前被 Pydantic 拦下的情形）。"""
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "validation_error",
            "message": "请求体校验失败",
            "details": jsonable_encoder(exc.errors()),
        }},
    )


@app.on_event("startup")
def _startup() -> None:
    storage.ensure_dirs()
    db.init_db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _box_dto(b: dict) -> dict:
    return {"x": b["x"], "y": b["y"], "width": b["width"],
            "height": b["height"], "reason": b["reason"]}


def _require_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise ApiError(404, "document_not_found", f"文档不存在: {doc_id}")
    return doc


# ---------- 档案员侧 ----------

@app.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> dict:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ApiError(
            415, "unsupported_media_type",
            f"仅接受 image/png 或 image/jpeg，收到 {content_type!r}",
        )

    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise ApiError(
            413, "file_too_large",
            f"文件超过大小上限 {settings.max_upload_bytes} 字节",
        )

    decoded = decode_original(data, content_type)  # 解码失败 → 422
    source_sha = sha256_hex(data)
    doc_id = _new_id("doc")
    now = _now()

    # 写事务一律 IMMEDIATE：开始即取 RESERVED 锁，靠 busy_timeout 排队，
    # 避免多个延迟事务在 SHARED 锁上同时升级写锁触发 SQLITE_BUSY 死锁
    with db.transaction(immediate=True) as conn:
        db.insert_document(
            conn,
            doc_id=doc_id,
            content_type=content_type,
            source_filename=file.filename or "",
            width=decoded.width,
            height=decoded.height,
            sha256=source_sha,
            byte_size=len(data),
            created_at=now,
        )
    storage.save_atomic(storage.original_path(doc_id), data)

    return {
        "document_id": doc_id,
        "width": decoded.width,
        "height": decoded.height,
        "orientation": "EXIF 已旋正，标注坐标基于此尺寸",
        "source_sha256": source_sha,
        "revision": 0,
        "boxes": [],
        "created_at": now,
    }


@app.get("/documents/{doc_id}")
def get_document(doc_id: str) -> dict:
    with db.transaction() as conn:
        doc = _require_document(conn, doc_id)
        ann = db.get_annotations(conn, doc_id)
    return {
        "document_id": doc["id"],
        "width": doc["width"],
        "height": doc["height"],
        "content_type": doc["content_type"],
        "source_filename": doc["source_filename"],
        "source_sha256": doc["sha256"],
        "created_at": doc["created_at"],
        "revision": ann["revision"],
        "boxes": [_box_dto(b) for b in db.decode_boxes(ann["boxes"])],
        "updated_at": ann["updated_at"],
    }


@app.put("/documents/{doc_id}/annotations")
def update_annotations(doc_id: str, payload: AnnotationsUpdate) -> dict:
    """整体替换标注集合；expected_revision 必须等于服务端当前修订号。"""
    candidate = [b.model_dump() for b in payload.boxes]
    now = _now()

    with db.transaction(immediate=True) as conn:
        doc = _require_document(conn, doc_id)
        try:
            validated = validate_boxes(candidate, doc["width"], doc["height"])
        except BoxValidationError as exc:
            raise ApiError(422, "invalid_box", "标注矩形不合法", exc.errors)

        boxes_json = db.encode_boxes(validated)
        ok = db.replace_annotations(
            conn,
            doc_id=doc_id,
            expected_revision=payload.expected_revision,
            boxes_json=boxes_json,
            updated_at=now,
        )
        if not ok:
            current = db.get_annotations(conn, doc_id)
            raise ApiError(
                409, "revision_conflict",
                f"修订号已过期：你提交的是 {payload.expected_revision}，"
                f"服务端当前为 {current['revision']}；请重新读取标注后再修改",
            )
        new_revision = payload.expected_revision + 1

    return {
        "document_id": doc_id,
        "revision": new_revision,
        "boxes": [_box_dto(b) for b in validated],
        "updated_at": now,
    }


@app.post("/documents/{doc_id}/releases")
def publish_release(doc_id: str, response: Response) -> dict:
    """按当前修订固化发布。同一修订重复发布返回既有成品（幂等，200）。

    渲染用的矩形、写进记录的 revision/source_sha256 与 PNG 落盘，
    全部绑定在同一个 IMMEDIATE 事务读到的修订上，并发修改无法穿插。
    """
    release_id = _new_id("rel")
    now = _now()

    with db.transaction(immediate=True) as conn:
        doc = _require_document(conn, doc_id)
        ann = db.get_annotations(conn, doc_id)
        revision = ann["revision"]
        boxes = db.decode_boxes(ann["boxes"])

        existing = conn.execute(
            "SELECT * FROM releases WHERE document_id = ? AND revision = ?",
            (doc_id, revision),
        ).fetchone()
        if existing is not None:
            response.status_code = 200
            return {
                "release_id": existing["id"],
                "document_id": doc_id,
                "revision": revision,
                "source_sha256": existing["source_sha256"],
                "png_sha256": existing["png_sha256"],
                "png_width": existing["width"],
                "png_height": existing["height"],
                "box_count": existing["box_count"],
                "boxes": [_box_dto(b) for b in db.decode_boxes(existing["boxes"])],
                "created_at": existing["created_at"],
                "public_url": f"/releases/{existing['id']}",
                "content_url": f"/releases/{existing['id']}/content",
                "note": "该修订已发布，返回既有成品；后续标注变化不会改变本成品",
            }

        original_data = storage.read_file(storage.original_path(doc_id))
        if sha256_hex(original_data) != doc["sha256"]:
            # 存储层原图被外部破坏时拒绝发布，绝不基于未固化的字节生成成品
            raise ApiError(500, "integrity_error",
                           "原图摘要校验失败，已中止发布")
        boxes = validate_boxes(boxes, doc["width"], doc["height"])
        png_data, width, height = render_published(
            original_data, doc["content_type"], boxes
        )
        png_sha = sha256_hex(png_data)

        db.insert_release(
            conn,
            release_id=release_id,
            doc_id=doc_id,
            revision=revision,
            source_sha256=doc["sha256"],
            png_sha256=png_sha,
            width=width,
            height=height,
            byte_size=len(png_data),
            box_count=len(boxes),
            boxes_json=db.encode_boxes(boxes),
            created_at=now,
        )
        # 先落盘再提交：提交成功后成品字节即不可变
        storage.save_atomic(storage.release_path(release_id), png_data)

        response.status_code = 201
        result = {
            "release_id": release_id,
            "document_id": doc_id,
            "revision": revision,
            "source_sha256": doc["sha256"],
            "png_sha256": png_sha,
            "png_width": width,
            "png_height": height,
            "box_count": len(boxes),
            "boxes": [_box_dto(b) for b in boxes],
            "created_at": now,
            "public_url": f"/releases/{release_id}",
            "content_url": f"/releases/{release_id}/content",
        }
    return result


@app.get("/documents/{doc_id}/releases")
def list_document_releases(doc_id: str) -> dict:
    with db.transaction() as conn:
        _require_document(conn, doc_id)
        rows = db.list_releases(conn, doc_id)
        return {
            "document_id": doc_id,
            "releases": [
                {
                    "release_id": r["id"],
                    "revision": r["revision"],
                    "source_sha256": r["source_sha256"],
                    "png_sha256": r["png_sha256"],
                    "width": r["width"],
                    "height": r["height"],
                    "box_count": r["box_count"],
                    "created_at": r["created_at"],
                    "public_url": f"/releases/{r['id']}",
                    "content_url": f"/releases/{r['id']}/content",
                }
                for r in rows
            ],
        }


# ---------- 公开只读 ----------

def _require_release(conn: sqlite3.Connection, release_id: str) -> sqlite3.Row:
    row = db.get_release(conn, release_id)
    if row is None:
        # 不区分“未知版本”与“不存在”，一律明确报错
        raise ApiError(404, "release_not_found",
                       f"未知发布版本: {release_id}")
    return row


def _release_meta(row: sqlite3.Row) -> dict:
    return {
        "release_id": row["id"],
        "document_id": row["document_id"],
        "revision": row["revision"],
        "source_sha256": row["source_sha256"],
        "png_sha256": row["png_sha256"],
        "width": row["width"],
        "height": row["height"],
        "box_count": row["box_count"],
        "boxes": [_box_dto(b) for b in db.decode_boxes(row["boxes"])],
        "created_at": row["created_at"],
        "content_url": f"/releases/{row['id']}/content",
    }


@app.get("/releases/{release_id}")
def get_public_release(release_id: str) -> dict:
    """公开读取：只返回指定发布版的元信息与遮盖坐标/原因（不含原图字节）。"""
    with db.transaction() as conn:
        row = _require_release(conn, release_id)
        return _release_meta(row)


@app.get("/releases/{release_id}/content")
def get_public_release_content(release_id: str) -> Response:
    """公开读取：返回脱敏后的发布 PNG 成品，永不返回原图。"""
    with db.transaction() as conn:
        row = _require_release(conn, release_id)
    png_bytes = storage.read_file(storage.release_path(row["id"]))
    if sha256_hex(png_bytes) != row["png_sha256"]:
        raise ApiError(500, "integrity_error", "发布成品摘要校验失败")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{release_id}.png"',
            "X-Release-Revision": str(row["revision"]),
            "Cache-Control": "immutable, max-age=31536000",
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
