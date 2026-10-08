"""Resuming an experiment does not repeat saved failures or model calls."""

from typer.testing import CliRunner

from openharness.cli import app
from openharness.evaluation.dataset import dataset_version, load_cases
from openharness.evaluation.models import RunArtifact
from openharness.evaluation.observer import timestamp
from openharness.evaluation.runner import write_artifact


def test_resume_skips_saved_cases_and_refuses_changed_profile(tmp_path, monkeypatch):
    from openharness.evaluation import runner

    calls = []

    class ControlledRunner:
        def __init__(self, cases, directory, output, **kwargs):
            self.output = output
            self.version = dataset_version(cases)
            self.version_info = {"commit": "stable", "working_tree_hash": "stable"}
            output.mkdir(parents=True, exist_ok=True)

        async def run_case(self, case, *, repetition, run_name):
            calls.append(case.id)
            artifact = RunArtifact(
                run_id=run_name,
                case_id=case.id,
                repetition=repetition,
                dataset_version=self.version,
                trace_id="0" * 32,
                started_at=timestamp(),
                status="failed",
                error="retained failure",
            )
            write_artifact(
                self.output / "results" / f"{case.id}-r{repetition}-{run_name}.json",
                artifact,
            )
            return artifact

    monkeypatch.setattr(runner, "ExperimentRunner", ControlledRunner)
    args = [
        "eval",
        "run",
        "--profile",
        "agent",
        "--rules-only",
        "--case-id",
        load_cases()[0].id,
        "--output",
        str(tmp_path),
    ]
    cli = CliRunner()
    first = cli.invoke(app, args)
    assert first.exit_code == 0, first.output
    destination = next(tmp_path.iterdir())
    resumed = cli.invoke(app, [*args, "--resume", str(destination)])
    assert resumed.exit_code == 0 and len(calls) == 1
    changed = list(args)
    changed[3] = "changed-agent"
    assert cli.invoke(app, [*changed, "--resume", str(destination)]).exit_code == 1
    assert len(calls) == 1
