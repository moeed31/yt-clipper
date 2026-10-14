import os
import sys
import subprocess
import argparse
import textwrap
import yt_dlp
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TRACKER_FILE = "processed_vods.txt"
WATERMARK_FILE = "logo.png"

# ==========================================================
# 9-GAME OFFICIAL TOURNAMENT CHANNEL DIRECTORY
# ==========================================================
TOURNAMENT_CHANNELS = {
    "valorant": {
        "name": "VALORANT VCT",
        "url": "https://www.youtube.com/@ValorantEsports/streams",
        "tags": ["Valorant", "VCT", "ValorantClips", "Esports"],
        "accent": (255, 70, 85)
    },
    "cs2": {
        "name": "CS2 PREMIER",
        "url": "https://www.youtube.com/@BLASTPremier/streams",
        "tags": ["CS2", "CounterStrike2", "BLASTPremier", "ESLCS"],
        "accent": (255, 165, 0)
    },
    "pubgm": {
        "name": "PUBG MOBILE",
        "url": "https://www.youtube.com/@PUBGMOBILEEsports/streams",
        "tags": ["PUBGMobile", "PMGC", "PUBGMEsports", "MobileGaming"],
        "accent": (255, 204, 0)
    },
    "pubgpc": {
        "name": "PUBG ESPORTS",
        "url": "https://www.youtube.com/@PUBGEsports/streams",
        "tags": ["PUBG", "PUBGEsports", "PGC", "Battlegrounds"],
        "accent": (242, 169, 0)
    },
    "apex": {
        "name": "APEX ALGS",
        "url": "https://www.youtube.com/@playapexesports/streams",
        "tags": ["ApexLegends", "ALGS", "ApexClips", "Esports"],
        "accent": (218, 41, 42)
    },
    "r6": {
        "name": "RAINBOW SIX",
        "url": "https://www.youtube.com/@RainbowSixEsports/streams",
        "tags": ["RainbowSixSiege", "R6Esports", "SixInvitational", "R6S"],
        "accent": (0, 174, 239)
    },
    "cod": {
        "name": "COD LEAGUE",
        "url": "https://www.youtube.com/@CODLeague/streams",
        "tags": ["CallOfDuty", "CDL", "CODLeague", "Warzone"],
        "accent": (255, 102, 0)
    },
    "tekken": {
        "name": "TEKKEN 8 TWT",
        "url": "https://www.youtube.com/@BNFC_official/videos",
        "tags": ["Tekken8", "TWT", "TekkenWorldTour", "FGC"],
        "accent": (255, 0, 102)
    },
    "fortnite": {
        "name": "FORTNITE FNCS",
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
    ydl_opts = {'skip_download': True, 'quiet': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(vod_url, download=False)
        return info.get('id')

def get_unprocessed_vod_from_channel(channel_url, processed_ids):
    ydl_opts = {
        'extract_flat': True,
        'playlistend': 8,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        for entry in info.get('entries', []):
            vid_id = entry.get('id')
            live_status = entry.get('live_status')
            if vid_id and live_status not in ('is_live', 'is_upcoming'):
                if vid_id not in processed_ids:
                    return vid_id, f"https://www.youtube.com/watch?v={vid_id}"
                else:
                    print(f"Skipping {vid_id} (Already clipped and uploaded).")
    return None, None

def get_top_rounds(vod_url, num_rounds=8, clip_duration=45):
    ydl_opts = {'skip_download': True, 'quiet': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(vod_url, download=False)

    title = info.get('title', 'Tournament Match')
    heatmap = info.get('heatmap')
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
        duration = info.get('duration', 1800)
        step = max(120, duration // (num_rounds + 1))
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
    return title, chrono_rounds, best_shorts_rounds

def download_round_clips(vod_url, rounds, output_dir="clips"):
    os.makedirs(output_dir, exist_ok=True)
    for idx, r in enumerate(rounds):
        out_path = os.path.join(output_dir, f"round_{idx+1:02d}.mp4")
        section_arg = f"*{r['start']}-{r['end']}"
        cmd = [
            "yt-dlp",
            "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
            "--download-sections", section_arg,
            "--force-keyframes-at-cuts",
            "--merge-output-format", "mp4",
            "-o", out_path,
            vod_url
        ]
        subprocess.run(cmd, check=True)
        r['file_path'] = out_path

def build_16_9_recap(rounds, output_file="match_recap_16_9.mp4"):
    """
    Stitches rounds together and overlays the logo watermark in the top-right corner.
    """
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

    # If logo.png exists, scale it to width 140px and overlay at top-right (W-w-35:35)
    if os.path.exists(WATERMARK_FILE):
        filter_complex = "[0:v][1:v]overlay=W-w-35:35[outv]"
        subprocess.run([
            "ffmpeg", "-y", "-i", temp_merged, "-i", WATERMARK_FILE,
            "-filter_complex", f"[1:v]scale=140:-1[logo];[0:v][logo]overlay=W-w-35:35",
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

    # Inputs: [0:v] video, [1:v] banner.png, [2:v] logo.png (optional)
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

def generate_custom_thumbnail(best_clip_path, game_cfg, match_title, output_thumb="thumbnail.jpg"):
    raw_frame = "raw_frame.jpg"
    subprocess.run([
        "ffmpeg", "-y", "-ss", "27", "-i", best_clip_path,
        "-frames:v", "1", "-q:v", "2", raw_frame
    ], check=True)

    img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.30)
    img = ImageEnhance.Contrast(img).enhance(1.15)

    # Paste logo watermark onto top-right of 16:9 thumbnail if present
    if os.path.exists(WATERMARK_FILE):
        logo_thumb = Image.open(WATERMARK_FILE).convert("RGBA")
        logo_thumb.thumbnail((160, 160))
        img.paste(logo_thumb, (1280 - logo_thumb.width - 40, 40), logo_thumb)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(320, 720):
        alpha = int(235 * ((y - 320) / 400) ** 1.3)
        draw_ov.line([(0, y), (1280, y)], fill=(8, 10, 16, alpha))

    accent = game_cfg.get("accent", (255, 70, 85))
    draw_ov.rectangle([0, 0, 1279, 719], outline=accent + (255,), width=10)

    game_label = game_cfg["name"].upper()
    font_badge = load_bold_font(42)
    font_hero = load_bold_font(102)
    font_sub = load_bold_font(44)

    badge_bbox = draw_ov.textbbox((0, 0), game_label, font=font_badge)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 56
    draw_ov.rounded_rectangle([48, 42, 48 + badge_w, 118], radius=14, fill=accent + (245,))
    draw_ov.text((76, 54), game_label, font=font_badge, fill=(255, 255, 255, 255))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    draw.text(
        (52, 465), "TOP ROUNDS",
        font=font_hero, fill=(255, 230, 0, 255),
        stroke_width=8, stroke_fill=(0, 0, 0, 255)
    )

    clean_sub = textwrap.shorten(match_title.upper(), width=44, placeholder="...")
    draw.text(
        (56, 595), clean_sub,
        font=font_sub, fill=(255, 255, 255, 255),
        stroke_width=4, stroke_fill=(0, 0, 0, 255)
    )

    img.convert("RGB").save(output_thumb, "JPEG", quality=92)
    return output_thumb

def get_youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(youtube, file_path, title, description, tags, thumbnail_path=None):
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
    print(f"Uploaded Video ID: {video_id}")

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            ).execute()
            print(f"Attached custom thumbnail to Video ID: {video_id}")
        except Exception as e:
            print(f"Note: Custom thumbnail upload skipped: {e}")

    return video_id

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

    title, chrono_rounds, best_rounds = get_top_rounds(target_url, num_rounds=8, clip_duration=45)
    download_round_clips(target_url, chrono_rounds)
    recap_file, chapters = build_16_9_recap(chrono_rounds)
    thumb_file = generate_custom_thumbnail(best_rounds[0]['file_path'], game_cfg, title)

    short_files = []
    for i in range(min(args.shorts_count, len(best_rounds))):
        out_short = f"short_{i+1}.mp4"
        build_9_16_short(
            best_rounds[i]['file_path'],
            out_short,
            game_cfg=game_cfg,
            match_title=title,
            round_rank=i+1
        )
        short_files.append((out_short, best_rounds[i]['score']))

    if args.upload:
        yt = get_youtube_client()
        recap_desc = f"{game_cfg['name']} Top Rounds & Highlights from {title}\n\nChapters:\n{chapters}\n\nSource: {target_url}"
        upload_to_youtube(
            yt, recap_file,
            f"{title} - All Best Rounds ({game_cfg['name']})",
            recap_desc, game_cfg["tags"],
            thumbnail_path=thumb_file
        )

        for idx, (s_file, score) in enumerate(short_files):
            s_title = f"{game_cfg['name']} Insane Tournament Clutch #{idx+1} 🔥 #Shorts"
            s_desc = f"Most replayed tournament moment from {title} (Hype Score: {score}) #Shorts #{' #'.join(game_cfg['tags'])}"
            upload_to_youtube(yt, s_file, s_title, s_desc, game_cfg["tags"] + ["Shorts"])

    mark_vod_as_processed(target_id, selected_game, title)
