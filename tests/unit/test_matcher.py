"""The salary-row -> Yahoo-player cascade (SPEC §6) and its aliases."""

import json
import random
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from fha.domain.matcher import (
    NO_ALIASES,
    POOL_FUZZY,
    ROSTER_FUZZY,
    Aliases,
    Candidate,
    MatchResult,
    Query,
    Scope,
    Scored,
    Status,
    Step,
    match,
)
from fha.domain.names import name_keys, normalize_name

SEED = json.loads((Path(__file__).parents[1] / "fixtures" / "alias_seed.json").read_text())
SEED_ALIASES = Aliases.from_seed(SEED)


def c(pid: str, name: str, team: str | None = None, pos: str | None = None) -> Candidate:
    return Candidate(pid, name, team, pos)


MCDAVID = c("1", "Connor McDavid", "Edm", "C")
DRAISAITL = c("2", "Leon Draisaitl", "Edm", "C,LW")
AHO_C = c("3", "Sebastian Aho", "Car", "C")
AHO_D = c("4", "Sebastian Aho", "NYI", "D")
PETTERSSON_C = c("5", "Elias Pettersson", "Van", "C")
PETTERSSON_D = c("6", "Elias Pettersson", "Van", "D")
STUTZLE = c("7", "Tim Stutzle", "Ott", "C,LW")
POOL = [MCDAVID, DRAISAITL, AHO_C, AHO_D, PETTERSSON_C, PETTERSSON_D, STUTZLE]


def matched(result: MatchResult) -> Candidate:
    assert result.status is Status.MATCHED, result
    assert result.player is not None
    return result.player


# ---------------------------------------------------------------- the cascade, whole pool


def test_exact_name_and_team_is_step_one() -> None:
    result = match(Query("Connor McDavid", "EDM", "C"), POOL)
    assert (matched(result), result.step) == (MCDAVID, Step.EXACT)
    assert not result.by_alias
    assert not result.by_position
    assert result.candidates == ()


def test_team_codes_from_any_source_compare_equal() -> None:
    for team in ("EDM", "Edm", "Edmonton", "Oilers"):
        assert match(Query("Connor McDavid", team), POOL).step is Step.EXACT


def test_normalized_name_and_team_is_step_two() -> None:
    result = match(Query("Stützle, Tim", "OTT"), POOL)
    assert (matched(result), result.step) == (STUTZLE, Step.NORMALIZED)


def test_nicknames_match_at_step_two() -> None:
    pool = [c("9", "Matt Boldy", "Min", "LW")]
    result = match(Query("Matthew Boldy", "MIN"), pool)
    assert result.step is Step.NORMALIZED


def test_name_only_is_step_three_when_the_team_differs() -> None:
    result = match(Query("Leon Draisaitl", "TOR"), POOL)  # traded, or the sheet is stale
    assert (matched(result), result.step) == (DRAISAITL, Step.NAME)


@pytest.mark.parametrize("team", [None, "", "New York", "XYZ"])
def test_an_unknown_team_skips_steps_one_and_two(team: str | None) -> None:
    result = match(Query("Connor McDavid", team), POOL)
    assert (matched(result), result.step) == (MCDAVID, Step.NAME)


def test_exact_name_is_case_and_space_sensitive_only_up_to_whitespace() -> None:
    assert match(Query("Connor\N{NO-BREAK SPACE} McDavid ", "EDM"), POOL).step is Step.EXACT
    assert match(Query("connor mcdavid", "EDM"), POOL).step is Step.NORMALIZED


# ---------------------------------------------------------------- position breaks ties


def test_the_two_sebastian_ahos_resolve_by_position() -> None:
    d = match(Query("Aho, Sebastian", None, "D"), POOL)
    assert (matched(d), d.step, d.by_position) == (AHO_D, Step.NAME, True)
    forward = match(Query("Sebastian Aho", None, "C"), POOL)
    assert (matched(forward), forward.by_position) == (AHO_C, True)


def test_the_two_elias_petterssons_share_a_team_and_resolve_by_position() -> None:
    d = match(Query("Elias Pettersson", "VAN", "D"), POOL)
    assert (matched(d), d.step, d.by_position) == (PETTERSSON_D, Step.EXACT, True)
    wing = match(Query("Elias Pettersson", "VAN", "L"), POOL)  # PuckPedia's L is a forward
    assert matched(wing) == PETTERSSON_C


