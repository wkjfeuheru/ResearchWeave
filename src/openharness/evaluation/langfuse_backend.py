"""Explicit Langfuse v4 connection, versioned datasets and safe local-result import."""

from __future__ import annotations

import os
import asyncio
import json
from pathlib import Path
from importlib.metadata import version
from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import urlsplit

from openharness.evaluation.dataset import dataset_version
from openharness.evaluation.observer import RecordingObserver


class LangfuseConfigurationError(ValueError):
    pass


def langfuse_usage(usage):
    """SDK flat usage buckets must be exclusive; local provider counts are inclusive."""
    if not usage or not usage.get("reported"):
        return None
    read, write = usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens")
    uncached = usage["input_tokens"] - (read or 0) - (write or 0)
    if uncached < 0:
        return None
    buckets = {"input": uncached, "output": usage["output_tokens"]}
    if read is not None:
        buckets["input_cached_tokens"] = read
    if write is not None:
        buckets["input_cache_creation_tokens"] = write
    return buckets


def expand_blobs(value, blobs):
    if isinstance(value, dict):
        if set(value) == {"$blob"}:
            return blobs[value["$blob"]]
        return {key: expand_blobs(item, blobs) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_blobs(item, blobs) for item in value]
    return value


def connection_config(path=None):
    path = Path(path or ".openharness/evaluation.local.json")
    config = json.loads(path.read_text()).get("langfuse", {}) if path.is_file() else {}
    return {
        "base_url": os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or config.get("base_url"),
        "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY") or config.get("public_key"),
        "secret_key": os.environ.get("LANGFUSE_SECRET_KEY") or config.get("secret_key"),
    }


def connect():
    config = connection_config()
    url, public, secret = (config[k] for k in ("base_url", "public_key", "secret_key"))
    if not all(isinstance(value, str) and value.strip() for value in (url, public, secret)):
        raise LangfuseConfigurationError(
            "请设置 LANGFUSE_BASE_URL、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY"
        )
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise LangfuseConfigurationError("Langfuse base_url 必须是无内嵌凭据的 HTTP(S) 地址")
    from langfuse import Langfuse

    if version("langfuse").split(".")[0] != "4":
        raise LangfuseConfigurationError("需要 Langfuse Python SDK v4")
    client = Langfuse(
        base_url=url,
        public_key=public,
        secret_key=secret,
        timeout=15,
        blocked_instrumentation_scopes=["openai", "httpx", "anthropic"],
        should_export_span=lambda span: span.instrumentation_scope.name == "langfuse-sdk",
        environment="openharness-evaluation",
    )
    try:
        if not client.auth_check():
            raise LangfuseConfigurationError("Langfuse 鉴权检查失败")
        health = client.api.health.health()
        if health.status.upper() != "OK" or int(health.version.split(".")[0]) < 4:
            raise LangfuseConfigurationError("实验数据模型需要 Langfuse v4 服务")
        # Checking dataset and observation methods prevents using the retired SDK trace API.
        if not all(
            hasattr(client, method)
            for method in ("run_experiment", "start_as_current_observation", "create_dataset_item")
        ):
            raise LangfuseConfigurationError("Langfuse SDK 不支持所需 v4 接口")
        return client
    except Exception:
        client.shutdown()
        raise LangfuseConfigurationError("Langfuse 连接或兼容性检查失败，请检查服务配置") from None


class LangfuseObserver(RecordingObserver):
    def __init__(self, client, **kwargs):
        config = connection_config()
        kwargs["secrets"] = [
            *kwargs.get("secrets", ()),
            config.get("public_key"),
            config.get("secret_key"),
        ]
        super().__init__(**kwargs)
        self.client = client
        self.export_failed = False
        self.trace_id = None

    @contextmanager
    def span(self, name, *, kind="span", input=None, metadata=None):
        with super().span(name, kind=kind, input=input, metadata=metadata) as local:
            remote_context = None
            remote = None
            try:
                remote_context = self.client.start_as_current_observation(
                    name=name,
                    as_type=kind if kind in {"generation", "tool", "agent"} else "span",
                    input=expand_blobs(self.clean(input), self.blobs),
                    metadata=self.clean(metadata or {}),
                    model=(metadata or {}).get("model") if kind == "generation" else None,
                )
                remote = remote_context.__enter__()
                self.trace_id = self.client.get_current_trace_id()
            except Exception:
                self.export_failed = True
            try:
                yield local
                if local.observation.status == "running":
                    local.observation.status = "ok"
            except BaseException as exc:
                local.observation.status = (
                    "cancelled"
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    else "error"
                )
                raise
            finally:
                if remote is not None:
                    try:
                        values = {
                            "output": local.observation.output,
                            "metadata": {
                                **local.observation.metadata,
                                "execution_status": local.observation.status,
                                "recorded_observation_id": local.observation.id,
                            },
                        }
                        if kind == "generation" and langfuse_usage(local.observation.usage):
                            values["usage_details"] = langfuse_usage(local.observation.usage)
                        try:
                            remote.update(**values)
                        finally:
                            remote_context.__exit__(None, None, None)
                    except Exception:
                        self.export_failed = True


