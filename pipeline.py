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
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from faster_whisper import WhisperModel

# ==============================================================================
# 1. 9-GAME CONFIGURATION (FIGHTING GAMES vs. TACTICAL vs. BATTLE ROYALE)
# ==============================================================================
GAME_CONFIG = {
    "valorant": {
        "name": "VALORANT",
        "type": "tactical",
        "accent_color": (255, 70, 85),
        "queries": [
            "Valorant Champions Tour VCT full match VOD",
            "VCT Americas EMEA Pacific Grand Final live stream VOD"
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
            "ALGS Pro League Match Point Finals full stream"
        ],
        "playlist_title": "Apex Legends ALGS Final Circles & Best Fights",
        "hashtags": "#ApexLegends #ALGS #ApexClips #BattleRoyale #Esports"
    },
    "cs2": {
        "name": "Counter-Strike 2",
        "type": "tactical",
        "accent_color": (245, 158, 11),
        "queries": [
            "CS2 ESL Pro League BLAST Premier full match VOD",
            "CS2 Major Playoffs Grand Final full match"
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
            "PUBG Esports Tournament Grand Final stream"
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
            "Fortnite FNCS Major Finals broadcast VOD"
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
            "R6 Esports Major Playoffs full match"
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
IG_STAGE_DIR = Path("ig_stage").resolve()
IG_STAGE_DIR.mkdir(parents=True, exist_ok=True)


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
# 3. GENRE-AWARE MULTI-MATCH STREAM SPLITTER & DOWNLOADER
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


def find_all_match_windows_from_stream(info_data: dict, game_key: str) -> list[tuple[int, int, str]]:
    duration = int(info_data.get("duration") or 0)
    chapters = info_data.get("chapters") or []
    match_windows = []
    
    game_info = GAME_CONFIG[game_key]
    is_fighting = (game_info["type"] == "tactical" and game_key == "tekken")

    for ch in chapters:
        ch_start = int(ch.get("start_time") or 0)
        ch_end = int(ch.get("end_time") or 0)
        ch_dur = ch_end - ch_start
        title = (ch.get("title") or "").strip()
        title_lower = title.lower()

        is_junk = any(w in title_lower for w in ["pre-show", "countdown", "break", "interview", "desk", "analyst", "opening", "closing", "group stage draw"])
        
        if is_fighting:
            is_set = any(w in title_lower for w in [" vs ", " vs. ", "top 8", "finals", "semi-finals", "losers", "winners", "grand final", "set"])
            if 180 <= ch_dur <= 2400 and is_set and not is_junk:
                match_windows.append((ch_start, ch_end, title))
        else:
            is_match = any(w in title_lower for w in [" vs ", " vs. ", "map", "match", "game", "grand final", "playoffs", "round"])
            if 900 <= ch_dur <= 5400 and is_match and not is_junk:
                match_windows.append((ch_start, ch_end, title))

    if not match_windows and duration > 3300:
        print(f"[!] No distinct chapters found for {game_info['name']}. Slicing stream into sequential blocks...")
        block_dur = 900 if is_fighting else 2700
        current = max(600, int(duration * 0.25))
        while current + block_dur < duration - 300:
            match_windows.append((current, current + block_dur, f"Tournament Set Block {len(match_windows)+1}"))
            current += block_dur

    if not match_windows:
        match_windows.append((0, duration, info_data.get("title", f"{game_info['name']} Tournament Match")))

    print(f"[+] Isolated {len(match_windows)} individual sets/matches for {game_info['name']} from this broadcast!")
    return match_windows


# ==============================================================================
# 4. 2-CHUNK 1X NORMAL-SPEED GEMINI AI VISION CLIPPER
# ==============================================================================
def get_audio_fallback_segments(video_path: Path, game_key: str, total_duration: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    game_type = GAME_CONFIG[game_key]["type"]
    pcm_cmd = ["ffmpeg", "-i", str(video_path.resolve()), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    proc = subprocess.Popen(pcm_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_audio, _ = proc.communicate()
    audio_samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32)

    samples_per_sec = 8000
    num_seconds = len(audio_samples) // samples_per_sec
    if num_seconds < 240:
        return [(0.0, total_duration)], [(10.0, min(68.0, total_duration))]

    trimmed = audio_samples[:num_seconds * samples_per_sec].reshape((num_seconds, samples_per_sec))
    rms_per_sec = np.sqrt(np.mean(trimmed ** 2, axis=1))
    smoothed = np.convolve(rms_per_sec, np.ones(5) / 5.0, mode="same")
    median_energy = float(np.median(smoothed))

    start_bound = min(60, int(num_seconds * 0.04))
    end_bound = max(num_seconds - 30, int(num_seconds * 0.96))

    def find_natural_round_end(peak_s: int, min_tail: int, max_tail: int) -> float:
        search_start = min(num_seconds - 5, peak_s + min_tail)
        search_end = min(num_seconds - 2, peak_s + max_tail)
        for t in range(search_start, search_end):
            if smoothed[t] < median_energy * 0.95:
                return float(min(total_duration, t + 6.0))
        return float(min(total_duration, search_end))

    shorts_energy = smoothed.copy()
    shorts_energy[:start_bound] = 0
    shorts_energy[end_bound:] = 0
    short_windows = []
    for _ in range(5):
        p = int(np.argmax(shorts_energy))
        if shorts_energy[p] <= 0:
            break
        s_end = find_natural_round_end(p, min_tail=32, max_tail=48)
        s_start = max(5.0, s_end - 58.0)
        short_windows.append((round(s_start, 2), round(s_end, 2)))
        shorts_energy[max(0, p - 80):min(num_seconds, p + 80)] = 0

    segments = []
    if game_type == "battle_royale":
        mid_cut = int(start_bound + (end_bound - start_bound) * 0.70)
        end_slice = smoothed[mid_cut:end_bound]
        climax = (mid_cut + int(np.argmax(end_slice))) if len(end_slice) > 0 else (end_bound - 45)
        fc_start = max(start_bound, climax - 270)
        fc_end = min(total_duration, climax + 65)

        ec = smoothed.copy()
        ec[:start_bound] = 0
        ec[max(0, fc_start - 20):] = 0
        for _ in range(7):
            p = int(np.argmax(ec))
            if ec[p] <= 0:
                break
            f_start = max(float(start_bound), float(p - 25))
            f_end = min(float(fc_start - 5), find_natural_round_end(p, min_tail=50, max_tail=85))
            if f_end - f_start >= 40:
                segments.append((f_start, f_end))
            ec[max(0, p - 80):min(num_seconds, p + 80)] = 0
        segments.append((float(fc_start), float(fc_end)))
    else:
        ec = smoothed.copy()
        ec[:start_bound] = 0
        ec[end_bound:] = 0
        for _ in range(18):
            p = int(np.argmax(ec))
            if ec[p] <= 0:
                break
            r_start = max(float(start_bound), float(p - 20))
            r_end = find_natural_round_end(p, min_tail=35, max_tail=65)
            segments.append((r_start, r_end))
            ec[max(0, p - 50):min(num_seconds, p + 55)] = 0

    segments.sort(key=lambda x: x[0])
    merged = []
    for seg in segments:
        if not merged:
            merged.append(list(seg))
        elif seg[0] <= merged[-1][1] + 8.0:
            merged[-1][1] = max(merged[-1][1], seg[1])
        else:
            merged.append(list(seg))
    return [(round(s[0], 2), round(s[1], 2)) for s in merged], short_windows


def get_ai_highlight_segments(video_path: Path, game_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path.resolve())
    ]
    total_duration = float(subprocess.check_output(probe_cmd).decode().strip())
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    game_info = GAME_CONFIG[game_key]

    if not api_key:
        print("[!] No GEMINI_API_KEY found, using silence-aware audio detector...")
        return get_audio_fallback_segments(video_path, game_key, total_duration)

    try:
        client = genai.Client(api_key=api_key)
        is_br = game_info["type"] == "battle_royale"

        mid_point = round(total_duration / 2.0, 2)
        chunks = [
            (0.0, mid_point, "Part 1 (Early/Mid Match)"),
            (mid_point, total_duration, "Part 2 (Late Match & Final Climax)")
        ]

        all_recap_raw = []
        all_shorts_raw = []

        for idx, (c_start, c_end, label) in enumerate(chunks):
            c_dur = round(c_end - c_start, 2)
            if c_dur < 60:
                continue

            if idx > 0:
                print("[*] Waiting 65s for Gemini Free-Tier 250k TPM token bucket to reset before Part 2...")
                time.sleep(65)

            proxy_chunk = (WORK_DIR / f"ai_proxy_part{idx+1}.mp4").resolve()
            print(f"[*] Creating 1x Normal-Speed AI Proxy for {label} ({c_start:.0f}s - {c_end:.0f}s)...")
            subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(c_start), "-t", str(c_dur),
                "-i", str(video_path.resolve()),
                "-vf", "scale=640:360", "-r", "1",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
                "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-ar", "16000",
                str(proxy_chunk)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            print(f"[*] Uploading {label} to Gemini Files API...")
            uploaded_file = client.files.upload(file=str(proxy_chunk))
            while uploaded_file.state.name == "PROCESSING":
                time.sleep(5)
                uploaded_file = client.files.get(name=uploaded_file.name)

            if uploaded_file.state.name != "ACTIVE":
                raise RuntimeError(f"Gemini file state error on {label}: {uploaded_file.state.name}")

            if is_br and idx == 1:
                chunk_goal = (
                    "This is the SECOND HALF of a Battle Royale tournament match. "
                    "Identify the top 3-4 complete squad fights AND include the COMPLETE, UNBROKEN Final Circle showdown "
                    "(the last 4.5 to 5.5 minutes of the match all the way through the Winner/Champion screen)."
                )
            elif is_br:
                chunk_goal = (
                    "This is the FIRST HALF of a Battle Royale tournament match. "
                    "Identify the 4 best complete squad fights from initial engagement until the squad wipe finishes + 4 seconds."
                )
            else:
                chunk_goal = (
                    f"This is {label} of a TACTICAL {game_info['name']} tournament match. "
                    "Identify the 9 to 11 best full rounds in this half. "
                    "CRITICAL: Watch the top HUD timer, killfeed, and Round-Won banner. "
                    "Every clip MUST start during the site execute/engagement and MUST NOT END until 4 seconds AFTER the final kill and the Round-Won banner appears. NEVER cut mid-fight!"
                )

            prompt = f"""
You are an elite esports video editor watching a {c_dur:.0f}-second segment ({label}) of a {game_info['name']} tournament VOD.
Note: Timestamps in your response must be relative to THIS video file (from 0.0 to {c_dur:.0f}).
{chunk_goal}

Also pick the top 2 most explosive clutches or squad wipes in this half for vertical Shorts (each Short window must be 45 to 58 seconds long and end 4 seconds AFTER the final kill / round-win banner).

Return ONLY raw valid JSON in this exact format:
{{
  "recap_segments": [
    {{"start": 45.0, "end": 105.0, "reason": "Full round execute and clutch finish"}}
  ],
  "short_segments": [
    {{"start": 210.0, "end": 266.0, "reason": "Full ace clutch through round win banner"}}
  ]
}}
"""
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(temperature=0.1)
            )

            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
            proxy_chunk.unlink(missing_ok=True)

            raw_json = re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw_json)

            for item in data.get("recap_segments", []):
                all_recap_raw.append({
                    "start": float(item["start"]) + c_start,
                    "end": float(item["end"]) + c_start
                })
            for item in data.get("short_segments", []):
                all_shorts_raw.append({
                    "start": float(item["start"]) + c_start,
                    "end": float(item["end"]) + c_start
                })

        segments = []
        for item in all_recap_raw:
            s = max(0.0, item["start"] - 2.0)
            e = min(total_duration, item["end"] + 4.0)
            if e - s >= 15.0:
                segments.append((s, e))

        short_windows = []
        for item in all_shorts_raw:
            s = max(0.0, item["start"])
            e = min(total_duration, item["end"] + 4.0)
            if e - s > 58.0:
                s = e - 58.0
            elif e - s < 35.0:
                s = max(0.0, e - 50.0)
            short_windows.append((round(s, 2), round(e, 2)))

        if len(segments) >= 4 and len(short_windows) >= 1:
            segments.sort(key=lambda x: x[0])
            merged = []
            for seg in segments:
                if not merged:
                    merged.append(list(seg))
                elif seg[0] <= merged[-1][1] + 6.0:
                    merged[-1][1] = max(merged[-1][1], seg[1])
                else:
                    merged.append(list(seg))
            final_segs = [(round(x[0], 2), round(x[1], 2)) for x in merged]
            total_mins = sum(x[1] - x[0] for x in final_segs) / 60.0
            print(f"[+] 2-Chunk Gemini AI Vision Selected {len(final_segs)} Complete Segments ({total_mins:.1f} mins)!")
            return final_segs, short_windows

    except Exception as e:
        print(f"[!] 2-Chunk Gemini AI Clipper fallback triggered: {e}")

    return get_audio_fallback_segments(video_path, game_key, total_duration)


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
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
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


def generate_custom_thumbnail(source_vod: Path, thumb_sec: float, game_key: str, headline: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(thumb_sec), "-i", str(source_vod.resolve()),
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


def render_9x16_short(source_vod: Path, short_window: tuple[float, float], game_key: str, banner_title: str, whisper_model: WhisperModel, out_mp4: Path):
    start_sec, end_sec = short_window
    clip_dur = round(min(58.5, max(30.0, end_sec - start_sec)), 2)
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
        f"Dialogue: 0,0:00:00.00,{format_ass_time(clip_dur)},TopBanner,,0,0,0,,{GAME_CONFIG[game_key]['name'].upper()}\\N{clean_banner}"
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

    vf_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
        "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=subtitles.ass,format=yuv420p[vout]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_clip),
        "-filter_complex", vf_complex,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        str(out_mp4_abs)
    ], cwd=str(WORK_DIR.resolve()), check=True)
    print(f"[+] Rendered AI-Verified 9:16 Short ({start_sec}s -> {end_sec}s): {out_mp4_abs}")


# ==============================================================================
# 6. GEMINI AI SEO METADATA GENERATOR
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
        print(f"[!] Gemini SEO fallback used: {e}")
        return fallback_data


# ==============================================================================
# 7. MULTI-PLATFORM PUBLISHERS (YOUTUBE + PLAYLISTS, FB PAGE, FB REELS, IG REELS)
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


def stage_video_for_instagram(video_path: Path, idx: int) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()

    if repo:
        try:
            stage_filename = f"reel_{int(time.time())}_{idx}.mp4"
            staged_path = IG_STAGE_DIR / stage_filename
            shutil.copy2(video_path.resolve(), staged_path)

            subprocess.run(["git", "add", "-f", str(staged_path)], check=True)
            subprocess.run(["git", "commit", "-m", f"temp: stage reel {idx} for IG [skip ci]"], check=True)
            subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True)

            raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/ig_stage/{stage_filename}"
            time.sleep(5)
            print(f"[+] Staged MP4 on GitHub Raw CDN: {raw_url}")
            return raw_url
        except Exception as e:
            print(f"[!] GitHub Raw staging fallback triggered: {e}")

    try:
        with open(video_path.resolve(), "rb") as f:
            res = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=180
            )
        if res.status_code == 200 and res.text.strip().startswith("http"):
            return res.text.strip()
    except Exception:
        pass

    return None


