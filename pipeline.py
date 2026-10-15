import os
import sys
import time
import json
import subprocess
import argparse
import textwrap
import re
import requests
import yt_dlp
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TRACKER_FILE = "processed_vods.txt"
PLAYLIST_CACHE_FILE = "playlists.json"
WATERMARK_FILE = "logo.png"
COOKIES_FILE = "cookies.txt"

COOKIE_CLIENTS = {'web', 'mweb', 'web_creator', 'web_embedded'}

def setup_cookies_file():
    raw_cookies = os.environ.get("YT_COOKIES", "").strip()
    if not raw_cookies:
        return False

    repaired_lines = ["# Netscape HTTP Cookie File"]
    for line in raw_cookies.splitlines():
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("#HttpOnly_")):
            continue
        parts = s.split("\t") if "\t" in s else s.split()
        if len(parts) >= 7:
            repaired_lines.append("\t".join(parts[:6] + [" ".join(parts[6:])]))
        elif len(parts) == 6:
            repaired_lines.append("\t".join(parts) + "\t")

    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(repaired_lines) + "\n")
    print(f"Loaded and formatted {len(repaired_lines)-1} cookies from YT_COOKIES secret.")
    return True

HAS_COOKIES = setup_cookies_file()

def get_ydl_opts(client_name='web'):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'remote_components': ['ejs:github'],
        'extractor_args': {
            'youtube': {
                'player_client': [client_name],
            },
            'youtubepot-bgutilhttp': {
                'base_url': ['http://127.0.0.1:4416']
            }
        }
    }
    if HAS_COOKIES and os.path.exists(COOKIES_FILE) and client_name in COOKIE_CLIENTS:
        opts['cookiefile'] = COOKIES_FILE
    return opts

def extract_info_resilient(url, extra_opts=None):
    extra_opts = extra_opts or {}
    clients_order = ['web', 'mweb', 'web_creator', 'ios', 'android']
    last_err = None
    for client_name in clients_order:
        opts = {**get_ydl_opts(client_name), **extra_opts}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"Metadata client [{client_name}] fallback ({e})...")
            last_err = e
    raise last_err

# ==========================================================
# 9-GAME OFFICIAL TOURNAMENT CHANNEL DIRECTORY
# ==========================================================
TOURNAMENT_CHANNELS = {
    "valorant": {
        "name": "VALORANT VCT",
        "playlist_title": "VALORANT VCT Tournament Highlights & Clutches",
        "url": "https://www.youtube.com/@ValorantEsports/streams",
        "tags": ["Valorant", "VCT", "ValorantClips", "Esports"],
        "accent": (255, 70, 85)
    },
    "cs2": {
        "name": "CS2 PREMIER",
        "playlist_title": "CS2 Premier Tournament Highlights & Clutches",
        "url": "https://www.youtube.com/@BLASTPremier/streams",
        "tags": ["CS2", "CounterStrike2", "BLASTPremier", "ESLCS"],
        "accent": (255, 165, 0)
    },
    "pubgm": {
        "name": "PUBG MOBILE",
        "playlist_title": "PUBG Mobile Esports Highlights & Clutches",
        "url": "https://www.youtube.com/@PUBGMOBILEEsports/streams",
        "tags": ["PUBGMobile", "PMGC", "PUBGMEsports", "MobileGaming"],
        "accent": (255, 204, 0)
    },
    "pubgpc": {
        "name": "PUBG ESPORTS",
        "playlist_title": "PUBG PC Tournament Highlights & Clutches",
        "url": "https://www.youtube.com/@PUBGEsports/streams",
        "tags": ["PUBG", "PUBGEsports", "PGC", "Battlegrounds"],
        "accent": (242, 169, 0)
    },
    "apex": {
        "name": "APEX ALGS",
        "playlist_title": "Apex Legends ALGS Highlights & Clutches",
        "url": "https://www.youtube.com/@playapexesports/streams",
        "tags": ["ApexLegends", "ALGS", "ApexClips", "Esports"],
        "accent": (218, 41, 42)
    },
    "r6": {
        "name": "RAINBOW SIX",
        "playlist_title": "Rainbow Six Siege Esports Highlights & Clutches",
        "url": "https://www.youtube.com/@RainbowSixEsports/streams",
        "tags": ["RainbowSixSiege", "R6Esports", "SixInvitational", "R6S"],
        "accent": (0, 174, 239)
    },
    "cod": {
        "name": "COD LEAGUE",
        "playlist_title": "Call of Duty League CDL Highlights & Clutches",
        "url": "https://www.youtube.com/@CODLeague/streams",
        "tags": ["CallOfDuty", "CDL", "CODLeague", "Warzone"],
        "accent": (255, 102, 0)
    },
    "tekken": {
        "name": "TEKKEN 8 TWT",
        "playlist_title": "Tekken 8 World Tour Highlights & Combos",
        "url": "https://www.youtube.com/@BNFC_official/videos",
        "tags": ["Tekken8", "TWT", "TekkenWorldTour", "FGC"],
        "accent": (255, 0, 102)
    },
    "fortnite": {
        "name": "FORTNITE FNCS",
        "playlist_title": "Fortnite FNCS Tournament Highlights & Clutches",
        "url": "https://www.youtube.com/@fncompetitive/streams",
        "tags": ["Fortnite", "FNCS", "FortniteCompetitive", "FortniteClips"],
        "accent": (0, 210, 255)
    }
}

