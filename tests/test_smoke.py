from sku_cockpit import __version__


def test_package_imports() -> None:
    assert __version__


def test_runtime_dependencies_are_importable() -> None:
    """Fail fast if the environment lost a core dependency."""
    import duckdb  # noqa: F401
    import pandas  # noqa: F401
    import pandera  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
