"""Optional execution observations. Ordinary sessions use a network-free no-op."""

from contextlib import contextmanager
from collections.abc import Mapping, Iterator
from typing import Protocol, ContextManager


class ObservationHandle(Protocol):
    def update(self, **values: object) -> None: ...


class Observer(Protocol):
    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: object = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ContextManager[ObservationHandle]: ...


class NullObservation:
    def update(self, **values: object) -> None:
        pass


class NullObserver:
    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        input: object = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[ObservationHandle]:
        yield NullObservation()


NULL_OBSERVER = NullObserver()


def observer_for(metadata: Mapping[str, object] | None) -> Observer:
    value = (metadata or {}).get("observer")
    # ExecutionMetadata enforces the observer protocol at the producer boundary.
    from typing import cast

    return cast(Observer, value) if value is not None else NULL_OBSERVER
