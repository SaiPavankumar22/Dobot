"""Typed configuration for the Dobot backend.

Everything the runtime needs comes from the environment (see ``.env.example``). No module reads
``os.environ`` directly: it asks :func:`get_settings`, which keeps configuration testable and keeps
secret handling in one place.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_root() -> Path:
    """Where Dobot keeps ``.env``, ``skills/`` and ``.dobot/``.

    Three worlds, in priority order:

    - ``DOBOT_HOME`` is set: that directory (tests, portable installs).
    - Frozen (the PyInstaller sidecar the desktop installer bundles): the directory holding the
      executable — Tauri installs it next to ``Dobot.exe``, so ``.env`` sits beside it and stays
      editable from the app's Settings page.
    - Otherwise: the source checkout (two levels above this file).
    """
    override = os.environ.get("DOBOT_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # The installation directory is read-only on many Windows machines.  Keep mutable
        # configuration, memory and downloaded runtimes in the per-user app-data directory while
        # still allowing DOBOT_HOME to opt into a portable layout.
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "Dobot"
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _resolve_root()


def _split_csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and the repo-root ``.env``."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime ------------------------------------------------------------
    dobot_env: str = "development"
    log_level: str = "INFO"
    dobot_host: str = "127.0.0.1"
    dobot_port: int = 8756
    dobot_state_dir: str = ".dobot"
    #: Where skills live. Empty means the repo's own ``skills/`` folder. It is configurable for one
    #: specific reason: without it the test suite reads and *writes* the shipped skills directory,
    #: which is how a test can silently edit the user's repo.
    dobot_skills_dir: str = ""
    automation_poll_seconds: int = 30

    # --- reasoning: Nebius Token Factory / Nemotron --------------------------
    nebius_api_key: str = ""
    # Nebius Token Factory OpenAI-compatible endpoint. The legacy api.studio.nebius.com host
    # returns 404 for /chat/completions — it must be the Token Factory host.
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    # Defaults match the model ids served by Nebius Token Factory (verified against GET /v1/models).
    nemotron_model: str = "nvidia/Nemotron-3-Ultra-550b-a55b"
    nemotron_super_model: str = "nvidia/nemotron-3-super-120b-a12b"
    nemotron_light_model: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    #: Only used when set — screen transcription degrades to local OCR when this is empty or absent.
    nemotron_vision_model: str = "openbmb/MiniCPM-V-4_5"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    #: cloud = Nebius, local = an OpenAI-compatible local server, hybrid = local with cloud fallback.
    #: The local server is never started implicitly.
    reasoning_provider: str = "cloud"
    local_model_base_url: str = "http://127.0.0.1:11434/v1"
    local_model: str = ""
    # Nemotron 3 is a reasoning model: by default it emits a thinking trace before the answer, which
    # can be thousands of tokens of latency. Short conversational turns and the final answer
    # composition do not need it, so the shell disables thinking for the light tier and for composed
    # answers (chat_template_kwargs: {enable_thinking: false}). Set this to true to always think.
    nemotron_always_think: bool = False

    # --- web research: Tavily ------------------------------------------------
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"

    # --- memory --------------------------------------------------------------
    # Structured records and vector memory are always local SQLite/filesystem stores.

    # --- conversation sessions ------------------------------------------------
    # The transcript is durable and unbounded; what reaches the model is not. These three numbers
    # are the whole context policy: keep this many recent turns raw, in at most this many
    # characters, and fold everything older into the rolling summary once the unsummarised tail
    # grows past ``session_compact_after`` messages.
    #: Recent turns handed to the planner verbatim. Follow-ups need these to resolve "it" / "that".
    session_history_turns: int = 10
    #: Character budget for that raw tail — a long session must not be able to blow the prompt.
    session_history_chars: int = 4000
    #: Cap on one stored message. The transcript is durable; it is not a context window.
    session_message_chars: int = 8000
    #: Unsummarised messages that trigger a compaction pass.
    session_compact_after: int = 16
    #: Target length of the rolling summary.
    session_summary_chars: int = 1200

    # --- execution runtime: Hermes ------------------------------------------
    # local = Dobot's guarded in-process tools. cli = the `hermes` CLI. remote/api/hermes =
    # the real Hermes Agent gateway over HTTP (/v1/runs + SSE). auto = gateway if reachable.
    agent_runtime: str = "local"
    hermes_command: str = "hermes"
    hermes_args: str = "-p"
    #: Base URL of a running Hermes gateway. Empty means http://127.0.0.1:8642.
    hermes_endpoint: str = ""
    #: Bearer token for the gateway — the value of the gateway's API_SERVER_KEY.
    hermes_api_key: str = ""
    #: Optional fixed conversation id so successive Dobot turns share one Hermes session.
    hermes_session_id: str = ""
    #: When the gateway is unreachable, keep serving with Dobot's own guarded tools instead of
    #: failing every request. Set false for strict "Hermes or nothing" deployments.
    hermes_fallback_local: bool = True
    #: How long a health answer is trusted before the next call re-probes the gateway.
    hermes_health_ttl_seconds: int = 30
    #: Short timeout for the health probe; a dead gateway must not delay a plan.
    hermes_probe_timeout_seconds: float = 2.5
    hermes_allowed_tools: str = "browser_exec,computer_use,terminal,filesystem,skills"
    hermes_timeout_seconds: int = 300

    # --- security boundary: NemoClaw / OpenShell or local action firewall ----
    # local = Dobot's action firewall. nemoclaw = NemoClaw CLI / OpenShell gateway.
    sandbox_provider: str = "local"
    nemoclaw_command: str = "nemoclaw"
    nemoclaw_sandbox: str = "dobot"
    openshield_gateway_url: str = ""
    sandbox_allowed_network: str = (
        "tavily.com,api.tavily.com,api.studio.nebius.com,github.com,arxiv.org"
    )
    sandbox_allowed_paths: str = "~/"

    # --- decision layer ------------------------------------------------------
    jev_enabled: bool = True
    jev_mode: str = "advisory"
    # The judgement model behind JEV: AutoTrust JEV-27B-VL on Novita's decision-evaluation
    # endpoint (POST /v3/autotrust/v1/decide). One request = one question; the answer is a
    # probability distribution over options, not free text — nothing to parse, nothing to
    # hallucinate. JEV asks it how risky an action is (with thinking=auto, so an unsure answer
    # gets a second look), whether untrusted on-screen/web content is trying to manipulate the
    # assistant, and — vision — whether a credential is visible in the captured screenshot.
    # Bring your own key from the app's Settings page (Your API keys → Novita) or set
    # NOVITA_API_KEY. Without a key the Doctor says so and the deterministic policies decide alone.
    novita_api_key: str = ""
    novita_base_url: str = "https://api.novita.ai"
    jev_model: str = "autotrust/jev-27b-vl"
    #: off | auto | on. `auto` thinks only when the risk answer's confidence is below JEV_THRESHOLD;
    #: `score`-kind questions would reject it, but JEV asks choice/noul questions.
    jev_thinking: str = "auto"
    #: Confidence below which `thinking=auto` lets the model reason before answering.
    jev_threshold: float = 0.8
    #: Thinking effort when it does think: xhigh | medium | low (the model rejects `high`).
    jev_reasoning_effort: str = "medium"
    #: Hard cap on thinking tokens, so an uncertain judgement costs a little, never unbounded.
    jev_think_budget: int = 512
    #: Ask the vision question against the screenshot SEE captured (is a credential on screen?).
    jev_screen_check: bool = True
    #: Read timeout for one decide call. Thinking calls get JEV_THINK_BUDGET tokens of headroom.
    jev_timeout_seconds: float = 25.0
    shadow_mode: bool = False
    # --- agent harness (LangGraph) -------------------------------------------
    # Every tool step is driven through a structured validate→execute→finalise loop. When langgraph
    # is installed the loop runs as a real state graph; otherwise the same stages run inline. The
    # outcome is identical either way — this is structure and traceability, not a behaviour change.
    langgraph_enabled: bool = True
    # The whole request lifecycle (see→understand→decide→act) also runs as a LangGraph state machine
    # when langgraph is installed; set false to drive the identical stage helpers directly.
    orchestration_engine_langgraph: bool = True
    # --- deep research (deepagents harness) -----------------------------------
    # Optional deepagents-based research subagent (LangChain's agent harness on LangGraph). Off by
    # default; when on, the planner can choose `deep_research` for questions that need multiple
    # searches synthesised into a cited report. Read-only; still gated by the decision engine.
    deepagents_enabled: bool = False
    # On by default so untrusted content cannot steer medium-risk mutations without a human check.
    # Existing trusted deployments may explicitly set REQUIRE_WRITE_APPROVAL=false. Reads
    # (fs_list, fs_read) stay automatic either way — this gates changes, not inspection.
    require_write_approval: bool = True
    #: Execution mode when a request does not name one: ask | assist | agent. Each mode is a different
    #: approval threshold — see app/core/decision_engine.py. `agent` is the specification default.
    execution_mode: str = "agent"
    kill_switch_hotkey: str = "Ctrl+Shift+Esc"

    # --- file guard ----------------------------------------------------------
    # A guard independent of the command guards: these locations are never read or written by a tool,
    # approved or not. Credential stores, by default. Add your own with a comma-separated list.
    protected_paths: str = (
        "~/.ssh,~/.aws,~/.gnupg,~/.kube,~/.netrc,~/.config/gh,~/.config/gcloud,"
        "~/.docker/config.json,~/.dobot.secret"
    )

    # --- skill scanner -------------------------------------------------------
    # block = a flagged skill is refused; warn = it loads but is reported and cannot run unattended;
    # off = do not scan. A flagged skill never reaches the planner in either active mode.
    skill_scan_mode: str = "block"
    #: Skill names that are trusted regardless of what the scanner finds.
    skill_scan_allowlist: str = ""

    # --- tokenjuice ----------------------------------------------------------
    # Tool output is compressed before it reaches the model. The *full* output is still stored on the
    # step record and shown in the UI; only the copy sent to the reasoner is compressed. Every
    # compression records chars-saved, so the saving is measurable rather than claimed.
    tokenjuice_enabled: bool = True
    #: Outputs shorter than this are passed through untouched — compressing them costs more than it saves.
    tokenjuice_min_chars: int = 1200
    #: Per-tool-result budget handed to the model.
    tokenjuice_max_chars: int = 4000
    #: Lines kept from the head and the tail when a multi-line blob has to be shrunk.
    tokenjuice_head_lines: int = 40
    tokenjuice_tail_lines: int = 25

    # --- cost accounting ------------------------------------------------------
    # Blended price in USD per million tokens, used only to estimate spend. Defaults to 0 (unknown),
    # because inventing a price would make the ledger lie. Set them for your plan.
    price_per_mtok_input: float = 0.0
    price_per_mtok_output: float = 0.0
    #: Longest run journal kept per task, in entries.
    run_journal_limit: int = 200
    #: How many task journals exist at all. The local store rewrites a whole collection per write, so
    #: an unbounded journal collection would slow down with every task ever run.
    run_journal_keep: int = 200

    # --- memory maintenance ---------------------------------------------------
    #: Ebbinghaus-style retention: a memory's strength decays with time and is reinforced on use.
    memory_decay_enabled: bool = True
    #: Half-life of an untouched memory, in days. Frequently recalled memories keep getting reinforced.
    memory_half_life_days: float = 21.0
    #: Memories whose strength falls below this are forgotten by the consolidation pass.
    memory_forget_threshold: float = 0.12
    #: The canonical ledger is injected on every turn, so it is capped deliberately.
    canonical_facts_limit: int = 40
    consolidation_enabled: bool = True
    consolidation_interval_seconds: int = 21_600  # every 6 hours
    #: Promote a fact to the canonical ledger once it has been stated this many times.
    canonical_promote_after: int = 3
    #: Pass B (LLM review): opt-in. When True the consolidator asks a cheap model to propose
    #: near-duplicate merges, stale-fact flags, and canonical promotions. Secrets are stripped
    #: before any prompt is built; proposals land in the 'consolidation_proposals' store table.
    consolidation_review_enabled: bool = False
    #: When True, pass B merges of low-importance (< 0.5) records are applied automatically
    #: without waiting for a human to review them on the Doctor page.
    consolidation_auto_apply_reviews: bool = False

    # --- identity -------------------------------------------------------------
    #: Your name and what Dobot should call you. Kept in config so the identity layer can render
    #: GENOME.md / TELOS.md on first run without asking the model anything.
    principal_name: str = ""
    assistant_name: str = "Dobot"

    # --- pre-action interceptors ---------------------------------------------
    # Runtime enforcement for skills: deterministic rules that read the live tool call and can block,
    # escalate, or annotate it. Unlike a prompt instruction, an interceptor fires on every match by
    # construction.
    interceptors_enabled: bool = True

    # --- voice ----------------------------------------------------------------
    # Off by default: speaking without being asked is startling. When on, Dobot can read an answer
    # aloud. Uses the OS speech engine (Windows SAPI via PowerShell) — no extra dependency, no network.
    voice_enabled: bool = False
    voice_name: str = ""
    #: Words per minute delta from the engine default (-10..10).
    voice_rate: int = 0
    voice_max_chars: int = 1200

    # --- speech-to-text (CrisperWhisper) ---------------------------------------
    # Local transcription of microphone audio, running in this process. Audio never leaves the
    # machine; nothing is transcribed until the checkpoint is downloaded once from Hugging Face.
    #
    # The default is Whisper *small* (244M params, MIT, native CTranslate2 build): the same size
    # class as CrisperWhisper 2.0's small checkpoint but loadable by faster-whisper as-is, and a
    # fraction of the 3 GB CrisperWhisper 1.0 build this used to default to. Voice commands are
    # short utterances; small keeps the download, RAM (~250 MB at int8) and latency low.
    # CrisperWhisper (verbatim, [UM]/[UH]) stays available — its checkpoints must be CT2 builds
    # (`nyralabs/faster_CrisperWhisper`, or convert `CrisperWhisper2.0_*` yourself with
    # `pip install "crisperwhisper[convert]"`); the transformers safetensors will not load.
    transcriber_enabled: bool = True
    #: CTranslate2 model repo id (or a local directory) loadable by faster-whisper.
    transcriber_model: str = "Systran/faster-whisper-small"
    #: "auto" prefers CUDA, then falls back to CPU if the card cannot load *or* run the checkpoint.
    #: Override with "cuda" or "cpu" to be literal (an explicit device is never silently ignored).
    transcriber_device: str = "auto"
    #: ctranslate2 compute type; "auto" is sensible. e.g. "int8_float16" on GPU, "int8" on CPU.
    transcriber_compute_type: str = "auto"
    #: Threads for CPU inference. 0 = every logical core (faster-whisper would use 4).
    transcriber_cpu_threads: int = 0

    # --- voice agent: the live call ---------------------------------------------------------------
    # A full-duplex conversation in the shape of huggingface/speech-to-speech's cascade, run
    # in-process so every stage streams into the next:
    #   mic frames → VAD → local Whisper → streaming LLM → Sarvam TTS sentence by sentence
    # The panel plays the audio as it arrives and can interrupt mid-word (barge-in cancels the LLM
    # stream and the TTS request, exactly like that repo's `turn_detected` path).
    voice_call_enabled: bool = True
    # VAD: Silero v5 ONNX (2.3 MB, downloaded once into the models dir). Without onnxruntime — or
    # if the model cannot be fetched — an adaptive energy detector does the same job, worse in
    # noise; the call says which one is live instead of pretending they are equal.
    voice_vad_model_url: str = (
        "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"
    )
    voice_vad_threshold: float = 0.5
    #: Silence that ends a turn — long enough to keep a speaker's mid-sentence pause inside it.
    voice_vad_silence_ms: int = 550
    #: Audio kept from before the trigger so the first consonant is not clipped off the transcript.
    voice_vad_pad_ms: int = 160

    # --- voice agent TTS: Sarvam AI (bulbul) -------------------------------------------------------
    # The key lives in `.env` (or Sites/Settings secrets) and is only ever read by this process —
    # no window, log line or asset sees it. New Sarvam accounts carry ₹100 of credits.
    sarvam_api_key: str = ""
    sarvam_tts_base_url: str = "https://api.sarvam.ai"
    sarvam_tts_model: str = "bulbul:v4-flash"
    #: Empty = the model's documented default persona; set an id from the Sarvam voices page.
    sarvam_tts_voice: str = ""
    sarvam_tts_language: str = "en-IN"
    #: Streaming supports 8/16/22.05/24 kHz; 24000 matches the panel's playback context.
    sarvam_tts_sample_rate: int = 24000

    # --- voice agent LLM ----------------------------------------------------------------------------
    # Defaults to the reasoning stack — the NVIDIA Nemotron models the agent already plans with.
    # To take the call straight to NVIDIA's own endpoint instead:
    #   VOICE_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
    #   VOICE_LLM_API_KEY=<key from build.nvidia.com/settings/api-keys>
    #   VOICE_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b
    voice_llm_base_url: str = ""
    voice_llm_api_key: str = ""
    #: Empty = NEMOTRON_SUPER_MODEL (nvidia/nemotron-3-super-120b-a12b).
    voice_llm_model: str = ""
    #: Spoken answers are short: 220 tokens is a few sentences, and stopping early is latency won.
    voice_llm_max_tokens: int = 220
    voice_llm_temperature: float = 0.5

    # --- local API auth -------------------------------------------------------
    # Empty means the loopback API is open (the default, for a single-user laptop). Set a token to
    # require `Authorization: Bearer <token>` on every request except /health. Generate one with
    # `python -m app.selftest --new-token`.
    dobot_api_token: str = ""
    # --- multi-user (opt-in) --------------------------------------------------
    # false (default): one person, one `.env`, everything exactly as before.
    # true: /auth/register + /auth/login issue per-user API tokens; each user's memory, tasks,
    # approvals, activity and API keys are isolated (see app/core/tenancy.py). The first
    # registered user becomes the owner; later users need an owner invite unless
    # multi_user_open_registration is true. DOBOT_API_TOKEN keeps working as the operator gate.
    multi_user: bool = False
    multi_user_open_registration: bool = False
    # --- screen / ocr --------------------------------------------------------
    screen_capture: str = "on_demand"
    ocr_enabled: bool = True
    ocr_language: str = "eng"

    # --- derived -------------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        """Directory for local state (file store, screenshots, logs)."""
        path = Path(self.dobot_state_dir or ".dobot")
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_dir(self) -> Path:
        """Stable app-owned data directory for durable user state."""
        path = self.state_dir / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        """Rotating-log location shared by the backend and the desktop diagnostics UI."""
        path = self.state_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def models_dir(self) -> Path:
        """Downloaded local model cache; never place large mutable files beside the installer."""
        path = self.state_dir / "models"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def skills_dir(self) -> Path:
        configured = (self.dobot_skills_dir or "").strip()
        if not configured:
            # Installed app: the bundler copies skills/ next to the executable (writable, so the
            # Skills page can manage them). Standalone frozen run: skills live inside the exe
            # (PyInstaller --add-data). Source checkout: the repo folder.
            local = REPO_ROOT / "skills"
            if local.is_dir():
                return local
            if getattr(sys, "frozen", False):
                bundled = Path(getattr(sys, "_MEIPASS", "")) / "skills"
                if bundled.is_dir():
                    return bundled
            return local
        path = Path(configured)
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def protected_paths_resolved(self) -> list[Path]:
        """Locations the file guard never lets a tool touch (reads included)."""
        resolved: list[Path] = []
        for raw in _split_csv(self.protected_paths):
            try:
                resolved.append(Path(os.path.expanduser(raw)).resolve())
            except OSError:  # pragma: no cover - defensive
                continue
        return resolved

    @property
    def protected_path_list(self) -> list[str]:
        return _split_csv(self.protected_paths)

    @property
    def skill_allowlist(self) -> list[str]:
        return _split_csv(self.skill_scan_allowlist)

    @property
    def allowed_paths(self) -> list[Path]:
        resolved: list[Path] = []
        for raw in _split_csv(self.sandbox_allowed_paths):
            path = Path(os.path.expanduser(raw))
            try:
                resolved.append(path.resolve())
            except OSError:  # pragma: no cover - defensive
                continue
        return resolved

    @property
    def allowed_hosts(self) -> list[str]:
        return [host.lower() for host in _split_csv(self.sandbox_allowed_network)]

    @property
    def hermes_tools(self) -> list[str]:
        return _split_csv(self.hermes_allowed_tools)

    @property
    def has_nebius(self) -> bool:
        return bool(self.nebius_api_key.strip())

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key.strip())

    @property
    def has_novita(self) -> bool:
        return bool(self.novita_api_key.strip())

    @property
    def secret_values(self) -> list[str]:
        """Secrets that must be redacted from logs."""
        return [
            value
            for value in (
                self.nebius_api_key,
                self.tavily_api_key,
                self.novita_api_key,
                self.dobot_api_token,
            )
            if value
        ]

    @property
    def api_auth_required(self) -> bool:
        return bool(self.dobot_api_token.strip())

    @property
    def pricing_configured(self) -> bool:
        return self.price_per_mtok_input > 0 or self.price_per_mtok_output > 0

    def estimate_cost_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        """Estimated spend for one model call. Returns 0.0 when no price is configured."""
        if not self.pricing_configured:
            return 0.0
        million = 1_000_000
        return round(
            input_tokens / million * self.price_per_mtok_input
            + output_tokens / million * self.price_per_mtok_output,
            6,
        )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("agent_runtime")
    @classmethod
    def _known_runtime(cls, value: str) -> str:
        allowed = {"local", "cli", "remote", "api", "hermes", "auto"}
        value = value.lower()
        return value if value in allowed else "local"

    @field_validator("reasoning_provider")
    @classmethod
    def _known_reasoning_provider(cls, value: str) -> str:
        value = value.lower()
        return value if value in {"cloud", "local", "hybrid"} else "cloud"

    @field_validator("sandbox_provider")
    @classmethod
    def _known_sandbox(cls, value: str) -> str:
        allowed = {"local", "nemoclaw"}
        value = value.lower()
        return value if value in allowed else "local"

    @field_validator("execution_mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        allowed = {"ask", "assist", "agent"}
        value = value.strip().lower()
        return value if value in allowed else "agent"

    @field_validator("skill_scan_mode")
    @classmethod
    def _known_scan_mode(cls, value: str) -> str:
        allowed = {"block", "warn", "off"}
        value = value.strip().lower()
        return value if value in allowed else "block"

    @field_validator("jev_thinking")
    @classmethod
    def _known_jev_thinking(cls, value: str) -> str:
        allowed = {"off", "auto", "on"}
        value = value.strip().lower()
        return value if value in allowed else "auto"

    @field_validator("jev_reasoning_effort")
    @classmethod
    def _known_jev_effort(cls, value: str) -> str:
        # The model rejects `high` outright — only these three exist.
        allowed = {"xhigh", "medium", "low"}
        value = value.strip().lower()
        return value if value in allowed else "medium"

    @field_validator("voice_rate")
    @classmethod
    def _clamp_voice_rate(cls, value: int) -> int:
        return max(-10, min(10, int(value)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the cached settings so environment changes take effect."""
    get_settings.cache_clear()
