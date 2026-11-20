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
import os
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from data_agent._registry_payloads import _AUDIT_SAMPLE_SALES_CSV
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
        saved = workspace.save_upload_stream(
            file.filename or "dataset.csv", file.file, settings.max_upload_bytes
        )
        if saved.stat().st_size == 0:
            raise ValueError("上传文件为空。")
        workspace.load(saved)
        rows, columns = len(workspace.dataframe), len(workspace.dataframe.columns)
        if rows > settings.max_rows or rows * columns > settings.max_cells:
            raise ValueError(
                f"数据规模超过限制：最多 {settings.max_rows:,} 行或 {settings.max_cells:,} 个单元格。"
            )
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


@router.get("/api/samples")
def list_samples() -> dict[str, Any]:
    """列出内置演示数据及其一句话亮点。"""
    from data_agent.demo_catalog import demo_metadata

    return {"samples": demo_metadata()}


@router.post("/api/sessions/sample", status_code=201)
def create_sample_session(audit: bool = False, kind: str | None = None) -> dict[str, Any]:
    """用内置演示数据创建会话，供新用户快速体验。

    ``kind`` 选择演示目录中的数据集；不传时保持旧的 ``audit`` 布尔行为。
    带口径/策略/血缘的演示会一并写入工作区，加载后即可直接核验。
    """
    from data_agent import api
    from data_agent.demo_catalog import (
        DEMOS,
        demo_lines,
        generate_bigdata,
        generate_growth,
        lineage_for,
    )
    from data_agent.workspace import DataWorkspace

    settings = api.bootstrap_settings
    session_id = f"api_{uuid4().hex[:12]}"
    workspace = DataWorkspace(settings.runs_dir, session_id=session_id)
    try:
        demo = demo_lines(kind) if kind else None
        if demo is None:
            filename, sample_csv = (
                ("sample_sales_audit.csv", _AUDIT_SAMPLE_SALES_CSV)
                if audit
                else ("sample_sales.csv", _SAMPLE_SALES_CSV)
            )
        else:
            filename = demo.filename
            if demo.generated:
                sample_csv = generate_bigdata() if demo.kind == "bigdata" else generate_growth()
            else:
                sample_csv = demo.csv
        saved = workspace.save_upload_stream(
            filename,
            io.BytesIO(sample_csv.encode("utf-8")),
            settings.max_upload_bytes,
        )
        workspace.load(saved)
        dataframe = workspace.dataframe
        # 演示是可信常量，可以大于普通上传上限，但仍设硬上限，避免无界加载。
        demo_limit = 250_000 if demo is not None and kind == "bigdata" else settings.max_rows
        if (
            len(dataframe) > demo_limit
            or len(dataframe) * len(dataframe.columns) > settings.max_cells
        ):
            raise ValueError(
                f"演示数据规模超过限制：最多 {demo_limit:,} 行或 {settings.max_cells:,} 个单元格。"
            )
        if demo is not None:
            if demo.schema is not None:
                from data_agent.audit_schema import approve_schema, save_schema

                save_schema(workspace.root, demo.schema, author="演示数据组")
                approve_schema(workspace.root, "质量与合规组")
            if demo.policy is not None:
                from data_agent.audit_policy import save_policy

                save_policy(workspace.root, demo.policy)
            lineage = lineage_for(demo.kind)
            if lineage is not None:
                from data_agent.audit_lineage import save_lineage

                save_lineage(workspace.root, lineage)
    except Exception as exc:  # noqa: BLE001 —— 示例数据为常量，失败属配置问题
        workspace.cleanup()
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail="示例数据初始化失败。") from exc
    actual_id, record = api.registry.create(workspace)
    insights = None
    if kind:
        from data_agent.demo_insights import insights_for

        insights = insights_for(kind, sample_csv, DEMOS[kind].policy)
        if insights:
            from data_agent.demo_artifacts import write_demo_chart_artifacts

            write_demo_chart_artifacts(workspace, insights)
    payload = _session_payload(actual_id, record)
    if kind:
        payload["demo"] = DEMOS[kind].public()
        payload["demo"]["insights"] = insights
    return payload


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    from data_agent import api

    return _session_payload(session_id, api.registry.get(session_id))


class CommentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    author: str = Field(default="", max_length=40)


@router.get("/api/sessions/{session_id}/comments")
def list_session_comments(session_id: str) -> dict[str, Any]:
    from data_agent import api
    from data_agent.comments import list_comments

    workspace = api.registry.get(session_id).workspace
    return {"session_id": session_id, "comments": list_comments(workspace.root)}


