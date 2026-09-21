import os
import re
import time
import asyncio
import hashlib
import secrets
import bcrypt
from dotenv import load_dotenv
from app.database import (
    execute as db_execute,
    fetch as db_fetch,
    fetchrow as db_fetchrow,
    get_lifetime_stats,
    persist_request_log,
)

load_dotenv()


def reload_models_from_env():
    """Re-read model lists from .env after they've been updated via /api/models/refresh."""
    load_dotenv(override=True)

    global BLUESMINDS_MODELS_RAW, NARA_MODELS_RAW
    global DAHL_MODELS_RAW, QWEN_CLOUD_MODELS_RAW, MARKETKU_MODELS_RAW
    global KIMCHI_MODELS_RAW, ATOMESUS_MODELS_RAW, WEIZE_MODELS_RAW
    global BLUESMINDS_MODELS, NARA_MODELS
    global DAHL_MODELS, QWEN_CLOUD_MODELS, MARKETKU_MODELS
    global KIMCHI_MODELS, ATOMESUS_MODELS, WEIZE_MODELS
    global DAHL_MODELS_SHORT, DAHL_MODEL_MAP

    BLUESMINDS_MODELS_RAW = os.getenv("BLUESMINDS_MODELS", "")
    NARA_MODELS_RAW = os.getenv("NARA_MODELS", "")
    DAHL_MODELS_RAW = os.getenv("DAHL_MODELS", "")
    QWEN_CLOUD_MODELS_RAW = os.getenv("QWEN_CLOUD_MODELS", "")
    MARKETKU_MODELS_RAW = os.getenv("MARKETKU_MODELS", "")
    KIMCHI_MODELS_RAW = os.getenv("KIMCHI_MODELS", "")
    ATOMESUS_MODELS_RAW = os.getenv("ATOMESUS_MODELS", "")
    WEIZE_MODELS_RAW = os.getenv("WEIZE_MODELS", "")

    BLUESMINDS_MODELS = [m.strip() for m in BLUESMINDS_MODELS_RAW.split(",") if m.strip()]
    NARA_MODELS = [m.strip() for m in NARA_MODELS_RAW.split(",") if m.strip()]
    DAHL_MODELS = [m.strip() for m in DAHL_MODELS_RAW.split(",") if m.strip()]
    QWEN_CLOUD_MODELS = [m.strip() for m in QWEN_CLOUD_MODELS_RAW.split(",") if m.strip()]
    MARKETKU_MODELS = [m.strip() for m in MARKETKU_MODELS_RAW.split(",") if m.strip()]
    KIMCHI_MODELS = [m.strip() for m in KIMCHI_MODELS_RAW.split(",") if m.strip()]
    ATOMESUS_MODELS = [m.strip() for m in ATOMESUS_MODELS_RAW.split(",") if m.strip()]
    WEIZE_MODELS = [m.strip() for m in WEIZE_MODELS_RAW.split(",") if m.strip()]

    DAHL_MODELS_SHORT = [m.split("/", 1)[-1] if "/" in m else m for m in DAHL_MODELS]
    DAHL_MODEL_MAP = dict(zip(DAHL_MODELS_SHORT, DAHL_MODELS))

DEFAULT_UPSTREAM_URL_RAW = os.getenv("DEFAULT_UPSTREAM_URL")
if not DEFAULT_UPSTREAM_URL_RAW:
    raise ValueError("DEFAULT_UPSTREAM_URL environment variable is not set")

# Models configuration
BLUESMINDS_MODELS_RAW = os.getenv("BLUESMINDS_MODELS", "")
NARA_MODELS_RAW = os.getenv("NARA_MODELS", "")
DAHL_MODELS_RAW = os.getenv("DAHL_MODELS", "")
QWEN_CLOUD_MODELS_RAW = os.getenv("QWEN_CLOUD_MODELS", "")
MARKETKU_MODELS_RAW = os.getenv("MARKETKU_MODELS", "")
KIMCHI_MODELS_RAW = os.getenv("KIMCHI_MODELS", "")
ATOMESUS_MODELS_RAW = os.getenv("ATOMESUS_MODELS", "")
WEIZE_MODELS_RAW = os.getenv("WEIZE_MODELS", "")

BLUESMINDS_MODELS = [m.strip() for m in BLUESMINDS_MODELS_RAW.split(",") if m.strip()]
NARA_MODELS = [m.strip() for m in NARA_MODELS_RAW.split(",") if m.strip()]
DAHL_MODELS = [m.strip() for m in DAHL_MODELS_RAW.split(",") if m.strip()]
QWEN_CLOUD_MODELS = [m.strip() for m in QWEN_CLOUD_MODELS_RAW.split(",") if m.strip()]
MARKETKU_MODELS = [m.strip() for m in MARKETKU_MODELS_RAW.split(",") if m.strip()]
KIMCHI_MODELS = [m.strip() for m in KIMCHI_MODELS_RAW.split(",") if m.strip()]
ATOMESUS_MODELS = [m.strip() for m in ATOMESUS_MODELS_RAW.split(",") if m.strip()]
WEIZE_MODELS = [m.strip() for m in WEIZE_MODELS_RAW.split(",") if m.strip()]
# Short display names: strip vendor prefixes so upstream "moonshotai/Kimi-K2.6" becomes "Kimi-K2.6"
DAHL_MODELS_SHORT = [m.split("/", 1)[-1] if "/" in m else m for m in DAHL_MODELS]
DAHL_MODEL_MAP = dict(zip(DAHL_MODELS_SHORT, DAHL_MODELS))

ROUTER_DOMAIN = os.getenv("ROUTER_DOMAIN", "localhost")

BLUESMINDS_API_KEY = os.getenv("BLUESMINDS_API_KEY")
NARA_API_KEYS_RAW = os.getenv("NARA_API_KEYS", "")
NARA_API_KEYS_ENV = [k.strip() for k in NARA_API_KEYS_RAW.split(",") if k.strip()]
DAHL_API_KEYS_RAW = os.getenv("DAHL_API_KEYS", "")
DAHL_API_KEYS_ENV = [k.strip() for k in DAHL_API_KEYS_RAW.split(",") if k.strip()]
QWEN_CLOUD_API_KEYS_RAW = os.getenv("QWEN_CLOUD_API_KEYS", "")
QWEN_CLOUD_API_KEYS_ENV = [k.strip() for k in QWEN_CLOUD_API_KEYS_RAW.split(",") if k.strip()]
MARKETKU_API_KEYS_RAW = os.getenv("MARKETKU_API_KEYS", os.getenv("MARKETKU_API_KEY", ""))
MARKETKU_API_KEYS_ENV = [k.strip() for k in MARKETKU_API_KEYS_RAW.split(",") if k.strip()]

