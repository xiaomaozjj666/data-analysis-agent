from __future__ import annotations

import shutil
from pathlib import Path

from data_agent.api import SessionRegistry
from data_agent.storage import LocalSessionStorage, S3SessionStorage, build_session_storage
from data_agent.workspace import DataWorkspace


class FakeS3Client:
    def __init__(self, root: Path) -> None:
        self.root = root

    def upload_file(self, source: str, bucket: str, key: str) -> None:
        destination = self.root / bucket / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / bucket / key, destination)

    def head_bucket(self, Bucket: str) -> None:
        (self.root / Bucket).mkdir(parents=True, exist_ok=True)


def fake_storage(root: Path) -> S3SessionStorage:
    storage = object.__new__(S3SessionStorage)
    storage.bucket = "test-bucket"
    storage.endpoint_url = "https://example.invalid"
    storage.prefix = "sessions"
    storage.client = FakeS3Client(root)
    return storage


def test_s3_storage_round_trips_session_archive(tmp_path):
    storage = fake_storage(tmp_path / "remote")
    source = tmp_path / "runs" / "api_test"
    (source / "input").mkdir(parents=True)
    (source / "artifacts").mkdir()
    (source / "input" / "sales.csv").write_text("region,sales\nEast,100\n", encoding="utf-8")
    (source / "session.json").write_text('{"chat": []}', encoding="utf-8")

    storage.sync_session("api_test", source)
    shutil.rmtree(source)

    assert storage.restore_session("api_test", source) is True
    assert (source / "input" / "sales.csv").read_text(encoding="utf-8").endswith("East,100\n")
    assert storage.healthcheck()["status"] == "ok"


def test_storage_defaults_to_local(monkeypatch):
    monkeypatch.delenv("DATA_AGENT_STORAGE_BACKEND", raising=False)
    storage = build_session_storage()
    assert isinstance(storage, LocalSessionStorage)
    assert storage.persistent is False


def test_registry_restores_session_from_remote_archive(tmp_path):
    storage = fake_storage(tmp_path / "remote")
    workspace = DataWorkspace(tmp_path / "runs", session_id="api_remote")
    source = workspace.save_upload("sales.csv", b"region,sales\nEast,100\n")
    workspace.load(source)
    SessionRegistry(tmp_path / "runs", 10, 24, storage=storage).create(workspace)
    workspace.cleanup()

    restored = SessionRegistry(tmp_path / "runs", 10, 24, storage=storage).get("api_remote")
    assert restored.workspace.dataframe.iloc[0].to_dict() == {"region": "East", "sales": 100}
