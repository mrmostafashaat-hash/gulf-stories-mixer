import os
import re
import random
import shutil
import uuid
import urllib.parse
import requests
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, Optional
from pydub import AudioSegment, effects

app = FastAPI(title="Auto-SFX & Anti-Copyright Video Engine")

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
    sfx_volume_reduction: int = 8
    apply_anti_copyright: bool = True  # مفعّل افتراضياً لكسر الكوبي رايت

def download_from_drive(file_id: str, dest_path: str):
    """تحميل مباشر للملفات من Google Drive"""
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
    """البحث التلقائي عن المؤثر الصوتي وتنزيله مع فلترة المدة"""
    clean_name = re.sub(r'[^a-zA-Z0-9_]', '', sfx_name.lower())
    cached_path = os.path.join(SFX_CACHE_DIR, f"{clean_name}.mp3")
    
    if os.path.exists(cached_path):
        return cached_path

    words = sfx_name.replace("_", " ").replace("-", " ").split()
    search_query = " ".join(words[:2])

    # 1. البحث في Freesound (مؤثرات قصيرة أقل من 5 ثوانٍ)
    try:
        fs_url = f"https://freesound.org/search/?q={urllib.parse.quote(search_query)}&f=duration%3A%5B0+TO+5%5D"
        resp = requests.get(fs_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
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
        print(f"Freesound warning for {sfx_name}: {e}")

    # 2. البحث الاحتياطي في MyInstants
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
        print(f"MyInstants warning for {sfx_name}: {e}")

    return None

def build_anti_copyright_filter() -> str:
    """توليد سلسلة فلاتر عشوائية في كل مرة لكسر البصمة الرقمية للفيديو تماماً"""
    # زوم عشوائي بين 6% و 10%
    crop_factor = round(random.uniform(0.90, 0.94), 3)
    # تغيير طفيف في السرعة بين 2% و 4%
    speed_pts = round(random.uniform(0.96, 0.98), 3)
    # تعديلات لونية طفيفة وغير ملحوظة للعين لكن تغير قيم البيكسل برمجياً
    contrast = round(random.uniform(1.03, 1.07), 2)
    saturation = round(random.uniform(1.04, 1.10), 2)
    brightness = round(random.uniform(0.01, 0.03), 2)
    gamma = round(random.uniform(0.97, 1.03), 2)

    vf_chain = (
        f"hflip,"                                                                 # قلب أفقي للفيديو
        f"setpts={speed_pts}*PTS,"                                                # كسر البصمة الزمنية للفريمات
        f"crop=iw*{crop_factor}:ih*{crop_factor}:(iw-ow)/2:(ih-oh)/2,"            # قص الأطراف (Zoom-in)
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"   # ضبط أبعاد الشورتس القياسية
        f"eq=contrast={contrast}:saturation={saturation}:brightness={brightness}:gamma={gamma}," # تغيير البصمة اللونية
        f"unsharp=3:3:0.3:3:3:0.0,"                                               # تغيير حدة الحواف لكسر الـ Hash
        f"fps=30,"                                                                # إعادة توليد الإطارات
        f"format=yuv420p"                                                         # توافق قياسي مع كل المنصات
    )
    return vf_chain

def cleanup_files(folder_path: str):
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)

@app.get("/")
def health_check():
    return {
        "status": "online",
        "auto_sfx": True,
        "anti_copyright_filters": "active",
        "ffmpeg_installed": shutil.which("ffmpeg") is not None
    }

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
        # 1. تحميل الصوت والفيديو من جوجل درايف
        download_from_drive(req.audio_drive_id, voice_path)
        download_from_drive(req.video_drive_id, video_path)

        # 2. تحميل التعليق الصوتي وعمل تطبيع قياسي للصوت (Normalization)
        main_audio = AudioSegment.from_file(voice_path)
        main_audio = effects.normalize(main_audio)

        # 3. جلب وفلترة ودمج المؤثرات الصوتية
        for sfx_name, timestamp in req.sfx_timestamps.items():
            if timestamp is None:
                continue
            
            sfx_file = fetch_sfx_automatically(sfx_name)
            
            if sfx_file and os.path.exists(sfx_file):
                try:
                    sfx_audio = AudioSegment.from_file(sfx_file)
                    sfx_audio = effects.normalize(sfx_audio)
                    
                    # فلترة المدة: قص المؤثر لو زاد عن 3 ثوانٍ مع عمل دخول وخروج ناعم (Fade In/Out)
                    if len(sfx_audio) > 3000:
                        sfx_audio = sfx_audio[:3000]
                    sfx_audio = sfx_audio.fade_in(100).fade_out(400)
                    
                    # خفض صوت المؤثر ليكون خلفية واضحة بدون تشويش على القصة
                    sfx_audio = sfx_audio - req.sfx_volume_reduction
                    position_ms = int(float(timestamp) * 1000)
                    main_audio = main_audio.overlay(sfx_audio, position=position_ms)
                except Exception as sfx_err:
                    print(f"Skipping corrupted SFX {sfx_name}: {sfx_err}")

        main_audio.export(mixed_audio_path, format="mp3", bitrate="192k")

        # 4. تجهيز فلاتر الفيديو والصوت في FFmpeg
        video_filter = build_anti_copyright_filter() if req.apply_anti_copyright else "scale=1080:1920,format=yuv420p"
        audio_filter = "highpass=f=70,acompressor=threshold=-16dB:ratio=2:attack=10:release=100"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", video_path,
            "-i", mixed_audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map_metadata", "-1",          # مسح كافة الميتاداتا الأصلية للفيديو
            "-vf", video_filter,            # تطبيق فلاتر كسر الكوبي رايت
            "-af", audio_filter,            # تنقية وضغط الصوت النهائي
            "-c:v", "libx264",
            "-preset", "ultrafast",         # الحفاظ على سرعة الرندر العالية على Render
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",      # تهيئة الفيديو للرفع السريع على يوتيوب وتيك توك
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
