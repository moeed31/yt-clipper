import asyncio
import json
import os
import re
import shutil
import subprocess
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

# ==============================================================================
# CONFIGURATION
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
    "gemini-3.8-flash",
    "gemini-2.5-flash",
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
TTS_VOICE = "en-US-AndrewNeural"
TTS_RATE = "+8%"

MIN_CLIP_DURATION = 12
MAX_CLIP_DURATION = 52
MIN_RELEVANCE_SCORE = 7
MIN_ENTERTAINMENT_SCORE = 6

# High-engagement streamers likely playing/reacting to GTA 6
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
# UTILITY & TEXT SANITIZATION HELPERS
# ==============================================================================
def load_processed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_id(path: Path, clip_id: str) -> None:
    existing = load_processed_ids(path)
    if clip_id not in existing:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{clip_id}\n")


def strip_emojis_and_non_ascii(text: str) -> str:
    """Prevents FFmpeg drawtext missing-glyph boxes."""
    cleaned = text.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_word_boundary_title(raw: str, max_len: int = 68, suffix: str = "") -> str:
    """Truncates cleanly on word boundaries so titles never cut off mid-word."""
    text = strip_emojis_and_non_ascii(raw)
    text = re.sub(r"#\w+", "", text).strip(" |:-")
    budget = max_len - len(suffix)
    if len(text) <= budget:
        return f"{text}{suffix}".strip()
    cut = text[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-|!?")
    return f"{cut}{suffix}".strip()


def run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def probe_video(path: Path) -> Tuple[float, bool]:
    """Returns (duration_seconds, has_audio)."""
    res = run_cmd([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path)
    ])
    info = json.loads(res.stdout)
    duration = float(info.get("format", {}).get("duration", 0.0))
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return duration, has_audio


def has_heavy_profanity(text: str, opening_only_words: Optional[int] = None) -> bool:
    sample = text
    if opening_only_words is not None:
        sample = " ".join(text.split()[:opening_only_words])
    hits = sum(len(re.findall(pat, sample, flags=re.IGNORECASE)) for pat in HARD_PROFANITY_PATTERNS)
    if opening_only_words is not None:
        return hits >= 1
    return hits >= 3


