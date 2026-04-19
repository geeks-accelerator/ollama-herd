"""Vision embedding model management — download, cache, and load.

Supports three model backends:
  - DINOv2 ViT-S/14 via MLX (Apple Silicon, 85MB, 384-dim) — primary
  - SigLIP2-base via MLX (Apple Silicon, 350MB, 768-dim) — general-purpose
  - CLIP ViT-B/32 via ONNX (cross-platform, 90MB int8, 512-dim) — fallback
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Model cache directory
MODELS_DIR = Path(os.path.expanduser("~/.fleet-manager/models"))

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

VISION_EMBEDDING_MODELS = {
    "dinov2-vit-s14": {
        "runtime": "onnx",
        "dimensions": 384,
        "hf_repo": "sefaburak/dinov2-small-onnx",
        "hf_filename": "dinov2_vits14.onnx",
        "size_mb": 85,
        "input_size": 224,  # ONNX export uses 224x224
        "description": "Best visual similarity, smallest, fastest",
    },
    "siglip2-base": {
        "runtime": "onnx",
        "dimensions": 768,
        "hf_repo": "onnx-community/siglip2-base-patch16-224-ONNX",
        "hf_filename": "onnx/vision_model_int8.onnx",
        "size_mb": 90,
        "input_size": 224,
        "description": "General-purpose vision+text embeddings (int8)",
    },
    "clip-vit-b32": {
        "runtime": "onnx",
        "dimensions": 512,
        "hf_repo": "Qdrant/clip-ViT-B-32-vision",
        "hf_filename": "model.onnx",
        "size_mb": 352,
        "input_size": 224,
        "description": "Cross-platform fallback via ONNX",
    },
}

# Preprocessing constants (raw tuples — converted to np arrays lazily)
_CLIP_MEAN_RAW = (0.48145466, 0.4578275, 0.40821073)
_CLIP_STD_RAW = (0.26862954, 0.26130258, 0.27577711)
_IMAGENET_MEAN_RAW = (0.485, 0.456, 0.406)
_IMAGENET_STD_RAW = (0.229, 0.224, 0.225)


def _mlx_available() -> bool:
    """Check if MLX is available (Apple Silicon only)."""
    try:
        import mlx.core  # noqa: F401

        return True
    except ImportError:
        return False


def get_model_dir(model_name: str) -> Path:
    """Return the cache directory for a model."""
    return MODELS_DIR / model_name


def is_model_downloaded(model_name: str) -> bool:
    """Check if a model's weights are cached locally."""
    model_dir = get_model_dir(model_name)
    if not model_dir.exists():
        return False
    # Check for any ONNX file (may have custom names or be in subdirs)
    return bool(list(model_dir.rglob("*.onnx"))) or any(
        model_dir.rglob("*.safetensors")
    )


def download_model(model_name: str) -> Path:
    """Download a vision embedding model to the local cache.

    Returns the model directory path.
    """
    spec = VISION_EMBEDDING_MODELS.get(model_name)
    if not spec:
        raise ValueError(f"Unknown vision embedding model: {model_name}")

    model_dir = get_model_dir(model_name)
    model_dir.mkdir(parents=True, exist_ok=True)

    if is_model_downloaded(model_name):
        logger.info(f"Model {model_name} already cached at {model_dir}")
        return model_dir

    logger.info(
        f"Downloading {model_name} ({spec['size_mb']}MB) "
        f"from {spec['hf_repo']}..."
    )

    if spec["runtime"] == "onnx":
        _download_onnx_model(model_name, spec, model_dir)
    else:
        _download_mlx_model(model_name, spec, model_dir)

    logger.info(f"Model {model_name} downloaded to {model_dir}")
    return model_dir


def _download_onnx_model(model_name: str, spec: dict, model_dir: Path) -> None:
    """Download an ONNX model file from HuggingFace."""
    from huggingface_hub import hf_hub_download

    filename = spec.get("hf_filename", "model.onnx")
    hf_hub_download(
        repo_id=spec["hf_repo"],
        filename=filename,
        local_dir=str(model_dir),
    )


def _download_mlx_model(model_name: str, spec: dict, model_dir: Path) -> None:
    """Download an MLX-compatible model from HuggingFace."""
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=spec["hf_repo"],
        local_dir=str(model_dir),
        ignore_patterns=["*.bin", "*.pt", "*.pth", "tf_*", "flax_*"],
    )


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------


