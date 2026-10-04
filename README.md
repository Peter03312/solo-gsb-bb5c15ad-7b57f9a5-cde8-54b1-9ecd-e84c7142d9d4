# 档案影像脱敏发布服务

基于 **Python · FastAPI · Pillow · SQLite** 的档案影像脱敏发布服务。

档案员上传 PNG/JPEG 原图后，在**按 EXIF 方向旋正之后的像素坐标系**上标注敏感矩形及原因；标注修改采用**修订号乐观锁**，过期修改会被拒绝。发布时把当前标注矩形**不可逆地涂实**，生成一份**独立 PNG**，同时**剥离原图全部元数据**，并在数据库中固化：

- 原图 SHA-256 摘要
- 发布所依据的标注修订号
- 发布成品 PNG 的 SHA-256 摘要

发布完成后，即使标注继续修改，已发布成品的字节与记录都**不会再变化**。没有任何标注也可以发布，得到一份仅去元数据的 PNG。

公开接口**只返回指定发布版**的成品及其遮盖坐标、原因，**任何接口都不返回原图**。

---

## 一、一键启动（Docker Compose）

```bash
# 可选：复制环境变量模板并调整端口等配置
cp .env.example .env

# 构建镜像、建卷、初始化数据库并启动
docker compose up -d --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| 服务地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| OpenAPI 文档（可在线调用） | <http://localhost:8000/docs> |
| OpenAPI JSON | <http://localhost:8000/openapi.json> |

停止 / 查看日志：

```bash
docker compose logs -f
docker compose down            # 停止；保留数据卷
docker compose down -v         # 停止并删除所有已存数据
```

数据全部位于命名卷 `archive-data`（容器内 `/data`）：

```
/data/app.db              SQLite 数据库（WAL 模式）
/data/originals/<doc_id>  原图字节，仅服务端可读，无任何 HTTP 接口暴露
/data/releases/<rel_id>.png  已发布的脱敏成品，不可变
```

## 二、不用 Docker 的本地启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export DATA_DIR=./data
python -m scripts.init_db                       # 初始化目录与表结构
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 三、配置

全部通过环境变量配置，均有默认值：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT`（仅 compose） | `8000` | 宿主机映射端口；容器内固定 8000 |
| `DATA_DIR` | `/data` | 数据库与影像文件根目录 |
| `DATABASE_PATH` | `$DATA_DIR/app.db` | SQLite 文件路径 |
| `ORIGINALS_DIR` | `$DATA_DIR/originals` | 原图存储目录 |
| `RELEASES_DIR` | `$DATA_DIR/releases` | 发布 PNG 存储目录 |
| `MAX_UPLOAD_MB` | `20` | 上传字节上限（MB） |
| `MAX_IMAGE_PIXELS` | `60000000` | 解码像素上限（防解压炸弹） |
| `REDACTION_COLOR` | `000000` | 遮盖矩形填充色，十六进制 RGB，默认纯黑 |
| `SQLITE_BUSY_TIMEOUT_MS` | `15000` | SQLite 写锁等待超时 |
| `UVICORN_WORKERS` | `1` | uvicorn worker 数 |

## 四、业务规则

1. **坐标基准**：服务端用 Pillow `exif_transpose` 旋正后再返回 `width/height`，所有 `x/y/width/height` 均基于旋正后的图像，原点为左上角。
2. **修订号**：上传后初始修订号为 `0`；每次整体替换标注集合 `revision += 1`。修改必须回传 `expected_revision`，与服务端不一致返回 `409 revision_conflict`，需重新 GET 标注后再提交。
3. **发布固化**：发布在单个 `BEGIN IMMEDIATE` 事务中读取修订号、写发布记录并落盘 PNG，因此**并发的标注修改无法穿插**，成品像素与记录必然对应同一修订。同一文档同一修订重复发布是幂等的，返回同一份既有成品。
4. **不可逆涂实**：遮盖区域以纯色矩形填充，不保留原像素；成品重新编码为独立 PNG，不写入 EXIF / ICC / 注释等任何元数据。
5. **公开最小化**：`/releases/{id}` 只给元数据和遮盖坐标/原因；`/releases/{id}/content` 只给发布 PNG。原图没有任何读取出口。
6. **无标注发布**：`boxes` 为空时正常发布，得到与旋正后原图同尺寸、仅去元数据的 PNG。
7. **明确报错**：空矩形、负坐标/越界、非整数、原因为空、解码失败、错误 Content-Type、未知发布版本、修订号过期均返回结构化错误（见下表）。

### 错误响应格式

