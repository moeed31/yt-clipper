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

# Force immediate real-time logging in GitHub Actions
sys.stdout.reconfigure(line_buffering=True)

# ==============================================================================
# 1. 9-GAME CONFIGURATION (EXPANDED MULTI-QUERY SEARCH POOL)
# ==============================================================================
GAME_CONFIG = {
    "valorant": {
        "name": "VALORANT",
        "type": "tactical",
        "accent_color": (255, 70, 85),
        "queries": [
            "Valorant Champions Tour VCT full match map VOD",
            "VCT Americas EMEA Pacific Grand Final full match",
            "Valorant Masters Champions full map gameplay",
            "VCT Playoffs full match highlight VOD"
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
            "ALGS Pro League Match Point Finals full game",
            "Apex Legends ALGS Championship full match gameplay",
            "ALGS Split Playoffs Finals full game VOD"
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
            "CS2 Major Playoffs full match highlight VOD",
            "IEM Katowice Cologne CS2 full map match",
            "CS2 Premier tournament full game VOD"
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
            "PUBG Esports Tournament Grand Final Match VOD",
            "PUBG Nations Cup Global Championship full match",
            "PUBG PGS Finals full game esports"
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
            "Fortnite FNCS Major Finals full match",
            "FNCS Global Championship full game highlights",
            "Fortnite competitive tournament finals full match"
        ],
        "playlist_title": "Fortnite FNCS Endgames & Best Fights",
        "hashtags": "#Fortnite #FNCS #FortniteClips #FortniteCompetitive #Gaming"
    },
    "r6": {
        "name": "Rainbow Six Siege",
        "type": "tactical",
        "accent_color": (59, 130, 246),
        "queries": [
            "Rainbow Six Siege Six Invitational full match",
            "R6 Esports Major Playoffs full map VOD",
            "Rainbow Six Siege Pro League full game",
            "BLAST R6 Major full match gameplay"
        ],
        "playlist_title": "Rainbow Six Siege Pro Recaps",
        "hashtags": "#R6Siege #RainbowSixSiege #R6Esports #FPS #Gaming"
    },
    "cod": {
        "name": "Call of Duty",
        "type": "tactical",
        "accent_color": (34, 197, 94),
        "queries": [
            "Call of Duty League CDL Major full match",
            "CDL Championship Weekend full series match",
            "Call of Duty League OpTic FaZe Ultra Thieves full match",
            "CDL Major Finals full game VOD"
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
            "EVO Tekken 8 Finals full match set",
            "Tekken 8 Arslan Ash Knee LowHigh tournament full set",
            "Tekken 8 Major Top 8 full match"
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
            "LoL Esports MSI Worlds Playoffs full match",
            "T1 GenG G2 Fnatic full game highlight VOD",
            "League of Legends Worlds Finals full game"
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
# 2. STATE TRACKING & DYNAMIC GEMINI MODEL RESOLVER
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
    print(f"[*] Auto-selected game in rotation: {GAME_CONFIG[selected]['name']} ({selected})", flush=True)
    return selected


def resolve_working_gemini_model(client: genai.Client) -> str:
    candidates = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.0-flash"
    ]
    try:
        available = [m.name.replace("models/", "") for m in client.models.list()]
        for c in candidates:
            if c in available:
                print(f"[+] Using active Gemini model: {c}", flush=True)
                return c
        for m in available:
            if "flash" in m and "tts" not in m and "image" not in m:
                print(f"[+] Using discovered Gemini Flash model: {m}", flush=True)
                return m
    except Exception as e:
        print(f"[!] Model list check warning: {e}", flush=True)
    return "gemini-2.5-flash"


# ==============================================================================
# 3. MULTI-QUERY YOUTUBE VOD DISCOVERY & AUTO-ROTATING DOWNLOADER
# ==============================================================================
def get_ytdlp_base_args() -> list[str]:
    args = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout", "30",
        "--retries", "3",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
    ]
    cookie_file = Path("cookies.txt").resolve()
    if cookie_file.exists() and cookie_file.stat().st_size > 40:
        args.extend(["--cookies", str(cookie_file)])
    return args


def search_candidates_for_game(game_key: str, processed_ids: set) -> list[tuple[str, int]]:
    """
    Searches across ALL queries for a game (up to 30 results per query) and accepts
    VODs from 12 minutes (720s) up to 3 hours (10800s) so R6, COD, and Tekken never fail!
    """
    game_info = GAME_CONFIG[game_key]
    queries = list(game_info["queries"])
    random.shuffle(queries)
    candidates = []
    seen = set()

    for query in queries:
        print(f"[*] Searching YouTube for {game_info['name']} VODs: '{query}'", flush=True)
        search_cmd = get_ytdlp_base_args() + ["--dump-json", "--flat-playlist", f"ytsearch30:{query}"]
        try:
            res = subprocess.run(search_cmd, capture_output=True, text=True, timeout=90)
        except Exception as e:
            print(f"[!] Search query timeout/error: {e}", flush=True)
            continue

        for line in res.stdout.splitlines():
            try:
                data = json.loads(line)
                vid_id = data.get("id")
                duration = int(data.get("duration") or 0)
                live_status = data.get("live_status") or ""
                if not vid_id or vid_id in processed_ids or vid_id in seen:
                    continue
                if live_status in ("is_live", "is_upcoming"):
                    continue
                # Accept 12 mins (720s) to 3 hours (10800s)
                if 720 <= duration <= 10800:
                    seen.add(vid_id)
                    candidates.append((f"https://www.youtube.com/watch?v={vid_id}", duration))
            except Exception:
                continue

        # As soon as we have at least 5 strong candidates, stop searching more queries
        if len(candidates) >= 5:
            break

    # Prefer single-map / single-set VODs (15–55 mins) first for fastest download speed
    candidates.sort(key=lambda x: 0 if 900 <= x[1] <= 3300 else 1)
    return candidates


def find_and_download_vod(initial_game_key: str) -> tuple[Path, dict, str]:
    custom_url = os.environ.get("CUSTOM_VOD_URL", "").strip()
    processed_ids = load_processed_vods()
    output_template = str((WORK_DIR / "source_vod.%(ext)s").resolve())

    # Build rotation order starting with initial_game_key so if one game has 0 results, it tries the next
    all_keys = list(GAME_CONFIG.keys())
    start_idx = all_keys.index(initial_game_key) if initial_game_key in all_keys else 0
    ordered_games = all_keys[start_idx:] + all_keys[:start_idx]

    for game_key in ordered_games:
        game_info = GAME_CONFIG[game_key]
        if custom_url:
            candidates = [(custom_url, 2400)]
        else:
            candidates = search_candidates_for_game(game_key, processed_ids)

        if not candidates:
            print(f"[!] No suitable VODs found for {game_info['name']}, rotating to next game...", flush=True)
            continue

        print(f"[+] Found {len(candidates)} candidate VODs for {game_info['name']}!", flush=True)

        for url, dur in candidates[:4]:
            print(f"[*] Downloading VOD ({dur//60} mins): {url}", flush=True)
            dl_cmd = get_ytdlp_base_args() + [
                "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--concurrent-fragments", "8",
                "--write-info-json",
                "-o", output_template,
                url
            ]
            try:
                proc = subprocess.run(dl_cmd, timeout=600)
            except subprocess.TimeoutExpired:
                print(f"[!] Download timed out for {url}, trying next candidate...", flush=True)
                continue

            mp4_path = (WORK_DIR / "source_vod.mp4").resolve()
            info_path = (WORK_DIR / "source_vod.info.json").resolve()
            if proc.returncode == 0 and mp4_path.exists():
                info_data = {}
                if info_path.exists():
                    info_data = json.loads(info_path.read_text(encoding="utf-8", errors="ignore"))

                # If the downloaded VOD is longer than 50 minutes (e.g. a 2-hour BO5 series),
                # fast-trim the active 45-minute match block locally with stream copy (-c copy) in 3 seconds!
                actual_dur = int(info_data.get("duration") or dur or 0)
                if actual_dur > 3000:
                    trimmed_mp4 = (WORK_DIR / "source_vod_trimmed.mp4").resolve()
                    offset_s = min(300, int(actual_dur * 0.05))
                    print(f"[*] VOD is {actual_dur//60}m long; fast-slicing 45-minute prime match window locally...", flush=True)
                    subprocess.run([
                        "ffmpeg", "-y", "-ss", str(offset_s), "-t", "2700",
                        "-i", str(mp4_path), "-c", "copy", str(trimmed_mp4)
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
                    mp4_path.unlink(missing_ok=True)
                    trimmed_mp4.rename(mp4_path)

                print(f"[+] Download & preparation complete: {mp4_path.name} ({mp4_path.stat().st_size / (1024*1024):.1f} MB)", flush=True)
                return mp4_path, info_data, game_key

    raise RuntimeError("All VOD download attempts across games failed.")


# ==============================================================================
# 4. HANG-PROOF HIGHLIGHT ENGINE (2-CHUNK GEMINI VISION + DISK-BASED FALLBACK)
# ==============================================================================
def get_audio_fallback_segments(video_path: Path, game_key: str, total_duration: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    print("[*] Running silence-aware audio highlight analysis (disk-buffered)...", flush=True)
    game_type = GAME_CONFIG[game_key]["type"]
    pcm_file = (WORK_DIR / "audio_energy.pcm").resolve()

    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path.resolve()),
        "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", str(pcm_file)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)

    raw_bytes = pcm_file.read_bytes()
    pcm_file.unlink(missing_ok=True)
    audio_samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)

    samples_per_sec = 4000
    num_seconds = len(audio_samples) // samples_per_sec
    if num_seconds < 240:
        return [(0.0, total_duration)], [(10.0, min(58.0, total_duration))]

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
        s_end = find_natural_round_end(p, min_tail=35, max_tail=48)
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
            r_end = find_natural_round_end(p, min_tail=38, max_tail=65)
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

    final_segs = [(round(s[0], 2), round(s[1], 2)) for s in merged]
    print(f"[+] Audio Highlight Engine selected {len(final_segs)} full segments!", flush=True)
    return final_segs, short_windows


def get_ai_highlight_segments(video_path: Path, game_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]], str]:
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path.resolve())
    ]
    total_duration = float(subprocess.check_output(probe_cmd, timeout=30).decode().strip())
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    game_info = GAME_CONFIG[game_key]

    if not api_key:
        segs, shorts = get_audio_fallback_segments(video_path, game_key, total_duration)
        return segs, shorts, "gemini-2.5-flash"

    client = genai.Client(api_key=api_key)
    model_name = resolve_working_gemini_model(client)

    try:
        is_br = game_info["type"] == "battle_royale"
        mid_point = round(total_duration / 2.0, 2)
        chunks = [
            (0.0, mid_point, "Part 1 (First Half)"),
            (mid_point, total_duration, "Part 2 (Second Half & Climax)")
        ]

        all_recap_raw = []
        all_shorts_raw = []

        for idx, (c_start, c_end, label) in enumerate(chunks):
            c_dur = round(c_end - c_start, 2)
            if c_dur < 60:
                continue

            if idx > 0:
                print("[*] Waiting 65s for Gemini Free-Tier 250k TPM bucket reset before Part 2...", flush=True)
                time.sleep(65)

            proxy_chunk = (WORK_DIR / f"ai_proxy_part{idx+1}.mp4").resolve()
            print(f"[*] Creating 1x Normal-Speed AI Proxy for {label} ({c_start:.0f}s - {c_end:.0f}s)...", flush=True)
            subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(c_start), "-t", str(c_dur),
                "-i", str(video_path.resolve()),
                "-vf", "scale=480:270", "-r", "1",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34",
                "-c:a", "aac", "-b:a", "24k", "-ac", "1", "-ar", "16000",
                str(proxy_chunk)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)

            print(f"[*] Uploading {label} ({proxy_chunk.stat().st_size / (1024*1024):.1f} MB) to Gemini Files API...", flush=True)
            uploaded_file = client.files.upload(file=str(proxy_chunk))

            for _ in range(36):
                if uploaded_file.state.name != "PROCESSING":
                    break
                time.sleep(5)
                uploaded_file = client.files.get(name=uploaded_file.name)

            if uploaded_file.state.name != "ACTIVE":
                raise RuntimeError(f"Gemini file processing timed out or failed: {uploaded_file.state.name}")

            if is_br and idx == 1:
                chunk_goal = (
                    "Identify the top 3-4 complete squad fights AND include the COMPLETE, UNBROKEN Final Circle showdown "
                    "(the last 4.5 to 5.5 minutes of the match all the way through the Winner/Champion screen)."
                )
            elif is_br:
                chunk_goal = "Identify the 4 best complete squad fights from initial engagement until the squad wipe finishes + 4 seconds."
            else:
                chunk_goal = (
                    f"Identify the 8 to 10 best full rounds in {label}. "
                    "CRITICAL: Watch the top HUD timer, killfeed, and Round-Won banner. "
                    "Every clip MUST start during the site execute/engagement and MUST NOT END until 4 seconds AFTER the final kill and Round-Won banner. NEVER cut mid-fight!"
                )

            prompt = f"""
You are an elite esports video editor watching a {c_dur:.0f}-second segment ({label}) of a {game_info['name']} tournament VOD.
Timestamps must be relative to THIS video file (from 0.0 to {c_dur:.0f}).
{chunk_goal}

Also pick the top 2 most explosive clutches or squad wipes in this half for vertical Shorts (each Short window must be 45 to 58 seconds long and end 4 seconds AFTER the final kill / round-win banner).

Return ONLY raw valid JSON:
{{
  "recap_segments": [{{"start": 45.0, "end": 105.0}}],
  "short_segments": [{{"start": 210.0, "end": 266.0}}]
}}
"""
            print(f"[*] Analyzing {label} with {model_name}...", flush=True)
            resp = client.models.generate_content(
                model=model_name,
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
                all_recap_raw.append({"start": float(item["start"]) + c_start, "end": float(item["end"]) + c_start})
            for item in data.get("short_segments", []):
                all_shorts_raw.append({"start": float(item["start"]) + c_start, "end": float(item["end"]) + c_start})

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

        if len(segments) >= 3 and len(short_windows) >= 1:
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
            print(f"[+] Gemini AI Vision selected {len(final_segs)} complete segments!", flush=True)
            return final_segs, short_windows, model_name

    except Exception as e:
        print(f"[!] Gemini AI Vision fallback triggered: {e}", flush=True)

    segs, shorts = get_audio_fallback_segments(video_path, game_key, total_duration)
    return segs, shorts, model_name


# ==============================================================================
# 5. VIDEO RENDERING: 16:9 RECAP, CUSTOM THUMBNAIL & 9:16 SUBTITLED SHORTS
# ==============================================================================
def render_16x9_recap(source_vod: Path, segments: list[tuple[float, float]], output_path: Path):
    print(f"[*] Rendering 16:9 Recap across {len(segments)} segments...", flush=True)
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
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            str(part_path)
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
            part_files.append(part_path)
        except Exception as e:
            print(f"[!] Segment {idx} skipped: {e}", flush=True)

    with open(concat_list_file, "w", encoding="utf-8") as f:
        for p in part_files:
            f.write(f"file '{p.resolve()}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list_file), "-c", "copy", str(output_path.resolve())
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)

    for p in part_files:
        p.unlink(missing_ok=True)
    print(f"[+] Rendered 16:9 Full Match Recap: {output_path.name}", flush=True)


def generate_custom_thumbnail(source_vod: Path, thumb_sec: float, game_key: str, headline: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(thumb_sec), "-i", str(source_vod.resolve()),
        "-frames:v", "1", "-q:v", "2", str(raw_frame)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

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
    print(f"[+] Generated Custom Thumbnail: {out_jpg.name}", flush=True)


def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def render_9x16_short(source_vod: Path, short_window: tuple[float, float], game_key: str, banner_title: str, whisper_model: WhisperModel, out_mp4: Path):
    start_sec, end_sec = short_window
    clip_dur = round(min(58.0, max(30.0, end_sec - start_sec)), 2)
    raw_clip = (WORK_DIR / "short_raw.mp4").resolve()
    wav_clip = (WORK_DIR / "short_audio.wav").resolve()
    ass_file = (WORK_DIR / "subtitles.ass").resolve()
    out_mp4_abs = out_mp4.resolve()

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(clip_dur),
        "-i", str(source_vod.resolve()), "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(raw_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_clip), "-vn", "-ac", "1", "-ar", "16000", str(wav_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)

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
    ], cwd=str(WORK_DIR.resolve()), timeout=180, check=True)
    print(f"[+] Rendered 9:16 Subtitled Short ({start_sec}s -> {end_sec}s): {out_mp4_abs.name}", flush=True)


