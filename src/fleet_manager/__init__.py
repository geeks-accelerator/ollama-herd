"""Ollama Herd — smart inference router that herds your Ollama instances into one endpoint."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ollama-herd")
except PackageNotFoundError:
    # Fallback for local development without installation
    __version__ = "0.0.0-dev"
