"""Optional SDK boundaries, denied/cancelled status and local upload recovery."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from researchx.evaluation.langfuse_backend import (
    LangfuseObserver,
    LangfuseConfigurationError,
    connection_config,
    connect,
    export_artifact,
    import_sdk_experiments,
    langfuse_usage,
)
from researchx.evaluation.models import MetricResult, RunArtifact
from researchx.evaluation.observer import timestamp


def test_langfuse_usage_exclusive_buckets_do_not_double_count_cache():
    usage = {
        "reported": True,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 70,
        "cache_creation_input_tokens": 10,
    }
    buckets = langfuse_usage(usage)
    assert buckets["input"] == 20 and sum(buckets.values()) == 120
    assert langfuse_usage({**usage, "reported": False}) is None


class Remote:
    def __init__(self, values=None):
        self.values, self.ended = values or {}, False

    def update(self, **values):
        self.values.update(values)

    def start_observation(self, **values):
        return Remote(values)

    def end(self):
        self.ended = True


class Client:
    def __init__(self, fail=False):
        self.remote, self.fail, self.scores = [], fail, []
        self.api = SimpleNamespace(
            trace=SimpleNamespace(
                get=lambda _: SimpleNamespace(
                    observations=[{}],
                    scores=[SimpleNamespace(id=s["score_id"]) for s in self.scores],
                )
            )
        )

    @contextmanager
    def start_as_current_observation(self, **values):
        remote = Remote(values)
        self.remote.append(remote)
        try:
            yield remote
        finally:
            remote.end()

    def get_current_trace_id(self):
        return "0" * 32

    def create_score(self, **values):
        self.scores.append(values)

    def flush(self):
        if self.fail:
            raise OSError("upload failed")


def test_private_config_environment_priority_and_missing_credentials_no_sdk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LangfuseConfigurationError):
        connect()
    (tmp_path / ".researchx").mkdir()
    (tmp_path / ".researchx/evaluation.local.json").write_text(
        '{"langfuse":{"base_url":"https://private.example","public_key":"public","secret_key":"private"}}'
    )
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://override.example")
    assert connection_config()["base_url"] == "https://override.example"
    observer = LangfuseObserver(Client())
    with observer.span("tool", input="contains private and public"):
        pass
    assert "private" not in str(observer.observations[0].input)


@pytest.mark.asyncio
async def test_remote_status_closes_after_nested_error_or_cancellation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = Client()
    observer = LangfuseObserver(client)
    with observer.span("task"):
        with observer.span("denied") as span:
            span.update(status="denied")
        with pytest.raises(asyncio.CancelledError):
            with observer.span("cancelled"):
                raise asyncio.CancelledError()
        with pytest.raises(ValueError):
            with observer.span("failed"):
                raise ValueError()
    assert [r.values["metadata"]["execution_status"] for r in client.remote] == [
        "ok",
        "denied",
        "cancelled",
        "error",
    ]
    assert all(r.ended for r in client.remote)


def test_upload_failure_keeps_result_and_flush_requires_readback():
    artifact = RunArtifact(
        run_id="r",
        case_id="c",
        dataset_version="v",
        trace_id="0" * 32,
        started_at=timestamp(),
        answer="local result",
    )
    artifact.scores = [MetricResult(name="task_success", value=0)]
    with pytest.raises(LangfuseConfigurationError):
        export_artifact(Client(fail=True), artifact)
    assert artifact.answer == "local result" and artifact.upload_status == "failed"
    client = Client()
    export_artifact(client, artifact)
    assert artifact.upload_status == "uploaded"
    client.api.trace.get = lambda _: SimpleNamespace(observations=[], scores=[])
    export_artifact(client, artifact)
    assert artifact.upload_status == "pending"


def test_installed_v4_sdk_contract_accepts_hosted_items():
    pytest.importorskip("langfuse")
    import inspect
    from langfuse import Langfuse
    from langfuse._client.datasets import DatasetClient

    assert "data" in inspect.signature(Langfuse.run_experiment).parameters
    assert "run_name" in inspect.signature(DatasetClient.run_experiment).parameters
    assert "score_id" in inspect.signature(Langfuse.create_score).parameters


def test_sdk_import_replays_saved_results_and_does_not_duplicate_confirmed_upload():
    pytest.importorskip("langfuse")
    from researchx.evaluation.dataset import load_cases

    case = load_cases()[0]
    artifact = RunArtifact(
        run_id="saved",
        case_id=case.id,
        dataset_version="frozen-v1",
        trace_id="1" * 32,
        started_at=timestamp(),
        answer="existing answer",
        status="completed",
        scores=[MetricResult(name="task_success", value=0)],
    )

    class ReplayClient(Client):
        experiments = 0

        def create_dataset(self, **kwargs):
            pass

        def create_dataset_item(self, **kwargs):
            pass

        def get_dataset(self, name):
            return SimpleNamespace(items=[SimpleNamespace(input={"id": case.id})])

        def run_experiment(self, **kwargs):
            self.experiments += 1
            output = kwargs["task"](item=kwargs["data"][0])
            assert output["answer"] == "existing answer"
            for score in kwargs["evaluators"][0](output=output):
                self.create_score(score_id=score.name, name=score.name)
            return SimpleNamespace(dataset_run_url="https://example.org/experiment")

    client = ReplayClient()
    client.api.trace.get = lambda _: SimpleNamespace(
        observations=[SimpleNamespace(metadata={})],
        scores=[SimpleNamespace(name=s["name"], id=s["score_id"]) for s in client.scores],
    )
    persisted = []
    states = import_sdk_experiments(
        client,
        [artifact],
        [case],
        version_id="frozen-v1",
        persist=lambda a: persisted.append(a.model_copy(deep=True)),
    )
    assert states["uploaded"] == 1 and client.experiments == 1
    assert persisted[0].upload_status == "pending"
    assert persisted[0].provenance["langfuse_import_trace_id"]
    import_sdk_experiments(
        client,
        [artifact],
        [case],
        version_id="frozen-v1",
        persist=lambda _: None,
    )
    assert client.experiments == 1