# ==============================================================================
# TWITCH CLIP FETCHING (HELIX + KEYLESS GQL FALLBACK)
# ==============================================================================
def get_helix_token() -> Optional[str]:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        return None
    try:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
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
        # Look up GTA 6 game_id
        g = requests.get(
            "https://api.twitch.tv/helix/games",
            headers=headers,
            params={"name": "Grand Theft Auto VI"},
            timeout=15,
        )
        game_data = g.json().get("data", [])
        if game_data:
            game_id = game_data[0]["id"]
            started_at = datetime.fromtimestamp(time.time() - 72 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            c = requests.get(
                "https://api.twitch.tv/helix/clips",
                headers=headers,
                params={"game_id": game_id, "first": limit, "started_at": started_at},
                timeout=20,
            )
            for item in c.json().get("data", []):
                clips.append({
                    "id": item["id"],
                    "slug": item["id"],
                    "url": item["url"],
                    "title": item.get("title", ""),
                    "broadcaster": item.get("broadcaster_name", "Streamer"),
                    "views": int(item.get("view_count", 0)),
                    "duration": float(item.get("duration", 0)),
                    "game": "Grand Theft Auto VI",
                    "language": item.get("language", "en"),
                })
    except Exception as e:
        print(f"[Twitch Helix] Fetch error: {e}")
    return clips


def fetch_clips_gql_by_game(game_name: str = "Grand Theft Auto VI", period: str = "LAST_DAY", limit: int = 35) -> List[Dict]:
    query = """
    query($gameName: String!, $limit: Int!, $filter: ClipsFilter!) {
      game(name: $gameName) {
        clips(first: $limit, criteria: { period: $filter }) {
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
    payload = {
        "query": query,
        "variables": {"gameName": game_name, "limit": limit, "filter": period},
    }
    clips: List[Dict] = []
    try:
        r = requests.post("https://gql.twitch.tv/gql", headers=headers, json=payload, timeout=20)
        edges = r.json().get("data", {}).get("game", {}).get("clips", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            bc = node.get("broadcaster") or {}
            clips.append({
                "id": node.get("slug") or node.get("id"),
                "slug": node.get("slug"),
                "url": node.get("url") or f"https://clips.twitch.tv/{node.get('slug')}",
                "title": node.get("title", ""),
                "broadcaster": bc.get("displayName", "Streamer"),
                "views": int(node.get("viewCount", 0)),
                "duration": float(node.get("durationSeconds", 0)),
                "game": game_name,
                "language": (node.get("language") or "en").lower(),
            })
    except Exception as e:
        print(f"[Twitch GQL Game] Error: {e}")
    return clips


def fetch_clips_gql_streamers(streamers: List[str], period: str = "LAST_WEEK") -> List[Dict]:
    """Backup search on whitelisted streamers filtering for GTA 6 titles."""
    query = """
    query($login: String!, $period: ClipsPeriod!) {
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
                "https://gql.twitch.tv/gql",
                headers=headers,
                json={"query": query, "variables": {"login": login, "period": period}},
                timeout=12,
            )
            user = (r.json().get("data") or {}).get("user")
            if not user:
                continue
            bc_name = user.get("displayName", login)
            for edge in (user.get("clips") or {}).get("edges", []):
                node = edge.get("node", {})
                title_lower = (node.get("title") or "").lower()
                game_name = ((node.get("game") or {}).get("name") or "")
                is_gta_game = "grand theft auto vi" in game_name.lower() or "gta 6" in game_name.lower()
                is_gta_title = any(k in title_lower for k in GTA6_KEYWORDS)
                if is_gta_game or is_gta_title:
                    results.append({
                        "id": node.get("slug"),
                        "slug": node.get("slug"),
                        "url": node.get("url") or f"https://clips.twitch.tv/{node.get('slug')}",
                        "title": node.get("title", ""),
                        "broadcaster": bc_name,
                        "views": int(node.get("viewCount", 0)),
                        "duration": float(node.get("durationSeconds", 0)),
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
    candidates = []
    for c in raw:
        cid = c.get("id")
        if not cid or cid in processed or cid in seen:
            continue
        seen.add(cid)
        dur = c.get("duration", 0)
        lang = (c.get("language") or "en").lower()
        if not (MIN_CLIP_DURATION <= dur <= MAX_CLIP_DURATION):
            continue
        # Stick strictly to English clips to keep audience profile consistent
        if lang and not lang.startswith("en"):
            continue
        candidates.append(c)

    # Prefer whitelisted streamers + high views
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
    """Returns (full_text, word_segments, detected_language)."""
    segments, info = whisper_model.transcribe(
        str(video_path),
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
    )
    words = []
    texts = []
    for seg in segments:
        texts.append(seg.text.strip())
        if seg.words:
            for w in seg.words:
                cleaned = strip_emojis_and_non_ascii(w.word).strip()
                if cleaned:
                    words.append({"word": cleaned, "start": float(w.start), "end": float(w.end)})
    return " ".join(texts).strip(), words, (info.language or "en")


# ==============================================================================
# GEMINI AI QUALITY GATE & SEO GENERATOR (FREE TIER SAFE)
# ==============================================================================
def call_gemini_json(prompt: str) -> Optional[Dict]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_err = None
    for model_name in dict.fromkeys(GEMINI_MODELS):
        if not model_name:
            continue
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.4,
                ),
            )
            return json.loads(resp.text)
        except Exception as e:
            last_err = e
            print(f"[Gemini] Model {model_name} failed: {e}")
            time.sleep(2)
    print(f"[Gemini] All models failed: {last_err}")
    return None


def evaluate_and_package_short(clip: Dict, transcript: str) -> Optional[Dict]:
    prompt = f"""
You are a top-performing YouTube Shorts gaming strategist for the channel "The Rippers Gaming" focusing strictly on GTA 6 (Grand Theft Auto VI).

Analyze this Twitch clip candidate:
- Broadcaster: {clip['broadcaster']}
- Twitch Category: {clip['game']}
- Twitch Clip Title: {clip['title']}
- Views: {clip['views']}
- Duration: {clip['duration']}s
- Transcript: "{transcript[:1800]}"

STRICT QUALITY RULES:
1. Reject if the streamer is talking about unrelated games (like Ark, WoW, Valorant), food/dieting, or random off-topic chatter instead of GTA 6 gameplay, Vice City, characters (Lucia/Jason), or genuine GTA 6 reactions.
2. Reject if there is heavy profanity or swearing in the first 30 seconds (hurts YouTube distribution).
3. Never use generic spam titles like "UNREAL PRO CLUTCH!" or "INSANE MOMENT!". Write a specific, curiosity-driven title based on what actually happens in the clip.
4. Keep yt_title STRICTLY under 60 characters, ASCII only, NO emojis, NO hashtags in the title.
5. Keep banner_text STRICTLY 2 to 5 punchy words, ALL CAPS, ASCII only, NO emojis (used for FFmpeg overlay).
6. Write hook_vo: a natural, hype 1-sentence narrator intro (8 to 13 words max, spoken in ~3 seconds) that sets up the moment so the Short is transformative and stops scrolling immediately. Example: "Watch how quickly Kai reacts when Vice City police ambush him."
7. Write yt_description: 2 short organic sentences describing the moment + proper credit to {clip['broadcaster']} + a specific question related to THIS clip (not "Rate 1-10") + 4 relevant hashtags (#GTA6 #GrandTheftAuto6 #GTAVI #Shorts).
8. Provide 12 SEO tags (list of strings).

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
# VOICEOVER (EDGE-TTS) & SUBTITLE (.ASS) GENERATION
# ==============================================================================
def generate_edge_tts(text: str, out_mp3: Path) -> float:
    """Generates free neural voiceover and returns its duration in seconds."""
    async def _run():
        comm = edge_tts.Communicate(text, voice=TTS_VOICE, rate=TTS_RATE)
        await comm.save(str(out_mp3))

    try:
        asyncio.run(_run())
        dur, _ = probe_video(out_mp3)
        return dur
    except Exception as e:
        print(f"[edge-tts] Failed ({e}), proceeding without VO.")
        return 0.0


def fmt_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass_subtitles(words: List[Dict], ass_path: Path, vo_dur: float) -> None:
    """Creates bold, mobile-safe 3-word-chunk kinetic subtitles positioned above Shorts UI."""
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
    lines = [header]
    # Group words into punchy 3-word bursts; skip overlapping during VO intro
    chunk_size = 3
    i = 0
    while i < len(words):
        chunk = words[i:i + chunk_size]
        i += chunk_size
        start = chunk[0]["start"]
        end = max(chunk[-1]["end"], start + 0.35)
        if end <= vo_dur:
            continue
        start = max(start, vo_dur + 0.05)
        text = " ".join(w["word"].upper() for w in chunk)
        # Fix common GTA 6 ASR typos
        text = re.sub(r"\bGTA\s*SIX\b", "GTA 6", text)
        text = re.sub(r"\bLOUISA\b|\bLUCIA'S\b", "LUCIA", text)
        text = text.replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},Default,,0,0,0,,{text}")

    ass_path.write_text("\n".join(lines), encoding="utf-8")


# ==============================================================================
# FFMPEG 9:16 SHORTS RENDERER (PUNCH-IN FRAMING + VO DUCKING + CLEAN BANNER)
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

    vo_path = workdir / "hook_vo.mp3"
    vo_dur = generate_edge_tts(strip_emojis_and_non_ascii(hook_vo), vo_path) if hook_vo else 0.0

    banner_file = workdir / "banner.txt"
    banner_file.write_text(strip_emojis_and_non_ascii(banner_text).upper()[:38], encoding="utf-8")

    credit_file = workdir / "credit.txt"
    credit_file.write_text(f"CREDIT: {strip_emojis_and_non_ascii(broadcaster).upper()}", encoding="utf-8")

    ass_path = workdir / "subs.ass"
    build_ass_subtitles(words, ass_path, vo_dur)

    # Escape path for libass filter
    ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    banner_escaped = str(banner_file).replace("\\", "/").replace(":", "\\:")
    credit_escaped = str(credit_file).replace("\\", "/").replace(":", "\\:")

    inputs = ["-i", str(raw_video)]
    vo_idx = None
    logo_idx = None
    next_idx = 1

    if vo_dur > 0.2 and vo_path.exists():
        inputs.extend(["-i", str(vo_path)])
        vo_idx = next_idx
        next_idx += 1

    if LOGO_PATH.exists():
        inputs.extend(["-i", str(LOGO_PATH)])
        logo_idx = next_idx
        next_idx += 1

    # Visual pipeline:
    # 1. Blurred 9:16 ambient background
    # 2. 1.35x punched-in foreground gameplay (1080x1020) so FPS/GTA details are readable on mobile
    # 3. Sleek dark banner bar at top + crisp ASCII text via textfile (zero glyph bugs)
    # 4. Kinetic .ass subtitles in mobile safe zone
    v_filters = [
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=22:8,eq=brightness=-0.15[bg]",
        "[0:v]scale=-2:1020,crop=min(iw\\,1080):1020:(iw-min(iw\\,1080))/2:0[fg]",
        "[bg][fg]overlay=(W-w)/2:420[base]",
        "[base]drawbox=x=50:y=220:w=980:h=130:color=0x0B0B0D@0.88:t=fill,"
        "drawbox=x=50:y=346:w=980:h=6:color=0x007BFF@1.0:t=fill,"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{banner_escaped}':"
        "fontcolor=white:fontsize=50:x=(w-text_w)/2:y=260,"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{credit_escaped}':"
        "fontcolor=0xCCCCCC:fontsize=28:x=60:y=1458:box=1:boxcolor=0x000000@0.65:boxborderw=8,"
        f"ass='{ass_escaped}'[vpre]",
    ]

    if logo_idx is not None:
        v_filters.append(f"[{logo_idx}:v]scale=120:-1[lg]")
        v_filters.append("[vpre][lg]overlay=W-w-45:70[vout]")
    else:
        v_filters.append("[vpre]copy[vout]")

    # Audio pipeline: duck original clip audio during VO hook, then restore to 100%, loudness normalize
    a_filters = []
    if has_audio and vo_idx is not None:
        duck_end = vo_dur + 0.15
        a_filters.append(
            f"[0:a]volume='if(lt(t,{duck_end:.2f}),0.22,1.0)':eval=frame[aduck]"
        )
        a_filters.append(
            f"[{vo_idx}:a]volume=1.18[avo]"
        )
        a_filters.append(
            "[aduck][avo]amix=inputs=2:duration=first:dropout_transition=1:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
    elif has_audio:
        a_filters.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    elif vo_idx is not None:
        a_filters.append(f"[{vo_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")

    filter_complex = ";".join(v_filters + a_filters)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if a_filters:
        cmd.extend(["-map", "[aout]"])
    cmd.extend([
        "-t", str(min(duration, 58.0)),
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_video),
    ])

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
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
    )

    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags[:15],
            "categoryId": "20",  # Gaming
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    vid_id = resp.get("id")
    print(f"[YouTube] Published Short: https://youtube.com/shorts/{vid_id}")
    return vid_id


def upload_to_facebook_reel(video_path: Path, caption: str) -> None:
    if not (FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN):
        print("[Facebook] Missing credentials, skipping.")
        return
    try:
        # Step 1: Initialize upload session
        init_r = requests.post(
            f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/video_reels",
            params={"upload_phase": "start", "access_token": FB_PAGE_ACCESS_TOKEN},
            timeout=30,
        ).json()
        video_id = init_r.get("video_id")
        upload_url = init_r.get("upload_url")
        if not (video_id and upload_url):
            print(f"[Facebook] Init failed: {init_r}")
            return

        # Step 2: Upload binary file
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

        # Step 3: Publish Reel
        fin_r = requests.post(
            f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/video_reels",
            params={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": caption,
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
        run_cmd(["git", "add", str(staged_file)])
        run_cmd(["git", "commit", "-m", f"stage ig reel {filename}"])
        run_cmd(["git", "push", "origin", GITHUB_REF_NAME])
    except Exception as e:
        print(f"[Instagram] Git staging push failed: {e}")
        return None

    raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/{GITHUB_REF_NAME}/ig_stage/{filename}"
    # Wait for GitHub CDN to serve HTTP 200 before calling Meta API
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
            run_cmd(["git", "add", "-u", str(IG_STAGE_DIR)])
            run_cmd(["git", "commit", "-m", f"cleanup ig reel {filename}"])
            run_cmd(["git", "push", "origin", GITHUB_REF_NAME])
        except Exception as e:
            print(f"[Instagram] Cleanup commit note: {e}")


def upload_to_instagram_reel(video_path: Path, caption: str, clip_id: str) -> None:
    if not (IG_USER_ID and FB_PAGE_ACCESS_TOKEN):
        print("[Instagram] Missing credentials, skipping.")
        return
    filename = f"short_{clip_id}.mp4"
    raw_url = git_stage_push_and_get_raw_url(video_path, filename)
    if not raw_url:
        return
    try:
        create_r = requests.post(
            f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media",
            params={
                "media_type": "REELS",
                "video_url": raw_url,
                "caption": caption,
                "share_to_feed": "true",
                "access_token": FB_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        ).json()
        creation_id = create_r.get("id")
        if not creation_id:
            print(f"[Instagram] Container creation failed: {create_r}")
            return

        # Poll container until FINISHED
        for _ in range(24):
            time.sleep(10)
            status_r = requests.get(
                f"https://graph.facebook.com/v21.0/{creation_id}",
                params={"fields": "status_code", "access_token": FB_PAGE_ACCESS_TOKEN},
                timeout=15,
            ).json()
            code = status_r.get("status_code")
            print(f"[Instagram] Container status: {code}")
            if code == "FINISHED":
                pub_r = requests.post(
                    f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish",
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

            rel = int(ai.get("relevance_score", 0))
            ent = int(ai.get("entertainment_score", 0))
            is_rel = bool(ai.get("is_gta6_relevant", False))
            open_prof = bool(ai.get("has_opening_profanity", False))
            prof_lvl = str(ai.get("profanity_level", "none")).lower()

            print(f"[AI Score] relevant={is_rel} ({rel}/10), fun={ent}/10, profanity={prof_lvl}")
            if not is_rel or rel < MIN_RELEVANCE_SCORE or ent < MIN_ENTERTAINMENT_SCORE or open_prof or prof_lvl == "heavy":
                print(f"[Rejected] {ai.get('reject_reason', 'Below quality threshold')}")
                save_processed_id(STATE_FILE, cid)
                continue

            # Qualified! Render 9:16 Short
            final_mp4 = workdir / f"final_{cid}.mp4"
            yt_title = clean_word_boundary_title(ai.get("yt_title", clip["title"]), max_len=68, suffix=" #Shorts")
            banner_text = strip_emojis_and_non_ascii(ai.get("banner_text", "GTA 6 MOMENT"))
            hook_vo = strip_emojis_and_non_ascii(ai.get("hook_vo", ""))
            yt_desc = ai.get("yt_description", f"GTA 6 clip featuring {clip['broadcaster']}. #GTA6 #Shorts")
            yt_tags = ai.get("yt_tags", ["GTA 6", "Grand Theft Auto VI", "GTA VI", clip["broadcaster"], "Shorts"])

            ok = render_short(raw_mp4, final_mp4, banner_text, clip["broadcaster"], words, hook_vo, workdir)
            if not ok:
                continue

            # Publish across YouTube, Facebook, and Instagram
            upload_to_youtube(final_mp4, yt_title, yt_desc, yt_tags)
            social_caption = f"{yt_title}\n\n{yt_desc}"
            upload_to_facebook_reel(final_mp4, social_caption)
            upload_to_instagram_reel(final_mp4, social_caption, cid)

            save_processed_id(STATE_FILE, cid)
            print("[Done] Successfully published 1 high-quality GTA 6 Short.")
            return

    print("[Pipeline] No candidate passed the strict quality gate this run.")


if __name__ == "__main__":
    main()
