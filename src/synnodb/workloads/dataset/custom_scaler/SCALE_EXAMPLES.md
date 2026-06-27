# Scale Parquet Examples

## Scale Up (Multiply rows by 2x)

```bash
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/sf2 \
  --scale 2
```

This will:
- Read all tables from the DuckDB database
- Multiply each table's rows by 2x
- For numeric PK/FK columns: add offset multipliers (e.g., 1, 1+offset, 1+2*offset)
- For string PK/FK columns: append suffixes (e.g., 'id', 'id_0', 'id_1')
- Write scaled tables as Parquet files to the output directory

## Scale Down (Sample to 10% of original size)

```bash
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/ \
  --scale 0.1
```

This will:
- Read all tables from the DuckDB database
- Sample 10% of rows from each table
- Write sampled tables as Parquet files to the output directory

## Different Downscale Factors

```bash
# 25% downscale
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/ \
  --scale 0.25

# 50% downscale
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/ \
  --scale 0.5
```

## Different Scale Factors

```bash
# 3x upscale
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/ \
  --scale 3

# 10x upscale
python -m dataset.custom_scaler.scale_parquet \
  --duckdb /mnt/labstore/bespoke_olap/imdb_parquet/imdb.duckdb \
  --output-dir /mnt/labstore/bespoke_olap/imdb_parquet/ \
  --scale 10
```
