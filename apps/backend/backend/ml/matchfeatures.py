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

# Team-level strength columns produced by ``team_form`` (goals per game).
TEAM_FORM_COLUMNS = ["team_scored_pg", "team_conceded_pg"]


def _sorted_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """Panel sorted into the chronological order every shift/roll relies on."""
    return panel.sort_values(["code", "season", "gw"]).reset_index(drop=True)


def add_player_form(panel: pd.DataFrame) -> pd.DataFrame:
    """Trailing player form: shifted rolling means over prior gameweeks only.

    Emits ``<stem>_l3``/``<stem>_l5`` (rolling means) and ``<stem>_std``
    (expanding season-to-date mean) for each source column, plus
    ``games_std``, the count of prior gameweeks in which the player featured,
    and ``pts_sd_std``, the expanding standard deviation of prior gameweek
    points (the dispersion behind a floor/ceiling band).
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
    # Dispersion of prior gameweek points — the floor/ceiling input. Expanding
    # (not rolling) because a ceiling is a season-long property: one haul in
    # GW2 still says something about a player in GW30.
    prior_points = grouped["total_points"].shift(1)
    out["pts_sd_std"] = prior_points.groupby(
        [out["code"], out["season"]], sort=False
    ).transform(lambda s: s.expanding().std())
    played_prior = grouped["played"].shift(1).astype("Float64").fillna(0)
    out["games_std"] = played_prior.groupby(
        [out["code"], out["season"]], sort=False
    ).transform(lambda s: s.expanding().sum())
    return out


def player_form_columns() -> list[str]:
    """Every column ``add_player_form`` adds, in emission order.

    These depend only on the player's own earlier gameweeks, so they are one
    value per (player, gameweek) — the set ``matchmodel.stub_frame`` computes
    once per event and attaches to each fixture of a double.
    """
    columns = []
    for stem in FORM_SOURCES.values():
        columns += [f"{stem}_l{SHORT_WINDOW}", f"{stem}_l{LONG_WINDOW}", f"{stem}_std"]
    return [*columns, "pts_l1", "mins_l1", "pts_sd_std", "games_std"]


def team_form(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Expanding, shifted team strength tables derived from the panel itself.

    Returns ``(overall, by_position)``:

    * ``overall`` — per ``(season, gw, team_id)``: ``team_scored_pg`` and
      ``team_conceded_pg``, the team's goals for/against per game across
      its played gameweeks strictly before ``gw``. Every gameweek in the
      panel gets a row, including blanks and the upcoming stub gameweek
      (see ``_carry_team_form``).
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
        # Post-gameweek value (this gameweek included) — what the *next*
        # gameweek inherits when the team has no played row there.
        overall[_post(name)] = grouped[src].transform(lambda s: s.expanding().mean())
    overall = overall[
        ["season", "gw", "team_id", *TEAM_FORM_COLUMNS, *map(_post, TEAM_FORM_COLUMNS)]
    ]
    overall = _carry_team_form(overall, panel)

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


def _post(column: str) -> str:
    """Name of the post-gameweek companion of a team-form column."""
    return f"{column}_post"


def _carry_team_form(overall: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """Give every team a strength row in every gameweek of the panel.

    Contract: for every ``(season, gw, team_id)`` the team-form columns are the
    mean over the team's *played* gameweeks strictly before ``gw`` — whether or
    not the team plays in ``gw`` itself.

    * Gameweeks the team played keep the shifted expanding mean computed in
      ``team_form`` untouched.
    * Gameweeks with no played row — a blank, or the upcoming gameweek scored
      from outcome-free stub rows by ``matchmodel.build_gw_xp`` — take the
      *post*-gameweek value of the team's last played gameweek, i.e. the
      expanding mean that includes it. Carrying that gameweek's own
      pre-gameweek value instead would leave serving one gameweek stale (and
      all-NaN at GW2), so the backtest and the live forecast would disagree
      about what "team form" means.

    Without this a gap would join as NaN and silently drop the whole fixture
    signal exactly when it is needed. Gameweeks before the team's first played
    one stay NaN. Expects ``overall`` to carry ``_post(column)`` companions
    for each of ``TEAM_FORM_COLUMNS``; they are consumed and dropped here.
    """
    keys = ["season", "team_id"]
    grid = (
        panel[["season", "gw"]].drop_duplicates()
        .merge(overall[keys].drop_duplicates(), on="season")
    )
    filled = (
        grid.merge(overall, on=["season", "gw", "team_id"], how="left", indicator=True)
        .sort_values(["season", "team_id", "gw"])
    )
    played = filled.pop("_merge").eq("both")
    by_team = filled.groupby(keys, sort=False)
    for column in TEAM_FORM_COLUMNS:
        carried = by_team[_post(column)].transform(lambda s: s.ffill().shift(1))
        filled[column] = filled[column].where(played, carried)
    filled = filled.drop(columns=[_post(c) for c in TEAM_FORM_COLUMNS])
    return filled.reset_index(drop=True)


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
