"""会话 CRUD 路由。

- POST /api/sessions：上传数据文件创建分析会话。
- POST /api/sessions/sample：用内置示例数据创建会话。
- GET  /api/sessions：列出历史会话摘要。
- GET  /api/sessions/{id}：获取会话详情。
- DELETE /api/sessions/{id}：删除会话及其产物。
- PATCH /api/sessions/{id}：重命名会话（自定义标题）。
- GET  /api/sessions/{id}/export：导出会话为 ZIP。
- POST /api/sessions/import：导入 ZIP 恢复会话。
"""

from __future__ import annotations

import io
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

from data_agent.registry import _SAMPLE_SALES_CSV, _session_payload

router = APIRouter()


@router.get("/api/sessions")
def list_sessions(limit: int = 30) -> dict[str, Any]:
    """List recent sessions for the sidebar history panel.

    仅返回 manifest 摘要（id、filename、status、created_at、has_result），
    不加载 DataFrame，确保接口在 runs/ 有几十上百个会话时仍然很快。
    """
    from data_agent import api

    capped = max(1, min(int(limit), 100))
    return {"sessions": api.registry.list_recent(limit=capped)}


@router.post("/api/sessions", status_code=201)
async def create_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    from data_agent import api
    from data_agent.workspace import DataWorkspace

    # Resource limits and the run directory are process-level deployment
    # settings. Reuse the startup snapshot so uploads cannot drift into a
    # different directory when the environment changes mid-process.
    settings = api.bootstrap_settings
    session_id = f"api_{uuid4().hex[:12]}"
    workspace = DataWorkspace(settings.runs_dir, session_id=session_id)
    try:
        saved = workspace.save_upload_stream(file.filename or "dataset.csv", file.file, settings.max_upload_bytes)
        if saved.stat().st_size == 0:
            raise ValueError("上传文件为空。")
        workspace.load(saved)
        rows, columns = len(workspace.dataframe), len(workspace.dataframe.columns)
        if rows > settings.max_rows or rows * columns > settings.max_cells:
            raise ValueError(f"数据规模超过限制：最多 {settings.max_rows:,} 行或 {settings.max_cells:,} 个单元格。")
    except Exception as exc:
        # pd.read_parquet 损坏文件抛 pyarrow.ArrowInvalid，pd.read_excel 抛
        # openpyxl.exceptions.InvalidFileException，都不在 ValueError/OSError 子类内。
        # 之前只捕获 (ValueError, OSError) 会漏掉这些异常，留下孤儿 workspace 目录
        # （registry.create 未执行，TTL prune 也清理不到）。通用 Exception 兜底确保
        # 任何 load 失败都会清理临时目录。
        workspace.cleanup()
        # 已知的业务错误返回 422，未知异常返回 500 避免暴露内部细节。
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="数据文件解析失败，请检查格式。") from exc
    actual_id, record = api.registry.create(workspace)
    return _session_payload(actual_id, record)


@router.post("/api/sessions/sample", status_code=201)
async def create_sample_session() -> dict[str, Any]:
    """用内置销售示例数据创建会话，供新用户快速体验。"""
    from data_agent import api
    from data_agent.workspace import DataWorkspace

    settings = api.bootstrap_settings
    session_id = f"api_{uuid4().hex[:12]}"
    workspace = DataWorkspace(settings.runs_dir, session_id=session_id)
    try:
        saved = workspace.save_upload_stream(
            "sample_sales.csv",
            io.BytesIO(_SAMPLE_SALES_CSV.encode("utf-8")),
            settings.max_upload_bytes,
        )
        workspace.load(saved)
    except Exception as exc:  # noqa: BLE001 —— 示例数据为常量，失败属配置问题
        workspace.cleanup()
        raise HTTPException(status_code=500, detail="示例数据初始化失败。") from exc
    actual_id, record = api.registry.create(workspace)
    return _session_payload(actual_id, record)


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    from data_agent import api

    return _session_payload(session_id, api.registry.get(session_id))


