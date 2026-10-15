import os
import re
import sys
import json
import time
import shutil
import random
import subprocess
import requests
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from faster_whisper import WhisperModel

# ==============================================================================
# 1. 9-GAME CONFIGURATION (TACTICAL ROUND GAMES vs. BATTLE ROYALE GAMES)
# ==============================================================================
GAME_CONFIG = {
    "valorant": {
        "name": "VALORANT",
        "type": "tactical",
        "accent_color": (255, 70, 85),
        "queries": [
            "Valorant Champions Tour VCT full match map VOD",
            "VCT Americas EMEA Pacific Grand Final full match"
        ],
        "playlist_title": "VALORANT Tournament Highlights & Full Recaps",
        "hashtags": "#Valorant #VCT #ValorantClips #ValorantEsports #Gaming"
    },
    "apex": {
        "name": "Apex Legends",
        "type": "battle_royale",
        "accent_color": (220, 38, 38),
        "queries": [
            "ALGS Apex Legends Global Series Finals full match VOD",
            "ALGS Pro League Match Point Finals full game"
        ],
        "playlist_title": "Apex Legends ALGS Final Circles & Best Fights",
        "hashtags": "#ApexLegends #ALGS #ApexClips #BattleRoyale #Esports"
    },
    "cs2": {
        "name": "Counter-Strike 2",
        "type": "tactical",
        "accent_color": (245, 158, 11),
        "queries": [
            "CS2 ESL Pro League BLAST Premier full match map VOD",
            "CS2 Major Playoffs full match highlight VOD"
        ],
        "playlist_title": "CS2 Pro Match Recaps & Highlights",
        "hashtags": "#CS2 #CounterStrike2 #CS2Clips #ESL #Esports"
    },
    "pubg": {
        "name": "PUBG Battlegrounds",
        "type": "battle_royale",
        "accent_color": (234, 179, 8),
        "queries": [
            "PUBG Global Series PGS PGC Grand Finals full match VOD",
            "PUBG Esports Tournament Grand Final Match VOD"
        ],
        "playlist_title": "PUBG Tournament Final Circles & Recaps",
        "hashtags": "#PUBG #PUBGEsports #BattleRoyale #PUBGClips #Gaming"
    },
    "fortnite": {
        "name": "Fortnite",
        "type": "battle_royale",
        "accent_color": (56, 189, 248),
        "queries": [
            "FNCS Fortnite Championship Series Grand Finals full game VOD",
            "Fortnite FNCS Major Finals full match"
        ],
        "playlist_title": "Fortnite FNCS Endgames & Best Fights",
        "hashtags": "#Fortnite #FNCS #FortniteClips #FortniteCompetitive #Gaming"
    },
    "r6": {
        "name": "Rainbow Six Siege",
        "type": "tactical",
        "accent_color": (59, 130, 246),
        "queries": [
            "Rainbow Six Siege Six Invitational Pro League full match VOD",
            "R6 Esports Major Playoffs full map VOD"
        ],
        "playlist_title": "Rainbow Six Siege Pro Recaps",
        "hashtags": "#R6Siege #RainbowSixSiege #R6Esports #FPS #Gaming"
    },
    "cod": {
        "name": "Call of Duty",
        "type": "tactical",
        "accent_color": (34, 197, 94),
        "queries": [
            "Call of Duty League CDL Major Finals full match VOD",
            "CDL Championship Weekend full series match"
        ],
        "playlist_title": "Call of Duty League Match Recaps",
        "hashtags": "#CallOfDuty #CDL #CODLeague #CODClips #Esports"
    },
    "tekken": {
        "name": "Tekken 8",
        "type": "tactical",
        "accent_color": (168, 85, 247),
        "queries": [
            "Tekken 8 World Tour TWT Top 8 Grand Finals full set",
            "EVO Tekken 8 Finals full match set"
        ],
        "playlist_title": "Tekken 8 Tournament Sets & Highlights",
        "hashtags": "#Tekken8 #TWT #FightingGames #FGC #Esports"
    },
    "lol": {
        "name": "League of Legends",
        "type": "tactical",
        "accent_color": (14, 165, 233),
        "queries": [
            "LCK LEC Worlds League of Legends full game match VOD",
            "LoL Esports MSI Worlds Playoffs full match"
        ],
        "playlist_title": "League of Legends Pro Match Recaps",
        "hashtags": "#LeagueOfLegends #LoLEsports #LCK #Worlds #Gaming"
    }
}

