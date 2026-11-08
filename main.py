import os
import shutil
import uuid
import requests
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, Optional
from pydub import AudioSegment

app = FastAPI(title="Gulf Stories Video & SFX Engine")

TEMP_DIR = "/app/temp"
SFX_DIR = "/app/sfx"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SFX_DIR, exist_ok=True)

class RenderRequest(BaseModel):
    story_id: str
    audio_drive_id: str
    video_drive_id: str
    sfx_timestamps: Dict[str, Optional[float]]
    sfx_volume_reduction: int = 6

def download_from_drive(file_id: str, dest_path: str):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    response = session.get(url, stream=True)
    
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            url = f"https://drive.google.com/uc?export=download&confirm={value}&id={file_id}"
            response = session.get(url, stream=True)
            break

    if response.status_code != 200:
        raise Exception(f"Failed to download Drive file ID: {file_id}")

    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)

def cleanup_files(folder_path: str):
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)

@app.get("/")
def health_check():
    return {"status": "online", "ffmpeg_installed": shutil.which("ffmpeg") is not None}

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
        download_from_drive(req.audio_drive_id, voice_path)
        download_from_drive(req.video_drive_id, video_path)

        main_audio = AudioSegment.from_file(voice_path)

        for sfx_name, timestamp in req.sfx_timestamps.items():
            if timestamp is None:
                continue
            
            sfx_file = None
            for ext in [".mp3", ".wav"]:
                candidate = os.path.join(SFX_DIR, f"{sfx_name}{ext}")
                if os.path.exists(candidate):
                    sfx_file = candidate
                    break
            
            if sfx_file:
                sfx_audio = AudioSegment.from_file(sfx_file)
                sfx_audio = sfx_audio - req.sfx_volume_reduction
                position_ms = int(float(timestamp) * 1000)
                main_audio = main_audio.overlay(sfx_audio, position=position_ms)

        main_audio.export(mixed_audio_path, format="mp3", bitrate="192k")

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
