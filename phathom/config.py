"""Application settings, loaded from .env (see SPEC §4)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    GROQ_API_KEY: str = ""
    OWNER_NAME: str = "Daniyal Areeb"
    OWNER_SHORT_NAME: str = "Daniyal"
    DEFAULT_MODE: str = "assistant"
    ANSWER_DELAY_S: int = 6
    MAX_CALL_S: int = 600
    SILENCE_HANGUP_S: int = 45
    LISTEN_LIVE: bool = False
    DISCLOSURE: bool = True
    STT_MODEL_LIVE: str = "whisper-large-v3-turbo"
    STT_MODEL_FINAL: str = "whisper-large-v3"
    LLM_MODEL_LIVE: str = "openai/gpt-oss-20b"
    LLM_MODEL_SMART: str = "qwen/qwen3.8-27b"
    TTS_ENGINE: str = "edge"
    TTS_VOICE_EN: str = "en-US-AndrewNeural"
    TTS_VOICE_UR: str = "ur-PK-AsadNeural"
    GROQ_TTS_VOICE: str = "troy"
    # Embedded by default: the DB runs inside `phathom serve` from data/db (no
    # separate process). A ws:// URL still works for an external SurrealDB.
    SURREAL_URL: str = "surrealkv://data/db"
    SURREAL_USER: str = "root"
    SURREAL_PASS: str = "root"
    SURREAL_NS: str = "phathom"
    SURREAL_DB: str = "main"
    CHROME_CHANNEL: str = "chrome"
    KEEP_AUDIO_DAYS: int = 30
    # Off: audio is used only to transcribe + summarise, then deleted. Calls keep
    # their summary, notes and transcript; a failed call keeps its audio so
    # Reprocess can retry.
    KEEP_RECORDINGS: bool = False
    # --- v2 server (EXTENSION_SPEC §5.6, §6.5, §6.6) ---
    EXTENSION_ID: str = ""          # pinned from the first valid connection if unset
    SUMMARY_MAX_TOKENS: int = 900   # Groq free-tier output TPM guard (§6.6)
    LIVE_TRANSCRIPT: bool = True
    SIDE_PANEL_AUTO: bool = True
    OWNER_CALL_MAX_S: int = 3600    # copilot cap for the owner's own calls
    # --- v2 caller audio (§5.7) + talk mode (§5.3a/B4) ---
    CALLER_SOURCE: str = "auto"       # auto | webrtc | speaker_tap | tab_capture
    TALK_MODE_ENABLED: bool = True    # assistant talk behind a setting (B4)
    TEST_TONE_HEARD: bool = False     # user confirmed the bot path in Diagnostics (B4)


settings = Settings()