def upload_to_instagram_reels(video_path: Path, caption: str, idx: int):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (ig_user_id and token):
        print("[!] Skipping Instagram Reel: Missing IG_USER_ID or FB_PAGE_ACCESS_TOKEN")
        return

    try:
        public_video_url = stage_video_for_instagram(video_path, idx)
        if not public_video_url:
            print("[-] IG Staging Failed: Could not obtain public MP4 URL.")
            return

        container_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
        c_res = requests.post(container_url, data={
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token
        }).json()

        if "id" not in c_res:
            print(f"[-] IG Container Creation Error: {c_res}")
            return

        creation_id = c_res["id"]
        print(f"[*] Polling IG Container {creation_id} status...")

        status_url = f"https://graph.facebook.com/v21.0/{creation_id}"
        for attempt in range(24):
            time.sleep(10)
            s_res = requests.get(status_url, params={"fields": "status_code,status", "access_token": token}).json()
            code = s_res.get("status_code", "")
            print(f"    [IG Poll {attempt+1}/24] status_code = {code} | details = {s_res.get('status', '')}")
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


# ==============================================================================
# 8. MAIN EXECUTION PIPELINE
# ==============================================================================
def main():
    game_key = select_target_game()
    shorts_count = int(os.environ.get("SHORTS_COUNT", "2"))

    custom_url = os.environ.get("CUSTOM_VOD_URL", "").strip()
    processed_ids = load_processed_vods()
    game_info = GAME_CONFIG[game_key]

    # 1. Discover stream broadcast
    candidates = []
    if custom_url:
        candidates.append(custom_url)
    else:
        query = random.choice(game_info["queries"])
        print(f"[*] Searching YouTube for {game_info['name']} tournament streams: '{query}'")
        search_cmd = get_ytdlp_base_args() + ["--dump-json", "--flat-playlist", f"ytsearch15:{query}"]
        res = subprocess.run(search_cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            try:
                data = json.loads(line)
                vid_id = data.get("id")
                duration = data.get("duration") or 0
                if vid_id and vid_id not in processed_ids and 1080 <= duration <= 43200:
                    candidates.append(f"https://www.youtube.com/watch?v={vid_id}")
            except Exception:
                continue

    if not candidates:
        raise RuntimeError(f"No suitable VODs found for {game_info['name']}.")

    target_url = candidates[0]
    print(f"[*] Inspecting stream metadata for: {target_url}")
    meta_cmd = get_ytdlp_base_args() + ["--dump-single-json", target_url]
    meta_proc = subprocess.run(meta_cmd, capture_output=True, text=True)
    if meta_proc.returncode != 0:
        raise RuntimeError("Failed to fetch stream metadata.")

    info_data = json.loads(meta_proc.stdout)
    vod_id = info_data.get("id", "stream_vod")
    base_stream_title = info_data.get("title", f"{game_info['name']} Pro Tournament")

    # 2. Isolate all distinct matches / sets from the stream
    match_windows = find_all_match_windows_from_stream(info_data, game_key)
    youtube = get_youtube_client()
    playlist_id = ensure_game_playlist(youtube, game_key)
    whisper_model = None

    # 3. Process each match / set separately
    for match_idx, (start_sec, end_sec, match_title_hint) in enumerate(match_windows):
        print(f"\n==================================================")
        print(f"[*] Processing Match {match_idx+1} of {len(match_windows)}: '{match_title_hint}'")
        print(f"==================================================")

        match_vod_path = (WORK_DIR / f"match_{match_idx}_source.mp4").resolve()
        
        dl_cmd = get_ytdlp_base_args() + [
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "8",
            "--download-sections", f"*{start_sec}-{end_sec}",
            "--force-keyframes-at-cuts",
            "-o", str(match_vod_path),
            target_url
        ]
        dl_proc = subprocess.run(dl_cmd)
        if dl_proc.returncode != 0 or not match_vod_path.exists():
            print(f"[!] Failed to download section for match {match_idx+1}, skipping...")
            continue

        specific_vod_title = f"{base_stream_title} - {match_title_hint}"

        segments, short_windows = get_ai_highlight_segments(match_vod_path, game_key)
        seo = generate_seo_metadata(specific_vod_title, game_key)

        recap_mp4 = (WORK_DIR / f"match_{match_idx}_recap_16x9.mp4").resolve()
        thumb_jpg = (WORK_DIR / f"match_{match_idx}_thumb.jpg").resolve()
        render_16x9_recap(match_vod_path, segments, recap_mp4)
        thumb_timestamp = (short_windows[0][0] + short_windows[0][1]) / 2.0 if short_windows else 40.0
        generate_custom_thumbnail(match_vod_path, thumb_timestamp, game_key, seo["match_headline"], thumb_jpg)

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

        if short_windows:
            if not whisper_model:
                print("[*] Loading faster-whisper model for word-by-word Short subtitles...")
                whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

            for s_idx, s_window in enumerate(short_windows[:shorts_count]):
                short_mp4 = (WORK_DIR / f"match_{match_idx}_short_{s_idx+1}.mp4").resolve()
                render_9x16_short(match_vod_path, s_window, game_key, seo["match_headline"], whisper_model, short_mp4)

                short_title = f"{seo['match_headline']} - {match_title_hint} Highlight #{s_idx+1} #Shorts"
                short_caption = seo["short_caption"]

                upload_youtube_video(youtube=youtube, video_path=short_mp4, title=short_title[:99], description=f"{short_caption}\n\nFull Match Recap on our channel!", thumb_path=None, playlist_id=playlist_id)
                upload_to_facebook_reels(short_mp4, short_caption)
                upload_to_instagram_reels(short_mp4, short_caption, (match_idx * 10) + s_idx + 1)

        match_vod_path.unlink(missing_ok=True)

    mark_vod_processed(vod_id)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(IG_STAGE_DIR, ignore_errors=True)
    print("[+] ALL MATCHUPS AND SETS FROM BROADCAST SPLIT AND PUBLISHED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
