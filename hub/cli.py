"""Hub CLI entry points."""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from .config import load_config, save_api_key
from .main import HubDaemon
from .relay_client import RelayClient


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Hybro Hub — bridge local A2A agents to hybro.ai."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ──── hybro-hub start ────

@main.command()
@click.option("--api-key", default=None, help="Hybro API key (also saves to config).")
@click.pass_context
def start(ctx: click.Context, api_key: str | None) -> None:
    """Start the hub daemon (foreground)."""
    if api_key:
        save_api_key(api_key)

    config = load_config(api_key=api_key)
    if not config.api_key:
        click.echo(
            "Error: No API key configured.\n"
            "Run: hybro-hub start --api-key hba_...\n"
            "Or set HYBRO_API_KEY environment variable.",
            err=True,
        )
        sys.exit(1)

    daemon = HubDaemon(config)
    asyncio.run(daemon.run())


# ──── hybro-hub status ────

@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show hub status from the relay service."""
    config = load_config()
    if not config.api_key:
        click.echo("Error: No API key configured.", err=True)
        sys.exit(1)

    async def _status() -> None:
        relay = RelayClient(
            gateway_url=config.gateway_url,
            hub_id=config.hub_id,
            api_key=config.api_key,
        )
        try:
            data = await relay.get_status()
            hubs = data.get("hubs", [])
            if not hubs:
                click.echo("No hubs registered.")
                return
            for h in hubs:
                online = "Online" if h.get("is_online") else "Offline"
                agents = h.get("agent_count", 0)
                click.echo(f"  Hub {h['hub_id'][:12]}... — {online} — {agents} agent(s)")
        finally:
            await relay.close()

    asyncio.run(_status())


# ──── hybro-hub agents ────

@main.command()
@click.pass_context
def agents(ctx: click.Context) -> None:
    """List discovered local agents."""
    from .agent_registry import AgentRegistry

    config = load_config()

    async def _agents() -> None:
        registry = AgentRegistry(config)
        found = await registry.discover()
        await registry.close()
        if not found:
            click.echo("No local agents found.")
            return
        for a in found:
            health = "healthy" if a.healthy else "unhealthy"
            click.echo(f"  {a.name} — {a.url} — {health} (id={a.local_agent_id})")

    asyncio.run(_agents())


# ──── hybro-hub agent start ollama ────

@main.group()
def agent() -> None:
    """Manage local agent adapters."""


@agent.command("start")
@click.argument("adapter_type")
@click.option("--model", default="llama3.2:8b", help="Ollama model name.")
@click.option("--port", default=10010, type=int, help="Port for the A2A server.")
@click.option("--system-prompt", default=None, help="System prompt for the model.")
def agent_start(adapter_type: str, model: str, port: int, system_prompt: str | None) -> None:
    """Start a local A2A agent adapter (e.g. 'ollama')."""
    if adapter_type == "ollama":
        try:
            from a2a_adapter import OllamaAdapter, serve_agent
        except ImportError:
            click.echo(
                "Error: a2a-adapter package not installed.\n"
                "Install with: pip install a2a-adapter",
                err=True,
            )
            sys.exit(1)

        click.echo(f"Starting Ollama A2A adapter (model={model}, port={port})...")
        adapter = OllamaAdapter(
            model=model,
            name=f"Ollama ({model})",
            description=f"Local LLM via Ollama ({model})",
            system_prompt=system_prompt,
        )
        serve_agent(adapter, port=port)
    else:
        click.echo(f"Unknown adapter type: {adapter_type}", err=True)
        click.echo("Supported adapters: ollama", err=True)
        sys.exit(1)