def sync_dataset(client, cases, *, version_id=None):
    identifier = version_id or dataset_version(cases)
    name = "openharness/" + identifier
    client.create_dataset(
        name=name,
        description="200 条投研 Agent 四维评测；固定与联网结果分组",
        metadata={"version": identifier, "count": len(cases)},
    )
    for case in cases:
        client.create_dataset_item(
            dataset_name=name,
            id=str(uuid5(NAMESPACE_URL, name + "/" + case.id)),
            input=case.agent_input(),
            expected_output={
                "requirements": [r.model_dump() for r in case.requirements],
                "reference_facts": case.reference_facts,
                "path": case.path.model_dump(),
            },
            metadata={
                "case_id": case.id,
                "split": case.split,
                "category": case.category,
                "difficulty": case.difficulty,
                "environment": case.environment,
                "family": case.family,
                "review_status": case.review_status,
            },
        )
    return name


def export_artifact(client, artifact, *, trace_id=None, scores=True):
    """Import an already completed run without invoking the agent or judge again."""
    spans = {}
    target_id = trace_id or artifact.trace_id
    try:
        with client.start_as_current_observation(
            name="imported_agent_task",
            as_type="agent",
            trace_context={"trace_id": target_id},
            input={"case_id": artifact.case_id},
            output={"answer": artifact.answer},
            metadata={
                **artifact.provenance,
                "run_id": artifact.run_id,
                "recorded_elapsed_ms": artifact.elapsed_ms,
                "recorded_started_at": artifact.started_at,
                "imported": True,
                "status": artifact.status,
            },
        ) as root:
            for observation in artifact.observations:
                parent = spans.get(observation.parent_id, root)
                values = {
                    "name": observation.name,
                    "as_type": observation.kind
                    if observation.kind in {"agent", "tool", "generation"}
                    else "span",
                    "input": expand_blobs(observation.input, artifact.trace_blobs),
                    "output": observation.output,
                    "metadata": {
                        **observation.metadata,
                        "recorded_duration_ms": observation.duration_ms,
                        "recorded_started_at": observation.started_at,
                        "execution_status": observation.status,
                        "recorded_observation_id": observation.id,
                    },
                }
                if observation.kind == "generation":
                    values["model"] = observation.metadata.get("model")
                    if observation.usage and observation.usage.get("reported"):
                        values["usage_details"] = langfuse_usage(observation.usage)
                spans[observation.id] = parent.start_observation(**values)
            for observation in reversed(artifact.observations):
                spans[observation.id].end()
            for score in artifact.scores if scores else []:
                if score.value is not None:
                    client.create_score(
                        name=score.name,
                        value=score.value,
                        trace_id=target_id,
                        score_id=str(uuid5(NAMESPACE_URL, target_id + score.name)),
                        comment=score.explanation,
                        metadata={
                            "source": score.source,
                            "numerator": score.numerator,
                            "denominator": score.denominator,
                        },
                    )
        client.flush()
        # SDK flush is not a server acknowledgement. Confirm observations and scores by readback.
        artifact.upload_status = "pending"
        try:
            trace = client.api.trace.get(target_id)
            expected = {
                str(uuid5(NAMESPACE_URL, target_id + s.name))
                for s in artifact.scores
                if s.value is not None
            }
            if (
                scores
                and len(trace.observations or []) >= len(artifact.observations) + 1
                and expected <= {s.id for s in trace.scores or []}
            ):
                artifact.upload_status = "uploaded"
        except Exception:
            pass  # Eventual consistency is explicitly pending, never claimed as confirmed.
    except Exception:
        artifact.upload_status = "failed"
        raise LangfuseConfigurationError("结果上传未完成，本地结果已保留，可以单独补传") from None