@pytest.mark.parametrize("pos", [None, "", "X", "C/D"])
def test_a_tie_without_a_usable_position_is_ambiguous(pos: str | None) -> None:
    result = match(Query("Sebastian Aho", None, pos), POOL)
    assert result.status is Status.AMBIGUOUS
    assert result.player is None
    assert {s.candidate for s in result.candidates} == {AHO_C, AHO_D}


def test_a_tie_position_cannot_break_is_ambiguous() -> None:
    pool = [c("a", "Jake Smith", "Bos", "C"), c("b", "Jake Smith", "Buf", "LW")]
    result = match(Query("Jake Smith", None, "RW"), pool)
    assert result.status is Status.AMBIGUOUS
    assert len(result.candidates) == 2


def test_with_no_team_a_position_mismatch_sends_a_unique_name_to_review() -> None:
    # Owner's decision (M3 round 2): a CSV row has no team, so its position group
    # is the only other evidence, and a mismatch mustn't bind silently.
    result = match(Query("Connor McDavid", None, "D"), POOL)
    assert result.status is Status.REVIEW
    assert result.candidates[0].candidate == MCDAVID
    assert result.candidates[0].score == 100.0


def test_with_the_team_matching_a_position_mismatch_still_matches() -> None:
    result = match(Query("Connor McDavid", "EDM", "D"), POOL)  # sources disagree on position
    assert (matched(result), result.step) == (MCDAVID, Step.EXACT)


def test_with_no_team_and_no_position_a_unique_name_matches() -> None:
    assert matched(match(Query("Connor McDavid"), POOL)) == MCDAVID
    assert matched(match(Query("Connor McDavid", None, "C"), POOL)) == MCDAVID


# ---------------------------------------------------------------- fuzzy: candidates only


def test_a_typo_is_a_candidate_for_review_never_a_match() -> None:
    result = match(Query("Leon Draisatl", "EDM"), POOL)
    assert result.status is Status.REVIEW
    assert result.player is None
    assert result.candidates[0].candidate == DRAISAITL
    assert result.candidates[0].score >= POOL_FUZZY


def test_review_and_unmatched_offer_the_top_three_best_first() -> None:
    result = match(Query("Connie McDavis"), POOL)
    assert result.status is Status.UNMATCHED
    assert len(result.candidates) == 3
    scores = [s.score for s in result.candidates]
    assert scores == sorted(scores, reverse=True)
    assert result.candidates[0].candidate == MCDAVID
    assert scores[0] < POOL_FUZZY


def test_fuzzy_ties_prefer_the_rows_position_group() -> None:
    result = match(Query("Sebastian Ahoo", None, "D"), POOL)
    assert result.status is Status.REVIEW
    assert [s.candidate for s in result.candidates[:2]] == [AHO_D, AHO_C]
    result = match(Query("Sebastian Ahoo", None, "C"), POOL)
    assert [s.candidate for s in result.candidates[:2]] == [AHO_C, AHO_D]


@pytest.mark.parametrize("scope", list(Scope))
def test_a_blank_name_scores_zero_against_everyone(scope: Scope) -> None:
    result = match(Query("  "), POOL[:2], scope=scope)
    assert result.status is Status.UNMATCHED
    assert [s.score for s in result.candidates] == [0.0, 0.0]


def test_a_nameless_candidate_scores_zero() -> None:
    result = match(Query("Connor McDavid"), [c("x", "")])
    assert result.candidates == (Scored(c("x", ""), 0.0),)


def test_an_empty_pool_is_unmatched_with_no_candidates() -> None:
    assert match(Query("Connor McDavid"), []) == MatchResult(Status.UNMATCHED)


def test_the_result_does_not_depend_on_pool_order() -> None:
    shuffled = POOL[::-1]
    for query in (
        Query("Sebastian Aho", None, "D"),
        Query("Sebastian Aho"),
        Query("Leon Draisatl"),
        Query("Connie McDavis"),
    ):
        assert match(query, POOL) == match(query, shuffled)


# ---------------------------------------------------------------- aliases (every seed is a test)


def same_person(a: dict[str, str], b: dict[str, str]) -> bool:
    names_a = {normalize_name(a["stats_name"]), normalize_name(a["salary_name"])}
    names_b = {normalize_name(b["stats_name"]), normalize_name(b["salary_name"])}
    return bool(names_a & names_b)


