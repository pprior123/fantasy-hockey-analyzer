"""A synthetic NHL slate for any date the demo requests."""

from datetime import UTC, date, datetime, time

from fha.domain.schedule import Game


class DemoScheduleSource:
    async def games_on(self, day: date) -> tuple[Game, ...]:
        early = datetime.combine(day, time(23), UTC).timestamp()
        return (
            Game("TBL", "BOS", early, 2),
            Game("NYR", "TOR", early + 1800, 2),
            Game("EDM", "LAK", early + 3 * 3600, 2),
        )
