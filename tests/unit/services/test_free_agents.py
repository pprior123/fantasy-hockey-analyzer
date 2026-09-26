"""Free-agent salary import: bind once, idempotent re-import, review, overrides (SPEC §6)."""

from typing import Any

import pytest

from fha.domain.matcher import NO_ALIASES, Aliases, Status
from fha.services.free_agents import (
    FreeAgentError,
    confirm,
    import_free_agent_salaries,
    load_free_agent_salaries,
    pending_reviews,
    row_key,
    set_aav_override,
    unbind,
)
from fha.sources.puckpedia import SalaryRow, parse_salary_csv
from fha.sources.yahoo.models import Player
from fha.storage.memory import InMemoryRepository


def player(pid: str, name: str, team: str = "", pos: str = "C") -> Player:
    ptype = "G" if pos == "G" else "P"
    return Player(f"465.p.{pid}", pid, name, team, pos, tuple(pos.split(",")), ptype)


POOL = [
    player("1", "Ada Knight", "TB"),
    player("2", "Sebastian Aho", "Car", "C"),
    player("3", "Sebastian Aho", "NYI", "D"),
    player("4", "Matthew Boldy", "Min", "LW"),
    player("5", "Cy Wall", "Bos", "G"),
    player("6", "Bo Stone", "Edm", "D"),
]


def row(name: str, pos: str, aav: int, line: int = 2, team: str | None = None) -> SalaryRow:
    return SalaryRow(name, pos, team, aav, line)


ROWS = [
    row("Knight, Ada", "C", 1_000_000, 2),
    row("Aho, Sebastian", "C", 9_750_000, 3),
    row("Aho, Sebastian", "D", 875_000, 4),
    row("Boldy, Matt", "L", 7_000_000, 5),
    row("Wall, Cy", "G", 2_500_000, 6),
    row("Nobody, Known", "R", 800_000, 7),
]


