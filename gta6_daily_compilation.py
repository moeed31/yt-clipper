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

WORK_DIR = Path("work_gta6_comp").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
GTA6_COMP_RECORD = Path("processed_gta6_comp_clips.txt")
PLAYLIST_CACHE_FILE = Path("playlists.json")
LOGO_PATH = Path("logo.png").resolve()

TWITCH_GIANTS = {
    "kaicenat", "xqc", "ibai", "caseoh_", "jynxzi", "shroud", "summit1g",
    "lirik", "tarik", "stableronaldo", "plaqueboymax", "agent00", "fanum",
    "dukedennis", "auronplay", "rubius", "elxokas", "illojuan", "thegrefg",
    "spreen", "gaules", "alanzoka", "loud_coringa", "kamet0", "gotaga",
    "hasanabi", "mizkif", "emiru", "esfandtv", "nmplol", "sodapoppin",
    "asmongold", "moistcr1tikal", "ludwig", "cdawgva", "ironmouse",
    "timthetatman", "nickmercs", "cloakzy", "symfuhny", "pokimane",
    "valkyrae", "fuslie", "sykkuno"
}

NON_VIP_MIN_VIEWS = 100000
GTA6_KEYWORDS = re.compile(r"\b(gta\s*6|gta\s*vi|gtavi|gta6|vice\s*city|leonida|lucia|jason|rockstar)\b", re.IGNORECASE)


def load_recorded_clips() -> set:
    if not GTA6_COMP_RECORD.exists():
        GTA6_COMP_RECORD.write_text("", encoding="utf-8")
        return set()
    return set(line.strip() for line in GTA6_COMP_RECORD.read_text(encoding="utf-8").splitlines() if line.strip())


def record_clips(clip_ids: list[str]):
    recorded = load_recorded_clips()
    with open(GTA6_COMP_RECORD, "a", encoding="utf-8") as f:
        for cid in clip_ids:
            if cid not in recorded:
                f.write(f"{cid}\n")