WHISPER_MODEL = None

def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        print("Loading Faster-Whisper model (base.en, int8)...")
        WHISPER_MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")
    return WHISPER_MODEL

def load_processed_vods():
    if not os.path.exists(TRACKER_FILE):
        return set()
    with open(TRACKER_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def mark_vod_as_processed(vod_id, game_key, title):
    with open(TRACKER_FILE, "a", encoding="utf-8") as f:
        f.write(f"{vod_id}\n")
    print(f"Saved VOD ID [{vod_id}] ({game_key}: {title}) to {TRACKER_FILE}")

def extract_video_id(vod_url):
    info = extract_info_resilient(vod_url, {'skip_download': True})
    return info.get('id')

def get_unprocessed_vod_from_channel(channel_url, processed_ids):
    info = extract_info_resilient(channel_url, {
        'extract_flat': True,
        'playlistend': 10,
    })
    for entry in info.get('entries', []):
        vid_id = entry.get('id')
        live_status = entry.get('live_status')
        if vid_id and live_status not in ('is_live', 'is_upcoming'):
            if vid_id not in processed_ids:
                return vid_id, f"https://www.youtube.com/watch?v={vid_id}"
            else:
                print(f"Skipping {vid_id} (Already clipped and uploaded).")
    return None, None

# ==========================================================
# ULTRA LOW-TOKEN GEMINI AI SEO & TEAM NAME EXTRACTOR
# ==========================================================
def regex_extract_teams(title):
    patterns = [
        r"([A-Za-z0-9\.\s]{2,25}?)\s+(?:vs\.?|versus|v)\s+([A-Za-z0-9\.\s]{2,25})(?:\s*[\|\-\(\[]|$)",
        r"([\w\s]+)\s+vs\.?\s+([\w\s]+)",
    ]
    for pat in patterns:
        match = re.search(pat, title, re.IGNORECASE)
        if match:
            t1 = match.group(1).strip().split('|')[-1].split('-')[-1].strip()
            t2 = match.group(2).strip().split('|')[0].split('-')[0].strip()
            if 2 <= len(t1) <= 25 and 2 <= len(t2) <= 25:
                return f"{t1} vs {t2}"
    return None

def generate_ai_seo_metadata(game_cfg, raw_title, chapters, target_url):
    teams_fallback = regex_extract_teams(raw_title) or game_cfg["name"]
    seo_hook = f"Watch the highest-intensity pro rounds and clutch plays from {raw_title} ({game_cfg['name']})!"
    extra_tags = []

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = (
                f"Esport:{game_cfg['name']}\nTitle:{raw_title}\n"
                "Return strict JSON: {\"teams\":\"TeamA vs TeamB (or tournament name if Battle Royale)\","
                "\"seo\":\"2-sentence high-CTR YouTube SEO summary with keywords\","
                "\"tags\":[\"tag1\",\"tag2\",\"tag3\"]}"
            )
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"response_mime_type": "application/json", "max_output_tokens": 140}
            )
            data = json.loads(resp.text)
            if data.get("teams"):
                teams_fallback = data["teams"]
            if data.get("seo"):
                seo_hook = data["seo"]
            if isinstance(data.get("tags"), list):
                extra_tags = [str(t).replace("#", "") for t in data["tags"][:5]]
            print(f"Gemini SEO generated! Matchup: {teams_fallback}")
        except Exception as e:
            print(f"Gemini SEO skipped ({e}), using regex fallback.")

    all_tags = list(dict.fromkeys(game_cfg["tags"] + extra_tags + ["Esports", "Gaming", "TournamentHighlights"]))
    hashtags = " ".join(f"#{t}" for t in all_tags[:8])

    recap_title = f"{teams_fallback} - All Best Rounds & Clutches | {game_cfg['name']}"[:100]
    recap_desc = (
        f"🔥 {seo_hook}\n\n"
        f"🎮 Match: {raw_title}\n"
        f"🏆 Esport: {game_cfg['name']}\n\n"
        f"⏱️ Round Highlights & Timestamps:\n{chapters}\n\n"
        f"📺 Official Broadcast Source: {target_url}\n\n"
        f"Subscribe for daily pro esports recaps, top clutches, and tournament highlights!\n\n"
        f"{hashtags}"
    )
    return teams_fallback, recap_title, recap_desc, all_tags

