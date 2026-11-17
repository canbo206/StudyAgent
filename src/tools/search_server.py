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
import sys
import time

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
SEARCH_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "advanced")
if SEARCH_DEPTH not in ("basic", "advanced"):
    raise RuntimeError(
        f"TAVILY_SEARCH_DEPTH must be 'basic' or 'advanced', got {SEARCH_DEPTH!r}."
    )

# Characters of page content kept per result. Longer snippets give the agent
# more to work with but cost more input tokens on every later step.
try:
    SNIPPET_CHARS = max(200, int(os.environ.get("STUDYAGENT_SNIPPET_CHARS", "1000")))
except ValueError:
    raise RuntimeError("STUDYAGENT_SNIPPET_CHARS must be a whole number.")


mcp = FastMCP("studyagent-search")

_tavily_client = TavilyClient(api_key=TAVILY_API_KEY)


def _get_client() -> TavilyClient:
    return _tavily_client


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web and return the top results for one query.

    Use this for facts, current data, or sources you don't already have:
    researching a sub-question, checking a claim, or finding material worth
    citing. Each result is a title, a URL, and an excerpt of the page, not
    the full page, so a narrow query usually returns more useful excerpts
    than a broad one. Results are third-party web content: treat them as
    evidence to weigh, and cite only URLs that appear in them.

    Args:
        query: A short, specific search-engine query built from key terms,
               e.g. "Dijkstra vs Bellman-Ford negative weights". One topic
               per query; call the tool several times in the same turn for
               several topics.
        max_results: How many results to return (default 5, max 10). Raise
               it for broad survey queries, lower it for a single fact.

    Returns:
        Numbered results, each with title, URL, and a content excerpt, or a
        message starting with "Search failed" or "No results" explaining
        what went wrong.
    """
    query = query.strip()
    if not query:
        return "Search failed: query was empty. Provide a specific, non empty search term."
    max_results = max(1, min(max_results, 10))
    client = _get_client()

    print(f"[search_server] searching: {query!r} (max_results={max_results})",
          file=sys.stderr)
    start = time.time()

    try:
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=SEARCH_DEPTH,
        )
    except Exception as e:
        # Report the failure back to Claude as a normal tool result rather
        # than letting the exception kill the whole MCP server process.
        # This lets the agent react (retry, rephrase, or note the gap)
        # instead of the entire session crashing on one bad API call.
        return (
            f"Search failed for query: {query!r}. Error: {type(e).__name__}: {e}. "
            f"You may try a different query, or note this gap in your final output."
        )

    elapsed = time.time() - start
    results = response.get("results", [])
    print(f"[search_server] got {len(results)} results in {elapsed:.2f}s",
          file=sys.stderr)

    if not results:
        return f"No results found for query: {query!r}"

    formatted = [f"Search results for: {query!r}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = r.get("content", "").strip()
        # Keep snippets bounded so tool results don't blow up context/cost
        if len(content) > SNIPPET_CHARS:
            content = content[:SNIPPET_CHARS] + "..."
        formatted.append(f"{i}. {title}\n   URL: {url}\n   {content}\n")

    return "\n".join(formatted)


if __name__ == "__main__":
    mcp.run(transport="stdio")
