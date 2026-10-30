"""The committed Yahoo recording is safe to publish (SPEC §8, §10 M4.5).

The repo is public. ``scripts.sanitize_yahoo`` is a blocklist, and Yahoo adds
fields (the first real recording carried ``sendbird_channel_url``, the league
chat's channel, which the sanitizer didn't know). So every committed fixture is
also held to an allowlist: a key that isn't in ``ALLOWED_KEYS`` fails here and
gets reviewed before it can be committed.
"""

import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from scripts.record_yahoo import MANIFEST
from scripts.sanitize_yahoo import FORBIDDEN_KEYS, LEAGUE_NAME, TEAM_LOGO, problems, team_name

FIXTURES = Path(__file__).parents[1] / "fixtures" / "yahoo"
INDEX = re.compile(r"\d+")  # Yahoo's list-like objects: {"0": ..., "1": ..., "count": 2}

# Every key in the 2026-10-30 recording, each one reviewed: player and NHL
# data (public), league structure and settings, and the sanitizer's
# placeholders. No manager, contact, chat or token field.
ALLOWED_KEYS = frozenset(
    {
        "abbr",
        "allow_add_to_dl_extra_pos",
        "ascii_first",
        "ascii_last",
        "base_stat",
        "base_stats",
        "can_trade_draft_picks",
        "cant_cut_list",
        "code",
        "completed_games",
        "copyright",
        "cost",
        "count",
        "coverage_type",
        "coverage_value",
        "current_date",
        "current_week",
        "custom_scoring_preset_logo",
        "custom_scoring_preset_name",
        "date",
        "display_name",
        "display_position",
        "draft_pick_time",
        "draft_status",
        "draft_time",
        "draft_together",
        "draft_type",
        "edit_key",
        "editorial_player_key",
        "editorial_team_abbr",
        "editorial_team_full_name",
        "editorial_team_key",
        "editorial_team_url",
        "eligible_positions",
        "eligible_positions_to_add",
        "enabled",
        "end_date",
        "end_week",
        "fantasy_content",
        "felo_tier",
        "first",
        "full",
        "game",
        "game_code",
        "game_id",
        "game_key",
        "games",
        "group",
        "group_abbr",
        "group_display_name",
        "group_name",
        "groups",
        "has_draft_grade",
        "has_multiweek_championship",
        "has_player_notes",
        "has_playoff_consolation_games",
        "has_recent_player_notes",
        "headshot",
        "image_url",
        "injury_note",
        "invite_permission",
        "is_auction_draft",
        "is_cash_league",
        "is_composite_stat",
        "is_consolation",
        "is_editable",
        "is_flex",
        "is_game_over",
        "is_guillotine",
        "is_highscore",
        "is_keeper",
        "is_live_draft_lobby_active",
        "is_matchup_of_the_week",
        "is_offseason",
        "is_only_display_stat",
        "is_owned_by_current_login",
        "is_playoffs",
        "is_plus_league",
        "is_prescoring",
        "is_pro_league",
        "is_registration_over",
        "is_starting_position",
        "is_tied",
        "is_undroppable",
        "kept",
        "last",
        "league",
        "league_id",
        "league_key",
        "league_premium_features",
        "league_scoring_type",
        "league_type",
        "league_update_timestamp",
        "live_games",
        "matchup",
        "matchup_week",
        "matchups",
        "max_teams",
        "max_weekly_adds",
        "min_games_played",
        "minimum_games",
        "name",
        "num_playoff_consolation_teams",
        "num_playoff_teams",
        "num_teams",
        "number_of_moves",
        "number_of_trades",
        "on_disabled_list",
        "player",
        "player_advanced_stats",
        "player_id",
        "player_key",
        "player_notes_last_timestamp",
        "player_pool",
        "player_stats",
        "players",
        "playoff_start_week",
        "position",
        "position_type",
        "position_types",
        "post_draft_players",
        "previous_season_team_rank",
        "primary_position",
        "recordbook_url",
        "refresh_rate",
        "remaining_games",
        "renewed",
        "roster",
        "roster_adds",
        "roster_import_deadline",
        "roster_position",
        "roster_positions",
        "roster_type",
        "scoreboard",
        "scoring_label",
        "scoring_type",
        "season",
        "selected_position",
        "settings",
        "size",
        "sort_order",
        "start_date",
        "start_week",
        "stat",
        "stat_categories",
        "stat_id",
        "stat_position_type",
        "stat_position_types",
        "stat_winner",
        "stat_winners",
        "stats",
        "status",
        "status_full",
        "team",
        "team_id",
        "team_key",
        "team_logo",
        "team_logos",
        "team_points",
        "team_remaining_games",
        "team_stats",
        "teams",
        "time",
        "total",
        "trade_end_date",
        "trade_ratify_type",
        "trade_reject_time",
        "type",
        "uniform_number",
        "url",
        "uses_faab",
        "uses_lock_eliminated_teams",
        "uses_median_score",
        "uses_playoff",
        "uses_playoff_reseeding",
        "uses_roster_import",
        "value",
        "waiver_priority",
        "waiver_rule",
        "waiver_time",
        "waiver_type",
        "week",
        "week_end",
        "week_has_enough_qualifying_days",
        "week_start",
        "weekly_deadline",
        "winner_team_key",
        "xml:lang",
        "yahoo:uri",
    }
)


@cache
def fixtures() -> dict[str, Any]:
    return {
        path.name: json.loads(path.read_text())
        for path in sorted(FIXTURES.glob("*.json"))
        if path.name != MANIFEST
    }


def keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node).union(*(keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(keys(v) for v in node))
    return set()


def test_there_is_a_recording() -> None:
    assert len(fixtures()) > 100


def test_the_manifest_lists_every_fixture_file_and_no_other() -> None:
    manifest = json.loads((FIXTURES / MANIFEST).read_text())
    assert sorted(c["file"] for c in manifest["calls"]) == sorted(fixtures())


@pytest.mark.parametrize("name", sorted(fixtures()))
def test_a_fixture_passes_the_sanitizer_s_checks(name: str) -> None:
    assert problems(fixtures()[name]) == []


def test_every_key_in_the_recording_was_reviewed() -> None:
    found = keys(list(fixtures().values()))
    unreviewed = {k for k in found if not INDEX.fullmatch(k)} - ALLOWED_KEYS
    assert unreviewed == set(), f"review these Yahoo fields before committing: {sorted(unreviewed)}"


def test_no_forbidden_key_is_allowed() -> None:
    assert ALLOWED_KEYS.isdisjoint(FORBIDDEN_KEYS)


# ---------------------------------------------------------------- values, not just keys
# The sanitizer's name check needs the original names, which CI never has, so
# these pin what the sanitizer leaves in place of them (PR #19 review).

URL_HOSTS = frozenset(
    {"s.yimg.com", "sports.yahoo.com", "hockey.fantasysports.yahoo.com", "example.invalid"}
)


def merged(node: Any) -> Iterator[dict[str, Any]]:
    """Every object, with Yahoo's fragment lists ([{"team_key": ...}, {"name": ...}])
    merged into one."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from merged(value)
    elif isinstance(node, list):
        fragments = [item for item in node if isinstance(item, dict)]
        if fragments:
            yield {k: v for item in fragments for k, v in item.items()}
        for item in node:
            yield from merged(item)


def strings(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)
    elif isinstance(node, str):
        yield node


def test_teams_carry_only_placeholder_names_and_logos() -> None:
    teams = [o for o in merged(list(fixtures().values())) if "team_key" in o and "name" in o]
    assert teams
    for team in teams:
        assert team["name"] == team_name(team["team_key"])
        if "team_logos" in team:
            assert team["team_logos"] == TEAM_LOGO


def test_the_league_carries_only_the_placeholder_name() -> None:
    leagues = [o for o in merged(list(fixtures().values())) if "league_key" in o and "name" in o]
    assert leagues
    assert {o["name"] for o in leagues} == {LEAGUE_NAME}


# Any URL in any string: http(s) in any case, or scheme-relative, alone or in text.
# The authority runs to the first "/", "?", "#" or whitespace, so no character
# can end it early (Codex rounds 2-3: ":@", "'@", '"@' and "<@" each hid the host).
AUTHORITY = re.compile(r"(?i)(?:https?:)?//([^/?#\s]*)")
HOST = re.compile(r"([a-z0-9-]+(?:\.[a-z0-9-]+)*)(?::\d+)?", re.IGNORECASE)


def url_hosts(text: str) -> list[str | None]:
    """Each URL's host; None when the authority is anything but a plain host and
    optional port (credentials, quotes, brackets...): none belongs in a fixture."""
    hosts: list[str | None] = []
    for match in AUTHORITY.finditer(text):
        host = HOST.fullmatch(match.group(1))
        hosts.append(host.group(1).lower() if host else None)
    return hosts


def test_urls_point_only_at_yahoo_s_public_hosts_or_the_placeholder() -> None:
    hosts = {h for text in strings(list(fixtures().values())) for h in url_hosts(text)}
    assert hosts
    assert hosts <= URL_HOSTS, sorted(str(h) for h in hosts - URL_HOSTS)


@pytest.mark.parametrize(
    ("text", "host"),
    [
        ("https://s.yimg.com/a.png", "s.yimg.com"),
        ("HTTPS://Evil.Example/x", "evil.example"),
        ("//cdn.other.com/x.png", "cdn.other.com"),
        ("see https://evil.example/x for more", "evil.example"),
        ("http://host.example:8080/x", "host.example"),
        ("https://s.yimg.com:@external.example/a.png", None),  # credentials: refused
        ("https://s.yimg.com@external.example/a.png", None),
        ("https://s.yimg.com'@external.example/a.png", None),  # Codex round 3
        ('https://s.yimg.com"@external.example/a.png', None),
        ("https://s.yimg.com<@external.example/a.png", None),
        ("https://s.yimg.com?x=1", "s.yimg.com"),
        ("https://[::1]/x", None),  # not a plain host name
    ],
)
def test_the_url_check_finds_every_form(text: str, host: str | None) -> None:
    assert url_hosts(text) == [host]


# ---------------------------------------------------------------- the manifest
# The recorder writes it from the request paths, but it is committed too (Codex review).

MANIFEST_KEYS = {"params", "calls"}
PARAMS_KEYS = {"league_id", "available", "available_sort"}
CALL_KEYS = {"path", "status", "file"}
PATH = re.compile(r"[A-Za-z0-9_./;=,-]+")  # an API path the source builds: no free text


def test_the_manifest_holds_only_its_schema() -> None:
    manifest = json.loads((FIXTURES / MANIFEST).read_text())
    assert set(manifest) == MANIFEST_KEYS
    assert set(manifest["params"]) == PARAMS_KEYS
    assert all(set(call) == CALL_KEYS for call in manifest["calls"])
    assert all(PATH.fullmatch(call["path"]) for call in manifest["calls"])
    assert problems(manifest) == []
