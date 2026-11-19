import asyncio
import json
import os
import re
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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat

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

STATE_FILE = Path("processed_gta6_comp_clips.txt")
LOGO_PATH = Path("logo.png")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

TTS_VOICE = "en-US-AndrewNeural"
TTS_RATE = "+6%"

TARGET_MIN_CLIPS = 5
TARGET_MAX_CLIPS = 10
TARGET_MAX_TOTAL_SEC = 480  # ~8 minutes ideal for mid-roll & retention

HARD_PROFANITY_PATTERNS = [
    r"\bfuck(?:ing|ed|er|s)?\b",
    r"\bmotherfuck(?:ing|er|s)?\b",
    r"\bshit(?:ting|ty|s)?\b",
    r"\bbitch(?:es|ing)?\b",
    r"\bcunt\b",
    r"\bnigg(?:a|er)s?\b",
]


# ==============================================================================
# HELPERS
# ==============================================================================
def load_processed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_processed_ids(path: Path, clip_ids: List[str]) -> None:
    existing = load_processed_ids(path)
    with path.open("a", encoding="utf-8") as f:
        for cid in clip_ids:
            if cid not in existing:
                f.write(f"{cid}\n")
                existing.add(cid)


def strip_non_ascii(text: str) -> str:
    return re.sub(r"\s+", " ", text.encode("ascii", "ignore").decode("ascii")).strip()


def clean_word_boundary_title(raw: str, max_len: int = 70) -> str:
    text = strip_non_ascii(raw).strip(" |:-")
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-|!?")


