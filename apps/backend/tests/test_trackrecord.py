"""Tests for the live track record. Synthetic panels in tmp_path, no network."""

import json
import logging
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.ml import trackrecord as tr
from backend.ml.matchfeatures import build_match_frame

ARCHIVE, LIVE_SEASON = "2025-26", "2026-27"
N_TEAMS, PER_POSITION = 10, 30
# The synthetic seasons are a tenth of a real one; scale the fit threshold too.
SMALL_FIT = 60


def _season(season: str, gws: int, seed: int, team_offset: int = 0) -> pd.DataFrame:
    """A season where points depend on starting, quality and the opponent."""
    rng = np.random.default_rng(seed)
    leakiness = rng.uniform(0.4, 1.6, size=N_TEAMS + 1)
    players = [
        {"code": 1000 + 100 * position + index, "element_type": position,
         "team": 1 + index % N_TEAMS, "quality": rng.uniform(0.2, 1.0),
         "propensity": rng.uniform(0.1, 0.98)}
        for position in (1, 2, 3, 4) for index in range(PER_POSITION)
    ]
    rows = []
    for gw in range(1, gws + 1):
        for player in players:
            team = player["team"]
            opponent = 1 + (team + gw) % N_TEAMS
            if opponent == team:
                opponent = 1 + team % N_TEAMS
            started = bool(rng.random() < player["propensity"])
            cameo = not started and bool(rng.random() < 0.2)
            points = 0.0
            if started:
                points = max(0.0, 2.0 + player["quality"] * 4 * leakiness[opponent]
                             + rng.normal(0, 1.5))
            elif cameo:
                points = 1.0
            rows.append({
                "code": player["code"], "season": season, "gw": gw,
                "element_type": player["element_type"], "team_id": team + team_offset,
                "opponent_team": opponent + team_offset, "opponent_name": f"T{opponent}",
                "was_home": bool(gw % 2), "num_fixtures": 1,
                "played": started or cameo, "started": started,
                "minutes": 90 if started else (20 if cameo else 0),
                "total_points": round(points), "goals_scored": int(points > 6),
                "assists": 0, "clean_sheets": 0, "goals_conceded": 1,
                "saves": 2 * int(started), "bonus": 0, "bps": points * 3,
                "defensive_contribution": 4 * int(started),
                "expected_goals": player["quality"] * 0.3,
                "expected_assists": player["quality"] * 0.2,
                "expected_goal_involvements": player["quality"] * 0.5,
                "ict_index": player["quality"] * 10.0,
            })
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def panel() -> pd.DataFrame:
    """A full archive season plus GW1-4 of the live season."""
    return pd.concat([_season(ARCHIVE, 20, seed=7),
                      _season(LIVE_SEASON, 4, seed=11, team_offset=100)],
                     ignore_index=True)


@pytest.fixture(scope="module")
def frame(panel) -> pd.DataFrame:
    """``panel`` with features and labels, for writing served forecasts."""
    return build_match_frame(panel)


def _write_live_xp(ml_dir, frame: pd.DataFrame, gw: int) -> None:
    """A served forecast that is exactly right, so its rows are recognisable."""
    target = frame[(frame["season"] == LIVE_SEASON) & (frame["gw"] == gw)]
    served = pd.DataFrame({
        "code": target["code"], "xp": target["label_points"].astype(float),
        "p_start": target["started"].astype(float), "gw": gw,
    })
    ml_dir.mkdir(parents=True, exist_ok=True)
    served.to_parquet(ml_dir / f"xp_gw{gw}.parquet", index=False)


def _run(panel, ml_dir, **kwargs) -> pd.DataFrame:
    return tr.run(panel, LIVE_SEASON, ml_dir, min_train_rows=SMALL_FIT, **kwargs)


