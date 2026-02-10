import argparse

from .config import SETTINGS
from .llm import LLMClient
from .mcp_client import MCPClient, ensure_go_server
from .reports import (
    generate_league_summary,
    generate_starting_xi_report,
    generate_trades_report,
    generate_waiver_report,
    save_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="FPL MCP report CLI")
    parser.add_argument("--type", required=True, choices=["waivers", "league_summary", "trades", "starting_xi"])
    parser.add_argument("--league-id", type=int, default=14204)
    parser.add_argument("--entry-id", type=int, default=286192)
    parser.add_argument("--gw", type=int, default=0)
    args = parser.parse_args()

    ensure_go_server()
    client = MCPClient(SETTINGS.mcp_url, SETTINGS.mcp_api_key)
    llm = LLMClient()

    if args.gw == 0:
        summary = client.call_tool("league_summary", {"league_id": args.league_id, "gw": 0})
        cur = int(summary.get("gameweek", 0)) if isinstance(summary, dict) else 0
        args.gw = cur + 1 if args.type == "waivers" else cur

    if args.type == "waivers":
        content = generate_waiver_report(client, llm, args.league_id, args.entry_id, args.gw)
        paths = save_report(args.gw, "waiver_recommendations", content)
    elif args.type == "league_summary":
        content = generate_league_summary(client, llm, args.league_id, args.gw)
        paths = save_report(args.gw, "league_summary", content)
    elif args.type == "trades":
        content = generate_trades_report(client, llm, args.league_id, args.gw)
        paths = save_report(args.gw, "trades_summary", content)
    else:
        content = generate_starting_xi_report(client, llm, args.league_id, args.entry_id, args.gw)
        paths = save_report(args.gw, "starting_xi", content)

    print(paths)


if __name__ == "__main__":
    main()
