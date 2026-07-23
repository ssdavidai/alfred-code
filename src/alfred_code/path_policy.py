from __future__ import annotations

import re
from collections.abc import Iterable


_WRITE_GLOB_META = re.compile(r"[*?\[\]{}]")


def codex_write_path_supported(path: str) -> bool:
    """Return whether Codex can enforce this repository-relative write scope."""
    value = path.removeprefix("./").rstrip("/")
    if not value:
        return False
    if value.endswith("/**"):
        root = value[:-3].rstrip("/")
        return bool(root) and _WRITE_GLOB_META.search(root) is None
    return _WRITE_GLOB_META.search(value) is None


def unsupported_codex_write_paths(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if not codex_write_path_supported(path))


def codex_write_scope_error(paths: Iterable[str]) -> str | None:
    unsupported = unsupported_codex_write_paths(paths)
    if not unsupported:
        return None
    rendered = ", ".join(repr(path) for path in unsupported)
    return (
        "Codex writable scope supports only exact paths or directory subtrees "
        f"ending in '/**'; unsupported path(s): {rendered}"
    )
