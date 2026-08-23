# Predictions log

A standing accountability system: every Premier League match gets a predicted
scoreline **written down before lineups lock**, then scored against reality
after the gameweek completes. The point is not the picks — it is building a
calibrated record of what the models (and the reasoning on top of them) get
right and wrong, so both improve.

## Process

1. **Before each gameweek's first lineup lock** (whenever that falls — Friday
   night, Saturday morning, a midweek afternoon, or several times in a
   congested festive week): write `gw<NN>.md` with, for every fixture in the
   event:
   - predicted scoreline
   - 2-3 sentence reasoning (model inputs used, qualitative adjustments, and
     which of the two disagreed)
2. **After the event's final fixture finishes**: fill in actual scorelines,
   mark exact / correct-result / wrong, and add a short retro on the misses.
   Postponed fixtures are marked P-P and scored with the gameweek they are
   rescheduled into.
3. `PREDICTIONS.md` keeps the running accuracy table (exact %, result %) per
   gameweek, plus recurring failure patterns worth feeding back into the
   match model.

Schedule-shape rules: predictions cover **every fixture attached to the FPL
event**, however many there are (single round, double gameweek, blanks) —
the event calendar in `game/game.json` + fixture kickoff times are the source
of truth, never an assumed weekly rhythm.

League-specific predictions (H2H matchup calls, waiver outcomes) follow the
same write-then-score loop but live in `data/predictions/` (gitignored —
they contain league and manager names, which are never committed).
