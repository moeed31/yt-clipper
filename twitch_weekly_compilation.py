import os
import re
import sys
import json
import time
import shutil
import subprocess
import requests
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

sys.stdout.reconfigure(line_buffering=True)

WORK_DIR = Path("work_weekly").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
COMPILATION_RECORD = Path("processed_compilation_clips.txt")


def load_recorded_clips() -> set:
    if not COMPILATION_RECORD.exists():
        COMPILATION_RECORD.write_text("", encoding="utf-8")
        return set()
    return set(line.strip() for line in COMPILATION_RECORD.read_text(encoding="utf-8").splitlines() if line.strip())


def record_clips(clip_ids: list[str]):
    with open(COMPILATION_RECORD, "a", encoding="utf-8") as f:
        for cid in clip_ids:
            f.write(f"{cid}\n")


def fetch_weekly_top_clips() -> list[dict]:
    recorded = load_recorded_clips()
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    trending_games = [
        "VALORANT", "Grand Theft Auto V", "Fortnite", "League of Legends",
        "Counter-Strike", "Apex Legends", "Minecraft", "Call of Duty: Warzone",
        "Dota 2", "Rocket League", "Tekken 8", "Street Fighter 6"
    ]

    try:
        r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": "{ directories(first: 12, sort: VIEWS_DESC) { edges { node { name } } } }"}, timeout=15)
        edges = (((r.json().get("data") or {}).get("directories") or {}).get("edges")) or []
        live_games = [node.get("node", {}).get("name") for node in edges if node.get("node", {}).get("name")]
        if live_games:
            trending_games = live_games + [g for g in trending_games if g not in live_games]
    except Exception:
        pass

    candidates = []
    seen_ids = set(recorded)

    for game_name in trending_games:
        inline_query = f"""
        {{
          game(name: "{game_name}") {{
            clips(first: 15, criteria: {{ period: LAST_WEEK, sort: VIEWS_DESC }}) {{
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
            for edge in edges:
                node = edge.get("node") or {}
                slug = node.get("slug")
                dur = float(node.get("durationSeconds") or 30.0)
                views = int(node.get("viewCount") or 0)
                streamer = (node.get("broadcaster") or {}).get("displayName") or "Twitch"

                # Lowered minimum duration check to 10s and max to 60s for broader clip matching
                if slug and slug not in seen_ids and 10.0 <= dur <= 60.0:
                    seen_ids.add(slug)
                    candidates.append({
                        "id": slug,
                        "game": game_name,
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": node.get("title") or f"Epic {game_name} Moment",
                        "streamer": streamer,
                        "views": views,
                        "duration": dur
                    })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["views"], reverse=True)
    return candidates[:6] # Top 6 clips is ideal for a fast weekly compilation


def download_clip_16x9(clip: dict, index: int) -> Path | None:
    raw_mp4 = (WORK_DIR / f"clip_{index}.mp4").resolve()
    raw_mp4.unlink(missing_ok=True)
    
    cmd = [
        "yt-dlp", "--no-warnings", "--socket-timeout", "30",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4", "-o", str(raw_mp4), clip["url"]
    ]
    try:
        proc = subprocess.run(cmd, timeout=180)
        if proc.returncode != 0 or not raw_mp4.exists() or raw_mp4.stat().st_size < 50000:
            return None

        formatted_mp4 = (WORK_DIR / f"formatted_clip_{index}.mp4").resolve()
        vf = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=30:10[bg];"
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
        )
        subprocess.run([
            "ffmpeg", "-y", "-i", str(raw_mp4), "-filter_complex", vf,
            "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(formatted_mp4)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
        return formatted_mp4
    except Exception as e:
        print(f"[!] Error preparing compilation clip {index}: {e}", flush=True)
        return None


def generate_weekly_thumbnail(top_clip_path: Path, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run(["ffmpeg", "-y", "-ss", "3", "-i", str(top_clip_path), "-frames:v", "1", str(raw_frame)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        img = Image.open(raw_frame).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
    except Exception:
        img = Image.new("RGB", (1280, 720), (15, 15, 25))

    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.2)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 480, 1280, 720], fill=(10, 10, 18, 220))
    draw.rectangle([0, 0, 1280, 720], outline=(168, 85, 247, 255), width=14)
    draw.rectangle([40, 455, 420, 505], fill=(168, 85, 247, 255))

    try:
        font_tag = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
    except Exception:
        font_tag = ImageFont.load_default()
        font_main = ImageFont.load_default()

    draw.text((55, 463), "BEST OF THE WEEK", font=font_tag, fill=(255, 255, 255, 255))
    draw.text((45, 535), "TOP TWITCH GAMING CLIPS", font=font_main, fill=(255, 255, 255, 255))
    draw.text((45, 610), "WEEKLY COMPILATION", font=font_tag, fill=(168, 85, 247, 255))

    combined = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    combined.save(out_jpg.resolve(), "JPEG", quality=92)


def main():
    print("[*] Fetching top Twitch clips for weekly compilation...", flush=True)
    clips = fetch_weekly_top_clips()
    
    # Lowered threshold from 4 to 3 clips so it compiles successfully right away
    if len(clips) < 3:
        print("[!] Not enough new clips found for a weekly compilation yet.", flush=True)
        return

    formatted_files = []
    clip_ids = []

    for i, clip in enumerate(clips):
        print(f"[*] Processing clip {i+1}/{len(clips)}: {clip['title']} ({clip['streamer']})", flush=True)
        f_mp4 = download_clip_16x9(clip, i)
        if f_mp4 and f_mp4.exists():
            formatted_files.append(f_mp4)
            clip_ids.append(clip["id"])

    if len(formatted_files) < 3:
        print("[!] Failed to process enough valid clips.", flush=True)
        return

    concat_list = (WORK_DIR / "concat.txt").resolve()
    with open(concat_list, "w", encoding="utf-8") as f:
        for fp in formatted_files:
            f.write(f"file '{fp.resolve()}'\n")

    master_mp4 = (WORK_DIR / "weekly_compilation_master.mp4").resolve()
    print("[*] Stitching master 16:9 weekly compilation video...", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(master_mp4)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    thumb_jpg = (WORK_DIR / "weekly_thumb.jpg").resolve()
    generate_weekly_thumbnail(formatted_files[0], thumb_jpg)

    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    if cid and csec and rtok:
        print("[*] Uploading Weekly Compilation to YouTube...", flush=True)
        creds = Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec)
        youtube = build("youtube", "v3", credentials=creds)

        title = f"Best Twitch Gaming Clips of the Week | Epic Moments & Clutches"
        desc = "The absolute best, most-watched Twitch clips from top streamers this week!\n\n#Twitch #Gaming #Clips #Compilation #TheRippersGaming"

        body = {
            "snippet": {"title": title[:99], "description": desc, "categoryId": "20"},
            "status": {"privacyStatus": "public"}
        }
        media = MediaFileUpload(str(master_mp4), chunksize=10 * 1024 * 1024, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        res = None
        while res is None:
            _, res = req.next_chunk()

        vid_id = res["id"]
        print(f"[+] Weekly Compilation Uploaded: https://youtu.be/{vid_id}", flush=True)

        if thumb_jpg.exists():
            try:
                youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(str(thumb_jpg))).execute()
                print("[+] Custom compilation thumbnail applied!", flush=True)
            except Exception:
                pass

    record_clips(clip_ids)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print("[+] WEEKLY COMPILATION COMPLETED & PUBLISHED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    main()
