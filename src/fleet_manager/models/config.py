"""Configuration models with sensible defaults for zero-config startup."""

from __future__ import annotations

from datetime import datetime

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 11435
    heartbeat_interval: float = 5.0
    heartbeat_timeout: float = 15.0
    heartbeat_offline: float = 30.0
    mdns_service_type: str = "_fleet-manager._tcp.local."
    mdns_service_name: str = "Fleet Manager Router"
    data_dir: str = "~/.fleet-manager"

    # Scoring weights
    score_model_hot: float = 50.0
    score_model_warm: float = 30.0
    score_model_cold: float = 10.0
    score_memory_fit_max: float = 20.0
    score_queue_depth_max_penalty: float = 30.0
    score_queue_depth_penalty_per: float = 6.0
    score_wait_time_max_penalty: float = 25.0
    score_role_affinity_max: float = 15.0
    score_role_large_threshold_gb: float = 20.0
    score_role_small_threshold_gb: float = 8.0
    score_availability_trend_max: float = 10.0
    score_context_fit_max: float = 15.0

    # Rebalancer
    rebalance_interval: float = 5.0
    rebalance_threshold: int = 4
    rebalance_max_per_cycle: int = 3

    # Pre-warm
    pre_warm_threshold: int = 3
    pre_warm_min_availability: float = 0.60

    # Auto-pull
    auto_pull: bool = True
    auto_pull_timeout: float = 300.0  # 5 minutes

    # VRAM-aware fallback: route to loaded model in same category instead of cold-loading
    vram_fallback: bool = True

    # Context protection: prevent clients from triggering Ollama model reloads via num_ctx
    # "strip" = remove num_ctx when ≤ loaded context (default, prevents reload hang)
    # "warn"  = keep num_ctx but log warnings
    # "passthrough" = do nothing
    context_protection: str = "strip"

    # Stale request reaper
    # Seconds before in-flight requests are considered zombied (15 min default)
    stale_timeout: float = 600.0

    # Image generation routing
    image_generation: bool = True  # Route /api/generate-image to nodes with mflux/DiffusionKit
    image_timeout: float = 120.0  # Max seconds to wait for image generation

    # Transcription routing
    transcription: bool = True  # Route /api/transcribe to nodes with Qwen3-ASR
    transcription_timeout: float = 300.0  # Max seconds for transcription

    # Vision embedding routing (CLIP, DINOv2, SigLIP)
    vision_embedding: bool = True  # Route /api/embed-image to nodes with vision embeddings
    vision_embedding_timeout: float = 30.0  # Max seconds for embedding

    # Thinking model support
    thinking_overhead: float = 4.0  # Multiply num_predict by this for thinking models
    thinking_min_predict: int = 1024  # Minimum num_predict for thinking models

    # Dynamic context management
    dynamic_num_ctx: bool = False  # Inject num_ctx overrides on cold loads
    num_ctx_overrides: dict[str, int] = {}  # Per-model: {"gpt-oss:120b": 32768}
    num_ctx_auto_calculate: bool = False  # Auto-calculate from trace data

    # Fleet Intelligence — LLM-powered dashboard briefing
    fleet_intelligence: bool = True  # Enable briefing card on dashboard
    fleet_intelligence_model: str = ""  # Empty = auto-select best loaded LLM

    # Retry
    max_retries: int = 2

    # Device-aware scoring — see docs/plans/device-aware-scoring.md.
    # When true, Signal 5 (role affinity) rewards nodes proportional to their
    # memory bandwidth instead of using flat memory-size tiers, so a Mac
    # Studio (800 GB/s) outscores a MacBook (300 GB/s) for big models even
    # when both have plenty of free RAM.  Falls back to memory-tier scoring
    # when a node's bandwidth is unknown (older agents / unrecognized chips).
    bandwidth_aware_scoring: bool = True

    # Capacity-normalized queue penalty.  When true, a queue of N on a node
    # that's 4× faster than the fleet baseline is treated like a queue of
    # N/4 for Signal 3's penalty calculation — so the scorer doesn't flip
    # away from a fast node until it's genuinely saturated.  Combined with
    # ``bandwidth_aware_scoring`` this produces load distribution roughly
    # proportional to each node's bandwidth share of the fleet.
    queue_penalty_bandwidth_normalize: bool = True

    # Debug request capture — writes every request's full lifecycle (client body,
    # translated Ollama body, response, tokens, timings, error) to a JSONL file at
    # ``<data_dir>/debug/requests.<date>.jsonl``.  Intended for internal fleets
    # where you want to replay exact failures.  **Captures user prompts and
    # responses** — never enable on public gateways. See server/debug_log.py.
    debug_request_bodies: bool = False
    debug_request_retention_days: int = 7

    # Anthropic Messages API compat (for Claude Code etc.)
    # JSON map of claude-* model id → local Ollama model.
    # Always include a "default" key to catch unknown claude-* requests.
    anthropic_model_map: dict[str, str] = {
        "default": "qwen3-coder:30b",
        "claude-opus-4-7": "qwen3:32b",
        "claude-sonnet-4-6": "qwen3-coder:30b",
        "claude-sonnet-4-5": "qwen3-coder:30b",
        "claude-haiku-4-5": "qwen3:14b",
    }
    # Optional shared secret for /v1/messages. When require_key is true and the
    # client's x-api-key header doesn't match anthropic_api_key, return 401.
    anthropic_require_key: bool = False
    anthropic_api_key: str = ""
    anthropic_default_max_tokens: int = 4096
    # When the incoming /v1/messages request contains image content blocks, route
    # to this vision-capable model regardless of what the Claude tier would map to.
    # Empty string disables the override — images pass through to the mapped model,
    # which may or may not be vision-capable (qwen3-coder is not; gemma3:27b is).
    # Typical values: "gemma3:27b", "llava:13b".
    anthropic_vision_model: str = ""
    # Tool-schema fixup — work around Qwen3-Coder's long-context tool-call bug
    # (llama.cpp#20164) by promoting optional params with known defaults to
    # required-with-default in the outbound schema.  See
    # ``src/fleet_manager/server/tool_schema_fixup.py`` and the research doc
    # ``docs/research/why-claude-code-degrades-at-30k.md`` for details.
    #
    # Modes:
    #   "off"     — don't touch schemas (pre-fix behavior)
    #   "promote" — only promote params that already have ``default`` fields
    #               (no-op on current Claude Code, which doesn't emit defaults)
    #   "inject"  — use the built-in Claude Code defaults table + promote
    #               (the actual fix; default)
    anthropic_tool_schema_fixup: str = "inject"

    # ---- Context management (matches hosted Claude Code's behavior) -----
    # Mechanical tool-result clearing: drop old tool_result bodies by age
    # once the prompt crosses a threshold.  Replaces the body with a
    # short placeholder but keeps the conversation structure intact.
    # Closes the biggest gap vs hosted Claude Code, which does this
    # aggressively via its Context Editing API.  See
    # ``server/context_management.py`` and
    # ``docs/research/why-claude-code-degrades-at-30k.md``.
    #
    # Set trigger to 0 to disable.
    anthropic_auto_clear_tool_uses_trigger_tokens: int = 100_000
    # Number of most-recent tool_result blocks to preserve verbatim.
    # Older ones get the placeholder.  3 matches hosted Claude's
    # observed behavior of keeping the last 3-5 exchanges intact.
    anthropic_auto_clear_tool_uses_keep_recent: int = 3
    # Server-side tool filtering.  Comma-separated tool names to strip
    # from outbound Anthropic tool schemas before forwarding to the local
    # model.  Mirrors the community-known ``permissions.deny`` trick in
    # ``~/.claude/settings.json`` but applied at the router — lets
    # operators trim tools their workflow doesn't use without requiring
    # each Claude Code client to be reconfigured.  Typical savings:
    # ~40% of the tools-section token budget.  Example:
    #     FLEET_ANTHROPIC_TOOLS_DENY=NotebookEdit,TodoWrite
    # Empty string disables filtering (default).
    anthropic_tools_deny: str = ""
    # Size-based routing escalation.  When the prompt (raw, before any
    # context management) exceeds ``anthropic_size_escalation_tokens``,
    # route to ``anthropic_size_escalation_model`` regardless of what
    # the tier map resolved.  Useful for sending long-context runs to a
    # different (larger-context, possibly hosted) model while short
    # requests stay on the fast local default.  Matches the
    # ``longContext`` routing pattern in musistudio/claude-code-router.
    # Empty model disables; threshold = 0 disables.
    anthropic_size_escalation_tokens: int = 0
    anthropic_size_escalation_model: str = ""
    # Session-level rescue: if the prompt is still larger than this after
    # Layer 1 mechanical clearing, pass ``force_all=True`` to the LLM-based
    # compactor so it summarises EVERY tool_result regardless of the
    # per-strategy min_bloat gates.  Matches Anthropic's default
    # compaction trigger of 150K input tokens.  Set to 0 to disable.
    context_compaction_force_trigger_tokens: int = 150_000
    # Hard pre-inference cap on prompt size.  If, after BOTH Layer 1
    # clearing and Layer 2 compaction (including force-all), the prompt
    # still exceeds this, the request is refused with HTTP 413 before
    # it ever reaches the model.  Better to surface the error to the
    # client (which can run /compact and resubmit) than to let the
    # request wedge for 5+ minutes at the model layer.  Set to 0 to
    # disable.  180K leaves headroom under Qwen3-Coder-Next's 256K
    # native context while staying well inside effective-context bounds.
    anthropic_max_prompt_tokens: int = 180_000
    # Wall-clock timeout on MLX requests from admission → final byte.
    # Catches the wedged-request case where mlx_lm.server emits tokens
    # slowly but never hits a stop condition.  The slot is released and
    # the route returns 413 with a ``try /compact`` hint.
    mlx_wall_clock_timeout_s: float = 300.0

    # MLX backend — opt-in alternative serving path for large models that can't
    # coexist with Ollama's hardcoded 3-model concurrent-load cap on macOS.  Each
    # `mlx_lm.server` is an independent process with its own memory budget, so
    # running it alongside Ollama lets us keep 4+ models hot simultaneously on a
    # 512GB Mac Studio.  See `docs/plans/mlx-backend-for-large-models.md`.
    #
    # Model names prefixed with `mlx:` route to this backend instead of Ollama.
    # Example: FLEET_ANTHROPIC_MODEL_MAP='{"claude-opus-4-7":"mlx:Qwen3-Coder-480B-A35B-4bit", ...}'
    mlx_enabled: bool = False
    mlx_url: str = "http://localhost:11440"
    # When auto-start is on, herd-node will spawn `mlx_lm.server` as a subprocess.
    # Requires `mlx-lm` installed and a valid `mlx_auto_start_model` path.
    mlx_auto_start: bool = False
    mlx_auto_start_model: str = ""  # path or HF repo id for --model
    # KV cache quantization (matches Ollama's OLLAMA_KV_CACHE_TYPE=q8_0).  Requires
    # upstream PR #1073 merged or our local patch applied to mlx_lm.server.  Set
    # to 0 to skip the flag (f16 KV, works on stock mlx_lm).
    mlx_kv_bits: int = 0  # 0 disables; 4 or 8 for quantized KV (needs patched server)
    # Queue admission control for the MLX backend.  mlx_lm.server is
    # single-threaded per process — without a bound, Claude Code retry storms
    # stack up inside mlx's HTTP queue and wedge the whole backend.  With
    # this cap, the proxy accepts at most 1 in-flight + N queued requests;
    # overflow returns HTTP 503 + Retry-After so clients back off cleanly.
    # Tune per device: faster hardware drains the queue faster so can tolerate
    # a larger depth without excessive worst-case wait.
    #
    # Default bumped from 3 to 10 on 2026-04-24 after observing that real
    # Claude Code sessions routinely generate bursts of 4+ concurrent
    # requests (main turn + /compact trigger + tool_use expansions + any
    # parallel production scripts sharing the router), and depth=3 produced
    # false-positive 503s for legitimate traffic.  At the Mac Studio's
    # ~5s/request on Qwen3-Coder-Next cached prompts, depth=10 means
    # worst-case wait ≈ 50s.  Clients still get a clean 503 if overwhelmed.
    mlx_max_queue_depth: int = 10
    # Maximum concurrent in-flight requests per MLX model (per port).  Default
    # 1 = strict serialization, matching what the proxy has always done.
    # Set to 2-3 to let mlx_lm.server's BatchGenerator process multiple
    # requests in one inference pass — empirically validated 2026-04-27 to
    # produce wall-time ≈ max(individual) instead of wall ≈ sum (i.e. real
    # parallelism, not just overlap).  Higher values trade reliability for
    # throughput: each in-flight request carries its own KV cache state, so
    # running 2 × 100K-token prefills concurrently doubles the prompt-cache
    # memory footprint.  Concurrent-request paths in mlx_lm.server have
    # historically been bug magnets (e.g. #1166 fixed in v0.31.3), so the
    # conservative default is 1.  Bump to 2 if your workload bursts (multiple
    # Claude Code sessions, parallel tool calls) and you've measured headroom.
    # See ``docs/research/mlx-lm-stability-and-concurrency.md``.
    mlx_max_inflight_per_model: int = 1
    # Seconds to advertise in the Retry-After header when shedding load.
    mlx_retry_after_seconds: int = 10
    # HTTP read timeout (seconds) for requests to mlx_lm.server.  The proxy
    # sets stream=True internally so the timeout applies per-byte-chunk, not
    # end-to-end.  600s was tight for non-streaming calls to the 480B when
    # other models were competing for memory bandwidth — a full prefill +
    # generation could span 10+ min of silence.  1800s gives the big-model
    # prefill plenty of headroom while still bounding a truly stuck server.
    mlx_read_timeout_s: float = 1800.0

    # -- Context Hygiene Compactor ------------------------------------------
    # Server-side middleware that summarizes bloated tool_result blocks
    # (Read/Bash/WebFetch output) before they reach the main model.
    # Closes the effective-context gap between local LLMs and hosted Claude
    # on agent workloads.  See docs/experiments/context-bloat-analysis.py
    # for the opportunity measurement, and src/fleet_manager/server/
    # context_compactor.py for the implementation.
    #
    # Default OFF during soak; flip after validation.  Requires a curator
    # model (default gpt-oss:120b on the local Ollama) to be available.
    context_compaction_enabled: bool = False
    # Budget above which compaction fires.  Below this, pass through
    # unchanged.  Measured: median real Claude Code request is ~32K tokens,
    # 83% exceed 20K.  Tune based on model effective context.
    context_compaction_budget_tokens: int = 20_000
    # Curator model — must be an Ollama model id reachable via the local
    # Ollama client.  gpt-oss:120b works well; qwen3-coder:30b is faster.
    context_compaction_model: str = "gpt-oss:120b"
    # Recent turns to preserve verbatim.  Too low and compaction damages the
    # model's active reasoning context; too high and we don't compact enough
    # to help.
    context_compaction_preserve_turns: int = 3
    # Curator timeout per summary call.  Failures return None and the
    # original content passes through (fail-open).
    context_compaction_curator_timeout_s: float = 60.0
    # Dynamic curator selection: prefer whatever capable model is already
    # hot and idle over cold-loading the configured default.  A pinned
    # model that's been idle for ``idle_window_s`` is the IDEAL candidate
    # (user-preferred quality + guaranteed-hot + no contention).  A hot
    # model with recent activity gets penalised so we don't steal slots
    # from real user traffic.  Set to 0 to always use
    # ``context_compaction_model``.
    context_compaction_idle_window_s: int = 120
    # Min params (in billions) for a model to be considered a viable
    # curator.  Below this, summary quality is unreliable — we'd rather
    # skip compaction than use a tiny model.
    context_compaction_curator_min_params_b: float = 7.0

    # -- Model preloader + pinned models ------------------------------------
    # Ollama (as of 0.20.4 on macOS) has a HARDCODED 3-model hot cap that
    # no env override can raise.  The preloader's job is to keep the
    # right 3 models warm without thrashing the cap.
    #
    # Pinned models are ALWAYS kept warm — if evicted, the preloader
    # reloads them at its next refresh.  Useful for models you depend on
    # across projects (e.g. gpt-oss:120b for scripts + gemma3:27b for
    # vision).  Comma-separated list.
    pinned_models: str = ""  # e.g. "gpt-oss:120b,gemma3:27b"
    # Cap on how many models the preloader will load during startup or
    # refresh.  Should be <= Ollama's hot cap to avoid self-inflicted
    # thrashing.  3 is the Ollama 0.20.4 macOS default.
    model_preload_max_count: int = 3
    # Kill switch — set true to disable the preloader entirely (models
    # load on-demand on first request).  Useful if preloader is causing
    # unexpected eviction behavior.
    disable_model_preloader: bool = False

    model_config = {"env_prefix": "FLEET_"}