# ==============================================================================
# 6. GEMINI AI SEO METADATA GENERATOR
# ==============================================================================
def generate_seo_metadata(vod_title: str, game_key: str, model_name: str) -> dict:
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
        resp = client.models.generate_content(model=model_name, contents=prompt)
        raw_text = re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw_text)
        return {**fallback_data, **parsed}
    except Exception as e:
        print(f"[!] Gemini SEO fallback used: {e}", flush=True)
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
    if not youtube:
        return None
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
        print(f"[+] Created & Cached YouTube Playlist: {title} ({pl_id})", flush=True)
        return pl_id
    except Exception as e:
        print(f"[!] Playlist creation skipped: {e}", flush=True)
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
        print(f"[+] Uploaded to YouTube: https://youtu.be/{vid_id}", flush=True)

        if thumb_path and thumb_path.exists():
            try:
                youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(str(thumb_path.resolve()))).execute()
                print("[+] Custom YouTube thumbnail applied!", flush=True)
            except Exception as te:
                print(f"[!] Thumbnail upload warning: {te}", flush=True)

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
                print(f"[+] Added video {vid_id} to playlist {playlist_id}", flush=True)
            except Exception as pe:
                print(f"[!] Playlist insert warning: {pe}", flush=True)

        return vid_id
    except Exception as e:
        print(f"[-] YouTube Upload Error: {e}", flush=True)
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
    res = requests.post(url, data=payload, timeout=30).json()
    print(f"[+] Facebook Page Link Post response: {res}", flush=True)


