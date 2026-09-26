"""The league salary sheet in the app (SPEC §4a): read, bind, match, report.

- ``read_league_sheet`` fetches and stores the **parsed** sheet only (the
  grid, with the GMs' contact details, is discarded by the parser).
- Each tab is bound once to a Yahoo team (``bind_tab``); ``suggest_bindings``
  proposes a team by roster-name overlap.
- Each sheet row is matched **within the bound team's roster** (roster
  scope, SPEC §6). Automatic matches are persisted like confirmed ones, so a
  row is never re-matched by name once bound (``tab_reports``).
- ``TabReport`` is the discrepancy report: Yahoo roster players missing
  from the tab, tab rows not on the Yahoo roster, and a tab payroll that
  differs from the sum of its matched counted rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fha.domain.matcher import Aliases, MatchResult, Query, Scope, Scored, Status, match
from fha.domain.names import canonical_team
from fha.services.clock import Clock
from fha.services.free_agents import auto_how, candidates, row_key
from fha.sources.league_sheet.models import ParsedSheet, ParsedTab, SheetRow
from fha.sources.league_sheet.source import LeagueSheetSource
from fha.sources.yahoo.models import Player, Team
from fha.storage.repository import Repository

COLLECTION = "league_sheet"
LATEST = "latest"  # {"read_at", "sheet"}
TAB_BINDINGS = "tab_bindings"  # {"entries": {tab name: team_key}}
ROW_BINDINGS = "row_bindings"  # {"entries": {tab name: {row key: {"player_id", "how"}}}}
MIN_OVERLAP = 0.5  # share of a tab's rows that must match a roster to suggest it


class LeagueSheetServiceError(ValueError):
    """An owner action that can't be applied (unknown tab or team)."""


# ---------------------------------------------------------------- the stored sheet


async def read_league_sheet(
    repo: Repository, source: LeagueSheetSource, clock: Clock
) -> ParsedSheet:
    """Read the sheet (live or an uploaded .xlsx) and store what was parsed."""
    sheet = await source.fetch()
    await repo.put(COLLECTION, LATEST, {"read_at": clock.now(), "sheet": encode_sheet(sheet)})
    return sheet


async def load_league_sheet(repo: Repository) -> tuple[ParsedSheet, float] | None:
    """The last sheet read and when, or None before the first read."""
    doc = await repo.get(COLLECTION, LATEST)
    if doc is None:
        return None
    return decode_sheet(doc["sheet"]), float(doc["read_at"])


def encode_sheet(sheet: ParsedSheet) -> dict[str, Any]:
    return {
        "cap": sheet.cap,
        "cap_source": sheet.cap_source,
        "other_tabs": list(sheet.other_tabs),
        "tabs": [
            {
                "name": t.name,
                "status": t.status,
                "reason": t.reason,
                "payroll": t.payroll,
                "cap": t.cap,
                "salary_column": t.salary_column,
                "payroll_formula": t.payroll_formula,
                "payroll_range": t.payroll_range,
                "rows": [
                    {
                        "row": r.row,
                        "name": r.name,
                        "position": r.position,
                        "team": r.team,
                        "salary": r.salary,
                        "counted": r.counted,
                        "ir": r.ir,
                    }
                    for r in t.rows
                ],
            }
            for t in sheet.tabs
        ],
    }


def decode_sheet(doc: Mapping[str, Any]) -> ParsedSheet:
    return ParsedSheet(
        cap=doc["cap"],
        cap_source=doc["cap_source"],
        other_tabs=tuple(doc["other_tabs"]),
        tabs=tuple(
            ParsedTab(
                name=t["name"],
                status=t["status"],
                reason=t["reason"],
                payroll=t["payroll"],
                cap=t["cap"],
                salary_column=t["salary_column"],
                payroll_formula=t["payroll_formula"],
                payroll_range=t["payroll_range"],
                rows=tuple(
                    SheetRow(
                        r["row"],
                        r["name"],
                        r["position"],
                        r["team"],
                        r["salary"],
                        r["counted"],
                        r["ir"],
                    )
                    for r in t["rows"]
                ),
            )
            for t in doc["tabs"]
        ),
    )


# ---------------------------------------------------------------- tab <-> team


async def load_tab_bindings(repo: Repository) -> dict[str, str]:
    return await _entries(repo, TAB_BINDINGS)


