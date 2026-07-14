"""Node-side MLX routing helper.

Inference goes through the server-side
:class:`fleet_manager.server.mlx_proxy.MlxProxy`, and the node manages
`mlx_lm.server` subprocesses via :class:`fleet_manager.node.mlx_supervisor.MlxSupervisorSet`
(which owns health + model discovery).  All that remains here is the tiny
``mlx:``-prefix helper the collector uses when advertising MLX models in the
heartbeat.
"""

from __future__ import annotations


def prefix_mlx(model_id: str) -> str:
    """Add the ``mlx:`` prefix used by herd routing when advertising.

    Idempotent — if the id is already prefixed, returns it unchanged.
    """
    if model_id.startswith("mlx:"):
        return model_id
    return f"mlx:{model_id}"
