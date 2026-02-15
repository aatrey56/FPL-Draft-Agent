import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

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


def _current_gw(client: MCPClient) -> int:
    summary = client.call_tool("league_summary", {"league_id": 14204, "gw": 0})
    if isinstance(summary, dict):
        return int(summary.get("gameweek", 0) or 0)
    return 0


def run_tuesday_reports() -> None:
    client = MCPClient(SETTINGS.mcp_url, SETTINGS.mcp_api_key)
    llm = LLMClient()
    league_id = 14204
    entry_id = 286192
    gw = _current_gw(client) + 1
    save_report(gw, "league_summary", generate_league_summary(client, llm, league_id, gw))
    save_report(gw, "waiver_recommendations", generate_waiver_report(client, llm, league_id, entry_id, gw))
    save_report(gw, "trades_summary", generate_trades_report(client, llm, league_id, gw))


def run_friday_reports() -> None:
    client = MCPClient(SETTINGS.mcp_url, SETTINGS.mcp_api_key)
    llm = LLMClient()
    league_id = 14204
    entry_id = 286192
    gw = _current_gw(client) + 1
    save_report(gw, "waiver_recommendations", generate_waiver_report(client, llm, league_id, entry_id, gw))
    save_report(gw, "starting_xi", generate_starting_xi_report(client, llm, league_id, entry_id, gw))
    save_report(gw, "waiver_fa_summary", generate_trades_report(client, llm, league_id, gw))


def main() -> None:
    ensure_go_server()
    tz = ZoneInfo(SETTINGS.timezone)
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(run_tuesday_reports, CronTrigger(day_of_week="tue", hour=11, minute=0))
    scheduler.add_job(run_friday_reports, CronTrigger(day_of_week="fri", hour=23, minute=0))
    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
