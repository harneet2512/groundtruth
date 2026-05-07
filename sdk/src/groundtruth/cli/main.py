"""CLI entry point (``[cli]`` extra)."""

from __future__ import annotations

# pyright: reportUnknownArgumentType=false

import sys
from pathlib import Path
from typing import Optional, cast

from groundtruth.formatters import FormatName

try:  # pragma: no branch
    import typer as typer_pkg
except ImportError:  # pragma: no cover
    typer_pkg = None  # type: ignore[assignment]

cli_app = None
if typer_pkg is not None:  # pragma: no branch
    cli_app = typer_pkg.Typer(no_args_is_help=True, add_completion=False)

    @cli_app.callback()
    def _root() -> None:  # pyright: ignore[reportUnusedFunction]
        """Deterministic GroundTruth SDK utilities."""

    @cli_app.command("briefing")
    def cmd_briefing(
        symbol: str = typer_pkg.Argument(..., help="Symbol/name to resolve."),
        db: Path = typer_pkg.Option(..., "--db", help="Path to graph.db"),
        output: str = typer_pkg.Option("markdown", "--format", help="markdown|xml|plain|json"),
        family: str = typer_pkg.Option("TARGET", "--family"),
        max_results: int = typer_pkg.Option(10, "--max-results"),
    ) -> None:
        from groundtruth import GroundTruth
        from groundtruth.formatters import format_briefing, format_json

        assert typer_pkg is not None
        gt_local = GroundTruth(str(db))
        brief = gt_local.briefing(symbol, family=family, max_results=max_results)
        if output == "json":
            typer_pkg.echo(format_json(brief))
            return
        normalized = output.lower()
        if normalized not in {"markdown", "xml", "plain"}:
            raise typer_pkg.BadParameter("format must be markdown|xml|plain|json")
        typer_pkg.echo(format_briefing(brief, cast(FormatName, normalized)))

    @cli_app.command("check")
    def cmd_check(
        path: str = typer_pkg.Argument(..., help="nodes.file_path"),
        db: Path = typer_pkg.Option(..., "--db"),
        diff: Optional[str] = typer_pkg.Option(None, "--diff"),
    ) -> None:
        from groundtruth import GroundTruth

        assert typer_pkg is not None
        gt_local = GroundTruth(str(db))
        typer_pkg.echo(gt_local.check(path, diff=diff).summary)

    @cli_app.command("context")
    def cmd_context(
        symbol: str = typer_pkg.Argument(...),
        db: Path = typer_pkg.Option(..., "--db"),
        direction: str = typer_pkg.Option("callers", "--direction"),
        scope: Optional[str] = typer_pkg.Option(None, "--scope"),
        depth: int = typer_pkg.Option(2, "--depth"),
    ) -> None:
        from groundtruth import GroundTruth

        assert typer_pkg is not None
        gt_local = GroundTruth(str(db))
        ctx = gt_local.context(symbol, direction=direction, scope=scope, depth=depth)
        typer_pkg.echo(ctx.evidence)


def main() -> None:
    """Console script entry (`gt`)."""
    if typer_pkg is None or cli_app is None:
        sys.stderr.write("Install `[cli]` extra: pip install groundtruth[cli]\n")
        raise SystemExit(1)
    cli_app()