@router.delete("/api/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    """删除会话及其全部产物。

    清理内存记录、工作区目录（input/artifacts/session.json 等）和远端
    对象存储归档。运行中的会话返回 409，调用方应先取消分析再删除。
    """
    from data_agent import api

    api.registry.delete(session_id)
    return Response(status_code=204)


@router.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, payload: dict[str, Any]) -> dict[str, str]:
    """重命名会话（更新自定义标题）。

    请求体 ``{"title": "新名称"}``，空串清除自定义标题回退 filename。
    持久化到 session.json + 远端归档，返回清洗后的 title。
    """
    from data_agent import api

    # None（显式清除）与空串语义一致：都视为未设置，回退显示 filename。
    # 不能 str(None) 把 null 变成字面量 "None" 存进会话标题。
    title = str(payload.get("title") or "").strip()
    cleaned = api.registry.rename(session_id, title)
    return {"title": cleaned}


@router.get("/api/sessions/{session_id}/export")
def export_session(session_id: str) -> StreamingResponse:
    """导出会话完整状态为 ZIP 归档。

    将会话工作区根目录下所有文件（input/、artifacts/、session.json、
    workspace_state.parquet 等）打包成 ZIP 流式返回。前端下载后可在
    其他实例通过 /api/sessions/import 导入恢复完整会话状态。

    安全：仅打包会话根目录内的文件，不跟随符号链接（防止通过 symlink
    泄漏宿主任意文件）；rglob("*") 遍历后通过 path.is_file() 且
    not path.is_symlink() 过滤，避免 ZipFile 写入空目录条目和链接目标。
    """
    import zipfile

    from data_agent import api

    record = api.registry.get(session_id)
    root = record.workspace.root
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            # 跳过符号链接：rglob 默认跟随 symlink，若工作区被植入
            # 指向 /etc/passwd 的链接，导出的 ZIP 会泄漏宿主文件。
            if path.is_file() and not path.is_symlink():
                bundle.write(path, path.relative_to(root))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.zip"'},
    )


@router.post("/api/sessions/import", status_code=201)
async def import_session(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """导入会话 ZIP 归档，创建新会话。

    接收前端通过 /export 下载的 ZIP，解压到新的 runs/<session_id> 目录，
    调用 ``restore_from_directory`` 读取 manifest 并恢复工作区状态。

    安全：
    - 路径遍历防护：解压前遍历归档成员，校验每个解压目标必须位于
      会话根目录内，防止 ``../`` 等恶意路径逃逸。
    - Zip bomb 防护：限制上传大小（复用 max_upload_bytes）、解压后
      累计大小上限（1GB）、成员数量上限（10000），防止高压缩比 ZIP
      或海量小文件导致 OOM 或磁盘耗尽。
    - 无效 ZIP 返回 400；解压后 manifest 读取失败返回 400 并清理目录。
    - 生成新的 session_id，避免与已有会话冲突。
    """
    import shutil
    import zipfile

    from data_agent import api

    session_id = f"api_{uuid4().hex[:12]}"
    # 必须 resolve 为绝对路径：bootstrap_settings.runs_dir 可能是相对路径
    # （如 .env 默认的 ./runs），而下方校验用 (root / member.filename)
    # .resolve() 得到绝对路径做 parents 包含判断；root 保持相对形式时
    # 绝对路径的 parents 里永远找不到相对 root，导入一律误报
    # "归档包含不安全路径"。
    root = (api.bootstrap_settings.runs_dir / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Zip bomb 防护：限制上传大小、解压总大小、成员数量
    _MAX_IMPORT_BYTES = api.bootstrap_settings.max_upload_bytes
    _MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024  # 1GB
    _MAX_ZIP_MEMBERS = 10_000
    # 流式写入磁盘，避免大文件全量读入内存导致 OOM
    # （与 create_session 的 save_upload_stream 保持一致）
    tmp_zip = root / "_import.zip"
    try:
        total_uploaded = 0
        with tmp_zip.open("wb") as target:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_uploaded += len(chunk)
                if total_uploaded > _MAX_IMPORT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"归档过大（上限 {_MAX_IMPORT_BYTES // 1024 // 1024}MB）。",
                    )
                target.write(chunk)
        try:
            with zipfile.ZipFile(tmp_zip) as bundle:
                members = bundle.infolist()
                if len(members) > _MAX_ZIP_MEMBERS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"归档成员过多（上限 {_MAX_ZIP_MEMBERS} 个）。",
                    )
                # 路径遍历防护 + 累计大小校验：先校验所有成员再解压
                total_size = 0
                for member in members:
                    member_target = (root / member.filename).resolve()
                    if member_target != root and root not in member_target.parents:
                        raise HTTPException(
                            status_code=400, detail="归档包含不安全路径。"
                        )
                    total_size += member.file_size
                    if total_size > _MAX_UNCOMPRESSED_BYTES:
                        raise HTTPException(
                            status_code=400, detail="归档解压后过大（上限 1GB）。"
                        )
                bundle.extractall(root)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise HTTPException(status_code=400, detail="无效的 ZIP 文件。") from exc
        except HTTPException:
            shutil.rmtree(root, ignore_errors=True)
            raise
    finally:
        tmp_zip.unlink(missing_ok=True)
    record = api.registry.restore_from_directory(session_id)
    if record is None:
        # manifest 损坏或缺少 input 文件：清理临时目录并返回 400。
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="导入的会话归档无效。")
    return _session_payload(session_id, record)
