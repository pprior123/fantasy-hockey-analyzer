"""FakeYahooSource satisfies YahooSource and honours last_season."""

from fha.sources.yahoo.fake import FakeYahooSource
from fha.sources.yahoo.models import Game, LeagueSettings, LeagueSnapshot, Scoreboard, StatLine
from fha.sources.yahoo.source import YahooSource

LINE = StatLine("465.p.1", 2025, {"0": "82"})
SNAPSHOT = LeagueSnapshot(
    game=Game("465", 2026),
    settings=LeagueSettings("465.l.8076", 8, 2026, 1, 1, 25, (), ()),
    game_stat_categories=(),
    teams=(),
    available=(),
    stats={},
    last_season_stats={"465.p.1": LINE},
    scoreboard=Scoreboard(1, None, None, ()),
    next_scoreboard=None,
)


async def test_fake_serves_its_snapshot() -> None:
    fake = FakeYahooSource(SNAPSHOT)
    source: YahooSource = fake
    assert await source.fetch_snapshot() is SNAPSHOT
    without = await source.fetch_snapshot(last_season=False)
    assert without.last_season_stats is None
    assert without.game == SNAPSHOT.game
    assert fake.calls == [True, False]
