"""Explicit evaluation commands: no instrumentation or network activity on import."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import typer

from openharness.evaluation.dataset import DEFAULT_DATASET, load_cases, validate_dataset

app = typer.Typer(name="eval", help="运行投研 Agent 四维评测和 Langfuse 实验")


@app.command("validate")
def validate(dataset: Path = typer.Option(DEFAULT_DATASET)):
    result = validate_dataset(dataset)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["valid"]:
        raise typer.Exit(1)


@app.command("sync")
def sync(dataset: Path | None = typer.Option(None), results: Path | None = typer.Option(None)):
    from openharness.evaluation.langfuse_backend import (
        connect,
        import_sdk_experiments,
        sync_dataset,
    )
    from openharness.evaluation.report import read_results
    from openharness.evaluation.runner import write_artifact

    try:
        dataset = dataset or (results / "dataset" if results else DEFAULT_DATASET)
        checked = validate_dataset(dataset)
        if not checked["valid"]:
            raise ValueError("请先修复数据集静态校验错误")
        client = connect()
        try:
            name = sync_dataset(client, load_cases(dataset), version_id=checked["version"])
            if results:

                def persist(artifact):
                    write_artifact(
                        results
                        / "results"
                        / f"{artifact.case_id}-r{artifact.repetition}-{artifact.run_id}.json",
                        artifact,
                    )

                states = import_sdk_experiments(
                    client,
                    read_results(results),
                    load_cases(dataset),
                    version_id=checked["version"],
                    persist=persist,
                )
                typer.echo("补传状态：" + json.dumps(states, ensure_ascii=False))
            typer.echo(f"数据集已同步：{name}")
        finally:
            client.shutdown()
    except Exception:
        typer.echo(
            "Langfuse 同步失败；请检查 eval 依赖、环境配置和数据集。本地结果已保留。", err=True
        )
        raise typer.Exit(1) from None


def select_cases(cases, *, limit=None, ids=(), environment=None, split=None, representative=False):
    cases = [
        c
        for c in cases
        if (not ids or c.id in ids)
        and (not environment or c.environment == environment)
        and (not split or c.split == split)
    ]
    if representative:
        chosen = []
        for category in ("financial", "events", "digest", "deep", "cross"):
            group = [c for c in cases if c.category == category]
            for material, difficulty in (
                ("synthetic", "basic"),
                ("snapshot", "intermediate"),
                ("live", "complex"),
                ("synthetic", "complex"),
            ):
                candidates = [
                    c for c in group if c.material == material and c.difficulty == difficulty
                ]
                if candidates:
                    chosen.append(candidates[0])
        cases = chosen
    return cases[:limit] if limit else cases


@app.command("run")
def run(
    profile: str = typer.Option(...),
    judge_profile: str | None = typer.Option(None),
    dataset: Path = typer.Option(DEFAULT_DATASET),
    output: Path = typer.Option(Path(".openharness/evaluations")),
    limit: int | None = typer.Option(None, min=1),
    case_id: list[str] | None = typer.Option(None, "--case-id"),
    environment: str | None = typer.Option(None),
    split: str | None = typer.Option(None),
    repetitions: int = typer.Option(1, min=1),
    representative: bool = typer.Option(False),
    langfuse: bool = typer.Option(False, "--langfuse/--local"),
    rules_only: bool = typer.Option(False),
    context_window_tokens: int | None = typer.Option(None, min=1024),
    judge_context_window_tokens: int | None = typer.Option(None, min=1024),
    resume: Path | None = typer.Option(None),
):
    from openharness.evaluation.runner import ExperimentRunner
    from openharness.evaluation.report import write_report

    if not judge_profile and not rules_only:
        raise typer.BadParameter("请指定 --judge-profile，或显式使用 --rules-only 保留语义项未判定")
    checked = validate_dataset(dataset)
    if not checked["valid"]:
        raise typer.BadParameter("数据集校验失败，请运行 oh eval validate")
    all_cases = load_cases(dataset)
    selected = select_cases(
        all_cases,
        limit=limit,
        ids=case_id or (),
        environment=environment,
        split=split,
        representative=representative,
    )
    if not selected:
        raise typer.BadParameter("没有符合筛选条件的任务")
    experiment = "research-" + uuid4().hex[:12]
    destination = resume or output / experiment
    try:
        runner = ExperimentRunner(
            all_cases,
            dataset,
            destination,
            profile=profile,
            judge_profile=judge_profile if not rules_only else None,
            context_window_tokens=context_window_tokens,
            judge_context_window_tokens=judge_context_window_tokens,
        )
        from openharness.evaluation.report import read_results
        from openharness.utils.fs import atomic_write_text

        artifacts = read_results(destination) if resume else []
        manifest_path = destination / "experiment.json"
        manifest = {
            "name": experiment,
            "dataset_version": runner.version,
            "code_version": runner.version_info,
            "profile": profile,
            "judge_profile": judge_profile if not rules_only else None,
            "cases": [c.id for c in selected],
            "repetitions": repetitions,
            "context_window_tokens": context_window_tokens,
            "judge_context_window_tokens": judge_context_window_tokens,
        }
        if resume:
            existing = json.loads(manifest_path.read_text())
            if any(existing.get(key) != value for key, value in manifest.items() if key != "name"):
                raise ValueError("恢复时数据集、模型、任务筛选和预算配置必须与原实验一致")
            experiment = existing["name"]
        else:
            atomic_write_text(
                manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2), mode=0o600
            )
        done = {(a.case_id, a.repetition) for a in artifacts}
        if langfuse:
            from openharness.evaluation.langfuse_backend import connect, run_sdk_experiment

            client = connect()
            try:
                for repetition in range(1, repetitions + 1):
                    remaining = [c for c in selected if (c.id, repetition) not in done]
                    if not remaining:
                        continue
                    _, batch = run_sdk_experiment(
                        client,
                        runner,
                        remaining,
                        name=f"{experiment}-r{repetition}",
                        repetition=repetition,
                    )
                    artifacts.extend(batch)
            finally:
                client.shutdown()
        else:

            async def execute():
                for repetition in range(1, repetitions + 1):
                    for case in selected:
                        if (case.id, repetition) in done:
                            continue
                        typer.echo(f"运行 {case.id}；重复 {repetition}")
                        artifact = await runner.run_case(
                            case, repetition=repetition, run_name=f"{experiment}-r{repetition}"
                        )
                        artifacts.append(artifact)
                        write_report(destination, artifacts)
                        typer.echo(
                            f"{case.id}: {artifact.status}；语义评分 "
                            + ("已完成" if artifact.judge_results else "未判定")
                        )

            asyncio.run(execute())
        write_report(destination, artifacts)
        typer.echo(f"结果已保存：{destination.resolve()}")
    except Exception:
        typer.echo("评测启动失败，请检查依赖、profile 和连接配置。本地结果已保留。", err=True)
        raise typer.Exit(1) from None


@app.command("score")
def score(
    results: Path = typer.Argument(...),
    judge_profile: str = typer.Option(...),
    dataset: Path | None = typer.Option(None),
    judge_context_window_tokens: int | None = typer.Option(None, min=1024),
):
    from openharness.evaluation.judge import judge_case, JudgeOutputError
    from openharness.evaluation.report import read_results, write_report
    from openharness.evaluation.runner import resolve_profile, write_artifact
    from openharness.evaluation.scoring import score_case
    from openharness.runtime import _resolve_api_client_from_settings

    dataset = dataset or results / "dataset"
    cases = {c.id: c for c in load_cases(dataset)}
    artifacts = read_results(results)
    settings = resolve_profile(judge_profile)
    settings.timeout = max(120, settings.timeout)
    from openharness.evaluation.dataset import directory_version

    if any(
        a.dataset_version != directory_version(dataset, list(cases.values())) for a in artifacts
    ):
        raise typer.BadParameter("重新评分必须使用执行时的数据集版本")
    if judge_context_window_tokens:
        settings.context_window_tokens = judge_context_window_tokens
    if any(a.provenance.get("model") == settings.model for a in artifacts):
        raise typer.BadParameter("裁判必须与被测模型不同")

    async def execute():
        from openharness.evaluation.models import RunArtifact
        from openharness.evaluation.observer import RecordingObserver
        from openharness.utils.redaction import evaluation_credentials

        cleaner = RecordingObserver(
            secrets=[settings.resolve_auth().value, *evaluation_credentials()]
        )
        for index, artifact in enumerate(artifacts):
            client = None
            try:
                client = _resolve_api_client_from_settings(settings)
                judgment = await asyncio.wait_for(
                    judge_case(
                        cases[artifact.case_id],
                        artifact,
                        client,
                        settings.model,
                        context_window_tokens=settings.context_window_tokens,
                    ),
                    timeout=300,
                )
                artifact.judge_results = judgment.model_dump()
                artifact.scores = score_case(cases[artifact.case_id], artifact, judgment)
                artifact.provenance["judge_profile"] = judge_profile
                artifact.provenance.pop("judge_error", None)
            except Exception as exc:
                artifact.judge_results = None
                reason = str(exc) if isinstance(exc, JudgeOutputError) else type(exc).__name__
                artifact.provenance["judge_error"] = f"裁判未判定：{reason}"
                artifact.scores = score_case(cases[artifact.case_id], artifact)
            finally:
                if client:
                    try:
                        await client.close()
                    except Exception:
                        artifact.provenance["judge_close_error"] = "裁判连接关闭失败"
            artifact = RunArtifact.model_validate(cleaner.clean(artifact.model_dump()))
            artifacts[index] = artifact
            write_artifact(
                results
                / "results"
                / f"{artifact.case_id}-r{artifact.repetition}-{artifact.run_id}.json",
                artifact,
            )

    asyncio.run(execute())
    write_report(results, artifacts)
    typer.echo("已重新评分；未重跑 Agent。")


@app.command("report")
def report(results: Path = typer.Argument(...), compare: Path | None = typer.Option(None)):
    from openharness.evaluation.report import compare_runs, read_results, write_report

    artifacts = read_results(results)
    summary = write_report(results, artifacts)
    if compare:
        paired = compare_runs(artifacts, read_results(compare))
        (results / "comparison.json").write_text(json.dumps(paired, ensure_ascii=False, indent=2))
    typer.echo(f"报告已生成：{results.resolve()}；执行记录 {summary['task_count']} 条")
