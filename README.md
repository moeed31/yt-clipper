<div align="center">

# ⚡ YT-Clipper & Multi-Platform Esports Automator

[![GitHub Actions Status](https://img.shields.io/github/actions/workflow/status/moeed31/yt-clipper/youtube_automator.yml?branch=main&style=flat-square&logo=github-actions&logoColor=white&label=Tournament%20Pipeline)](https://github.com/moeed31/yt-clipper/actions)
[![Twitch Clips Pipeline](https://img.shields.io/github/actions/workflow/status/moeed31/yt-clipper/twitch_daily.yml?branch=main&style=flat-square&logo=twitch&logoColor=white&label=Twitch%20Automator)](https://github.com/moeed31/yt-clipper/actions)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)

An end-to-end, fully automated headless pipeline designed to scrape, clip, render, optimize, and distribute professional esports tournaments and trending gaming content across **YouTube, Facebook Pages, Facebook Reels, and Instagram Reels**.

</div>

---

## 🚀 Core Features

* **🏆 Multi-Game Tournament Automator:** Automatically searches, downloads, and processes pro matches across **9 major esports titles** with smart VOD duration management and multi-query search fallback.
* **🔥 Twice-Daily Trending Twitch Clips:** Scans live trending Twitch categories twice a day, extracts the top-viewed clips from **unique creators**, burns in word-by-word kinetic subtitles via `faster-whisper`, and pushes them instantly.
* **📼 Weekly Compilation Engine:** Automatically aggregates the top-performing weekly Twitch clips, formats them into a widescreen 16:9 master compilation with custom thumbnails, and publishes them to YouTube.
* **🧠 Gemini AI Vision & SEO Engine:** Leverages Google Gemini models to intelligently detect high-impact match moments, full rounds, or final circles, while auto-generating high-CTR titles, descriptions, and viral short captions.
* **🎨 Dynamic Video Processing:** Automatically handles 16:9 widescreen full match recaps with custom generated branded thumbnails, and 9:16 vertical short-form content with custom top banners and blur-fill padding.
* **🔄 Zero-Server Infrastructure:** Runs entirely on **GitHub Actions** using cron schedules and workflow dispatches with automated state persistence (`processed_vods.txt`, `processed_twitch_clips.txt`).

---

## 🎮 Supported Games & Categories

| Game | Format Type | Accent Color | Target Platforms |
| :--- | :--- | :--- | :--- |
| **VALORANT** | Tactical FPS | `#FF4655` | YouTube, FB, IG |
| **Apex Legends** | Battle Royale | `#DC2626` | YouTube, FB, IG |
| **Counter-Strike 2** | Tactical FPS | `#F59E0B` | YouTube, FB, IG |
| **PUBG Battlegrounds** | Battle Royale | `#EAB308` | YouTube, FB, IG |
| **Fortnite** | Battle Royale | `#38BDF8` | YouTube, FB, IG |
| **Rainbow Six Siege** | Tactical FPS | `#3B82F6` | YouTube, FB, IG |
| **Call of Duty** | Tactical FPS | `#22C55E` | YouTube, FB, IG |
| **Tekken 8** | Fighting Game | `#A855F7` | YouTube, FB, IG |
| **League of Legends** | MOBA | `#0EA5E9` | YouTube, FB, IG |

---

## ⚙️ Tech Stack & Dependencies

* **Core Language:** Python 3.11+
* **Video/Audio Processing:** FFmpeg, `yt-dlp` (with PO-Token provider support)
* **AI & Transcription:** Google GenAI SDK (`google-genai`), `faster-whisper` (CPU int8 optimization)
* **Image Generation:** Pillow (PIL)
* **APIs:** YouTube Data API v3, Facebook Graph API, Instagram Graph API (via GitHub Raw CDN staging)

---

## 🛠️ Installation & Setup

### 1. Clone the Repository
```bash
git clone [https://github.com/moeed31/yt-clipper.git](https://github.com/moeed31/yt-clipper.git)
cd yt-clipper
### 2. Configure GitHub Secrets
To allow the workflows to authenticate with your social channels and AI providers, add the following secrets in your repository settings (**Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions**):

| Secret Name | Description |
| :--- | :--- |
| `GEMINI_API_KEY` | Google Gemini API Key from [AI Studio](https://aistudio.google.com/apikey) |
| `YT_CLIENT_ID` | Google Cloud Console OAuth Client ID |
| `YT_CLIENT_SECRET` | Google Cloud Console OAuth Client Secret |
| `YT_REFRESH_TOKEN` | Permanent Google OAuth Refresh Token with YouTube scope |
| `FB_PAGE_ID` | Facebook Page ID for cross-posting and reels |
| `FB_PAGE_ACCESS_TOKEN` | Facebook Long-Lived Page Access Token |
| `IG_USER_ID` | Instagram Business Account ID linked to your FB Page |
| `YT_COOKIES` *(Optional)* | Netscape-formatted `cookies.txt` content for robust YouTube authentication |