def upload_to_facebook_reels(video_path: Path, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token):
        return

    try:
        init_url = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
        init_res = requests.post(init_url, data={"upload_phase": "start", "access_token": token}, timeout=30).json()
        if "video_id" not in init_res:
            print(f"[-] FB Reel Init Failed: {init_res}", flush=True)
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
            },
            timeout=60
        ).json()
        print(f"[+] Facebook Reel Published: {pub_res}", flush=True)
    except Exception as e:
        print(f"[-] FB Reel Error: {e}", flush=True)


def stage_video_for_instagram(video_path: Path, idx: int) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()

    if repo:
        try:
            stage_filename = f"reel_{int(time.time())}_{idx}.mp4"
            staged_path = IG_STAGE_DIR / stage_filename
            shutil.copy2(video_path.resolve(), staged_path)

            subprocess.run(["git", "add", "-f", str(staged_path)], timeout=30, check=True)
            subprocess.run(["git", "commit", "-m", f"temp: stage reel {idx} for IG [skip ci]"], timeout=30, check=True)
            subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], timeout=60, check=True)

            raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/ig_stage/{stage_filename}"
            time.sleep(5)
            print(f"[+] Staged MP4 on GitHub Raw CDN: {raw_url}", flush=True)
            return raw_url
        except Exception as e:
            print(f"[!] GitHub Raw staging fallback triggered: {e}", flush=True)

    return None