@pytest.mark.parametrize("seed", SEED, ids=[s["salary_name"] for s in SEED])
def test_every_seed_alias_matches_its_player(seed: dict[str, str]) -> None:
    """The seed's salary name finds its stats name among every other seeded player."""
    target = c("target", seed["stats_name"])
    others = [
        c(f"other{i}", s["stats_name"]) for i, s in enumerate(SEED) if not same_person(s, seed)
    ]
    random.Random(seed["salary_name"]).shuffle(others)  # noqa: S311 - test ordering, not crypto
    result = match(Query(seed["salary_name"]), [*others, target], aliases=SEED_ALIASES)
    assert matched(result) == target


# Seeds the normalizer and nickname groups can't resolve on their own.
NEEDS_AN_ALIAS = {
    "Janis Moser",  # J.J.
    "Mats Zuccarello-Aasen",
    "Emil Martinsen-Lilleberg",
}


@pytest.mark.parametrize("seed", SEED, ids=[s["salary_name"] for s in SEED])
def test_without_aliases_only_the_known_cases_fail(seed: dict[str, str]) -> None:
    result = match(Query(seed["salary_name"]), [c("target", seed["stats_name"])])
    if seed["salary_name"] in NEEDS_AN_ALIAS or seed["stats_name"] == "Dmitri Vornkov":
        assert result.status is not Status.MATCHED
    else:
        assert result.status is Status.MATCHED


def test_an_alias_is_flagged_and_replaces_the_rows_name() -> None:
    pool = [c("m", "J.J. Moser", "Tbl", "D"), c("x", "Janis Moser", "Ari", "D")]
    result = match(Query("Janis Moser", "TBL"), pool, aliases=SEED_ALIASES)
    assert (matched(result).player_id, result.by_alias, result.step) == ("m", True, Step.EXACT)
    plain = match(Query("Janis Moser", "TBL"), pool)
    assert (matched(plain).player_id, plain.by_alias) == ("x", False)


def test_a_salary_name_can_alias_to_several_stats_names() -> None:
    aliases = Aliases.from_seed(SEED)
    assert set(aliases.targets("Dimitri Voronkov")) == {"Dmitri Vornkov", "Dmitri Voronkov"}
    assert aliases.targets("dimitri   VORONKOV") == aliases.targets("Dimitri Voronkov")
    assert aliases.targets("Connor McDavid") == ()
    assert NO_ALIASES.targets("Dimitri Voronkov") == ()


def test_aliases_can_be_added_one_at_a_time() -> None:
    aliases = NO_ALIASES.with_alias(stats_name="Mitch Marner", salary_name="Mitchell Marner")
    assert aliases.targets("Mitchell Marner") == ("Mitch Marner",)
    assert NO_ALIASES.targets("Mitchell Marner") == ()  # immutable
    assert aliases.with_alias("Mitch Marner", "Mitchell Marner") == aliases


@pytest.mark.parametrize(
    "bad",
    [[{"stats_name": "A"}], [{"stats_name": "", "salary_name": "B"}], [{"x": 1}], ["A,B"]],
)
def test_a_malformed_seed_is_rejected(bad: list[object]) -> None:
    with pytest.raises(ValueError, match="alias"):
        Aliases.from_seed(bad)


# ---------------------------------------------------------------- team-scoped (league sheet rows)

ROSTER = [
    c("r1", "Frederik Andersen", "Car", "G"),
    c("r2", "Jake Oettinger", "Dal", "G"),
    c("r3", "Jack Hughes", "NJ", "C"),
    c("r4", "Quinn Hughes", "Van", "D"),
    c("r5", "Phillip Di Giuseppe", "Van", "LW"),
    c("r6", "Mitch Marner", "VGK", "RW"),
]


def test_a_unique_surname_in_the_roster_is_a_match() -> None:
    result = match(Query("Andersen"), ROSTER, scope=Scope.ROSTER)
    assert (matched(result).player_id, result.step) == ("r1", Step.SURNAME)
    result = match(Query("Di Giuseppe"), ROSTER, scope=Scope.ROSTER)
    assert matched(result).player_id == "r5"


