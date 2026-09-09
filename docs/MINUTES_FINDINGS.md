# Predicting minutes: what the data says

Two experiments on the 2025-26 per-gameweek panel (29,747 rows) plus the
2024-25 season aggregates. Everything below is measured, not assumed.

## 1. Last season stops mattering almost immediately

Predicting a player's start rate in GW k+1..k+3, Spearman:

| after k GWs | prior season only | this season to date | best blend weight on prior |
|---|---|---|---|
| 1 | 0.611 | 0.765 | 0.5 |
| 2 | 0.597 | 0.739 | 0.5 |
| 3 | 0.599 | 0.749 | **0.0** |
| 5 | 0.561 | 0.749 | 0.0 |
| 10 | 0.584 | 0.833 | 0.0 |

**One gameweek of current-season evidence already beats a full prior season.**
From GW3 onward the optimal weight on last season is zero — blending it in
makes predictions worse, not better. The "new managers and new signings make
last season unreliable" problem is therefore real but short-lived: it is a
two-to-three gameweek problem, not a season-long one.

## 2. Within that cold-start window, club moves wreck the prior

Prior-season start rate vs actual GW1-3 start rate:

| group | n | Spearman |
|---|---|---|
| same club | 448 | **0.656** |
| changed club | 69 | **0.245** |
| new to the league | 195 | no prior exists |

Of last season's nailed starters (start rate ≥ 0.75):

| group | still nailed in GW1-3 | mean start rate now |
|---|---|---|
| same club | 68% | 0.79 |
| changed club | **40%** | 0.53 |

A nailed starter who changes club is closer to a coin flip than a certainty.
And 27% of the pool has no prior season at all, so a model that leans on last
season is silent on a quarter of the players.

## 3. Recent minutes beat everything, including start rate

Mean Spearman across k = 4..30:

| predictor | predicting future START RATE | predicting future MINUTES |
|---|---|---|
| trailing 3 GW mean minutes | **0.791** | **0.835** |
| exponentially weighted starts (halflife 3) | 0.780 | — |
| trailing 3 GW start rate | 0.770 | 0.771 |
| trailing 5 GW mean | 0.769 | 0.830 |
| season to date | 0.757 | 0.811 |
| last gameweek only | 0.735 | — |

Two design consequences:

- **Use minutes, not the binary started flag** — minutes beat starts even at
  predicting starts, because they separate "played 90" from "hooked at 60" and
  "unused sub" from "25-minute cameo".
- **Use a short recency window (~3 games), not season-to-date.** But not one
  game: last-gameweek-only is the worst predictor tested. Three games is the
  sweet spot; exponential weighting with a 3-game halflife is equivalent.

## Implications for the minutes model

1. Target expected minutes directly, via the three states an official team
   sheet reports: omitted / benched / started.
2. Workhorse feature: trailing 3-gameweek mean minutes.
3. Prior-season blend weight ~0.5-1 for GW1-2, zero afterwards, and lower
   still for players who changed club. Flag club changes explicitly.
4. Official team sheets (already fetched ~1h before kickoff) are the ground
   truth and the cold-start rescue: one observed XI per club outranks a whole
   prior season. They also distinguish benched from omitted, which the panel
   cannot.
5. Missing inputs worth adding as a small maintained table: manager change and
   European competition (midweek rotation). Roughly twenty rows, rarely edited.

## Caveat

The cold-start figures come from a single season transition (2024-25 to
2025-26). One observation — directionally strong, but wide error bars.
