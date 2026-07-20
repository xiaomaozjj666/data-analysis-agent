from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from data_agent.serialization import to_jsonable

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet"}
PLOTLY_BUNDLE_NAME = "plotly.min.js"
WORKSPACE_STATE_NAME = "workspace_state.parquet"


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically: write to a sibling .tmp file then rename.

    A direct ``path.write_text`` truncates the destination before writing; if
    the process is killed mid-write (OOM, deploy restart, disk full) we leave
    a corrupt partial file that subsequent reads will fail on. The tmp + rename
    pattern guarantees readers either see the old file or the new file, never
    a half-written one. ``os.replace`` is atomic on POSIX and Windows for
    same-filesystem renames.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
        self._source_row_count = 0
        self.source_path: Path | None = None
        self._artifacts: list[Artifact] = []
        self.load_warnings: list[str] = []

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

    def count_artifacts(self, kind: str | None = None) -> int:
        """Count registered artifacts, optionally filtered by kind.

        Public accessor so tools and API code don't need to reach into the
        private ``_artifacts`` list. Filtering by kind lets callers answer
        "how many charts exist?" without materialising the full dict list.
        """
        if kind is None:
            return len(self._artifacts)
        return sum(1 for item in self._artifacts if item.kind == kind)

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

    def save_upload_stream(self, filename: str, stream: Any, max_bytes: int) -> Path:
        """Write an uploaded file incrementally so the request does not duplicate it in RAM."""
        safe_name = re.sub(r"[^\w.\-()\u4e00-\u9fff]", "_", Path(filename).name)
        if Path(safe_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型。支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        destination = (self.input_dir / safe_name).resolve()
        total = 0
        try:
            with destination.open("wb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"文件不能超过 {max_bytes // (1024 * 1024)}MB。")
                    target.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
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

        self.load_warnings = []
        try:
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
        except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"文件格式无法解析：{exc}") from exc

        if df.empty and len(df.columns) == 0:
            raise ValueError("数据文件为空或无法识别出列。")
        if len(df.columns) > 1000:
            raise ValueError("列数超过 1000，拒绝加载以防止意外资源耗尽。")
        self._df = df
        self._source_row_count = len(df)
        self.source_path = path
        return self.profile(sample_rows=5)

    def _read_delimited(self, path: Path, suffix: str) -> pd.DataFrame:
        sep = "\t" if suffix == ".tsv" else None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return pd.read_csv(path, sep=sep, engine="python", encoding=encoding, on_bad_lines="error")
            except pd.errors.ParserError as exc:
                last_error = exc
                try:
                    repaired = pd.read_csv(
                        path,
                        sep=sep,
                        engine="python",
                        encoding=encoding,
                        on_bad_lines="skip",
                    )
                except (pd.errors.ParserError, UnicodeDecodeError) as retry_exc:
                    last_error = retry_exc
                    continue
                self.load_warnings.append("文件包含格式异常行，已跳过无法解析的记录。")
                return repaired
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(f"无法识别文件编码：{last_error}")

    def repair_format(
        self,
        *,
        normalize_missing: bool = True,
        trim_strings: bool = True,
        parse_numeric: bool = True,
        parse_dates: bool = True,
        normalize_column_names: bool = False,
    ) -> dict[str, Any]:
        """Apply conservative, auditable repairs for unambiguous formatting issues."""
        df = self.dataframe.copy()
        before = {
            "rows": len(df),
            "columns": len(df.columns),
            "missing": int(df.isna().sum().sum()),
        }
        changes: list[str] = []
        warnings: list[str] = []

        if normalize_column_names:
            original = list(df.columns)
            normalized: list[str] = []
            seen: dict[str, int] = {}
            for value in original:
                base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(value).strip().lower()).strip("_") or "column"
                seen[base] = seen.get(base, 0) + 1
                normalized.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
            if normalized != original:
                df.columns = normalized
                changes.append("规范化列名并处理重复列名")

        if trim_strings:
            string_columns = list(df.select_dtypes(include=["object", "string"]).columns)
            trimmed = 0
            for column in string_columns:
                original_values = df[column].copy()
                df[column] = df[column].map(lambda value: value.strip() if isinstance(value, str) else value)
                if not df[column].equals(original_values):
                    trimmed += 1
            if trimmed:
                changes.append(f"清理 {trimmed} 个文本列首尾空格")

        if normalize_missing:
            missing_tokens = {"", "na", "n/a", "null", "none", "nan", "<na>"}
            normalized_missing = 0
            for column in df.select_dtypes(include=["object", "string"]).columns:
                values = df[column].astype("string")
                mask = values.str.strip().str.lower().isin(missing_tokens)
                normalized_missing += int(mask.sum())
                df.loc[mask, column] = pd.NA
            if normalized_missing:
                changes.append(f"将 {normalized_missing} 个明确缺失标记统一为空值")

        if parse_numeric:
            # Skip columns that carry semantic suffixes (units like kg, 元,
            # °C, km/h, ...) since converting them would silently drop the
            # unit and corrupt the data. Only currency prefixes and thousands
            # separators are considered unambiguous and get stripped.
            unit_residual_pattern = re.compile(
                r"^[\s¥￥$€]*[+-]?[\d.,]+(?:[eE][+-]?\d+)?"
            )
            for column in list(df.select_dtypes(include=["object", "string"]).columns):
                series = df[column]
                non_empty = series.dropna()
                if non_empty.empty:
                    continue
                text = non_empty.astype("string").str.strip()
                if text.str.contains("%", regex=False).any() or text.str.match(r"^0\d+$").any():
                    continue
                stripped = text.str.replace(",", "", regex=False).str.replace(
                    r"^[¥￥$€]\s*", "", regex=True
                )
                # After removing the leading number, anything non-empty that
                # remains is a unit suffix -> skip the column.
                residual = stripped.str.replace(unit_residual_pattern, "", regex=True).str.strip()
                if (residual.str.len() > 0).any():
                    continue
                candidate = pd.to_numeric(stripped, errors="coerce")
                if candidate.notna().all():
                    df[column] = pd.to_numeric(
                        df[column].astype("string").str.strip()
                        .str.replace(",", "", regex=False)
                        .str.replace(r"^[¥￥$€]\s*", "", regex=True),
                        errors="coerce",
                    )
                    changes.append(f"将格式明确的数值列 {column} 转为数值类型")

        if parse_dates:
            for column in list(df.select_dtypes(include=["object", "string"]).columns):
                if not re.search(r"date|time|日期|时间", str(column), flags=re.IGNORECASE):
                    continue
                non_empty = df[column].dropna()
                if non_empty.empty:
                    continue
                try:
                    parsed = pd.to_datetime(non_empty, errors="coerce", format="mixed")
                except (TypeError, ValueError):
                    parsed = pd.to_datetime(non_empty, errors="coerce")
                if parsed.notna().all():
                    df[column] = pd.to_datetime(df[column], errors="coerce", format="mixed")
                    changes.append(f"将日期列 {column} 转为日期时间类型")
                else:
                    warnings.append(f"日期列 {column} 存在无法确认的值，未自动转换。")

        changed = bool(changes)
        output: Path | None = None
        if changed:
            self.dataframe = df.reset_index(drop=True)
            output = self.save_dataframe("format_repaired.csv")
        after = {
            "rows": len(self.dataframe),
            "columns": len(self.dataframe.columns),
            "missing": int(self.dataframe.isna().sum().sum()),
        }
        return {
            "status": "ok",
            "changed": changed,
            "before": before,
            "after": after,
            "changes": changes,
            "warnings": warnings,
            "output": str(output) if output else None,
        }

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
                "load_warnings": list(self.load_warnings),
                "column_info": column_info,
                "sample": df.head(sample_rows),
            }
        )

    @property
    def source_row_count(self) -> int:
        """Number of rows loaded from the uploaded source before agent mutations."""
        return self._source_row_count

    def save_dataframe(
        self,
        filename: str = "cleaned_data.csv",
        dataframe: pd.DataFrame | None = None,
        description: str = "清洗或变换后的数据集",
    ) -> Path:
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", Path(filename).name)
        path = (self.artifacts_dir / safe_name).resolve()
        frame = self.dataframe if dataframe is None else dataframe
        suffix = path.suffix.lower()
        if suffix == ".csv":
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        elif suffix == ".xlsx":
            frame.to_excel(path, index=False)
        elif suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            raise ValueError("数据产物仅支持 .csv、.xlsx 或 .parquet。")
        self.register_artifact(path, "dataset", description)
        return path

    def ensure_plotly_bundle(self) -> Path | None:
        """Write the bundled Plotly.js once per workspace and reuse it for every chart.

        Returns the path to ``plotly.min.js`` inside this workspace's artifacts
        directory, or None when Plotly is not importable (extremely unlikely
        since it is a hard dependency). Each HTML artifact references this file
        relatively instead of inlining ~3 MB of JavaScript per chart.
        """
        try:
            from plotly.offline import get_plotlyjs
        except Exception:
            return None
        bundle = (self.artifacts_dir / PLOTLY_BUNDLE_NAME).resolve()
        if not bundle.exists():
            bundle.write_text(get_plotlyjs(), encoding="utf-8")
        return bundle

    def snapshot_state(self) -> tuple[pd.DataFrame, set[Path]]:
        """Capture the active data and artifact files before one agent step."""
        try:
            files = {
                path.resolve()
                for path in self.artifacts_dir.iterdir()
                if path.is_file()
            }
        except FileNotFoundError:
            # The artifacts dir may have been removed by a concurrent prune
            # between the run_lock check and this call; treat as empty.
            files = set()
        return self.dataframe.copy(deep=True), files

    def restore_state(self, snapshot: tuple[pd.DataFrame, set[Path]]) -> None:
        """Rollback data mutations and files created by a failed agent step."""
        dataframe, existing_files = snapshot
        self.dataframe = dataframe
        try:
            children = list(self.artifacts_dir.iterdir())
        except FileNotFoundError:
            children = []
        for path in children:
            resolved = path.resolve()
            if path.is_file() and resolved not in existing_files:
                path.unlink(missing_ok=True)
        self._artifacts = [item for item in self._artifacts if item.path.resolve() in existing_files]

    def save_checkpoint(self) -> Path:
        """Persist the active DataFrame separately from user-facing artifacts."""
        path = self.root / WORKSPACE_STATE_NAME
        self.dataframe.to_parquet(path, index=False)
        return path

    def restore_checkpoint(self) -> bool:
        """Restore the most recently persisted active DataFrame after a restart."""
        path = self.root / WORKSPACE_STATE_NAME
        if not path.is_file():
            return False
        self.dataframe = pd.read_parquet(path)
        return True

    def cleanup(self) -> None:
        """Remove this isolated workspace when a session expires or upload fails."""
        if self.root.exists():
            shutil.rmtree(self.root)

    def restore_artifacts(self, metadata: list[dict[str, str]] | None = None) -> None:
        """Re-register files already present when a persistent workspace is reopened."""
        if not self.artifacts_dir.is_dir():
            return
        metadata_by_name = {
            item.get("name", ""): item
            for item in (metadata or [])
            if isinstance(item, dict) and item.get("name")
        }
        for path in sorted(self.artifacts_dir.iterdir()):
            if not path.is_file() or path.name == PLOTLY_BUNDLE_NAME:
                continue
            suffix = path.suffix.lower()
            saved = metadata_by_name.get(path.name, {})
            kind = saved.get("kind") or (
                "visualization"
                if suffix == ".html"
                else "image"
                if suffix in {".png", ".jpg", ".jpeg"}
                else "chart_data"
                if path.name.endswith(".plotly.json")
                else "dataset"
            )
            description = saved.get("description") or path.stem
            self.register_artifact(path, kind, description)