@pytest.mark.parametrize(
    ("row", "player"),
    [
        ("Pageau", "Jean-Gabriel Pageau"),
        ("Dubois", "Pierre-Luc Dubois"),
        ("Vlasic", "Marc-Edouard Vlasic"),
    ],
)
def test_a_surname_matches_a_hyphenated_first_name(row: str, player: str) -> None:
    roster = [*ROSTER, c("h1", player, "Nyi", "C")]
    result = match(Query(row), roster, scope=Scope.ROSTER)
    assert (matched(result).player_id, result.step) == ("h1", Step.SURNAME)


def test_surname_only_is_not_a_match_in_the_whole_pool() -> None:
    result = match(Query("Andersen"), ROSTER, scope=Scope.POOL)
    assert result.status is not Status.MATCHED


def test_a_shared_surname_in_the_roster_is_broken_by_position_or_ambiguous() -> None:
    d = match(Query("Hughes", None, "D"), ROSTER, scope=Scope.ROSTER)
    assert (matched(d).player_id, d.step, d.by_position) == ("r4", Step.SURNAME, True)
    either = match(Query("Hughes"), ROSTER, scope=Scope.ROSTER)
    assert either.status is Status.AMBIGUOUS
    assert {s.candidate.player_id for s in either.candidates} == {"r3", "r4"}


def test_a_name_match_contradicted_by_team_and_position_is_only_a_candidate() -> None:
    pool = [c("p1", "Jonathan Smith", "Tor", "D")]
    result = match(Query("John Smith", "BOS", "C"), pool)
    assert result.status is Status.REVIEW
    assert result.candidates[0].candidate.player_id == "p1"


@pytest.mark.parametrize(
    ("team", "pos"),
    [("BOS", "D"), ("TOR", "C"), ("BOS", None)],  # (None, "C"): review, see McDavid
)
def test_one_contradiction_or_an_unknown_still_matches_by_name(
    team: str | None, pos: str | None
) -> None:
    pool = [c("p1", "Jonathan Smith", "Tor", "D")]
    assert matched(match(Query("John Smith", team, pos), pool)).player_id == "p1"


def test_the_contradiction_guard_applies_to_surnames_in_the_roster() -> None:
    result = match(Query("Andersen", "BOS", "C"), ROSTER, scope=Scope.ROSTER)
    assert result.status is Status.REVIEW


@pytest.mark.parametrize(
    ("team", "pos"),
    [("NJ", "D"), ("VAN", "C"), (None, "D"), ("NJ", None)],
)
def test_a_surname_match_contradicted_by_team_or_position_is_only_a_candidate(
    team: str | None, pos: str | None
) -> None:
    # Owner's decision (M3 round 2): a stale "Hughes / D / NJD" row mustn't bind
    # the roster's only Hughes (a C on another team) and hand him the salary.
    roster = [c("j", "Jack Hughes", "Van", "C"), c("o", "Other Guy", "Van", "D")]
    contradicts = (team is not None and team != "VAN") or (pos is not None and pos != "C")
    result = match(Query("Hughes", team, pos), roster, scope=Scope.ROSTER)
    if contradicts:
        assert result.status is Status.REVIEW
        assert result.candidates[0].candidate.player_id == "j"
    else:
        assert matched(result).player_id == "j"


def test_a_surname_that_fits_team_and_position_still_matches() -> None:
    result = match(Query("Andersen", "CAR", "G"), ROSTER, scope=Scope.ROSTER)
    assert (matched(result).player_id, result.step) == ("r1", Step.SURNAME)


def test_a_full_name_never_matches_on_surname_alone() -> None:
    result = match(Query("Luke Hughes", "NJ", "D"), ROSTER, scope=Scope.ROSTER)
    assert result.status is not Status.MATCHED


@pytest.mark.parametrize("typo", ["Oetterger", "Jake Oetterger"])
def test_a_roster_typo_is_a_candidate_for_review(typo: str) -> None:
    result = match(Query(typo), ROSTER, scope=Scope.ROSTER)
    assert result.status is Status.REVIEW
    assert result.candidates[0].candidate.player_id == "r2"
    assert result.candidates[0].score >= ROSTER_FUZZY


def test_the_roster_keeps_the_rest_of_the_cascade() -> None:
    assert match(Query("Marner, Mitchell", "Vegas"), ROSTER, scope=Scope.ROSTER).step is (
        Step.NORMALIZED
    )
    assert match(Query("Jake Oettinger", "Dallas"), ROSTER, scope=Scope.ROSTER).step is (Step.EXACT)


