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

    title = str(payload.get("title", "")).strip()
    cleaned = api.registry.rename(session_id, title)
    return {"title": cleaned}


@router.get("/api/sessions/{session_id}/export")
def export_session(session_id: str) -> StreamingResponse:
    """导出会话完整状态为 ZIP 归档。

    将会话工作区根目录下所有文件（input/、artifacts/、session.json、
    workspace_state.parquet 等）打包成 ZIP 流式返回。前端下载后可在
    其他实例通过 /api/sessions/import 导入恢复完整会话状态。

    安全：仅打包会话根目录内的文件，不跟随符号链接；rglob("*") 遍历
    后通过 path.is_file() 过滤掉目录，避免 ZipFile 写入空目录条目。
    """
    import zipfile

    from data_agent import api

    record = api.registry.get(session_id)
    root = record.workspace.root
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
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
    - 无效 ZIP 返回 400；解压后 manifest 读取失败返回 400 并清理目录。
    - 生成新的 session_id，避免与已有会话冲突。
    """
    import shutil
    import zipfile

    from data_agent import api

    session_id = f"api_{uuid4().hex[:12]}"
    root = api.bootstrap_settings.runs_dir / session_id
    root.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            # 路径遍历防护：先校验所有成员再解压，避免半解压后才发现
            # 恶意路径。target.resolve() 后检查 root 是否在其父链上。
            for member in bundle.infolist():
                target = (root / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise HTTPException(status_code=400, detail="归档包含不安全路径。")
            bundle.extractall(root)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="无效的 ZIP 文件。") from exc
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    record = api.registry.restore_from_directory(session_id)
    if record is None:
        # manifest 损坏或缺少 input 文件：清理临时目录并返回 400。
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="导入的会话归档无效。")
    return _session_payload(session_id, record)
