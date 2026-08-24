"""Leakage-safe per-gameweek feature frame for the match xP model.

Turns ``player_gameweeks.parquet`` (one row per ``(code, season, gw)``) into
the modelling table: every feature is computed from gameweeks *strictly
before* the row's own gameweek, labelled with that gameweek's points.

Contract: ``MATCH_MODEL_SPEC.md``. Key stances implemented here:

* **No leakage, enforced structurally** — trailing aggregates are built with
  ``groupby(...).shift(1)`` before any rolling window, so a row can never see
  its own outcome. The only current-gameweek columns used as features are the
  ones genuinely known before kickoff: venue, opponent, fixture count.
* **Fixture context is a first-class feature** — opponent strength is derived
  from the panel itself as expanding, shifted team form: goals scored and
  conceded per game, plus *points conceded to each position*, which is the
  direct "who is generous to midfielders" signal.
* **The startable pool is separated from the full panel** — most rows are
  players who will not feature, and predicting 0 for them is trivially easy.
  ``is_startable`` marks rows with recent minutes so evaluation can report the
  decision-relevant subset instead of flattering itself on the full panel.
"""

from __future__ import annotations

import pandas as pd

# Trailing windows (in gameweeks) used for player form.
SHORT_WINDOW = 3
LONG_WINDOW = 5
# A row counts as "startable" when the player averaged at least this many
# minutes over the trailing short window — the pool a manager actually chooses
# from. Below it, predicting a blank is trivial and inflates every metric.
STARTABLE_MIN_MINUTES = 30.0

# Player stats averaged over trailing windows (source column -> feature stem).
FORM_SOURCES = {
    "minutes": "mins",
    "total_points": "pts",
    "expected_goal_involvements": "xgi",
    "expected_goals": "xg",
    "expected_assists": "xa",
    "bps": "bps",
    "defensive_contribution": "dc",
    "saves": "saves",
    "ict_index": "ict",
    "started": "startrate",
}

KEY = ["code", "season"]


def _sorted_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """Panel sorted into the chronological order every shift/roll relies on."""
    return panel.sort_values(["code", "season", "gw"]).reset_index(drop=True)


def add_player_form(panel: pd.DataFrame) -> pd.DataFrame:
    """Trailing player form: shifted rolling means over prior gameweeks only.

    Emits ``<stem>_l3``/``<stem>_l5`` (rolling means) and ``<stem>_std``
    (expanding season-to-date mean) for each source column, plus
    ``games_std``, the count of prior gameweeks in which the player featured.
    """
    out = _sorted_panel(panel)
    grouped = out.groupby(KEY, sort=False)
    for source, stem in FORM_SOURCES.items():
        prior = grouped[source].shift(1)
        by_player = prior.groupby([out["code"], out["season"]], sort=False)
        out[f"{stem}_l{SHORT_WINDOW}"] = by_player.transform(
            lambda s: s.rolling(SHORT_WINDOW, min_periods=1).mean()
        )
        out[f"{stem}_l{LONG_WINDOW}"] = by_player.transform(
            lambda s: s.rolling(LONG_WINDOW, min_periods=1).mean()
        )
        out[f"{stem}_std"] = by_player.transform(lambda s: s.expanding().mean())
    for source, stem in (("total_points", "pts"), ("minutes", "mins")):
        out[f"{stem}_l1"] = grouped[source].shift(1)
    played_prior = grouped["played"].shift(1).astype("Float64").fillna(0)
    out["games_std"] = played_prior.groupby(
        [out["code"], out["season"]], sort=False
    ).transform(lambda s: s.expanding().sum())
    return out


def team_form(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Expanding, shifted team strength tables derived from the panel itself.

    Returns ``(overall, by_position)``:

    * ``overall`` — per ``(season, gw, team_id)``: ``team_scored_pg`` and
      ``team_conceded_pg``, the team's goals for/against per game across
      gameweeks strictly before ``gw``.
    * ``by_position`` — per ``(season, gw, team_id, element_type)``:
      ``opp_pts_allowed_pg``, the FPL points that team has conceded per game to
      players of that position. This is the fixture-targeting signal.
    """
    played = panel[panel["played"].astype("boolean").fillna(False)]
    # goals_conceded is a team stat replicated onto each player, so max over the
    # team's players who featured recovers the team value (0 when all blanked).
    overall = (
        played.groupby(["season", "gw", "team_id"], as_index=False)
        .agg(scored=("goals_scored", "sum"), conceded=("goals_conceded", "max"))
        .sort_values(["season", "team_id", "gw"])
    )
    grouped = overall.groupby(["season", "team_id"], sort=False)
    for src, name in (("scored", "team_scored_pg"), ("conceded", "team_conceded_pg")):
        overall[name] = grouped[src].transform(
            lambda s: s.shift(1).expanding().mean()
        )
    overall = overall[["season", "gw", "team_id", "team_scored_pg", "team_conceded_pg"]]

    allowed = (
        panel.groupby(
            ["season", "gw", "opponent_team", "element_type"], as_index=False
        )
        .agg(pts_allowed=("total_points", "sum"))
        .rename(columns={"opponent_team": "team_id"})
        .sort_values(["season", "team_id", "element_type", "gw"])
    )
    allowed["opp_pts_allowed_pg"] = allowed.groupby(
        ["season", "team_id", "element_type"], sort=False
    )["pts_allowed"].transform(lambda s: s.shift(1).expanding().mean())
    by_position = allowed[
        ["season", "gw", "team_id", "element_type", "opp_pts_allowed_pg"]
    ]
    return overall, by_position


def add_fixture_context(panel: pd.DataFrame) -> pd.DataFrame:
    """Attach opponent strength (and the player's own team's form) to each row."""
    overall, by_position = team_form(panel)
    out = panel.merge(
        overall.rename(
            columns={
                "team_id": "opponent_team",
                "team_scored_pg": "opp_scored_pg",
                "team_conceded_pg": "opp_conceded_pg",
            }
        ),
        on=["season", "gw", "opponent_team"],
        how="left",
    )
    out = out.merge(
        by_position.rename(columns={"team_id": "opponent_team"}),
        on=["season", "gw", "opponent_team", "element_type"],
        how="left",
    )
    out = out.merge(overall, on=["season", "gw", "team_id"], how="left")
    return out


def build_match_frame(panel: pd.DataFrame) -> pd.DataFrame:
    """Full modelling frame: trailing form + fixture context + label.

    The label is ``total_points`` for the row's own gameweek. ``is_startable``
    flags the decision-relevant pool (recent minutes), and ``has_history``
    flags rows with at least one prior gameweek — rows without it cannot be
    predicted from form and are excluded by the evaluation harness.
    """
    frame = add_player_form(panel)
    frame = add_fixture_context(frame)
    frame["is_startable"] = frame[f"mins_l{SHORT_WINDOW}"].fillna(0) >= STARTABLE_MIN_MINUTES
    frame["has_history"] = frame["games_std"].fillna(0) > 0
    frame["label_points"] = frame["total_points"]
    return _sorted_panel(frame)
