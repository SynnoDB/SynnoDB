from typing import Dict


class ValidateCacheType:
    """ValidateCacheType to include metrics for Wandb logging."""

    def __init__(
        self,
        outputs: str,
        success: bool,
        metrics: Dict,
        hash_payload: str,
        snapshot_hash: str,
        trace_output: str | None = None,
        cmd_output: str | None = None,
    ):
        self.outputs = outputs
        self.metrics = metrics
        self.success = success
        self.hash_payload = hash_payload
        self.snapshot_hash = snapshot_hash
        self.trace_output = trace_output
        self.cmd_output = cmd_output
