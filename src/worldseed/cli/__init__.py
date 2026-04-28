"""CLI entry point — worldseed [run|play|validate|runs]."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="WorldSeed — a persistent world for AI agents",
    )
    parser.add_argument("--port", type=int, default=8888, help="Port (default: 8888)")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    sub = parser.add_subparsers(dest="command")

    # play
    rp = sub.add_parser(
        "run",
        help="One-command MAIN launcher: server + workspace + agent activation.",
    )
    rp.add_argument("config", help="Scene config YAML")
    rp.add_argument("--workspace", default=None, help="Workspace path")
    rp.add_argument("--run-id", default=None, help="Workspace/run label")
    rp.add_argument("--host", default="127.0.0.1", help="Bind address")
    rp.add_argument("--port", type=int, default=8000, help="Port")
    rp.add_argument("--force", action="store_true", help="Overwrite generated workspace files")
    rp.add_argument(
        "--dm-model",
        default=os.environ.get("WORLDSEED_DM_MODEL", ""),
        help="DM model (default: $WORLDSEED_DM_MODEL)",
    )
    rp.add_argument("--dm-fallback", default=None, help="Fallback model")
    rp.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks")
    rp.add_argument(
        "--language",
        default=None,
        help="Content language (e.g. zh, en). Auto-detected if omitted.",
    )

    pp = sub.add_parser(
        "play",
        help="Start engine + server. Default: bare runtime for external MAIN agents.",
    )
    pp.add_argument("config", help="Scene config YAML")
    pp.add_argument(
        "--agent-runtime",
        choices=("none", "openclaw"),
        default="none",
        help="none (default): bare engine + server, agents driven externally via /act. "
        "openclaw: legacy auto-spawn OpenClaw gateway + WebSocket connector.",
    )
    pp.add_argument(
        "--dm-model",
        default=os.environ.get("WORLDSEED_DM_MODEL", ""),
        help="DM model (default: $WORLDSEED_DM_MODEL)",
    )
    pp.add_argument("--dm-fallback", default=None, help="Fallback model")
    pp.add_argument("--port", type=int, default=8000, help="Port")
    pp.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks")
    pp.add_argument(
        "--max-dm-calls",
        type=int,
        default=None,
        help="Stop DM after N calls",
    )
    pp.add_argument("--timeout", type=int, default=None, help="Stop after N minutes")
    pp.add_argument(
        "--language",
        default=None,
        help="Content language (e.g. zh, en). Auto-detected if omitted.",
    )
    # runs
    sub.add_parser("runs", help="List past runs")

    # validate
    vp = sub.add_parser("validate", help="Validate a scene config")
    vp.add_argument("config", help="Scene config YAML")
    vp.add_argument("--ticks", type=int, default=50, help="Physics ticks")
    vp.add_argument("--pedantic", action="store_true", help="Show hints")
    vp.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON output",
    )

    args = parser.parse_args()

    if args.command == "run":
        from worldseed.cli.run import run

        run(args)
    elif args.command == "play":
        from worldseed.cli.play import play

        play(args)
    elif args.command == "runs":
        from worldseed.cli.runs import runs

        runs()
    elif args.command == "validate":
        from worldseed.cli.validate import validate_cmd

        validate_cmd(args)
    else:
        # No subcommand → lobby mode (dashboard-first)
        _lobby(args)


def _lobby(args: argparse.Namespace) -> None:
    """Start server in lobby mode — no engine, configure via dashboard."""
    import uvicorn

    from worldseed.server.app import create_app

    port = args.port
    app = create_app(engine=None, port=port)

    print("\n  WorldSeed (lobby mode)")
    print(f"  Dashboard: http://127.0.0.1:{port}")
    print("  Configure and start a world from the dashboard.")
    print("  Press Ctrl+C to stop.\n")

    host = getattr(args, "host", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="warning")