def test_scores_every_finished_gameweek_with_the_right_key(panel, frame, tmp_path):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    table = _run(panel, ml_dir)

    on_disk = pd.read_csv(ml_dir / "track_record.csv")
    assert list(on_disk.columns) == tr.COLUMNS
    # GW1 has no in-season history and is skipped; GW2-4 are scored.
    assert sorted(on_disk["gw"].unique()) == [2, 3, 4]
    assert len(on_disk) == 3 * len(tr.POOLS) * 4 * len(tr.PREDICTORS)
    assert not on_disk.duplicated(tr.KEY).any()
    sources = on_disk.groupby("gw")["source"].unique().to_dict()
    assert [list(v) for v in sources.values()] == [["live"], ["replay"], ["replay"]]
    assert set(on_disk["predictor"]) == set(tr.PREDICTORS)
    # Brier belongs to the model row only.
    assert on_disk.loc[on_disk["predictor"] == "model_xp", "brier"].notna().all()
    assert on_disk.loc[on_disk["predictor"] != "model_xp", "brier"].isna().all()
    assert len(table) == len(on_disk)
    md = (ml_dir / "track_record.md").read_text(encoding="utf-8")
    assert "| 2 | live | live forecast |" in md
    assert "| 3 | replay | replay — not live evidence |" in md


def test_live_file_wins_over_replay(panel, frame, tmp_path):
    ml_dir = tmp_path / "ml"
    first = _run(panel, ml_dir)
    assert set(first.loc[first["gw"] == 2, "source"]) == {"replay"}

    _write_live_xp(ml_dir, frame, 2)
    second = _run(panel, ml_dir)
    gw2 = second[second["gw"] == 2]
    # The replayed GW2 rows are replaced, not kept beside the live ones.
    assert set(gw2["source"]) == {"live"}
    assert len(second) == len(first)
    # The perfect served forecast is what got scored.
    model = gw2[(gw2["predictor"] == "model_xp") & (gw2["pool"] == "all")]
    assert (model["spearman"].round(6) == 1.0).all()


def test_rerun_is_byte_identical_and_never_duplicates(panel, frame, tmp_path):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    _run(panel, ml_dir)
    csv_before = (ml_dir / "track_record.csv").read_bytes()
    md_before = (ml_dir / "track_record.md").read_bytes()

    _run(panel, ml_dir)
    assert (ml_dir / "track_record.csv").read_bytes() == csv_before
    assert (ml_dir / "track_record.md").read_bytes() == md_before
    assert not list(ml_dir.glob("*.tmp"))


def test_recorded_live_rows_survive_a_deleted_xp_file(panel, frame, tmp_path):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    before = _run(panel, ml_dir)
    (ml_dir / "xp_gw2.parquet").unlink()

    after = _run(panel, ml_dir)
    assert set(after.loc[after["gw"] == 2, "source"]) == {"live"}
    pd.testing.assert_frame_equal(after, before)


def test_xp_file_written_after_the_deadline_is_not_live(panel, frame, tmp_path, caplog):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    deadline = datetime(2026, 8, 28, 17, 30, tzinfo=timezone.utc)
    late = deadline.timestamp() + 86400
    os.utime(ml_dir / "xp_gw2.parquet", (late, late))
    bootstrap = {"events": {"current": 5, "next": 6, "data": [
        {"id": 2, "deadline_time": "2026-08-28T17:30:00Z", "finished": True}]}}

    with caplog.at_level(logging.WARNING):
        table = _run(panel, ml_dir, bootstrap=bootstrap)
    assert set(table.loc[table["gw"] == 2, "source"]) == {"replay"}
    assert "after the GW2 deadline" in caplog.text

    early = deadline.timestamp() - 86400
    os.utime(ml_dir / "xp_gw2.parquet", (early, early))
    table = _run(panel, ml_dir, bootstrap=bootstrap)
    assert set(table.loc[table["gw"] == 2, "source"]) == {"live"}


def test_gameweek_without_xp_or_training_rows_errors_naming_it(tmp_path):
    thin = _season(LIVE_SEASON, 2, seed=3)
    with pytest.raises(tr.TrackRecordError, match=r"^GW2: no xp_gw2\.parquet"):
        tr.run(thin, LIVE_SEASON, tmp_path / "ml")
    assert not (tmp_path / "ml" / "track_record.csv").exists()


