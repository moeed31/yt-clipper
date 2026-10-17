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

GAME_CONFIG = {
    "valorant": {
        "name": "VALORANT",
        "type": "tactical",
        "accent_color": (255, 70, 85),
        "queries": ["Valorant Champions Tour VCT full match VOD", "VCT Grand Final full match"],
        "playlist_title": "VALORANT Tournament Highlights & Full Recaps",
        "hashtags": "#Valorant #VCT #ValorantClips #ValorantEsports #Gaming"
    },
    "apex": {
        "name": "Apex Legends",
        "type": "battle_royale",
        "accent_color": (220, 38, 38),
        "queries": ["ALGS Apex Legends Global Series Finals full match VOD", "ALGS Finals full game"],
        "playlist_title": "Apex Legends ALGS Final Circles & Best Fights",
        "hashtags": "#ApexLegends #ALGS #ApexClips #BattleRoyale #Esports"
    },
    "cs2": {
        "name": "Counter-Strike 2",
        "type": "tactical",
        "accent_color": (245, 158, 11),
        "queries": ["CS2 ESL Pro League BLAST Premier full match VOD", "CS2 Major Playoffs full match"],
        "playlist_title": "CS2 Pro Match Recaps & Highlights",
        "hashtags": "#CS2 #CounterStrike2 #CS2Clips #ESL #Esports"
    },
    "pubg": {
        "name": "PUBG Battlegrounds",
        "type": "battle_royale",
        "accent_color": (234, 179, 8),
        "queries": ["PUBG Global Series PGS PGC Grand Finals full match VOD", "PUBG Tournament Grand Final"],
        "playlist_title": "PUBG Tournament Final Circles & Recaps",
        "hashtags": "#PUBG #PUBGEsports #BattleRoyale #PUBGClips #Gaming"
    },
    "fortnite": {
        "name": "Fortnite",
        "type": "battle_royale",
        "accent_color": (56, 189, 248),
        "queries": ["FNCS Fortnite Championship Series Grand Finals full game VOD"],
        "playlist_title": "Fortnite FNCS Endgames & Best Fights",
        "hashtags": "#Fortnite #FNCS #FortniteClips #FortniteCompetitive #Gaming"
    },
    "r6": {
        "name": "Rainbow Six Siege",
        "type": "tactical",
        "accent_color": (59, 130, 246),
        "queries": ["Rainbow Six Siege Six Invitational Pro League full match VOD"],
        "playlist_title": "Rainbow Six Siege Pro Recaps",
        "hashtags": "#R6Siege #RainbowSixSiege #R6Esports #FPS #Gaming"
    },
    "cod": {
        "name": "Call of Duty",
        "type": "tactical",
        "accent_color": (34, 197, 94),
        "queries": ["Call of Duty League CDL Major Finals full match VOD"],
        "playlist_title": "Call of Duty League Match Recaps",
        "hashtags": "#CallOfDuty #CDL #CODLeague #CODClips #Esports"
    },
    "tekken": {
        "name": "Tekken 8",
        "type": "tactical",
        "accent_color": (168, 85, 247),
        "queries": ["Tekken 8 World Tour TWT Top 8 Grand Finals full set"],
        "playlist_title": "Tekken 8 Tournament Sets & Highlights",
        "hashtags": "#Tekken8 #TWT #FightingGames #FGC #Esports"
    },
    "lol": {
        "name": "League of Legends",
        "type": "tactical",
        "accent_color": (14, 165, 233),
        "queries": ["LCK LEC Worlds League of Legends full game match VOD"],
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
    return keys[processed_count % len(keys)]


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
        print(f"[*] Searching YouTube for {game_info['name']} VODs: '{query}'")
        search_cmd = get_ytdlp_base_args() + ["--dump-json", "--flat-playlist", f"ytsearch10:{query}"]
        res = subprocess.run(search_cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            try:
                data = json.loads(line)
                vid_id = data.get("id")
                duration = data.get("duration") or 0
                # Filter for standard single tournament match VODs (20 mins to 2 hours) to avoid huge streams hanging
                if vid_id and vid_id not in processed_ids and 1200 <= duration <= 7200:
                    candidates.append(f"https://www.youtube.com/watch?v={vid_id}")
            except Exception:
                continue

    if not candidates:
        raise RuntimeError(f"No suitable standard VODs found for {game_info['name']}.")

    for url in candidates[:3]:
        print(f"[*] Downloading VOD safely: {url}")
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


def get_audio_fallback_segments(video_path: Path, game_key: str, total_duration: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    game_type = GAME_CONFIG[game_key]["type"]
    pcm_cmd = ["ffmpeg", "-i", str(video_path.resolve()), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    proc = subprocess.Popen(pcm_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_audio, _ = proc.communicate()
    audio_samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32)

    samples_per_sec = 8000
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
        segments.append((float(fc_start), float(fc_end)))
    else:
        ec = smoothed.copy()
        ec[:start_bound] = 0
        ec[end_bound:] = 0
        for _ in range(15):
            p = int(np.argmax(ec))
            if ec[p] <= 0:
                break
            r_start = max(float(start_bound), float(p - 20))
            r_end = find_natural_round_end(p, min_tail=35, max_tail=65)
            segments.append((r_start, r_end))
            ec[max(0, p - 50):min(num_seconds, p + 55)] = 0

    return segments, short_windows


def get_ai_highlight_segments(video_path: Path, game_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path.resolve())]
    total_duration = float(subprocess.check_output(probe_cmd).decode().strip())
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        return get_audio_fallback_segments(video_path, game_key, total_duration)

    try:
        client = genai.Client(api_key=api_key)
        proxy_chunk = (WORK_DIR / "ai_proxy.mp4").resolve()
        
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path.resolve()),
            "-vf", "scale=640:360", "-r", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
            "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-ar", "16000",
            str(proxy_chunk)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        uploaded_file = client.files.upload(file=str(proxy_chunk))
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)

        prompt = f"""
Watch this {total_duration:.0f}-second match video and return the exact timestamps for full rounds/fights and shorts in raw JSON format:
{{
  "recap_segments": [{{"start": 30.0, "end": 90.0}}],
  "short_segments": [{{"start": 120.0, "end": 178.0}}]
}}
"""
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(temperature=0.1)
        )
        client.files.delete(name=uploaded_file.name)
        proxy_chunk.unlink(missing_ok=True)

        data = json.loads(re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip())
        segs = [(float(x["start"]), float(x["end"])) for x in data.get("recap_segments", [])]
        shorts = [(float(x["start"]), float(x["end"])) for x in data.get("short_segments", [])]
        if segs:
            return segs, shorts
    except Exception as e:
        print(f"[!] AI Clipping fallback: {e}")

    return get_audio_fallback_segments(video_path, game_key, total_duration)


def render_16x9_recap(source_vod: Path, segments: list[tuple[float, float]], output_path: Path):
    concat_list_file = (WORK_DIR / "concat_list.txt").resolve()
    part_files = []

    for idx, (start_t, end_t) in enumerate(segments):
        dur = round(end_t - start_t, 2)
        part_path = (WORK_DIR / f"seg_{idx:03d}.mp4").resolve()
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_t), "-t", str(dur), "-i", str(source_vod.resolve()),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "192k", str(part_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        part_files.append(part_path)

    with open(concat_list_file, "w", encoding="utf-8") as f:
        for p in part_files:
            f.write(f"file '{p.resolve()}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_file), "-c", "copy", str(output_path.resolve())
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def generate_custom_thumbnail(source_vod: Path, thumb_sec: float, game_key: str, headline: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run(["ffmpeg", "-y", "-ss", str(thumb_sec), "-i", str(source_vod.resolve()), "-frames:v", "1", str(raw_frame)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    img.save(out_jpg.resolve(), "JPEG", quality=92)


def render_9x16_short(source_vod: Path, short_window: tuple[float, float], game_key: str, banner_title: str, whisper_model: WhisperModel, out_mp4: Path):
    start_sec, end_sec = short_window
    clip_dur = round(min(58.0, max(30.0, end_sec - start_sec)), 2)
    raw_clip = (WORK_DIR / "short_raw.mp4").resolve()
    
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(clip_dur), "-i", str(source_vod.resolve()),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", str(raw_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    shutil.copy(raw_clip, out_mp4.resolve())


def generate_seo_metadata(vod_title: str, game_key: str) -> dict:
    game_info = GAME_CONFIG[game_key]
    return {
        "match_headline": vod_title[:32],
        "yt_title": f"{vod_title[:60]} | Highlights ({game_info['name']})",
        "yt_description": f"{vod_title} - Full Match Highlights!\n{game_info['hashtags']} #TheRippersGaming",
        "short_caption": f"INSANE {game_info['name']} Moment! 🔥 {game_info['hashtags']} #TheRippersGaming"
    }


def get_youtube_client():
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        return None
    return build("youtube", "v3", credentials=Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec))


def ensure_game_playlist(youtube, game_key: str) -> str | None:
    if not youtube:
        return None
    try:
        res = youtube.playlists().insert(part="snippet,status", body={"snippet": {"title": GAME_CONFIG[game_key]["playlist_title"]}, "status": {"privacyStatus": "public"}}).execute()
        return res["id"]
    except Exception:
        return None


def upload_youtube_video(youtube, video_path: Path, title: str, description: str, thumb_path: Path | None, playlist_id: str | None) -> str | None:
    if not youtube:
        return None
    try:
        body = {"snippet": {"title": title[:99], "description": description, "categoryId": "20"}, "status": {"privacyStatus": "public"}}
        media = MediaFileUpload(str(video_path.resolve()), chunksize=10 * 1024 * 1024, resumable=True)
        response = None
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        while response is None:
            _, response = req.next_chunk()
        return response["id"]
    except Exception:
        return None


def post_youtube_link_to_facebook_page(yt_vid_id: str, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if page_id and token and yt_vid_id:
        requests.post(f"https://graph.facebook.com/v21.0/{page_id}/feed", data={"message": f"{caption}\nhttps://youtu.be/{yt_vid_id}", "link": f"https://youtu.be/{yt_vid_id}", "access_token": token})


def upload_to_facebook_reels(video_path: Path, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token):
        return
    try:
        init_res = requests.post(f"https://graph.facebook.com/v21.0/{page_id}/video_reels", data={"upload_phase": "start", "access_token": token}).json()
        video_id = init_res["video_id"]
        with open(video_path.resolve(), "rb") as f:
            requests.post(init_res["upload_url"], headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(video_path.stat().st_size)}, data=f)
        requests.post(f"https://graph.facebook.com/v21.0/{page_id}/video_reels", data={"access_token": token, "video_id": video_id, "upload_phase": "finish", "video_state": "PUBLISHED", "description": caption})
    except Exception:
        pass


def upload_to_instagram_reels(video_path: Path, caption: str, idx: int):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()
    if not (ig_user_id and token and repo):
        return
    try:
        staged_path = IG_STAGE_DIR / f"reel_{int(time.time())}_{idx}.mp4"
        shutil.copy2(video_path.resolve(), staged_path)
        subprocess.run(["git", "add", "-f", str(staged_path)], check=True)
        subprocess.run(["git", "commit", "-m", f"temp: stage reel {idx} [skip ci]"], check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True)
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/ig_stage/{staged_path.name}"
        time.sleep(5)

        c_res = requests.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media", data={"media_type": "REELS", "video_url": raw_url, "caption": caption, "access_token": token}).json()
        creation_id = c_res["id"]
        for _ in range(12):
            time.sleep(10)
            s_res = requests.get(f"https://graph.facebook.com/v21.0/{creation_id}", params={"fields": "status_code", "access_token": token}).json()
            if s_res.get("status_code") == "FINISHED":
                break
        requests.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish", data={"creation_id": creation_id, "access_token": token})
    except Exception:
        pass


def main():
    game_key = select_target_game()
    shorts_count = int(os.environ.get("SHORTS_COUNT", "2"))

    vod_path, info = find_and_download_vod(game_key)
    vod_id = info.get("id", "custom_vod")
    vod_title = info.get("title", f"{GAME_CONFIG[game_key]['name']} Match")

    segments, short_windows = get_ai_highlight_segments(vod_path, game_key)
    seo = generate_seo_metadata(vod_title, game_key)

    recap_mp4 = (WORK_DIR / "final_recap_16x9.mp4").resolve()
    thumb_jpg = (WORK_DIR / "custom_thumb.jpg").resolve()
    render_16x9_recap(vod_path, segments, recap_mp4)
    generate_custom_thumbnail(vod_path, 40.0, game_key, seo["match_headline"], thumb_jpg)

    youtube = get_youtube_client()
    playlist_id = ensure_game_playlist(youtube, game_key)
    yt_recap_id = upload_youtube_video(youtube, recap_mp4, seo["yt_title"], seo["yt_description"], thumb_jpg, playlist_id)
    if yt_recap_id:
        post_youtube_link_to_facebook_page(yt_recap_id, seo["yt_title"])

    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    for idx, s_window in enumerate(short_windows[:shorts_count]):
        short_mp4 = (WORK_DIR / f"short_{idx+1}.mp4").resolve()
        render_9x16_short(vod_path, s_window, game_key, seo["match_headline"], whisper_model, short_mp4)
        short_caption = seo["short_caption"]

        upload_youtube_video(youtube, short_mp4, f"{seo['match_headline']} #Shorts", short_caption, None, playlist_id)
        upload_to_facebook_reels(short_mp4, short_caption)
        upload_to_instagram_reels(short_mp4, short_caption, idx + 1)

    mark_vod_processed(vod_id)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(IG_STAGE_DIR, ignore_errors=True)
    print("[+] COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