PROCESSED_FILE = Path("processed_vods.txt")
PLAYLIST_CACHE_FILE = Path("playlists.json")
WORK_DIR = Path("work_dir").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 2. STATE TRACKING & GAME SELECTION
# ==============================================================================
def load_processed_vods() -> set:
    if not PROCESSED_FILE.exists():
        PROCESSED_FILE.write_text("", encoding="utf-8")
        return set()
    return set(line.strip() for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines() if line.strip())


def mark_vod_processed(vod_id: str):
    processed = load_processed_vods()
    if vod_id not in processed:
        with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{vod_id}\n")


def select_target_game() -> str:
    target = os.environ.get("TARGET_GAME", "auto").strip().lower()
    if target in GAME_CONFIG:
        return target
    processed_count = len(load_processed_vods())
    keys = list(GAME_CONFIG.keys())
    selected = keys[processed_count % len(keys)]
    print(f"[*] Auto-selected game in rotation: {GAME_CONFIG[selected]['name']} ({selected})")
    return selected


# ==============================================================================
# 3. YOUTUBE VOD DISCOVERY & ROBUST DOWNLOADER
# ==============================================================================
def get_ytdlp_base_args() -> list[str]:
    args = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    ]
    cookie_file = Path("cookies.txt").resolve()
    if cookie_file.exists() and cookie_file.stat().st_size > 40:
        args.extend(["--cookies", str(cookie_file)])
    return args


