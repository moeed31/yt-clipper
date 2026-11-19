import asyncio
import json
import os
import re
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
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageStat

# Ensure chronological logs in GitHub Actions
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()

STATE_FILE = Path("processed_gta6_comp_clips.txt")
LOGO_PATH = Path("logo.png")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

TARGET_MIN_CLIPS = 5
TARGET_MAX_CLIPS = 10
TARGET_MAX_TOTAL_SEC = 480  # ~8 minutes ideal for retention & mid-roll eligibility

HARD_PROFANITY_PATTERNS = [
    r"\bfuck(?:ing|ed|er|s)?\b",
    r"\bmotherfuck(?:ing|er|s)?\b",
    r"\bshit(?:ting|ty|s)?\b",
    r"\bbitch(?:es|ing)?\b",
    r"\bcunt\b",
    r"\bnigg(?:a|er)s?\b",
]


# ==============================================================================
# HELPERS & SANITIZERS
# ==============================================================================
def load_processed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_ids(path: Path, clip_ids: List[str]) -> None:
    existing = load_processed_ids(path)
    with path.open("a", encoding="utf-8") as f:
        for cid in clip_ids:
            if cid and cid not in existing:
                f.write(f"{cid}\n")
                existing.add(cid)


def strip_non_ascii(text: str) -> str:
    cleaned = (text or "").encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_word_boundary_title(raw: str, max_len: int = 70) -> str:
    text = strip_non_ascii(raw).strip(" |:-")
    if not text:
        text = "Best GTA 6 Streamer Moments Today"
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-|!?")
    return cut or text[:max_len].strip()


def sanitize_yt_description(desc: str) -> str:
    return (desc or "").replace("<", "").replace(">", "").strip()[:4800]


def sanitize_yt_tags(tags: List[str], fallback: List[str]) -> List[str]:
    source = tags if isinstance(tags, list) and tags else fallback
    result: List[str] = []
    seen = set()
    total_chars = 0
    for t in source:
        clean = strip_non_ascii(str(t)).replace("#", "").strip(" ,")
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
    return result or ["GTA 6", "Grand Theft Auto 6", "GTA VI"]


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
    dur = float((info.get("format") or {}).get("duration", 0.0) or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in (info.get("streams") or []))
    return dur, has_audio


def count_profanity(text: str) -> int:
    return sum(len(re.findall(p, text or "", flags=re.IGNORECASE)) for p in HARD_PROFANITY_PATTERNS)