# Per-model fallback order for Qwen Cloud when ALL keys have exhausted the requested model
QC_FALLBACK_ORDER_RAW = os.getenv("QC_FALLBACK_ORDER", "qwen3.7-max,qwen-max,qwen-plus,deepseek-v3.2,glm-5.2,kimi-k2.7-code,qwen-turbo")
QC_FALLBACK_ORDER = [m.strip() for m in QC_FALLBACK_ORDER_RAW.split(",") if m.strip()]

DAHL_BASE_URL = os.getenv("DAHL_BASE_URL", "https://inference.dahl.global/v1").rstrip("/")
QWEN_CLOUD_BASE_URL = os.getenv("QWEN_CLOUD_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/")
MARKETKU_BASE_URL = os.getenv("MARKETKU_BASE_URL", "https://router.marketku.id/v1").rstrip("/")
ATOMESUS_BASE_URL = os.getenv("ATOMESUS_BASE_URL", "https://api.atomesus.com/v1").rstrip("/")
WEIZE_BASE_URL = os.getenv("WEIZE_BASE_URL", "https://weizerouter.web.id/v1").rstrip("/")

ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD")
PORT_STR = os.getenv("PORT")
SSL_KEYFILE = os.getenv("SSL_KEYFILE")
SSL_CERTFILE = os.getenv("SSL_CERTFILE")

DEFAULT_UPSTREAM_URL = DEFAULT_UPSTREAM_URL_RAW.rstrip("/")
BLUESMINDS_BASE_URL = os.getenv("BLUESMINDS_BASE_URL", "https://api.bluesminds.com/v1").rstrip("/")
NARA_BASE_URL = os.getenv("NARA_BASE_URL", "https://router.bynara.id/v1").rstrip("/")
DAHL_BASE_URL = os.getenv("DAHL_BASE_URL", "https://inference.dahl.global/v1").rstrip("/")
QWEN_CLOUD_BASE_URL = os.getenv("QWEN_CLOUD_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/")
SHOW_REASONING = os.getenv("SHOW_REASONING", "true").lower() == "true"
AUGMENT_SYSTEM_PROMPT = os.getenv("AUGMENT_SYSTEM_PROMPT", "false").lower() == "true"
# Rotate key proactively if time-to-first-token exceeds this (ms). 0 = disabled.
SLOW_RESPONSE_THRESHOLD_MS = int(os.getenv("SLOW_RESPONSE_THRESHOLD_MS", "10000"))
# Minutes before a "Limited" key is automatically reset to "Standby". 0 = disabled.
LIMIT_COOLDOWN_MINUTES = int(os.getenv("LIMIT_COOLDOWN_MINUTES", "60"))
# Conversation Memory Configuration
CONVERSATION_MEMORY_ENABLED = os.getenv("CONVERSATION_MEMORY_ENABLED", "true").lower() == "true"
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MEMORY_RETENTION_DAYS = int(os.getenv("MEMORY_RETENTION_DAYS", "30"))

if not PORT_STR:
    raise ValueError("PORT environment variable is not set")

PORT = int(PORT_STR)

# Admin credentials - hash if plaintext, or load hash from env
_ADMIN_HASH = os.getenv("ADMIN_PASSWORD_HASH")
if _ADMIN_HASH:
    ADMIN_PASSWORD_HASH = _ADMIN_HASH.encode()
else:
    _raw_password = os.getenv("ADMIN_PASSWORD")
    if not _raw_password:
        raise ValueError("ADMIN_PASSWORD environment variable must be set on first run to generate hash.")
    ADMIN_PASSWORD_HASH = bcrypt.hashpw(_raw_password.encode(), bcrypt.gensalt())
    print("[SECURITY] ADMIN_PASSWORD_HASH is not set; using an in-memory hash for this process.")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "iyanadmin")

SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48)
if not os.getenv("SESSION_SECRET"):
    print("[SECURITY] SESSION_SECRET is not set; generated an ephemeral secret for this process.")

# In-memory state (primary for fast access, DB is persistence)
BM_API_KEYS = []
NR_API_KEYS = []
DAHL_API_KEYS = []
QC_API_KEYS = []
MARKETKU_API_KEYS = []

key_statuses = {}
key_limited_at: dict[str, float] = {}  # key_value -> time.time() when marked Limited
total_requests = 0
total_tokens = 0
failover_count = 0
recent_requests = []
START_TIME = time.time()
current_bm_key_index = 0
current_nr_key_index = 0
current_dahl_key_index = 0
current_qc_key_index = 0
current_marketku_key_index = 0

# A persisted maintenance gate used only for planned operations such as a
# database/server cutover. It lets existing streaming responses finish while
# preventing new inference requests from creating writes on the old primary.
MIGRATION_DRAIN_ENABLED = False
active_inference_requests = 0

# Qwen Cloud per-model key state
# model_short -> current key index for that model
qc_model_key_index: dict[str, int] = {}
# key_value -> {model_short: exhausted?}
qc_model_failures: dict[str, dict[str, bool]] = {}

BUILTIN_PROVIDER_PREFIXES = {"bm", "nry", "dahl", "qc", "marketku"}
BUILTIN_PROVIDER_NAMES = {
    "bm": "Bluesminds", "nry": "byNara",
    "dahl": "Dahl", "qc": "Qwen Cloud", "marketku": "MarketKu",
}
BUILTIN_PROVIDER_BASE_URLS = {
    "bm": BLUESMINDS_BASE_URL,
    "nry": NARA_BASE_URL, "dahl": DAHL_BASE_URL, "qc": QWEN_CLOUD_BASE_URL,
    "marketku": MARKETKU_BASE_URL,
}

# prefix -> provider URL, protocol, authentication header, version, and models
CUSTOM_PROVIDERS: dict[str, dict] = {}
# prefix -> [key_value, ...]
CUSTOM_PROVIDER_KEYS: dict[str, list] = {}
# prefix -> current rotation index into CUSTOM_PROVIDER_KEYS[prefix]
custom_key_index: dict[str, int] = {}
# built-in provider prefixes the admin has removed (see disabled_providers table)
DISABLED_PROVIDERS: set = set()


