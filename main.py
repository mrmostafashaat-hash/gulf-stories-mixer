import os
import re
import gc
import random
import shutil
import uuid
import urllib.parse
import requests
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, List, Optional
from pydub import AudioSegment, effects

app = FastAPI(title="Auto-SFX & Multi-Video Anti-Copyright Engine (Low-RAM)")

TEMP_DIR = "/tmp/render_jobs"
SFX_CACHE_DIR = "/tmp/sfx_cache"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SFX_CACHE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

class RenderRequest(BaseModel):
    story_id: str
    audio_drive_id: str
    video_drive_ids: List[str]
    sfx_timestamps: Dict[str, Optional[float]]
    sfx_volume_reduction: int = 8
    apply_anti_copyright: bool = True

def download_from_drive(file_id: str, dest_path: str):
    """تحميل مباشر للملفات من Google Drive مع تخطي صفحات التحذير الحديثة"""
    session = requests.Session()
    url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download", "confirm": "t"}
    
    response = session.get(url, params=params, stream=True, headers=HEADERS, timeout=60)
    content_type = response.headers.get("Content-Type", "")
    
    if "text/html" in content_type:
        alt_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
        response = session.get(alt_url, stream=True, headers=HEADERS, timeout=60)
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise Exception(f"Google Drive returned HTML for ID {file_id}. Check 'Anyone with the link' permission.")

    if response.status_code != 200:
        raise Exception(f"Failed to download Drive file ID: {file_id} (Status: {response.status_code})")

    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    session.close()

