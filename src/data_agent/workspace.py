from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from data_agent.serialization import to_jsonable

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet"}


@dataclass(frozen=True, slots=True)
class Artifact:
    name: str
    kind: str
    path: Path
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": str(self.path.resolve()),
            "description": self.description,
        }


class DataWorkspace:
    """Owns one analysis session, its active DataFrame and generated artifacts."""

    def __init__(self, root: str | Path, session_id: str | None = None) -> None:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id or uuid4().hex)
        self.root = Path(root).expanduser().resolve() / safe_id
        self.input_dir = self.root / "input"
        self.artifacts_dir = self.root / "artifacts"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._df: pd.DataFrame | None = None
        self.source_path: Path | None = None
        self._artifacts: list[Artifact] = []

    @property
    def dataframe(self) -> pd.DataFrame:
        if self._df is None:
            raise RuntimeError("尚未加载数据集。")
        return self._df

    @dataframe.setter
    def dataframe(self, value: pd.DataFrame) -> None:
        if not isinstance(value, pd.DataFrame):
            raise TypeError("工作区数据必须是 pandas DataFrame。")
        self._df = value

    @property
    def artifacts(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in self._artifacts]

    def register_artifact(self, path: str | Path, kind: str, description: str) -> Artifact:
        resolved = Path(path).resolve()
        if self.artifacts_dir.resolve() not in resolved.parents:
            raise ValueError("产物文件必须位于当前工作区 artifacts 目录内。")
        artifact = Artifact(resolved.name, kind, resolved, description)
        if all(existing.path != resolved for existing in self._artifacts):
            self._artifacts.append(artifact)
        return artifact

    def save_upload(self, filename: str, content: bytes) -> Path:
        safe_name = re.sub(r"[^\w.\-()\u4e00-\u9fff]", "_", Path(filename).name)
        if Path(safe_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型。支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        destination = (self.input_dir / safe_name).resolve()
        destination.write_bytes(content)
        return destination

    def load(self, source: str | Path, copy_into_workspace: bool = False) -> dict[str, Any]:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"数据文件不存在：{path}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持 {suffix} 文件。支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        if copy_into_workspace and self.input_dir.resolve() not in path.parents:
            target = self.input_dir / path.name
            shutil.copy2(path, target)
            path = target.resolve()

        if suffix in {".csv", ".tsv"}:
            df = self._read_delimited(path, suffix)
        elif suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(path)
        elif suffix == ".parquet":
            df = pd.read_parquet(path)
        elif suffix == ".jsonl":
            df = pd.read_json(path, lines=True)
        else:
            try:
                df = pd.read_json(path)
            except ValueError:
                df = pd.read_json(path, lines=True)

        if df.empty and len(df.columns) == 0:
            raise ValueError("数据文件为空或无法识别出列。")
        if len(df.columns) > 1000:
            raise ValueError("列数超过 1000，拒绝加载以防止意外资源耗尽。")
        self._df = df
        self.source_path = path
        return self.profile(sample_rows=5)

    @staticmethod
    def _read_delimited(path: Path, suffix: str) -> pd.DataFrame:
        sep = "\t" if suffix == ".tsv" else None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return pd.read_csv(path, sep=sep, engine="python", encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(f"无法识别文件编码：{last_error}")

    def profile(self, sample_rows: int = 5) -> dict[str, Any]:
        df = self.dataframe
        sample_rows = max(1, min(int(sample_rows), 20))
        missing = df.isna().sum()
        unique = df.nunique(dropna=True)
        column_info = [
            {
                "name": str(column),
                "dtype": str(df[column].dtype),
                "missing": int(missing[column]),
                "missing_pct": round(float(missing[column] / max(len(df), 1) * 100), 2),
                "unique": int(unique[column]),
            }
            for column in df.columns
        ]
        return to_jsonable(
            {
                "source": str(self.source_path) if self.source_path else None,
                "rows": len(df),
                "columns": len(df.columns),
                "duplicate_rows": int(df.duplicated().sum()),
                "memory_mb": round(float(df.memory_usage(deep=True).sum() / 1024**2), 3),
                "column_info": column_info,
                "sample": df.head(sample_rows),
            }
        )

    def save_dataframe(self, filename: str = "cleaned_data.csv") -> Path:
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", Path(filename).name)
        path = (self.artifacts_dir / safe_name).resolve()
        suffix = path.suffix.lower()
        if suffix == ".csv":
            self.dataframe.to_csv(path, index=False, encoding="utf-8-sig")
        elif suffix == ".xlsx":
            self.dataframe.to_excel(path, index=False)
        elif suffix == ".parquet":
            self.dataframe.to_parquet(path, index=False)
        else:
            raise ValueError("数据产物仅支持 .csv、.xlsx 或 .parquet。")
        self.register_artifact(path, "dataset", "清洗或变换后的数据集")
        return path