```json
{ "error": { "code": "invalid_box", "message": "标注矩形不合法", "details": ["boxes[0] 为空矩形（width/height 必须 ≥ 1，实际 0×10）"] } }
```

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 409 | `revision_conflict` | 修改标注携带的修订号过期 |
| 422 | `invalid_box` | 空矩形 / 越界 / 负坐标 / 非整数 / 原因为空等 |
| 422 | `decode_failed` | 上传字节无法解码为有效 PNG/JPEG、格式不符、像素超限 |
| 415 | `unsupported_media_type` | Content-Type 不是 image/png 或 image/jpeg |
| 413 | `file_too_large` | 超过 `MAX_UPLOAD_MB` |
| 404 | `document_not_found` / `release_not_found` | 文档或发布版本不存在（未知版本） |
| 500 | `integrity_error` | 原图/成品摘要与记录不符（存储被破坏） |

## 五、HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 上传原图（multipart 字段 `file`，PNG/JPEG），返回文档 ID、旋正后尺寸、原图摘要、`revision=0` |
| GET | `/documents/{doc_id}` | 读取文档信息与当前标注（档案员侧） |
| PUT | `/documents/{doc_id}/annotations` | 整体替换标注，需带 `expected_revision` |
| POST | `/documents/{doc_id}/releases` | 按当前修订发布脱敏 PNG（幂等） |
| GET | `/documents/{doc_id}/releases` | 列出该文档所有发布版 |
| GET | `/releases/{release_id}` | **公开**：发布版元信息 + 遮盖坐标/原因 |
| GET | `/releases/{release_id}/content` | **公开**：发布成品 PNG（`image/png`），永不返回原图 |
| GET | `/health` | 健康检查 |

## 六、调用示例

### 1. 上传原图

```bash
curl -s -X POST http://localhost:8000/documents \
  -F "file=@sample.jpg;type=image/jpeg"
```

```json
{
  "document_id": "doc_3f2a...",
  "width": 4032, "height": 3024,
  "source_sha256": "9c1b…",
  "revision": 0, "boxes": []
}
```

### 2. 提交标注（首次，expected_revision=0）

```bash
DOC=doc_3f2a...
curl -s -X PUT http://localhost:8000/documents/$DOC/annotations \
  -H "Content-Type: application/json" \
  -d '{
    "expected_revision": 0,
    "boxes": [
      {"x": 120, "y": 80,  "width": 300, "height": 60, "reason": "身份证号"},
      {"x": 120, "y": 200, "width": 300, "height": 60, "reason": "家庭住址"}
    ]
  }'
```

返回 `"revision": 1`。再带 `expected_revision: 0` 提交会得到 `409 revision_conflict`。

### 3. 发布

```bash
curl -s -X POST http://localhost:8000/documents/$DOC/releases
```

```json
{
  "release_id": "rel_91ab...",
  "revision": 1,
  "source_sha256": "9c1b…",
  "png_sha256": "d04e…",
  "box_count": 2,
  "boxes": [ {"x": 120, "y": 80, "width": 300, "height": 60, "reason": "身份证号"}, ... ],
  "public_url": "/releases/rel_91ab...",
  "content_url": "/releases/rel_91ab.../content"
}
```

### 4. 此后改标注，再发布

修改标注（`expected_revision=1`）后重新 POST 发布，会得到**新的 release_id、revision=2**；旧 release 的 PNG 字节、摘要与坐标保持不变。

### 5. 公开读取（只暴露发布版）

```bash
REL=rel_91ab...
curl -s http://localhost:8000/releases/$REL                 # 元信息 + 遮盖坐标/原因
curl -s -OJ http://localhost:8000/releases/$REL/content     # 下载脱敏 PNG
```

### 错误示例：空矩形 / 越界

```bash
curl -i -X PUT http://localhost:8000/documents/$DOC/annotations \
  -H "Content-Type: application/json" \
  -d '{"expected_revision": 1, "boxes": [{"x": 0, "y": 0, "width": 0, "height": 10, "reason": "x"}]}'
# HTTP/1.1 422
# {"error":{"code":"invalid_box","details":["boxes[0] 为空矩形（width/height 必须 ≥ 1，实际 0×10）"]}}
```

### 一键跑通的 Python 示例

见 [`examples/demo.py`](examples/demo.py)，它会生成一张带 EXIF 方向的 JPEG，演示上传 → 标注 → 过期被拒 → 发布 → 公开读取校验像素与摘要 → 改标注再发布并验证旧成品不变的完整流程：

```bash
pip install httpx
python examples/demo.py                       # 默认 http://localhost:8000
BASE_URL=http://127.0.0.1:8000 python examples/demo.py
```