# ==========================================================
# HEATMAP & CLIPPING FUNCTIONS
# ==========================================================
def get_top_rounds(vod_url, num_rounds=8, clip_duration=45):
    try:
        info = extract_info_resilient(vod_url, {'skip_download': True})
        raw_title = info.get('title', 'Tournament Match')
        heatmap = info.get('heatmap')
        duration = info.get('duration') or 1800
    except Exception as e:
        print(f"Full metadata fallback triggered ({e}), using interval sampling...")
        raw_title = "Esports Tournament Highlights"
        heatmap = None
        duration = 1800

    peaks = []
    if heatmap:
        sorted_heat = sorted(heatmap, key=lambda x: x.get('value', 0), reverse=True)
        for pt in sorted_heat:
            center_time = (pt['start_time'] + pt['end_time']) / 2
            if all(abs(center_time - p['center']) > 90 for p in peaks):
                start = max(0, int(center_time - (clip_duration * 0.6)))
                end = int(start + clip_duration)
                peaks.append({
                    'start': start,
                    'end': end,
                    'center': center_time,
                    'score': round(pt.get('value', 0), 2)
                })
            if len(peaks) >= num_rounds:
                break
    else:
        print("No heatmap data yet on this VOD. Sampling high-action mid-game intervals...")
        step = max(120, int(duration) // (num_rounds + 1))
        for i in range(1, num_rounds + 1):
            start = i * step
            peaks.append({
                'start': start,
                'end': start + clip_duration,
                'center': start + (clip_duration / 2),
                'score': round(1.0 - (i * 0.05), 2)
            })

    chrono_rounds = sorted(peaks, key=lambda x: x['start'])
    best_shorts_rounds = sorted(peaks, key=lambda x: x['score'], reverse=True)
    return raw_title, chrono_rounds, best_shorts_rounds

def download_round_clips(vod_url, rounds, output_dir="clips"):
    os.makedirs(output_dir, exist_ok=True)
    clients_to_try = ["web", "mweb", "web_creator", "ios", "android"]

    for idx, r in enumerate(rounds):
        out_path = os.path.join(output_dir, f"round_{idx+1:02d}.mp4")
        section_arg = f"*{r['start']}-{r['end']}"
        print(f"Downloading Round {idx+1}: {r['start']}s to {r['end']}s (Hype Score: {r['score']})")

        downloaded = False
        for client in clients_to_try:
            cmd = [
                "yt-dlp",
                "--remote-components", "ejs:github",
                "--extractor-args", f"youtube:player_client={client};youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
                "--format", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "--download-sections", section_arg,
                "--force-keyframes-at-cuts",
                "--merge-output-format", "mp4",
                "-o", out_path,
            ]
            if HAS_COOKIES and os.path.exists(COOKIES_FILE) and client in COOKIE_CLIENTS:
                cmd.extend(["--cookies", COOKIES_FILE])
            cmd.append(vod_url)

            res = subprocess.run(cmd)
            if res.returncode == 0 and os.path.exists(out_path):
                downloaded = True
                break
            print(f"Download client [{client}] failed, retrying with next client...")

        if not downloaded:
            raise RuntimeError(f"All player clients failed for {vod_url}.")
        r['file_path'] = out_path

def build_16_9_recap(rounds, output_file="match_recap_16_9.mp4"):
    concat_list = "concat_list.txt"
    chapters = []
    current_time = 0
    with open(concat_list, "w") as f:
        for idx, r in enumerate(rounds):
            f.write(f"file '{os.path.abspath(r['file_path'])}'\n")
            mins, secs = divmod(current_time, 60)
            chapters.append(f"{mins:02d}:{secs:02d} Round Highlight {idx+1}")
            current_time += (r['end'] - r['start'])

    temp_merged = "temp_merged.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", temp_merged
    ], check=True)

    if os.path.exists(WATERMARK_FILE):
        subprocess.run([
            "ffmpeg", "-y", "-i", temp_merged, "-i", WATERMARK_FILE,
            "-filter_complex", "[1:v]scale=140:-1[logo];[0:v][logo]overlay=W-w-35:35",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "copy", output_file
        ], check=True)
    else:
        os.rename(temp_merged, output_file)

    return output_file, "\n".join(chapters)

