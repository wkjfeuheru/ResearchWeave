"""Real report isolation acceptance: no skipped backend or host fallback."""

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from openharness.config import Settings
from openharness.sandbox.policy import report_settings, report_environment, ExecutionOwner
from openharness.utils.shell import create_shell_subprocess, terminate_shell_process
from openharness.tools.base import ToolExecutionContext
from openharness.tools.bash_tool import BashTool, BashToolInput
from tests.test_research.test_dispatch_subagents import project as project


def _settings() -> Settings:
    """Permit testing a freshly built image without replacing a user's tags."""
    settings = Settings()
    if image := os.environ.get("OPENHARNESS_TEST_DOCKER_IMAGE"):
        settings.sandbox.docker.image = image
    return settings


async def run(workspace, command, *, backend="srt", key="test"):
    settings = report_settings(_settings(), workspace)
    settings.sandbox.backend = backend
    process = await create_shell_subprocess(
        command,
        cwd=workspace,
        settings=settings,
        env=report_environment(workspace),
        owner=ExecutionOwner("acceptance", "project", key, workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
        return process.returncode, output.decode()
    finally:
        await terminate_shell_process(process, force=True)
        if backend == "docker":
            from openharness.sandbox.session import stop_docker_sandbox

            await stop_docker_sandbox(f"acceptance:project:{key}")


async def test_actual_srt_exit_codes(project):
    root = project.resolve_workspace("P")
    for code in (0, 1, 7):
        rc, output = await run(root, f"exit {code}")
        assert rc == code, output


@pytest.mark.parametrize("backend", ["srt", "docker"])
async def test_actual_python_path_and_write_isolation(project, tmp_path, monkeypatch, backend):
    import os

    root = project.resolve_workspace("P")
    outside = tmp_path / "other-project"
    outside.mkdir()
    private = outside / "credential.txt"
    private.write_text("acceptance-secret")
    (root / "artifacts" / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "acceptance-env-secret")
    protected = [
        str(private),
        str(root / os.path.relpath(private, root)),
        str(root / "artifacts" / "escape" / "credential.txt"),
        str(project.store.path),
    ]
    assert Path(protected[1]).resolve() == private
    program = f"""import os,json
from pathlib import Path
failures=[]
for value in {protected!r}:
 try: Path(value).read_text(); failures.append(value)
 except OSError: pass
for value in {[str(root / "MEMORY.md"), str(project.store.path)]!r}:
 try: Path(value).write_text('overwrite'); failures.append(value)
 except OSError: pass
Path('reports/allowed.txt').write_text('allowed')
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'OPENHARNESS_RESEARCH_SESSION_DIR' not in os.environ
assert not failures,failures
print('ISOLATED')"""
    rc, output = await run(root, "python -c " + shlex.quote(program), backend=backend)
    assert rc == 0 and "ISOLATED" in output, output
    assert (root / "reports" / "allowed.txt").read_text() == "allowed"
    assert private.read_text() == "acceptance-secret"
    assert "overwrite" not in (root / "MEMORY.md").read_text()


async def test_report_backend_missing_fails_closed(project, monkeypatch):
    import openharness.sandbox.adapter as adapter

    original = adapter.shutil.which
    monkeypatch.setattr(
        adapter.shutil, "which", lambda name: None if name == "srt" else original(name)
    )
    ctx = ToolExecutionContext(
        cwd=project.resolve_workspace("P"),
        metadata={"research_runtime": project},
        settings=_settings(),
    )
    result = await BashTool().execute(BashToolInput(command="touch reports/host-fallback"), ctx)
    assert result.is_error and "not found" in result.output
    assert not (ctx.cwd / "reports" / "host-fallback").exists()


async def test_report_timeout_and_cancel_remove_descendants(project):
    ctx = ToolExecutionContext(
        cwd=project.resolve_workspace("P"),
        metadata={"research_runtime": project},
        settings=_settings(),
    )
    command = "(sleep 3; touch reports/leaked) & wait"
    result = await BashTool().execute(BashToolInput(command=command, timeout_seconds=1), ctx)
    assert result.metadata.get("timed_out"), result.output
    task = asyncio.create_task(BashTool().execute(BashToolInput(command=command), ctx))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(3)
    assert not (ctx.cwd / "reports" / "leaked").exists()


async def test_nonreport_host_compatibility(tmp_path):
    private = tmp_path / "legacy.txt"
    private.write_text("legacy")
    result = await BashTool().execute(
        BashToolInput(
            command=f"{shlex.quote(sys.executable)} -c "
            + shlex.quote(f"from pathlib import Path; print(Path({str(private)!r}).read_text())")
        ),
        ToolExecutionContext(cwd=tmp_path, settings=_settings()),
    )
    assert not result.is_error and "legacy" in result.output


@pytest.mark.parametrize("backend", ["srt", "docker"])
async def test_actual_backends_own_concurrent_project_mounts(tmp_path, backend):
    roots = [tmp_path / "A", tmp_path / "B"]
    for root in roots:
        root.mkdir()
        for directory in ("artifacts", "reports", "subagents"):
            (root / directory).mkdir()
        (root / "MEMORY.md").write_text(root.name)
    credential = tmp_path / "credential"
    credential.write_text("host-private")
    programs = []
    for index, root in enumerate(roots):
        other = roots[1 - index]
        code = f"""from pathlib import Path
import os
assert Path('MEMORY.md').read_text() == {root.name!r}
for value in {[str(credential), str(other / "MEMORY.md")]!r}:
 try: Path(value).read_text()
 except OSError: pass
 else: raise AssertionError(value)
try: Path('MEMORY.md').write_text('corrupt')
except OSError: pass
else: raise AssertionError('memory writable')
Path('reports/result').write_text('ok')
assert 'OPENHARNESS_RESEARCH_SESSION_DIR' not in os.environ
print('DOCKER_ISOLATED')"""
        programs.append(run(root, "python -c " + shlex.quote(code), backend=backend, key=root.name))
    outcomes = await asyncio.gather(*programs)
    assert all(code == 0 and "DOCKER_ISOLATED" in output for code, output in outcomes), outcomes
    assert [(root / "MEMORY.md").read_text() for root in roots] == ["A", "B"]


async def test_actual_docker_cancel_removes_only_owned_execution(project, tmp_path):
    from openharness.sandbox.session import (
        get_docker_sandbox,
        start_docker_sandbox,
        stop_docker_sandbox,
    )

    root = project.resolve_workspace("P")
    settings = report_settings(_settings(), root)
    settings.sandbox.backend = "docker"
    keeper = await start_docker_sandbox(
        settings, "keeper-runtime:project:execution", root, report=True
    )
    try:
        ctx = ToolExecutionContext(
            cwd=root,
            settings=settings,
            runtime_id="cancel-runtime",
            metadata={"research_runtime": project},
        )
        timed = await BashTool().execute(
            BashToolInput(command="sleep 3; touch reports/leak", timeout_seconds=1), ctx
        )
        assert timed.metadata.get("timed_out"), timed.output
        assert keeper.is_running
        task = asyncio.create_task(
            BashTool().execute(BashToolInput(command="sleep 3; touch reports/leak"), ctx)
        )
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert get_docker_sandbox("keeper-runtime:project:execution") is keeper
        assert keeper.is_running
        await asyncio.sleep(3)
        assert not (root / "reports" / "leak").exists()
    finally:
        await stop_docker_sandbox("keeper-runtime:project:execution")


@pytest.mark.parametrize("backend", ["srt", "docker"])
async def test_actual_isolated_exports_registered_only_by_host(project, backend):
    from openharness.utils.session_files import SessionFiles

    root = project.resolve_workspace("P")
    settings = _settings()
    settings.sandbox.backend = backend
    execution = project.repository.begin_execution("bash", f"export-{backend}")
    ctx = ToolExecutionContext(
        cwd=root,
        settings=settings,
        runtime_id="export-acceptance",
        metadata={"research_runtime": project, "research_execution": execution},
    )
    # Reuse the existing shared exporter; the child has no writable Store mount.
    program = f"""import json
from pathlib import Path
from pydantic import BaseModel
from openharness.utils.research_exports import export_result
class Candidate(BaseModel):
 kind: str = 'sandbox-candidate'
 status: str = 'partial'
 gaps: list[str] = ['Main Agent must verify evidence']
 metrics: dict[str,int] = {{'value':42}}
packet=export_result(Candidate(),Path('reports'),Path({str(project.store.directory)!r}),
 'parent',render_markdown=lambda data,session:'# candidate',sheets=('metrics',))
assert packet['artifacts'] == [], 'sandbox must not write the host manifest'
print(json.dumps(packet))"""
    result = await BashTool().execute(
        BashToolInput(command="python -c " + shlex.quote(program)), ctx
    )
    assert not result.is_error, result.output
    exports = result.metadata["exported_files"]
    assert len(exports) == 4
    files = SessionFiles(project.store.directory)
    assert all(record["execution_id"] == execution["id"] for record in files.list("artifacts"))
    assert all(record["status"] == "pending_execution" for record in files.list("artifacts"))
    project.repository.commit_execution(execution["id"], [])
    assert all(not record["stale"] for record in files.list("artifacts"))
    assert not project.store.load().artifacts  # Downloads are candidates, not research acceptance.


async def test_export_registration_rejects_revoked_lease_and_link(project, tmp_path):
    from openharness.research.errors import ResearchError
    from openharness.tools.bash_tool import _register_exports
    from openharness.utils.session_files import SessionFiles
    import json

    root = project.resolve_workspace("P")
    execution = project.repository.begin_execution("bash", "revoked-export")
    ctx = ToolExecutionContext(cwd=root, metadata={"research_runtime": project})
    candidate = root / "reports/candidate.md"
    candidate.write_text("retained candidate")
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    (root / "reports/escape.md").symlink_to(outside)
    with pytest.raises(ResearchError):
        _register_exports(
            bytearray(json.dumps({"files": ["reports/escape.md"]}).encode()),
            ctx,
            project,
            execution,
        )
    await project.submit_feedback("P", "change direction before import")
    with pytest.raises(ResearchError, match="revoked"):
        _register_exports(
            bytearray(json.dumps({"files": ["reports/candidate.md"]}).encode()),
            ctx,
            project,
            execution,
        )
    assert not SessionFiles(project.store.directory).list("artifacts")


@pytest.mark.parametrize("backend", ["srt", "docker"])
async def test_report_rejects_preexisting_cross_project_hardlinks(project, tmp_path, backend):
    import os

    root = project.resolve_workspace("P")
    private = tmp_path / "other-project-secret"
    private.write_text("private")
    os.link(private, root / "artifacts/hardlink")
    settings = Settings()
    settings.sandbox.backend = backend
    result = await BashTool().execute(
        BashToolInput(
            command="python -c \"from pathlib import Path; Path('artifacts/hardlink').write_text('corrupt')\""
        ),
        ToolExecutionContext(cwd=root, settings=settings, metadata={"research_runtime": project}),
    )
    assert result.is_error and "hardlink" in result.output.lower()
    assert private.read_text() == "private"
