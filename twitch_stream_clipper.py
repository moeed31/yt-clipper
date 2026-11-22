import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()
# Set YT_PRIVACY_STATUS=unlisted in workflow env if you want to review uploads manually
YT_PRIVACY_STATUS = os.environ.get("YT_PRIVACY_STATUS", "public").strip().lower()
if YT_PRIVACY_STATUS not in {"public", "unlisted", "private"}:
    YT_PRIVACY_STATUS = "public"

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.environ.get("IG_USER_ID", "").strip()

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main").strip()

STATE_FILE = Path("processed_stream_segments.txt")
LOGO_PATH = Path("logo.png")
IG_STAGE_DIR = Path("ig_stage")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Auto-link-proof endpoint constants
GOOGLE_TOKEN_URI = "https://" "oauth2.googleapis.com/token"
TWITCH_GQL_URI = "https://" "gql.twitch.tv/gql"
TWITCH_LIVE_BASE = "https://" "www.twitch.tv/"
TWITCH_VOD_BASE = "https://" "www.twitch.tv/videos/"
GRAPH_FB_BASE = "https://" "graph.facebook.com/v21.0"
GH_RAW_BASE = "https://" "raw.githubusercontent.com"

# Stream capture & highlight settings
CAPTURE_SECONDS = 150          # 2.5-minute raw capture window from live stream / VOD
MIN_HIGHLIGHT_SEC = 18.0
MAX_HIGHLIGHT_SEC = 48.0
MIN_ENTERTAINMENT_SCORE = 7

# Set to True if you want to block GTA 6 completely while your channels recover.
# Leave False to allow GTA 6 ONLY when Gemini Vision verifies 100% clean gameplay.
BLOCK_GTA6_COMPLETELY = False

# Optional: lock to specific games (lowercase) for tight niche focus, e.g. ["valorant", "counter-strike"]
# Leave empty [] to clip any safe gaming stream from FAMOUS_STREAMERS.
ALLOWED_GAMES: List[str] = []

BLOCKED_CATEGORIES = {
    "pools, hot tubs, and beaches",
    "asmr",
    "slots",
    "virtual casino",
    "casino",
    "art",
    "body art",
    "i'm only sleeping",
}

FAMOUS_STREAMERS = [
    "kaicenat", "caseoh_", "jynxzi", "shroud", "tarik", "lirik",
    "summit1g", "xqc", "ohnepixel", "plaqueboymax", "clix",
    "stableronaldo", "jasontheween", "sodapoppin", "moistcr1tikal",
    "emiru", "esfandtv", "nmplol", "agent00", "fanum",
    "nickmercs", "timthetatman", "tfue", "tenz", "s1mple",
]

HARD_PROFANITY_PATTERNS = [
    r"\bfuck(?:ing|ed|er|s)?\b",
    r"\bmotherfuck(?:ing|er|s)?\b",
    r"\bshit(?:ting|ty|s)?\b",
    r"\bbitch(?:es|ing)?\b",
    r"\bcunt\b",
    r"\bnigg(?:a|er)s?\b",
    r"\bfagg?ot(?:s)?\b",
    r"\bretard(?:ed|s)?\b",
]


# ==============================================================================
# UTILITY & SANITIZATION HELPERS
# ==============================================================================
def load_processed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_id(path: Path, item_id: str) -> None:
    existing = load_processed_ids(path)
    if item_id and item_id not in existing:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{item_id}\n")