def load_bold_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

def generate_short_subtitles(input_clip, srt_path="short_subs.srt"):
    model = get_whisper_model()
    segments, _ = model.transcribe(input_clip, word_timestamps=True, vad_filter=True)

    words_buffer = []
    srt_entries = []
    idx = 1

    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            cleaned = w.word.strip().upper()
            if cleaned:
                words_buffer.append((cleaned, w.start, w.end))
            if len(words_buffer) >= 4:
                start_t = format_srt_time(words_buffer[0][1])
                end_t = format_srt_time(words_buffer[-1][2])
                text_line = " ".join(item[0] for item in words_buffer)
                srt_entries.append(f"{idx}\n{start_t} --> {end_t}\n{text_line}\n")
                idx += 1
                words_buffer = []

    if words_buffer:
        start_t = format_srt_time(words_buffer[0][1])
        end_t = format_srt_time(words_buffer[-1][2])
        text_line = " ".join(item[0] for item in words_buffer)
        srt_entries.append(f"{idx}\n{start_t} --> {end_t}\n{text_line}\n")

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_entries))
    return srt_path if srt_entries else None

def create_short_top_banner(game_cfg, match_title, round_rank, banner_path="short_banner.png"):
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    accent = game_cfg.get("accent", (255, 70, 85))

    draw.rounded_rectangle([44, 130, 1036, 515], radius=28, fill=(10, 12, 18, 225), outline=accent + (255,), width=6)

    font_badge = load_bold_font(38)
    font_main = load_bold_font(54)
    font_match = load_bold_font(36)

    badge_text = f"{game_cfg['name']}  •  TOP CLUTCH #{round_rank}"
    bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    bw = (bbox[2] - bbox[0]) + 60
    bx = (1080 - bw) // 2
    draw.rounded_rectangle([bx, 162, bx + bw, 232], radius=16, fill=accent + (255,))
    draw.text((bx + 30, 174), badge_text, font=font_badge, fill=(255, 255, 255, 255))

    hook_text = "MOST REPLAYED ROUND"
    h_bbox = draw.textbbox((0, 0), hook_text, font=font_main)
    hx = (1080 - (h_bbox[2] - h_bbox[0])) // 2
    draw.text((hx, 265), hook_text, font=font_main, fill=(255, 230, 0, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255))

    wrapped_lines = textwrap.wrap(match_title.upper(), width=34)[:2]
    y_text = 355
    for line in wrapped_lines:
        l_bbox = draw.textbbox((0, 0), line, font=font_match)
        lx = (1080 - (l_bbox[2] - l_bbox[0])) // 2
        draw.text((lx, y_text), line, font=font_match, fill=(235, 235, 235, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
        y_text += 52

    img.save(banner_path, "PNG")
    return banner_path

def build_9_16_short(input_clip, output_short, game_cfg, match_title, round_rank=1):
    banner_png = create_short_top_banner(game_cfg, match_title, round_rank, f"banner_{round_rank}.png")
    srt_file = generate_short_subtitles(input_clip, f"subs_{round_rank}.srt")

    sub_filter = ""
    if srt_file and os.path.exists(srt_file):
        style = (
            "Fontname=Liberation Sans,Fontsize=18,PrimaryColour=&H0000EAFF,"
            "OutlineColour=&H00000000,BackColour=&H80000000,Bold=1,BorderStyle=1,"
            "Outline=3,Shadow=1,Alignment=2,MarginV=75"
        )
        sub_filter = f",subtitles={srt_file}:force_style='{style}'"

    if os.path.exists(WATERMARK_FILE):
        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:5,eq=brightness=-0.18[bg];"
            "[0:v]scale=1080:-1:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2+60[base];"
            "[base][1:v]overlay=0:0[with_banner];"
            "[2:v]scale=120:-1[logo];"
            f"[with_banner][logo]overlay=W-w-50:550{sub_filter}[outv]"
        )
        cmd_inputs = ["ffmpeg", "-y", "-i", input_clip, "-i", banner_png, "-i", WATERMARK_FILE]
    else:
        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:5,eq=brightness=-0.18[bg];"
            "[0:v]scale=1080:-1:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2+60[base];"
            f"[base][1:v]overlay=0:0{sub_filter}[outv]"
        )
        cmd_inputs = ["ffmpeg", "-y", "-i", input_clip, "-i", banner_png]

    subprocess.run(cmd_inputs + [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-t", "58",
        output_short
    ], check=True)
    return output_short