def test_replay_sees_only_what_was_known_at_the_deadline(panel):
    """Replay is the serving path: GW3's xP ignores GW3 outcomes and anything later."""
    baseline = tr.replay_predictions(panel, LIVE_SEASON, [3], min_train_rows=SMALL_FIT)

    tampered = panel[~((panel["season"] == LIVE_SEASON) & (panel["gw"] > 3))].copy()
    week = (tampered["season"] == LIVE_SEASON) & (tampered["gw"] == 3)
    tampered.loc[week, "total_points"] = tampered.loc[week, "total_points"] + 10
    later = _season("2027-28", 5, seed=13, team_offset=200)
    replayed = tr.replay_predictions(pd.concat([tampered, later], ignore_index=True),
                                     LIVE_SEASON, [3], min_train_rows=SMALL_FIT)

    pd.testing.assert_series_equal(replayed["xp"], baseline["xp"])
    # Realized points are attached after scoring, so the tampered labels show up there.
    assert (replayed["label_points"] == baseline["label_points"] + 10).all()


def test_thin_position_scores_nan_not_crash(frame):
    rows = frame[(frame["season"] == LIVE_SEASON) & (frame["gw"] == 3)].copy()
    rows["xp"] = rows["label_points"].astype(float)
    rows["p_start"] = 0.5
    keepers = rows[rows["element_type"] == 1].index[: tr.me.MIN_TOP_N - 1]
    thin = pd.concat([rows[rows["element_type"] != 1], rows.loc[keepers]])

    scored = tr.score_gameweek(thin, 3, tr.REPLAY)
    assert len(scored) == len(tr.POOLS) * 4 * len(tr.PREDICTORS)
    gkp = scored[scored["position"] == "GKP"]
    assert gkp[["spearman", "top_frac", "mae", "brier"]].isna().all().all()
    assert scored.loc[scored["position"] == "MID", "spearman"].notna().any()


def test_gws_filter_rescores_only_those_and_keeps_the_rest(panel, tmp_path):
    ml_dir = tmp_path / "ml"
    full = _run(panel, ml_dir)
    partial = _run(panel, ml_dir, gws=[3])
    pd.testing.assert_frame_equal(partial, full)


def test_cli_reads_the_data_root(tmp_path, capsys):
    data = tmp_path / "data"
    archive_dir = data / "derived/ml"
    season_dir = data / f"derived/{LIVE_SEASON}/ml"
    archive_dir.mkdir(parents=True)
    season_dir.mkdir(parents=True)
    # A real-sized fit threshold needs >= 200 rows per position: 20 archive GWs give 600.
    _season(ARCHIVE, 20, seed=7).to_parquet(archive_dir / "player_gameweeks.parquet")
    _season(LIVE_SEASON, 3, seed=11, team_offset=100).to_parquet(
        season_dir / "player_gameweeks.parquet")
    raw = data / f"raw/{LIVE_SEASON}/bootstrap"
    raw.mkdir(parents=True)
    (raw / "bootstrap-static.json").write_text(json.dumps({"events": []}))

    assert tr.main(["--season", LIVE_SEASON, "--data-root", str(data)]) == 0
    out = capsys.readouterr().out
    assert "replay" in out and "rows in" in out
    assert len(pd.read_csv(season_dir / "track_record.csv")) == 2 * len(tr.POOLS) * 4 * 6


def _spy_replay(monkeypatch) -> list[list[int]]:
    """Record the gameweeks each ``replay_predictions`` call is asked to rebuild."""
    calls: list[list[int]] = []
    real = tr.replay_predictions

    def spy(panel, season, gws, min_train_rows=tr.mm.MIN_TRAIN_ROWS):
        calls.append(list(gws))
        return real(panel, season, gws, min_train_rows)

    monkeypatch.setattr(tr, "replay_predictions", spy)
    return calls


def test_recorded_gameweeks_are_skipped_on_a_rerun(panel, frame, tmp_path, monkeypatch):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    calls = _spy_replay(monkeypatch)
    first = _run(panel, ml_dir)
    csv_before = (ml_dir / "track_record.csv").read_bytes()
    md_before = (ml_dir / "track_record.md").read_bytes()

    # A recorded live week is not even re-read: a corrupt file would fail the join.
    (ml_dir / "xp_gw2.parquet").write_bytes(b"not parquet")
    second = _run(panel, ml_dir)
    assert calls == [[3, 4], []]
    pd.testing.assert_frame_equal(second, first)
    assert (ml_dir / "track_record.csv").read_bytes() == csv_before
    assert (ml_dir / "track_record.md").read_bytes() == md_before


