import os
import re
import shutil
import uuid
import urllib.parse
import requests
import subprocess
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, Optional
from pydub import AudioSegment

app = FastAPI(title="Auto-SFX & Video Mixer Engine")

TEMP_DIR = "/app/temp"
SFX_CACHE_DIR = "/app/sfx_cache"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SFX_CACHE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

class RenderRequest(BaseModel):
    story_id: str
    audio_drive_id: str
    video_drive_id: str
    sfx_timestamps: Dict[str, Optional[float]]
    sfx_volume_reduction: int = 8  # خفض صوت المؤثر 8 ديسيبل عشان الصوت البشري يفضل واضح

def download_from_drive(file_id: str, dest_path: str):
    """تحميل الصوت والفيديو من جوجل درايف"""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    response = session.get(url, stream=True, headers=HEADERS)
    
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            url = f"https://drive.google.com/uc?export=download&confirm={value}&id={file_id}"
            response = session.get(url, stream=True, headers=HEADERS)
            break

    if response.status_code != 200:
        raise Exception(f"Failed to download Drive file ID: {file_id}")

    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)

def fetch_sfx_automatically(sfx_name: str) -> Optional[str]:
    """البحث التلقائي عن المؤثر الصوتي وتنزيله بدون تدخل يدوي"""
    clean_name = re.sub(r'[^a-zA-Z0-9_]', '', sfx_name.lower())
    cached_path = os.path.join(SFX_CACHE_DIR, f"{clean_name}.mp3")
    
    # لو السيرفر نزله قبل كدا في قصة سابقة، يستخدمه فوراً
    if os.path.exists(cached_path):
        return cached_path

    # تحويل door_slam_wood إلى نص بحث: door slam
    words = sfx_name.replace("_", " ").replace("-", " ").split()
    search_query = " ".join(words[:2]) # أخذ أول كلمتين لنتائج أدق

    # 1. المحاولة الأولى: سحب تلقائي من Freesound (بدون API Key)
    try:
        fs_url = f"https://freesound.org/search/?q={urllib.parse.quote(search_query)}&f=duration%3A%5B0+TO+5%5D"
        resp = requests.get(fs_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            # استخراج رابط ملف الـ mp3 المباشر من صفحة النتائج
            mp3_matches = re.findall(r'https://cdn\.freesound\.org/previews/\d+/\d+_\d+-hq\.mp3', resp.text)
            if not mp3_matches:
                mp3_matches = re.findall(r'https://cdn\.freesound\.org/previews/[^"\'\s]+\.mp3', resp.text)
            
            if mp3_matches:
                audio_resp = requests.get(mp3_matches[0], headers=HEADERS, timeout=10)
                if audio_resp.status_code == 200:
                    with open(cached_path, "wb") as f:
                        f.write(audio_resp.content)
                    return cached_path
    except Exception as e:
        print(f"Freesound auto-fetch warning for {sfx_name}: {e}")

    # 2. المحاولة الاحتياطية: سحب تلقائي من MyInstants
    try:
        mi_url = f"https://www.myinstants.com/en/search/?name={urllib.parse.quote(search_query)}"
        resp = requests.get(mi_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            mp3_matches = re.findall(r'/media/sounds/[^"\'\s]+\.mp3', resp.text)
            if mp3_matches:
                full_mp3_url = "https://www.myinstants.com" + mp3_matches[0]
                audio_resp = requests.get(full_mp3_url, headers=HEADERS, timeout=10)
                if audio_resp.status_code == 200:
                    with open(cached_path, "wb") as f:
                        f.write(audio_resp.content)
                    return cached_path
    except Exception as e:
        print(f"MyInstants auto-fetch warning for {sfx_name}: {e}")

    return None

def cleanup_files(folder_path: str):
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)

@app.get("/")
def health_check():
    return {"status": "online", "auto_sfx": True, "ffmpeg_installed": shutil.which("ffmpeg") is not None}

@app.post("/render-story")
def render_story(req: RenderRequest, background_tasks: BackgroundTasks):
    job_id = f"{req.story_id}_{uuid.uuid4().hex[:6]}"
    job_dir = os.path.join(TEMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    voice_path = os.path.join(job_dir, "voice.wav")
    video_path = os.path.join(job_dir, "bg_video.mp4")
    mixed_audio_path = os.path.join(job_dir, "mixed_audio.mp3")
    output_video_path = os.path.join(job_dir, f"{req.story_id}_final.mp4")

    try:
        # 1. تنزيل صوت القصة وفيديو الخلفية من درايف
        download_from_drive(req.audio_drive_id, voice_path)
        download_from_drive(req.video_drive_id, video_path)

        main_audio = AudioSegment.from_file(voice_path)

        # 2. جلب وتركيب كل مؤثر صوتي أوتوماتيكياً
        for sfx_name, timestamp in req.sfx_timestamps.items():
            if timestamp is None:
                continue
            
            sfx_file = fetch_sfx_automatically(sfx_name)
            
            if sfx_file and os.path.exists(sfx_file):
                try:
                    sfx_audio = AudioSegment.from_file(sfx_file)
                    # قص المؤثر لو أطول من 3.5 ثانية عشان ميعملش إزعاج في القصة
                    if len(sfx_audio) > 3500:
                        sfx_audio = sfx_audio[:3500].fade_out(500)
                    
                    sfx_audio = sfx_audio - req.sfx_volume_reduction
                    position_ms = int(float(timestamp) * 1000)
                    main_audio = main_audio.overlay(sfx_audio, position=position_ms)
                except Exception as sfx_err:
                    print(f"Skipping corrupted SFX {sfx_name}: {sfx_err}")

        main_audio.export(mixed_audio_path, format="mp3", bitrate="192k")

        # 3. دمج الصوت النهائي مع الفيديو بأعلى سرعة وأقل استهلاك رامات
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", video_path,
            "-i", mixed_audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            output_video_path
        ]
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        background_tasks.add_task(cleanup_files, job_dir)

        return FileResponse(
            path=output_video_path,
            media_type="video/mp4",
            filename=f"{req.story_id}_final.mp4"
        )

    except Exception as e:
        cleanup_files(job_dir)
        raise HTTPException(status_code=500, detail=str(e))