def generate_custom_thumbnail(best_clip_path, game_cfg, teams_title, match_title, output_thumb="thumbnail.jpg"):
    raw_frame = "raw_frame.jpg"
    subprocess.run([
        "ffmpeg", "-y", "-ss", "22", "-i", best_clip_path,
        "-frames:v", "1", "-q:v", "2", raw_frame
    ], check=True)

    img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Contrast(img).enhance(1.18)

    if os.path.exists(WATERMARK_FILE):
        logo_thumb = Image.open(WATERMARK_FILE).convert("RGBA")
        logo_thumb.thumbnail((160, 160))
        img.paste(logo_thumb, (1280 - logo_thumb.width - 40, 40), logo_thumb)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(290, 720):
        alpha = int(240 * ((y - 290) / 430) ** 1.25)
        draw_ov.line([(0, y), (1280, y)], fill=(8, 10, 16, alpha))

    accent = game_cfg.get("accent", (255, 70, 85))
    draw_ov.rectangle([0, 0, 1279, 719], outline=accent + (255,), width=12)

    game_label = game_cfg["name"].upper()
    font_badge = load_bold_font(42)
    font_hero = load_bold_font(94)
    font_sub = load_bold_font(44)

    badge_bbox = draw_ov.textbbox((0, 0), game_label, font=font_badge)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 56
    draw_ov.rounded_rectangle([48, 42, 48 + badge_w, 118], radius=14, fill=accent + (245,))
    draw_ov.text((76, 54), game_label, font=font_badge, fill=(255, 255, 255, 255))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    hero_line = textwrap.shorten(teams_title.upper(), width=22, placeholder="...")
    draw.text(
        (52, 465), hero_line,
        font=font_hero, fill=(255, 230, 0, 255),
        stroke_width=8, stroke_fill=(0, 0, 0, 255)
    )

    clean_sub = textwrap.shorten(match_title.upper(), width=44, placeholder="...")
    draw.text(
        (56, 595), clean_sub,
        font=font_sub, fill=(255, 255, 255, 255),
        stroke_width=4, stroke_fill=(0, 0, 0, 255)
    )

    img.convert("RGB").save(output_thumb, "JPEG", quality=93)
    return output_thumb

