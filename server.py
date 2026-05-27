"""
YT Downloader API — FastAPI backend wrapping yt-dlp.
"""
import json
import os
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="YT Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для временных файлов (очищается при старте)
TMP_DIR = Path("/tmp/yt-downloader")
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Кэш информации о видео (in-memory, простой)
info_cache = {}

class InfoRequest(BaseModel):
    videoId: str

class DownloadRequest(BaseModel):
    videoId: str
    quality: str = "720p"

def get_yt_dlp_path():
    """Найти yt-dlp."""
    for p in ["/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp", "/usr/bin/yt-dlp"]:
        if os.path.exists(p):
            return p
    # Ищем в PATH
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise RuntimeError("yt-dlp not found")

YT_DLP = get_yt_dlp_path()

def extract_video_id(url_or_id: str) -> str:
    """Извлечь ID видео (11 символов)."""
    # Если это уже ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url_or_id):
        return url_or_id
    # Иначе парсим URL
    parsed = urlparse(url_or_id)
    if 'youtu.be' in parsed.netloc:
        return parsed.path.lstrip('/').split('?')[0][:11]
    qs = parse_qs(parsed.query)
    if 'v' in qs:
        return qs['v'][0][:11]
    raise HTTPException(status_code=400, detail="Не удалось извлечь ID видео из URL")

def run_yt_dlp(args, timeout=60):
    """Запустить yt-dlp."""
    try:
        result = subprocess.run(
            [YT_DLP] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"yt-dlp error: {result.stderr[:500]}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Таймаут yt-dlp")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp не найден")

@app.post("/info")
async def video_info(req: InfoRequest):
    """Получить информацию о видео."""
    video_id = extract_video_id(req.videoId)
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    try:
        raw = run_yt_dlp([
            "--dump-json",
            "--no-playlist",
            "--no-check-formats",
            url
        ], timeout=30)
        
        data = json.loads(raw)
        
        # Определяем доступные форматы
        formats = set()
        for fmt in data.get("formats", []):
            if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                h = fmt.get("height", 0)
                if h >= 1080:
                    formats.add("1080p")
                elif h >= 720:
                    formats.add("720p")
            if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none":
                formats.add("audio")
        
        info = {
            "title": data.get("title", ""),
            "duration": data.get("duration", 0),
            "viewCount": data.get("view_count", 0),
            "channel": data.get("channel", "") or data.get("uploader", ""),
            "thumbnail": data.get("thumbnail", ""),
            "formats": sorted(list(formats)),
        }
        
        # Кэшируем
        info_cache[video_id] = info
        return info
        
    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Не удалось распарсить ответ yt-dlp")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])

@app.post("/download")
async def download_video(req: DownloadRequest):
    """Получить прямую ссылку на скачивание или сам файл."""
    video_id = extract_video_id(req.videoId)
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # Формат для yt-dlp
    if req.quality == "1080p":
        fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    elif req.quality == "720p":
        fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    elif req.quality == "audio":
        fmt = "bestaudio/best"
    else:
        fmt = "best"
    
    try:
        # Получаем прямую ссылку
        raw = run_yt_dlp([
            "--get-url",
            "--no-playlist",
            "-f", fmt,
            url
        ], timeout=30)
        
        direct_url = raw.split('\n')[0].strip()
        if not direct_url or not direct_url.startswith("http"):
            raise HTTPException(status_code=500, detail="Не удалось получить ссылку на видео")
        
        # Получаем название файла
        title_raw = run_yt_dlp([
            "--get-filename",
            "--no-playlist",
            "-f", fmt,
            "-o", "%(title)s.%(ext)s",
            url
        ], timeout=15)
        
        filename = title_raw.strip() if title_raw else f"{video_id}.mp4"
        
        return {
            "url": direct_url,
            "filename": filename,
            "quality": req.quality
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])

@app.get("/health")
async def health():
    return {"status": "ok", "yt_dlp": YT_DLP}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
