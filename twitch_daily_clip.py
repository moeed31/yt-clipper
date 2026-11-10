import os
import re
import sys
import json
import time
import shutil
import subprocess
import requests
from pathlib import Path

from google import genai
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

    trending_games = [
        "VALORANT", "Grand Theft Auto V", "Fortnite", "League of Legends",
        "Counter-Strike", "Apex Legends", "Minecraft", "Call of Duty: Warzone",
        "Dota 2", "Rocket League", "Tekken 8", "Street Fighter 6"
    ]

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


def translate_and_polish_clip_data(raw_chunks: list[dict], clip_meta: dict) -> tuple[list[dict], str, str, str]:
    game_name = clip_meta["game"]
    streamer = clip_meta["streamer"]
    raw_title = clip_meta["title"]
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    fallback_eng_title = re.sub(r"[^A-Za-z0-9 !?\-&]", "", raw_title).strip() or f"INSANE {game_name.upper()} PLAY!"
    fallback_yt = f"{fallback_eng_title[:45]} | @{streamer} ({game_name}) #Shorts"
    fallback_cap = (
        f"🔥 {fallback_eng_title} (@{streamer} on Twitch)\n"
        f"Rate this {game_name} moment 1-10 in the comments! 👇\n"
        f"Follow @TheRippersGaming for daily gaming clips!\n\n"
        f"#Twitch #Gaming #Clips #Shorts #TheRippersGaming"
    )

    if not api_key:
        return raw_chunks, fallback_eng_title.upper()[:34], fallback_yt, fallback_cap

    try:
        client = genai.Client(api_key=api_key)
        lines_payload = [{"i": idx, "t": c["text"]} for idx, c in enumerate(raw_chunks)]
        prompt = f"""
Twitch Clip Title: '{raw_title}' | Streamer: {streamer} | Game: {game_name}.
Subtitle phrases: {json.dumps(lines_payload, ensure_ascii=False)}

IMPORTANT: Everything MUST be translated and written in 100% natural, viral ENGLISH (even if the streamer/title is Korean, Japanese, Spanish, French, Portuguese, or Russian).

Return ONLY raw valid JSON with keys:
- "english_banner_title": Catchy 3-to-5 word ALL CAPS English banner title for the top of the video (max 32 chars).
- "yt_title": High-CTR English YouTube Short title under 90 chars including @{streamer} and #Shorts.
- "social_caption": Viral Instagram/Facebook Reels caption in English with emojis, a question to bait comments, a reminder to follow @TheRippersGaming, and 5 trending gaming hashtags.
- "subs": Array of objects {{"i": index, "t": "ENGLISH ALL CAPS PHRASE (2-4 words)"}} matching the input subtitle phrases.
"""
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        parsed = json.loads(re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip())

        mapping = {int(item["i"]): str(item["t"]).upper().strip() for item in parsed.get("subs", []) if "i" in item and "t" in item}
        for idx, c in enumerate(raw_chunks):
            if idx in mapping and mapping[idx]:
                c["text"] = mapping[idx]

        eng_banner = str(parsed.get("english_banner_title") or fallback_eng_title).upper()[:34]
        yt_t = str(parsed.get("yt_title") or fallback_yt)
        soc_c = str(parsed.get("social_caption") or fallback_cap)
        return raw_chunks, eng_banner, yt_t, soc_c
    except Exception as e:
        print(f"[!] Gemini translation/caption fallback used: {e}", flush=True)
        return raw_chunks, fallback_eng_title.upper()[:34], fallback_yt, fallback_cap


def build_hormozi_ass_events(raw_chunks: list[dict], clip_dur: float) -> list[str]:
    events = []
    for chunk in raw_chunks:
        s_t = max(0.0, float(chunk["start"]))
        e_t = min(clip_dur, float(chunk["end"]))
        if e_t <= s_t:
            continue
        clean_phrase = re.sub(r"[{}\\]", "", chunk["text"]).strip().upper()
        words = [w for w in clean_phrase.split() if w]
        if not words:
            continue

        total_dur = max(0.15, e_t - s_t)
        word_step = total_dur / len(words)

        for w_idx in range(len(words)):
            w_start = s_t + (w_idx * word_step)
            w_end = s_t + ((w_idx + 1) * word_step) if w_idx < len(words) - 1 else e_t
            formatted_words = []
            for j, word_str in enumerate(words):
                if j == w_idx:
                    formatted_words.append(r"{\c&H0000FFFF&\fscx118\fscy118}" + word_str + r"{\c&H00FFFFFF&\fscx100\fscy100}")
                else:
                    formatted_words.append(word_str)
            events.append(f"Dialogue: 2,{format_ass_time(w_start)},{format_ass_time(w_end)},WordSub,,0,0,0,,{' '.join(formatted_words)}")
    return events