def test_rescore_rebuilds_recorded_gameweeks(panel, tmp_path, monkeypatch):
    ml_dir = tmp_path / "ml"
    calls = _spy_replay(monkeypatch)
    first = _run(panel, ml_dir)
    csv_before = (ml_dir / "track_record.csv").read_bytes()

    again = _run(panel, ml_dir, rescore=True)
    assert calls == [[2, 3, 4], [2, 3, 4]]
    pd.testing.assert_frame_equal(again, first)
    assert (ml_dir / "track_record.csv").read_bytes() == csv_before


def test_cli_rescore_flag_reaches_run(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(tr, "_load_panels", lambda paths: pd.DataFrame())

    def fake_run(panel, season, ml_dir, bootstrap, gws, rescore=False):
        seen["rescore"] = rescore
        return pd.DataFrame(columns=tr.COLUMNS)

    monkeypatch.setattr(tr, "run", fake_run)
    tr.main(["--season", LIVE_SEASON, "--data-root", str(tmp_path), "--rescore"])
    assert seen == {"rescore": True}


def test_xp_file_for_another_gameweek_is_not_live(panel, frame, tmp_path, caplog):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 3)
    (ml_dir / "xp_gw3.parquet").rename(ml_dir / "xp_gw2.parquet")

    with caplog.at_level(logging.WARNING):
        assert tr.live_xp_path(ml_dir, 2, None) is None
    assert "forecasts GW [3], not GW2" in caplog.text
    table = _run(panel, ml_dir, gws=[2])
    assert set(table["source"]) == {"replay"}

    _write_live_xp(ml_dir, frame, 2)
    assert tr.live_xp_path(ml_dir, 2, None) == ml_dir / "xp_gw2.parquet"


def test_xp_file_without_a_gw_column_is_not_live(frame, tmp_path, caplog):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    path = ml_dir / "xp_gw2.parquet"
    pd.read_parquet(path).drop(columns="gw").to_parquet(path, index=False)

    with caplog.at_level(logging.WARNING):
        assert tr.live_xp_path(ml_dir, 2, None) is None
    assert "no gw column" in caplog.text


def _stamp(path, generated_at: datetime) -> None:
    """Rewrite an xP file through ``matchmodel.write_xp`` with a fixed build time."""
    tr.mm.write_xp(pd.read_parquet(path), path, generated_at=generated_at)


def test_generated_at_stamp_wins_over_mtime(frame, tmp_path, caplog):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    path = ml_dir / "xp_gw2.parquet"
    deadline = datetime(2026, 8, 28, 17, 30, tzinfo=timezone.utc)
    bootstrap = {"events": [{"id": 2, "deadline_time": "2026-08-28T17:30:00Z"}]}

    # Built four days early, then copied (mtime reset to after the deadline): live.
    _stamp(path, datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc))
    late = deadline.timestamp() + 86400
    os.utime(path, (late, late))
    assert tr.live_xp_path(ml_dir, 2, bootstrap) == path

    # Rebuilt in October with an old mtime: the stamp still exposes it.
    _stamp(path, datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    early = deadline.timestamp() - 86400
    os.utime(path, (early, early))
    with caplog.at_level(logging.WARNING):
        assert tr.live_xp_path(ml_dir, 2, bootstrap) is None
    assert "2026-10-06T12:00:00+00:00" in caplog.text


def test_unstamped_or_unreadable_stamp_falls_back_to_mtime(frame, tmp_path):
    ml_dir = tmp_path / "ml"
    _write_live_xp(ml_dir, frame, 2)
    path = ml_dir / "xp_gw2.parquet"
    mtime = datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp()
    os.utime(path, (mtime, mtime))
    assert tr.written_at(path).timestamp() == mtime

    table = pa.Table.from_pandas(pd.read_parquet(path), preserve_index=False)
    table = table.replace_schema_metadata({tr.mm.GENERATED_AT_KEY: b"yesterday"})
    pq.write_table(table, path)
    os.utime(path, (mtime, mtime))
    assert tr.written_at(path).timestamp() == mtime
