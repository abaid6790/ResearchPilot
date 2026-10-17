"""Operator commands: ``flask llm-status``, ``flask llm-ping``, ``flask llm-reset``."""

from __future__ import annotations

import click

from providers import get_router


def register_cli(app) -> None:
    @app.cli.command("llm-status")
    def llm_status() -> None:
        """Show configured providers, models and key health (no network calls)."""
        router = get_router()
        active = router.active_providers()
        if not active:
            click.echo(
                "No LLM API keys configured. Set GEMINI_API_KEY_1, GROQ_API_KEY_1 or OPENROUTER_API_KEY_1."
            )
            return
        click.echo("Fallback order: " + " -> ".join(active))
        for provider in active:
            models = ", ".join(f"{t}={s.id}" for t, s in router.catalog.models_for(provider).items())
            click.echo(f"\n{provider}  ({models})")
            for row in (r for r in router.keys.snapshot() if r["provider"] == provider):
                extra = (
                    f" for {row['available_in_s']}s ({row['reason']})" if row["status"] != "available" else ""
                )
                click.echo(f"  {row['label']:<14} {row['status']}{extra}  group={row['group']}")

    @app.cli.command("llm-ping")
    @click.option("--provider", type=click.Choice(["gemini", "groq", "openrouter"]), default=None)
    @click.option("--key", "key_label", default=None, help="Only this key label, e.g. gemini#2")
    @click.option("--tier", type=click.Choice(["fast", "quality"]), default="fast")
    def llm_ping(provider: str | None, key_label: str | None, tier: str) -> None:
        """Make one tiny REAL call per key to verify keys and model names. Uses a little quota."""
        router = get_router()
        targets = [provider] if provider else router.active_providers()
        if not targets:
            click.echo("No LLM API keys configured.")
            raise SystemExit(1)
        failed = False
        for name in targets:
            for row in router.probe(name, key_label, tier):
                mark = "OK  " if row["ok"] else "FAIL"
                detail = "" if row["ok"] else f"  {row['kind']} (HTTP {row['status']}): {row['message']}"
                click.echo(
                    f"{mark} {row['label']:<14} model={row['model']}  {row.get('latency_s', 0)}s{detail}"
                )
                failed = failed or not row["ok"]
        raise SystemExit(1 if failed else 0)

    @app.cli.command("llm-reset")
    @click.option("--key", "key_label", default=None, help="Only this key label, e.g. gemini#2")
    def llm_reset(key_label: str | None) -> None:
        """Forget key cooldowns in this process (and the shared store, if Redis is used)."""
        count = get_router().keys.reset(key_label)
        click.echo(f"Reset {count} key(s).")