async def bind_tab(repo: Repository, tab: str, team_key: str | None) -> None:
    """Bind ``tab`` to a Yahoo team (``None`` unbinds). A team has at most one tab."""
    bindings = await load_tab_bindings(repo)
    if bindings.get(tab) == team_key:
        return  # unchanged: keep the tab's row bindings (the owner's confirmations)
    if team_key is None:
        bindings.pop(tab, None)
    else:
        for other, bound in list(bindings.items()):
            if bound == team_key and other != tab:
                raise LeagueSheetServiceError(f"that team is already bound to tab {other!r}")
        bindings[tab] = team_key
    await repo.put(COLLECTION, TAB_BINDINGS, {"entries": bindings})
    rows = await _entries(repo, ROW_BINDINGS)
    if rows.pop(tab, None) is not None:  # rows were matched against the old roster
        await repo.put(COLLECTION, ROW_BINDINGS, {"entries": rows})


def suggest_bindings(
    sheet: ParsedSheet, teams: Sequence[Team], aliases: Aliases
) -> dict[str, str | None]:
    """A team per tab by roster-name overlap, or None when no team is a clear fit.

    A team is suggested when it matches the most of the tab's rows, at least
    ``MIN_OVERLAP`` of them, and no other team matches as many; a team that
    would be suggested for two tabs is suggested for neither.
    """
    best: dict[str, str | None] = {}
    for tab in sheet.tabs:
        scores = sorted(
            ((_overlap(tab, t, aliases), t.team_key) for t in teams), key=lambda s: -s[0]
        )
        top = scores[0] if scores else (0, None)
        tied = len(scores) > 1 and scores[1][0] == top[0]
        enough = bool(tab.rows) and top[0] >= MIN_OVERLAP * len(tab.rows)
        best[tab.name] = top[1] if enough and not tied else None
    counts: dict[str, int] = {}
    for key in best.values():
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return {tab: key if key is None or counts[key] == 1 else None for tab, key in best.items()}


def _overlap(tab: ParsedTab, team: Team, aliases: Aliases) -> int:
    roster = candidates(e.player for e in team.roster)
    return sum(
        match(_query(r), roster, aliases=aliases, scope=Scope.ROSTER).status is Status.MATCHED
        for r in tab.rows
    )


# ---------------------------------------------------------------- rows <-> roster


@dataclass(frozen=True)
class RowMatch:
    row: SheetRow
    key: str
    player_id: str | None  # the bound roster player, or None
    how: str | None  # "auto:<step>", "confirmed"; None if unbound
    result: MatchResult | None  # the cascade's answer when the row isn't bound


@dataclass(frozen=True)
class TabReport:
    """One tab against its bound Yahoo team: row matches and discrepancies (SPEC §4a)."""

    tab: ParsedTab
    team_key: str | None  # None: the tab isn't bound yet
    rows: tuple[RowMatch, ...]
    missing_from_tab: tuple[Player, ...]  # on the Yahoo roster, in no row
    not_on_roster: tuple[SheetRow, ...]  # rows matched to no roster player
    matched_counted_total: int  # the numeric salaries of matched counted rows
    summed_rows: int = 0  # how many salaries that total adds (for the rounding tolerance)
    sheet_cap: int | None = None  # the summary tab's cap
    unknown_teams: tuple[str, ...] = ()  # NHL team strings no code matches (SPEC §6)

    @property
    def payroll(self) -> int | None:
        """The team's official payroll; None if the tab is unrecognized or unbound (SPEC §5)."""
        return self.tab.payroll if self.team_key is not None and self.tab.status == "ok" else None

    @property
    def payroll_differs(self) -> bool:
        """The tab's PAYROLL isn't the sum of its matched counted rows. Each salary and
        the payroll are rounded to whole dollars separately (at most $0.50 each), so a
        difference within that is rounding, not a discrepancy."""
        if self.payroll is None:
            return False
        return abs(self.payroll - self.matched_counted_total) > (self.summed_rows + 1) / 2

    @property
    def cap_differs(self) -> bool:
        """The tab's CAP isn't the summary tab's cap (SPEC §4a)."""
        return (
            self.tab.cap is not None
            and self.sheet_cap is not None
            and self.tab.cap != self.sheet_cap
        )

    @property
    def has_discrepancies(self) -> bool:
        return bool(
            self.missing_from_tab or self.not_on_roster or self.payroll_differs or self.cap_differs
        )