def find_and_download_vod(game_key: str) -> tuple[Path, dict]:
    custom_url = os.environ.get("CUSTOM_VOD_URL", "").strip()
    processed_ids = load_processed_vods()
    game_info = GAME_CONFIG[game_key]
    output_template = str((WORK_DIR / "source_vod.%(ext)s").resolve())

    candidates = []
    if custom_url:
        candidates.append(custom_url)
    else:
        query = random.choice(game_info["queries"])
        print(f"[*] Searching YouTube for {game_info['name']} tournament VODs: '{query}'")
        search_cmd = get_ytdlp_base_args() + [
            "--dump-json",
            "--flat-playlist",
            f"ytsearch20:{query}"
        ]
        res = subprocess.run(search_cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            try:
                data = json.loads(line)
                vid_id = data.get("id")
                duration = data.get("duration") or 0
                if vid_id and vid_id not in processed_ids and 1080 <= duration <= 5700:
                    candidates.append(f"https://www.youtube.com/watch?v={vid_id}")
            except Exception:
                continue

    if not candidates:
        raise RuntimeError(f"No suitable unprocessed VODs found for {game_info['name']}.")

    for url in candidates[:4]:
        print(f"[*] Attempting download: {url}")
        dl_cmd = get_ytdlp_base_args() + [
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--write-info-json",
            "-o", output_template,
            url
        ]
        proc = subprocess.run(dl_cmd)
        mp4_path = (WORK_DIR / "source_vod.mp4").resolve()
        info_path = (WORK_DIR / "source_vod.info.json").resolve()
        if proc.returncode == 0 and mp4_path.exists():
            info_data = {}
            if info_path.exists():
                info_data = json.loads(info_path.read_text(encoding="utf-8", errors="ignore"))
            return mp4_path, info_data

    raise RuntimeError("All VOD download attempts failed.")


# ==============================================================================
# 4. SMART HIGHLIGHT ENGINE (BATTLE ROYALE FINAL CIRCLE vs. EXTENDED TACTICAL)
# ==============================================================================
def get_smart_highlight_segments(video_path: Path, game_key: str) -> tuple[list[tuple[float, float]], list[float]]:
    """
    Returns:
      - segments: Chronological list of (start, end) tuples for the 16:9 Long-Form Recap.
      - top_peaks: Top hype timestamps (in seconds) for 9:16 Vertical Shorts/Reels/TikToks.
    """
    game_info = GAME_CONFIG[game_key]
    game_type = game_info["type"]

    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path.resolve())
    ]
    total_duration = float(subprocess.check_output(probe_cmd).decode().strip())

    # Extract 1-second RMS audio energy profile using ffmpeg at 8000Hz mono
    pcm_cmd = [
        "ffmpeg", "-i", str(video_path.resolve()), "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "-"
    ]
    proc = subprocess.Popen(pcm_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_audio, _ = proc.communicate()
    audio_samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32)

    samples_per_sec = 8000
    num_seconds = len(audio_samples) // samples_per_sec
    if num_seconds < 240:
        return [(0.0, total_duration)], [total_duration / 2]

    trimmed = audio_samples[:num_seconds * samples_per_sec].reshape((num_seconds, samples_per_sec))
    rms_per_sec = np.sqrt(np.mean(trimmed ** 2, axis=1))
    smoothed_energy = np.convolve(rms_per_sec, np.ones(5) / 5.0, mode="same")

    start_bound = min(90, int(num_seconds * 0.05))
    end_bound = max(num_seconds - 45, int(num_seconds * 0.96))

    # Find top 5 absolute hype peaks across the match for 9:16 Vertical Shorts
    shorts_energy = smoothed_energy.copy()
    shorts_energy[:start_bound] = 0
    shorts_energy[end_bound:] = 0
    top_peaks = []
    for _ in range(5):
        p = int(np.argmax(shorts_energy))
        if shorts_energy[p] <= 0:
            break
        top_peaks.append(float(p))
        shorts_energy[max(0, p - 60):min(num_seconds, p + 60)] = 0

    segments = []

    if game_type == "battle_royale":
        print(f"[*] Battle Royale Mode ({game_info['name']}): Extracting Best Full Fights + Unbroken Final Circle...")
        midgame_cutoff = int(start_bound + (end_bound - start_bound) * 0.72)

        # 1. Lock onto the Final Circle Climax in the last 28% of the VOD
        endgame_slice = smoothed_energy[midgame_cutoff:end_bound]
        climax_sec = (midgame_cutoff + int(np.argmax(endgame_slice))) if len(endgame_slice) > 0 else (end_bound - 30)

        # Capture continuous 4.5-minute Final Circle showdown (240s before winning moment + 30s winner reaction)
        final_circle_start = max(start_bound, climax_sec - 240)
        final_circle_end = min(total_duration, climax_sec + 30)

        # 2. Extract Top 8 Complete Mid-Game Squad Fights (75s each: 30s engage + 45s wipe/reset)
        energy_copy = smoothed_energy.copy()
        energy_copy[:start_bound] = 0
        energy_copy[max(0, final_circle_start - 15):] = 0

        for _ in range(8):
            peak_sec = int(np.argmax(energy_copy))
            if energy_copy[peak_sec] <= 0:
                break
            f_start = max(start_bound, peak_sec - 30)
            f_end = min(final_circle_start - 5, peak_sec + 45)
            if f_end - f_start >= 35:
                segments.append((float(f_start), float(f_end)))
            energy_copy[max(0, peak_sec - 55):min(num_seconds, peak_sec + 55)] = 0

        # Append the unbroken Final Circle climax at the end
        segments.append((float(final_circle_start), float(final_circle_end)))

    else:
        print(f"[*] Tactical Mode ({game_info['name']}): Extracting 24 Extended Rounds (~15-18 min video)...")
        # Extract 24 full action rounds (42 seconds each = ~16.8 minutes total)
        num_rounds = 24
        pre_pad = 24   # Full round execute / site take setup
        post_pad = 18  # Clutch finish + caster hype + round banner

        energy_copy = smoothed_energy.copy()
        energy_copy[:start_bound] = 0
        energy_copy[end_bound:] = 0

        for _ in range(num_rounds):
            peak_sec = int(np.argmax(energy_copy))
            if energy_copy[peak_sec] <= 0:
                break
            r_start = max(start_bound, peak_sec - pre_pad)
            r_end = min(total_duration, peak_sec + post_pad)
            segments.append((float(r_start), float(r_end)))
            energy_copy[max(0, peak_sec - 30):min(num_seconds, peak_sec + 30)] = 0

    # Sort chronologically and merge overlapping or adjacent clips within 4 seconds
    segments.sort(key=lambda x: x[0])
    merged = []
    for seg in segments:
        if not merged:
            merged.append(list(seg))
        else:
            prev = merged[-1]
            if seg[0] <= prev[1] + 4.0:
                prev[1] = max(prev[1], seg[1])
            else:
                merged.append(list(seg))

    final_segments = [(round(s[0], 2), round(s[1], 2)) for s in merged]
    total_mins = sum(e - s for s, e in final_segments) / 60.0
    print(f"[+] Built {len(final_segments)} chronological segments | Total Recap Duration: {total_mins:.1f} minutes")
    return final_segments, top_peaks