def fetch_sfx_automatically(sfx_name: str) -> Optional[str]:
    """البحث التلقائي عن المؤثر الصوتي وتنزيله"""
    clean_name = re.sub(r'[^a-zA-Z0-9_]', '', sfx_name.lower())
    cached_path = os.path.join(SFX_CACHE_DIR, f"{clean_name}.mp3")
    
    if os.path.exists(cached_path):
        return cached_path

    words = sfx_name.replace("_", " ").replace("-", " ").split()
    search_query = " ".join(words[:2])

    # 1. Freesound
    try:
        fs_url = f"https://freesound.org/search/?q={urllib.parse.quote(search_query)}&f=duration%3A%5B0+TO+5%5D"
        resp = requests.get(fs_url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            mp3_matches = re.findall(r'https://cdn\.freesound\.org/previews/\d+/\d+_\d+-hq\.mp3', resp.text)
            if not mp3_matches:
                mp3_matches = re.findall(r'https://cdn\.freesound\.org/previews/[^"\'\s]+\.mp3', resp.text)
            if mp3_matches:
                audio_resp = requests.get(mp3_matches[0], headers=HEADERS, timeout=8)
                if audio_resp.status_code == 200:
                    with open(cached_path, "wb") as f:
                        f.write(audio_resp.content)
                    return cached_path
    except Exception as e:
        print(f"Freesound warning for {sfx_name}: {e}")

    # 2. MyInstants
    try:
        mi_url = f"https://www.myinstants.com/en/search/?name={urllib.parse.quote(search_query)}"
        resp = requests.get(mi_url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            mp3_matches = re.findall(r'/media/sounds/[^"\'\s]+\.mp3', resp.text)
            if mp3_matches:
                full_mp3_url = "https://www.myinstants.com" + mp3_matches[0]
                audio_resp = requests.get(full_mp3_url, headers=HEADERS, timeout=8)
                if audio_resp.status_code == 200:
                    with open(cached_path, "wb") as f:
                        f.write(audio_resp.content)
                    return cached_path
    except Exception as e:
        print(f"MyInstants warning for {sfx_name}: {e}")

    return None

def build_single_video_filter(apply_anti_copyright: bool) -> str:
    """فلتر خفيف على الرامات (720x1280 بدلاً من 1080x1920 لتفادي انهيار رامات Render المجانية)"""
    target_res = "720:1280"
    if not apply_anti_copyright:
        return f"scale={target_res}:force_original_aspect_ratio=increase,crop={target_res},setsar=1,fps=30,format=yuv420p"

    crop_factor = round(random.uniform(0.90, 0.94), 3)
    speed_pts = round(random.uniform(0.96, 0.98), 3)
    contrast = round(random.uniform(1.03, 1.07), 2)
    saturation = round(random.uniform(1.04, 1.10), 2)
    brightness = round(random.uniform(0.01, 0.03), 2)
    gamma = round(random.uniform(0.97, 1.03), 2)

    return (
        f"hflip,"
        f"setpts={speed_pts}*PTS,"
        f"crop=iw*{crop_factor}:ih*{crop_factor}:(iw-ow)/2:(ih-oh)/2,"
        f"scale={target_res}:force_original_aspect_ratio=increase,crop={target_res},setsar=1,"
        f"eq=contrast={contrast}:saturation={saturation}:brightness={brightness}:gamma={gamma},"
        f"fps=30,"
        f"format=yuv420p"
    )

def cleanup_files(folder_path: str):
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)
    gc.collect()

def mix_audio_low_ram(voice_path: str, mixed_audio_path: str, sfx_timestamps: Dict[str, Optional[float]], volume_reduction: int):
    """دمج الصوت في دالة منفصلة لتفريغ الرامات بالكامل فور انتهاء الدمج"""
    main_audio = AudioSegment.from_file(voice_path)
    main_audio = effects.normalize(main_audio)

    for sfx_name, timestamp in sfx_timestamps.items():
        if timestamp is None:
            continue
        sfx_file = fetch_sfx_automatically(sfx_name)
        if sfx_file and os.path.exists(sfx_file):
            try:
                sfx_audio = AudioSegment.from_file(sfx_file)
                sfx_audio = effects.normalize(sfx_audio)
                if len(sfx_audio) > 3000:
                    sfx_audio = sfx_audio[:3000]
                sfx_audio = sfx_audio.fade_in(100).fade_out(400) - volume_reduction
                position_ms = int(float(timestamp) * 1000)
                main_audio = main_audio.overlay(sfx_audio, position=position_ms)
                del sfx_audio
            except Exception as sfx_err:
                print(f"Skipping SFX {sfx_name}: {sfx_err}")

    main_audio.export(mixed_audio_path, format="mp3", bitrate="128k")
    del main_audio
    if os.path.exists(voice_path):
        os.remove(voice_path)
    gc.collect()

@app.get("/")
def health_check():
    return {
        "status": "online",
        "multi_video_concat": True,
        "auto_sfx": True,
        "anti_copyright_filters": "active_low_ram",
        "ffmpeg_installed": shutil.which("ffmpeg") is not None
    }

@app.post("/render-story")
def render_story(req: RenderRequest, background_tasks: BackgroundTasks):
    if not req.video_drive_ids:
        raise HTTPException(status_code=400, detail="video_drive_ids list cannot be empty")

    job_id = f"{req.story_id}_{uuid.uuid4().hex[:6]}"
    job_dir = os.path.join(TEMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    voice_path = os.path.join(job_dir, "voice.wav")
    mixed_audio_path = os.path.join(job_dir, "mixed_audio.mp3")
    combined_bg_path = os.path.join(job_dir, "combined_bg.mp4")
    concat_list_path = os.path.join(job_dir, "concat_list.txt")
    output_video_path = os.path.join(job_dir, f"{req.story_id}_final.mp4")

    try:
        # 1. تحميل ودمج الصوت ثم تفريغ الرامات فوراً
        download_from_drive(req.audio_drive_id, voice_path)
        mix_audio_low_ram(voice_path, mixed_audio_path, req.sfx_timestamps, req.sfx_volume_reduction)

        # 2. تحميل ومعالجة كل فيديو بأقل استهلاك للرامات (-threads 1)
        processed_videos = []
        for idx, vid_id in enumerate(req.video_drive_ids):
            raw_vid_path = os.path.join(job_dir, f"raw_{idx}.mp4")
            proc_vid_path = os.path.join(job_dir, f"proc_{idx}.mp4")
            
            download_from_drive(vid_id, raw_vid_path)
            vf_filter = build_single_video_filter(req.apply_anti_copyright)
            
            prep_cmd = [
                "ffmpeg", "-y",
                "-threads", "1",
                "-i", raw_vid_path,
                "-an",
                "-map_metadata", "-1",
                "-vf", vf_filter,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "24",
                "-maxrate", "2M",
                "-bufsize", "2M",
                proc_vid_path
            ]
            subprocess.run(prep_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            processed_videos.append(proc_vid_path)
            os.remove(raw_vid_path)
            gc.collect()

        # 3. دمج الفيديوهات
        with open(concat_list_path, "w") as f:
            for p_vid in processed_videos:
                f.write(f"file '{p_vid}'\n")

        concat_cmd = [
            "ffmpeg", "-y",
            "-threads", "1",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            combined_bg_path
        ]
        subprocess.run(concat_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        for p_vid in processed_videos:
            if os.path.exists(p_vid):
                os.remove(p_vid)

        # 4. تركيب الصوت النهائي وتكرار الخلفية
        audio_filter = "highpass=f=70,acompressor=threshold=-16dB:ratio=2:attack=10:release=100"
        final_cmd = [
            "ffmpeg", "-y",
            "-threads", "1",
            "-stream_loop", "-1",
            "-i", combined_bg_path,
            "-i", mixed_audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map_metadata", "-1",
            "-c:v", "copy",
            "-af", audio_filter,
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-shortest",
            output_video_path
        ]
        subprocess.run(final_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        if os.path.exists(combined_bg_path):
            os.remove(combined_bg_path)
        if os.path.exists(mixed_audio_path):
            os.remove(mixed_audio_path)

        background_tasks.add_task(cleanup_files, job_dir)

        return FileResponse(
            path=output_video_path,
            media_type="video/mp4",
            filename=f"{req.story_id}_final.mp4"
        )

    except subprocess.CalledProcessError as ff_err:
        cleanup_files(job_dir)
        err_msg = ff_err.stderr.decode("utf-8", errors="ignore")[-500:] if ff_err.stderr else str(ff_err)
        raise HTTPException(status_code=500, detail=f"FFmpeg error: {err_msg}")
    except Exception as e:
        cleanup_files(job_dir)
        raise HTTPException(status_code=500, detail=str(e))
