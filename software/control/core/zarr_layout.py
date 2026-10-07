"""Chunk paths shared by the writer, upload recovery, and metadata resync.

New arrays use flat keys; existing slash-separated arrays remain readable.
Only numeric chunk keys are data: TensorStore locks and temporary files must
never be discovered as uploadable shards.
"""

import json
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


def chunk_key(indices: Iterable[int], separator: str = ".") -> str:
    if separator not in ("/", "."):
        raise ValueError(f"Unsupported chunk separator: {separator!r}")
    return separator.join(["c", *(str(i) for i in indices)])


def chunk_separator(array_dir: Path) -> str:
    with open(Path(array_dir) / "zarr.json", encoding="utf-8") as f:
        metadata = json.load(f)
    encoding = metadata.get("chunk_key_encoding", {})
    if encoding.get("name") != "default":
        raise ValueError(f"Unsupported chunk-key encoding: {encoding}")
    separator = encoding.get("configuration", {}).get("separator", "/")
    if separator not in ("/", "."):
        raise ValueError(f"Unsupported chunk separator: {separator!r}")
    return separator


def array_chunk_path(array_dir: Path, indices: Iterable[int]) -> Path:
    return Path(array_dir) / chunk_key(indices, chunk_separator(array_dir))


def timepoint_shards(array_dir: Path, timepoint: Optional[int] = None) -> List[Tuple[int, Path]]:
    """Enumerate 5D image shard files, optionally restricted to one timepoint."""
    array_dir = Path(array_dir)
    separator = chunk_separator(array_dir)
    if separator == ".":
        candidates = array_dir.glob("c.*" if timepoint is None else f"c.{timepoint}.*")
    else:
        base = array_dir / "c"
        if timepoint is not None:
            base /= str(timepoint)
        candidates = base.rglob("*")
    result = []
    for path in candidates:
        key = path.relative_to(array_dir).as_posix()
        parts = key.split(separator)
        if len(parts) != 6 or parts[0] != "c" or not all(p.isdecimal() for p in parts[1:]):
            continue
        if path.is_file():
            result.append((int(parts[1]), path))
    return sorted(result)