@router.post("/api/sessions/{session_id}/comments", status_code=201)
def create_session_comment(
    session_id: str, payload: CommentRequest, request: Request
) -> dict[str, Any]:
    from data_agent import api
    from data_agent.comments import add_comment

    workspace = api.registry.get(session_id).workspace
    actor = str(getattr(request.state, "actor", "") or "")
    author = payload.author.strip() or ({"admin": "管理员", "viewer": "访客"}.get(actor, ""))
    try:
        return add_comment(workspace.root, payload.text, author)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/api/sessions/{session_id}/comments/{comment_id}")
def delete_session_comment(session_id: str, comment_id: str) -> dict[str, bool]:
    from data_agent import api
    from data_agent.comments import delete_comment

    workspace = api.registry.get(session_id).workspace
    if not delete_comment(workspace.root, comment_id):
        raise HTTPException(status_code=404, detail="评论不存在。")
    return {"deleted": True}


class SalesAuditRequest(BaseModel):
    draft: str = Field(min_length=1, max_length=6000)
    confirm_full_table: bool = False
    # 可选：快速起草返回的轨迹哈希。带上它即要求草稿与模型原话逐字对应。
    draft_trace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@router.post("/api/sessions/{session_id}/sales-audit")
def sales_audit(session_id: str, payload: SalesAuditRequest) -> dict[str, Any]:
    """Recalculate against the uploaded CSV, independently of the analysis state."""
    from data_agent import api
    from data_agent.sales_audit import audit_sales_brief

    workspace = api.registry.get(session_id).workspace
    source = workspace.source_path
    if (
        source is None
        or source.suffix.lower() != ".csv"
        or workspace.input_dir.resolve() not in source.resolve().parents
    ):
        raise HTTPException(status_code=422, detail="经营简报核验需要会话上传的原始 CSV。")
    if not payload.draft.strip():
        raise HTTPException(status_code=422, detail="请输入待核验的简报草稿。")
    trace_digest = payload.draft_trace_sha256
    if trace_digest:
        from data_agent.audit_draft import TRACE_DIR_NAME, QuickDraftError, verify_trace

        try:
            verify_trace(workspace.root / TRACE_DIR_NAME, trace_digest, payload.draft)
        except QuickDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        from data_agent.audit_schema import load_schema, load_schema_document

        metric_specs = load_schema(workspace.root)
        schema_document = load_schema_document(workspace.root)
        from data_agent.audit_policy import (
            load_policy,
            policy_fingerprint,
            rls_filter_hash,
            rls_value,
        )

        policy = load_policy(workspace.root)
        rls_column, rls_value_matched = rls_value(policy) if policy else (None, None)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        result = audit_sales_brief(
            source,
            payload.draft,
            confirm_full_table=payload.confirm_full_table,
            max_rows=api.bootstrap_settings.max_rows,
            draft_trace_sha256=trace_digest,
            metric_specs=metric_specs,
            pii_columns=tuple(policy.get("pii_columns", ())) if policy else (),
            rls_column=rls_column,
            rls_value=rls_value_matched,
            policy_fingerprint=policy_fingerprint(policy) if policy else None,
        )
        result["schema_status"] = (schema_document or {}).get("status")
        policy_sha = policy_fingerprint(policy) if policy else None
        rls_sha = rls_filter_hash(rls_column, rls_value_matched or "") if rls_column else None
        if policy:
            result["policy"] = {
                "principal_id": policy["principal_id"],
                "pii_columns": list(policy.get("pii_columns", ())),
                "rls_column": rls_column,
                "sha256": policy_sha,
            }
        from data_agent.audit_lineage import freshness, lineage_fingerprint, load_lineage

        lineage = load_lineage(workspace.root)
        lineage_sha = None
        if lineage:
            lineage_sha = lineage_fingerprint(lineage)
            state = freshness(lineage)
            result["lineage"] = {**lineage, **state, "sha256": lineage_sha}
            if state["stale"]:
                result.setdefault("warnings", []).append(
                    {
                        "code": "source_stale",
                        "message": (
                            "上游数据已超过新鲜度 SLA："
                            f"{state['age_hours']} 小时 > {state['freshness_sla_hours']} 小时。"
                        ),
                        "age_hours": state["age_hours"],
                        "freshness_sla_hours": state["freshness_sla_hours"],
                    }
                )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail="无法读取原始 CSV，请重新上传数据。") from exc
    # 每次复算都留痕；写账本失败不影响本次核验结果，只附加说明。
    from data_agent.audit_ledger import LedgerError, append_entry, sha256_text

    try:
        entry = append_entry(
            workspace.root,
            kind="audit",
            session_id=session_id,
            payload={
                "audit_id": result["audit_id"],
                "source_sha256": result["source"]["sha256"],
                "source_filename": result["source"]["filename"],
                "draft_sha256": sha256_text(payload.draft),
                "draft_trace_sha256": trace_digest,
                "schema_fingerprint": result.get("schema_fingerprint"),
                "lineage_sha256": lineage_sha,
                "principal_id": policy["principal_id"] if policy else None,
                "policy_sha256": policy_sha,
                "rls_filter_hash": rls_sha,
                "confirm_full_table": payload.confirm_full_table,
                "summary": result["summary"],
            },
        )
        result["ledger"] = {
            "seq": entry["seq"],
            "hash": entry["hash"],
            "prev": entry["prev"],
        }
    except LedgerError as exc:
        result["ledger_error"] = str(exc)
    return result


