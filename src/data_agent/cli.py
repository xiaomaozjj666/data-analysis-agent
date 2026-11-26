"""命令行接口：通过 Typer 提供本地数据分析能力。

用法::

    data-agent analyze data.csv --task "分析销售趋势"
    data-agent analyze data.xlsx --provider openai --model gpt-4.1-mini
    data-agent clean --dry-run          # 预览可清理的旧运行目录

输出为 Markdown 格式的分析报告，产物路径打印在报告下方。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from data_agent import __version__
from data_agent.agent import DataAnalysisAgent
from data_agent.config import AgentSettings
from data_agent.retention import (
    ALL_PREFIXES,
    DEFAULT_PREFIXES,
    DEFAULT_TRASH_DAYS,
    format_bytes,
    prune_runs,
    purge_trash,
)
from data_agent.workspace import DataWorkspace

app = typer.Typer(
    help="LangChain + LangGraph + ReAct 数据分析 Agent",
    no_args_is_help=True,
)
# 报告正文走 stdout；进度、提示、错误走 stderr，保证 `data-agent analyze x.csv > report.md`
# 重定向出来的文件里只有报告。
console = Console()
err_console = Console(stderr=True)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[
        bool, typer.Option("--version", help="显示版本并退出", is_eager=True)
    ] = False,
) -> None:
    """LangChain + LangGraph + ReAct 数据分析 Agent。"""
    if version:
        console.print(f"data-agent {__version__}")
        raise typer.Exit()


EXIT_USAGE_ERROR = 2
EXIT_RUNTIME_ERROR = 1
EXIT_CANCELLED = 130


def _fail(
    message: str, *, hint: str | None = None, code: int = EXIT_USAGE_ERROR
) -> NoReturn:
    """打印一行可读的错误（不带 traceback）并以指定退出码结束。

    message / hint 常含异常文本或路径（如 ``[Errno 13] ...``），必须 escape，
    否则 rich 会把方括号当作样式标记吞掉，用户看到的错误信息就不完整。
    """
    err_console.print(f"[bold red]错误：[/bold red]{escape(message)}")
    if hint:
        err_console.print(f"[dim]{escape(hint)}[/dim]")
    raise typer.Exit(code=code)


def _cancelled() -> NoReturn:
    err_console.print("\n[yellow]已取消分析。[/yellow]")
    raise typer.Exit(code=EXIT_CANCELLED)


@app.command()
def analyze(
    file: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, readable=True, help="CSV/Excel/JSON/Parquet 数据文件"
        ),
    ],
    task: Annotated[
        str, typer.Option("--task", "-t", help="分析任务")
    ] = "完成数据清洗、统计分析和合适的可视化，并总结关键发现。",
    provider: Annotated[str | None, typer.Option(help="模型提供商：deepseek 或 openai")] = None,
    model: Annotated[str | None, typer.Option(help="覆盖默认模型名称")] = None,
    base_url: Annotated[str | None, typer.Option(help="覆盖模型 API 地址")] = None,
    debug: Annotated[bool, typer.Option("--debug", help="出错时打印完整堆栈")] = False,
) -> None:
    """分析一个本地数据文件并输出 Markdown 报告。"""
    try:
        settings = AgentSettings.from_env(provider=provider) if provider else AgentSettings.from_env()
    except ValueError as exc:  # 例如环境变量里的数值配置写错
        _fail(f"配置有误：{exc}")
    if model:
        settings.model = model
    if base_url:
        settings.base_url = base_url
    _auto_prune(settings.runs_dir)
    workspace = DataWorkspace(settings.runs_dir, session_id=f"cli_{uuid4().hex[:12]}")
    try:
        with err_console.status("[bold green]正在加载数据..."):
            profile = workspace.load(file, copy_into_workspace=True)
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        _fail(
            f"无法读取数据文件 {file.name}：{exc}",
            hint="请确认文件格式为 CSV/Excel/JSON/Parquet，且未被其他程序占用。",
        )
    except KeyboardInterrupt:
        _cancelled()
    err_console.print(f"已加载 {profile['rows']:,} 行 × {profile['columns']} 列")
    try:
        agent = DataAnalysisAgent(workspace, settings)
        with err_console.status("[bold green]Agent 正在分析（可能调用多个工具）..."):
            result = agent.run(task)
    except ValueError as exc:  # API Key 缺失、模型名为空等配置问题
        _fail(str(exc))
    except KeyboardInterrupt:
        _cancelled()
    except Exception as exc:  # noqa: BLE001 - CLI 边界：任何未预期异常都要变成一行可读错误
        if debug:
            err_console.print_exception()
        _fail(
            f"分析失败：{type(exc).__name__}: {exc}",
            hint="这是运行期错误（网络/上游模型/工具链），可加 --debug 查看完整堆栈后重试。",
            code=EXIT_RUNTIME_ERROR,
        )
    console.print(Markdown(result.response))
    if result.artifacts:
        err_console.print("\n[bold]产物：[/bold]")
        for artifact in result.artifacts:
            err_console.print(f"  • {artifact['kind']}: {artifact['path']}")


RETENTION_ENV = "DATA_AGENT_CLI_RETENTION_DAYS"
RETENTION_MAX_ENV = "DATA_AGENT_CLI_RETENTION_MAX_RUNS"


def _active_workspace_paths() -> set[Path]:
    """当前正在分析 / 取消中的会话目录；清理时必须原样保留。"""
    try:
        from data_agent.registry import registry  # noqa: PLC0415 - 延迟导入避免 CLI 启动开销
    except Exception:  # pragma: no cover - 导入失败时退化为不保护额外目录
        return set()
    try:
        return registry.busy_workspace_paths()
    except Exception:  # pragma: no cover - 注册表异常不能阻断清理
        return set()


def _auto_prune(runs_dir: Path) -> None:
    """按环境变量自动清理旧的 cli_/mcp_ 运行目录。

    支持两种阈值（可同时使用）：
    - ``DATA_AGENT_CLI_RETENTION_DAYS``：超过 N 天未修改；
    - ``DATA_AGENT_CLI_RETENTION_MAX_RUNS``：最多保留 N 个（按最近修改排序）。
    都未设置或 <= 0 时不清理。目录先移动到 ``runs/_trash/<时间戳>``，保留
    ``DEFAULT_TRASH_DAYS`` 天后才永久删除，误清理时还能手动移回。
    """
    days_raw = os.getenv(RETENTION_ENV, "").strip()
    max_raw = os.getenv(RETENTION_MAX_ENV, "").strip()
    days = 0.0
    max_runs: int | None = None
    if days_raw:
        try:
            days = float(days_raw)
        except ValueError:
            err_console.print(
                f"[yellow]忽略 {RETENTION_ENV}={escape(repr(days_raw))}：需要是天数（数字）。[/yellow]"
            )
    if max_raw:
        try:
            max_runs = int(max_raw)
        except ValueError:
            err_console.print(
                f"[yellow]忽略 {RETENTION_MAX_ENV}={escape(repr(max_raw))}：需要是整数。[/yellow]"
            )
    if days <= 0 and not max_runs:
        return
    busy = _active_workspace_paths()
    keep_latest = min(10, max_runs) if max_runs else 10
    result = prune_runs(
        runs_dir,
        max_age_days=days if days > 0 else 365000.0,
        keep_latest=keep_latest,
        max_runs=max_runs,
        use_trash=True,
        is_protected=busy.__contains__,
    )
    if result.removed:
        where = f"；可在 {escape(str(result.trash_dir))} 恢复" if result.trash_dir else ""
        err_console.print(
            f"[dim]已自动清理 {len(result.removed)} 个旧运行目录，"
            f"腾出 {format_bytes(result.freed_bytes)}{where}。[/dim]"
        )
    if result.skipped_active:
        err_console.print(
            f"[dim]跳过 {len(result.skipped_active)} 个正在使用的运行目录。[/dim]"
        )
    if result.failed:
        err_console.print(
            f"[yellow]{len(result.failed)} 个目录未能清理（可能被占用），"
            f"可稍后运行 data-agent clean 重试。[/yellow]"
        )
    purged = purge_trash(runs_dir, older_than_days=DEFAULT_TRASH_DAYS)
    if purged.removed:
        err_console.print(
            f"[dim]回收站中 {len(purged.removed)} 个超过 {DEFAULT_TRASH_DAYS:g} 天的批次已永久删除，"
            f"释放 {format_bytes(purged.freed_bytes)}。[/dim]"
        )


@app.command()
def clean(
    days: Annotated[float, typer.Option("--days", "-d", min=0, help="删除超过多少天未修改的运行目录")] = 7,
    keep: Annotated[int, typer.Option("--keep", "-k", min=0, help="无论多旧都保留最新的 N 个")] = 20,
    max_runs: Annotated[
        int | None,
        typer.Option("--max-runs", min=0, help="最多保留 N 个（按最近修改排序，0 表示同 --keep）"),
    ] = None,
    include_web: Annotated[
        bool, typer.Option("--include-web", help="同时清理 Web 会话（api_*，会从历史侧栏消失）")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出将删除的目录，不实际删除")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
    all_runs: Annotated[
        bool, typer.Option("--all", help="允许 --days 0（删除全部候选目录，危险）")
    ] = False,
    permanent: Annotated[
        bool,
        typer.Option("--permanent", help="直接永久删除，不进入回收站（不可恢复）"),
    ] = False,
    trash_days: Annotated[
        float,
        typer.Option("--trash-days", min=0, help="回收站保留天数，超期后永久删除"),
    ] = DEFAULT_TRASH_DAYS,
    runs_dir: Annotated[Path | None, typer.Option(help="覆盖 DATA_AGENT_RUNS_DIR")] = None,
) -> None:
    """清理 runs/ 下的旧运行目录（默认只清理 CLI / MCP 产生的 cli_*、mcp_*）。

    默认把目录移入 ``runs/_trash/<时间戳>/`` 并写恢复清单，保留 ``--trash-days``
    天；期间把会话目录移回 runs/ 即可恢复。``--permanent`` 才会立刻永久删除。
    正在分析 / 取消中的会话目录始终跳过。
    """
    explicit_dir = runs_dir is not None
    if runs_dir is None:
        try:
            runs_dir = AgentSettings.from_env().runs_dir
        except ValueError as exc:
            _fail(f"配置有误：{exc}")
    if not runs_dir.is_dir():
        if explicit_dir:
            _fail(
                f"运行目录不存在或不是目录：{runs_dir}",
                hint="检查 --runs-dir 是否写错；默认目录来自 DATA_AGENT_RUNS_DIR。",
            )
        console.print(f"运行目录尚未创建（{escape(str(runs_dir))}），无需清理。")
        return
    if days == 0 and not all_runs and not dry_run:
        _fail(
            "--days 0 会删除全部候选目录（保留数由 --keep 决定）。",
            hint="确认要这样做时再加 --all；只想预览可先 --dry-run。",
        )
    prefixes = ALL_PREFIXES if include_web else DEFAULT_PREFIXES
    busy = _active_workspace_paths()
    preview = prune_runs(
        runs_dir,
        max_age_days=days,
        keep_latest=keep,
        max_runs=max_runs,
        prefixes=prefixes,
        dry_run=True,
        is_protected=busy.__contains__,
    )
    if not preview.removed:
        console.print(f"没有需要清理的目录（{escape(str(runs_dir))}，保留 {preview.kept} 个）。")
        if preview.skipped_active:
            console.print(f"[dim]另有 {len(preview.skipped_active)} 个正在使用的目录已跳过。[/dim]")
        return
    action = "永久删除" if permanent else "移入回收站"
    rule = f"超过 {days:g} 天未修改" if days > 0 else "按保留数量"
    if max_runs is not None:
        rule += f"，最多保留 {max_runs} 个"
    console.print(
        f"将{action} {len(preview.removed)} 个目录，共 {format_bytes(preview.freed_bytes)}"
        f"（{rule}；另外保留最新 {keep} 个）："
    )
    for item in preview.removed[:15]:
        console.print(f"  • {escape(item.path.name)}  {format_bytes(item.size_bytes)}")
    if len(preview.removed) > 15:
        console.print(f"  … 另有 {len(preview.removed) - 15} 个")
    if preview.skipped_active:
        console.print(f"[dim]正在使用的 {len(preview.skipped_active)} 个目录不会被清理。[/dim]")
    if include_web:
        console.print("[yellow]提示：包含 Web 会话，建议先停止后端服务再清理。[/yellow]")
    if dry_run:
        if not permanent:
            console.print(
                f"[dim]回收站保留 {trash_days:g} 天，期间可把会话目录移回 runs/ 恢复。[/dim]"
            )
        return
    if not yes and not typer.confirm("确认删除？", default=False):
        raise typer.Exit(code=1)
    result = prune_runs(
        runs_dir,
        max_age_days=days,
        keep_latest=keep,
        max_runs=max_runs,
        prefixes=prefixes,
        use_trash=not permanent,
        is_protected=busy.__contains__,
        trash_days=trash_days,
    )
    if permanent:
        console.print(
            f"[green]已永久删除 {len(result.removed)} 个目录，释放 {format_bytes(result.freed_bytes)}。[/green]"
        )
    else:
        console.print(
            f"[green]已移动 {len(result.removed)} 个目录到回收站，腾出 {format_bytes(result.freed_bytes)}。[/green]"
        )
        if result.trash_dir:
            console.print(
                f"[dim]恢复方式：把 {escape(str(result.trash_dir))} 下的目录移回 "
                f"{escape(str(runs_dir))}；{trash_days:g} 天后自动永久删除。[/dim]"
            )
        purged = purge_trash(runs_dir, older_than_days=trash_days)
        if purged.removed:
            console.print(
                f"[dim]同时永久删除 {len(purged.removed)} 个超期回收站批次，"
                f"释放 {format_bytes(purged.freed_bytes)}。[/dim]"
            )
    if result.failed:
        _fail(
            f"{len(result.failed)} 个目录清理失败（可能被占用）：{result.failed[0].name} 等",
            hint="关闭占用这些文件的程序后重试。",
            code=EXIT_RUNTIME_ERROR,
        )


if __name__ == "__main__":
    app()
