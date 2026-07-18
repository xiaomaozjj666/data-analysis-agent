from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.markdown import Markdown

from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.workspace import DataWorkspace

app = typer.Typer(help="LangChain + LangGraph + ReAct 数据分析 Agent")
console = Console()


@app.callback()
def main() -> None:
    """LangChain + LangGraph + ReAct 数据分析 Agent。"""


@app.command()
def analyze(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="CSV/Excel/JSON/Parquet 数据文件")],
    task: Annotated[str, typer.Option("--task", "-t", help="分析任务")] = "完成数据清洗、统计分析和合适的可视化，并总结关键发现。",
    provider: Annotated[str | None, typer.Option(help="模型提供商：deepseek 或 openai")] = None,
    model: Annotated[str | None, typer.Option(help="覆盖默认模型名称")] = None,
    base_url: Annotated[str | None, typer.Option(help="覆盖模型 API 地址")] = None,
) -> None:
    """Analyze one local dataset and print the result."""
    settings = AgentSettings.from_env()
    if provider:
        settings = AgentSettings.from_env(provider=provider)
    if model:
        settings.model = model
    if base_url:
        settings.base_url = base_url
    workspace = DataWorkspace(settings.runs_dir, session_id=f"cli_{uuid4().hex[:12]}")
    with console.status("[bold green]正在加载数据..."):
        profile = workspace.load(file, copy_into_workspace=True)
    console.print(f"已加载 {profile['rows']:,} 行 × {profile['columns']} 列")
    agent = DataAnalysisAgent(workspace, settings)
    with console.status("[bold green]Agent 正在分析（可能调用多个工具）..."):
        result = agent.run(task)
    console.print(Markdown(result.response))
    if result.artifacts:
        console.print("\n[bold]产物：[/bold]")
        for artifact in result.artifacts:
            console.print(f"  • {artifact['kind']}: {artifact['path']}")


if __name__ == "__main__":
    app()
