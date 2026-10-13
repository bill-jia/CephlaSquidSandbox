# Zarr shards and streaming upload

New acquisitions use flat shard filenames and one shard per FOV, timepoint,
store, and pyramid level. Arrays remain 5D `(T, C, Z, Y, X)` with one image plane
per independently compressed inner chunk `(1, 1, 1, Y, X)`. A full-stack shard is
`(1, C, Z, Y, X)`; compression remains the configured Blosc preset.

The writer buffers incoming planes until all channels and Z positions for the
stack have arrived, then writes each level's entire shard once. An early upload
barrier cannot publish or delete an incomplete stack. On finalization, captured
planes in a short/aborted stack are saved and submitted for upload too.

Retained stack images have a 256 MiB budget shared across writers in each saving
process (`ZarrWriter.MAX_BUFFERED_STACK_BYTES`). Oversized or incomplete stacks
spill received planes to local shards while withholding them from upload until
complete or finalized. Spills can incur additional shard rewrites. This budget
does not include transient assembly arrays, compression buffers, or TensorStore
caches; it is not a total-process RAM limit.

## Filenames and compatibility

The default Zarr v3 chunk-key encoding uses `separator: "."`, for example:

```
BF.ome.zarr/A/1/0/0/c.165.0.0.0.0
BF.ome.zarr/A/1/0/frame_times/c.0.0.0
```

The plate/well/FOV/level hierarchy remains. Recovery and backfill read each
array's `zarr.json` to support both flat names and existing slash-separated
names. Upload cleanup deletes the exact verified file paths; only legacy
slash-layout arrays need empty-directory pruning. Timestamp resync also reads
the encoding from metadata.

Defaults in `control/_def.py`:

```python
ZARR_SHARD_PER_Z = False
ZARR_CHUNK_SEPARATOR = "."
```

Setting `ZARR_SHARD_PER_Z = True` retains per-Z shards for workloads that benefit
from earlier disk commits. Setting the separator to `"/"` creates the old nested
coordinate directories. These settings affect newly created arrays after an
application restart; they do not convert existing data or change a running job.
Do not rename existing shard files or edit their metadata to change the layout.

## Metadata upload

Upload barriers still submit metadata candidates, allowing failed transfers to
be retried by later barriers. The upload worker remembers the source SHA-256
only after a successful verified transfer. Under the destination's upload lock,
an unchanged `zarr.json` is acknowledged without another remote copy/readback
or duplicate manifest append. Changed metadata is transferred and verified again.
Image shards and timestamp chunks never use this cache.

After all saving processes finish, metadata resync transfers final changes and
the settled timestamp chunks. Partial stacks committed at finalization enter
the same upload task counter and verified-deletion flow as other shards.

For an acquisition with 11 BF Z planes and two single-plane fluorescence stores,
five resolution levels require 15 image files per FOV/timepoint instead of 65.
Pixel data volume is essentially unchanged. Upload speed depends on whether
small-file operations or network bandwidth are the limiting factor.
