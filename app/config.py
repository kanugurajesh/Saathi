"""Central configuration, read from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- storage ---
    mongodb_uri: str = os.getenv("MONGODB_URI", "")  # empty -> in-memory mongomock
    mongodb_db: str = os.getenv("MONGODB_DB", "hextgen_automation")

    # --- LLM (Gemini 2.5 Flash, via Vertex AI or AI Studio key) ---
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    use_vertex: bool = _bool("GOOGLE_GENAI_USE_VERTEXAI")
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    llm_disabled: bool = _bool("LLM_DISABLED")  # force the deterministic engine (tests/evals)

    # --- portal automation ---
    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "http://127.0.0.1:8100")
    headless: bool = _bool("PLAYWRIGHT_HEADLESS", True)
    otp_auto_wait_s: float = float(os.getenv("OTP_AUTO_WAIT_S", "6"))
    otp_human_wait_s: float = float(os.getenv("OTP_HUMAN_WAIT_S", "120"))
    sync_interval_min: float = float(os.getenv("SYNC_INTERVAL_MINUTES", "0"))  # 0 = manual only
    artifacts_dir: Path = ROOT / "runs"

    # --- SLAs (IRDAI Master Circular on Health Insurance, 29-May-2024) ---
    sla_preauth_min: int = int(os.getenv("SLA_PREAUTH_MIN", "60"))  # cashless decision
    sla_discharge_min: int = int(os.getenv("SLA_DISCHARGE_MIN", "180"))  # final authorisation
    sla_query_reply_min: int = int(os.getenv("SLA_QUERY_REPLY_MIN", "120"))  # hospital-side target

    # --- alerts ---
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "")  # Slack / Google Chat compatible

    # --- HMS integration: claim events pushed to the hospital's HMS (e.g. HextGen IPD billing) ---
    hms_webhook_url: str = os.getenv("HMS_WEBHOOK_URL", "")  # blank = events only recorded in hms_outbox
    hms_webhook_secret: str = os.getenv("HMS_WEBHOOK_SECRET", "")  # HMAC-SHA256 signing key

    # --- receivables + daily WhatsApp summary (Meta WhatsApp Cloud API; blank token = dry run to outbox) ---
    hospital_name: str = os.getenv("HOSPITAL_NAME", "Godavari Demo Multispeciality Hospital, Nalgonda")
    dashboard_url: str = os.getenv("DASHBOARD_URL", "http://127.0.0.1:8000/#receivables")
    whatsapp_token: str = os.getenv("WHATSAPP_TOKEN", "")
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    whatsapp_owner_to: str = os.getenv("WHATSAPP_OWNER_TO", "")  # E.164 digits, e.g. 919876500011
    whatsapp_template: str = os.getenv("WHATSAPP_TEMPLATE", "receivables_daily_summary")
    whatsapp_template_lang: str = os.getenv("WHATSAPP_TEMPLATE_LANG", "en")
    whatsapp_api_version: str = os.getenv("WHATSAPP_API_VERSION", "v23.0")
    receivables_summary_at: str = os.getenv("RECEIVABLES_SUMMARY_AT", "")  # "08:00" IST; blank = send manually only

    portal_credentials: dict = field(default_factory=lambda: {
        "nimbus": {"username": os.getenv("NIMBUS_USER", "godavari_tpa"),
                       "password": os.getenv("NIMBUS_PASS", "demo@123")},
        "orbit": {"hospital_code": os.getenv("ORBIT_HOSP", "HSP-TS-0420"),
                     "username": os.getenv("ORBIT_USER", "tpadesk"),
                     "password": os.getenv("ORBIT_PASS", "demo@123"),
                     "otp_phone": os.getenv("ORBIT_OTP_PHONE", "9876500011")},
        "lotus": {"username": os.getenv("LOTUS_USER", "godavari.billing"),
                    "password": os.getenv("LOTUS_PASS", "demo@123")},
    })


settings = Settings()