@router.delete("/api/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    """删除会话及其全部产物。

    清理内存记录、工作区目录（input/artifacts/session.json 等）和远端
    对象存储归档。运行中的会话返回 409，调用方应先取消分析再删除。
    """
    from data_agent import api

    api.registry.delete(session_id)
    return Response(status_code=204)


@router.get("/api/sessions/{session_id}/metrics")
def get_session_metrics(session_id: str) -> dict[str, Any]:
    """读取当前会话生效的指标口径（全局环境变量优先于工作区文件）。"""
    from data_agent import api
    from data_agent.metrics import (
        METRICS_FILENAME,
        METRICS_PATH_ENV,
        load_metrics_for_workspace,
        serialize_metric_definitions,
    )

    record = api.registry.get(session_id)
    configured = os.environ.get(METRICS_PATH_ENV, "").strip()
    env_file = Path(configured).expanduser() if configured else None
    env_overrides = bool(env_file and env_file.is_file())
    workspace_file = Path(record.workspace.root) / METRICS_FILENAME
    try:
        definitions = load_metrics_for_workspace(record.workspace.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source = "env" if env_overrides else ("workspace" if workspace_file.is_file() else None)
    return {
        "metrics": serialize_metric_definitions(definitions)["metrics"],
        "source": source,
        "env_overrides": env_overrides,
    }


@router.put("/api/sessions/{session_id}/metrics")
def put_session_metrics(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把指标口径写入本会话工作区的 metrics.json。"""
    from data_agent import api
    from data_agent.metrics import (
        METRICS_PATH_ENV,
        save_workspace_metrics,
        serialize_metric_definitions,
    )

    configured = os.environ.get(METRICS_PATH_ENV, "").strip()
    if configured and Path(configured).expanduser().is_file():
        raise HTTPException(
            status_code=409,
            detail="当前使用全局 DATA_AGENT_METRICS_PATH，无法在会话内改写口径。",
        )
    record = api.registry.get(session_id)
    try:
        definitions = save_workspace_metrics(record.workspace.root, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "metrics": serialize_metric_definitions(definitions)["metrics"],
        "source": "workspace",
        "env_overrides": False,
    }


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
def export_session(session_id: str) -> Response:
    """导出会话完整状态为 ZIP 归档。

    将会话工作区根目录下所有文件（input/、artifacts/、session.json、
    workspace_state.parquet 等）打包成 ZIP 返回。前端下载后可在其他实例
    通过 /api/sessions/import 导入恢复完整会话状态。

    安全：仅打包会话根目录内的文件，不跟随符号链接（防止通过 symlink
    泄漏宿主任意文件）；账本签名私钥（audit_keys/*.key）**永不导出**。
    内存：打包到临时文件后用 FileResponse 流式发送，响应结束即删除——
    大产物不会让"源数据 + 压缩包"两份同时驻留内存（2026-11-20 收口）。
    """
    from data_agent import api
    from data_agent._downloads import zip_file_response
    from data_agent.audit_signing import KEY_DIR

    record = api.registry.get(session_id)
    root = record.workspace.root

    def write(bundle) -> None:  # type: ignore[no-untyped-def]
        for path in sorted(root.rglob("*")):
            # 跳过符号链接：rglob 默认跟随 symlink，若工作区被植入
            # 指向 /etc/passwd 的链接，导出的 ZIP 会泄漏宿主文件。
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == KEY_DIR and relative.name.endswith(".key"):
                continue
            bundle.write(path, relative)

    return zip_file_response(f"{session_id}.zip", write)


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
                        raise HTTPException(status_code=400, detail="归档包含不安全路径。")
                    total_size += member.file_size
                    if total_size > _MAX_UNCOMPRESSED_BYTES:
                        raise HTTPException(status_code=400, detail="归档解压后过大（上限 1GB）。")
                bundle.extractall(root)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise HTTPException(status_code=400, detail="无效的 ZIP 文件。") from exc
    except HTTPException:
        # 上传超限/成员过多/路径逃逸/坏 ZIP：session 目录还没注册进 registry，
        # 不清理会留下 TTL 永远回收不到的孤儿空目录（413 路径此前就在漏）。
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