def _bg(coro):
    async def _wrapped():
        try:
            await coro
        except Exception as e:
            print(f"[BG ERROR] {e}")
    try:
        asyncio.create_task(_wrapped())
    except RuntimeError:
        pass


async def init_state_from_db():
    global BM_API_KEYS, NR_API_KEYS, DAHL_API_KEYS, QC_API_KEYS, MARKETKU_API_KEYS, key_statuses, total_requests, total_tokens, failover_count, current_bm_key_index, current_nr_key_index, current_dahl_key_index, current_qc_key_index, current_marketku_key_index, START_TIME, CUSTOM_PROVIDERS, CUSTOM_PROVIDER_KEYS, DISABLED_PROVIDERS, MIGRATION_DRAIN_ENABLED

    BM_API_KEYS.clear()
    NR_API_KEYS.clear()
    DAHL_API_KEYS.clear()
    QC_API_KEYS.clear()
    MARKETKU_API_KEYS.clear()
    key_statuses.clear()
    qc_model_key_index.clear()
    qc_model_failures.clear()

    # Load custom (admin-added) providers and disabled built-ins first, so the
    # key-loading loop below knows where to put keys for a custom provider
    # instead of dumping unrecognized providers into the default kc bucket.
    from app.database import get_custom_providers, get_disabled_providers
    CUSTOM_PROVIDERS.clear()
    CUSTOM_PROVIDER_KEYS.clear()
    for row in await get_custom_providers():
        CUSTOM_PROVIDERS[row["prefix"]] = {
            "name": row["name"], "base_url": row["base_url"].rstrip("/"), "api_format": row["api_format"],
            "auth_header": row["auth_header"],
            "anthropic_version": row["anthropic_version"],
            "models": [m.strip() for m in (row["models"] or "").split(",") if m.strip()],
        }
        CUSTOM_PROVIDER_KEYS[row["prefix"]] = []
    DISABLED_PROVIDERS = await get_disabled_providers()
    drain_row = await db_fetchrow(
        "SELECT value FROM server_config WHERE key = 'migration_drain_enabled'"
    )
    MIGRATION_DRAIN_ENABLED = bool(drain_row and drain_row["value"] == "true")

    # Restore counters once at startup. Dashboard status can then stay on the
    # in-memory fast path instead of aggregating the whole log table per poll.
    lifetime = await get_lifetime_stats()
    total_requests = int(lifetime["total_requests"])
    total_tokens = int(lifetime["total_tokens"])
    failover_count = int(lifetime["total_rotations"])

    # Load keys from DB
    rows = await db_fetch("SELECT key_value, key_prefix, status, provider FROM api_keys ORDER BY id")
    if rows:
        for r in rows:
            val = r["key_value"]
            provider = r.get("provider", "kc")
            # Auto-correct mis-classified nry keys (sk-nry- prefix stored as 'kc')
            if provider != "nry" and val.startswith("sk-nry-"):
                provider = "nry"
                _bg(db_execute("UPDATE api_keys SET provider='nry' WHERE key_value=$1", val))
            if provider == "bm":
                BM_API_KEYS.append(val)
            elif provider == "nry":
                NR_API_KEYS.append(val)
            elif provider == "dahl":
                DAHL_API_KEYS.append(val)
            elif provider == "qc":
                QC_API_KEYS.append(val)
            elif provider == "marketku":
                MARKETKU_API_KEYS.append(val)
            elif provider in CUSTOM_PROVIDERS:
                CUSTOM_PROVIDER_KEYS[provider].append(val)
            key_statuses[val] = r["status"]
    # Always ensure BM key from env is seeded and present
    bm_key = BLUESMINDS_API_KEY
    if bm_key and bm_key not in BM_API_KEYS:
        BM_API_KEYS.append(bm_key)
        key_statuses[bm_key] = "Active"
        prefix = bm_key[:15] + "..." if len(bm_key) > 15 else bm_key
        await db_execute(
            "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, $3, 'bm') ON CONFLICT DO NOTHING",
            bm_key, prefix, key_statuses[bm_key]
        )

    # Seed byNara keys from env if not already present
    for nr_key in NARA_API_KEYS_ENV:
        if nr_key not in NR_API_KEYS:
            NR_API_KEYS.append(nr_key)
            key_statuses[nr_key] = "Standby"
            prefix = nr_key[:15] + "..." if len(nr_key) > 15 else nr_key
            await db_execute(
                "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, $3, 'nry') ON CONFLICT (key_value) DO UPDATE SET provider='nry'",
                nr_key, prefix, "Standby"
            )

    # Seed Dahl keys from env if not already present
    for dahl_key in DAHL_API_KEYS_ENV:
        if dahl_key not in DAHL_API_KEYS:
            DAHL_API_KEYS.append(dahl_key)
            key_statuses[dahl_key] = "Standby"
            prefix = dahl_key[:15] + "..." if len(dahl_key) > 15 else dahl_key
            await db_execute(
                "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, $3, 'dahl') ON CONFLICT (key_value) DO UPDATE SET provider='dahl'",
                dahl_key, prefix, "Standby"
            )

    # Seed Qwen Cloud keys from env if not already present
    for qc_key in QWEN_CLOUD_API_KEYS_ENV:
        if qc_key not in QC_API_KEYS:
            QC_API_KEYS.append(qc_key)
            key_statuses[qc_key] = "Standby"
            prefix = qc_key[:15] + "..." if len(qc_key) > 15 else qc_key
            await db_execute(
                "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, $3, 'qc') ON CONFLICT (key_value) DO UPDATE SET provider='qc'",
                qc_key, prefix, "Standby"
            )

    # A QC quota belongs to a key/model pair. Restore those pairs so a
    # process restart or deployment does not retry already exhausted slots.
    exhausted_rows = await db_fetch(
        "SELECT key_value, model FROM qc_model_exhaustions ORDER BY exhausted_at"
    )
    for row in exhausted_rows:
        key = row["key_value"]
        if key in QC_API_KEYS:
            qc_model_failures.setdefault(key, {})[row["model"]] = True

    # Seed MarketKu keys from env
    for mk_key in MARKETKU_API_KEYS_ENV:
        if mk_key not in MARKETKU_API_KEYS:
            MARKETKU_API_KEYS.append(mk_key)
            key_statuses[mk_key] = "Standby"
            prefix = mk_key[:15] + "..." if len(mk_key) > 15 else mk_key
            await db_execute(
                "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, $3, 'marketku') ON CONFLICT (key_value) DO UPDATE SET provider='marketku'",
                mk_key, prefix, "Standby"
            )

    for i, k in enumerate(BM_API_KEYS):
        if key_statuses.get(k) == "Active":
            current_bm_key_index = i
            break
    else:
        if BM_API_KEYS:
            current_bm_key_index = 0
            key_statuses[BM_API_KEYS[0]] = "Active"
            await db_execute(
                "UPDATE api_keys SET status = 'Active' WHERE key_value = $1",
                BM_API_KEYS[0]
            )

    for i, k in enumerate(NR_API_KEYS):
        if key_statuses.get(k) == "Active":
            current_nr_key_index = i
            break
    else:
        if NR_API_KEYS:
            current_nr_key_index = 0
            key_statuses[NR_API_KEYS[0]] = "Active"
            await db_execute(
                "UPDATE api_keys SET status = 'Active' WHERE key_value = $1",
                NR_API_KEYS[0]
            )

    for i, k in enumerate(DAHL_API_KEYS):
        if key_statuses.get(k) == "Active":
            current_dahl_key_index = i
            break
    else:
        if DAHL_API_KEYS:
            current_dahl_key_index = 0
            key_statuses[DAHL_API_KEYS[0]] = "Active"
            await db_execute(
                "UPDATE api_keys SET status = 'Active' WHERE key_value = $1",
                DAHL_API_KEYS[0]
            )

    for i, k in enumerate(QC_API_KEYS):
        if key_statuses.get(k) == "Active":
            current_qc_key_index = i
            break
    else:
        if QC_API_KEYS:
            current_qc_key_index = 0
            key_statuses[QC_API_KEYS[0]] = "Active"
            await db_execute(
                "UPDATE api_keys SET status = 'Active' WHERE key_value = $1",
                QC_API_KEYS[0]
            )

    for i, k in enumerate(MARKETKU_API_KEYS):
        if key_statuses.get(k) == "Active":
            current_marketku_key_index = i
            break
    else:
        if MARKETKU_API_KEYS:
            current_marketku_key_index = 0
            key_statuses[MARKETKU_API_KEYS[0]] = "Active"
            await db_execute(
                "UPDATE api_keys SET status = 'Active' WHERE key_value = $1",
                MARKETKU_API_KEYS[0]
            )

    custom_key_total = sum(len(v) for v in CUSTOM_PROVIDER_KEYS.values())
    print(f"[INIT] Loaded {len(BM_API_KEYS)} bm / {len(NR_API_KEYS)} nry / {len(DAHL_API_KEYS)} dahl / {len(QC_API_KEYS)} qc / {len(MARKETKU_API_KEYS)} marketku / {custom_key_total} custom ({len(CUSTOM_PROVIDERS)} providers) keys, {total_requests} total requests, {failover_count} failovers from DB, {len(DISABLED_PROVIDERS)} disabled providers")