def upload_to_instagram_reels(video_path: Path, caption: str, idx: int):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (ig_user_id and token):
        print("[!] Skipping Instagram Reel: Missing IG_USER_ID or FB_PAGE_ACCESS_TOKEN", flush=True)
        return

    try:
        public_video_url = stage_video_for_instagram(video_path, idx)
        if not public_video_url:
            print("[-] IG Staging Failed: Could not obtain public MP4 URL.", flush=True)
            return

        container_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
        c_res = requests.post(container_url, data={
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token
        }, timeout=30).json()

        if "id" not in c_res:
            print(f"[-] IG Container Creation Error: {c_res}", flush=True)
            return

        creation_id = c_res["id"]
        print(f"[*] Polling IG Container {creation_id} status...", flush=True)

        status_url = f"https://graph.facebook.com/v21.0/{creation_id}"
        for attempt in range(20):
            time.sleep(10)
            s_res = requests.get(status_url, params={"fields": "status_code,status", "access_token": token}, timeout=30).json()
            code = s_res.get("status_code", "")
            print(f"    [IG Poll {attempt+1}/20] status_code = {code} | details = {s_res.get('status', '')}", flush=True)
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                print(f"[-] IG Transcode Error: {s_res}", flush=True)
                return
        else:
            print("[-] IG Container timed out.", flush=True)
            return

        pub_res = requests.post(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=30
        ).json()
        print(f"[+] SUCCESS! Instagram Reel Published: {pub_res}", flush=True)
    except Exception as e:
        print(f"[-] Instagram Upload Exception: {e}", flush=True)


