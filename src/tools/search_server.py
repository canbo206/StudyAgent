"""
MCP server that exposes one tool: web_search.

This runs as its own process, speaking the Model Context Protocol over stdio.
The orchestrator (src/orchestrator.py) launches this as a subprocess and calls
the `web_search` tool whenever Claude decides it needs external information —
the same pattern as the cupcake-store MCP server in the Foundry workshop,
just wired to a real search API instead of a toy inventory.

Run standalone for a quick sanity check:
    python src/tools/search_server.py
(it will just sit waiting for an MCP client to connect over stdio — that's expected)
"""

import os
import warnings

warnings.filterwarnings(
    "ignore",
    message=".*Field 'lifespan' has an incomplete definition.*",
)
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from tavily import TavilyClient

load_dotenv()

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

if not TAVILY_API_KEY:
    raise RuntimeError(
        "TAVILY_API_KEY is not set. Copy .env.example to .env and add your key "
        "(free tier at https://tavily.com). Refusing to start without it."
    )


mcp = FastMCP("autoagent-search")

_tavily_client = TavilyClient(api_key=TAVILY_API_KEY)


def _get_client() -> TavilyClient:
    return _tavily_client


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web for current information on a topic.

    Use this whenever you need facts, current data, or sources you don't
    already have — for example when researching a sub-question you've
    identified, verifying a claim, or gathering material for a report or
    study guide.

    Args:
        query: A specific, focused search query (not a whole sentence goal —
               break the goal into smaller queries first).
        max_results: How many results to return (default 5, max 10).

    Returns:
        A formatted string of search results: title, URL, and a short
        content snippet for each result.
    """
    max_results = max(1, min(max_results, 10))
    client = _get_client()

    response = client.search(
        query=query,
        max_results=max_results,
        search_depth="advanced",
    )

    results = response.get("results", [])
    if not results:
        return f"No results found for query: {query!r}"

    formatted = [f"Search results for: {query!r}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = r.get("content", "").strip()
        # Keep snippets short so tool results don't blow up context/cost
        if len(content) > 500:
            content = content[:500] + "..."
        formatted.append(f"{i}. {title}\n   URL: {url}\n   {content}\n")

    return "\n".join(formatted)


if __name__ == "__main__":
    mcp.run(transport="stdio")
