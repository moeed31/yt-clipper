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

from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

sys.stdout.reconfigure(line_buffering=True)

WORK_DIR = Path("work_weekly").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
COMPILATION_RECORD = Path("processed_compilation_clips.txt")
PLAYLIST_CACHE_FILE = Path("playlists.json")


def load_recorded_clips() -> set:
    if not COMPILATION_RECORD.exists():
        COMPILATION_RECORD.write_text("", encoding="utf-8")
        return set()
    return set(line.strip() for line in COMPILATION_RECORD.read_text(encoding="utf-8").splitlines() if line.strip())


def record_clips(clip_ids: list[str]):
    recorded = load_recorded_clips()
    with open(COMPILATION_RECORD, "a", encoding="utf-8") as f:
        for cid in clip_ids:
            if cid not in recorded:
                f.write(f"{cid}\n")


def fetch_weekly_top_clips(limit: int = 8) -> list[dict]:
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
        r = requests.post(
            "https://gql.twitch.tv/gql",
            headers=headers,
            json={"query": "{ directories(first: 12, sort: VIEWS_DESC) { edges { node { name } } } }"},
            timeout=15
        )
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

    # Pick top clips while ensuring creator variety (max 1 clip per streamer first)
    selected = []
    seen_streamers = set()
    for c in candidates:
        ch = c["streamer"].lower()
        if ch not in seen_streamers:
            seen_streamers.add(ch)
            selected.append(c)
            if len(selected) >= limit:
                break

    if len(selected) < limit:
        for c in candidates:
            if c not in selected:
                selected.append(c)
                if len(selected) >= limit:
                    break

    return selected[:limit]


def generate_weekly_seo_and_translations(clips: list[dict]) -> dict:
    """
    Translates any foreign Twitch clip titles into 100% English and generates
    a high-CTR YouTube compilation title, description, and thumbnail shock badge.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    streamers_preview = ", ".join(list(dict.fromkeys(c["streamer"] for c in clips))[:4])

    fallback_titles = [
        re.sub(r"[^A-Za-z0-9 !?\-&]", "", c["title"]).strip() or f"Insane {c['game']} Highlight"
        for c in clips
    ]
    fallback = {
        "english_clip_titles": fallback_titles,
        "thumb_headline": "MOST WATCHED CLIPS!",
        "thumb_shock_tag": "UNREAL MOMENTS!",
        "yt_title": f"Best Twitch Gaming Clips of the Week | ft. {streamers_preview}",
        "yt_description": (
            "🔥 Subscribe to The Rippers Gaming for weekly Twitch clip compilations and daily pro highlights!\n\n"
            "Which clip was the craziest moment of the week? Drop a comment below! 👇\n\n"
            "#Twitch #TwitchClips #GamingCompilation #ViralGaming #Esports #TheRippersGaming"
        )
    }

    if not api_key:
        return fallback

    try:
        client = genai.Client(api_key=api_key)
        payload = [{"i": idx, "title": c["title"], "streamer": c["streamer"], "game": c["game"]} for idx, c in enumerate(clips)]
        prompt = f"""
Below is the list of top Twitch clips for this week's 16:9 YouTube compilation:
{json.dumps(payload, ensure_ascii=False)}

IMPORTANT: Translate ALL non-English clip titles (Korean, Japanese, Spanish, French, Russian, Portuguese, etc.) into natural, hype ENGLISH!

