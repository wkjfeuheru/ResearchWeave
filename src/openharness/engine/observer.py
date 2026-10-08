"""Optional execution observations. Ordinary sessions use a network-free no-op."""

from contextlib import contextmanager


class NullObservation:
    def update(self, **values):
        pass


class NullObserver:
    @contextmanager
    def span(self, name, *, kind="span", input=None, metadata=None):
        yield NullObservation()


NULL_OBSERVER = NullObserver()


def observer_for(metadata):
    return (metadata or {}).get("observer") or NULL_OBSERVER
