"""cli.py 错误路径：配置/数据问题应输出一行可读错误与退出码 2，而不是 traceback。"""

from __future__ import annotations

from typer.testing import CliRunner

from data_agent import cli as cli_module


def _csv(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    path = tmp_path / "sales.csv"
    path.write_text("region,sales\nEast,100\n", encoding="utf-8")
    return path


def test_missing_api_key_prints_friendly_error(tmp_path, monkeypatch):
    class KeylessAgent:
        def __init__(self, workspace, settings, **kwargs):
            raise ValueError("未配置 API Key。请复制 .env.example 为 .env 后设置 DEEPSEEK_API_KEY。")

    monkeypatch.setattr(cli_module, "DataAnalysisAgent", KeylessAgent)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 2
    assert "未配置 API Key" in result.output
    assert "Traceback" not in result.output


def test_debug_flag_prints_traceback(tmp_path, monkeypatch):
    """--debug 时保留完整堆栈，便于排查运行期错误；默认仍是一行文案。"""

    class BoomAgent:
        def __init__(self, workspace, settings, **kwargs):
            pass

        def run(self, task, history=None, resume_from=None):
            raise RuntimeError("upstream 502")

    monkeypatch.setattr(cli_module, "DataAnalysisAgent", BoomAgent)
    result = CliRunner().invoke(
        cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch)), "--debug"]
    )
    assert result.exit_code == 1
    assert "Traceback" in result.output


def test_unexpected_agent_error_exits_1_without_traceback(tmp_path, monkeypatch):
    """网络/上游模型等运行期异常也要一行可读，退出码与用法错误区分开。"""

    class BoomAgent:
        def __init__(self, workspace, settings, **kwargs):
            pass

        def run(self, task, history=None, resume_from=None):
            raise RuntimeError("upstream 502")

    monkeypatch.setattr(cli_module, "DataAnalysisAgent", BoomAgent)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 1
    assert "RuntimeError" in result.output
    assert "upstream 502" in result.output
    assert "Traceback" not in result.output


def test_unreadable_data_file_prints_friendly_error(tmp_path, monkeypatch):
    def broken_load(self, *args, **kwargs):
        raise ValueError("不支持的文件编码")

    monkeypatch.setattr(cli_module.DataWorkspace, "load", broken_load)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 2
    assert "无法读取数据文件 sales.csv" in result.output
    assert "Traceback" not in result.output


def test_keyboard_interrupt_exits_cleanly(tmp_path, monkeypatch):
    class InterruptedAgent:
        def __init__(self, workspace, settings, **kwargs):
            pass

        def run(self, task, history=None, resume_from=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "DataAnalysisAgent", InterruptedAgent)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 130
    assert "已取消分析" in result.output


def test_error_text_with_brackets_is_not_swallowed_by_markup(tmp_path, monkeypatch):
    """异常文本里的方括号（如列名 [region]）不能被 rich 当成样式标记吞掉。"""

    def bad_column(self, *args, **kwargs):
        raise ValueError("缺少必需列 [region]")

    monkeypatch.setattr(cli_module.DataWorkspace, "load", bad_column)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 2
    assert "[region]" in result.output


def test_keyboard_interrupt_while_loading(tmp_path, monkeypatch):
    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.DataWorkspace, "load", interrupted)
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 130
    assert "已取消分析" in result.output


def test_invalid_numeric_env_config_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "not-a-number")
    result = CliRunner().invoke(cli_module.app, ["analyze", str(_csv(tmp_path, monkeypatch))])
    assert result.exit_code == 2
    assert "配置有误" in result.output
