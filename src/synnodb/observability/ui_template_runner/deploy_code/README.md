# prepare_code_zip

Packages a bespoke C++ engine snapshot into a zip for deployment.

Given a wandb run-id, it resolves the right git snapshot hash, restores the corresponding workspace state, writes the non-tracked template files on top, and zips the result.

## Usage

```bash
python prepare_code_zip.py <benchmark> <wandb_id> [--snapshot_hash <hash>]
```

**Arguments**

| Argument | Required | Description |
|---|---|---|
| `benchmark` | yes | `tpch` or `ceb` |
| `wandb_id` | yes | Wandb run-id to check out |
| `--snapshot_hash` | no | Override the auto-resolved snapshot hash (must appear in the run's history) |

## Snapshot hash resolution

Without `--snapshot_hash`, the script picks the last snapshot from the **optim-expert** phase (before the "optim human" section begins). This avoids selecting snapshots from the human-baseline phase, which sometimes uses CPU features not available on the public deployment machine.

If no "optim human" section is found, it falls back to the final snapshot of the run.

Use `--snapshot_hash` to pin a specific turn, e.g. to pick an earlier checkpoint. The hash is validated against the run's wandb history and the script will error if it wasn't produced by that run.

## Outputs

Both outputs are written to the `deploy_code/` directory (next to this README):

- **`<wandb_id>.zip`** — the full engine workspace, ready to transfer
- **`code_metadata.json`** — provenance record:
  ```json
  {
    "wandb_run": "<wandb_id>",
    "turn": 42,
    "git_snapshot_hash": "<hash>",
    "model": "claude-opus-4-7"
  }
  ```

## What it does internally

1. Fetches run metrics and history from wandb
2. Resolves the snapshot hash (see above)
3. Cleans the `output/` working directory (prompts if there are uncommitted changes)
4. Writes non-git-tracked template files via `prepare_repo` + `prepare_repo_for_mt`
5. Restores the resolved git snapshot from `git://c01/bespoke_cache.git`
6. Zips everything under `output/` (excluding `.git/`)

## Transfer

After zipping, use `deploy.sh` to push the zip to the target machine.
