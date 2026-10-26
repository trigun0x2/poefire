from collections.abc import Iterator

import pytest

from poefire import store as store_module


@pytest.fixture(autouse=True)
def close_stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every Store a test opened, so ResourceWarnings stay meaningful."""
    opened: list[store_module.Store] = []
    original_init = store_module.Store.__init__

    def tracking_init(self: store_module.Store, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    monkeypatch.setattr(store_module.Store, "__init__", tracking_init)
    yield
    for s in opened:
        s.close()
