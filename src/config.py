"""Central config: paths, constants, versioning. Nothing here does I/O except
reading .env / environment variables."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "bayut.db"
SESSION_FILE = ROOT / "data" / "bayut_session.json"
GOLD_CSV = ROOT / "data" / "gold" / "gold_25.csv"
OUT_XLSX = ROOT / "data" / "out" / "dataset.xlsx"

# Bump when the extraction prompt or the JSON schema handed to the model
# changes shape. Every extraction row is stamped with this, so a bump lets
# you invalidate exactly the affected rows (`extractions.prompt_version <
# PROMPT_VERSION`) instead of re-running the whole dataset blind.
PROMPT_VERSION = "2"
# Bump when the normalization taxonomy (enum values, glossary) changes, for
# the same reason.
TAXONOMY_VERSION = "1"

# --- Algolia (discovery) ---
ALGOLIA_APP_ID = "LL8IZ711CS"
ALGOLIA_FALLBACK_KEY = "07de0a8209b2f3cd921152dfe39310a9"
ALGOLIA_INDEX = "bayut-eg-production-ads-ar"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PURPOSES = ["for-sale", "for-rent"]
CATEGORIES = ["apartments", "villas", "chalets", "townhouses",
              "duplexes", "penthouses", "twin-houses"]

# Governorate name -> Algolia location.externalID (province level, level=1).
# Verified live against the index (read off real hits already in the DB from
# a prior discovery run, cross-checked against location.name_l1) -- not
# guesses. `discover.verify_values()` re-checks these every run and drops
# any that return 0 hits, so a stale/wrong value here fails loudly.
GOVERNORATES = {
    "Cairo": "1-5",
    "Giza": "1-68",
    "Alexandria": "1-6",
}

# --- Fetch (Playwright) ---
# Real installed Chrome, NOT the bundled Chromium -- confirmed live that
# bundled Chromium triggers a client-side reload loop against Bayut's bot
# check (Humbucker) even with stealth patches applied, while real Chrome
# clears it cleanly for at least one navigation. See README "How did you
# get the data out" for the full writeup, including the honest limitation:
# in live testing, a burst of ~15-20 automated navigations against
# bayut.eg from one IP within an hour was enough to make EVERY subsequent
# navigation get challenged -- home and search pages included, not just
# detail pages. This looks like request-velocity/IP-reputation scoring,
# not a per-request or per-session defeat. The mitigation is pacing, not a
# cleverer bypass: long human-scale delays, small batches, and a circuit
# breaker that stops the whole run (rather than burning through the queue)
# the moment even a warm-up navigation gets challenged.
BROWSER_CHANNEL = "chrome"
FETCH_MIN_DELAY = 3.0
FETCH_MAX_DELAY = 6.0
WARMUP_WAIT_MS = 3500
NAV_WAIT_MS = 2500
MAX_CONSECUTIVE_BLOCKS = 4

# --- Extract (LLM) ---
# Provider is auto-selected: if GOOGLE_API_KEY is set, the pipeline talks
# directly to Google AI Studio's free Gemini tier (no credit card, works from
# anywhere) via its OpenAI-compatible endpoint; otherwise it uses OpenRouter.
# Force one with EXTRACT_PROVIDER = "google" | "openrouter".
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Google AI Studio free tier. Get a key (no card) at
# https://aistudio.google.com/apikey and put it in .env as GOOGLE_API_KEY=...
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

_forced = os.environ.get("EXTRACT_PROVIDER", "").strip().lower()
EXTRACT_PROVIDER = _forced or ("google" if GOOGLE_API_KEY else "openrouter")
# Default model, per provider. Gemini Flash is the balance point: strong
# bilingual (Arabic/English) extraction -- notably better than Haiku at
# picking compound/developer names out of prose -- and free on Google AI
# Studio. Google retired gemini-2.5-flash for new keys (it now points new
# users at gemini-3.6-flash), so the Google default tracks that; OpenRouter
# still serves 2.5. The "google/" prefix is stripped automatically for the
# native Google endpoint. Override with EXTRACT_MODEL or --model.
# Model choice on the FREE tier is really a rate-limit choice. Per Google AI
# Studio's own limits page, the free daily caps (RPD) differ wildly:
#   gemini-2.5-flash-lite   RPM 10  RPD 20     <- tiny, exhausts instantly
#   gemini-3.6-flash        RPM 6   RPD 25     <- tiny
#   gemini-3.5-flash-lite   RPM 15  RPD 500    <- enough for ~500 listings/day
#   gemini-3.1-flash-lite   RPM 15  RPD 500    <- same; use for the tail if needed
# So gemini-3.5-flash-lite is the pick: 500/day covers the >=500 target in one
# run. Quality is fine for Group B -- the compound name comes mostly from
# Bayut's structured location data, and the LLM handles the free-text rest.
_default_model = ("google/gemini-3.5-flash-lite" if EXTRACT_PROVIDER == "google"
                  else "google/gemini-2.5-flash")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", _default_model)
EXTRACT_TEMPERATURE = 0
# Cap the output ceiling. The Group B JSON is small (~200-600 tokens), but
# Gemini 3.x Flash is a *thinking* model that spends output tokens on internal
# reasoning first -- with too small a cap it burns the whole budget thinking
# and returns EMPTY content (a "Expecting value: line 1 column 1" JSON error).
# So Google gets a roomy 8192 (free tier, billed on actual use anyway), while
# OpenRouter stays at 1500 -- there a big cap gets *reserved* up-front and a
# low balance refuses the request ("requested up to 64000, can only afford
# 8000").
_default_max_tokens = "8192" if EXTRACT_PROVIDER == "google" else "1500"
EXTRACT_MAX_TOKENS = int(os.environ.get("EXTRACT_MAX_TOKENS", _default_max_tokens))
# Pace requests under the provider's per-minute cap, and retry the ones that
# still slip through with backoff rather than dropping the listing.
#   Google free tier ~15 req/min  -> 4.5s spacing (~13/min, safe headroom)
#   OpenRouter new account ~10/min -> 7s spacing
# Once an account has usage history these caps lift; lower the interval then.
_default_interval = "5.0" if EXTRACT_PROVIDER == "google" else "7.0"
EXTRACT_MIN_INTERVAL_S = float(os.environ.get("EXTRACT_MIN_INTERVAL_S", _default_interval))
# Google's free tier enforces a ~20 requests/minute cap on a shared metric;
# the occasional 429 is normal and recoverable, so be patient rather than
# dropping the listing. 12 attempts x up to ~60s covers the worst rolling
# window. Every 429 waits exactly as long as Google's message asks.
EXTRACT_MAX_RETRIES = int(os.environ.get("EXTRACT_MAX_RETRIES", "12"))