def run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def probe_video(path: Path) -> Tuple[float, bool]:
    res = run_cmd([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    info = json.loads(res.stdout)
    dur = float(info.get("format", {}).get("duration", 0.0))
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return dur, has_audio


def count_profanity(text: str) -> int:
    return sum(len(re.findall(p, text, flags=re.IGNORECASE)) for p in HARD_PROFANITY_PATTERNS)


def fmt_chapter_time(seconds: float) -> str:
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ==============================================================================
# TWITCH FETCHING
# ==============================================================================
def fetch_gta6_clips(processed: set) -> List[Dict]:
    clips: List[Dict] = []

    # 1. Helix API if configured
    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        try:
            tok = requests.post(
                "https://id.twitch.tv/oauth2/token",
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
                    "https://api.twitch.tv/helix/games",
                    headers=headers,
                    params={"name": "Grand Theft Auto VI"},
                    timeout=15,
                ).json().get("data", [])
                if g:
                    started_at = datetime.fromtimestamp(time.time() - 72 * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    c = requests.get(
                        "https://api.twitch.tv/helix/clips",
                        headers=headers,
                        params={"game_id": g[0]["id"], "first": 50, "started_at": started_at},
                        timeout=20,
                    ).json().get("data", [])
                    for item in c:
                        clips.append({
                            "id": item["id"],
                            "url": item["url"],
                            "title": item.get("title", ""),
                            "broadcaster": item.get("broadcaster_name", "Streamer"),
                            "views": int(item.get("view_count", 0)),
                            "duration": float(item.get("duration", 0)),
                            "language": (item.get("language") or "en").lower(),
                        })
        except Exception as e:
            print(f"[Helix] Fallback to GQL: {e}")

    # 2. Keyless GQL Fallback
    if len(clips) < 15:
        query = """
        query($gameName: String!, $limit: Int!, $filter: ClipsFilter!) {
          game(name: $gameName) {
            clips(first: $limit, criteria: { period: $filter }) {
              edges {
                node {
                  slug url title viewCount durationSeconds language
                  broadcaster { displayName }
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
                    "https://gql.twitch.tv/gql",
                    headers=headers,
                    json={"query": query, "variables": {"gameName": "Grand Theft Auto VI", "limit": 50, "filter": period}},
                    timeout=20,
                )
                edges = ((r.json().get("data") or {}).get("game") or {}).get("clips", {}).get("edges", [])
                for edge in edges:
                    node = edge.get("node", {})
                    clips.append({
                        "id": node.get("slug"),
                        "url": node.get("url") or f"https://clips.twitch.tv/{node.get('slug')}",
                        "title": node.get("title", ""),
                        "broadcaster": (node.get("broadcaster") or {}).get("displayName", "Streamer"),
                        "views": int(node.get("viewCount", 0)),
                        "duration": float(node.get("durationSeconds", 0)),
                        "language": (node.get("language") or "en").lower(),
                    })
                if len(clips) >= 25:
                    break
            except Exception as e:
                print(f"[GQL {period}] Error: {e}")

    seen = set()
    filtered = []
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
            "yt-dlp", "--no-playlist",
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
def call_gemini_json(prompt: str) -> Optional[Dict]:
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
            return json.loads(resp.text)
        except Exception as e:
            print(f"[Gemini {model_name}] Error: {e}")
            time.sleep(2)
    return None


def score_clips_batch(clips_with_transcripts: List[Dict]) -> Optional[Dict]:
    """Single batched Gemini call to score all clips, pick the cleanest #1 hook, and write SEO metadata."""
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
2. CRITICAL: The FIRST clip in selected_order MUST have ZERO profanity (profanity_hits == 0) AND be the single most exciting hook, because profanity in the first 30 seconds throttles YouTube impressions.
3. Exclude any clip with heavy profanity (profanity_hits >= 3).
4. Pick between {TARGET_MIN_CLIPS} and {TARGET_MAX_CLIPS} best clips, ordered for maximum retention.
5. For each selected clip, write a short 3-6 word YouTube Chapter title (ASCII only, no emojis).
6. Write yt_title: STRICTLY under 68 characters, ASCII only, NO emojis, high-CTR curiosity title mentioning GTA 6 and the top 1-2 streamers featured.
7. Write thumb_text: STRICTLY 2 to 4 ALL-CAPS words for the thumbnail (e.g. "CHAOS IN VICE CITY" or "HE BROKE GTA 6").
8. Write intro_vo: 1 energetic narrator sentence (12 to 18 words, ~4.5 seconds) hooking viewers into today's best GTA 6 moments.
9. Write yt_description_intro: 2 natural SEO sentences summarizing today's highlights + an engaging question about one of the moments.
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
    async def _run():
        comm = edge_tts.Communicate(text, voice=TTS_VOICE, rate=TTS_RATE)
        await comm.save(str(out_mp3))

    try:
        asyncio.run(_run())
        dur, _ = probe_video(out_mp3)
        return dur
    except Exception as e:
        print(f"[edge-tts] Failed: {e}")
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
    """Normalizes clip to 1080p60/30, adds clean lower-third badge, ducks audio if Clip #1 has intro VO."""
    dur, has_audio = probe_video(raw_mp4)
    badge_file = workdir / f"badge_{out_mp4.stem}.txt"
    badge_text = f"{strip_non_ascii(broadcaster).upper()}  |  {strip_non_ascii(chapter_title)[:42]}"
    badge_file.write_text(badge_text, encoding="utf-8")
    badge_escaped = str(badge_file).replace("\\", "/").replace(":", "\\:")

    inputs = ["-i", str(raw_mp4)]
    vo_idx = None
    logo_idx = None
    next_idx = 1

    if intro_vo_path and intro_vo_dur > 0.2:
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
        "[base]drawbox=x=40:y=965:w=900:h=72:color=0x0B0B0D@0.82:t=fill:enable='lt(t,6)',"
        "drawbox=x=40:y=965:w=8:h=72:color=0x007BFF@1.0:t=fill:enable='lt(t,6)',"
        f"drawtext=fontfile='{FONT_PATH}':textfile='{badge_escaped}':"
        "fontcolor=white:fontsize=30:x=64:y=986:enable='lt(t,6)'[vpre]",
    ]

    if logo_idx is not None:
        v_filters.append(f"[{logo_idx}:v]scale=110:-1[lg]")
        v_filters.append("[vpre][lg]overlay=W-w-36:36[vout]")
    else:
        v_filters.append("[vpre]copy[vout]")

    a_filters = []
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
    else:
        a_filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.2f}[aout]")

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(v_filters + a_filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(out_mp4),
    ]
    run_cmd(cmd)
    actual_dur, _ = probe_video(out_mp4)
    return actual_dur


def build_high_ctr_thumbnail(first_clip_path: Path, thumb_text: str, out_jpg: Path, workdir: Path) -> bool:
    """Picks the sharpest, brightest frame from Clip #1 and builds a bold, phone-readable 1280x720 thumbnail."""
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

    # Pick frame with best combined contrast + colorfulness
    best_frame = frame_files[0]
    best_score = -1.0
    for fpath in frame_files:
        im = Image.open(fpath).convert("RGB")
        stat = ImageStat.Stat(im)
        brightness = sum(stat.mean) / 3.0
        contrast = sum(stat.stddev) / 3.0
        score = contrast * 1.5 - abs(brightness - 135) * 0.4
        if score > best_score:
            best_score = score
            best_frame = fpath

    img = Image.open(best_frame).convert("RGBA")
    img = ImageEnhance.Contrast(img).enhance(1.22)
    img = ImageEnhance.Color(img).enhance(1.25)

    # Bottom gradient scrim for readable text
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(420, 720):
        alpha = int(215 * ((y - 420) / 300.0))
        draw_ov.line([(0, y), (1280, y)], fill=(8, 10, 16, alpha))
    # Subtle electric-blue esports accent frame
    draw_ov.rectangle([(0, 0), (1279, 719)], outline=(0, 123, 255, 255), width=10)
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    text = strip_non_ascii(thumb_text).upper()[:28]
    font_size = 104
    font = ImageFont.truetype(FONT_PATH, font_size)
    while draw.textbbox((0, 0), text, font=font)[2] > 1180 and font_size > 52:
        font_size -= 6
        font = ImageFont.truetype(FONT_PATH, font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    tx = (1280 - tw) // 2
    ty = 555

    # Thick black outline + bright yellow/white fill for instant pop
    draw.text((tx, ty), text, font=font, fill=(255, 215, 0, 255), stroke_width=10, stroke_fill=(0, 0, 0, 255))

    # Top-left "GTA 6" badge
    badge_font = ImageFont.truetype(FONT_PATH, 44)
    draw.rounded_rectangle([(36, 32), (225, 102)], radius=12, fill=(0, 123, 255, 240))
    draw.text((62, 42), "GTA 6", font=badge_font, fill=(255, 255, 255, 255))

    if LOGO_PATH.exists():
        lg = Image.open(LOGO_PATH).convert("RGBA")
        lg.thumbnail((125, 125))
        img.alpha_composite(lg, (1280 - lg.width - 36, 32))

    img.convert("RGB").save(out_jpg, "JPEG", quality=92)
    return out_jpg.exists()


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
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags[:15],
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
            print(f"[YouTube] Thumbnail upload skipped/failed (verify channel phone status): {e}")
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
                    "description": description,
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
        if not plan or len(plan.get("selected_clips", [])) < TARGET_MIN_CLIPS:
            print("[Compilation] Gemini quality gate did not find enough relevant GTA 6 clips.")
            return

        # Build intro VO over Clip #1 (zero static screen delay)
        intro_vo_text = strip_non_ascii(plan.get("intro_vo", ""))
        intro_vo_path = workdir / "intro_vo.mp3"
        intro_vo_dur = generate_edge_tts(intro_vo_text, intro_vo_path) if intro_vo_text else 0.0

        concat_entries = []
        chapters = []
        used_ids = []
        featured_streamers = []
        elapsed = 0.0
        first_std_clip: Optional[Path] = None

        for order_idx, sel in enumerate(plan["selected_clips"]):
            idx = sel.get("index")
            if idx is None or not (0 <= idx < len(inspected)):
                continue
            clip = inspected[idx]
            # Extra safeguard: enforce zero profanity on Clip #1
            if order_idx == 0 and clip["profanity_hits"] > 0:
                continue

            chap_title = strip_non_ascii(sel.get("chapter_title", clip["broadcaster"]))
            std_mp4 = workdir / f"std_{order_idx:02d}.mp4"
            actual_dur = standardize_clip_16x9(
                clip["local_path"],
                std_mp4,
                clip["broadcaster"],
                chap_title,
                workdir,
                intro_vo_path=intro_vo_path if order_idx == 0 else None,
                intro_vo_dur=intro_vo_dur if order_idx == 0 else 0.0,
            )

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
            print("[Compilation] Insufficient valid clips after assembly checks.")
            return

        concat_list = workdir / "concat.txt"
        concat_list.write_text("\n".join(concat_entries), encoding="utf-8")
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
        yt_description = (
            f"{desc_intro}\n\n"
            f"Chapters:\n{chapters_block}\n\n"
            f"Featured Creators: {credits_line}\n"
            f"Subscribe to The Rippers Gaming for daily GTA 6 highlights!\n\n"
            f"#GTA6 #GrandTheftAuto6 #GTAVI #ViceCity #GamingHighlights"
        )
        yt_tags = plan.get("yt_tags", ["GTA 6", "Grand Theft Auto 6", "GTA VI", "GTA 6 Highlights"])

        upload_compilation_youtube(final_mp4, thumb_jpg if thumb_ok else None, yt_title, yt_description, yt_tags)
        upload_compilation_facebook(final_mp4, yt_title, yt_description)

        save_processed_ids(STATE_FILE, used_ids)
        print(f"[Compilation] Done! Published {len(used_ids)} clips ({elapsed:.1f}s total).")


if __name__ == "__main__":
    main()
