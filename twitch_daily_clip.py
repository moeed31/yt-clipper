import os
import re
import sys
import json
import time
import shutil
import subprocess
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from faster_whisper import WhisperModel

sys.stdout.reconfigure(line_buffering=True)

# ==============================================================================
# 1. 9-GAME TWITCH DIRECTORY MAPPING
# ==============================================================================
TWITCH_GAMES = {
    "valorant": {"name": "VALORANT", "twitch_id": "516575", "hashtags": "#Valorant #TwitchClips #ValorantClips #Gaming #Shorts"},
    "apex": {"name": "Apex Legends", "twitch_id": "511224", "hashtags": "#ApexLegends #TwitchClips #ApexClips #Gaming #Shorts"},
    "cs2": {"name": "Counter-Strike", "twitch_id": "32399", "hashtags": "#CS2 #CounterStrike2 #TwitchClips #CS2Clips #Shorts"},
    "pubg": {"name": "PUBG: BATTLEGROUNDS", "twitch_id": "493057", "hashtags": "#PUBG #TwitchClips #PUBGClips #BattleRoyale #Shorts"},
    "fortnite": {"name": "Fortnite", "twitch_id": "33214", "hashtags": "#Fortnite #TwitchClips #FortniteClips #Gaming #Shorts"},
    "r6": {"name": "Tom Clancy's Rainbow Six Siege", "twitch_id": "460630", "hashtags": "#R6Siege #RainbowSixSiege #TwitchClips #FPS #Shorts"},
    "cod": {"name": "Call of Duty: Warzone", "twitch_id": "512710", "hashtags": "#CallOfDuty #Warzone #CODClips #TwitchClips #Shorts"},
    "tekken": {"name": "TEKKEN 8", "twitch_id": "1961912896", "hashtags": "#Tekken8 #FGC #TwitchClips #FightingGames #Shorts"},
    "lol": {"name": "League of Legends", "twitch_id": "21779", "hashtags": "#LeagueOfLegends #LoLClips #TwitchClips #Gaming #Shorts"}
}

PROCESSED_TWITCH_FILE = Path("processed_twitch_clips.txt")
PLAYLIST_CACHE_FILE = Path("playlists.json")
WORK_DIR = Path("work_twitch").resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
IG_STAGE_DIR = Path("ig_stage").resolve()
IG_STAGE_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 2. STATE TRACKING & DAILY GAME ROTATION
# ==============================================================================
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


def select_daily_game() -> str:
    target = os.environ.get("TARGET_GAME", "auto").strip().lower()
    if target in TWITCH_GAMES:
        return target
    processed_count = len(load_processed_clips())
    keys = list(TWITCH_GAMES.keys())
    selected = keys[processed_count % len(keys)]
    print(f"[*] Daily Twitch Game Selected: {TWITCH_GAMES[selected]['name']} ({selected})", flush=True)
    return selected


# ==============================================================================
# 3. FETCH MOST-WATCHED 24H TWITCH CLIPS (HELIX API + ZERO-KEY GQL FALLBACK)
# ==============================================================================
def fetch_top_clips_helix(game_key: str, processed: set) -> list[dict]:
    """Optional Official Twitch Helix API if TWITCH_CLIENT_ID & SECRET are set."""
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    csec = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        return []

    try:
        tok_res = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"},
            timeout=20
        ).json()
        access_token = tok_res.get("access_token")
        if not access_token:
            return []

        started_at = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {"Client-ID": cid, "Authorization": f"Bearer {access_token}"}
        params = {
            "game_id": TWITCH_GAMES[game_key]["twitch_id"],
            "first": 25,
            "started_at": started_at
        }
        res = requests.get("https://api.twitch.tv/helix/clips", headers=headers, params=params, timeout=20).json()
        clips = []
        for item in res.get("data", []):
            slug = item.get("id")
            dur = float(item.get("duration") or 30.0)
            if slug and slug not in processed and 12.0 <= dur <= 60.0:
                clips.append({
                    "id": slug,
                    "url": item.get("url") or f"https://clips.twitch.tv/{slug}",
                    "title": item.get("title") or "Insane Twitch Highlight",
                    "streamer": item.get("broadcaster_name") or "ProStreamer",
                    "views": int(item.get("view_count") or 0),
                    "duration": dur
                })
        clips.sort(key=lambda x: x["views"], reverse=True)
        return clips
    except Exception as e:
        print(f"[!] Twitch Helix API check skipped: {e}", flush=True)
        return []


