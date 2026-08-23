from __future__ import annotations

import os

import pandas as pd
import pytest

from data_agent.workspace import DataWorkspace


def _skip_kaleido() -> bool:
    """是否跳过依赖 kaleido 真实无头渲染的测试。

    kaleido 的 PNG 渲染会拉起无头 Chrome，偶发挂起且无法被 pytest-timeout
    的信号中断（实测全量本地跑 16 分钟未结束）。以下任一条件成立即跳过：

    - 设置了 ``DATA_AGENT_SKIP_KALEIDO=1``（本地/CI 均可显式禁用）；
    - 运行在 CI 环境（``CI`` 已设置）——保留 503/500 降级分支测试覆盖异常路径，
      仅跳过需要真实渲染出 PNG 的那一个。

    未设置且非 CI 时（本地已安装 kaleido 并希望真实验证 PNG 通道），不跳过。
    """
    return bool(os.environ.get("DATA_AGENT_SKIP_KALEIDO")) or bool(os.environ.get("CI"))


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

