# BFF File Layout

BFF is a proposed analytical file format for this runner. It is not Parquet.
The core API exposes metadata and byte-buffer reads, not Arrow tables, so future
DuckDB/DataFusion bindings can build native scan operators.

## Dataset

A dataset is a directory of self-describing table files:

```text
dataset.bff/
  _manifest.bffmeta   optional table discovery metadata
  customer.bff
  orders.bff
  lineitem.bff
```

The manifest is optional. Each `.bff` table file should be readable alone. The
format should support direct I/O, mmap, footer pruning, row-group/page reads,
zero-copy friendly buffers, and an Arrow-independent query-time read path.

## Table File

```text
file header
row group 0 pages
row group 1 pages
...
footer
trailer
```

The trailer stores magic bytes, format version, footer offset, footer size, and
footer checksum. Readers open the file, read the trailer, then load and
optionally cache the footer.

## Footer

The footer is the main pruning surface. It should contain:

- schema: column ids, names, physical types, nullability
- table row count and byte size
- row group offsets, row counts, byte ranges
- page offsets, sizes, row counts, encodings
- row group/page stats: min, max, null count, distinct count
- compression/checksum metadata

The C++ API exposes this through `BffFooterInfo`, `BffTableInfo`,
`BffRowGroupInfo`, and `BffPageInfo`.

## Pruning

Expected scan flow:

1. `open_bff_dataset`
2. `load_bff_footer` or `cached_bff_footer`
3. prune tables, row groups, pages, and columns
4. `open_bff_table`
5. `read_bff_row_group` or `read_bff_page`
6. decode selected buffers and `release_bff_buffer`

The BFF API does not evaluate predicates. DuckDB/DataFusion adapters should use
metadata to decide which row groups/pages/columns to read.

## I/O And Buffers

`BffIoMode` supports buffered reads, direct I/O, and mmap-backed reads.

`BffBuffer` is zero-copy friendly:

- `data`, `size`: byte range
- `alignment`: pointer alignment for direct wrapping
- `file_offset`: source file range
- `storage`: owned, borrowed, mmap, direct I/O, or external
- `immutable`: safe to expose as read-only memory
- `encoded`: whether BFF decoding is still required
- `owner`: opaque lifetime handle

Adapters can wrap a buffer directly only when it is immutable, aligned, and
already in a representation compatible with the target engine.

## Parquet Ingest

Ingest may use Arrow/Parquet internally:

```text
Parquet -> Arrow/Parquet decoding -> BFF writer -> .bff files
```

Query-time reads should use native BFF footer, row group, page, and buffer APIs.

## Likely Extensions

- logical schema metadata and row range selection
- `BffScanRequest` for columns, row groups, pages, and read options
- bloom filters or other pruning indexes
- read metrics: bytes read, pages skipped, direct-I/O fallback count
