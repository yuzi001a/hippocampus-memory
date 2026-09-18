from types import SimpleNamespace

import pytest

from v3core.prefetch import prefetch


def test_prefetch_rejects_postgres_config_without_matching_pg_handle():
    cfg = SimpleNamespace(pg=SimpleNamespace(host="127.0.0.1", port=55444, database="other"))
    with pytest.raises(ValueError, match="requires pg"):
        prefetch("isolation probe", config=cfg, pg=None, core=None)
