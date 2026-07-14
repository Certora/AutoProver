"""Unit tests for the shared local-command runner (``run_local_command``).

Needs neither a Rust wheel nor Postgres/LLM: ``run_local_command`` shells out to
trivial system binaries. Covers file materialization, path confinement, and the
error/timeout paths. (It still backs the trusted Python build steps — e.g. the sBPF
build; the Rust backend's own toolchain runs now go through ``run-confined`` in the
wheel, see ``docs/rust-backend-api.md``.)
"""

import pytest

from composer.sandbox.command import (
    NOT_FOUND_EXIT,
    UnsafePath,
    run_local_command,
)


@pytest.mark.asyncio
async def test_run_local_command_materializes_files_and_captures_output(tmp_path):
    res = await run_local_command(
        "printf", ["%s", "hello"], {"note.txt": "hi", "sub/deep.txt": "deep"}, workdir=tmp_path
    )
    assert res.exit_code == 0
    assert res.stdout == "hello"
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
