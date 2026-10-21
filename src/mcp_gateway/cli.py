"""Command line: ``gateway serve | stdio | session-create | reproduce | report``."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
import uvicorn

from mcp_gateway.admin_models import CreateSessionRequest
from mcp_gateway.app import build_gateway, build_http_app
from mcp_gateway.bench.report import run_report
from mcp_gateway.demo import audit_line, run_demo
from mcp_gateway.fakes.services import FakePlatform
from mcp_gateway.fakes.world import world_from_settings
from mcp_gateway.logging_setup import configure_logging
from mcp_gateway.mcp_server import build_mcp_server, fixed_credentials
from mcp_gateway.settings import Mode, load_settings
from mcp_gateway.timeutil import ManualClock, parse_iso

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def serve() -> None:
    """Serve the admin API and MCP over streamable HTTP on server.host:server.port."""
    settings = load_settings()
    http = build_http_app(build_gateway(settings))
    configure_logging(settings.server.log_level)
    uvicorn.run(http.app, host=settings.server.host, port=settings.server.port, log_config=None)


@app.command()
def stdio(
    offline: Annotated[bool, typer.Option(help="Use the in-process fakes instead of real services.")] = False,
) -> None:
    """Serve MCP over stdio for the session whose key is in GATEWAY_AGENT_API_KEY."""
    settings = load_settings()
    if settings.agent_api_key is None:
        raise typer.BadParameter("set GATEWAY_AGENT_API_KEY to the session's API key")
    if offline:
        clock = ManualClock(parse_iso(settings.demo.wall_clock))
        platform = FakePlatform(world=world_from_settings(settings.fakes), cfg=settings.fakes, clock=clock)
        gateway = build_gateway(settings, clock=clock, transports=platform.transports())
    else:
        gateway = build_gateway(settings)
    server = build_mcp_server(gateway, fixed_credentials(settings.agent_api_key.get_secret_value()))
    configure_logging(settings.server.log_level)
    asyncio.run(server.run_stdio_async())


@app.command("session-create")
def session_create(
    agent_id: Annotated[str, typer.Option()],
    mode: Annotated[Mode, typer.Option()],
    known_at: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Create a session directly in the store (for stdio use) and print it with its one-time API key."""
    gateway = build_gateway(load_settings())
    response = gateway.create_session(CreateSessionRequest(agent_id=agent_id, mode=mode, known_at=known_at))
    typer.echo(response.model_dump_json(indent=2))


@app.command()
def reproduce() -> None:
    """Offline end-to-end demo against in-process fakes; prints the steps and the audit log."""
    settings = load_settings()
    configure_logging("WARNING")
    result = asyncio.run(run_demo(settings))
    typer.echo("Scripted agent session")
    for step in result.steps:
        typer.echo(f"  {step.tool:<13} {step.outcome:<30} {step.name}: {step.detail}")
    typer.echo(f"\nAudit log ({len(result.audit)} entries, hash chain intact: {result.chain_intact})")
    for entry in result.audit:
        typer.echo("  " + audit_line(entry))
    typer.echo(
        f"\nTrials received by fake S6: {result.ledger_trials}; orders received by fake I6: {result.exchange_orders}"
    )
    typer.echo(f"Summary written to {result.summary_path}")


@app.command()
def report() -> None:
    """Run the report experiments and write metric tables under the data root."""
    settings = load_settings()
    configure_logging("WARNING")
    tables = asyncio.run(run_report(settings))
    typer.echo(json.dumps(tables, indent=2))