def render_vertical_short(raw_mp4: Path, clip_meta: dict, out_mp4: Path, whisper_model: WhisperModel) -> tuple[str, str]:
    probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(raw_mp4.resolve())]
    clip_dur = min(58.0, float(subprocess.check_output(probe_cmd, timeout=20).decode().strip()))

    wav_clip = (WORK_DIR / "clip_audio.wav").resolve()
    ass_file = (WORK_DIR / "subtitles.ass").resolve()

    subprocess.run(["ffmpeg", "-y", "-i", str(raw_mp4.resolve()), "-t", str(clip_dur), "-vn", "-ac", "1", "-ar", "16000", str(wav_clip)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)

    segments_gen, _ = whisper_model.transcribe(str(wav_clip), task="translate", word_timestamps=True, beam_size=5)

    raw_chunks = []
    for seg in segments_gen:
        if seg.words:
            chunk = []
            for w in seg.words:
                chunk.append(w)
                if len(chunk) == 3:
                    raw_chunks.append({
                        "start": max(0.0, chunk[0].start),
                        "end": min(clip_dur, chunk[-1].end),
                        "text": " ".join(x.word.strip() for x in chunk)
                    })
                    chunk = []
            if chunk:
                raw_chunks.append({
                    "start": max(0.0, chunk[0].start),
                    "end": min(clip_dur, chunk[-1].end),
                    "text": " ".join(x.word.strip() for x in chunk)
                })
        elif seg.text.strip():
            words = seg.text.strip().split()
            seg_dur = max(0.5, seg.end - seg.start)
            num_groups = max(1, (len(words) + 2) // 3)
            step = seg_dur / num_groups
            for g_idx in range(num_groups):
                grp = words[g_idx * 3:(g_idx + 1) * 3]
                raw_chunks.append({
                    "start": max(0.0, seg.start + g_idx * step),
                    "end": min(clip_dur, seg.start + (g_idx + 1) * step),
                    "text": " ".join(grp)
                })

    raw_chunks, eng_banner, yt_title, social_caption = translate_and_polish_clip_data(raw_chunks, clip_meta)

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TopBanner,DejaVu Sans,50,&H00FFFFFF,&H000000FF,&H00000000,&H969146FF,-1,0,0,0,100,100,0,0,3,16,0,8,40,40,170,1
Style: WordSub,DejaVu Sans,70,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,4,2,60,60,330,1
Style: Watermark,DejaVu Sans,30,&H80FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,3,1,2,40,40,185,1
Style: SubscribeCTA,DejaVu Sans,46,&H00FFFFFF,&H0000FFFF,&H00000000,&HD80000FF,-1,0,0,0,100,100,0,0,3,18,0,2,40,40,115,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean_streamer = re.sub(r"[^A-Za-z0-9_]", "", clip_meta["streamer"])[:20] or "TWITCH"
    clean_banner = re.sub(r"[^A-Za-z0-9 !?\-&]", "", eng_banner).strip()[:34]
    cta_start = max(3.0, clip_dur - 3.5)

    events = [
        f"Dialogue: 0,0:00:00.00,{format_ass_time(clip_dur)},TopBanner,,0,0,0,,@{clean_streamer.upper()} • {clip_meta['game'].upper()}\\N{clean_banner}",
        f"Dialogue: 0,0:00:00.00,{format_ass_time(cta_start)},Watermark,,0,0,0,,@THERIPPERSGAMING",
        f"Dialogue: 3,{format_ass_time(cta_start)},{format_ass_time(clip_dur)},SubscribeCTA,,0,0,0,,SUBSCRIBE & FOLLOW @THERIPPERSGAMING\\NFOR DAILY GAMING CLIPS!"
    ]
    events.extend(build_hormozi_ass_events(raw_chunks, clip_dur))

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
    ], cwd=str(WORK_DIR.resolve()), timeout=240, check=True)
    print(f"[+] Rendered 100% English Vertical Short: {out_mp4.name}", flush=True)
    return yt_title, social_caption


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
        if "uploadLimitExceeded" in str(e):
            print("[!] YouTube Daily Upload Limit Reached! Continuing to Facebook & Instagram Reels...", flush=True)
        else:
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
        subprocess.run(["git", "pull", "--rebase", "-X", "ours", "origin", branch], timeout=60, check=False)
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

    print("[*] Loading faster-whisper ('small' multilingual-to-English model)...", flush=True)
    whisper_model = WhisperModel("small", device="cpu", compute_type="int8")

    for i, clip_meta in enumerate(clips):
        print(f"\n--- Processing Clip {i+1}/2: '{clip_meta['title']}' by {clip_meta['streamer']} ({clip_meta['game']}) ---", flush=True)
        raw_mp4 = download_clip(clip_meta, i)
        if not raw_mp4:
            continue

        final_short = (WORK_DIR / f"short_{i}.mp4").resolve()
        yt_title, caption = render_vertical_short(raw_mp4, clip_meta, final_short, whisper_model)

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
