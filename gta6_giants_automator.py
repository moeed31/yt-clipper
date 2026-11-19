import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import edge_tts
import requests
from faster_whisper import WhisperModel
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
    "gemini-3.8-flash",
]

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.environ.get("IG_USER_ID", "").strip()

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main").strip()

STATE_FILE = Path("processed_gta6_clips.txt")
LOGO_PATH = Path("logo.png")
IG_STAGE_DIR = Path("ig_stage")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Auto-link-proof endpoint constants
GOOGLE_TOKEN_URI = "https://" "oauth2.googleapis.com/token"
TWITCH_TOKEN_URI = "https://" "id.twitch.tv/oauth2/token"
TWITCH_HELIX_BASE = "https://" "api.twitch.tv/helix"
TWITCH_GQL_URI = "https://" "gql.twitch.tv/gql"
TWITCH_CLIP_BASE = "https://" "clips.twitch.tv/"
GRAPH_FB_BASE = "https://" "graph.facebook.com/v21.0"
GH_RAW_BASE = "https://" "raw.githubusercontent.com"

MIN_CLIP_DURATION = 12
MAX_CLIP_DURATION = 58
MIN_RELEVANCE_SCORE = 7
MIN_ENTERTAINMENT_SCORE = 6

WHITELIST_STREAMERS = [
    "kaicenat", "xqc", "lirik", "caseoh_", "jynxzi", "summit1g",
    "shroud", "hasanabi", "tarik", "ohnepixel", "plaqueboymax",
    "stableronaldo", "jasontheween", "alanzoka", "ibai", "elspreen",
    "esfandtv", "nmplol", "sodapoppin", "moistcr1tikal", "clix",
    "zackrawrr", "emiru", "mizkif", "agent00", "fanum", "duke",
]

GTA6_KEYWORDS = [
    "gta 6", "gta6", "gta vi", "grand theft auto 6", "grand theft auto vi",
    "vice city", "leonida", "lucia", "jason", "rockstar",
]

HARD_PROFANITY_PATTERNS = [
    r"\bfuck(?:ing|ed|er|s)?\b",
    r"\bmotherfuck(?:ing|er|s)?\b",
    r"\bshit(?:ting|ty|s)?\b",
    r"\bbitch(?:es|ing)?\b",
    r"\bcunt\b",
    r"\bnigg(?:a|er)s?\b",
]


# ==============================================================================
# UTILITY & SANITIZATION HELPERS
# ==============================================================================
def load_processed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_id(path: Path, clip_id: str) -> None:
    existing = load_processed_ids(path)
    if clip_id and clip_id not in existing:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{clip_id}\n")


