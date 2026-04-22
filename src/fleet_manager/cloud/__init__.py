"""Cloud connector — optional WebSocket tunnel to gotomy.ai platform.

Allows remote access to a local fleet via a hosted coordination server.
When enabled, the herd router maintains a persistent WebSocket connection
to the platform, receives inference requests from remote clients, and
streams responses back.

Enable with: `herd --cloud --token ft_xxx`
"""
from fleet_manager.cloud.connector import CloudConnector

__all__ = ["CloudConnector"]