# ==========================================================
# YOUTUBE UPLOAD & AUTO-PLAYLIST SORTING
# ==========================================================
def get_youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube"
        ]
    )
    return build("youtube", "v3", credentials=creds)

def get_or_create_game_playlist(youtube, game_key, game_cfg):
    """Finds or creates the dedicated playlist for this game and caches its ID."""
    cache = {}
    if os.path.exists(PLAYLIST_CACHE_FILE):
        try:
            with open(PLAYLIST_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    if game_key in cache and cache[game_key]:
        return cache[game_key]

    target_title = game_cfg.get("playlist_title", f"{game_cfg['name']} Highlights")
    try:
        # Check existing playlists on the channel first
        req = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
        res = req.execute()
        for item in res.get("items", []):
            if item["snippet"]["title"].strip().lower() == target_title.lower():
                pl_id = item["id"]
                cache[game_key] = pl_id
                with open(PLAYLIST_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                return pl_id

        # Not found -> Create new public playlist for this game
        create_res = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": target_title,
                    "description": f"Official tournament highlights, full match recaps, and pro clutches for {game_cfg['name']}."
                },
                "status": {"privacyStatus": "public"}
            }
        ).execute()
        pl_id = create_res["id"]
        cache[game_key] = pl_id
        with open(PLAYLIST_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        print(f"Created new YouTube Playlist [{target_title}] -> ID: {pl_id}")
        return pl_id
    except Exception as e:
        print(f"Playlist lookup/creation skipped (Verify YT_REFRESH_TOKEN has 'youtube' scope): {e}")
        return None

def add_video_to_playlist(youtube, video_id, playlist_id):
    if not playlist_id or not video_id:
        return
    try:
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        ).execute()
        print(f"Sorted Video [{video_id}] into Playlist [{playlist_id}]")
    except Exception as e:
        print(f"Could not add video {video_id} to playlist: {e}")

