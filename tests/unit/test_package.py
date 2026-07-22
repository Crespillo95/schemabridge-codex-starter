"""Package metadata smoke tests."""

from importlib.metadata import version

from schemabridge import __version__


def test_imported_version_matches_installed_metadata() -> None:
    assert __version__ == version("schemabridge")
