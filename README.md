<div align="center">
⚡ YT-Clipper & Multi-Platform Esports Automator
An end-to-end, fully automated headless pipeline designed to scrape, clip, render, optimize, and distribute professional esports tournaments and trending gaming content across YouTube, Facebook Pages, Facebook Reels, and Instagram Reels.
</div>
🚀 Core Features
 * 🏆 Multi-Game Tournament Automator: Automatically searches, downloads, and processes pro matches across 9 major esports titles with smart VOD duration management and multi-query search fallback.
 * 🔥 Twice-Daily Trending Twitch Clips: Scans live trending Twitch categories twice a day, extracts the top-viewed clips from unique creators, burns in word-by-word kinetic subtitles via faster-whisper, and pushes them instantly.
 * 📼 Weekly Compilation Engine: Automatically aggregates the top-performing weekly Twitch clips, formats them into a widescreen 16:9 master compilation with custom thumbnails, and publishes them to YouTube.
 * 🧠 Gemini AI Vision & SEO Engine: Leverages Google Gemini models to intelligently detect high-impact match moments, full rounds, or final circles, while auto-generating high-CTR titles, descriptions, and viral short captions.
 * 🎨 Dynamic Video Processing: Automatically handles 16:9 widescreen full match recaps with custom generated branded thumbnails, and 9:16 vertical short-form content with custom top banners and blur-fill padding.
 * 🔄 Zero-Server Infrastructure: Runs entirely on GitHub Actions using cron schedules and workflow dispatches with automated state persistence (processed_vods.txt, processed_twitch_clips.txt).
🎮 Supported Games & Categories
| Game | Format Type | Accent Color | Target Platforms |
|---|---|---|---|
| VALORANT | Tactical FPS | #FF4655 | YouTube, FB, IG |
| Apex Legends | Battle Royale | #DC2626 | YouTube, FB, IG |
| Counter-Strike 2 | Tactical FPS | #F59E0B | YouTube, FB, IG |
| PUBG Battlegrounds | Battle Royale | #EAB308 | YouTube, FB, IG |
| Fortnite | Battle Royale | #38BDF8 | YouTube, FB, IG |
| Rainbow Six Siege | Tactical FPS | #3B82F6 | YouTube, FB, IG |
| Call of Duty | Tactical FPS | #22C55E | YouTube, FB, IG |
| Tekken 8 | Fighting Game | #A855F7 | YouTube, FB, IG |
| League of Legends | MOBA | #0EA5E9 | YouTube, FB, IG |
⚙️ Tech Stack & Dependencies
 * Core Language: Python 3.11+
 * Video/Audio Processing: FFmpeg, yt-dlp (with PO-Token provider support)
 * AI & Transcription: Google GenAI SDK (google-genai), faster-whisper (CPU int8 optimization)
 * Image Generation: Pillow (PIL)
 * APIs: YouTube Data API v3, Facebook Graph API, Instagram Graph API (via GitHub Raw CDN staging)
🛠️ Installation & Setup
1. Clone the Repository
git clone https://github.com/moeed31/yt-clipper.git
cd yt-clipper

2. Configure GitHub Secrets
To allow the workflows to authenticate with your social channels and AI providers, add the following secrets in your repository settings (Settings \rightarrow Secrets and variables \rightarrow Actions):
| Secret Name | Description |
|---|---|
| GEMINI_API_KEY | Google Gemini API Key from AI Studio |
| YT_CLIENT_ID | Google Cloud Console OAuth Client ID |
| YT_CLIENT_SECRET | Google Cloud Console OAuth Client Secret |
| YT_REFRESH_TOKEN | Permanent Google OAuth Refresh Token with YouTube scope |
| FB_PAGE_ID | Facebook Page ID for cross-posting and reels |
| FB_PAGE_ACCESS_TOKEN | Facebook Long-Lived Page Access Token |
| IG_USER_ID | Instagram Business Account ID linked to your FB Page |
| YT_COOKIES (Optional) | Netscape-formatted cookies.txt content for robust YouTube authentication |
⏰ Workflow Automation Schedules
| Workflow File | Schedule (UTC) | Local Prime-Time (PKT) | Purpose |
|---|---|---|---|
| youtube_automator.yml | Every 6 hours (0 */6 * * *) | Every 6 hours | Scrapes tournament VODs, builds playlists, posts recaps & shorts. |
| twitch_daily.yml | Twice daily (0 11,17 * * *) | 4:00 PM & 10:00 PM | Fetches top 2 unique trending Twitch clips and multi-posts. |
| twitch_weekly.yml | Every Sunday (0 12 * * SUN) | Sunday 5:00 PM | Stitches weekly top clips into a 16:9 compilation video. |
🕹️ Manual Triggers
All pipelines support Workflow Dispatch. You can trigger any workflow manually from the Actions tab on GitHub:
 * Tournament Pipeline: Choose between specific games (valorant, cs2, etc.), run in auto rotation, provide an optional custom VOD URL, or set custom short counts.
 * Twitch Automator: Select a game category or leave it on auto to scan all trending directories.
📄 License
Distributed under the MIT License. Feel free to fork, adapt, and build your own automated media pipelines.
