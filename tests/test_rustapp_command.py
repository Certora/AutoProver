"""Unit tests for the ``RunCommand`` effect and the shared local-command runner.

These need neither a Rust wheel nor Postgres/LLM: a tiny fake session drives the
IoC loop (``drive_session``), and ``run_local_command`` shells out to trivial
system binaries. They cover the effect round-trip, path confinement, and the
error/timeout paths.
"""

from __future__ import annotations

import json

import pytest

from composer.rustapp.command import (
    NOT_FOUND_EXIT,
    UnsafePath,
    run_local_command,
)
from composer.rustapp.loop import RustFormalized, drive_session


class _RunThenPublish:
    """A minimal decider: ``start`` → one ``RunCommand`` (writing a couple of
    files, running ``printf``), then ``command_result`` → ``publish`` echoing the
    observed stdout/exit into the result. Anything else → ``give_up``."""

    def resume(self, observation_json: str) -> str:
        obs = json.loads(observation_json)
        kind = obs["kind"]
        if kind == "start":
            return json.dumps(
                {
                    "kind": "run_command",
                    "program": "printf",
                    "args": ["%s", "hello"],
                    "files": {"note.txt": "hi", "sub/deep.txt": "deep"},
                }
            )
        if kind == "command_result":
            return json.dumps(
                {
                    "kind": "publish",
                    "result": {
                        "artifact_text": obs["stdout"],
                        "commentary": f"exit={obs['exit_code']}",
                    },
                }
            )
        return json.dumps({"kind": "give_up", "reason": f"unexpected {kind}"})


class _CmdEffects:
    """Effects that only implement ``run_command`` (all the fake session uses),
    delegating to the real :func:`run_local_command` against ``workdir``."""

    def __init__(self, workdir):
        self._workdir = workdir

    async def run_command(self, program, args, files):
        res = await run_local_command(program, args, files, workdir=self._workdir)
        return res.as_observation()


@pytest.mark.asyncio
async def test_run_command_effect_roundtrip(tmp_path):
    result = await drive_session(_RunThenPublish(), _CmdEffects(tmp_path))
    assert isinstance(result, RustFormalized)
    # stdout from `printf %s hello` flowed back to the decider and into the result.
    assert result.data["artifact_text"] == "hello"
    assert result.data["commentary"] == "exit=0"
    # files (incl. a nested path) were materialized into the workdir.
    assert (tmp_path / "note.txt").read_text() == "hi"
    assert (tmp_path / "sub" / "deep.txt").read_text() == "deep"


@pytest.mark.asyncio
async def test_run_local_command_missing_binary(tmp_path):
    res = await run_local_command("autoprover-no-such-binary-xyz", [], {}, workdir=tmp_path)
    assert res.exit_code == NOT_FOUND_EXIT
    assert "not found" in res.stderr


@pytest.mark.asyncio
async def test_run_local_command_nonzero_exit(tmp_path):
    res = await run_local_command("false", [], {}, workdir=tmp_path)
    assert res.exit_code != 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../evil.txt", "/etc/evil", "a/../../evil"])
async def test_run_local_command_rejects_path_escape(tmp_path, bad):
    with pytest.raises(UnsafePath):
        await run_local_command("true", [], {bad: "x"}, workdir=tmp_path)


@pytest.mark.asyncio
async def test_run_local_command_no_shell_injection(tmp_path):
    # Args are argv, never a shell string: a shell metacharacter is inert. `printf`
    # emits it literally rather than a subshell running `id`.
    res = await run_local_command("printf", ["%s", "$(id)"], {}, workdir=tmp_path)
    assert res.exit_code == 0
    assert res.stdout == "$(id)"


@pytest.mark.asyncio
async def test_run_local_command_timeout(tmp_path):
    res = await run_local_command("sleep", ["5"], {}, workdir=tmp_path, timeout_s=1)
    assert res.exit_code == -1
    assert "timed out" in res.stderr