def import_sdk_experiments(client, artifacts, cases, *, version_id, persist):
    """Replay saved outputs into SDK experiments, with no model or agent execution.

    Langfuse v4 observations are immutable. An uncertain upload is checked again,
    never blindly reinserted into an existing trace.
    """
    from collections import defaultdict
    from langfuse import Evaluation

    sync_dataset(client, cases, version_id=version_id)
    hosted = client.get_dataset("openharness/" + version_id)
    items = {item.input["id"]: item for item in hosted.items}
    batches = defaultdict(list)

    def confirm(artifact):
        target = artifact.provenance.get("langfuse_import_trace_id")
        if not target:
            return False
        try:
            trace = client.api.trace.get(target)
            expected = {s.name for s in artifact.scores if s.value is not None}
            recorded = {
                (o.metadata or {}).get("recorded_observation_id") for o in trace.observations or []
            }
            if {o.id for o in artifact.observations} <= recorded and expected <= {
                s.name for s in trace.scores or []
            }:
                artifact.upload_status = "uploaded"
                persist(artifact)
                return True
        except Exception:
            pass
        artifact.upload_status = "pending"
        persist(artifact)
        return False

    for artifact in artifacts:
        if artifact.case_id not in items or artifact.dataset_version != version_id:
            raise LangfuseConfigurationError("补传必须使用执行时的数据集版本")
        if artifact.provenance.get("langfuse_import_trace_id"):
            confirm(artifact)
            continue
        batches[(artifact.run_id, artifact.repetition)].append(artifact)

    for (run_id, repetition), batch in batches.items():
        saved = {a.case_id: a for a in batch}

        def task(*, item, **kwargs):
            case_id = item["input"]["id"] if isinstance(item, dict) else item.input["id"]
            artifact = saved[case_id]
            target = client.get_current_trace_id()
            # Persist the delivery intent before sending any immutable observations.
            artifact.provenance["langfuse_import_trace_id"] = target
            artifact.upload_status = "pending"
            persist(artifact)
            export_artifact(client, artifact, trace_id=target, scores=False)
            persist(artifact)
            return {
                "case_id": case_id,
                "answer": artifact.answer,
                "status": artifact.status,
                "recorded_elapsed_ms": artifact.elapsed_ms,
            }

        def evaluator(*, output, **kwargs):
            return [
                Evaluation(name=s.name, value=s.value, comment=s.explanation)
                for s in saved[output["case_id"]].scores
                if s.value is not None
            ]

        result = client.run_experiment(
            name="openharness-saved-agent-evaluation",
            run_name=f"{run_id}-import-r{repetition}",
            data=[items[a.case_id] for a in batch],
            task=task,
            evaluators=[evaluator],
            max_concurrency=1,
            metadata={
                "dataset_version": version_id,
                "imported": "true",
                "timing": "Original execution timing is in recorded_* metadata and scores",
            },
        )
        client.flush()
        for artifact in batch:
            artifact.provenance["langfuse_experiment_url"] = result.dataset_run_url
            confirm(artifact)
            persist(artifact)
    return {
        state: sum(a.upload_status == state for a in artifacts)
        for state in ("uploaded", "pending", "failed", "local")
    }


def run_sdk_experiment(client, runner, cases, *, name, repetition=1):
    from langfuse import Evaluation

    sync_dataset(client, list(runner.cases.values()), version_id=runner.version)
    hosted = client.get_dataset("openharness/" + runner.version)
    selected = {c.id for c in cases}
    data = [item for item in hosted.items if item.input["id"] in selected]
    if len(data) != len(cases):
        raise LangfuseConfigurationError("服务端任务集与本地版本不一致")
    artifacts = {}
    runner.observer_factory = lambda **kwargs: LangfuseObserver(client, **kwargs)

    async def task(*, item, **kwargs):
        case_id = item["input"]["id"] if isinstance(item, dict) else item.input["id"]
        artifact = await runner.run_case(
            runner.cases[case_id], repetition=repetition, run_name=name
        )
        artifacts[case_id] = artifact
        return {"case_id": case_id, "answer": artifact.answer, "status": artifact.status}

    def evaluator(*, output, **kwargs):
        artifact = artifacts[output["case_id"]]
        return [
            Evaluation(name=s.name, value=s.value, comment=s.explanation)
            for s in artifact.scores
            if s.value is not None
        ]

    result = client.run_experiment(
        name="openharness-agent-evaluation",
        run_name=name,
        data=data,
        task=task,
        evaluators=[evaluator],
        max_concurrency=1,
        metadata={"dataset_version": runner.version, "repetition": str(repetition)},
    )
    client.flush()
    from openharness.evaluation.runner import write_artifact

    for artifact in artifacts.values():
        artifact.provenance["langfuse_experiment_url"] = result.dataset_run_url
        artifact.provenance["langfuse_import_trace_id"] = artifact.trace_id
        write_artifact(
            runner.output
            / "results"
            / f"{artifact.case_id}-r{artifact.repetition}-{artifact.run_id}.json",
            artifact,
        )
    return result, list(artifacts.values())
