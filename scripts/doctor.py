"""Convenience wrapper for environments where the console script is unavailable."""

from schemabridge.entrypoints.cli.main import app

if __name__ == "__main__":
    app(["doctor"])
