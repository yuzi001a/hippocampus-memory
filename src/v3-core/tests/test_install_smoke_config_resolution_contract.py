from v3core import first_run as fr


def test_install_smoke_uses_canonical_config_resolution_for_env_refs():
    src = fr.smoke_write_recall.__code__
    names = set(src.co_names)
    assert "resolve_config" in names
    assert "from_legacy_dict" not in names
