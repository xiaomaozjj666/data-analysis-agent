"""溯源链：记录、校验、篡改检测与 API 导出。"""

from __future__ import annotations

import json
import threading

from fastapi.testclient import TestClient

from data_agent import api, provenance
from data_agent.agent import AnalysisResult
from data_agent.config import AgentSettings
from data_agent.storage import LocalSessionStorage
from data_agent.workspace import DataWorkspace


def _session_root(tmp_path) -> tuple[DataWorkspace, str]:
    workspace = DataWorkspace(tmp_path / "runs", session_id="prov")
    source = workspace.save_upload("sales.csv", b"region,sales\nEast,100\nWest,200\n")
    workspace.load(source)
    response = "# 报告\n\n东部销售额 100。"
    (workspace.root / "session.json").write_text(
        json.dumps({"last_result": {"response": response}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return workspace, response


def _record(workspace: DataWorkspace, response: str, **kwargs) -> dict:
    artifact = workspace.artifacts_dir / "chart.html"
    artifact.write_text("<html>chart</html>", encoding="utf-8")
    trace = [
        {"type": "tool_call", "name": "run_sql", "detail": "SELECT 1"},
        {"type": "tool_result", "name": "run_sql", "detail": "1"},
    ]
    artifacts = [
        {
            "name": artifact.name,
            "kind": "chart",
            "path": str(artifact),
            "description": "销售图",
        }
    ]
    return provenance.record_analysis(
        workspace.root,
        task="分析销售",
        response=response,
        trace=trace,
        artifacts=artifacts,
        dataset_path=workspace.source_path,
        provider="deepseek",
        model="deepseek-flash",
        code_version="1.0.0",
        **kwargs,
    )


def test_record_and_verify_roundtrip(tmp_path):
    workspace, response = _session_root(tmp_path)
    summary = _record(workspace, response)

    assert summary["entries"] == 6  # meta + dataset + 2 tools + artifact + report
    assert summary["tool_calls"] == 1
    assert summary["artifacts"] == 1
    assert len(summary["root_hash"]) == 64

    result = provenance.verify(workspace.root)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["entries"] == summary["entries"]
    assert result["root_hash"] == summary["root_hash"]
    assert result["checks"]["chain"] is True


def test_verify_missing_provenance(tmp_path):
    workspace, _ = _session_root(tmp_path)
    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert result["exists"] is False
    assert provenance.load_summary(workspace.root) is None


def test_tampered_entry_breaks_chain(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    path = workspace.root / provenance.PROVENANCE_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["data"]["size"] += 1
    lines[1] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("hash 校验失败" in item for item in result["errors"])


def test_tampered_dataset_detected(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    workspace.source_path.write_bytes(b"region,sales\nEast,999\nWest,200\n")

    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("数据集哈希不匹配" in item for item in result["errors"])


def test_tampered_artifact_detected(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    (workspace.artifacts_dir / "chart.html").write_text("edited", encoding="utf-8")

    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("产物哈希不匹配" in item for item in result["errors"])


def test_tampered_report_detected(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    (workspace.root / "session.json").write_text(
        json.dumps({"last_result": {"response": response + "补充"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert "报告正文与溯源记录不一致。" in result["errors"]


def test_export_payload_is_self_contained(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)

    payload = provenance.export_payload(workspace.root)
    assert payload["summary"]["root_hash"]
    assert len(payload["entries"]) == payload["summary"]["entries"]
    assert payload["verification"]["ok"] is True
    assert payload["entries"][0]["type"] == "meta"
    assert payload["entries"][-1]["type"] == "report"


def test_missing_summary_and_bad_lines_are_reported(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    (workspace.root / provenance.SUMMARY_FILE).unlink()

    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("缺失" in item for item in result["errors"])
    assert provenance.load_summary(workspace.root) is None

    path = workspace.root / provenance.PROVENANCE_FILE
    path.write_text("not-json\n[1,2]\n\n", encoding="utf-8")
    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("不是合法 JSON" in item for item in result["errors"])
    assert any("不是 JSON 对象" in item for item in result["errors"])


def test_external_dataset_and_artifact(tmp_path):
    workspace, response = _session_root(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    dataset = external / "ext.csv"
    dataset.write_bytes(b"a,b\n1,2\n")
    artifact = external / "ext.html"
    artifact.write_text("<html>ext</html>", encoding="utf-8")

    provenance.record_analysis(
        workspace.root,
        task="外部数据",
        response=response,
        artifacts=[
            {"name": "ext.html", "kind": "chart", "path": str(artifact), "description": ""},
            {"name": "virtual", "kind": "chart", "path": str(external / "missing.html")},
        ],
        dataset_path=dataset,
    )
    result = provenance.verify(workspace.root)
    assert result["ok"] is True

    dataset.unlink()
    artifact.unlink()
    result = provenance.verify(workspace.root)
    assert result["ok"] is False
    assert any("数据集文件缺失" in item for item in result["errors"])
    assert any("产物文件缺失" in item for item in result["errors"])


def test_record_without_dataset_or_trace(tmp_path):
    workspace, response = _session_root(tmp_path)
    summary = provenance.record_analysis(workspace.root, task="纯文本", response=response)
    assert summary["entries"] == 2
    assert provenance.verify(workspace.root)["ok"] is True


def test_export_without_provenance(tmp_path):
    workspace, _ = _session_root(tmp_path)
    payload = provenance.export_payload(workspace.root)
    assert payload["summary"] is None
    assert payload["entries"] == []
    assert payload["verification"]["ok"] is False


def test_non_dict_trace_and_artifact_items_are_skipped(tmp_path):
    workspace, response = _session_root(tmp_path)
    provenance.record_analysis(
        workspace.root,
        task="异常输入",
        response=response,
        trace=[None, {"type": "tool_call", "name": "run_sql", "detail": "SELECT 1"}],
        artifacts=[None, {"name": "virtual", "kind": "chart"}],
    )
    result = provenance.verify(workspace.root)
    assert result["ok"] is True
    assert result["entries"] == 4  # meta + tool + artifact + report


def test_summary_mismatch_and_bad_session_json(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)

    summary_path = workspace.root / provenance.SUMMARY_FILE
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["entries"] += 1
    summary["root_hash"] = "0" * 64
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    result = provenance.verify(workspace.root)
    assert any("条目数" in item for item in result["errors"])
    assert any("根哈希" in item for item in result["errors"])

    summary_path.write_text("not-json", encoding="utf-8")
    assert provenance.load_summary(workspace.root) is None

    (workspace.root / "session.json").write_text("not-json", encoding="utf-8")
    result = provenance.verify(workspace.root)
    assert result["checks"]["report_compared"] is True
    assert any("session.json 无法解析" in item for item in result["errors"])

    (workspace.root / "session.json").write_text('{"last_result": []}', encoding="utf-8")
    result = provenance.verify(workspace.root)
    assert any("缺少可比的报告正文" in item for item in result["errors"])


def test_prev_mismatch_is_reported(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    path = workspace.root / provenance.PROVENANCE_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["prev"] = "wrong"
    lines[1] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = provenance.verify(workspace.root)
    assert any("prev" in item for item in result["errors"])


def test_dataset_entry_without_path_falls_back_to_input_dir(tmp_path):
    workspace, response = _session_root(tmp_path)
    _record(workspace, response)
    path = workspace.root / provenance.PROVENANCE_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["data"].pop("path")
    lines[1] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = provenance.verify(workspace.root)
    assert not any("数据集文件缺失" in item for item in result["errors"])


def test_record_provenance_failure_does_not_raise(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from data_agent.routers.analysis import _record_provenance

    workspace, response = _session_root(tmp_path)
    record = SimpleNamespace(
        workspace=workspace,
        session_id="prov",
        last_result=None,
    )
    result = SimpleNamespace(response=response, trace=[], artifacts=[])
    monkeypatch.setattr(
        provenance, "record_analysis", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk"))
    )
    _record_provenance(record, result, SimpleNamespace(provider="p", model="m"), task="t")


def test_chart_images_filters_non_images(tmp_path):
    from data_agent.routers.analysis import _chart_images

    (tmp_path / "chart.png").write_bytes(b"png")
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    class Workspace:
        artifacts_dir = tmp_path
        artifacts = [{"name": "chart.png"}, {"name": "data.csv"}]

    assert _chart_images(Workspace()) == [tmp_path / "chart.png"]


def _isolate_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings(
        api_key="not-used",
        provider="deepseek",
        runs_dir=runs_dir,
        max_active_sessions=100,
        session_ttl_hours=24,
        rate_limit_per_minute=1000,
        max_concurrent_analyses=2,
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(
        api,
        "registry",
        api.SessionRegistry(
            runs_dir,
            settings.max_active_sessions,
            settings.session_ttl_hours,
            storage=LocalSessionStorage(),
        ),
    )
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    monkeypatch.setattr(api, "analysis_slots", threading.BoundedSemaphore(2))
    api.request_buckets.clear()


def test_provenance_endpoints(tmp_path, monkeypatch):
    _isolate_runtime(tmp_path, monkeypatch)
    client = TestClient(api.app)
    response = client.post(
        "/api/sessions",
        files={"file": ("sales.csv", b"region,sales\nEast,100\nWest,200\n", "text/csv")},
    )
    assert response.status_code == 201
    session_id = response.json()["id"]

    assert client.get(f"/api/sessions/{session_id}/provenance").status_code == 404
    assert client.get(f"/api/sessions/{session_id}/provenance/export").status_code == 404

    record = api.registry.get(session_id)
    text = "# 报告\n\n东部销售额 100。"
    record.last_result = AnalysisResult(
        response=text,
        trace=[{"type": "tool_call", "name": "run_sql", "detail": "SELECT 1"}],
        artifacts=[],
        dataset_profile=record.workspace.profile(sample_rows=2),
        plan=[],
        completed_steps=[],
    )
    record.set_finished("completed")
    api.registry.persist(session_id, record)
    provenance.record_analysis(
        record.workspace.root,
        task="分析销售",
        response=text,
        trace=record.last_result.trace,
        dataset_path=record.workspace.source_path,
        provider="deepseek",
        model="deepseek-flash",
        code_version="1.0.0",
    )

    payload = client.get(f"/api/sessions/{session_id}/provenance")
    assert payload.status_code == 200
    body = payload.json()
    assert body["summary"]["entries"] == 4  # meta + dataset + tool + report
    assert body["verification"]["ok"] is True

    exported = client.get(f"/api/sessions/{session_id}/provenance/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    document = exported.json()
    assert document["verification"]["ok"] is True
    assert document["summary"]["report_sha256"] == body["summary"]["report_sha256"]