def fetch_top_clips_gql_keyless(game_key: str, processed: set) -> list[dict]:
    """
    Zero-API-Key Method: Queries Twitch's public GraphQL endpoint for the Top Viewed
    24-hour clips (falls back to 7-day top clips if a game had a quiet 24h window).
    """
    game_name = TWITCH_GAMES[game_key]["name"]
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    for criteria_filter in ["LAST_DAY", "LAST_WEEK"]:
        query = """
        query($gameName: String!, $filter: MatchesSort!) {
          game(name: $gameName) {
            clips(first: 25, criteria: { period: $filter, sort: VIEWS_DESC }) {
              edges {
                node {
                  slug
                  title
                  viewCount
                  durationSeconds
                  url
                  broadcaster {
                    displayName
                  }
                }
              }
            }
          }
        }
        """
        # Standard inline GQL query for game clips by period
        inline_query = f"""
        {{
          game(name: "{game_name}") {{
            clips(first: 25, criteria: {{ period: {criteria_filter} }}) {{
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
            r = requests.post("https://gql.twitch.tv/gql", headers=headers, json={"query": inline_query}, timeout=25)
            data = r.json()
            edges = (((data.get("data") or {}).get("game") or {}).get("clips") or {}).get("edges") or []
            clips = []
            for edge in edges:
                node = edge.get("node") or {}
                slug = node.get("slug")
                dur = float(node.get("durationSeconds") or 30.0)
                views = int(node.get("viewCount") or 0)
                broadcaster = (node.get("broadcaster") or {}).get("displayName") or "Twitch"
                if slug and slug not in processed and 12.0 <= dur <= 60.0:
                    clips.append({
                        "id": slug,
                        "url": node.get("url") or f"https://clips.twitch.tv/{slug}",
                        "title": node.get("title") or f"Insane {game_name} Moment",
                        "streamer": broadcaster,
                        "views": views,
                        "duration": dur
                    })
            if clips:
                clips.sort(key=lambda x: x["views"], reverse=True)
                print(f"[+] Found {len(clips)} trending Twitch clips ({criteria_filter}) for {game_name}!", flush=True)
                return clips
        except Exception as e:
            print(f"[!] Twitch GQL ({criteria_filter}) warning: {e}", flush=True)

    return []


def find_and_download_top_twitch_clip(initial_game_key: str) -> tuple[Path, dict, str]:
    processed = load_processed_clips()
    all_keys = list(TWITCH_GAMES.keys())
    start_idx = all_keys.index(initial_game_key) if initial_game_key in all_keys else 0
    ordered_games = all_keys[start_idx:] + all_keys[:start_idx]

    for game_key in ordered_games:
        clips = fetch_top_clips_helix(game_key, processed)
        if not clips:
            clips = fetch_top_clips_gql_keyless(game_key, processed)

        if not clips:
            print(f"[!] No unprocessed clips found for {TWITCH_GAMES[game_key]['name']}, checking next game...", flush=True)
            continue

        for clip in clips[:4]:
            print(f"[*] Downloading #1 Trending Clip: '{clip['title']}' by {clip['streamer']} ({clip['views']:,} views)", flush=True)
            out_mp4 = (WORK_DIR / "raw_twitch_clip.mp4").resolve()
            out_mp4.unlink(missing_ok=True)

            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--socket-timeout", "30",
                "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", str(out_mp4),
                clip["url"]
            ]
            try:
                proc = subprocess.run(cmd, timeout=180)
                if proc.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 50000:
                    return out_mp4, clip, game_key
            except Exception as e:
                print(f"[!] Download error on {clip['url']}: {e}", flush=True)

    raise RuntimeError("Could not download any trending Twitch clips across games.")


# ==============================================================================
# 4. RENDER 9:16 VERTICAL SHORT WITH STREAMER BANNER & WORD-BY-WORD SUBTITLES
# ==============================================================================
def format_ass_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def render_vertical_twitch_short(raw_mp4: Path, clip_meta: dict, game_key: str, out_mp4: Path):
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(raw_mp4.resolve())
    ]
    clip_dur = min(58.0, float(subprocess.check_output(probe_cmd, timeout=20).decode().strip()))

    wav_clip = (WORK_DIR / "clip_audio.wav").resolve()
    ass_file = (WORK_DIR / "subtitles.ass").resolve()

    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_mp4.resolve()),
        "-t", str(clip_dur), "-vn", "-ac", "1", "-ar", "16000", str(wav_clip)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)

    print("[*] Transcribing Twitch Clip audio with faster-whisper...", flush=True)
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
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

    events = [
        f"Dialogue: 0,0:00:00.00,{format_ass_time(clip_dur)},TopBanner,,0,0,0,,@{clean_streamer.upper()} • {TWITCH_GAMES[game_key]['name'].upper()}\\N{clean_title.upper()}"
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
        "ffmpeg", "-y", "-t", str(clip_dur), "-i", str(raw_mp4.resolve()),
        "-filter_complex", vf_complex,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        str(out_mp4.resolve())
    ], cwd=str(WORK_DIR.resolve()), timeout=180, check=True)
    print(f"[+] Rendered 9:16 Vertical Twitch Short: {out_mp4.name}", flush=True)


# ==============================================================================
# 5. VIRAL CAPTION GENERATION & SOCIAL UPLOADERS (YT SHORTS, FB REELS, IG REELS)
# ==============================================================================
def build_viral_captions(clip_meta: dict, game_key: str) -> tuple[str, str]:
    game_name = TWITCH_GAMES[game_key]["name"]
    hashtags = TWITCH_GAMES[game_key]["hashtags"]
    streamer = clip_meta["streamer"]
    raw_title = clip_meta["title"]

    yt_title = f"{raw_title[:55]} | @{streamer} ({game_name}) #Shorts"
    social_caption = (
        f"🔥 {raw_title} (@{streamer} on Twitch)\n"
        f"👁️ {clip_meta['views']:,} views in 24h!\n\n"
        f"{hashtags} #TheRippersGaming"
    )
    return yt_title[:99], social_caption


def upload_youtube_short(video_path: Path, title: str, description: str):
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    csec = os.environ.get("YT_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        print("[!] Skipping YouTube Short: Missing YT credentials", flush=True)
        return
    try:
        creds = Credentials(token=None, refresh_token=rtok, token_uri="https://oauth2.googleapis.com/token", client_id=cid, client_secret=csec)
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {"title": title[:99], "description": description, "categoryId": "20"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
        }
        media = MediaFileUpload(str(video_path.resolve()), chunksize=10 * 1024 * 1024, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = req.next_chunk()
        print(f"[+] Uploaded Twitch Short to YouTube: https://youtube.com/shorts/{response['id']}", flush=True)
    except Exception as e:
        print(f"[-] YouTube Short Upload Error: {e}", flush=True)


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
        with open(video_path.resolve(), "rb") as f:
            requests.post(
                init_res["upload_url"],
                headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(video_path.stat().st_size)},
                data=f,
                timeout=180
            )
        pub_res = requests.post(
            init_url,
            data={"access_token": token, "video_id": video_id, "upload_phase": "finish", "video_state": "PUBLISHED", "description": caption},
            timeout=60
        ).json()
        print(f"[+] Facebook Reel Published: {pub_res}", flush=True)
    except Exception as e:
        print(f"[-] FB Reel Error: {e}", flush=True)


def upload_to_instagram_reels(video_path: Path, caption: str):
    ig_user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip()
    if not (ig_user_id and token and repo):
        return

    try:
        stage_filename = f"twitch_reel_{int(time.time())}.mp4"
        staged_path = IG_STAGE_DIR / stage_filename
        shutil.copy2(video_path.resolve(), staged_path)

        subprocess.run(["git", "add", "-f", str(staged_path)], timeout=30, check=True)
        subprocess.run(["git", "commit", "-m", "temp: stage daily twitch reel for IG [skip ci]"], timeout=30, check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], timeout=60, check=True)

        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/ig_stage/{stage_filename}"
        time.sleep(5)

        c_res = requests.post(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media",
            data={"media_type": "REELS", "video_url": raw_url, "caption": caption, "share_to_feed": "true", "access_token": token},
            timeout=30
        ).json()
        if "id" not in c_res:
            print(f"[-] IG Container Error: {c_res}", flush=True)
            return

        creation_id = c_res["id"]
        for attempt in range(20):
            time.sleep(10)
            s_res = requests.get(f"https://graph.facebook.com/v21.0/{creation_id}", params={"fields": "status_code,status", "access_token": token}, timeout=30).json()
            code = s_res.get("status_code", "")
            print(f"    [IG Poll {attempt+1}/20] status_code = {code}", flush=True)
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                return
        else:
            return

        pub_res = requests.post(
            f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=30
        ).json()
        print(f"[+] SUCCESS! Instagram Reel Published: {pub_res}", flush=True)
    except Exception as e:
        print(f"[-] Instagram Upload Exception: {e}", flush=True)


def main():
    initial_game = select_daily_game()
    raw_mp4, clip_meta, game_key = find_and_download_top_twitch_clip(initial_game)

    final_short = (WORK_DIR / "daily_twitch_short.mp4").resolve()
    render_vertical_twitch_short(raw_mp4, clip_meta, game_key, final_short)

    yt_title, social_caption = build_viral_captions(clip_meta, game_key)

    upload_youtube_short(final_short, yt_title, social_caption)
    upload_to_facebook_reels(final_short, social_caption)
    upload_to_instagram_reels(final_short) if False else upload_to_instagram_reels(final_short, social_caption)

    mark_clip_processed(clip_meta["id"])
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(IG_STAGE_DIR, ignore_errors=True)
    print("[+] DAILY #1 TWITCH CLIP PUBLISHED ACROSS YOUTUBE, FACEBOOK & INSTAGRAM!", flush=True)


if __name__ == "__main__":
    main()