# ==============================================================================
# 5. VIDEO RENDERING: 16:9 RECAP, CUSTOM THUMBNAIL & 9:16 SUBTITLED SHORTS
# ==============================================================================
def render_16x9_recap(source_vod: Path, segments: list[tuple[float, float]], output_path: Path):
    concat_list_file = (WORK_DIR / "concat_list.txt").resolve()
    part_files = []

    for idx, (start_t, end_t) in enumerate(segments):
        dur = round(end_t - start_t, 2)
        part_path = (WORK_DIR / f"seg_{idx:03d}.mp4").resolve()
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_t),
            "-t", str(dur),
            "-i", str(source_vod.resolve()),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(part_path)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        part_files.append(part_path)

    with open(concat_list_file, "w", encoding="utf-8") as f:
        for p in part_files:
            f.write(f"file '{p.resolve()}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list_file), "-c", "copy", str(output_path.resolve())
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    for p in part_files:
        p.unlink(missing_ok=True)
    print(f"[+] Rendered 16:9 Full Match Recap: {output_path}")


def generate_custom_thumbnail(source_vod: Path, peak_sec: float, game_key: str, headline: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(peak_sec), "-i", str(source_vod.resolve()),
        "-frames:v", "1", "-q:v", "2", str(raw_frame)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Contrast(img).enhance(1.2)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    accent = GAME_CONFIG[game_key]["accent_color"]
    draw.rectangle([0, 480, 1280, 720], fill=(10, 10, 16, 215))
    draw.rectangle([0, 0, 1280, 720], outline=accent + (255,), width=14)
    draw.rectangle([40, 455, 440, 505], fill=accent + (255,))

    try:
        font_tag = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
    except Exception:
        font_tag = ImageFont.load_default()
        font_main = ImageFont.load_default()

    tag_text = "FINAL CIRCLE & HIGHLIGHTS" if GAME_CONFIG[game_key]["type"] == "battle_royale" else "FULL MATCH HIGHLIGHTS"
    draw.text((55, 463), tag_text[:24], font=font_tag, fill=(255, 255, 255, 255))
    draw.text((45, 535), headline.upper()[:34], font=font_main, fill=(255, 255, 255, 255))
    draw.text((45, 615), GAME_CONFIG[game_key]["name"].upper(), font=font_main, fill=accent + (255,))

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    combined.save(out_jpg.resolve(), "JPEG", quality=92)
    print(f"[+] Generated Custom Thumbnail: {out_jpg}")


def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def render_9x16_short(source_vod: Path, peak_sec: float, game_key: str, banner_title: str, whisper_model: WhisperModel, out_mp4: Path):
    # 58 seconds fits YouTube Shorts, IG Reels, FB Reels, and TikTok cleanly
    clip_dur = 58.0
    start_sec = max(10.0, peak_sec - 28.0)
    raw_clip = (WORK_DIR / "short_raw.mp4").resolve()
    wav_clip = (WORK_DIR / "short_audio.wav").resolve()
    ass_file = (WORK_DIR / "subtitles.ass").resolve()
    out_mp4_abs = out_mp4.resolve()

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(clip_dur),
        "-i", str(source_vod.resolve()), "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", str(raw_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_clip), "-vn", "-ac", "1", "-ar", "16000", str(wav_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Transcribe word-by-word captions with faster-whisper
    segments_gen, _ = whisper_model.transcribe(str(wav_clip), word_timestamps=True)
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TopBanner,DejaVu Sans,52,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,16,0,8,40,40,170,1
Style: WordSub,DejaVu Sans,68,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,310,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean_banner = re.sub(r"[^A-Za-z0-9 vsVS!\-&]", "", banner_title).strip()[:36]
    events = [
        f"Dialogue: 0,0:00:00.00,0:00:58.00,TopBanner,,0,0,0,,{GAME_CONFIG[game_key]['name'].upper()}\\N{clean_banner}"
    ]

    for seg in segments_gen:
        if not seg.words:
            continue
        chunk = []
        for w in seg.words:
            chunk.append(w)
            if len(chunk) == 3:
                s_t = format_ass_time(max(0.0, chunk[0].start))
                e_t = format_ass_time(min(clip_dur, chunk[-1].end))
                txt = re.sub(r"[{}\\]", "", " ".join(x.word.strip().upper() for x in chunk))
                events.append(f"Dialogue: 1,{s_t},{e_t},WordSub,,0,0,0,,{txt}")
                chunk = []
        if chunk:
            s_t = format_ass_time(max(0.0, chunk[0].start))
            e_t = format_ass_time(min(clip_dur, chunk[-1].end))
            txt = re.sub(r"[{}\\]", "", " ".join(x.word.strip().upper() for x in chunk))
            events.append(f"Dialogue: 1,{s_t},{e_t},WordSub,,0,0,0,,{txt}")

    ass_file.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8")

    # Render blurred 9:16 background + sharp centered gameplay (scale=1080:-2 ensures even height) + burned-in ASS subtitles
    vf_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
        "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=subtitles.ass[vout]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_clip),
        "-filter_complex", vf_complex,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k",
        str(out_mp4_abs)
    ], cwd=str(WORK_DIR.resolve()), check=True)
    print(f"[+] Rendered 9:16 Subtitled Short: {out_mp4_abs}")


# ==============================================================================
# 6. MINIMAL-TOKEN GEMINI AI SEO METADATA GENERATOR
# ==============================================================================
def generate_seo_metadata(vod_title: str, game_key: str) -> dict:
    game_info = GAME_CONFIG[game_key]
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    is_br = game_info["type"] == "battle_royale"

    fallback_title = (
        f"{vod_title[:55]} | FINAL CIRCLE & BEST FIGHTS ({game_info['name']})"
        if is_br else f"{vod_title[:60]} | FULL MATCH HIGHLIGHTS ({game_info['name']})"
    )
    fallback_data = {
        "match_headline": vod_title[:32],
        "yt_title": fallback_title[:98],
        "yt_description": (
            f"{vod_title} - Full {game_info['name']} Tournament Match Recap & Highlights!\n\n"
            f"Watch the best teamfights, clutches, and final moments from {game_info['name']} esports.\n\n"
            f"{game_info['hashtags']} #TheRippersGaming"
        ),
        "short_caption": f"INSANE {game_info['name']} Tournament Moment! 🔥 {game_info['hashtags']} #TheRippersGaming"
    }

    if not api_key:
        return fallback_data

    try:
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Source VOD Title: '{vod_title}' | Game: {game_info['name']} ({game_info['type']}). "
            "Return ONLY raw JSON with keys: "
            "'match_headline' (e.g. 'SENTINELS vs FNATIC' or 'ALGS GRAND FINALS', max 28 chars), "
            "'yt_title' (high-CTR YouTube title under 95 chars; mention 'FINAL CIRCLE & BEST FIGHTS' if battle_royale else 'FULL HIGHLIGHTS & ALL ROUNDS'), "
            "'yt_description' (2-paragraph SEO esports description with hashtags), "
            "'short_caption' (hype 1-line vertical video caption with 4 hashtags)."
        )
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw_text = re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw_text)
        return {**fallback_data, **parsed}
    except Exception as e:
        print(f"[!] Gemini fallback used: {e}")
        return fallback_data


