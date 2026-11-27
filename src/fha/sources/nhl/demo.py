"""A synthetic NHL slate for any date the demo requests."""

from datetime import UTC, date, datetime, time

from fha.domain.schedule import Game

# Two slates alternating by date, so "Tonight + tomorrow" differs from "Tonight". They
# share BOS, so the demo shows a back-to-back (two games over the two days).
SLATES = (
    (("TBL", "BOS"), ("NYR", "TOR"), ("EDM", "LAK")),
    (("BOS", "MTL"), ("CHI", "DAL"), ("VGK", "SEA")),
)


class DemoScheduleSource:
    async def games_on(self, day: date) -> tuple[Game, ...]:
        # 23:00 UTC or later, so every start falls on ``day`` in the East.
        early = datetime.combine(day, time(23), UTC).timestamp()
        teams = SLATES[day.toordinal() % 2]
        starts = (early, early + 1800, early + 3 * 3600)
        return tuple(
            Game(away, home, start, 2) for (away, home), start in zip(teams, starts, strict=True)
        )
