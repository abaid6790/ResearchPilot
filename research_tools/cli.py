"""Operator commands: ``flask search-ping`` and ``flask fetch-check``."""

from __future__ import annotations

import click


def register_cli(app) -> None:
    @app.cli.command("search-ping")
    @click.argument("query", default="what is retrieval augmented generation")
    def search_ping(query: str) -> None:
        """Run ONE real Tavily search (about 1 credit) to verify TAVILY_API_KEY."""
        from research_tools import get_search_client
        from research_tools.search import SearchUnavailableError

        client = get_search_client()
        if not client.configured:
            click.echo("TAVILY_API_KEY is not set.")
            raise SystemExit(1)
        try:
            response = client.search(query, max_results=3)
        except SearchUnavailableError as exc:
            click.echo(f"FAIL {exc.kind}: {exc}")
            raise SystemExit(1) from exc
        click.echo(f"OK  {len(response.hits)} result(s) in {response.latency_s:.1f}s")
        for hit in response.hits:
            click.echo(f"  - {hit.title[:70]}  {hit.url}")

    @app.cli.command("fetch-check")
    @click.argument("url")
    def fetch_check(url: str) -> None:
        """Fetch one URL through the safe fetcher and show what came back (no database writes)."""
        import os

        from research_tools.extract import extract_html
        from research_tools.fetch import FetchSettings, SafeFetcher

        result = SafeFetcher(FetchSettings.from_env(os.environ)).fetch(url)
        if not result.ok:
            click.echo(f"FAIL ({result.reason}): {result.error}")
            raise SystemExit(1)
        click.echo(
            f"OK  HTTP {result.status}  {result.content_type}  {len(result.body)} bytes  final={result.final_url}"
        )
        if result.kind == "html":
            ex = extract_html(result.body, result.encoding)
            click.echo(f"title: {ex.title}\npublished: {ex.published_at}\ntext chars: {len(ex.text)}")