def test_roster_fuzzy_threshold_is_below_the_pools() -> None:
    assert ROSTER_FUZZY < POOL_FUZZY == 90


# ---------------------------------------------------------------- properties

WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=9)


@given(st.lists(st.tuples(WORD, WORD), min_size=1, max_size=12, unique=True), st.data())
def test_any_unique_name_in_the_pool_matches_itself(
    names: list[tuple[str, str]], data: st.DataObject
) -> None:
    pool = [c(str(i), f"{first.title()} {last.title()}") for i, (first, last) in enumerate(names)]
    target = data.draw(st.sampled_from(pool))
    mine = name_keys(target.name)
    assume(not any(p != target and mine & name_keys(p.name) for p in pool))
    shuffled = data.draw(st.permutations(pool))
    assert matched(match(Query(target.name), shuffled)) == target


@given(st.lists(st.tuples(WORD, WORD), min_size=1, max_size=8), WORD, WORD)
def test_fuzzy_never_matches(names: list[tuple[str, str]], first: str, last: str) -> None:
    pool = [c(str(i), f"{a} {b}") for i, (a, b) in enumerate(names)]
    result = match(Query(f"{first} {last}"), pool)
    if result.status is Status.MATCHED:
        assert result.step is not None
        assert result.player is not None
    else:
        assert result.player is None
        assert result.step is None
        assert len(result.candidates) <= max(3, len(pool))


def test_a_weak_match_offers_the_picked_player_first_then_the_closest_others() -> None:
    from rapidfuzz import fuzz

    roster = [
        c("j", "Jack Hughes", "Van", "C"),
        c("h", "Dan Hughs", "Van", "D"),  # surname close to the row's
        c("z", "Bob Zed", "Van", "D"),
        c("y", "Al Yu", "Van", "D"),
    ]
    result = match(Query("Hughes", "NJ", "C"), roster, scope=Scope.ROSTER)
    assert result.status is Status.REVIEW
    assert not result.by_alias
    ids = [s.candidate.player_id for s in result.candidates]
    assert (len(ids), ids[:2], len(set(ids))) == (3, ["j", "h"], 3)  # TOP, no repeats
    assert result.candidates[0].score == 100.0
    # Roster scope: scored against the surname, as fuzzy review would be.
    assert result.candidates[1].score == pytest.approx(fuzz.token_sort_ratio("hughes", "hughs"))
    assert result.candidates[1].score >= result.candidates[2].score


def test_a_weak_match_by_alias_says_so() -> None:
    aliases = Aliases().with_alias("Connor McDavid", "Con MacDavid")
    result = match(Query("Con MacDavid", None, "D"), POOL, aliases=aliases)
    assert result.status is Status.REVIEW
    assert result.by_alias
    assert result.candidates[0].candidate == MCDAVID


def test_a_position_tie_break_to_a_player_on_another_team_is_only_a_candidate() -> None:
    # Both Hugheses fit the guard (Jack: NJ but a C; Quinn: a D but VAN); position
    # picks Quinn, whose team then contradicts the row: only a candidate.
    result = match(Query("Hughes", "NJ", "D"), ROSTER, scope=Scope.ROSTER)
    assert result.status is Status.REVIEW
    assert result.candidates[0].candidate.player_id == "r4"


def test_review_candidates_after_the_pick_are_in_a_stable_order() -> None:
    tied = [
        c("j", "Jack Hughes", "Van", "C"),
        c("b", "Bo Zed", "Van", "D"),
        c("a", "Al Zed", "Van", "D"),
    ]
    for order in (tied, list(reversed(tied))):
        result = match(Query("Hughes", "NJ", "C"), order, scope=Scope.ROSTER)
        # Al Zed and Bo Zed score alike: name order breaks the tie, whatever came first.
        assert [s.candidate.player_id for s in result.candidates] == ["j", "a", "b"]


@pytest.mark.parametrize(("their_team", "weak"), [(None, True), ("Hamilton", True), ("TB", False)])
def test_a_position_mismatch_needs_the_team_to_confirm_a_name_match(
    their_team: str | None, weak: bool
) -> None:
    pool = [c("p", "Jonathan Smith", their_team, "D")]
    result = match(Query("John Smith", "TBL", "C"), pool)  # the row has a team, he may not
    assert (result.status is Status.REVIEW) is weak
