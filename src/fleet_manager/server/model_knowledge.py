"""Curated knowledge base of popular Ollama models.

Provides benchmark scores, memory requirements, and capability classifications
so the recommendation engine can suggest optimal model mixes for each fleet node.
Benchmark scores are approximate and sourced from public leaderboards.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ModelCategory(StrEnum):
    GENERAL = "general"
    REASONING = "reasoning"
    CODING = "coding"
    CREATIVE = "creative"
    FAST_CHAT = "fast-chat"
    IMAGE = "image"


class ModelSize(StrEnum):
    SMALL = "small"  # 1-4B
    MEDIUM = "medium"  # 7-22B
    LARGE = "large"  # 27-72B
    EXTRA_LARGE = "xl"  # 100B+


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ModelBenchmarks(BaseModel):
    """Approximate benchmark scores (0-100 scale where applicable)."""

    mmlu: float | None = None
    humaneval: float | None = None
    mt_bench: float | None = None

    @property
    def quality_score(self) -> float:
        """Composite quality score (0-100) from available benchmarks."""
        scores = [s for s in [self.mmlu, self.humaneval, self.mt_bench] if s is not None]
        return sum(scores) / len(scores) if scores else 0.0


class ModelSpec(BaseModel):
    """Everything we know about a model for recommendation purposes."""

    ollama_name: str  # e.g. "qwen3:8b"
    display_name: str  # e.g. "Qwen 3 8B"
    family: str  # e.g. "qwen3"
    params_b: float  # Total parameters in billions
    active_params_b: float | None = None  # Active params for MoE models
    ram_gb: float  # Approx RAM needed at Q4_K_M
    size_class: ModelSize
    category: ModelCategory
    secondary_categories: list[ModelCategory] = Field(default_factory=list)
    benchmarks: ModelBenchmarks = Field(default_factory=ModelBenchmarks)
    context_length: int = 8192
    notes: str = ""

    @property
    def is_moe(self) -> bool:
        return self.active_params_b is not None


# ---------------------------------------------------------------------------
# The knowledge base
# ---------------------------------------------------------------------------

MODEL_CATALOG: list[ModelSpec] = [
    # ── SMALL (1-4B) — fast responses, low memory ────────────
    ModelSpec(
        ollama_name="qwen3:4b",
        display_name="Qwen 3 4B",
        family="qwen3",
        params_b=4.0,
        ram_gb=4.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=65.0, humaneval=60.0),
        notes="Rivals much larger models on reasoning",
    ),
    ModelSpec(
        ollama_name="gemma3:4b",
        display_name="Gemma 3 4B",
        family="gemma3",
        params_b=4.0,
        ram_gb=4.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.CODING,
        secondary_categories=[ModelCategory.GENERAL],
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=70.0, humaneval=71.3),
        notes="Strong coding for its size, 128K context",
    ),
    ModelSpec(
        ollama_name="llama3.2:3b",
        display_name="Llama 3.2 3B",
        family="llama3.2",
        params_b=3.0,
        ram_gb=3.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.GENERAL,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=63.4, humaneval=55.0),
        notes="Meta, solid lightweight baseline",
    ),
    ModelSpec(
        ollama_name="llama3.2:1b",
        display_name="Llama 3.2 1B",
        family="llama3.2",
        params_b=1.0,
        ram_gb=2.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.FAST_CHAT,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=47.0, humaneval=35.0),
        notes="100+ tok/s, ideal for quick lookups",
    ),
    ModelSpec(
        ollama_name="phi-3:mini",
        display_name="Phi-3 Mini",
        family="phi-3",
        params_b=3.8,
        ram_gb=3.5,
        size_class=ModelSize.SMALL,
        category=ModelCategory.REASONING,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=62.0, humaneval=55.0),
        notes="Punches above its weight on reasoning tasks",
    ),
    ModelSpec(
        ollama_name="qwen2.5-coder:1.5b",
        display_name="Qwen 2.5 Coder 1.5B",
        family="qwen2.5-coder",
        params_b=1.5,
        ram_gb=2.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.CODING,
        context_length=32768,
        benchmarks=ModelBenchmarks(humaneval=50.0),
        notes="Tiny coding specialist",
    ),
    # ── MEDIUM (7-22B) — good balance ────────────────────────
    ModelSpec(
        ollama_name="qwen3:8b",
        display_name="Qwen 3 8B",
        family="qwen3",
        params_b=8.0,
        ram_gb=6.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=75.0, humaneval=72.0),
        notes="Strong all-rounder",
    ),
    ModelSpec(
        ollama_name="qwen3:14b",
        display_name="Qwen 3 14B",
        family="qwen3",
        params_b=14.0,
        ram_gb=10.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=80.0, humaneval=78.0),
        notes="Excellent quality-to-size ratio",
    ),
    ModelSpec(
        ollama_name="phi-4:14b",
        display_name="Phi-4 14B",
        family="phi-4",
        params_b=14.0,
        ram_gb=10.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.REASONING,
        context_length=16384,
        benchmarks=ModelBenchmarks(mmlu=82.0, humaneval=76.0),
        notes="Matches models 5-10x its size on reasoning",
    ),
    ModelSpec(
        ollama_name="llama3.1:8b",
        display_name="Llama 3.1 8B",
        family="llama3.1",
        params_b=8.0,
        ram_gb=6.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.GENERAL,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=73.0, humaneval=72.0),
        notes="Meta workhorse, 128K context",
    ),
    ModelSpec(
        ollama_name="gemma3:12b",
        display_name="Gemma 3 12B",
        family="gemma3",
        params_b=12.0,
        ram_gb=9.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.GENERAL,
        secondary_categories=[ModelCategory.CREATIVE],
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=76.0, humaneval=70.0),
        notes="Google, multimodal capable",
    ),
    ModelSpec(
        ollama_name="deepseek-r1:8b",
        display_name="DeepSeek R1 8B",
        family="deepseek-r1",
        params_b=8.0,
        ram_gb=6.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.REASONING,
        context_length=65536,
        benchmarks=ModelBenchmarks(mmlu=72.0, humaneval=68.0),
        notes="Distilled reasoning from 671B model",
    ),
    ModelSpec(
        ollama_name="deepseek-r1:14b",
        display_name="DeepSeek R1 14B",
        family="deepseek-r1",
        params_b=14.0,
        ram_gb=10.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.REASONING,
        context_length=65536,
        benchmarks=ModelBenchmarks(mmlu=78.0, humaneval=75.0),
        notes="Chain-of-thought reasoning",
    ),
    ModelSpec(
        ollama_name="mistral-nemo:12b",
        display_name="Mistral NeMo 12B",
        family="mistral-nemo",
        params_b=12.0,
        ram_gb=9.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.CREATIVE,
        secondary_categories=[ModelCategory.GENERAL],
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=72.0, humaneval=65.0),
        notes="Strong creative writing, 128K context",
    ),
    ModelSpec(
        ollama_name="qwen2.5-coder:7b",
        display_name="Qwen 2.5 Coder 7B",
        family="qwen2.5-coder",
        params_b=7.0,
        ram_gb=5.5,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.CODING,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=65.0, humaneval=88.4),
        notes="Beats much larger code models on HumanEval",
    ),
    ModelSpec(
        ollama_name="qwen2.5-coder:14b",
        display_name="Qwen 2.5 Coder 14B",
        family="qwen2.5-coder",
        params_b=14.0,
        ram_gb=10.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.CODING,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=72.0, humaneval=90.0),
        notes="State-of-art coding at 14B",
    ),
    ModelSpec(
        ollama_name="codestral:22b",
        display_name="Codestral 22B",
        family="codestral",
        params_b=22.0,
        ram_gb=15.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.CODING,
        context_length=262144,
        benchmarks=ModelBenchmarks(humaneval=86.6),
        notes="Mistral, 256K context, fast code generation",
    ),
    # ── LARGE (27-72B) — high quality ────────────────────────
    ModelSpec(
        ollama_name="qwen3:30b-a3b",
        display_name="Qwen 3 30B-A3B (MoE)",
        family="qwen3",
        params_b=30.0,
        active_params_b=3.0,
        ram_gb=19.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=81.4, humaneval=75.0),
        notes="MoE: 30B total, 3B active — great quality per GB",
    ),
    ModelSpec(
        ollama_name="qwen3:32b",
        display_name="Qwen 3 32B",
        family="qwen3",
        params_b=32.0,
        ram_gb=22.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        secondary_categories=[ModelCategory.REASONING],
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=83.0, humaneval=82.0),
        notes="Best dense open model at 32B",
    ),
    ModelSpec(
        ollama_name="deepseek-r1:32b",
        display_name="DeepSeek R1 32B",
        family="deepseek-r1",
        params_b=32.0,
        ram_gb=22.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.REASONING,
        context_length=65536,
        benchmarks=ModelBenchmarks(mmlu=84.0, humaneval=80.0),
        notes="Distilled from 671B, outperforms o1-mini",
    ),
    ModelSpec(
        ollama_name="gemma3:27b",
        display_name="Gemma 3 27B",
        family="gemma3",
        params_b=27.0,
        ram_gb=19.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        secondary_categories=[ModelCategory.CREATIVE],
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=80.0, humaneval=75.0),
        notes="Google, multimodal, good creative writing",
    ),
    ModelSpec(
        ollama_name="qwen2.5-coder:32b",
        display_name="Qwen 2.5 Coder 32B",
        family="qwen2.5-coder",
        params_b=32.0,
        ram_gb=22.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.CODING,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=75.0, humaneval=92.7),
        notes="Best open-source code model",
    ),
    ModelSpec(
        ollama_name="devstral:24b",
        display_name="Devstral 24B",
        family="devstral",
        params_b=24.0,
        ram_gb=17.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.CODING,
        context_length=131072,
        benchmarks=ModelBenchmarks(humaneval=82.0),
        notes="Agentic coding, fits 32GB Mac",
    ),
    ModelSpec(
        ollama_name="command-r:35b",
        display_name="Command R 35B",
        family="command-r",
        params_b=35.0,
        ram_gb=24.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=72.0, humaneval=65.0),
        notes="Cohere, RAG-optimized, 128K context",
    ),
    ModelSpec(
        ollama_name="deepseek-r1:70b",
        display_name="DeepSeek R1 70B",
        family="deepseek-r1",
        params_b=70.0,
        ram_gb=42.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.REASONING,
        context_length=65536,
        benchmarks=ModelBenchmarks(mmlu=88.0, humaneval=85.0),
        notes="Near-frontier reasoning quality",
    ),
    ModelSpec(
        ollama_name="llama3.3:70b",
        display_name="Llama 3.3 70B",
        family="llama3.3",
        params_b=70.0,
        ram_gb=42.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=86.0, humaneval=88.4),
        notes="Meta flagship, 20-30 tok/s on Apple Silicon",
    ),
    ModelSpec(
        ollama_name="qwen2.5:72b",
        display_name="Qwen 2.5 72B",
        family="qwen2.5",
        params_b=72.0,
        ram_gb=44.0,
        size_class=ModelSize.LARGE,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=78.4, humaneval=82.0),
        notes="Alibaba flagship, best multilingual support",
    ),
    # ── EXTRA LARGE (100B+) — frontier quality ───────────────
    ModelSpec(
        ollama_name="qwen3.5:122b-a10b",
        display_name="Qwen 3.5 122B-A10B (MoE)",
        family="qwen3.5",
        params_b=122.0,
        active_params_b=10.0,
        ram_gb=75.0,
        size_class=ModelSize.EXTRA_LARGE,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=86.0, humaneval=85.0),
        notes="MoE, strong tool use, beats GPT-5 mini on function calling",
    ),
    ModelSpec(
        ollama_name="gpt-oss:120b",
        display_name="GPT-OSS 120B",
        family="gpt-oss",
        params_b=120.0,
        ram_gb=72.0,
        size_class=ModelSize.EXTRA_LARGE,
        category=ModelCategory.REASONING,
        secondary_categories=[ModelCategory.GENERAL],
        context_length=131072,
        benchmarks=ModelBenchmarks(mmlu=90.0, humaneval=88.0),
        notes="OpenAI open-weight, frontier-class",
    ),
    ModelSpec(
        ollama_name="qwen3:235b-a22b",
        display_name="Qwen 3 235B-A22B (MoE)",
        family="qwen3",
        params_b=235.0,
        active_params_b=22.0,
        ram_gb=130.0,
        size_class=ModelSize.EXTRA_LARGE,
        category=ModelCategory.GENERAL,
        context_length=32768,
        benchmarks=ModelBenchmarks(mmlu=88.0, humaneval=87.0),
        notes="MoE flagship",
    ),
    ModelSpec(
        ollama_name="deepseek-r1:671b",
        display_name="DeepSeek R1 671B",
        family="deepseek-r1",
        params_b=671.0,
        ram_gb=350.0,
        size_class=ModelSize.EXTRA_LARGE,
        category=ModelCategory.REASONING,
        context_length=65536,
        benchmarks=ModelBenchmarks(mmlu=90.8, humaneval=90.2),
        notes="Frontier-class reasoning, needs server hardware",
    ),
    # ── IMAGE — image generation models ─────────────────────────
    # DiffusionKit models (Stable Diffusion 3.x via MLX)
    ModelSpec(
        ollama_name="sd3-medium",
        display_name="Stable Diffusion 3 Medium",
        family="stable-diffusion-3",
        params_b=2.0,
        ram_gb=8.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.IMAGE,
        context_length=0,
        benchmarks=ModelBenchmarks(),
        notes="Stability AI's SD3 — MLX-native via DiffusionKit",
    ),
    ModelSpec(
        ollama_name="sd3.5-large",
        display_name="Stable Diffusion 3.5 Large",
        family="stable-diffusion-3",
        params_b=8.0,
        ram_gb=16.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.IMAGE,
        context_length=0,
        benchmarks=ModelBenchmarks(),
        notes="Highest quality SD model — MLX-native via DiffusionKit, uses T5 encoder",
    ),
    # Ollama native image generation models
    ModelSpec(
        ollama_name="x/z-image-turbo",
        display_name="Z-Image-Turbo",
        family="z-image-turbo",
        params_b=6.0,
        ram_gb=8.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.IMAGE,
        context_length=0,
        benchmarks=ModelBenchmarks(),
        notes="Ollama native image gen — fast photorealistic images",
    ),
    ModelSpec(
        ollama_name="x/flux2-klein",
        display_name="FLUX.2 Klein 4B",
        family="flux2-klein",
        params_b=4.0,
        ram_gb=6.0,
        size_class=ModelSize.SMALL,
        category=ModelCategory.IMAGE,
        context_length=0,
        benchmarks=ModelBenchmarks(),
        notes="Ollama native image gen — good text rendering",
    ),
    ModelSpec(
        ollama_name="x/flux2-klein:9b",
        display_name="FLUX.2 Klein 9B",
        family="flux2-klein",
        params_b=9.0,
        ram_gb=12.0,
        size_class=ModelSize.MEDIUM,
        category=ModelCategory.IMAGE,
        context_length=0,
        benchmarks=ModelBenchmarks(),
        notes="Ollama native image gen — higher quality variant",
    ),
]

# Index by name for quick lookups
_BY_NAME: dict[str, ModelSpec] = {m.ollama_name: m for m in MODEL_CATALOG}


def lookup_model(name: str) -> ModelSpec | None:
    """Find a model spec by Ollama name (exact or fuzzy)."""
    # Exact match
    if name in _BY_NAME:
        return _BY_NAME[name]
    # Try without :latest suffix
    base = name.removesuffix(":latest")
    if base in _BY_NAME:
        return _BY_NAME[base]
    # Family match (e.g. "qwen3-coder" -> first qwen model with coder)
    for spec in MODEL_CATALOG:
        if spec.family == base or spec.ollama_name.startswith(base + ":"):
            return spec
    return None


def classify_model(name: str) -> ModelCategory:
    """Classify a model name into a category. Falls back to GENERAL."""
    spec = lookup_model(name)
    if spec:
        return spec.category
    # Heuristic fallback for unknown models
    lower = name.lower()
    if any(k in lower for k in ("coder", "codestral", "devstral", "starcoder")):
        return ModelCategory.CODING
    if any(k in lower for k in ("deepseek-r1", "phi-4", "reasoning")):
        return ModelCategory.REASONING
    if any(k in lower for k in ("creative", "mistral-nemo", "story")):
        return ModelCategory.CREATIVE
    # Ollama native image models use x/ prefix
    if lower.startswith("x/"):
        return ModelCategory.IMAGE
    return ModelCategory.GENERAL


def is_image_model(name: str) -> bool:
    """Check if a model name is an Ollama native image generation model.

    Only matches models with the ``x/`` prefix (Ollama's image model namespace).
    Does NOT match mflux model names like ``z-image-turbo`` — those are handled
    by the separate mflux image server on port 11436.
    """
    if not name.lower().startswith("x/"):
        return False
    return classify_model(name) == ModelCategory.IMAGE


def models_fitting_ram(available_gb: float) -> list[ModelSpec]:
    """Return models that fit in available RAM, sorted by quality (best first)."""
    return sorted(
        [m for m in MODEL_CATALOG if m.ram_gb <= available_gb],
        key=lambda m: m.benchmarks.quality_score,
        reverse=True,
    )


def best_for_category(category: ModelCategory, available_gb: float) -> ModelSpec | None:
    """Return the highest-quality model for a category that fits in RAM."""
    candidates = [
        m
        for m in models_fitting_ram(available_gb)
        if m.category == category or category in m.secondary_categories
    ]
    return candidates[0] if candidates else None
