"""LocalJsonRepository (dev only) against the shared contract, plus its file handling."""

import json
import os
import stat
from pathlib import Path

import pytest

from fha.storage.local_json import LocalJsonRepository
from fha.storage.repository import Repository, RepositoryError
from tests.unit.storage.contract import RepositoryContract


class TestLocalJsonRepository(RepositoryContract):
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Repository:
        return LocalJsonRepository(tmp_path / "private" / "repo.json", environ={})


async def test_state_survives_a_new_instance(tmp_path: Path) -> None:
    path = tmp_path / "repo.json"
    await LocalJsonRepository(path, environ={}).put("things", "a", {"v": 1, "f": 2.0})
    again = LocalJsonRepository(path, environ={})
    assert await again.get("things", "a") == {"v": 1, "f": 2.0}
    assert type((await again.all("things"))["a"]["f"]) is float


async def test_the_file_is_owner_only_and_replaced_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "repo.json"
    tmp = tmp_path / "repo.json.tmp"
    tmp.write_text("stale")  # a leftover from an interrupted write, readable by others
    tmp.chmod(0o644)
    modes: list[int] = []
    real_dump = json.dump

    def dump(obj: object, f: object, **kw: object) -> None:
        modes.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))  # type: ignore[attr-defined]
        real_dump(obj, f, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(json, "dump", dump)
    await LocalJsonRepository(path, environ={}).put("things", "a", {"v": 1})
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not tmp.exists()
    assert json.loads(path.read_text()) == {"things": {"a": {"v": 1}}}


async def test_a_failed_write_leaves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "repo.json"
    repo = LocalJsonRepository(path, environ={})
    await repo.put("things", "a", {"v": 1})

    def broken_dump(*args: object, **kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", broken_dump)
    with pytest.raises(OSError, match="disk full"):
        await repo.put("things", "b", {"v": 2})
    monkeypatch.undo()
    assert await repo.all("things") == {"a": {"v": 1}}


async def test_deleting_a_missing_document_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "repo.json"
    await LocalJsonRepository(path, environ={}).delete("things", "a")
    assert not path.exists()


def test_refuses_to_run_on_vercel(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="dev only, not on Vercel"):
        LocalJsonRepository(tmp_path / "repo.json", environ={"VERCEL": "1"})


@pytest.mark.parametrize(
    ("content", "message"),
    [("{not json", "is not valid JSON"), ("[1]", "not a repository file"), ('{"c": 1}', "not a")],
)
async def test_a_corrupt_file_is_an_error(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "repo.json"
    path.write_text(content)
    with pytest.raises(RepositoryError, match=message):
        await LocalJsonRepository(path, environ={}).get("things", "a")