class CountingRepository(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    async def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        self.writes += 1
        await super().put(collection, doc_id, doc)

    async def replace_all(self, collection: str, docs: Any) -> None:
        self.writes += 1
        await super().replace_all(collection, docs)


def test_row_key_is_normalized_name_and_position_group() -> None:
    assert row_key("Aho, Sebastian", "D") == "sebastian aho|D"
    assert row_key("Sebastian  AHO", "LW") == "sebastian aho|F"
    assert row_key("X Y", None) == "x y|?"


async def test_first_import_binds_what_the_cascade_can_and_lists_the_rest() -> None:
    repo = InMemoryRepository()
    report = await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    assert (report.rows, report.bound, report.already_bound, report.changed) == (6, 5, 0, True)
    assert [r.row.name for r in report.reviews] == ["Nobody, Known"]
    assert report.reviews[0].result.status is Status.UNMATCHED
    salaries = await load_free_agent_salaries(repo)
    assert salaries.aav == {
        "1": 1_000_000,
        "2": 9_750_000,  # the C
        "3": 875_000,  # the D: position group broke the tie
        "4": 7_000_000,  # Matt = Matthew
        "5": 2_500_000,  # goalies too (cap checks)
    }
    assert salaries.problems == ()


async def test_re_importing_the_same_rows_writes_nothing() -> None:
    repo = CountingRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    before = repo.writes
    snapshot = {c: dict(d) for c, d in repo.collections.items()}
    again = await import_free_agent_salaries(repo, list(reversed(ROWS)), POOL, NO_ALIASES)
    assert repo.writes == before
    assert repo.collections == snapshot
    assert (again.changed, again.bound, again.already_bound) == (False, 0, 5)


async def test_bindings_are_kept_not_re_matched() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    # A new pool player with the same name would now make the name ambiguous.
    crowded = [*POOL, player("9", "Ada Knight", "Ott")]
    report = await import_free_agent_salaries(repo, ROWS, crowded, NO_ALIASES)
    assert report.already_bound == 5
    assert (await load_free_agent_salaries(repo)).aav["1"] == 1_000_000


async def test_a_changed_cap_hit_updates_in_place_keeping_the_binding() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    changed = [row("Knight, Ada", "C", 1_250_000, 2), *ROWS[1:]]
    report = await import_free_agent_salaries(repo, changed, POOL, NO_ALIASES)
    assert (report.changed, report.bound) == (True, 0)
    assert (await load_free_agent_salaries(repo)).aav["1"] == 1_250_000


async def test_a_row_dropped_from_the_file_no_longer_gives_a_salary() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    await import_free_agent_salaries(repo, ROWS[1:], POOL, NO_ALIASES)
    assert "1" not in (await load_free_agent_salaries(repo)).aav


async def test_duplicates_collapse_and_conflicting_ones_are_not_imported() -> None:
    repo = InMemoryRepository()
    rows = [*ROWS, row("Knight, Ada", "C", 1_000_000, 9), row("Stone, Bo", "D", 1, 10)]
    rows.append(row("Stone, Bo", "D", 2, 11))
    report = await import_free_agent_salaries(repo, rows, POOL, NO_ALIASES)
    assert report.rows == 6
    assert report.conflicts == ("bo stone|D",)
    assert "6" not in (await load_free_agent_salaries(repo)).aav


async def test_aliases_apply() -> None:
    repo = InMemoryRepository()
    aliases = Aliases().with_alias("Bo Stone", "Robert Stonewall")
    report = await import_free_agent_salaries(
        repo, [row("Stonewall, Robert", "D", 900_000)], POOL, aliases
    )
    assert report.bound == 1
    assert (await load_free_agent_salaries(repo)).aav == {"6": 900_000}


async def test_review_then_confirm_binds_the_row() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    [pending] = await pending_reviews(repo, POOL, NO_ALIASES)
    assert pending.key == "known nobody|F"
    assert len(pending.result.candidates) == 3
    await confirm(repo, pending.key, "6")
    assert await pending_reviews(repo, POOL, NO_ALIASES) == []
    assert (await load_free_agent_salaries(repo)).aav["6"] == 800_000
    await unbind(repo, pending.key)
    assert "6" not in (await load_free_agent_salaries(repo)).aav
    await unbind(repo, pending.key)  # nothing to undo: fine


async def test_confirming_an_unknown_row_is_refused() -> None:
    with pytest.raises(FreeAgentError, match=r"no imported row 'x\|F'"):
        await confirm(InMemoryRepository(), "x|F", "1")


async def test_two_rows_bound_to_one_player_give_no_salary_and_a_problem() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    await confirm(repo, "known nobody|F", "1")
    salaries = await load_free_agent_salaries(repo)
    assert "1" not in salaries.aav
    assert salaries.problems == ("player 1 is bound to 2 rows: ada knight|F, known nobody|F",)


async def test_an_override_wins_and_can_be_removed() -> None:
    repo = InMemoryRepository()
    await import_free_agent_salaries(repo, ROWS, POOL, NO_ALIASES)
    await set_aav_override(repo, "1", 3_000_000)
    await set_aav_override(repo, "77", 500_000)  # a player with no imported row
    assert (await load_free_agent_salaries(repo)).aav["1"] == 3_000_000
    assert (await load_free_agent_salaries(repo)).aav["77"] == 500_000
    await set_aav_override(repo, "1", None)
    assert (await load_free_agent_salaries(repo)).aav["1"] == 1_000_000


@pytest.mark.parametrize("bad", [-1, True, 1.5])
async def test_an_override_must_be_whole_dollars(bad: Any) -> None:
    with pytest.raises(FreeAgentError, match="whole number of dollars"):
        await set_aav_override(InMemoryRepository(), "1", bad)


async def test_nothing_imported_means_no_salaries_and_no_reviews() -> None:
    repo = InMemoryRepository()
    assert (await load_free_agent_salaries(repo)).aav == {}
    assert await pending_reviews(repo, POOL, NO_ALIASES) == []


async def test_end_to_end_from_csv_bytes() -> None:
    csv = 'Rank,Player,Picture,Age,Pos,Cap Hit\n1,"Knight,\xa0Ada",x,30,C,"$1,000,000 "\n'
    rows = parse_salary_csv(csv.encode("mac_roman"))
    repo = InMemoryRepository()
    report = await import_free_agent_salaries(repo, rows, POOL, NO_ALIASES)
    assert report.bound == 1
    assert (await load_free_agent_salaries(repo)).aav == {"1": 1_000_000}