def upload_to_youtube(youtube, file_path, title, description, tags, thumbnail_path=None, playlist_id=None):
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "20"
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    }
    media = MediaFileUpload(file_path, chunksize=1024*1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response['id']
    print(f"Uploaded YouTube Video ID: {video_id}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            ).execute()
            print(f"Attached custom thumbnail to Video ID: {video_id}")
        except Exception as e:
            print(f"Note: Custom thumbnail upload skipped: {e}")

    if playlist_id:
        add_video_to_playlist(youtube, video_id, playlist_id)

    return video_id

# ==========================================================
# MULTI-PLATFORM DISTRIBUTION (FACEBOOK, INSTAGRAM, TIKTOK)
# ==========================================================
def share_youtube_link_on_facebook(yt_video_id, message):
    fb_page_id = os.environ.get("FB_PAGE_ID", "").strip()
    fb_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not fb_page_id or not fb_token:
        print("FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN not set; skipping Facebook link post.")
        return

    yt_link = f"https://www.youtube.com/watch?v={yt_video_id}"
    url = f"https://graph.facebook.com/v21.0/{fb_page_id}/feed"
    resp = requests.post(url, data={
        "message": f"{message}\n\n📺 Watch Full HD Match Recap: {yt_link}",
        "link": yt_link,
        "access_token": fb_token
    }, timeout=30)
    print(f"Facebook Link Post Response ({resp.status_code}): {resp.text}")

def upload_short_to_facebook_reels(video_path, caption):
    fb_page_id = os.environ.get("FB_PAGE_ID", "").strip()
    fb_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not fb_page_id or not fb_token:
        return

    try:
        init_url = f"https://graph.facebook.com/v21.0/{fb_page_id}/video_reels"
        init_res = requests.post(init_url, data={
            "upload_phase": "start",
            "access_token": fb_token
        }, timeout=30).json()
        video_id = init_res.get("video_id")
        upload_url = init_res.get("upload_url")
        if not video_id or not upload_url:
            print(f"FB Reels init failed: {init_res}")
            return

        file_size = os.path.getsize(video_path)
        with open(video_path, "rb") as f:
            headers = {
                "Authorization": f"OAuth {fb_token}",
                "offset": "0",
                "file_size": str(file_size)
            }
            requests.post(upload_url, headers=headers, data=f, timeout=180)

        fin_res = requests.post(init_url, data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": fb_token
        }, timeout=30).json()
        print(f"Facebook Reel Published: {fin_res}")
    except Exception as e:
        print(f"Facebook Reel upload error: {e}")

def upload_temp_public_url(video_path):
    with open(video_path, "rb") as f:
        resp = requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": f},
            timeout=120
        )
    if resp.status_code == 200 and resp.text.startswith("http"):
        return resp.text.strip()
    return None

def upload_short_to_instagram_reels(video_path, caption):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    fb_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not ig_user_id or not fb_token:
        return

    try:
        public_video_url = upload_temp_public_url(video_path)
        if not public_video_url:
            print("Could not stage temporary URL for Instagram Reels.")
            return

        create_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
        container = requests.post(create_url, data={
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "access_token": fb_token
        }, timeout=30).json()

        creation_id = container.get("id")
        if not creation_id:
            print(f"IG Reel container error: {container}")
            return

        for _ in range(12):
            time.sleep(10)
            status_res = requests.get(
                f"https://graph.facebook.com/v21.0/{creation_id}",
                params={"fields": "status_code", "access_token": fb_token},
                timeout=20
            ).json()
            if status_res.get("status_code") == "FINISHED":
                break

        pub_res = requests.post(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": fb_token},
            timeout=30
        ).json()
        print(f"Instagram Reel Published: {pub_res}")
    except Exception as e:
        print(f"Instagram Reel upload error: {e}")