def migration_status() -> dict:
    return {
        "draining": MIGRATION_DRAIN_ENABLED,
        "active_inference_requests": active_inference_requests,
    }


async def set_migration_drain(enabled: bool) -> dict:
    """Persist a cutover gate so a restart cannot accidentally reopen writes."""
    global MIGRATION_DRAIN_ENABLED
    MIGRATION_DRAIN_ENABLED = enabled
    await db_execute(
        "INSERT INTO server_config (key, value) VALUES ('migration_drain_enabled', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        "true" if enabled else "false",
    )
    return migration_status()


async def auto_reset_limited_keys():
    """Reset keys that have been Limited for longer than LIMIT_COOLDOWN_MINUTES."""
    if LIMIT_COOLDOWN_MINUTES <= 0:
        return []
    now = time.time()
    cooldown_secs = LIMIT_COOLDOWN_MINUTES * 60
    reset_keys = []
    for key, limited_at in list(key_limited_at.items()):
        if now - limited_at >= cooldown_secs and key_statuses.get(key) == "Limited":
            key_statuses[key] = "Standby"
            await db_execute("UPDATE api_keys SET status = 'Standby' WHERE key_value = $1", key)
            del key_limited_at[key]
            reset_qc_model_failures(key)
            reset_keys.append(key[:15] + "...")
    if reset_keys:
        print(f"[AUTO-RESET] Auto-reset {len(reset_keys)} Limited key(s) to Standby: {reset_keys}")
    return reset_keys


def get_current_bm_key():
    if not BM_API_KEYS:
        return BLUESMINDS_API_KEY
    return BM_API_KEYS[current_bm_key_index]


def rotate_bm_key(reason: str = "Limited"):
    global current_bm_key_index, failover_count
    if len(BM_API_KEYS) <= 1:
        return get_current_bm_key()
    current_bm_key_index = (current_bm_key_index + 1) % len(BM_API_KEYS)
    new_key = get_current_bm_key()
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated bm key → index {current_bm_key_index}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


def get_current_nr_key():
    if not NR_API_KEYS:
        return ""
    return NR_API_KEYS[current_nr_key_index]


def rotate_nr_key(reason: str = "Limited"):
    global current_nr_key_index, failover_count
    if len(NR_API_KEYS) <= 1:
        return get_current_nr_key()
    current_nr_key_index = (current_nr_key_index + 1) % len(NR_API_KEYS)
    new_key = get_current_nr_key()
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated nry key → index {current_nr_key_index}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


def get_current_dahl_key():
    if not DAHL_API_KEYS:
        return ""
    return DAHL_API_KEYS[current_dahl_key_index]


def rotate_dahl_key(reason: str = "Limited"):
    global current_dahl_key_index, failover_count
    if len(DAHL_API_KEYS) <= 1:
        return get_current_dahl_key()
    current_dahl_key_index = (current_dahl_key_index + 1) % len(DAHL_API_KEYS)
    new_key = get_current_dahl_key()
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated dahl key → index {current_dahl_key_index}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


def get_current_qc_key():
    if not QC_API_KEYS:
        return ""
    return QC_API_KEYS[current_qc_key_index]


def get_current_qc_key_for_model(model: str) -> str:
    """Return the active key for a specific Qwen Cloud model.

    Each model tracks its own key index so that quota exhaustion on one model
    does not rotate keys for other models.
    """
    if not QC_API_KEYS:
        return ""
    start_idx = qc_model_key_index.get(model, 0) % len(QC_API_KEYS)
    for offset in range(len(QC_API_KEYS)):
        idx = (start_idx + offset) % len(QC_API_KEYS)
        candidate = QC_API_KEYS[idx]
        if not is_qc_model_exhausted(candidate, model):
            qc_model_key_index[model] = idx
            return candidate
    return ""