class NodeSettings(BaseSettings):
    node_id: str = ""
    ollama_host: str = "http://localhost:11434"
    router_url: str = ""
    heartbeat_interval: float = 5.0
    poll_interval: float = 5.0
    mdns_service_type: str = "_fleet-manager._tcp.local."
    enable_capacity_learning: bool = False
    data_dir: str = "~/.fleet-manager"

    # Platform connection (all None when disconnected).
    # The operator token is stored as SecretStr so it never appears in
    # repr() / str() / model_dump() without explicit get_secret_value().
    # Persisted separately to ~/.fleet-manager/platform.json with 0600
    # permissions — never written to the main config.yaml.
    platform_url: str | None = None
    platform_token: SecretStr | None = None
    platform_node_id: str | None = None
    platform_connected_at: datetime | None = None

    # Telemetry opt-ins (require platform connection to take effect)
    telemetry_local_summary: bool = False
    telemetry_include_tags: bool = False

    # MLX backend — when enabled, the node agent polls mlx_lm.server and merges
    # its models into the heartbeat alongside Ollama's.  Each MLX model shows
    # up in the fleet with an `mlx:` prefix so routers / Anthropic routes can
    # direct requests to it.  See `docs/plans/mlx-backend-for-large-models.md`.
    mlx_enabled: bool = False
    mlx_url: str = "http://localhost:11440"
    # Subprocess lifecycle (Phase 3): when auto_start is true, the node agent
    # launches `mlx_lm.server` with the configured model + KV bits, monitors
    # its health, and restarts it on crash.
    mlx_auto_start: bool = False
    mlx_auto_start_model: str = ""  # local path or HF repo id for --model
    mlx_kv_bits: int = 0  # 0 disables; 4 or 8 for quantized KV (needs patched server)
    mlx_prompt_cache_size: int = 4
    mlx_prompt_cache_bytes: int = 17179869184  # 16 GiB
    # Speculative decoding — draft model that proposes tokens the main
    # model verifies.  Accepted tokens skip a main-model forward pass
    # and yield 10-30% throughput on coding workloads.  Draft must share
    # the main model's tokenizer (Qwen3 main → Qwen3-family draft).
    # Empty string disables.  Example: "mlx-community/Qwen3-1.7B-4bit"
    # alongside main="mlx-community/Qwen3-Coder-Next-4bit".
    # See docs/plans/claude-code-performance-improvements.md §1.
    mlx_draft_model: str = ""
    # How many tokens the draft proposes per step.  3-4 is typical —
    # higher increases acceptance opportunity but wastes more on rejections.
    mlx_num_draft_tokens: int = 4

    # Multi-MLX-server support.  When non-empty, overrides the single-server
    # fields above (mlx_auto_start_model / mlx_url / mlx_kv_bits are ignored —
    # they're preserved only for back-compat with old deploys).  Each entry
    # spawns one `mlx_lm.server` subprocess on its own port; the node
    # aggregates them in the heartbeat and the router proxy looks up the
    # right URL per request.
    #
    # JSON-encoded list.  Each entry accepts:
    #   model       (str)   — HF repo id or local path (required)
    #   port        (int)   — listen port (required, must be unique)
    #   kv_bits     (int)   — 0 / 4 / 8 (optional, default 0)
    #   prompt_cache_bytes (int) — optional, default matches single-server
    #   draft_model (str)   — optional speculative-decoding draft
    #
    # Example:
    #   FLEET_NODE_MLX_SERVERS='[
    #     {"model":"mlx-community/Qwen3-Coder-Next-4bit","port":11440,"kv_bits":8},
    #     {"model":"mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit","port":11441,"kv_bits":8}
    #   ]'
    # See docs/issues/multi-mlx-server-support.md for the full design.
    mlx_servers: str = ""
    # Bind host for mlx_lm.server subprocesses.  Default 127.0.0.1 keeps the
    # servers local-only.  Set to "0.0.0.0" to expose them on the LAN so the
    # router on a different machine can reach them directly.  The node's LAN
    # IP gets reported in the heartbeat as the URL each MLX server is
    # reachable at, so the router can route multi-node MLX traffic.
    mlx_bind_host: str = "127.0.0.1"
    # Memory-pressure startup gate.  Before spawning each mlx_lm.server, the
    # supervisor estimates the model's weight size (from the HF cache on
    # disk) and refuses to start if
    #   (weight_gb + mlx_memory_headroom_gb) > psutil.virtual_memory().available_gb
    # Prevents OOM crash-loops when operators configure more servers than the
    # box can host.  Failed servers log WARNING once and get retried on a
    # slower cadence in case memory frees up (e.g. an Ollama model evicts).
    mlx_memory_headroom_gb: float = 10.0

    # NOTE: an "Ollama watchdog" used to live here — auto-probe + pkill on
    # stuck runners.  Removed 2026-04-23 after it caused more harm than
    # good in production: the probe picked the smallest loaded model as
    # its chat-probe target, which selected embedding-only models like
    # ``nomic-embed-text``; ``/api/chat`` on an embed model returns 400,
    # which the watchdog interpreted as a stuck runner.  Cascade: 13
    # kicks in ~13 min, then escalation to a full ``ollama serve``
    # restart that wiped all pinned models.  See ``docs/issues.md``.
    # If real stuck-runner recovery is ever needed again, add it back
    # with (a) explicit probe-model allowlisting, not size-based, and
    # (b) per-cause cooldowns so a guaranteed-failing probe can't
    # escalate.

    model_config = {"env_prefix": "FLEET_NODE_"}
