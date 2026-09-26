"""The demo league (FHA_DEMO=1): deterministic, complete, and usable by the engine."""

from collections import Counter

from fha.domain.engine import prefers_baseline, rate
from fha.sources.yahoo.demo import FREE_AGENTS, TEAMS, WEEK, demo_snapshot
from fha.sources.yahoo.models import IR_SLOTS
from fha.sources.yahoo.stat_map import build_stat_map, to_player_season

SNAP = demo_snapshot()


def test_the_same_seed_gives_the_same_league_and_another_seed_another() -> None:
    assert demo_snapshot() == SNAP
    assert demo_snapshot(seed=1) != SNAP


def test_eight_full_rosters_and_the_free_agents() -> None:
    assert len(SNAP.teams) == TEAMS == 8
    assert all(len(t.roster) == 27 for t in SNAP.teams)
    assert len(SNAP.available) == FREE_AGENTS == 300
    assert [t.is_mine for t in SNAP.teams].count(True) == 1
    keys = [p.player_key for p in SNAP.pool]
    assert len(keys) == len(set(keys)) == 8 * 27 + 300
    assert len({p.name for p in SNAP.pool}) == len(keys)  # names are unique too


def test_every_team_uses_every_slot_kind_within_the_league_settings() -> None:
    counts = {s.position: s.count for s in SNAP.settings.roster_slots}
    for team in SNAP.teams:
        used = Counter(e.selected_position for e in team.roster)
        assert set(used) >= {"C", "LW", "RW", "D", "G", "BN", "IR", "IR+"}
        assert all(used[slot] <= counts[slot] for slot in used)
        assert all(e.player.status == "IR" for e in team.roster if e.in_ir_slot)
        assert sum(e.selected_position in IR_SLOTS for e in team.roster) == 2


def test_both_seasons_have_a_line_for_every_pool_player() -> None:
    keys = {p.player_key for p in SNAP.pool}
    assert SNAP.last_season_stats is not None
    assert set(SNAP.stats) == set(SNAP.last_season_stats) == keys
    assert {line.season for line in SNAP.stats.values()} == {SNAP.game.season}
    assert {line.season for line in SNAP.last_season_stats.values()} == {SNAP.game.season - 1}


def test_the_engine_rates_the_demo_league() -> None:
    stat_map = build_stat_map(SNAP.settings.stat_categories, SNAP.game_stat_categories)
    assert stat_map.ppp_direct
    assert SNAP.last_season_stats is not None
    last = [to_player_season(p, SNAP.last_season_stats[p.player_key], stat_map) for p in SNAP.pool]
    now = [to_player_season(p, SNAP.stats[p.player_key], stat_map) for p in SNAP.pool]
    assert prefers_baseline(now, 10)  # early season: the baseline view applies
    result = rate(last)
    assert result.eligible_count > 300
    assert all(d > 0 for d in result.divisors.values())
    lines = [SNAP.last_season_stats[p.player_key].values for p in SNAP.pool if p.is_goalie]
    played = [v for v in lines if v["29"] != "0"]
    assert played
    assert all(v["26"].startswith(".") for v in played)  # Yahoo writes SV% as ".905"
    assert all(v["26"] == "-" for v in lines if v["29"] == "0")


def test_scoreboards_pair_every_team_once_each_week() -> None:
    assert SNAP.next_scoreboard is not None
    for board, week in ((SNAP.scoreboard, WEEK), (SNAP.next_scoreboard, WEEK + 1)):
        assert board.week == week
        teams = [k for m in board.matchups for k in m.team_keys]
        assert sorted(teams) == sorted(t.team_key for t in SNAP.teams)
    mine = SNAP.my_team
    assert mine is not None
    assert SNAP.scoreboard.opponent(mine.team_key) != SNAP.next_scoreboard.opponent(mine.team_key)
    assert (SNAP.scoreboard.week_start, SNAP.scoreboard.week_end) == ("2026-10-19", "2026-10-25")