def strip_emojis_and_non_ascii(text: str) -> str:
    cleaned = (text or "").encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_word_boundary_title(raw: str, max_len: int = 68, suffix: str = "") -> str:
    text = strip_emojis_and_non_ascii(raw)
    text = re.sub(r"#\w+", "", text).strip(" |:-")
    if not text:
        text = "GTA 6 Streamer Moment"
    budget = max(10, max_len - len(suffix))
    if len(text) <= budget:
        return f"{text}{suffix}".strip()
    cut = text[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-|!?")
    if not cut:
        cut = text[:budget].strip()
    return f"{cut}{suffix}".strip()


def sanitize_yt_description(desc: str, fallback: str) -> str:
    cleaned = (desc or fallback).replace("<", "").replace(">", "").strip()
    return cleaned[:4800]


def sanitize_yt_tags(tags: List[str], fallback: List[str]) -> List[str]:
    source = tags if isinstance(tags, list) and tags else fallback
    result: List[str] = []
    seen = set()
    total_chars = 0
    for t in source:
        clean = strip_emojis_and_non_ascii(str(t)).replace("#", "").strip(" ,")
        key = clean.lower()
        if not clean or len(clean) > 45 or key in seen:
            continue
        if total_chars + len(clean) + 2 > 420:
            break
        seen.add(key)
        result.append(clean)
        total_chars += len(clean) + 2
        if len(result) >= 15:
            break
    return result or ["GTA 6", "Grand Theft Auto VI", "Shorts"]


def escape_ffmpeg_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def probe_video(path: Path) -> Tuple[float, bool]:
    res = run_cmd([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    info = json.loads(res.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration", 0.0) or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in (info.get("streams") or []))
    return duration, has_audio


def has_heavy_profanity(text: str, opening_only_words: Optional[int] = None) -> bool:
    sample = " ".join((text or "").split()[:opening_only_words]) if opening_only_words else (text or "")
    hits = sum(len(re.findall(pat, sample, flags=re.IGNORECASE)) for pat in HARD_PROFANITY_PATTERNS)
    return hits >= (1 if opening_only_words is not None else 3)


# ==============================================================================
# TWITCH CLIP FETCHING (HELIX + KEYLESS GQL FALLBACK)
# ==============================================================================
def get_helix_token() -> Optional[str]:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        return None
    try:
        r = requests.post(
            TWITCH_TOKEN_URI,
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        print(f"[Twitch Helix] Token fetch failed, falling back to GQL: {e}")
        return None


def fetch_clips_helix(limit: int = 40) -> List[Dict]:
    token = get_helix_token()
    if not token:
        return []
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    clips: List[Dict] = []
    try:
        g = requests.get(
            f"{TWITCH_HELIX_BASE}/games",
            headers=headers,
            params={"name": "Grand Theft Auto VI"},
            timeout=15,
        )
        game_data = (g.json() or {}).get("data") or []
        if game_data:
            game_id = game_data[0]["id"]
            started_at = datetime.fromtimestamp(time.time() - 72 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            c = requests.get(
                f"{TWITCH_HELIX_BASE}/clips",
                headers=headers,
                params={"game_id": game_id, "first": limit, "started_at": started_at},
                timeout=20,
            )
            for item in (c.json() or {}).get("data") or []:
                clips.append({
                    "id": item.get("id", ""),
                    "slug": item.get("id", ""),
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "broadcaster": item.get("broadcaster_name", "Streamer"),
                    "views": int(item.get("view_count", 0) or 0),
                    "duration": float(item.get("duration", 0) or 0),
                    "game": "Grand Theft Auto VI",
                    "language": (item.get("language") or "en").lower(),
                })
    except Exception as e:
        print(f"[Twitch Helix] Fetch error: {e}")
    return clips


def fetch_clips_gql_by_game(game_name: str = "Grand Theft Auto VI", period: str = "LAST_DAY", limit: int = 35) -> List[Dict]:
    query = """
    query($gameName: String!, $limit: Int!,$filter: ClipsFilter!) {
      game(name: $gameName) {
        clips(first: $limit, criteria: { period:$filter }) {
          edges {
            node {
              id
              slug
              url
              title
              viewCount
              durationSeconds
              language
              broadcaster { displayName login }
            }
          }
        }
      }
    }
    """
    headers = {"Client-ID": TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"}
    payload = {"query": query, "variables": {"gameName": game_name, "limit": limit, "filter": period}}
    clips: List[Dict] = []
    try:
        r = requests.post(TWITCH_GQL_URI, headers=headers, json=payload, timeout=20)
        edges = (((r.json() or {}).get("data") or {}).get("game") or {}).get("clips", {}).get("edges") or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            slug = node.get("slug") or node.get("id")
            if not slug:
                continue
            bc = node.get("broadcaster") or {}
            clips.append({
                "id": slug,
                "slug": slug,
                "url": node.get("url") or f"{TWITCH_CLIP_BASE}{slug}",
                "title": node.get("title", ""),
                "broadcaster": bc.get("displayName") or bc.get("login") or "Streamer",
                "views": int(node.get("viewCount", 0) or 0),
                "duration": float(node.get("durationSeconds", 0) or 0),
                "game": game_name,
                "language": (node.get("language") or "en").lower(),
            })
    except Exception as e:
        print(f"[Twitch GQL Game] Error: {e}")
    return clips


def fetch_clips_gql_streamers(streamers: List[str], period: str = "LAST_WEEK") -> List[Dict]:
    query = """
    query($login: String!,$period: ClipsPeriod!) {
      user(login: $login) {
        displayName
        clips(first: 10, criteria: { period: $period }) {
          edges {
            node {
              slug
              url
              title
              viewCount
              durationSeconds
              language
              game { name }
            }
          }
        }
      }
    }
    """
    headers = {"Client-ID": TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"}
    results: List[Dict] = []
    for login in streamers[:15]:
        try:
            r = requests.post(
                TWITCH_GQL_URI,
                headers=headers,
                json={"query": query, "variables": {"login": login, "period": period}},
                timeout=12,
            )
            user = ((r.json() or {}).get("data") or {}).get("user")
            if not user:
                continue
            bc_name = user.get("displayName", login)
            for edge in ((user.get("clips") or {}).get("edges") or []):
                node = (edge or {}).get("node") or {}
                slug = node.get("slug")
                if not slug:
                    continue
                title_lower = (node.get("title") or "").lower()
                game_name = ((node.get("game") or {}).get("name") or "")
                is_gta_game = "grand theft auto vi" in game_name.lower() or "gta 6" in game_name.lower()
                is_gta_title = any(k in title_lower for k in GTA6_KEYWORDS)
                if is_gta_game or is_gta_title:
                    results.append({
                        "id": slug,
                        "slug": slug,
                        "url": node.get("url") or f"{TWITCH_CLIP_BASE}{slug}",
                        "title": node.get("title", ""),
                        "broadcaster": bc_name,
                        "views": int(node.get("viewCount", 0) or 0),
                        "duration": float(node.get("durationSeconds", 0) or 0),
                        "game": game_name or "Grand Theft Auto VI",
                        "language": (node.get("language") or "en").lower(),
                    })
        except Exception:
            continue
    return results


def get_candidate_clips(processed: set) -> List[Dict]:
    raw = fetch_clips_helix(40)
    if len(raw) < 10:
        raw.extend(fetch_clips_gql_by_game("Grand Theft Auto VI", "LAST_DAY", 35))
    if len(raw) < 10:
        raw.extend(fetch_clips_gql_by_game("Grand Theft Auto VI", "LAST_WEEK", 35))
    if len(raw) < 10:
        raw.extend(fetch_clips_gql_streamers(WHITELIST_STREAMERS, "LAST_WEEK"))

    seen = set()
    candidates: List[Dict] = []
    for c in raw:
        cid = c.get("id")
        if not cid or cid in processed or cid in seen:
            continue
        seen.add(cid)
        dur = c.get("duration", 0)
        lang = (c.get("language") or "en").lower()
        if not (MIN_CLIP_DURATION <= dur <= MAX_CLIP_DURATION):
            continue
        if lang and not lang.startswith("en"):
            continue
        candidates.append(c)

    def rank_key(clip: Dict) -> Tuple[int, int]:
        wl = 1 if clip["broadcaster"].lower() in WHITELIST_STREAMERS else 0
        return (wl, clip["views"])

    candidates.sort(key=rank_key, reverse=True)
    return candidates[:15]


# ==============================================================================
# DOWNLOAD & LOCAL FREE TRANSCRIPTION (FASTER-WHISPER)
# ==============================================================================
def download_clip(url: str, out_path: Path) -> bool:
    try:
        run_cmd([
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--retries", "3",
            "--socket-timeout", "20",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(out_path),
            url,
        ])
        return out_path.exists() and out_path.stat().st_size > 100_000
    except Exception as e:
        print(f"[Download] Failed for {url}: {e}")
        return False


def transcribe_clip(whisper_model: WhisperModel, video_path: Path) -> Tuple[str, List[Dict], str]:
    segments, info = whisper_model.transcribe(
        str(video_path),
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
    )
    words: List[Dict] = []
    texts: List[str] = []
    for seg in segments:
        texts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                cleaned = strip_emojis_and_non_ascii(w.word).strip()
                if cleaned:
                    words.append({"word": cleaned, "start": float(w.start), "end": float(w.end)})
    return " ".join(texts).strip(), words, (info.language or "en")


# ==============================================================================
# GEMINI AI QUALITY GATE & SEO GENERATOR (WITH 503 BACKOFF + 3.8 FALLBACK)
# ==============================================================================
def parse_json_safely(raw_text: str) -> Dict:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def call_gemini_json(prompt: str) -> Optional[Dict]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_err = None

    for model_name in dict.fromkeys(GEMINI_MODELS):
        if not model_name:
            continue
        for attempt in range(1, 4):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.4,
                    ),
                )
                return parse_json_safely(resp.text)
            except Exception as e:
                last_err = e
                err_str = str(e)
                print(f"[Gemini] {model_name} (try {attempt}/3) failed: {e}")
                if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")):
                    time.sleep(4 * attempt)
                    continue
                break

    print(f"[Gemini] All models failed: {last_err}")
    return None


def evaluate_and_package_short(clip: Dict, transcript: str) -> Optional[Dict]:
    prompt = f"""
You are a top-performing YouTube Shorts gaming strategist for "The Rippers Gaming" focusing strictly on GTA 6 (Grand Theft Auto VI).

CRITICAL FACT: Today is November 2026. Grand Theft Auto VI (GTA 6) IS OFFICIALLY RELEASED and streamers are actively playing real GTA 6 gameplay live on Twitch right now. NEVER reject a clip by claiming GTA 6 is unreleased.

Analyze this Twitch clip candidate:
- Broadcaster: {clip['broadcaster']}
- Twitch Category: {clip['game']}
- Twitch Clip Title: {clip['title']}
- Views: {clip['views']}
- Duration: {clip['duration']}s
- Transcript: "{transcript[:1800]}"

STRICT QUALITY RULES:
1. Approve genuine GTA 6 gameplay, Vice City/Leonida moments, Jason/Lucia story reactions, funny in-game glitches, or direct GTA 6 streamer commentary.
2. Reject off-topic chatter (unrelated games like Ark/WoW/Valorant, food/dieting, politics/drama) or overly sexual/NSFW clips that could get age-restricted.
3. Reject if there is heavy profanity or swearing in the first 30 seconds.
4. Never use generic spam titles like "UNREAL PRO CLUTCH!" or "INSANE MOMENT!". Write a specific, curiosity-driven title based on what happens in the clip.
5. Keep yt_title STRICTLY under 60 characters, ASCII only, NO emojis, NO hashtags.
6. Keep banner_text STRICTLY 2 to 5 punchy words, ALL CAPS, ASCII only, NO emojis.
7. Write hook_vo: a natural, hype 1-sentence narrator intro (8 to 13 words max, spoken in ~3 seconds) setting up the moment so the Short is transformative and stops scrolling immediately.
8. Write yt_description: 2 short organic sentences describing the moment + credit to {clip['broadcaster']} + 1 specific question about THIS clip (not "Rate 1-10") + 4 hashtags (#GTA6 #GrandTheftAuto6 #GTAVI #Shorts).
9. Provide 12 targeted SEO tags (list of strings).

Return ONLY valid JSON with these exact keys:
{{
  "is_gta6_relevant": true,
  "relevance_score": 0,
  "entertainment_score": 0,
  "has_opening_profanity": false,
  "profanity_level": "none",
  "reject_reason": "",
  "yt_title": "",
  "banner_text": "",
  "hook_vo": "",
  "yt_description": "",
  "yt_tags": []
}}
"""
    return call_gemini_json(prompt)


# ==============================================================================
# VOICEOVER (EDGE-TTS WITH MULTI-VOICE FALLBACK) & SUBTITLES (.ASS)
# ==============================================================================
def generate_edge_tts(text: str, out_mp3: Path) -> float:
    clean_text = re.sub(r"[^A-Za-z0-9 ,.!?'-]", "", text or "").strip()
    if not clean_text:
        return 0.0

    attempts = [
        ("en-US-GuyNeural", "+6%"),
        ("en-US-ChristopherNeural", "+0%"),
        ("en-US-AriaNeural", "+0%"),
        ("en-GB-RyanNeural", "+0%"),
    ]

    async def _run(voice: str, rate: str):
        comm = edge_tts.Communicate(clean_text, voice=voice, rate=rate)
        await comm.save(str(out_mp3))

    for voice, rate in attempts:
        try:
            if out_mp3.exists():
                out_mp3.unlink()
            asyncio.run(_run(voice, rate))
            if out_mp3.exists() and out_mp3.stat().st_size > 1000:
                dur, _ = probe_video(out_mp3)
                if dur > 0.2:
                    print(f"[edge-tts] Generated VO ({dur:.1f}s) using {voice}")
                    return dur
        except Exception as e:
            print(f"[edge-tts] {voice} failed: {e}")

    if out_mp3.exists():
        out_mp3.unlink()
    print("[edge-tts] All voices unavailable on runner, proceeding without VO.")
    return 0.0


def fmt_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass_subtitles(words: List[Dict], ass_path: Path, vo_dur: float) -> int:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,68,&H00FFFFFF,&H0000D7FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,410,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues: List[str] = []
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i + chunk_size]
        start = chunk[0]["start"]
        end = max(chunk[-1]["end"], start + 0.35)
        if end <= vo_dur:
            continue
        start = max(start, vo_dur + 0.05)
        if end <= start + 0.05:
            end = start + 0.35
        text = " ".join(w["word"].upper() for w in chunk)
        text = re.sub(r"\bGTA\s*SIX\b", "GTA 6", text)
        text = re.sub(r"\bLOUISA\b", "LUCIA", text)
        text = text.replace("{", "").replace("}", "").replace("\\", "")
        if text.strip():
            dialogues.append(f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},Default,,0,0,0,,{text}")

    ass_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return len(dialogues)


# ==============================================================================
# FFMPEG 9:16 SHORTS RENDERER
# ==============================================================================
def render_short(
    raw_video: Path,
    out_video: Path,
    banner_text: str,
    broadcaster: str,
    words: List[Dict],
    hook_vo: str,
    workdir: Path,
) -> bool:
    duration, has_audio = probe_video(raw_video)
    if duration < MIN_CLIP_DURATION:
        return False

    target_dur = min(duration, 58.0)
    vo_path = workdir / "hook_vo.mp3"
    vo_dur = generate_edge_tts(strip_emojis_and_non_ascii(hook_vo), vo_path) if hook_vo else 0.0

    clean_banner = (strip_emojis_and_non_ascii(banner_text).upper() or "GTA 6 MOMENT")[:36]
    if len(clean_banner) <= 22:
        banner_fontsize = 50
    elif len(clean_banner) <= 30:
        banner_fontsize = 42
    else:
        banner_fontsize = 34

    banner_file = workdir / "banner.txt"
    banner_file.write_text(clean_banner, encoding="utf-8")

    credit_file = workdir / "credit.txt"
    credit_file.write_text(f"CREDIT: {strip_emojis_and_non_ascii(broadcaster).upper()[:24]}", encoding="utf-8")

    ass_path = workdir / "subs.ass"
    sub_count = build_ass_subtitles(words, ass_path, vo_dur)

    banner_escaped = escape_ffmpeg_path(banner_file)
    credit_escaped = escape_ffmpeg_path(credit_file)
    ass_escaped = escape_ffmpeg_path(ass_path)

    inputs = ["-i", str(raw_video)]
    vo_idx: Optional[int] = None
    logo_idx: Optional[int] = None
    next_idx = 1

    if vo_dur > 0.2 and vo_path.exists():
        inputs.extend(["-i", str(vo_path)])
        vo_idx = next_idx
        next_idx += 1

    if LOGO_PATH.exists():
        inputs.extend(["-i", str(LOGO_PATH)])
        logo_idx = next_idx
        next_idx += 1

    ass_chain = f",ass='{ass_escaped}'" if sub_count > 0 else ""

    v_filters = [
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=22:8,eq=brightness=-0.15[bg]",
        "[0:v]scale=-2:1020,crop=min(iw\\,1080):1020:(iw-min(iw\\,1080))/2:0[fg]",
        "[bg][fg]overlay=(W-w)/2:420[base]",
        "[base]drawbox=x=50:y=220:w=980:h=130:color=0x0B0B0D@0.88:t=fill,"
        "drawbox=x=50:y=346:w=980:h=6:color=0x007BFF@1.0:t=fill,"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{banner_escaped}':"
        f"fontcolor=white:fontsize={banner_fontsize}:x=(w-text_w)/2:y=220+(130-text_h)/2,"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{credit_escaped}':"
        f"fontcolor=0xCCCCCC:fontsize=28:x=60:y=1458:box=1:boxcolor=0x000000@0.65:boxborderw=8{ass_chain}[vpre]",
    ]

    if logo_idx is not None:
        v_filters.append(f"[{logo_idx}:v]scale=120:-1[lg]")
        v_filters.append("[vpre][lg]overlay=W-w-45:70[vout]")
    else:
        v_filters.append("[vpre]copy[vout]")

    a_filters: List[str] = []
    if has_audio and vo_idx is not None:
        duck_end = vo_dur + 0.15
        a_filters.append(f"[0:a]volume='if(lt(t,{duck_end:.2f}),0.22,1.0)':eval=frame[aduck]")
        a_filters.append(f"[{vo_idx}:a]volume=1.18[avo]")
        a_filters.append(
            "[aduck][avo]amix=inputs=2:duration=first:dropout_transition=1:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
    elif has_audio:
        a_filters.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    elif vo_idx is not None:
        a_filters.append(f"[{vo_idx}:a]apad,atrim=0:{target_dur:.2f},loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        a_filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{target_dur:.2f}[aout]")

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(v_filters + a_filters),
        "-map", "[vout]",
        "-map", "[aout]",
        "-t", f"{target_dur:.2f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(out_video),
    ]

    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        print(f"[FFmpeg Error]\n{res.stderr[-1500:]}")
        return False
    return out_video.exists() and out_video.stat().st_size > 100_000


# ==============================================================================
# MULTI-PLATFORM PUBLISHING (YOUTUBE + FACEBOOK REELS + INSTAGRAM REELS)
# ==============================================================================
def upload_to_youtube(video_path: Path, title: str, description: str, tags: List[str]) -> Optional[str]:
    if not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
        print("[YouTube] Missing credentials, skipping.")
        return None

    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "20",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    resp = yt.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    vid_id = resp.get("id")
    print(f"[YouTube] Published Short ID: {vid_id}")
    return vid_id


def upload_to_facebook_reel(video_path: Path, caption: str) -> None:
    if not (FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN):
        print("[Facebook] Missing credentials, skipping.")
        return
    try:
        init_r = requests.post(
            f"{GRAPH_FB_BASE}/{FB_PAGE_ID}/video_reels",
            params={"upload_phase": "start", "access_token": FB_PAGE_ACCESS_TOKEN},
            timeout=30,
        ).json()
        video_id = init_r.get("video_id")
        upload_url = init_r.get("upload_url")
        if not (video_id and upload_url):
            print(f"[Facebook] Init failed: {init_r}")
            return

        file_size = video_path.stat().st_size
        with video_path.open("rb") as f:
            up_r = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {FB_PAGE_ACCESS_TOKEN}",
                    "offset": "0",
                    "file_size": str(file_size),
                },
                data=f,
                timeout=120,
            )
            up_r.raise_for_status()

        fin_r = requests.post(
            f"{GRAPH_FB_BASE}/{FB_PAGE_ID}/video_reels",
            params={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": caption[:2000],
                "access_token": FB_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        ).json()
        print(f"[Facebook] Reel response: {fin_r}")
    except Exception as e:
        print(f"[Facebook] Upload error: {e}")


def git_stage_push_and_get_raw_url(video_path: Path, filename: str) -> Optional[str]:
    if not GITHUB_REPOSITORY:
        print("[Instagram] GITHUB_REPOSITORY not set, skipping IG staging.")
        return None
    IG_STAGE_DIR.mkdir(exist_ok=True)
    staged_file = IG_STAGE_DIR / filename
    shutil.copy2(video_path, staged_file)
    try:
        run_cmd(["git", "pull", "--rebase", "origin", GITHUB_REF_NAME], check=False)
        run_cmd(["git", "add", str(staged_file)])
        run_cmd(["git", "commit", "-m", f"stage ig reel {filename}"])
        run_cmd(["git", "push", "origin", GITHUB_REF_NAME])
    except Exception as e:
        print(f"[Instagram] Git staging push failed: {e}")
        if staged_file.exists():
            staged_file.unlink()
        return None

    raw_url = f"{GH_RAW_BASE}/{GITHUB_REPOSITORY}/{GITHUB_REF_NAME}/ig_stage/{filename}"
    for _ in range(12):
        try:
            head = requests.head(raw_url, timeout=10)
            if head.status_code == 200:
                return raw_url
        except Exception:
            pass
        time.sleep(5)
    print(f"[Instagram] Timed out waiting for raw URL: {raw_url}")
    return None


def cleanup_ig_stage(filename: str) -> None:
    staged_file = IG_STAGE_DIR / filename
    if staged_file.exists():
        staged_file.unlink()
        try:
            run_cmd(["git", "pull", "--rebase", "origin", GITHUB_REF_NAME], check=False)
            run_cmd(["git", "add", "-A", str(IG_STAGE_DIR)])
            run_cmd(["git", "commit", "-m", f"cleanup ig reel {filename}"])
            run_cmd(["git", "push", "origin", GITHUB_REF_NAME])
        except Exception as e:
            print(f"[Instagram] Cleanup commit note: {e}")


def upload_to_instagram_reel(video_path: Path, caption: str, clip_id: str) -> None:
    if not (IG_USER_ID and FB_PAGE_ACCESS_TOKEN):
        print("[Instagram] Missing credentials, skipping.")
        return
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", clip_id)[:40] or str(int(time.time()))
    filename = f"short_{safe_id}.mp4"
    raw_url = git_stage_push_and_get_raw_url(video_path, filename)
    if not raw_url:
        cleanup_ig_stage(filename)
        return
    try:
        create_r = requests.post(
            f"{GRAPH_FB_BASE}/{IG_USER_ID}/media",
            params={
                "media_type": "REELS",
                "video_url": raw_url,
                "caption": caption[:2000],
                "share_to_feed": "true",
                "access_token": FB_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        ).json()
        creation_id = create_r.get("id")
        if not creation_id:
            print(f"[Instagram] Container creation failed: {create_r}")
            return

        for _ in range(24):
            time.sleep(10)
            status_r = requests.get(
                f"{GRAPH_FB_BASE}/{creation_id}",
                params={"fields": "status_code", "access_token": FB_PAGE_ACCESS_TOKEN},
                timeout=15,
            ).json()
            code = status_r.get("status_code")
            print(f"[Instagram] Container status: {code}")
            if code == "FINISHED":
                pub_r = requests.post(
                    f"{GRAPH_FB_BASE}/{IG_USER_ID}/media_publish",
                    params={"creation_id": creation_id, "access_token": FB_PAGE_ACCESS_TOKEN},
                    timeout=30,
                ).json()
                print(f"[Instagram] Published Reel: {pub_r}")
                break
            if code == "ERROR":
                print(f"[Instagram] Container error: {status_r}")
                break
    except Exception as e:
        print(f"[Instagram] Upload error: {e}")
    finally:
        cleanup_ig_stage(filename)


# ==============================================================================
# MAIN PIPELINE
# ==============================================================================
def main() -> None:
    processed = load_processed_ids(STATE_FILE)
    candidates = get_candidate_clips(processed)
    print(f"[Pipeline] Found {len(candidates)} fresh English GTA 6 candidates.")

    if not candidates:
        print("[Pipeline] No eligible clips found. Exiting cleanly.")
        return

    whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for clip in candidates:
            cid = clip["id"]
            print(f"\n[Inspecting] {clip['broadcaster']} | {clip['title']} ({clip['views']} views)")
            raw_mp4 = workdir / f"raw_{cid}.mp4"
            if not download_clip(clip["url"], raw_mp4):
                continue

            transcript, words, lang = transcribe_clip(whisper_model, raw_mp4)
            if lang != "en" or len(transcript.split()) < 6:
                print(f"[Skip] Non-English ({lang}) or too little speech.")
                save_processed_id(STATE_FILE, cid)
                continue

            if has_heavy_profanity(transcript, opening_only_words=45):
                print("[Skip] Hard profanity detected in first 30 seconds.")
                save_processed_id(STATE_FILE, cid)
                continue

            ai = evaluate_and_package_short(clip, transcript)
            if not ai:
                continue

            rel = int(ai.get("relevance_score", 0) or 0)
            ent = int(ai.get("entertainment_score", 0) or 0)
            is_rel = bool(ai.get("is_gta6_relevant", False))
            open_prof = bool(ai.get("has_opening_profanity", False))
            prof_lvl = str(ai.get("profanity_level", "none")).lower()

            print(f"[AI Score] relevant={is_rel} ({rel}/10), fun={ent}/10, profanity={prof_lvl}")
            if not is_rel or rel < MIN_RELEVANCE_SCORE or ent < MIN_ENTERTAINMENT_SCORE or open_prof or prof_lvl == "heavy":
                print(f"[Rejected] {ai.get('reject_reason', 'Below quality threshold')}")
                save_processed_id(STATE_FILE, cid)
                continue

            final_mp4 = workdir / f"final_{cid}.mp4"
            yt_title = clean_word_boundary_title(ai.get("yt_title", clip["title"]), max_len=68, suffix=" #Shorts")
            banner_text = strip_emojis_and_non_ascii(ai.get("banner_text", "GTA 6 MOMENT"))
            hook_vo = strip_emojis_and_non_ascii(ai.get("hook_vo", ""))
            yt_desc = sanitize_yt_description(
                ai.get("yt_description", ""),
                f"GTA 6 clip featuring {clip['broadcaster']}. #GTA6 #GrandTheftAuto6 #Shorts",
            )
            yt_tags = sanitize_yt_tags(
                ai.get("yt_tags", []),
                ["GTA 6", "Grand Theft Auto VI", "GTA VI", clip["broadcaster"], "Shorts"],
            )

            ok = render_short(raw_mp4, final_mp4, banner_text, clip["broadcaster"], words, hook_vo, workdir)
            if not ok:
                continue

            vid_id = upload_to_youtube(final_mp4, yt_title, yt_desc, yt_tags)
            social_caption = f"{yt_title}\n\n{yt_desc}"
            upload_to_facebook_reel(final_mp4, social_caption)
            upload_to_instagram_reel(final_mp4, social_caption, cid)

            if vid_id or not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
                save_processed_id(STATE_FILE, cid)
            print("[Done] Successfully completed GTA 6 Short pipeline.")
            return

    print("[Pipeline] No candidate passed the strict quality gate this run.")


if __name__ == "__main__":
    main()