# ==============================================================================
# 7. MULTI-PLATFORM PUBLISHERS (YOUTUBE + PLAYLISTS, FB, IG REELS, TIKTOK)
# ==============================================================================
def get_youtube_client():
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        return None
    creds = Credentials(
        token=None,
        refresh_token=rtok,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csec
    )
    return build("youtube", "v3", credentials=creds)


def ensure_game_playlist(youtube, game_key: str) -> str | None:
    cache = {}
    if PLAYLIST_CACHE_FILE.exists():
        try:
            cache = json.loads(PLAYLIST_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    if game_key in cache and cache[game_key]:
        return cache[game_key]

    title = GAME_CONFIG[game_key]["playlist_title"]
    try:
        req = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": f"Automated {GAME_CONFIG[game_key]['name']} tournament match recaps, final circles, and pro highlights by The Rippers Gaming."
                },
                "status": {"privacyStatus": "public"}
            }
        )
        res = req.execute()
        pl_id = res["id"]
        cache[game_key] = pl_id
        PLAYLIST_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        print(f"[+] Created & Cached YouTube Playlist: {title} ({pl_id})")
        return pl_id
    except Exception as e:
        print(f"[!] Playlist creation skipped: {e}")
        return None


def upload_youtube_video(youtube, video_path: Path, title: str, description: str, thumb_path: Path | None, playlist_id: str | None) -> str | None:
    if not youtube:
        return None
    try:
        body = {
            "snippet": {
                "title": title[:99],
                "description": description,
                "categoryId": "20"
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False
            }
        }
        media = MediaFileUpload(str(video_path.resolve()), chunksize=10 * 1024 * 1024, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = req.next_chunk()
        vid_id = response["id"]
        print(f"[+] Uploaded to YouTube: https://youtu.be/{vid_id}")

        if thumb_path and thumb_path.exists():
            try:
                youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(str(thumb_path.resolve()))).execute()
                print("[+] Custom YouTube thumbnail applied!")
            except Exception as te:
                print(f"[!] Thumbnail upload warning: {te}")

        if playlist_id:
            try:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": vid_id}
                        }
                    }
                ).execute()
                print(f"[+] Added video {vid_id} to playlist {playlist_id}")
            except Exception as pe:
                print(f"[!] Playlist insert warning: {pe}")

        return vid_id
    except Exception as e:
        print(f"[-] YouTube Upload Error: {e}")
        return None