def rotate_qc_key_for_model(model: str, after_key: str | None = None) -> bool:
    """Move to the next key that has not exhausted quota for this model.

    Returns True if a fresh key is found, False if all keys have exhausted
    this model.
    """
    global failover_count
    if not QC_API_KEYS:
        return False

    if after_key in QC_API_KEYS:
        start_idx = QC_API_KEYS.index(after_key)
    else:
        start_idx = qc_model_key_index.get(model, 0) % len(QC_API_KEYS)
    for offset in range(1, len(QC_API_KEYS) + 1):
        idx = (start_idx + offset) % len(QC_API_KEYS)
        candidate = QC_API_KEYS[idx]
        if not is_qc_model_exhausted(candidate, model):
            qc_model_key_index[model] = idx
            failover_count += 1
            print(f"[LOG] Rotated qc key for model {model} -> index {idx}: {candidate[:15]}...")
            _bg(db_execute(
                "INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                str(failover_count)
            ))
            return True

    return False


def mark_qc_model_exhausted(key: str, model: str):
    """Mark a specific model as exhausted for a given key.

    QC keys are one-time use per model - once marked, stays exhausted
    until manual reset. Key can still be used for other models.
    """
    if key not in qc_model_failures:
        qc_model_failures[key] = {}
    qc_model_failures[key][model] = True
    _bg(db_execute(
        "INSERT INTO qc_model_exhaustions (key_value, model) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        key,
        model,
    ))


def is_qc_model_exhausted(key: str, model: str) -> bool:
    """Check whether a key has exhausted quota for a specific model.

    QC keys are one-time use per model - once exhausted, permanently marked
    until manual reset. Key can still be used for other models.
    """
    return qc_model_failures.get(key, {}).get(model, False)


def reset_qc_model_failures(key: str):
    """Clear per-model failure state for a key (e.g. on manual reset)."""
    if key in qc_model_failures:
        del qc_model_failures[key]
    _bg(db_execute("DELETE FROM qc_model_exhaustions WHERE key_value = $1", key))
    for model, idx in list(qc_model_key_index.items()):
        if QC_API_KEYS and QC_API_KEYS[idx % len(QC_API_KEYS)] == key:
            qc_model_key_index[model] = 0


def rotate_qc_key(reason: str = "Limited"):
    """Legacy whole-key rotation. Kept for compatibility with other code paths."""
    global current_qc_key_index, failover_count
    if len(QC_API_KEYS) <= 1:
        return get_current_qc_key()
    current_qc_key_index = (current_qc_key_index + 1) % len(QC_API_KEYS)
    new_key = get_current_qc_key()
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated qc key → index {current_qc_key_index}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


def get_current_marketku_key():
    if not MARKETKU_API_KEYS:
        return ""
    return MARKETKU_API_KEYS[current_marketku_key_index]


def rotate_marketku_key(reason: str = "Limited"):
    global current_marketku_key_index, failover_count
    if len(MARKETKU_API_KEYS) <= 1:
        return get_current_marketku_key()
    current_marketku_key_index = (current_marketku_key_index + 1) % len(MARKETKU_API_KEYS)
    new_key = get_current_marketku_key()
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated marketku key → index {current_marketku_key_index}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


def get_current_custom_key(prefix: str):
    keys = CUSTOM_PROVIDER_KEYS.get(prefix) or []
    if not keys:
        return ""
    idx = custom_key_index.get(prefix, 0) % len(keys)
    return keys[idx]


def rotate_custom_key(prefix: str, reason: str = "Limited"):
    global failover_count
    keys = CUSTOM_PROVIDER_KEYS.get(prefix) or []
    if len(keys) <= 1:
        return get_current_custom_key(prefix)
    idx = (custom_key_index.get(prefix, 0) + 1) % len(keys)
    custom_key_index[prefix] = idx
    new_key = keys[idx]
    key_statuses[new_key] = "Active"
    failover_count += 1
    print(f"[LOG] Rotated {prefix} key → index {idx}: {new_key[:15]}... (reason: {reason})")
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", new_key))
    _bg(db_execute("INSERT INTO server_config (key, value) VALUES ('failover_count', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", str(failover_count)))
    return new_key


async def set_custom_provider_models(prefix: str, models: list):
    """
    Manually pin a custom provider's model list, bypassing refresh_custom_provider_models.
    Useful when the provider's own /models endpoint lists things the account
    doesn't actually have entitlement to call (e.g. copilot-api advertises
    every model in the Copilot catalog regardless of your plan).
    """
    from app.database import update_custom_provider_models

    info = CUSTOM_PROVIDERS.get(prefix)
    if not info:
        return False, "Unknown provider"
    cleaned = list(dict.fromkeys(m.strip() for m in models if m.strip()))
    info["models"] = cleaned
    await update_custom_provider_models(prefix, ",".join(cleaned))
    return True, f"Set {len(cleaned)} models"


def custom_provider_headers(info: dict, key: str, request_headers=None) -> dict:
    """Build only approved upstream headers; never forward router credentials."""
    headers = {"Content-Type": "application/json"}
    if info.get("auth_header") == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    if info.get("api_format") == "anthropic":
        version = info.get("anthropic_version", "2023-06-01")
        if version:
            headers["anthropic-version"] = version
        if request_headers:
            for name in ("anthropic-version", "anthropic-beta", "anthropic-workspace-id"):
                value = request_headers.get(name)
                if value:
                    headers[name] = value
    return headers


async def refresh_custom_provider_models(prefix: str):
    """Fetch {base_url}/models with whatever key is currently active for this
    provider and cache the id list. Called right after a provider (with a
    key) is added, and can be re-run later to pick up catalog changes."""
    import httpx
    from app.database import update_custom_provider_models

    info = CUSTOM_PROVIDERS.get(prefix)
    if not info:
        return False, "Unknown provider"
    key = get_current_custom_key(prefix)
    if not key:
        return False, "No API key configured for this provider yet"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{info['base_url']}/models", headers=custom_provider_headers(info, key))
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} fetching model list"
        data = r.json()
        raw_ids = [m["id"] for m in data.get("data", []) if m.get("id")]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if not raw_ids:
        return False, "Provider returned no models"

    # Some providers self-namespace their ids (e.g. "mk/auto"); strip that so
    # it isn't doubled up when we prefix it again for display/dispatch.
    own_prefix = f"{prefix}/"
    models = list(dict.fromkeys(m[len(own_prefix):] if m.startswith(own_prefix) else m for m in raw_ids))

    info["models"] = models
    await update_custom_provider_models(prefix, ",".join(models))
    return True, f"Fetched {len(models)} models"