def strip_emojis_and_non_ascii(text: str) -> str:
    cleaned = (text or "").encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_word_boundary_title(raw: str, max_len: int = 68, suffix: str = "") -> str:
    text = strip_emojis_and_non_ascii(raw)
    text = re.sub(r"#\w+", "", text).strip(" |:-")
    if not text:
        text = "Epic Streamer Gaming Moment"
    budget = max(10, max_len - len(suffix))
    if len(text) <= budget:
        return f"{text}{suffix}".strip()
    cut = text[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-|!?")
    return f"{cut or text[:budget].strip()}{suffix}".strip()


def sanitize_yt_description(desc: str, fallback: str) -> str:
    return (desc or fallback).replace("<", "").replace(">", "").strip()[:4800]


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
    return result or ["Twitch", "Gaming", "Shorts"]


def escape_ffmpeg_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def run_cmd(cmd: List[str], check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check, timeout=timeout
    )


def probe_video(path: Path) -> Tuple[float, bool]:
    res = run_cmd([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    info = json.loads(res.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration", 0.0) or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in (info.get("streams") or []))
    return duration, has_audio


def count_profanity(text: str) -> int:
    return sum(len(re.findall(pat, text or "", flags=re.IGNORECASE)) for pat in HARD_PROFANITY_PATTERNS)


def is_category_allowed(game_name: str) -> bool:
    g = (game_name or "").strip().lower()
    if not g:
        return False
    if g in BLOCKED_CATEGORIES:
        return False
    if BLOCK_GTA6_COMPLETELY and ("grand theft auto vi" in g or "gta 6" in g or "gta vi" in g):
        return False
    if ALLOWED_GAMES and not any(allowed in g for allowed in ALLOWED_GAMES):
        return False
    return True


# ==============================================================================
# TWITCH LIVE STREAM & RECENT VOD DISCOVERY (KEYLESS GQL)
# ==============================================================================
def fetch_live_and_vod_sources(processed: set) -> List[Dict]:
    """Queries famous streamers via Twitch GQL; prioritizes live streams, falls back to recent VODs."""
    query = """
    query($login: String!) {
      user(login: $login) {
        displayName
        login
        stream {
          id
          title
          viewersCount
          game { name }
        }
        videos(first: 2, type: ARCHIVE, sort: TIME) {
          edges {
            node {
              id
              title
              lengthSeconds
              viewCount
              game { name }
            }
          }
        }
      }
    }
    """
    headers = {"Client-ID": TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"}
    live_targets: List[Dict] = []
    vod_targets: List[Dict] = []

    streamers = list(FAMOUS_STREAMERS)
    random.shuffle(streamers)

    for login in streamers:
        try:
            r = requests.post(
                TWITCH_GQL_URI,
                headers=headers,
                json={"query": query, "variables": {"login": login}},
                timeout=12,
            )
            user = ((r.json() or {}).get("data") or {}).get("user")
            if not user:
                continue
            display_name = user.get("displayName") or login

            # 1. Check active live stream
            stream = user.get("stream")
            if stream and stream.get("id"):
                game_name = ((stream.get("game") or {}).get("name") or "")
                state_key = f"live_{stream['id']}"
                if state_key not in processed and is_category_allowed(game_name):
                    live_targets.append({
                        "mode": "live",
                        "state_key": state_key,
                        "url": f"{TWITCH_LIVE_BASE}{login}",
                        "broadcaster": display_name,
                        "login": login,
                        "title": stream.get("title", ""),
                        "game": game_name,
                        "viewers": int(stream.get("viewersCount", 0) or 0),
                        "offset_sec": 0,
                    })

            # 2. Collect recent VODs as fallback
            edges = ((user.get("videos") or {}).get("edges") or [])
            for edge in edges:
                node = (edge or {}).get("node") or {}
                vid_id = node.get("id")
                length_sec = int(node.get("lengthSeconds", 0) or 0)
                game_name = ((node.get("game") or {}).get("name") or "")
                if not vid_id or length_sec < 1200 or not is_category_allowed(game_name):
                    continue
                # Pick a mid-stream slice (skip first 15 min intro/starting-soon & last 10 min outro)
                safe_start = max(900, int(length_sec * 0.25))
                safe_end = max(safe_start + 300, int(length_sec * 0.80))
                # Round offset to 5-minute buckets for deterministic state tracking
                offset_sec = (random.randint(safe_start, safe_end) // 300) * 300
                state_key = f"vod_{vid_id}_{offset_sec}"
                if state_key in processed:
                    continue
                vod_targets.append({
                    "mode": "vod",
                    "state_key": state_key,
                    "url": f"{TWITCH_VOD_BASE}{vid_id}",
                    "broadcaster": display_name,
                    "login": login,
                    "title": node.get("title", ""),
                    "game": game_name,
                    "viewers": int(node.get("viewCount", 0) or 0),
                    "offset_sec": offset_sec,
                })
        except Exception:
            continue

    live_targets.sort(key=lambda x: x["viewers"], reverse=True)
    vod_targets.sort(key=lambda x: x["viewers"], reverse=True)
    # Try up to 4 live streams first, then up to 4 recent VOD slices
    return live_targets[:4] + vod_targets[:4]


# ==============================================================================
# LIVE STREAM / VOD SEGMENT CAPTURE VIA YT-DLP + FFMPEG
# ==============================================================================
def resolve_twitch_hls_url(twitch_url: str) -> Optional[str]:
    try:
        res = run_cmd(
            [
                "yt-dlp",
                "--no-warnings",
                "--no-playlist",
                "-g",
                "-f", "best[height<=1080]/best",
                twitch_url,
            ],
            timeout=30,
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip().startswith("http")]
        return lines[0] if lines else None
    except Exception as e:
        print(f"[yt-dlp] Could not resolve HLS URL for {twitch_url}: {e}")
        return None


def capture_stream_segment(target: Dict, out_mp4: Path) -> bool:
    hls_url = resolve_twitch_hls_url(target["url"])
    if not hls_url:
        return False

    cmd = ["ffmpeg", "-y", "-rw_timeout", "15000000"]
    if target["mode"] == "vod" and target["offset_sec"] > 0:
        cmd.extend(["-ss", str(target["offset_sec"])])
    cmd.extend([
        "-i", hls_url,
        "-t", str(CAPTURE_SECONDS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        str(out_mp4),
    ])

    timeout_sec = (CAPTURE_SECONDS + 110) if target["mode"] == "live" else 180
    try:
        print(f"[Capture] Recording {CAPTURE_SECONDS}s from {target['broadcaster']} ({target['mode'].upper()} - {target['game']})...")
        res = run_cmd(cmd, check=False, timeout=timeout_sec)
        if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 300_000:
            print(f"[Capture] FFmpeg capture failed: {(res.stderr or '')[-500:]}")
            return False
        dur, _ = probe_video(out_mp4)
        return dur >= 35.0
    except subprocess.TimeoutExpired:
        print("[Capture] Timed out capturing stream segment.")
        return False


# ==============================================================================
# LOCAL TRANSCRIPTION & GEMINI AI HIGHLIGHT + VISION SAFETY GATE
# ==============================================================================
def transcribe_segment(whisper_model: WhisperModel, video_path: Path) -> Tuple[str, List[Dict], str]:
    segments, info = whisper_model.transcribe(
        str(video_path),
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
    )
    words: List[Dict] = []
    timed_lines: List[str] = []
    for seg in segments:
        txt = strip_emojis_and_non_ascii(seg.text)
        if txt:
            timed_lines.append(f"[{seg.start:.1f}s - {seg.end:.1f}s] {txt}")
        if seg.words:
            for w in seg.words:
                cleaned = strip_emojis_and_non_ascii(w.word).strip()
                if cleaned:
                    words.append({"word": cleaned, "start": float(w.start), "end": float(w.end)})
    return "\n".join(timed_lines), words, (info.language or "en")


def parse_json_safely(raw_text: str) -> Optional[Dict]:
    text = (raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def call_gemini_json(contents: Any) -> Tuple[Optional[Dict], bool]:
    """Returns (parsed_json, blocked_by_safety_flag)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    for model_name in dict.fromkeys(GEMINI_MODELS):
        if not model_name:
            continue
        for attempt in range(1, 4):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                )
                # Detect safety block (fail-closed)
                candidates = getattr(resp, "candidates", None) or []
                if candidates:
                    finish_reason = str(getattr(candidates[0], "finish_reason", "")).upper()
                    if "SAFETY" in finish_reason or "PROHIBITED" in finish_reason:
                        print(f"[Gemini] Response blocked by safety filter ({finish_reason}).")
                        return None, True
                parsed = parse_json_safely(getattr(resp, "text", ""))
                if parsed is not None:
                    return parsed, False
            except Exception as e:
                err_str = str(e)
                print(f"[Gemini] {model_name} (try {attempt}/3) failed: {e}")
                if "SAFETY" in err_str.upper() or "PROHIBITED" in err_str.upper():
                    return None, True
                if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")):
                    time.sleep(4 * attempt)
                    continue
                break
    return None, False


def find_best_clean_highlight(target: Dict, timed_transcript: str, segment_dur: float) -> Optional[Dict]:
    prompt = f"""
You are a YouTube Shorts editor for "The Rippers Gaming".
Your job is to pick ONE clean, high-retention 20 to 45 second highlight from this {segment_dur:.0f}-second Twitch stream segment.

Stream Metadata:
- Broadcaster: {target['broadcaster']}
- Game: {target['game']}
- Stream Title: {target['title']}
- Timestamped Transcript:
{timed_transcript[:5500]}

STRICT RULES:
1. Pick the single funniest, highest-energy, or most exciting continuous window between {int(MIN_HIGHLIGHT_SEC)}s and {int(MAX_HIGHLIGHT_SEC)}s long.
2. STRICT SAFETY: Reject (found_good_clip=false) if the highlight has sexual/NSFW topics, nudity talk, strip clubs, hate speech, or heavy swearing/profanity.
3. Reject boring menu navigation, dead air, or sponsor reads.
4. Keep yt_title STRICTLY under 58 characters, ASCII only, NO emojis, NO hashtags. Mention {target['broadcaster']} and the game/moment naturally.
5. Keep banner_text STRICTLY 2 to 5 punchy words, ALL CAPS, ASCII only, NO emojis.
6. Write hook_vo: 1 energetic narrator sentence (8 to 13 words max, ~3 seconds) introducing what is about to happen.
7. Write yt_description: 2 natural sentences describing the moment + credit to {target['broadcaster']} on Twitch + 1 specific question for viewers + 4 relevant hashtags including #Shorts.
8. Provide 12 SEO tags (list of strings).

Return ONLY valid JSON:
{{
  "found_good_clip": true,
  "start_sec": 0.0,
  "end_sec": 30.0,
  "entertainment_score": 0,
  "reject_reason": "",
  "yt_title": "",
  "banner_text": "",
  "hook_vo": "",
  "yt_description": "",
  "yt_tags": []
}}
"""
    data, blocked = call_gemini_json(prompt)
    if blocked or not data:
        return None
    return data


def slice_highlight_clip(segment_mp4: Path, out_clip: Path, start_sec: float, end_sec: float) -> bool:
    clip_len = max(MIN_HIGHLIGHT_SEC, min(MAX_HIGHLIGHT_SEC, end_sec - start_sec))
    res = run_cmd(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.2f}",
            "-i", str(segment_mp4),
            "-t", f"{clip_len:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_clip),
        ],
        check=False,
    )
    return res.returncode == 0 and out_clip.exists() and out_clip.stat().st_size > 100_000


def visual_safety_audit_clip(clip_mp4: Path, clip_transcript: str, workdir: Path) -> Tuple[bool, str]:
    """
    FAIL-CLOSED Gemini Vision Audit.
    Extracts 1 frame every 3 seconds and inspects every frame for nudity, strip clubs,
    lingerie/explicit outfits, gore, or Twitch ad/BRB screens.
    """
    frames_dir = workdir / "audit_frames"
    frames_dir.mkdir(exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()

    run_cmd(
        [
            "ffmpeg", "-y", "-i", str(clip_mp4),
            "-vf", "fps=1/3,scale=640:-2",
            "-q:v", "5",
            "-frames:v", "16",
            str(frames_dir / "frame_%02d.jpg"),
        ],
        check=False,
    )

    frame_files = sorted(frames_dir.glob("frame_*.jpg"))
    if len(frame_files) < 3:
        return False, "Could not extract audit frames"

    image_parts = []
    for fp in frame_files:
        image_parts.append(types.Part.from_bytes(data=fp.read_bytes(), mime_type="image/jpeg"))

    audit_prompt = f"""
You are a strict YouTube, Facebook, and Instagram Community Guidelines content moderator.
Inspect ALL attached video frames (taken every 3 seconds from a gaming Short) AND this transcript:
"{clip_transcript[:1000]}"

ZERO-TOLERANCE REJECTION CHECKLIST (set is_safe=false if ANY frame violates):
1. Nudity, partial nudity, exposed breasts/buttocks/genitals (real or in-game, including GTA strip clubs, beach/bikini close-ups, lingerie, explicit outfits, or sexual cutscenes).
2. Sexually suggestive dancing, pole dancing, twerking, explicit gestures, or NSFW mods.
3. Extreme gore, graphic torture, or excessive blood/dismemberment.
4. Twitch purple "Commercial break in progress" ad screens, "Starting Soon", "BRB", or static frozen screens.
5. Hate symbols, slurs, or self-harm imagery.

When in doubt, ALWAYS mark is_safe=false to protect the channel from strikes and age-restrictions.

Return ONLY valid JSON:
{{
  "is_safe": false,
  "has_nudity_or_suggestive": false,
  "has_extreme_gore": false,
  "is_ad_or_brb_screen": false,
  "reason": ""
}}
"""
    result, blocked = call_gemini_json([audit_prompt] + image_parts)
    if blocked:
        return False, "Blocked by Gemini built-in safety filter (likely NSFW)"
    if not result:
        return False, "Vision audit unavailable (fail-closed)"

    is_safe = bool(result.get("is_safe", False))
    nudity = bool(result.get("has_nudity_or_suggestive", False))
    gore = bool(result.get("has_extreme_gore", False))
    ad_screen = bool(result.get("is_ad_or_brb_screen", False))
    reason = str(result.get("reason", "Failed safety audit"))

    if not is_safe or nudity or gore or ad_screen:
        return False, reason
    return True, "Passed full visual & audio safety audit"


# ==============================================================================
# VOICEOVER, SUBTITLES & 9:16 SHORTS RENDERING
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
    for i in range(0, len(words), 3):
        chunk = words[i:i + 3]
        start = chunk[0]["start"]
        end = max(chunk[-1]["end"], start + 0.35)
        if end <= vo_dur:
            continue
        start = max(start, vo_dur + 0.05)
        if end <= start + 0.05:
            end = start + 0.35
        text = " ".join(w["word"].upper() for w in chunk)
        text = text.replace("{", "").replace("}", "").replace("\\", "")
        if text.strip():
            dialogues.append(f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},Default,,0,0,0,,{text}")

    ass_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return len(dialogues)


def render_short(
    clip_mp4: Path,
    out_mp4: Path,
    banner_text: str,
    broadcaster: str,
    words: List[Dict],
    hook_vo: str,
    workdir: Path,
) -> bool:
    duration, has_audio = probe_video(clip_mp4)
    if duration < MIN_HIGHLIGHT_SEC - 2:
        return False

    target_dur = min(duration, 56.0)
    vo_path = workdir / "hook_vo.mp3"
    vo_dur = generate_edge_tts(strip_emojis_and_non_ascii(hook_vo), vo_path) if hook_vo else 0.0

    clean_banner = (strip_emojis_and_non_ascii(banner_text).upper() or "STREAM HIGHLIGHT")[:36]
    banner_fontsize = 50 if len(clean_banner) <= 22 else (42 if len(clean_banner) <= 30 else 34)

    banner_file = workdir / "banner.txt"
    banner_file.write_text(clean_banner, encoding="utf-8")

    credit_file = workdir / "credit.txt"
    credit_file.write_text(f"TWITCH: {strip_emojis_and_non_ascii(broadcaster).upper()[:24]}", encoding="utf-8")

    ass_path = workdir / "subs.ass"
    sub_count = build_ass_subtitles(words, ass_path, vo_dur)

    banner_escaped = escape_ffmpeg_path(banner_file)
    credit_escaped = escape_ffmpeg_path(credit_file)
    ass_escaped = escape_ffmpeg_path(ass_path)

    inputs = ["-i", str(clip_mp4)]
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

    res = run_cmd(
        [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", ";".join(v_filters + a_filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{target_dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(out_mp4),
        ],
        check=False,
    )
    if res.returncode != 0:
        print(f"[FFmpeg Render Error]\n{(res.stderr or '')[-1200:]}")
        return False
    return out_mp4.exists() and out_mp4.stat().st_size > 100_000


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
            "privacyStatus": YT_PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    resp = yt.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    vid_id = resp.get("id")
    print(f"[YouTube] Published Short ({YT_PRIVACY_STATUS}) ID: {vid_id}")
    return vid_id


def upload_to_facebook_reel(video_path: Path, caption: str) -> None:
    if not (FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN):
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

        with video_path.open("rb") as f:
            up_r = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {FB_PAGE_ACCESS_TOKEN}",
                    "offset": "0",
                    "file_size": str(video_path.stat().st_size),
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
        print(f"[Instagram] Git staging failed: {e}")
        if staged_file.exists():
            staged_file.unlink()
        return None

    raw_url = f"{GH_RAW_BASE}/{GITHUB_REPOSITORY}/{GITHUB_REF_NAME}/ig_stage/{filename}"
    for _ in range(12):
        try:
            if requests.head(raw_url, timeout=10).status_code == 200:
                return raw_url
        except Exception:
            pass
        time.sleep(5)
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
            print(f"[Instagram] Cleanup note: {e}")


def upload_to_instagram_reel(video_path: Path, caption: str, unique_key: str) -> None:
    if not (IG_USER_ID and FB_PAGE_ACCESS_TOKEN):
        return
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", unique_key)[:40] or str(int(time.time()))
    filename = f"stream_{safe_id}.mp4"
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
            print(f"[Instagram] Container failed: {create_r}")
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
    targets = fetch_live_and_vod_sources(processed)
    print(f"[Pipeline] Found {len(targets)} candidate famous streamer broadcasts (live + VODs).")
    if not targets:
        print("[Pipeline] No eligible streams found right now. Exiting cleanly.")
        return

    whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for target in targets:
            state_key = target["state_key"]
            print(f"\n[Target] {target['broadcaster']} | {target['game']} ({target['mode'].upper()})")
            segment_mp4 = workdir / f"seg_{state_key}.mp4"

            if not capture_stream_segment(target, segment_mp4):
                continue

            seg_dur, _ = probe_video(segment_mp4)
            timed_transcript, words, lang = transcribe_segment(whisper_model, segment_mp4)
            if lang != "en" or len(words) < 18:
                print(f"[Skip] Non-English ({lang}) or too little speech ({len(words)} words).")
                save_processed_id(STATE_FILE, state_key)
                continue

            pick = find_best_clean_highlight(target, timed_transcript, seg_dur)
            if not pick or not pick.get("found_good_clip"):
                reason = (pick or {}).get("reject_reason", "No standout clean moment")
                print(f"[Skip] Highlight finder rejected segment: {reason}")
                save_processed_id(STATE_FILE, state_key)
                continue

            ent = int(pick.get("entertainment_score", 0) or 0)
            start_sec = max(0.0, float(pick.get("start_sec", 0.0) or 0.0))
            end_sec = min(seg_dur, float(pick.get("end_sec", start_sec + 30.0) or (start_sec + 30.0)))
            if ent < MIN_ENTERTAINMENT_SCORE or (end_sec - start_sec) < MIN_HIGHLIGHT_SEC:
                print(f"[Skip] Low score ({ent}/10) or short window ({end_sec - start_sec:.1f}s).")
                save_processed_id(STATE_FILE, state_key)
                continue

            # Extract shifted words & verify zero hard profanity inside the chosen window
            clip_words = [
                {"word": w["word"], "start": w["start"] - start_sec, "end": w["end"] - start_sec}
                for w in words
                if start_sec <= w["start"] <= end_sec
            ]
            clip_text = " ".join(w["word"] for w in clip_words)
            if count_profanity(clip_text) > 0:
                print("[Skip] Hard profanity detected inside chosen highlight window.")
                save_processed_id(STATE_FILE, state_key)
                continue

            raw_clip_mp4 = workdir / f"clip_{state_key}.mp4"
            if not slice_highlight_clip(segment_mp4, raw_clip_mp4, start_sec, end_sec):
                continue

            # MANDATORY FAIL-CLOSED VISUAL SAFETY AUDIT (NO NUDITY / NO AD SCREENS)
            is_safe, audit_reason = visual_safety_audit_clip(raw_clip_mp4, clip_text, workdir)
            print(f"[Vision Audit] safe={is_safe} | {audit_reason}")
            if not is_safe:
                save_processed_id(STATE_FILE, state_key)
                continue

            # Render final 9:16 Short
            final_mp4 = workdir / f"final_{state_key}.mp4"
            yt_title = clean_word_boundary_title(
                pick.get("yt_title", f"{target['broadcaster']} {target['game']} Moment"),
                max_len=68,
                suffix=" #Shorts",
            )
            banner_text = strip_emojis_and_non_ascii(pick.get("banner_text", target["game"].upper()))
            hook_vo = strip_emojis_and_non_ascii(pick.get("hook_vo", ""))
            yt_desc = sanitize_yt_description(
                pick.get("yt_description", ""),
                f"{target['broadcaster']} playing {target['game']} live on Twitch. #Gaming #Twitch #Shorts",
            )
            yt_tags = sanitize_yt_tags(
                pick.get("yt_tags", []),
                [target["broadcaster"], target["game"], "Twitch Highlights", "Gaming", "Shorts"],
            )

            if not render_short(raw_clip_mp4, final_mp4, banner_text, target["broadcaster"], clip_words, hook_vo, workdir):
                continue

            vid_id = upload_to_youtube(final_mp4, yt_title, yt_desc, yt_tags)
            social_caption = f"{yt_title}\n\n{yt_desc}"
            if YT_PRIVACY_STATUS == "public":
                upload_to_facebook_reel(final_mp4, social_caption)
                upload_to_instagram_reel(final_mp4, social_caption, state_key)

            if vid_id or not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
                save_processed_id(STATE_FILE, state_key)
            print("[Done] Successfully clipped, audited, and published stream highlight.")
            return

    print("[Pipeline] No candidate stream segment passed both highlight and Vision safety gates.")


if __name__ == "__main__":
    main()
