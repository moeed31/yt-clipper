import os
import re
import sys
import json
import time
import shutil
import subprocess
import requests
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from faster_whisper import WhisperModel

sys.stdout.reconfigure(line_buffering=True)

PROCESSED_TWITCH_FILE = Path("processed_twitch_clips.txt")
WORK_DIR = Path("work_twitch").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
IG_STAGE_DIR = Path("ig_stage").resolve()
IG_STAGE_DIR.mkdir(parents=True, exist_ok=True)


def load_processed_clips() -> set:
    if not PROCESSED_TWITCH_FILE.exists():
        PROCESSED_TWITCH_FILE.write_text("", encoding="utf-8")
        return set()
    return set(line.strip() for line in PROCESSED_TWITCH_FILE.read_text(encoding="utf-8").splitlines() if line.strip())


def mark_clip_processed(clip_id: str):
    processed = load_processed_clips()
    if clip_id not in processed:
        with open(PROCESSED_TWITCH_FILE, "a", encoding="utf-8") as f:
            f.write(f"{clip_id}\n")


def fetch_trending_twitch_clips(processed: set, limit: int = 2) -> list[dict]:
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    # Robust Fallback List of All Top Gaming Categories on Twitch
    trending_games = [
        "VALORANT", "Grand Theft Auto V", "Fortnite", "League of Legends",
        "Counter-Strike", "Apex Legends", "Minecraft", "Call of Duty: Warzone",
        "Dota 2", "Rocket League", "Tekken 8", "Street Fighter 6"
    ]

    # Attempt to fetch live trending directories dynamically
    trending_query = """
    {
      directories(first: 12, sort: VIEWS_DESC) {
        edges {
          node {
            name
          }
        }
      }
    }
    """
    try:
        r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": trending_query}, timeout=15)
        edges = (((r.json().get("data") or {}).get("directories") or {}).get("edges")) or []
        live_games = [node.get("node", {}).get("name") for node in edges if node.get("node", {}).get("name")]
        if live_games:
            # Combine live trending with fallback to guarantee coverage
            trending_games = live_games + [g for g in trending_games if g not in live_games]
    except Exception as e:
        print(f"[!] Using default gaming categories due to query timeout: {e}", flush=True)

    print(f"[+] Scanning Twitch categories: {', '.join(trending_games[:6])}...", flush=True)

    collected_clips = []
    seen_channels = set()

    for game_name in trending_games:
        if len(collected_clips) >= limit:
            break

        inline_query = f"""
        {{
          game(name: "{game_name}") {{
            clips(first: 15, criteria: {{ period: LAST_DAY }}) {{
              edges {{
                node {{
                  slug
                  title
                  viewCount
                  durationSeconds
                  url
                  broadcaster {{
                    displayName
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        try:
            r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": inline_query}, timeout=20)
            edges = (((r.json().get("data") or {}).get("game") or {}).get("clips") or {}).get("edges") or []
            
            game_clips = []
            for edge in edges:
                node = edge.get("node") or {}
                slug = node.get("slug")
                dur = float(node.get("durationSeconds") or 30.0)
                views = int(node.get("viewCount") or 0)
                streamer = (node.get("broadcaster") or {}).get("displayName") or "Twitch"
                
                if slug and slug not in processed and 12.0 <= dur <= 60.0:
                    game_clips.append({
                        "id": slug,
                        "game": game_name,
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": node.get("title") or f"Insane {game_name} Moment",
                        "streamer": streamer,
                        "views": views,
                        "duration": dur
                    })
            
            game_clips.sort(key=lambda x: x["views"], reverse=True)

            for clip in game_clips:
                ch = clip["streamer"].lower()
                if ch not in seen_channels:
                    seen_channels.add(ch)
                    collected_clips.append(clip)
                    break
        except Exception:
            continue

    return collected_clips[:limit]


def download_clip(clip: dict, index: int) -> Path | None:
    out_mp4 = (WORK_DIR / f"raw_twitch_clip_{index}.mp4").resolve()
    out_mp4.unlink(missing_ok=True)
    cmd = [
        "yt-dlp", "--no-warnings", "--socket-timeout", "30",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4", "-o", str(out_mp4), clip["url"]
    ]
    try:
        proc = subprocess.run(cmd, timeout=180)
        if proc.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 50000:
            return out_mp4
    except Exception as e:
        print(f"[!] Download error on {clip['url']}: {e}", flush=True)
    return None


def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def render_vertical_short(raw_mp4: Path, clip_meta: dict, out_mp4: Path, whisper_model: WhisperModel):
    probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(raw_mp4.resolve())]
    clip_dur = min(58.0, float(subprocess.check_output(probe_cmd, timeout=20).decode().strip()))

    wav_clip = (WORK_DIR / f"audio_{clip_meta['id']}.wav").resolve()
    ass_file = (WORK_DIR / f"subtitles_{clip_meta['id']}.ass").resolve()

    subprocess.run(["ffmpeg", "-y", "-i", str(raw_mp4.resolve()), "-t", str(clip_dur), "-vn", "-ac", "1", "-ar", "16000", str(wav_clip)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)

    segments_gen, _ = whisper_model.transcribe(str(wav_clip), word_timestamps=True)
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TopBanner,DejaVu Sans,50,&H00FFFFFF,&H000000FF,&H00000000,&H969146FF,-1,0,0,0,100,100,0,0,3,16,0,8,40,40,170,1
Style: WordSub,DejaVu Sans,68,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,310,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean_streamer = re.sub(r"[^A-Za-z0-9_]", "", clip_meta["streamer"])[:22]
    clean_title = re.sub(r"[^A-Za-z0-9 !?\-&]", "", clip_meta["title"]).strip()[:34]

    events = [f"Dialogue: 0,0:00:00.00,{format_ass_time(clip_dur)},TopBanner,,0,0,0,,@{clean_streamer.upper()} • {clip_meta['game'].upper()}\\N{clean_title.upper()}"]

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
        "ffmpeg", "-y", "-t", str(clip_dur), "-i", str(raw_mp4.resolve()),
        "-filter_complex", vf_complex, "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", "30",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        str(out_mp4.resolve())
    ], cwd=str(WORK_DIR.resolve()), timeout=180, check=True)
    print(f"[+] Rendered Vertical Short: {out_mp4.name}", flush=True)


def upload_youtube_short(video_path: Path, title: str, description: str):
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        return
    try:
        creds = Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec)
        youtube = build("youtube", "v3", credentials=creds)
        body = {"snippet": {"title": title[:99], "description": description, "categoryId": "20"}, "status": {"privacyStatus": "public"}}
        media = MediaFileUpload(str(video_path.resolve()), chunksize=10 * 1024 * 1024, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        res = None
        while res is None:
            _, res = req.next_chunk()
        print(f"[+] YouTube Short Uploaded: https://youtube.com/shorts/{res['id']}", flush=True)
    except Exception as e:
        print(f"[-] YouTube Error: {e}", flush=True)


def upload_to_facebook_reels(video_path: Path, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token):
        return
    try:
        init_res = requests.post(f"https://graph.facebook.com/v21.0/{page_id}/video_reels", data={"upload_phase": "start", "access_token": token}, timeout=30).json()
        video_id = init_res["video_id"]
        with open(video_path.resolve(), "rb") as f:
            requests.post(init_res["upload_url"], headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(video_path.stat().st_size)}, data=f, timeout=180)
        requests.post(f"https://graph.facebook.com/v21.0/{page_id}/video_reels", data={"access_token": token, "video_id": video_id, "upload_phase": "finish", "video_state": "PUBLISHED", "description": caption}, timeout=60)
        print(f"[+] Facebook Reel Published!", flush=True)
    except Exception as e:
        print(f"[-] FB Error: {e}", flush=True)


def upload_to_instagram_reels(video_path: Path, caption: str, idx: int):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()
    if not (ig_user_id and token and repo):
        return
    try:
        stage_filename = f"twitch_reel_{int(time.time())}_{idx}.mp4"
        staged_path = IG_STAGE_DIR / stage_filename
        shutil.copy2(video_path.resolve(), staged_path)
        subprocess.run(["git", "add", "-f", str(staged_path)], timeout=30, check=True)
        subprocess.run(["git", "commit", "-m", f"temp: stage twitch reel {idx} [skip ci]"], timeout=30, check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], timeout=60, check=True)

        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/ig_stage/{stage_filename}"
        time.sleep(5)

        c_res = requests.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media", data={"media_type": "REELS", "video_url": raw_url, "caption": caption, "share_to_feed": "true", "access_token": token}, timeout=30).json()
        creation_id = c_res["id"]
        for _ in range(20):
            time.sleep(10)
            s_res = requests.get(f"https://graph.facebook.com/v21.0/{creation_id}", params={"fields": "status_code", "access_token": token}, timeout=30).json()
            if s_res.get("status_code") == "FINISHED":
                break
        requests.post(f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish", data={"creation_id": creation_id, "access_token": token}, timeout=30)
        print(f"[+] Instagram Reel Published!", flush=True)
    except Exception as e:
        print(f"[-] IG Error: {e}", flush=True)


def main():
    processed = load_processed_clips()
    print("[*] Fetching top 2 unique trending clips from different channels...", flush=True)
    clips = fetch_trending_twitch_clips(processed, limit=2)

    if not clips:
        print("[!] No new unique trending clips found right now.", flush=True)
        return

    print("[*] Loading faster-whisper model...", flush=True)
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

    for i, clip_meta in enumerate(clips):
        print(f"\n--- Processing Clip {i+1}/2: '{clip_meta['title']}' by {clip_meta['streamer']} ({clip_meta['game']}) ---", flush=True)
        raw_mp4 = download_clip(clip_meta, i)
        if not raw_mp4:
            continue

        final_short = (WORK_DIR / f"short_{i}.mp4").resolve()
        render_vertical_short(raw_mp4, clip_meta, final_short, whisper_model)

        yt_title = f"{clip_meta['title'][:50]} | @{clip_meta['streamer']} ({clip_meta['game']}) #Shorts"
        caption = f"🔥 {clip_meta['title']} (@{clip_meta['streamer']} on Twitch)\n👁️ {clip_meta['views']:,} views!\n\n#Twitch #Gaming #Clips #Shorts #TheRippersGaming"

        upload_youtube_short(final_short, yt_title, caption)
        upload_to_facebook_reels(final_short, caption)
        upload_to_instagram_reels(final_short, caption, i)

        mark_clip_processed(clip_meta["id"])
        time.sleep(15)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(IG_STAGE_DIR, ignore_errors=True)
    print("[+] BATCH OF 2 TRENDING CLIPS PUBLISHED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    main()
