package insights

// Form computes the rolling average points for a team over the last `window`
// completed gameweeks strictly before `asOfGW`.
func Form(
	weekly WeeklyPoints,
	teamID int,
	asOfGW int,
	window int,
) float64 {
	if window <= 0 {
		window = 3
	}

	sum := 0
	count := 0

	for gw := asOfGW - 1; gw >= 1 && count < window; gw-- {
		pointsByTeam, ok := weekly[gw]
		if !ok {
			continue
		}
		pts, ok := pointsByTeam[teamID]
		if !ok {
			continue
		}

		sum += pts
		count++
	}

	if count == 0 {
		return 0
	}

	return float64(sum) / float64(count)
}