def preprocess_image(
    image,  # PIL.Image.Image
    input_size: int = 224,
    mean: tuple = _IMAGENET_MEAN_RAW,
    std: tuple = _IMAGENET_STD_RAW,
):
    """Preprocess an image for vision model input.

    Resize (bicubic, shortest side), center crop, normalize.
    Returns array of shape (1, 3, H, W) as float32.
    """
    import numpy as np
    from PIL import Image as PILImage

    image = image.convert("RGB")

    # Resize shortest side to input_size, preserving aspect ratio
    w, h = image.size
    if w < h:
        new_w, new_h = input_size, int(h * input_size / w)
    else:
        new_w, new_h = int(w * input_size / h), input_size
    image = image.resize((new_w, new_h), PILImage.BICUBIC)

    # Center crop to input_size x input_size
    left = (new_w - input_size) // 2
    top = (new_h - input_size) // 2
    image = image.crop((left, top, left + input_size, top + input_size))

    # To float32 array, normalize
    np_mean = np.array(mean, dtype=np.float32)
    np_std = np.array(std, dtype=np.float32)
    arr = np.array(image, dtype=np.float32) / 255.0
    arr = (arr - np_mean) / np_std

    # HWC -> CHW, add batch dim -> (1, 3, H, W)
    arr = np.transpose(arr, (2, 0, 1))
    return np.expand_dims(arr, axis=0)


# ---------------------------------------------------------------------------
# Inference backends
# ---------------------------------------------------------------------------


class ONNXBackend:
    """Vision embedding inference via ONNX Runtime.

    Works with any ONNX vision model (CLIP, DINOv2, SigLIP).
    Reads model spec for input size, preprocessing, and output dimensions.
    """

    def __init__(self, model_dir: Path, model_name: str = "clip-vit-b32"):
        import onnxruntime as ort

        spec = VISION_EMBEDDING_MODELS.get(model_name, {})
        # Find the ONNX file
        onnx_filename = spec.get("hf_filename", "model.onnx")
        # Handle nested paths (e.g., "onnx/model.onnx")
        model_path = model_dir / onnx_filename
        if not model_path.exists():
            # Try without subdirectory
            model_path = model_dir / model_path.name
        if not model_path.exists():
            # Fallback: find any .onnx file
            onnx_files = list(model_dir.rglob("*.onnx"))
            if onnx_files:
                model_path = onnx_files[0]
            else:
                raise FileNotFoundError(f"No ONNX file found in {model_dir}")

        # Use CPU only. CoreMLExecutionProvider can trigger macOS TCC
        # permission dialogs that block the Python process indefinitely
        # (Neural Engine access / Desktop folder access). On M-series
        # chips, CPU inference is fast enough (~60ms/image) and reliable.
        # Users can opt-in via FLEET_EMBEDDING_USE_COREML=true.
        providers = ["CPUExecutionProvider"]
        if os.environ.get("FLEET_EMBEDDING_USE_COREML", "").lower() == "true":
            try:
                available = ort.get_available_providers()
                if "CoreMLExecutionProvider" in available:
                    providers.insert(0, "CoreMLExecutionProvider")
                    logger.warning(
                        "CoreML provider enabled — may trigger macOS TCC "
                        "permission dialogs that block the process"
                    )
            except Exception:
                pass
        self.session = ort.InferenceSession(str(model_path), providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.dimensions = spec.get("dimensions", 512)
        self.input_size = spec.get("input_size", 224)

        # Use CLIP preprocessing for CLIP, ImageNet for everything else
        if "clip" in model_name.lower():
            self._mean = _CLIP_MEAN_RAW
            self._std = _CLIP_STD_RAW
        else:
            self._mean = _IMAGENET_MEAN_RAW
            self._std = _IMAGENET_STD_RAW

        logger.info(
            f"ONNX backend loaded: {model_name} from {model_path.name}, "
            f"{self.dimensions}-dim, {self.input_size}px input"
        )

    def embed(self, images):
        """Generate embeddings for a batch of images.

        Returns (N, dims) float32 array, L2-normalized.
        """
        import numpy as np

        pixels = np.concatenate(
            [preprocess_image(img, self.input_size, self._mean, self._std)
             for img in images],
            axis=0,
        )
        outputs = self.session.run(None, {self.input_name: pixels})
        embeddings = outputs[0]
        # Some models return (1, seq_len, dims) — take [CLS] token
        if embeddings.ndim == 3:
            embeddings = embeddings[:, 0, :]
        # L2 normalize
        norms = np.linalg.norm(embeddings, axis=-1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        return embeddings / norms


def load_backend(model_name: str) -> ONNXBackend:
    """Load the ONNX inference backend for a model.

    Downloads the model if not already cached.
    """
    spec = VISION_EMBEDDING_MODELS.get(model_name)
    if not spec:
        raise ValueError(f"Unknown vision embedding model: {model_name}")

    model_dir = download_model(model_name)
    return ONNXBackend(model_dir, model_name)


def select_default_model() -> str:
    """Choose the best available model.

    Prefers DINOv2 (smallest, best visual similarity).
    Falls back to any downloaded model, then CLIP as last resort.
    """
    # Prefer whatever is already downloaded
    for name in ("dinov2-vit-s14", "siglip2-base", "clip-vit-b32"):
        if is_model_downloaded(name):
            return name
    # Default to DINOv2 (smallest download)
    return "dinov2-vit-s14"