def fetch_daily_gta6_comp_clips(limit: int = 8) -> list[dict]:
    recorded = load_recorded_clips()
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    vip_candidates = []
    verified_viral_candidates = []
    seen_ids = set(recorded)

    print("[*] Scanning Famous Twitch Giants for today's 16:9 GTA 6 compilation...", flush=True)
    for login in TWITCH_GIANTS:
        user_query = f"""
        {{
          user(login: "{login}") {{
            displayName
            clips(first: 20, criteria: {{ period: LAST_DAY }}) {{
              edges {{
                node {{
                  slug
                  title
                  viewCount
                  durationSeconds
                  url
                  game {{
                    name
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        try:
            r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": user_query}, timeout=15)
            user_data = (r.json().get("data") or {}).get("user") or {}
            display_name = user_data.get("displayName") or login
            edges = (user_data.get("clips") or {}).get("edges") or []

            for edge in edges:
                node = edge.get("node") or {}
                slug = node.get("slug")
                if not slug or slug in seen_ids:
                    continue
                dur = float(node.get("durationSeconds") or 30.0)
                if not (10.0 <= dur <= 60.0):
                    continue
                views = int(node.get("viewCount") or 0)
                title = node.get("title") or ""
                game_name = ((node.get("game") or {}).get("name") or "").strip()

                if game_name.lower() == "grand theft auto vi" or bool(GTA6_KEYWORDS.search(title)):
                    seen_ids.add(slug)
                    vip_candidates.append({
                        "id": slug,
                        "game": "Grand Theft Auto VI",
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": title if title else f"{display_name} GTA 6 Highlight",
                        "streamer": display_name,
                        "views": views,
                        "duration": dur
                    })
        except Exception:
            continue

    print("[*] Scanning 'Grand Theft Auto VI' directory for VIPs & Verified 100k+ clips...", flush=True)
    for period in ["LAST_DAY", "LAST_WEEK"]:
        dir_query = f"""
        {{
          game(name: "Grand Theft Auto VI") {{
            clips(first: 45, criteria: {{ period: {period}, sort: VIEWS_DESC }}) {{
              edges {{
                node {{
                  slug
                  title
                  viewCount
                  durationSeconds
                  url
                  broadcaster {{
                    login
                    displayName
                    roles {{
                      isPartner
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        try:
            r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": dir_query}, timeout=20)
            edges = (((r.json().get("data") or {}).get("game") or {}).get("clips") or {}).get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                slug = node.get("slug")
                if not slug or slug in seen_ids:
                    continue
                dur = float(node.get("durationSeconds") or 30.0)
                if not (10.0 <= dur <= 60.0):
                    continue
                views = int(node.get("viewCount") or 0)
                broadcaster = node.get("broadcaster") or {}
                b_login = (broadcaster.get("login") or "").lower().strip()
                streamer = broadcaster.get("displayName") or "Twitch"
                is_partner = bool((broadcaster.get("roles") or {}).get("isPartner"))

                if b_login in TWITCH_GIANTS:
                    seen_ids.add(slug)
                    vip_candidates.append({
                        "id": slug,
                        "game": "Grand Theft Auto VI",
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": node.get("title") or "GTA 6 Highlight",
                        "streamer": streamer,
                        "views": views,
                        "duration": dur
                    })
                elif is_partner and views >= NON_VIP_MIN_VIEWS:
                    seen_ids.add(slug)
                    verified_viral_candidates.append({
                        "id": slug,
                        "game": "Grand Theft Auto VI",
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": node.get("title") or "GTA 6 Viral Highlight",
                        "streamer": streamer,
                        "views": views,
                        "duration": dur
                    })
        except Exception:
            continue

    vip_candidates.sort(key=lambda x: x["views"], reverse=True)
    verified_viral_candidates.sort(key=lambda x: x["views"], reverse=True)
    all_eligible = vip_candidates + verified_viral_candidates

    # Pick unique streamers first, then allow up to 2 clips per giant if needed
    selected = []
    streamer_counts = {}
    for c in all_eligible:
        ch = c["streamer"].lower()
        if streamer_counts.get(ch, 0) < 1:
            streamer_counts[ch] = 1
            selected.append(c)
            if len(selected) >= limit:
                break

    if len(selected) < limit:
        for c in all_eligible:
            if c not in selected:
                ch = c["streamer"].lower()
                if streamer_counts.get(ch, 0) < 2:
                    streamer_counts[ch] = streamer_counts.get(ch, 0) + 1
                    selected.append(c)
                    if len(selected) >= limit:
                        break

    return selected[:limit]


def generate_gta6_comp_seo(clips: list[dict]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    streamers_preview = ", ".join(list(dict.fromkeys(c["streamer"] for c in clips))[:4])

    fallback_titles = [
        re.sub(r"[^A-Za-z0-9 !?\-&]", "", c["title"]).strip() or "Insane GTA 6 Moment"
        for c in clips
    ]
    fallback = {
        "english_clip_titles": fallback_titles,
        "thumb_headline": "STREAMERS REACT TO GTA 6!",
        "thumb_shock_tag": "VICE CITY CHAOS!",
        "yt_title": f"GTA 6 Best Streamer Reactions & Moments! (ft. {streamers_preview})",
        "yt_description": (
            "🚨 Subscribe to The Rippers Gaming for daily GTA 6 (Grand Theft Auto VI) streamer compilations & highlights!\n\n"
            "Who had the funniest reaction in Vice City today? Comment your favorite moment below! 👇\n\n"
            "#GTA6 #GTAVI #GrandTheftAuto6 #ViceCity #TwitchClips #TheRippersGaming"
        )
    }

    if not api_key:
        return fallback

    try:
        client = genai.Client(api_key=api_key)
        payload = [{"i": idx, "title": c["title"], "streamer": c["streamer"]} for idx, c in enumerate(clips)]
        prompt = f"""
Below is the list of Famous Twitch Streamer clips for today's 16:9 YouTube GTA 6 compilation:
{json.dumps(payload, ensure_ascii=False)}

IMPORTANT: Translate ALL non-English clip titles (Spanish, Portuguese, French, etc.) into natural, viral ENGLISH!

Return ONLY raw valid JSON with keys:
- "english_clip_titles": Array of {len(clips)} strings (clean, punchy English title for each clip in order, max 42 chars each).
- "thumb_headline": 2-to-4 word ALL CAPS headline for the thumbnail (e.g. "KAI & XQC IN GTA 6!", "BEST GTA 6 REACTIONS!", max 26 chars).
- "thumb_shock_tag": 2-to-3 word ALL CAPS yellow shock badge for the thumbnail (e.g. "VICE CITY CHAOS!", "UNREAL GRAPHICS!", max 20 chars).
- "yt_title": High-CTR English YouTube title under 95 chars mentioning top streamers ({streamers_preview}) and GTA 6.
- "yt_description": Engaging 2-paragraph English SEO description asking viewers to comment their favorite streamer moment + trending GTA 6 hashtags.
"""
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        parsed = json.loads(re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip())
        if not isinstance(parsed.get("english_clip_titles"), list) or len(parsed["english_clip_titles"]) < len(clips):
            parsed["english_clip_titles"] = fallback_titles
        return {**fallback, **parsed}
    except Exception as e:
        print(f"[!] Gemini GTA 6 compilation SEO fallback used: {e}", flush=True)
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

        ass_file = (WORK_DIR / "clip_overlay.ass").resolve()
        ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CreditCard,DejaVu Sans,34,&H00FFFFFF,&H0000FFFF,&H00000000,&HB49314FF,-1,0,0,0,100,100,0,0,3,14,0,1,45,45,45,1
Style: SubscribeCTA,DejaVu Sans,44,&H00FFFFFF,&H0000FFFF,&H00000000,&HD89314FF,-1,0,0,0,100,100,0,0,3,18,0,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        clean_streamer = re.sub(r"[^A-Za-z0-9_]", "", clip["streamer"])[:22] or "TWITCH"
        clean_eng = re.sub(r"[^A-Za-z0-9 !?\-&]", "", eng_title).strip().upper()[:42] or "GTA 6 HIGHLIGHT"

        events = [
            f"Dialogue: 1,0:00:00.00,{format_ass_time(dur)},CreditCard,,0,0,0,,🌴 @{clean_streamer.upper()} • GTA VI\\N{clean_eng}"
        ]
        if is_last and dur > 4.0:
            events.append(
                f"Dialogue: 2,{format_ass_time(dur - 4.0)},{format_ass_time(dur)},SubscribeCTA,,0,0,0,,SUBSCRIBE TO THE RIPPERS GAMING FOR DAILY GTA 6 HIGHLIGHTS!"
            )

        ass_file.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8")

        formatted_mp4 = (WORK_DIR / f"formatted_clip_{index}.mp4").resolve()

        # Top-Right Logo Overlay on 16:9 video if logo.png exists
        if LOGO_PATH.exists():
            vf = (
                "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[bg];"
                "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=clip_overlay.ass[subbed];"
                "[1:v]scale=165:-1,format=rgba,colorchannelmixer=aa=0.85[logo];"
                "[subbed][logo]overlay=W-w-40:35,format=yuv420p[vout]"
            )
            ff_cmd = [
                "ffmpeg", "-y", "-i", str(raw_mp4), "-i", str(LOGO_PATH),
                "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                str(formatted_mp4)
            ]
        else:
            vf = (
                "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=20:5[bg];"
                "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=clip_overlay.ass,format=yuv420p[vout]"
            )
            ff_cmd = [
                "ffmpeg", "-y", "-i", str(raw_mp4),
                "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                str(formatted_mp4)
            ]

        subprocess.run(ff_cmd, cwd=str(WORK_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
        return formatted_mp4, dur
    except Exception as e:
        print(f"[!] Error preparing compilation clip {index}: {e}", flush=True)
        return None, 0.0


def generate_gta6_thumbnail(top_clip_path: Path, headline: str, shock_tag: str, out_jpg: Path):
    raw_frame = (WORK_DIR / "thumb_raw.jpg").resolve()
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "3", "-i", str(top_clip_path), "-frames:v", "1", str(raw_frame)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    try:
        img = Image.open(raw_frame).convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (1280, 720), (20, 10, 30, 255))

    img = ImageEnhance.Color(img).enhance(1.40)
    img = ImageEnhance.Contrast(img).enhance(1.25)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Vice City Hot Pink / Magenta Neon Accent (255, 20, 147)
    vice_pink = (255, 20, 147, 255)
    draw.rectangle([0, 465, 1280, 720], fill=(10, 6, 18, 232))
    draw.rectangle([0, 0, 1280, 720], outline=vice_pink, width=16)

    # Top-Left Viral Yellow Shock Badge (leaves Top-Right open for your Logo!)
    shock_clean = re.sub(r"[^A-Za-z0-9 !?+]", "", shock_tag).strip().upper()[:20] or "VICE CITY CHAOS!"
    draw.rectangle([35, 35, 535, 115], fill=(255, 215, 0, 255), outline=(0, 0, 0, 255), width=4)

    # Mid-left GTA VI Tag
    draw.rectangle([40, 440, 480, 495], fill=vice_pink)

    try:
        font_shock = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_tag = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
    except Exception:
        font_shock = ImageFont.load_default()
        font_tag = ImageFont.load_default()
        font_main = ImageFont.load_default()

    draw.text((55, 53), shock_clean, font=font_shock, fill=(10, 10, 10, 255))
    draw.text((55, 452), "GRAND THEFT AUTO VI", font=font_tag, fill=(255, 255, 255, 255))
    draw.text((45, 520), headline.upper()[:28], font=font_main, fill=(255, 255, 255, 255))
    draw.text((45, 605), "TWITCH GIANTS HIGHLIGHTS", font=font_tag, fill=(0, 255, 255, 255))

    combined = Image.alpha_composite(img, overlay)

    # Paste your Logo in Top-Right corner of Thumbnail if logo.png exists
    if LOGO_PATH.exists():
        try:
            logo_img = Image.open(LOGO_PATH).convert("RGBA")
            target_w = 170
            ratio = target_w / float(logo_img.width)
            target_h = int(float(logo_img.height) * ratio)
            logo_img = logo_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            combined.paste(logo_img, (1280 - target_w - 35, 35), logo_img)
        except Exception as e:
            print(f"[!] Thumbnail logo overlay note: {e}", flush=True)

    combined.convert("RGB").save(out_jpg.resolve(), "JPEG", quality=94)


def ensure_gta6_playlist(youtube) -> str | None:
    if not youtube:
        return None
    cache = {}
    if PLAYLIST_CACHE_FILE.exists():
        try:
            cache = json.loads(PLAYLIST_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    if "gta6" in cache and cache["gta6"]:
        return cache["gta6"]

    title = "GTA 6 (Grand Theft Auto VI) Best Streamer Clips & Highlights"
    try:
        res = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": "The biggest GTA 6 (Grand Theft Auto VI) streamer reactions, Vice City gameplay moments, and viral Twitch clips by The Rippers Gaming."
                },
                "status": {"privacyStatus": "public"}
            }
        ).execute()
        pl_id = res["id"]
        cache["gta6"] = pl_id
        PLAYLIST_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        return pl_id
    except Exception:
        return None


def post_youtube_link_to_facebook_page(yt_vid_id: str, caption: str):
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not (page_id and token and yt_vid_id):
        return
    try:
        url = f"https://graph.facebook.com/v21.0/{page_id}/feed"
        payload = {
            "message": f"🚨 {caption}\n\nWatch the Full GTA 6 Streamers Compilation on YouTube: https://www.youtube.com/watch?v={yt_vid_id}",
            "link": f"https://www.youtube.com/watch?v={yt_vid_id}",
            "access_token": token
        }
        res = requests.post(url, data=payload, timeout=30).json()
        print(f"[+] Facebook Page Link Post response: {res}", flush=True)
    except Exception as e:
        print(f"[!] FB Page share error: {e}", flush=True)


def main():
    print("[*] Fetching top GTA 6 clips from Famous Twitch Giants for daily 16:9 compilation...", flush=True)
    clips = fetch_daily_gta6_comp_clips(limit=8)

    if len(clips) < 3:
        print("[!] Fewer than 3 new eligible GTA 6 clips found right now; skipping compilation.", flush=True)
        return

    seo = generate_gta6_comp_seo(clips)
    eng_titles = seo["english_clip_titles"]

    formatted_files = []
    clip_ids = []
    chapters = []
    running_time = 0.0

    for i, clip in enumerate(clips):
        eng_t = eng_titles[i] if i < len(eng_titles) else clip["title"]
        print(f"[*] Processing 16:9 clip {i+1}/{len(clips)}: {eng_t} (@{clip['streamer']})", flush=True)
        is_last = (i == len(clips) - 1)
        f_mp4, dur = download_and_format_clip_16x9(clip, eng_t, i, is_last)
        if f_mp4 and f_mp4.exists() and dur > 0:
            chapters.append(f"{format_yt_timestamp(running_time)} @{clip['streamer']} - {eng_t[:45]}")
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

    master_mp4 = (WORK_DIR / "gta6_daily_compilation.mp4").resolve()
    print("[*] Stitching master 16:9 GTA 6 daily compilation video...", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(master_mp4)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    thumb_jpg = (WORK_DIR / "gta6_thumb.jpg").resolve()
    generate_gta6_thumbnail(
        top_clip_path=formatted_files[0],
        headline=seo.get("thumb_headline", "STREAMERS REACT TO GTA 6!"),
        shock_tag=seo.get("thumb_shock_tag", "VICE CITY CHAOS!"),
        out_jpg=thumb_jpg
    )

    chapters_block = "\n".join(chapters)
    full_description = f"{seo['yt_description']}\n\n⏱️ TIMESTAMPS & STREAMERS:\n{chapters_block}\n"

    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    if cid and csec and rtok:
        try:
            print("[*] Uploading GTA 6 16:9 Compilation to YouTube...", flush=True)
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
            print(f"[+] GTA 6 Compilation Uploaded: https://youtu.be/{vid_id}", flush=True)

            if thumb_jpg.exists():
                try:
                    youtube.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(str(thumb_jpg))).execute()
                    print("[+] Custom GTA 6 thumbnail with logo applied!", flush=True)
                except Exception as te:
                    print(f"[!] Thumbnail warning: {te}", flush=True)

            pl_id = ensure_gta6_playlist(youtube)
            if pl_id:
                try:
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={"snippet": {"playlistId": pl_id, "resourceId": {"kind": "youtube#video", "videoId": vid_id}}}
                    ).execute()
                except Exception:
                    pass

            post_youtube_link_to_facebook_page(vid_id, seo["yt_title"])
            record_clips(clip_ids)
        except Exception as e:
            if "uploadLimitExceeded" in str(e):
                print("[!] YouTube Daily Upload Limit Reached! Clips were NOT marked as processed so you can re-run later.", flush=True)
            else:
                print(f"[-] YouTube Upload Error: {e}", flush=True)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    print("[+] GTA 6 DAILY 16:9 COMPILATION PIPELINE FINISHED!", flush=True)


if __name__ == "__main__":
    main()