def upload_short_to_tiktok(video_path, title):
    tt_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
    if not tt_token:
        return

    try:
        file_size = os.path.getsize(video_path)
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        headers = {
            "Authorization": f"Bearer {tt_token}",
            "Content-Type": "application/json; charset=UTF-8"
        }
        payload = {
            "post_info": {
                "title": title[:150],
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1
            }
        }
        init_res = requests.post(init_url, headers=headers, json=payload, timeout=30).json()
        upload_url = init_res.get("data", {}).get("upload_url")
        if not upload_url:
            print(f"TikTok init response: {init_res}")
            return

        with open(video_path, "rb") as f:
            put_headers = {
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                "Content-Type": "video/mp4"
            }
            put_res = requests.put(upload_url, headers=put_headers, data=f, timeout=180)
            print(f"TikTok Upload Status: {put_res.status_code}")
    except Exception as e:
        print(f"TikTok upload error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="auto", choices=["auto"] + list(TOURNAMENT_CHANNELS.keys()))
    parser.add_argument("--url", default="", help="Optional custom VOD URL")
    parser.add_argument("--shorts-count", type=int, default=2)
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args()

    processed_ids = load_processed_vods()
    target_id, target_url, selected_game = None, None, None

    if args.url.strip():
        target_url = args.url.strip()
        target_id = extract_video_id(target_url)
        selected_game = "valorant" if args.game == "auto" else args.game
        if target_id in processed_ids:
            print(f"VOD {target_id} is already in {TRACKER_FILE}! Exiting.")
            sys.exit(0)
    elif args.game == "auto":
        for g_key, g_cfg in TOURNAMENT_CHANNELS.items():
            print(f"Checking {g_cfg['name']} for new unclipped VODs...")
            vid_id, vod_url = get_unprocessed_vod_from_channel(g_cfg["url"], processed_ids)
            if vid_id:
                target_id, target_url, selected_game = vid_id, vod_url, g_key
                break
    else:
        selected_game = args.game
        target_id, target_url = get_unprocessed_vod_from_channel(
            TOURNAMENT_CHANNELS[selected_game]["url"], processed_ids
        )

    if not target_url:
        print("All recent tournament VODs have already been clipped and uploaded!")
        sys.exit(0)

    game_cfg = TOURNAMENT_CHANNELS[selected_game]
    print(f"New Match Found! Processing [{game_cfg['name']}] VOD: {target_url}")

    raw_title, chrono_rounds, best_rounds = get_top_rounds(target_url, num_rounds=8, clip_duration=45)
    download_round_clips(target_url, chrono_rounds)
    recap_file, chapters = build_16_9_recap(chrono_rounds)

    teams_title, recap_title, recap_desc, seo_tags = generate_ai_seo_metadata(
        game_cfg, raw_title, chapters, target_url
    )

    thumb_file = generate_custom_thumbnail(best_rounds[0]['file_path'], game_cfg, teams_title, raw_title)

    short_files = []
    for i in range(min(args.shorts_count, len(best_rounds))):
        out_short = f"short_{i+1}.mp4"
        build_9_16_short(
            best_rounds[i]['file_path'],
            out_short,
            game_cfg=game_cfg,
            match_title=f"{teams_title} - {raw_title}",
            round_rank=i+1
        )
        short_files.append((out_short, best_rounds[i]['score']))

    if args.upload:
        yt = get_youtube_client()

        # Find or auto-create the dedicated playlist for this game
        playlist_id = get_or_create_game_playlist(yt, selected_game, game_cfg)

        # 1. Upload 16:9 Full Match Recap + Custom Thumbnail + Sort into Game Playlist
        recap_vid_id = upload_to_youtube(
            yt, recap_file,
            recap_title,
            recap_desc, seo_tags,
            thumbnail_path=thumb_file,
            playlist_id=playlist_id
        )

        # 2. Share the 16:9 YouTube Video as a Link Post on Facebook Page
        share_youtube_link_on_facebook(
            recap_vid_id,
            f"🔥 {recap_title}\n\nWatch all the best pro clutches and highlights from {teams_title}!"
        )

        # 3. Upload 9:16 Shorts to YouTube (sorted into Game Playlist), IG Reels, FB Reels & TikTok
        for idx, (s_file, score) in enumerate(short_files):
            s_title = f"{teams_title} Insane Tournament Clutch #{idx+1} 🔥 {game_cfg['name']} #Shorts"
            s_caption = (
                f"{teams_title} Insane Clutch #{idx+1} 🔥 ({game_cfg['name']})\n"
                f"Match: {raw_title}\n"
                f"#Shorts #Reels #Esports #{' #'.join(game_cfg['tags'])}"
            )
            upload_to_youtube(
                yt, s_file, s_title, s_caption,
                seo_tags + ["Shorts"],
                playlist_id=playlist_id
            )
            upload_short_to_facebook_reels(s_file, s_caption)
            upload_short_to_instagram_reels(s_file, s_caption)
            upload_short_to_tiktok(s_file, s_caption)

    mark_vod_as_processed(target_id, selected_game, raw_title)
