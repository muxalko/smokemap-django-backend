"""Retired legacy request importer.

M3 submission creation must go through the authenticated, owner-bound,
idempotent API. Keeping this filename fail-closed avoids an unsafe compatibility
path for old operational scripts.
"""


def import_csv(_file_path):
    raise RuntimeError(
        "Legacy request import is disabled; use the owner-bound M3 submission API."
    )


if __name__ == "__main__":
    import_csv(None)
