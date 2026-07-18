from __future__ import annotations

import pandas as pd
import pytest

from data_agent.workspace import DataWorkspace


@pytest.fixture()
def workspace(tmp_path):
    data = pd.DataFrame(
        {
            "region": ["East", "West", "East", "West", "East", "East"],
            "sales": [100.0, 200.0, 120.0, 230.0, None, 100.0],
            "profit": [10.0, 32.0, 14.0, 40.0, 12.0, 10.0],
            "category": [" A ", "B", "A", "B", "A", " A "],
        }
    )
    source = tmp_path / "dirty.csv"
    data.to_csv(source, index=False)
    result = DataWorkspace(tmp_path / "runs", session_id="test")
    result.load(source, copy_into_workspace=True)
    return result