def match_tab(
    tab: ParsedTab,
    team: Team | None,
    aliases: Aliases,
    bound_rows: Mapping[str, Mapping[str, str]],
    sheet_cap: int | None = None,
) -> TabReport:
    """Match ``tab``'s rows within ``team``'s roster (pure). Bound rows keep their player.

    An unbound tab, or an unrecognized one, reports no row discrepancies: there is
    nothing yet to compare (its status is the report).
    """
    unknown = tuple(sorted({r.team for r in tab.rows if r.team and canonical_team(r.team) is None}))
    if team is None or tab.status != "ok":
        idle = tuple(RowMatch(r, _key(r), None, None, None) for r in tab.rows)
        return TabReport(
            tab, None if team is None else team.team_key, idle, (), (), 0, 0, sheet_cap, unknown
        )
    roster = {e.player.player_id: e.player for e in team.roster}
    pool = candidates(roster.values())
    matches: list[RowMatch] = []
    taken: dict[str, int] = {}
    for row in tab.rows:
        key = _key(row)
        bound = bound_rows.get(key)
        if bound is not None and bound["player_id"] in roster:
            matches.append(RowMatch(row, key, bound["player_id"], bound["how"], None))
        else:
            result = match(_query(row), pool, aliases=aliases, scope=Scope.ROSTER)
            if result.status is Status.MATCHED and result.player is not None:
                how = auto_how(result)
                matches.append(RowMatch(row, key, result.player.player_id, how, None))
            else:
                matches.append(RowMatch(row, key, None, None, result))
        pid = matches[-1].player_id
        if pid is not None:
            taken[pid] = taken.get(pid, 0) + 1
    # Two rows on one player: neither is trusted; both go to review, with that player.
    matches = [
        m
        if m.player_id is None or taken[m.player_id] == 1
        else RowMatch(m.row, m.key, None, None, _contested(roster[m.player_id]))
        for m in matches
    ]
    matched_ids = {m.player_id for m in matches if m.player_id is not None}
    summed = [
        m.row.salary for m in matches if m.player_id and m.row.counted and m.row.salary is not None
    ]
    return TabReport(
        tab=tab,
        team_key=team.team_key,
        rows=tuple(matches),
        missing_from_tab=tuple(p for pid, p in roster.items() if pid not in matched_ids),
        not_on_roster=tuple(m.row for m in matches if m.player_id is None),
        matched_counted_total=sum(summed),
        summed_rows=len(summed),
        sheet_cap=sheet_cap,
        unknown_teams=unknown,
    )


def _contested(player: Player) -> MatchResult:
    [candidate] = candidates([player])
    return MatchResult(Status.AMBIGUOUS, candidates=(Scored(candidate, 100.0),))


async def tab_reports(
    repo: Repository, sheet: ParsedSheet, teams: Sequence[Team], aliases: Aliases
) -> list[TabReport]:
    """Every tab's report; new automatic row matches are persisted (bind once)."""
    tab_bindings = await load_tab_bindings(repo)
    row_bindings = await _entries(repo, ROW_BINDINGS)
    by_key = {t.team_key: t for t in teams}
    reports = []
    changed = False
    for tab in sheet.tabs:
        team = by_key.get(tab_bindings.get(tab.name, ""))
        bound = dict(row_bindings.get(tab.name, {}))
        report = match_tab(tab, team, aliases, bound, sheet.cap)
        for m in report.rows:
            if m.player_id is not None and bound.get(m.key, {}).get("player_id") != m.player_id:
                bound[m.key] = {"player_id": m.player_id, "how": m.how}
                changed = True
        if bound:
            row_bindings[tab.name] = bound
        reports.append(report)
    if changed:
        await repo.put(COLLECTION, ROW_BINDINGS, {"entries": row_bindings})
    return reports


async def confirm_row(repo: Repository, tab: str, key: str, player_id: str) -> None:
    """The owner binds a sheet row to a roster player (match review)."""
    if tab not in await load_tab_bindings(repo):
        raise LeagueSheetServiceError(f"tab {tab!r} isn't bound to a team yet")
    rows = await _entries(repo, ROW_BINDINGS)
    rows.setdefault(tab, {})[key] = {"player_id": player_id, "how": "confirmed"}
    await repo.put(COLLECTION, ROW_BINDINGS, {"entries": rows})


# ---------------------------------------------------------------- salaries for the app


@dataclass(frozen=True)
class RosteredSalary:
    aav: int | None  # None: the sheet cell had no number
    counted: bool  # in the PAYROLL range (False: an IR row, not counted)
    tab: str


def rostered_salaries(reports: Iterable[TabReport]) -> dict[str, RosteredSalary]:
    """Cap hits of matched sheet rows by Yahoo player ID (the sheet wins, SPEC §4)."""
    return {
        m.player_id: RosteredSalary(m.row.salary, m.row.counted, r.tab.name)
        for r in reports
        if r.team_key is not None
        for m in r.rows
        if m.player_id is not None
    }


def payrolls(reports: Iterable[TabReport]) -> dict[str, int | None]:
    """Official payroll by team key, for bound tabs (None if the tab is unrecognized)."""
    return {r.team_key: r.payroll for r in reports if r.team_key is not None}


def _key(row: SheetRow) -> str:
    return row_key(row.name, row.position)


def _query(row: SheetRow) -> Query:
    return Query(row.name, row.team or None, row.position or None)


async def _entries(repo: Repository, doc_id: str) -> dict[str, Any]:
    doc = await repo.get(COLLECTION, doc_id)
    return dict(doc["entries"]) if doc is not None else {}