def fmt_chapter_time(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


# ==============================================================================
# TWITCH FETCHING (HELIX + KEYLESS GQL FALLBACK)
# ==============================================================================
def fetch_gta6_clips(processed: set) -> List[Dict]:
    clips: List[Dict] = []

    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        try:
            tok = requests.post(
                "[https://id.twitch.tv/oauth2/token](https://id.twitch.tv/oauth2/token)",
                params={
                    "client_id": TWITCH_CLIENT_ID,
                    "client_secret": TWITCH_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
                timeout=15,
            ).json().get("access_token")
            if tok:
                headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {tok}"}
                g = requests.get(
                    "[https://api.twitch.tv/helix/games](https://api.twitch.tv/helix/games)",
                    headers=headers,
                    params={"name": "Grand Theft Auto VI"},
                    timeout=15,
                ).json().get("data") or []
                if g:
                    started_at = datetime.fromtimestamp(time.time() - 72 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    c = requests.get(
                        "[https://api.twitch.tv/helix/clips](https://api.twitch.tv/helix/clips)",
                        headers=headers,
                        params={"game_id": g[0]["id"], "first": 50, "started_at": started_at},
                        timeout=20,
                    ).json().get("data") or []
                    for item in c:
                        clips.append({
                            "id": item.get("id", ""),
                            "url": item.get("url", ""),
                            "title": item.get("title", ""),
                            "broadcaster": item.get("broadcaster_name", "Streamer"),
                            "views": int(item.get("view_count", 0) or 0),
                            "duration": float(item.get("duration", 0) or 0),
                            "language": (item.get("language") or "en").lower(),
                        })
        except Exception as e:
            print(f"[Helix] Fallback to GQL: {e}")

    if len(clips) < 15:
        query = """
        query($gameName: String!, $limit: Int!,$filter: ClipsFilter!) {
          game(name: $gameName) {
            clips(first: $limit, criteria: { period:$filter }) {
              edges {
                node {
                  slug url title viewCount durationSeconds language
                  broadcaster { displayName login }
                }
              }
            }
          }
        }
        """
        headers = {"Client-ID": TWITCH_GQL_CLIENT_ID, "Content-Type": "application/json"}
        for period in ("LAST_DAY", "LAST_WEEK"):
            try:
                r = requests.post(
                    "[https://gql.twitch.tv/gql](https://gql.twitch.tv/gql)",
                    headers=headers,
                    json={"query": query, "variables": {"gameName": "Grand Theft Auto VI", "limit": 50, "filter": period}},
                    timeout=20,
                )
                edges = (((r.json() or {}).get("data") or {}).get("game") or {}).get("clips", {}).get("edges") or []
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    slug = node.get("slug")
                    if not slug:
                        continue
                    bc = node.get("broadcaster") or {}
                    clips.append({
                        "id": slug,
                        "url": node.get("url") or f"[https://clips.twitch.tv/](https://clips.twitch.tv/){slug}",
                        "title": node.get("title", ""),
                        "broadcaster": bc.get("displayName") or bc.get("login") or "Streamer",
                        "views": int(node.get("viewCount", 0) or 0),
                        "duration": float(node.get("durationSeconds", 0) or 0),
                        "language": (node.get("language") or "en").lower(),
                    })
                if len(clips) >= 25:
                    break
            except Exception as e:
                print(f"[GQL {period}] Error: {e}")

    seen = set()
    filtered: List[Dict] = []
    for c in clips:
        cid = c.get("id")
        if not cid or cid in processed or cid in seen:
            continue
        seen.add(cid)
        if not (12 <= c["duration"] <= 60):
            continue
        if c["language"] and not c["language"].startswith("en"):
            continue
        filtered.append(c)

    filtered.sort(key=lambda x: x["views"], reverse=True)
    return filtered[:28]


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
    except Exception:
        return False


# ==============================================================================
# GEMINI AI FILTERING & COMPILATION PACKAGING
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
            return parse_json_safely(resp.text)
        except Exception as e:
            print(f"[Gemini {model_name}] Error: {e}")
            time.sleep(2)
    return None


def score_clips_batch(clips_with_transcripts: List[Dict]) -> Optional[Dict]:
    items_summary = []
    for idx, c in enumerate(clips_with_transcripts):
        items_summary.append({
            "index": idx,
            "broadcaster": c["broadcaster"],
            "title": c["title"],
            "views": c["views"],
            "profanity_hits": c["profanity_hits"],
            "transcript": c["transcript"][:600],
        })

    prompt = f"""
You are the lead YouTube strategist for "The Rippers Gaming" (strictly GTA 6 / Grand Theft Auto VI).

Review these candidate Twitch clips for today's 16:9 Daily GTA 6 Compilation:
{json.dumps(items_summary, indent=2)}

STRICT RULES:
1. Select ONLY genuine GTA 6 gameplay or authentic GTA 6 reactions (relevance >= 7/10). Reject off-topic clips (other games, food/fasting, random drama, silent/boring clips).
2. CRITICAL: The FIRST clip in selected_clips MUST have ZERO profanity (profanity_hits == 0) AND be the single most exciting hook.
3. Exclude any clip with heavy profanity (profanity_hits >= 3).
4. Pick between {TARGET_MIN_CLIPS} and {TARGET_MAX_CLIPS} unique clips, ordered for maximum retention.
5. For each selected clip, write a short 3-6 word YouTube Chapter title (ASCII only, no emojis).
6. Write yt_title: STRICTLY under 68 characters, ASCII only, NO emojis, high-CTR curiosity title mentioning GTA 6 and the top 1-2 streamers featured.
7. Write thumb_text: STRICTLY 2 to 4 ALL-CAPS words for the thumbnail (e.g. "CHAOS IN VICE CITY" or "HE BROKE GTA 6").
8. Write intro_vo: 1 energetic narrator sentence (12 to 18 words, ~4.5 seconds) hooking viewers into today's best GTA 6 moments.
9. Write yt_description_intro: 2 natural SEO sentences summarizing today's highlights + 1 engaging question about one of the moments.
10. Provide 15 targeted GTA 6 SEO tags.

Return ONLY valid JSON:
{{
  "selected_clips": [
    {{ "index": 0, "chapter_title": "Kai Cenat Flees Vice City Cops" }}
  ],
  "yt_title": "",
  "thumb_text": "",
  "intro_vo": "",
  "yt_description_intro": "",
  "yt_tags": []
}}
"""
    return call_gemini_json(prompt)


# ==============================================================================
# VOICEOVER, CLIP STANDARDIZATION & HIGH-CTR THUMBNAIL
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
                    print(f"[edge-tts] Generated Intro VO ({dur:.1f}s) using {voice}")
                    return dur
        except Exception as e:
            print(f"[edge-tts] {voice} failed: {e}")

    if out_mp3.exists():
        out_mp3.unlink()
    print("[edge-tts] All voices unavailable on runner, proceeding without Intro VO.")
    return 0.0


def standardize_clip_16x9(
    raw_mp4: Path,
    out_mp4: Path,
    broadcaster: str,
    chapter_title: str,
    workdir: Path,
    intro_vo_path: Optional[Path] = None,
    intro_vo_dur: float = 0.0,
) -> float:
    """Normalizes clip to uniform 1080p30 stereo H.264/AAC for seamless -c copy concat."""
    dur, has_audio = probe_video(raw_mp4)
    badge_file = workdir / f"badge_{out_mp4.stem}.txt"
    badge_text = f"{strip_non_ascii(broadcaster).upper()[:18]}  |  {strip_non_ascii(chapter_title)[:32]}"
    badge_file.write_text(badge_text, encoding="utf-8")
    badge_escaped = escape_ffmpeg_path(badge_file)

    inputs = ["-i", str(raw_mp4)]
    vo_idx: Optional[int] = None
    logo_idx: Optional[int] = None
    next_idx = 1

    if intro_vo_path and intro_vo_path.exists() and intro_vo_dur > 0.2:
        inputs.extend(["-i", str(intro_vo_path)])
        vo_idx = next_idx
        next_idx += 1

    if LOGO_PATH.exists():
        inputs.extend(["-i", str(LOGO_PATH)])
        logo_idx = next_idx
        next_idx += 1

    v_filters = [
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[base]",
        "[base]drawbox=x=40:y=965:w=980:h=72:color=0x0B0B0D@0.82:t=fill:enable='lt(t,6)',"
        "drawbox=x=40:y=965:w=8:h=72:color=0x007BFF@1.0:t=fill:enable='lt(t,6)',"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{badge_escaped}':"
        "fontcolor=white:fontsize=28:x=64:y=987:enable='lt(t,6)'[vpre]",
    ]

    if logo_idx is not None:
        v_filters.append(f"[{logo_idx}:v]scale=110:-1[lg]")
        v_filters.append("[vpre][lg]overlay=W-w-36:36[vout]")
    else:
        v_filters.append("[vpre]copy[vout]")

    a_filters: List[str] = []
    if has_audio and vo_idx is not None:
        duck_end = intro_vo_dur + 0.2
        a_filters.append(f"[0:a]volume='if(lt(t,{duck_end:.2f}),0.20,1.0)':eval=frame[aduck]")
        a_filters.append(f"[{vo_idx}:a]volume=1.18[avo]")
        a_filters.append(
            "[aduck][avo]amix=inputs=2:duration=first:dropout_transition=1:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
    elif has_audio:
        a_filters.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    elif vo_idx is not None:
        a_filters.append(f"[{vo_idx}:a]apad,atrim=0:{dur:.2f},loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        a_filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.2f}[aout]")

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(v_filters + a_filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "30", "-video_track_timescale", "30000",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(out_mp4),
    ]
    run_cmd(cmd)
    actual_dur, _ = probe_video(out_mp4)
    return actual_dur


def build_high_ctr_thumbnail(first_clip_path: Path, thumb_text: str, out_jpg: Path, workdir: Path) -> bool:
    """Selects sharpest/brightest frame from Clip #1 and builds a 1280x720 high-CTR thumbnail."""
    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    run_cmd([
        "ffmpeg", "-y", "-i", str(first_clip_path),
        "-vf", "fps=1/3,scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
        "-frames:v", "6",
        str(frames_dir / "f_%02d.jpg"),
    ], check=False)

    frame_files = sorted(frames_dir.glob("f_*.jpg"))
    if not frame_files:
        return False

    best_frame = frame_files[0]
    best_score = -1.0
    for fpath in frame_files:
        im = Image.open(fpath).convert("RGB")
        stat = ImageStat.Stat(im)
        brightness = sum(stat.mean) / 3.0
        contrast = sum(stat.stddev) / 3.0
        score = contrast * 1.5 - abs(brightness - 135.0) * 0.4
        if score > best_score:
            best_score = score
            best_frame = fpath

    img = Image.open(best_frame).convert("RGBA")
    img = ImageEnhance.Contrast(img).enhance(1.22)
    img = ImageEnhance.Color(img).enhance(1.25)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(420, 720):
        alpha = int(215 * ((y - 420) / 300.0))
        draw_ov.line([(0, y), (1280, y)], fill=(8, 10, 16, alpha))
    draw_ov.rectangle([(0, 0), (1279, 719)], outline=(0, 123, 255, 255), width=10)
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    text = (strip_non_ascii(thumb_text).upper() or "GTA 6 CHAOS")[:28]
    font_size = 104
    font = load_font(font_size)
    while draw.textbbox((0, 0), text, font=font)[2] > 1180 and font_size > 52:
        font_size -= 6
        font = load_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    tx = max(20, (1280 - tw) // 2)
    ty = 555

    draw.text((tx, ty), text, font=font, fill=(255, 215, 0, 255), stroke_width=10, stroke_fill=(0, 0, 0, 255))

    badge_font = load_font(44)
    draw.rounded_rectangle([(36, 32), (225, 102)], radius=12, fill=(0, 123, 255, 240))
    draw.text((62, 42), "GTA 6", font=badge_font, fill=(255, 255, 255, 255))

    if LOGO_PATH.exists():
        lg = Image.open(LOGO_PATH).convert("RGBA")
        lg.thumbnail((125, 125))
        img.alpha_composite(lg, (1280 - lg.width - 36, 32))

    img.convert("RGB").save(out_jpg, "JPEG", quality=90)
    return out_jpg.exists() and out_jpg.stat().st_size < 2_000_000


# ==============================================================================
# PUBLISHING (YOUTUBE + THUMBNAIL + FACEBOOK)
# ==============================================================================
def upload_compilation_youtube(
    video_path: Path, thumb_path: Optional[Path], title: str, description: str, tags: List[str]
) -> Optional[str]:
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
    print(f"[YouTube] Published Compilation: https://youtu.be/{vid_id}")

    if vid_id and thumb_path and thumb_path.exists():
        try:
            yt.thumbnails().set(
                videoId=vid_id,
                media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
            ).execute()
            print("[YouTube] Custom high-CTR thumbnail uploaded.")
        except Exception as e:
            print(f"[YouTube] Thumbnail upload skipped/failed: {e}")
    return vid_id


def upload_compilation_facebook(video_path: Path, title: str, description: str) -> None:
    if not (FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN):
        return
    try:
        with video_path.open("rb") as f:
            r = requests.post(
                f"https://graph-video.facebook.com/v21.0/{FB_PAGE_ID}/videos",
                data={
                    "title": title,
                    "description": description[:4000],
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                },
                files={"source": f},
                timeout=300,
            )
            print(f"[Facebook] Video publish response: {r.json()}")
    except Exception as e:
        print(f"[Facebook] Upload error: {e}")


# ==============================================================================
# MAIN PIPELINE
# ==============================================================================
def main() -> None:
    processed = load_processed_ids(STATE_FILE)
    candidates = fetch_gta6_clips(processed)
    print(f"[Compilation] Found {len(candidates)} candidate GTA 6 clips.")
    if len(candidates) < TARGET_MIN_CLIPS:
        print("[Compilation] Not enough fresh clips today. Skipping upload to protect channel quality.")
        return

    whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        inspected: List[Dict] = []

        for c in candidates:
            if len(inspected) >= 16:
                break
            mp4 = workdir / f"raw_{c['id']}.mp4"
            if not download_clip(c["url"], mp4):
                continue
            segs, info = whisper_model.transcribe(str(mp4), beam_size=1, vad_filter=True)
            text = " ".join(s.text.strip() for s in segs).strip()
            if (info.language or "en") != "en" or len(text.split()) < 5:
                continue
            c["local_path"] = mp4
            c["transcript"] = text
            c["profanity_hits"] = count_profanity(text)
            inspected.append(c)

        if len(inspected) < TARGET_MIN_CLIPS:
            print("[Compilation] Fewer than minimum English clips after inspection. Skipping.")
            return

        plan = score_clips_batch(inspected)
        raw_selected = (plan or {}).get("selected_clips") or []
        if len(raw_selected) < TARGET_MIN_CLIPS:
            print("[Compilation] Gemini quality gate did not find enough relevant GTA 6 clips.")
            return

        # Deduplicate selections and enforce valid indices
        ordered_items: List[Tuple[Dict, str]] = []
        seen_idx = set()
        for sel in raw_selected:
            idx = sel.get("index")
            if not isinstance(idx, int) or idx in seen_idx or not (0 <= idx < len(inspected)):
                continue
            clip = inspected[idx]
            if clip["profanity_hits"] >= 3:
                continue
            seen_idx.add(idx)
            chap = strip_non_ascii(sel.get("chapter_title", clip["broadcaster"])) or clip["broadcaster"]
            ordered_items.append((clip, chap))

        # Promote a zero-profanity clip to slot #1 so opening 30s + intro VO stay 100% clean
        clean_pos = next((i for i, (c, _) in enumerate(ordered_items) if c["profanity_hits"] == 0), None)
        if clean_pos is not None and clean_pos > 0:
            ordered_items.insert(0, ordered_items.pop(clean_pos))

        if len(ordered_items) < TARGET_MIN_CLIPS:
            print("[Compilation] Insufficient clean clips after safety ordering.")
            return

        intro_vo_text = strip_non_ascii(plan.get("intro_vo", ""))
        intro_vo_path = workdir / "intro_vo.mp3"
        intro_vo_dur = generate_edge_tts(intro_vo_text, intro_vo_path) if intro_vo_text else 0.0

        concat_entries: List[str] = []
        chapters: List[str] = []
        used_ids: List[str] = []
        featured_streamers: List[str] = []
        elapsed = 0.0
        first_std_clip: Optional[Path] = None

        for order_idx, (clip, chap_title) in enumerate(ordered_items):
            is_first = (len(used_ids) == 0)
            std_mp4 = workdir / f"std_{order_idx:02d}.mp4"
            try:
                actual_dur = standardize_clip_16x9(
                    clip["local_path"],
                    std_mp4,
                    clip["broadcaster"],
                    chap_title,
                    workdir,
                    intro_vo_path=intro_vo_path if is_first else None,
                    intro_vo_dur=intro_vo_dur if is_first else 0.0,
                )
            except Exception as e:
                print(f"[Standardize] Skipping clip {clip['id']} due to FFmpeg error: {e}")
                continue

            if elapsed + actual_dur > TARGET_MAX_TOTAL_SEC and len(used_ids) >= TARGET_MIN_CLIPS:
                break

            if first_std_clip is None:
                first_std_clip = std_mp4

            chapters.append(f"{fmt_chapter_time(elapsed)} - {clip['broadcaster']}: {chap_title}")
            elapsed += actual_dur
            concat_entries.append(f"file '{std_mp4.as_posix()}'")
            used_ids.append(clip["id"])
            if clip["broadcaster"] not in featured_streamers:
                featured_streamers.append(clip["broadcaster"])

        if len(used_ids) < TARGET_MIN_CLIPS or first_std_clip is None:
            print("[Compilation] Insufficient valid clips after assembly.")
            return

        concat_list = workdir / "concat.txt"
        concat_list.write_text("\n".join(concat_entries) + "\n", encoding="utf-8")
        final_mp4 = workdir / "gta6_daily_compilation.mp4"
        run_cmd([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy", "-movflags", "+faststart",
            str(final_mp4),
        ])

        thumb_jpg = workdir / "thumbnail.jpg"
        thumb_ok = build_high_ctr_thumbnail(
            first_std_clip, plan.get("thumb_text", "GTA 6 CHAOS"), thumb_jpg, workdir
        )

        yt_title = clean_word_boundary_title(plan.get("yt_title", "Best GTA 6 Streamer Moments Today"), max_len=70)
        desc_intro = plan.get("yt_description_intro", "Today's top GTA 6 moments and reactions from Twitch.")
        chapters_block = "\n".join(chapters)
        credits_line = ", ".join(featured_streamers)
        yt_description = sanitize_yt_description(
            f"{desc_intro}\n\n"
            f"Chapters:\n{chapters_block}\n\n"
            f"Featured Creators: {credits_line}\n"
            f"Subscribe to The Rippers Gaming for daily GTA 6 highlights!\n\n"
            f"#GTA6 #GrandTheftAuto6 #GTAVI #ViceCity #GamingHighlights"
        )
        yt_tags = sanitize_yt_tags(
            plan.get("yt_tags", []),
            ["GTA 6", "Grand Theft Auto 6", "GTA VI", "GTA 6 Highlights", "Vice City"],
        )

        vid_id = upload_compilation_youtube(final_mp4, thumb_jpg if thumb_ok else None, yt_title, yt_description, yt_tags)
        upload_compilation_facebook(final_mp4, yt_title, yt_description)

        if vid_id or not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
            save_processed_ids(STATE_FILE, used_ids)
        print(f"[Compilation] Done! Published {len(used_ids)} clips ({elapsed:.1f}s total).")


if __name__ == "__main__":
    main()
