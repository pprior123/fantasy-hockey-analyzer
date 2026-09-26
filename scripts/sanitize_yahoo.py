"""Sanitize recorded Yahoo responses before they become fixtures (SPEC §9).

- drops every ``managers`` / ``manager`` block (nickname, guid, email,
  image_url, ...), any token field, and league invite links and chat IDs;
- replaces team names with ``Team N`` (N from the team key, which is kept)
  and team logos with a placeholder; the league's name with a placeholder,
  and drops its logo;
- keeps player data, which is public.

``problems`` then re-checks the result: a fixture is written only if it
reports nothing. The repo is public, so this errs on the side of refusing.
"""

from __future__ import annotations

import re
from typing import Any

DROP_KEYS = frozenset(
    {
        "managers",
        "manager",
        "access_token",
        "refresh_token",
        "id_token",
        "logo_url",
        "short_invitation_url",
        "password",
        "iris_group_chat_id",
    }
)
# Keys that must never survive, whatever block they turn up in.
FORBIDDEN_KEYS = frozenset({"managers", "manager", "guid", "email", "nickname"})
FORBIDDEN_KEYS |= {"access_token", "refresh_token", "id_token", "short_invitation_url"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
TEAM_LOGO = [{"team_logo": {"size": "large", "url": "https://example.invalid/team-logo.png"}}]
LEAGUE_NAME = "League"


def team_name(team_key: str) -> str:
    """``Team N`` for the team key ``{game}.l.{league}.t.N``."""
    _, sep, number = team_key.rpartition(".t.")
    if not sep or not number.isdigit():
        raise ValueError(f"not a team key: {team_key!r}")
    return f"Team {number}"


def _team_key(fragments: list[Any]) -> str | None:
    for item in fragments:
        if isinstance(item, dict) and isinstance(item.get("team_key"), str):
            return str(item["team_key"])
    return None


def sanitize(node: Any) -> Any:
    """A sanitized deep copy of a Yahoo response (or any part of one)."""
    if isinstance(node, list):
        out = [sanitize(item) for item in node]
        team_key = _team_key(out)
        if team_key is not None:  # a team split into fragments: [{team_key}, {name}, ...]
            for i, item in enumerate(out):
                if isinstance(item, dict) and "name" in item:
                    out[i] = {**item, "name": team_name(team_key)}
                if isinstance(item, dict) and "team_logos" in item:
                    out[i] = {**item, "team_logos": TEAM_LOGO}
        return out
    if isinstance(node, dict):
        out_d = {k: sanitize(v) for k, v in node.items() if k not in DROP_KEYS}
        if isinstance(out_d.get("team_key"), str):  # a team as one flat object
            if "name" in out_d:
                out_d["name"] = team_name(out_d["team_key"])
            if "team_logos" in out_d:
                out_d["team_logos"] = TEAM_LOGO
        if "league_key" in out_d and "name" in out_d:
            out_d["name"] = LEAGUE_NAME
        return out_d
    return node


def problems(node: Any, path: str = "$") -> list[str]:
    """Where ``node`` still holds something that must not be committed."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}: forbidden key")
            found.extend(problems(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(problems(item, f"{path}[{i}]"))
    elif isinstance(node, str) and EMAIL.search(node):
        found.append(f"{path}: email address")
    return found