# ==============================================================================
# 8. MAIN EXECUTION PIPELINE
# ==============================================================================
def main():
    initial_game_key = select_target_game()
    shorts_count = int(os.environ.get("SHORTS_COUNT", "2"))

    # 1. Discover & Download Tournament VOD (Auto-rotates if a game has 0 matches)
    vod_path, info, game_key = find_and_download_vod(initial_game_key)
    vod_id = info.get("id", "custom_vod")
    vod_title = info.get("title", f"{GAME_CONFIG[game_key]['name']} Pro Tournament Match")
    print(f"[+] Downloaded Match Video: {vod_title} ({vod_id})", flush=True)

    # 2. Extract AI-Verified Highlights (with Auto-Detected Gemini Model + Disk Fallback)
    segments, short_windows, active_model = get_ai_highlight_segments(vod_path, game_key)

    # 3. Generate Gemini SEO Metadata
    seo = generate_seo_metadata(vod_title, game_key, active_model)

    # 4. Render 16:9 Full Recap & Custom Thumbnail
    recap_mp4 = (WORK_DIR / "final_recap_16x9.mp4").resolve()
    thumb_jpg = (WORK_DIR / "custom_thumb.jpg").resolve()
    render_16x9_recap(vod_path, segments, recap_mp4)
    thumb_timestamp = (short_windows[0][0] + short_windows[0][1]) / 2.0 if short_windows else 45.0
    generate_custom_thumbnail(vod_path, thumb_timestamp, game_key, seo["match_headline"], thumb_jpg)

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

    # 6. Render & Distribute 9:16 Vertical Shorts (YouTube Shorts, FB Reels, IG Reels)
    print("[*] Loading faster-whisper model for word-by-word Short subtitles...", flush=True)
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

    for idx, s_window in enumerate(short_windows[:shorts_count]):
        short_mp4 = (WORK_DIR / f"short_{idx+1}.mp4").resolve()
        render_9x16_short(vod_path, s_window, game_key, seo["match_headline"], whisper_model, short_mp4)

        short_title = f"{seo['match_headline']} - Insane {GAME_CONFIG[game_key]['name']} Highlight #{idx+1} #Shorts"
        short_caption = seo["short_caption"]

        upload_youtube_video(
            youtube=youtube,
            video_path=short_mp4,
            title=short_title[:99],
            description=f"{short_caption}\n\nFull Match Recap on our channel!",
            thumb_path=None,
            playlist_id=playlist_id
        )
        upload_to_facebook_reels(short_mp4, short_caption)
        upload_to_instagram_reels(short_mp4, short_caption, idx + 1)

    # 7. Record VOD ID & Clean Up
    mark_vod_processed(vod_id)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(IG_STAGE_DIR, ignore_errors=True)
    print("[+] ALL PLATFORMS (YOUTUBE, FACEBOOK, INSTAGRAM) COMPLETED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    main()