def post_youtube_link_to_facebook_page(yt_vid_id: str, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token and yt_vid_id):
        return
    url = f"https://graph.facebook.com/v21.0/{page_id}/feed"
    payload = {
        "message": f"{caption}\n\nWatch Full Match Recap on YouTube: https://www.youtube.com/watch?v={yt_vid_id}",
        "link": f"https://www.youtube.com/watch?v={yt_vid_id}",
        "access_token": token
    }
    res = requests.post(url, data=payload).json()
    print(f"[+] Facebook Page Link Post response: {res}")


def upload_to_facebook_reels(video_path: Path, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token):
        return

    try:
        init_url = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
        init_res = requests.post(init_url, data={"upload_phase": "start", "access_token": token}).json()
        if "video_id" not in init_res:
            print(f"[-] FB Reel Init Failed: {init_res}")
            return
        video_id = init_res["video_id"]
        upload_url = init_res["upload_url"]

        file_size = video_path.stat().st_size
        with open(video_path.resolve(), "rb") as f:
            requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(file_size)
                },
                data=f,
                timeout=180
            )

        pub_res = requests.post(
            init_url,
            data={
                "access_token": token,
                "video_id": video_id,
                "upload_phase": "finish",
                "video_state": "PUBLISHED",
                "description": caption
            }
        ).json()
        print(f"[+] Facebook Reel Published: {pub_res}")
    except Exception as e:
        print(f"[-] FB Reel Error: {e}")