async def add_custom_provider(prefix: str, name: str, base_url: str, api_format: str,
                              auth_header: str = "bearer", anthropic_version: str = "2023-06-01"):
    from app.database import insert_custom_provider, enable_provider as db_enable_provider
    prefix = prefix.strip().lower()
    name = name.strip()
    base_url = base_url.strip().rstrip("/")
    if not prefix or not name or not base_url:
        return False, "Prefix, name, and base URL are all required"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,19}", prefix):
        return False, "Prefix must be lowercase letters/numbers/hyphens, 1-20 chars"
    if prefix in BUILTIN_PROVIDER_PREFIXES or prefix in CUSTOM_PROVIDERS:
        return False, f"Prefix '{prefix}' is already in use"
    if api_format not in ("openai", "anthropic"):
        return False, "api_format must be 'openai' or 'anthropic'"
    if auth_header not in ("bearer", "x-api-key"):
        return False, "auth_header must be 'bearer' or 'x-api-key'"
    if not isinstance(anthropic_version, str) or (anthropic_version and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", anthropic_version)):
        return False, "anthropic_version must be YYYY-MM-DD or empty"

    await insert_custom_provider(prefix, name, base_url, api_format, auth_header, anthropic_version)
    CUSTOM_PROVIDERS[prefix] = {"name": name, "base_url": base_url, "api_format": api_format, "auth_header": auth_header, "anthropic_version": anthropic_version, "models": []}
    CUSTOM_PROVIDER_KEYS[prefix] = []
    custom_key_index[prefix] = 0
    # A brand new prefix can't collide with a disabled built-in, but clear
    # defensively in case a provider was re-added under a reused prefix.
    DISABLED_PROVIDERS.discard(prefix)
    await db_enable_provider(prefix)
    return True, "Provider added"


def remove_custom_key(prefix: str, key: str):
    keys = CUSTOM_PROVIDER_KEYS.get(prefix)
    if not keys or key not in keys:
        return False, "Key not found"
    was_active = get_current_custom_key(prefix) == key
    keys.remove(key)
    custom_key_index[prefix] = custom_key_index.get(prefix, 0) % max(len(keys), 1)
    key_statuses.pop(key, None)
    if was_active and keys:
        rotate_custom_key(prefix)
    _bg(db_execute("DELETE FROM api_keys WHERE key_value = $1", key))
    return True, "Key removed successfully"


async def remove_provider(prefix: str):
    """
    Remove a provider entirely.

    Built-ins keep their Python routing code (removing that needs a deploy),
    so "removing" one means: wipe its keys and mark it disabled so dispatch
    refuses it. Custom providers are dropped for real, including their keys.
    """
    from app.database import (
        delete_custom_provider, disable_provider as db_disable_provider,
    )

    if prefix in BUILTIN_PROVIDER_PREFIXES:
        provider_lists = {
            "bm": BM_API_KEYS, "nry": NR_API_KEYS,
            "dahl": DAHL_API_KEYS, "qc": QC_API_KEYS, "marketku": MARKETKU_API_KEYS,
        }
        keys = provider_lists[prefix]
        for k in list(keys):
            key_statuses.pop(k, None)
        keys.clear()
        await db_execute("DELETE FROM api_keys WHERE provider = $1", prefix)
        await db_disable_provider(prefix)
        DISABLED_PROVIDERS.add(prefix)
        return True, "Built-in provider disabled and its keys removed"

    if prefix not in CUSTOM_PROVIDERS:
        return False, "Unknown provider"

    for k in list(CUSTOM_PROVIDER_KEYS.get(prefix) or []):
        key_statuses.pop(k, None)
    await db_execute("DELETE FROM api_keys WHERE provider = $1", prefix)
    await delete_custom_provider(prefix)
    CUSTOM_PROVIDERS.pop(prefix, None)
    CUSTOM_PROVIDER_KEYS.pop(prefix, None)
    custom_key_index.pop(prefix, None)
    return True, "Provider removed"


# Model-name prefix -> provider key. Only where they differ from the provider
# key itself; everything else falls through unchanged.
_MODEL_PREFIX_TO_PROVIDER = {"dh": "dahl", "mk": "marketku", "at": "atomesus", "wz": "weize"}


def provider_from_model(model: str) -> str:
    """Derive the provider key from a prefixed model name (e.g. dh/foo -> dahl)."""
    if not model or "/" not in model:
        return "kc"
    prefix = model.split("/", 1)[0]
    return _MODEL_PREFIX_TO_PROVIDER.get(prefix, prefix)


def providers_signature() -> str:
    """
    Cheap fingerprint of which providers and how many models exist right now.

    The dashboard compares this between status broadcasts so it knows to
    refetch its model list when a provider is added/removed or a catalog is
    refreshed -- previously the model picker stayed stale until a manual
    page reload.
    """
    builtin_models = {
        "kc": KIMCHI_MODELS, "bm": BLUESMINDS_MODELS,
        "nry": NARA_MODELS, "dahl": DAHL_MODELS_SHORT, "qc": QWEN_CLOUD_MODELS,
        "marketku": MARKETKU_MODELS, "atomesus": ATOMESUS_MODELS, "weize": WEIZE_MODELS,
    }
    parts = []
    for prefix in sorted(BUILTIN_PROVIDER_PREFIXES):
        if prefix in DISABLED_PROVIDERS:
            continue
        parts.append(f"{prefix}:{len(builtin_models.get(prefix) or [])}")
    for prefix, info in sorted(CUSTOM_PROVIDERS.items()):
        parts.append(f"{prefix}:{len(info.get('models') or [])}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def add_request_log(model, status_code, key_used, rotated, latency_ms, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0, provider: str = None):
    global total_requests, total_tokens
    total_requests += 1
    total_tokens += (input_tokens + output_tokens)
    print(f"[LOG-REQ] model={model} in={input_tokens} out={output_tokens}")
    import datetime
    timestamp = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%H:%M:%S")
    if provider is None:
        provider = provider_from_model(model)
    log_item = {
        "id": f"live-{time.time_ns()}",
        "timestamp": timestamp,
        "model": model,
        "status_code": status_code,
        "key_used": key_used[:15] + "...",
        "rotated": rotated,
        "latency_ms": latency_ms,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
    }
    recent_requests.insert(0, log_item)
    if len(recent_requests) > 20:
        recent_requests.pop()
    # Persist to DB
    _bg(persist_request_log(
        model, status_code, log_item["key_used"], rotated, latency_ms,
        input_tokens, output_tokens, cached_tokens, provider,
    ))


def add_api_key(new_key: str, key_type: str = "auto"):
    global BM_API_KEYS, NR_API_KEYS, DAHL_API_KEYS, QC_API_KEYS, MARKETKU_API_KEYS
    new_key = new_key.strip()
    if not new_key:
        return False, "Key cannot be empty"
    all_custom_keys = [k for keys in CUSTOM_PROVIDER_KEYS.values() for k in keys]
    if new_key in BM_API_KEYS or new_key in NR_API_KEYS or new_key in DAHL_API_KEYS or new_key in QC_API_KEYS or new_key in MARKETKU_API_KEYS or new_key in all_custom_keys:
        return False, "Key already exists"

    if key_type == "bm":
        BM_API_KEYS.append(new_key)
        provider = "bm"
    elif key_type == "nry":
        NR_API_KEYS.append(new_key)
        provider = "nry"
    elif key_type == "dahl":
        DAHL_API_KEYS.append(new_key)
        provider = "dahl"
    elif key_type == "qc":
        QC_API_KEYS.append(new_key)
        provider = "qc"
    elif key_type == "marketku":
        MARKETKU_API_KEYS.append(new_key)
        provider = "marketku"
    elif key_type in CUSTOM_PROVIDERS:
        CUSTOM_PROVIDER_KEYS.setdefault(key_type, []).append(new_key)
        provider = key_type
    else:
        return False, f"Unknown provider: {key_type}"

    key_statuses[new_key] = "Standby"
    prefix = new_key[:15] + "..." if len(new_key) > 15 else new_key
    _bg(db_execute(
        "INSERT INTO api_keys (key_value, key_prefix, status, provider) VALUES ($1, $2, 'Standby', $3)",
        new_key, prefix, provider
    ))
    if provider in DISABLED_PROVIDERS:
        DISABLED_PROVIDERS.discard(provider)
        from app.database import enable_provider as db_enable_provider
        _bg(db_enable_provider(provider))
    return True, "Key added successfully"


def remove_api_key(key_prefix: str):
    global BM_API_KEYS, NR_API_KEYS, DAHL_API_KEYS, QC_API_KEYS, MARKETKU_API_KEYS, current_bm_key_index, current_nr_key_index, current_dahl_key_index, current_qc_key_index, current_marketku_key_index
    target_key = None
    target_list = None

    for key in BM_API_KEYS:
        if key.startswith(key_prefix):
            target_key = key
            target_list = BM_API_KEYS
            break

    if not target_key:
        for key in NR_API_KEYS:
            if key.startswith(key_prefix):
                target_key = key
                target_list = NR_API_KEYS
                break

    if not target_key:
        for key in DAHL_API_KEYS:
            if key.startswith(key_prefix):
                target_key = key
                target_list = DAHL_API_KEYS
                break

    if not target_key:
        for key in QC_API_KEYS:
            if key.startswith(key_prefix):
                target_key = key
                target_list = QC_API_KEYS
                break

    if not target_key:
        for key in MARKETKU_API_KEYS:
            if key.startswith(key_prefix):
                target_key = key
                target_list = MARKETKU_API_KEYS
                break

    if not target_key:
        for cprefix, keys in CUSTOM_PROVIDER_KEYS.items():
            for key in keys:
                if key.startswith(key_prefix):
                    return remove_custom_key(cprefix, key)
        return False, "Key not found"

    if len(target_list) <= 1:
        return False, "Cannot delete the last remaining key of this type"
        
    if target_list == BM_API_KEYS:
        active_key = get_current_bm_key()
        if target_key == active_key:
            rotate_bm_key()
        BM_API_KEYS.remove(target_key)
        try:
            current_bm_key_index = BM_API_KEYS.index(get_current_bm_key()) if get_current_bm_key() in BM_API_KEYS else 0
        except Exception:
            current_bm_key_index = 0
    elif target_list == NR_API_KEYS:
        active_key = get_current_nr_key()
        if target_key == active_key:
            rotate_nr_key()
        NR_API_KEYS.remove(target_key)
        try:
            current_nr_key_index = NR_API_KEYS.index(get_current_nr_key()) if get_current_nr_key() in NR_API_KEYS else 0
        except Exception:
            current_nr_key_index = 0
    elif target_list == DAHL_API_KEYS:
        active_key = get_current_dahl_key()
        if target_key == active_key:
            rotate_dahl_key()
        DAHL_API_KEYS.remove(target_key)
        try:
            current_dahl_key_index = DAHL_API_KEYS.index(get_current_dahl_key()) if get_current_dahl_key() in DAHL_API_KEYS else 0
        except Exception:
            current_dahl_key_index = 0
    elif target_list == QC_API_KEYS:
        active_key = get_current_qc_key()
        if target_key == active_key:
            rotate_qc_key()
        QC_API_KEYS.remove(target_key)
        try:
            current_qc_key_index = QC_API_KEYS.index(get_current_qc_key()) if get_current_qc_key() in QC_API_KEYS else 0
        except Exception:
            current_qc_key_index = 0
    elif target_list == MARKETKU_API_KEYS:
        active_key = get_current_marketku_key()
        if target_key == active_key:
            rotate_marketku_key()
        MARKETKU_API_KEYS.remove(target_key)
        try:
            current_marketku_key_index = MARKETKU_API_KEYS.index(get_current_marketku_key()) if get_current_marketku_key() in MARKETKU_API_KEYS else 0
        except Exception:
            current_marketku_key_index = 0

    if target_key in key_statuses:
        del key_statuses[target_key]

    # Remove from DB
    _bg(db_execute("DELETE FROM api_keys WHERE key_value = $1", target_key))
    return True, "Key removed successfully"


def bulk_remove_api_keys(key_prefixes: list):
    """Remove several keys by their masked prefix. Best-effort: keeps going
    even if one entry fails (e.g. it's the last key of its provider)."""
    removed, failed = [], []
    for prefix in key_prefixes:
        ok, msg = remove_api_key(prefix)
        (removed if ok else failed).append({"prefix": prefix, "message": msg})
    return removed, failed


def reset_key_status(key_prefix: str):
    for key in BM_API_KEYS + NR_API_KEYS + DAHL_API_KEYS + QC_API_KEYS + MARKETKU_API_KEYS:
        if key.startswith(key_prefix):
            key_statuses[key] = "Standby"
            reset_qc_model_failures(key)
            _bg(db_execute("UPDATE api_keys SET status = 'Standby' WHERE key_value = $1", key))
            return True, "Key status reset to Standby"
    return False, "Key not found"


def set_active_key(key_prefix: str, provider: str = None):
    global current_bm_key_index, current_nr_key_index, current_dahl_key_index, current_qc_key_index, current_marketku_key_index
    target_key = None
    target_list = None

    if provider == "bm": target_list = BM_API_KEYS
    elif provider == "nry": target_list = NR_API_KEYS
    elif provider == "dahl": target_list = DAHL_API_KEYS
    elif provider == "qc": target_list = QC_API_KEYS
    elif provider == "marketku": target_list = MARKETKU_API_KEYS
    else:
        for lst in [BM_API_KEYS, NR_API_KEYS, DAHL_API_KEYS, QC_API_KEYS, MARKETKU_API_KEYS]:
            for k in lst:
                if k.startswith(key_prefix):
                    target_key = k
                    target_list = lst
                    break
            if target_key: break

    if not target_list: return False, "Target list not found"
    
    if not target_key:
        for k in target_list:
            if k.startswith(key_prefix):
                target_key = k
                break
                
    if not target_key:
        return False, "Key not found"

    for k in target_list:
        if key_statuses.get(k) == "Active":
            key_statuses[k] = "Standby"
            _bg(db_execute("UPDATE api_keys SET status = 'Standby' WHERE key_value = $1", k))
            
    key_statuses[target_key] = "Active"
    _bg(db_execute("UPDATE api_keys SET status = 'Active' WHERE key_value = $1", target_key))
    
    idx = target_list.index(target_key)
    if target_list == BM_API_KEYS: current_bm_key_index = idx
    elif target_list == NR_API_KEYS: current_nr_key_index = idx
    elif target_list == DAHL_API_KEYS: current_dahl_key_index = idx
    elif target_list == QC_API_KEYS: current_qc_key_index = idx
    elif target_list == MARKETKU_API_KEYS: current_marketku_key_index = idx

    return True, "Key set as Active"


def get_masked_keys():
    result = []
    idx = 0
    for key in BM_API_KEYS + NR_API_KEYS + DAHL_API_KEYS + QC_API_KEYS + MARKETKU_API_KEYS:
        status = key_statuses.get(key, "Standby")
        masked = key[:15] + "..." if len(key) > 15 else key
        _provider = (
            "bm" if key in BM_API_KEYS
            else "nry" if key in NR_API_KEYS else "dahl" if key in DAHL_API_KEYS else "qc" if key in QC_API_KEYS
            else "marketku"
        )
        result.append({
            "index": idx,
            "masked": masked,
            "prefix": key[:15],
            "status": status,
            "provider": _provider,
            "provider_name": BUILTIN_PROVIDER_NAMES.get(_provider, _provider),
            "is_bm": key in BM_API_KEYS,
            "is_nr": key in NR_API_KEYS,
            "is_dahl": key in DAHL_API_KEYS,
            "is_qc": key in QC_API_KEYS,
            "is_marketku": key in MARKETKU_API_KEYS
        })
        idx += 1
    for prefix, keys in CUSTOM_PROVIDER_KEYS.items():
        info = CUSTOM_PROVIDERS.get(prefix, {})
        for key in keys:
            status = key_statuses.get(key, "Standby")
            masked = key[:15] + "..." if len(key) > 15 else key
            result.append({
                "index": idx,
                "masked": masked,
                "prefix": key[:15],
                "status": status,
                "provider": prefix,
                "provider_name": info.get("name", prefix),
                "is_bm": False, "is_nr": False, "is_dahl": False,
                "is_qc": False, "is_marketku": False,
                "is_custom": True,
            })
            idx += 1
    return result


def resolve_dahl_model(model_short: str) -> str:
    """Map short dh/<name> to upstream full model id."""
    return DAHL_MODEL_MAP.get(model_short, model_short)


# Auth helpers
def verify_admin_password(password: str) -> bool:
    return bcrypt.checkpw(password.encode(), ADMIN_PASSWORD_HASH)


# Stats helpers for pagination
async def get_paginated_logs(page: int = 1, per_page: int = 20, search: str = "", sort_by: str = "created_at", sort_order: str = "DESC"):
    """Get paginated request logs with search and sorting."""
    offset = (page - 1) * per_page
    allowed_sort = {"created_at", "model", "status_code", "latency_ms"}
    if sort_by not in allowed_sort:
        sort_by = "created_at"
    sort_order = "DESC" if sort_order.upper() == "DESC" else "ASC"

    search_clause = ""
    search_args = []
    if search:
        search_clause = "WHERE model ILIKE $1 OR key_prefix ILIKE $1"
        search_args = [f"%{search}%"]

    # Count total
    count_query = f"SELECT COUNT(*) FROM request_logs {search_clause}"
    total_row = await db_fetchrow(count_query, *search_args)
    total = total_row["count"] if total_row else 0

    # Fetch page
    args = search_args + [per_page, offset]
    arg_offset = len(search_args) + 1
    query = f"""
        SELECT id, model, status_code, key_prefix, rotated, latency_ms, created_at
        FROM request_logs
        {search_clause}
        ORDER BY {sort_by} {sort_order}
        LIMIT ${arg_offset} OFFSET ${arg_offset + 1}
    """
    rows = await db_fetch(query, *args)

    logs = []
    for r in rows:
        ts = r["created_at"]
        if hasattr(ts, "strftime"):
            import datetime
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            ts = ts.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
            ts = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts = str(ts)
        logs.append({
            "id": r["id"],
            "timestamp": ts,
            "model": r["model"],
            "status_code": r["status_code"],
            "key_used": r["key_prefix"],
            "rotated": r["rotated"],
            "latency_ms": r["latency_ms"]
        })

    return {
        "logs": logs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page
    }