Return ONLY raw valid JSON with keys:
- "english_clip_titles": Array of {len(clips)} strings (clean, punchy English title for each clip in order, max 42 chars each).
- "thumb_headline": 2-to-4 word ALL CAPS headline for the thumbnail (e.g. "TOP VIRAL TWITCH CLIPS", max 26 chars).
- "thumb_shock_tag": 2-to-3 word ALL CAPS yellow shock badge for the thumbnail (e.g. "1,000,000+ VIEWS!", "UNREAL MOMENTS!", max 20 chars).
- "yt_title": High-CTR English YouTube title under 95 chars mentioning top streamers/games and 'Best Twitch Clips of the Week'.
- "yt_description": Engaging 2-paragraph English SEO description asking viewers to comment their favorite clip + trending hashtags.
"""
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        parsed = json.loads(re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip())
        if not isinstance(parsed.get("english_clip_titles"), list) or len(parsed["english_clip_titles"]) < len(clips):
            parsed["english_clip_titles"] = fallback_titles
        return {**fallback, **parsed}
    except Exception as e:
        print(f"[!] Gemini weekly SEO fallback used: {e}", flush=True)
        return fallback


def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def format_yt_timestamp(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def download_and_format_clip_16x9(clip: dict, eng_title: str, index: int, is_last: bool) -> tuple[Path | None, float]:
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
            return None, 0.0

        probe_cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(raw_mp4)
        ]
        dur = float(subprocess.check_output(probe_cmd, timeout=20).decode().strip())

        # Create clean 16:9 ASS overlay with streamer credit, English clip title, and watermark
        ass_file = (WORK_DIR / "clip_overlay.ass").resolve()
        ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CreditCard,DejaVu Sans,34,&H00FFFFFF,&H0000FFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,14,0,1,45,45,45,1
Style: Watermark,DejaVu Sans,26,&H70FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,3,1,9,40,40,35,1
Style: SubscribeCTA,DejaVu Sans,44,&H00FFFFFF,&H0000FFFF,&H00000000,&HD80000FF,-1,0,0,0,100,100,0,0,3,18,0,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        clean_streamer = re.sub(r"[^A-Za-z0-9_]", "", clip["streamer"])[:22] or "TWITCH"
        clean_game = re.sub(r"[^A-Za-z0-9 :\-]", "", clip["game"])[:28]
        clean_eng = re.sub(r"[^A-Za-z0-9 !?\-&]", "", eng_title).strip().upper()[:42] or "EPIC HIGHLIGHT"

        events = [
            f"Dialogue: 1,0:00:00.00,{format_ass_time(dur)},CreditCard,,0,0,0,,🎥 @{clean_streamer.upper()} ({clean_game.upper()})\\N{clean_eng}",
            f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},Watermark,,0,0,0,,@THERIPPERSGAMING"
        ]
        if is_last and dur > 4.0:
            events.append(
                f"Dialogue: 2,{format_ass_time(dur - 4.0)},{format_ass_time(dur)},SubscribeCTA,,0,0,0,,SUBSCRIBE TO THE RIPPERS GAMING FOR WEEKLY COMPILATIONS!"
            )

        ass_file.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8")

        formatted_mp4 = (WORK_DIR / f"formatted_clip_{index}.mp4").resolve()
        vf = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[bg];"
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=clip_overlay.ass,format=yuv420p[vout]"
        )
        subprocess.run([
            "ffmpeg", "-y", "-i", str(raw_mp4), "-filter_complex", vf,
            "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "22", "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(formatted_mp4)
        ], cwd=str(WORK_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
        return formatted_mp4, dur
    except Exception as e:
        print(f"[!] Error preparing compilation clip {index}: {e}", flush=True)
        return None, 0.0


def generate_weekly_thumbnail(top_clip_path: Path, headline: str, shock_tag: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "3", "-i", str(top_clip_path), "-frames:v", "1", str(raw_frame)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    try:
        img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (1280, 720), (15, 15, 25, 255))

    img = ImageEnhance.Color(img).enhance(1.40)
    img = ImageEnhance.Contrast(img).enhance(1.25)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Bottom gradient banner & Neon Purple border
    draw.rectangle([0, 465, 1280, 720], fill=(8, 8, 16, 230))
    draw.rectangle([0, 0, 1280, 720], outline=(168, 85, 247, 255), width=16)

    # Top-Right Yellow Shock Badge
    shock_clean = re.sub(r"[^A-Za-z0-9 !?+]", "", shock_tag).strip().upper()[:20] or "MUST WATCH!"
    draw.rectangle([760, 35, 1245, 115], fill=(255, 215, 0, 255), outline=(0, 0, 0, 255), width=4)

    # Mid-left Tag
    draw.rectangle([40, 440, 440, 495], fill=(168, 85, 247, 255))

    try:
        font_shock = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_tag = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
    except Exception:
        font_shock = ImageFont.load_default()
        font_tag = ImageFont.load_default()
        font_main = ImageFont.load_default()

    draw.text((785, 53), shock_clean, font=font_shock, fill=(10, 10, 10, 255))
    draw.text((55, 452), "BEST OF THE WEEK", font=font_tag, fill=(255, 255, 255, 255))
    draw.text((45, 520), headline.upper()[:28], font=font_main, fill=(255, 255, 255, 255))
    draw.text((45, 605), "TWITCH WEEKLY COMPILATION", font=font_tag, fill=(255, 230, 0, 255))

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    combined.save(out_jpg.resolve(), "JPEG", quality=94)


def ensure_weekly_playlist(youtube) -> str | None:
    if not youtube:
        return None
    cache = {}
    if PLAYLIST_CACHE_FILE.exists():
        try:
            cache = json.loads(PLAYLIST_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    if "weekly_twitch" in cache and cache["weekly_twitch"]:
        return cache["weekly_twitch"]

    title = "Best Twitch Gaming Clips of the Week | Compilations"
    try:
        res = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": "Weekly compilations of the most-watched viral Twitch clips across gaming by The Rippers Gaming."
                },
                "status": {"privacyStatus": "public"}
            }
        ).execute()
        pl_id = res["id"]
        cache["weekly_twitch"] = pl_id
        PLAYLIST_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        return pl_id
    except Exception as e:
        print(f"[!] Weekly playlist creation skipped: {e}", flush=True)
        return None


def post_youtube_link_to_facebook_page(yt_vid_id: str, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token and yt_vid_id):
        return
    try:
        url = f"https://graph.facebook.com/v21.0/{page_id}/feed"
        payload = {
            "message": f"🔥 {caption}\n\nWatch the Full Weekly Twitch Compilation on YouTube: https://www.youtube.com/watch?v={yt_vid_id}",
            "link": f"https://www.youtube.com/watch?v={yt_vid_id}",
            "access_token": token
        }
        res = requests.post(url, data=payload, timeout=30).json()
        print(f"[+] Facebook Page Link Post response: {res}", flush=True)
    except Exception as e:
        print(f"[!] FB Page share error: {e}", flush=True)


def main():
    print("[*] Fetching top Twitch clips for weekly compilation...", flush=True)
    clips = fetch_weekly_top_clips(limit=8)

    if len(clips) < 3:
        print("[!] Not enough new clips found for a weekly compilation yet.", flush=True)
        return

    seo = generate_weekly_seo_and_translations(clips)
    eng_titles = seo["english_clip_titles"]

    formatted_files = []
    clip_ids = []
    chapters = []
    running_time = 0.0

    for i, clip in enumerate(clips):
        eng_t = eng_titles[i] if i < len(eng_titles) else clip["title"]
        print(f"[*] Processing clip {i+1}/{len(clips)}: {eng_t} (@{clip['streamer']})", flush=True)
        is_last = (i == len(clips) - 1)
        f_mp4, dur = download_and_format_clip_16x9(clip, eng_t, i, is_last)
        if f_mp4 and f_mp4.exists() and dur > 0:
            chapters.append(f"{format_yt_timestamp(running_time)} @{clip['streamer']} - {eng_t[:45]} ({clip['game']})")
            running_time += dur
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
    generate_weekly_thumbnail(
        top_clip_path=formatted_files[0],
        headline=seo.get("thumb_headline", "MOST WATCHED CLIPS!"),
        shock_tag=seo.get("thumb_shock_tag", "UNREAL MOMENTS!"),
        out_jpg=thumb_jpg
    )

    chapters_block = "\n".join(chapters)
    full_description = f"{seo['yt_description']}\n\n⏱️ TIMESTAMPS & CREATORS:\n{chapters_block}\n"

    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    if cid and csec and rtok:
        try:
            print("[*] Uploading Weekly Compilation to YouTube...", flush=True)
            creds = Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec)
            youtube = build("youtube", "v3", credentials=creds)

            body = {
                "snippet": {"title": seo["yt_title"][:99], "description": full_description, "categoryId": "20"},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
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
                except Exception as te:
                    print(f"[!] Thumbnail warning: {te}", flush=True)

            pl_id = ensure_weekly_playlist(youtube)
            if pl_id:
                try:
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={"snippet": {"playlistId": pl_id, "resourceId": {"kind": "youtube#video", "videoId": vid_id}}}
                    ).execute()
                    print(f"[+] Added compilation to playlist {pl_id}", flush=True)
                except Exception:
                    pass

            post_youtube_link_to_facebook_page(vid_id, seo["yt_title"])
            record_clips(clip_ids)
        except Exception as e:
            if "uploadLimitExceeded" in str(e):
                print("[!] YouTube Daily Upload Limit Reached! Clips were NOT marked as processed so you can re-run once your quota resets.", flush=True)
            else:
                print(f"[-] YouTube Upload Error: {e}", flush=True)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print("[+] WEEKLY COMPILATION PIPELINE FINISHED!", flush=True)


if __name__ == "__main__":
    main()