def upload_to_instagram_reels(video_path: Path, caption: str):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (ig_user_id and token):
        print("[!] Skipping Instagram Reel: Missing IG_USER_ID or FB_PAGE_ACCESS_TOKEN")
        return

    try:
        print("[*] Staging Short on Litterbox (Catbox) for Instagram Graph API...")
        with open(video_path.resolve(), "rb") as f:
            host_res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": f},
                timeout=180
            )
        if host_res.status_code != 200 or not host_res.text.startswith("http"):
            print(f"[-] IG Staging Failed: {host_res.text}")
            return

        public_video_url = host_res.text.strip()
        print(f"[+] Staged direct MP4 URL for IG: {public_video_url}")

        container_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
        c_res = requests.post(container_url, data={
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token
        }).json()

        if "id" not in c_res:
            print(f"[-] IG Container Error: {c_res}")
            return

        creation_id = c_res["id"]
        print(f"[*] Polling IG Container {creation_id} status...")

        status_url = f"https://graph.facebook.com/v21.0/{creation_id}"
        for attempt in range(18):
            time.sleep(10)
            s_res = requests.get(status_url, params={"fields": "status_code,status", "access_token": token}).json()
            code = s_res.get("status_code", "")
            print(f"    [IG Poll {attempt+1}/18] status_code = {code}")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                print(f"[-] IG Transcode Error: {s_res}")
                return
        else:
            print("[-] IG Container timed out.")
            return

        pub_res = requests.post(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token}
        ).json()
        print(f"[+] SUCCESS! Instagram Reel Published: {pub_res}")
    except Exception as e:
        print(f"[-] Instagram Upload Exception: {e}")


def get_active_tiktok_token() -> str:
    fallback_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
    refresh_token = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "sbawdctpohczr9bgiv").strip()
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "2e5DenQpSuXqO4w1pHtnFwnqyv8A0V17").strip()

    if refresh_token:
        try:
            res = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                },
                timeout=30
            ).json()
            if "access_token" in res:
                print("[+] Automatically refreshed TikTok Access Token!")
                return res["access_token"]
        except Exception as e:
            print(f"[!] TikTok refresh error: {e}")
    return fallback_token


def upload_to_tiktok(video_path: Path, title: str, access_token: str):
    if not access_token:
        print("[!] Skipping TikTok: Missing TIKTOK_ACCESS_TOKEN")
        return

    try:
        file_size = video_path.stat().st_size
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8"
        }

        # 1. Try Direct Post with SELF_ONLY (Required for TikTok Sandbox apps)
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        direct_payload = {
            "post_info": {
                "title": title[:150],
                "privacy_level": "SELF_ONLY",
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
        res = requests.post(init_url, headers=headers, json=direct_payload, timeout=30).json()

        # 2. Fallback to Inbox Draft Upload (/v2/post/publish/inbox/video/init/) if Direct Post is restricted
        if res.get("error", {}).get("code") != "ok":
            print(f"[*] TikTok Direct Post response ({res.get('error', {}).get('code')}), switching to Inbox Upload...")
            inbox_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
            inbox_payload = {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": file_size,
                    "total_chunk_count": 1
                }
            }
            res = requests.post(inbox_url, headers=headers, json=inbox_payload, timeout=30).json()

        if res.get("error", {}).get("code") != "ok":
            print(f"[-] TikTok Init Failed: {res}")
            return

        upload_url = res["data"]["upload_url"]
        publish_id = res["data"]["publish_id"]

        put_headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}"
        }
        with open(video_path.resolve(), "rb") as f:
            put_res = requests.put(upload_url, headers=put_headers, data=f, timeout=180)

        if put_res.status_code in (200, 201, 206):
            print(f"[+] SUCCESS! TikTok Video Uploaded (Publish ID: {publish_id})")
        else:
            print(f"[-] TikTok PUT Failed ({put_res.status_code}): {put_res.text}")
    except Exception as e:
        print(f"[-] TikTok Upload Exception: {e}")


# ==============================================================================
# 8. MAIN EXECUTION PIPELINE
# ==============================================================================
def main():
    game_key = select_target_game()
    shorts_count = int(os.environ.get("SHORTS_COUNT", "2"))

    # 1. Discover & Download Tournament VOD
    vod_path, info = find_and_download_vod(game_key)
    vod_id = info.get("id", "custom_vod")
    vod_title = info.get("title", f"{GAME_CONFIG[game_key]['name']} Pro Tournament Match")
    print(f"[+] Downloaded VOD: {vod_title} ({vod_id})")

    # 2. Extract Smart Highlights (Battle Royale Final Circle + Best Fights OR 24 Tactical Rounds)
    segments, top_peaks = get_smart_highlight_segments(vod_path, game_key)

    # 3. Generate Minimal-Token Gemini SEO Metadata
    seo = generate_seo_metadata(vod_title, game_key)

    # 4. Render 16:9 Full Recap & Custom Thumbnail
    recap_mp4 = (WORK_DIR / "final_recap_16x9.mp4").resolve()
    thumb_jpg = (WORK_DIR / "custom_thumb.jpg").resolve()
    render_16x9_recap(vod_path, segments, recap_mp4)
    generate_custom_thumbnail(vod_path, top_peaks[0], game_key, seo["match_headline"], thumb_jpg)

    # 5. Upload 16:9 Recap to YouTube + Auto-Sort into Game Playlist + Share on Facebook Page
    youtube = get_youtube_client()
    playlist_id = ensure_game_playlist(youtube, game_key)
    yt_recap_id = upload_youtube_video(
        youtube=youtube,
        video_path=recap_mp4,
        title=seo["yt_title"],
        description=seo["yt_description"],
        thumb_path=thumb_jpg,
        playlist_id=playlist_id
    )
    if yt_recap_id:
        post_youtube_link_to_facebook_page(yt_recap_id, seo["yt_title"])

    # 6. Render & Distribute 9:16 Vertical Shorts (YouTube Shorts, FB Reels, IG Reels, TikTok)
    print("[*] Loading faster-whisper model for word-by-word Short subtitles...")
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    tiktok_token = get_active_tiktok_token()

    for idx, peak in enumerate(top_peaks[:shorts_count]):
        short_mp4 = (WORK_DIR / f"short_{idx+1}.mp4").resolve()
        render_9x16_short(vod_path, peak, game_key, seo["match_headline"], whisper_model, short_mp4)

        short_title = f"{seo['match_headline']} - Insane {GAME_CONFIG[game_key]['name']} Highlight #{idx+1} #Shorts"
        short_caption = seo["short_caption"]

        # A. YouTube Short
        upload_youtube_video(
            youtube=youtube,
            video_path=short_mp4,
            title=short_title[:99],
            description=f"{short_caption}\n\nFull Match Recap on our channel!",
            thumb_path=None,
            playlist_id=playlist_id
        )
        # B. Facebook Reel
        upload_to_facebook_reels(short_mp4, short_caption)
        # C. Instagram Reel (Staged via Litterbox + Polled until FINISHED)
        upload_to_instagram_reels(short_mp4, short_caption)
        # D. TikTok (Sandbox-compliant SELF_ONLY + Inbox fallback)
        upload_to_tiktok(short_mp4, short_caption, tiktok_token)

    # 7. Record VOD ID so it is never duplicated
    mark_vod_processed(vod_id)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print("[+] ALL PLATFORMS COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
