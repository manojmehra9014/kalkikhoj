"""KalKiKhoj worker: fetch a queued job from Apps Script, build the video
(free Hindi TTS + free images + ffmpeg), upload to YouTube, report back."""
import asyncio, io, json, os, re, subprocess, sys, tempfile, time, traceback, urllib.parse

import edge_tts
import requests
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

WEBAPP = os.environ["WEBAPP_URL"]
TOKEN = os.environ["WORKER_TOKEN"]
VOICE = os.environ.get("TTS_VOICE", "hi-IN-SwaraNeural")
PRIVACY = os.environ.get("PRIVACY", "public")
W, H, FPS = 1280, 720, 25


def api(payload):
    payload["token"] = TOKEN
    r = requests.post(WEBAPP, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def chunk(text, size=2500):
    parts, cur = [], ""
    for s in re.split(r"(?<=[।.!?])\s+", text):
        if len(cur) + len(s) > size and cur:
            parts.append(cur.strip()); cur = s
        else:
            cur += " " + s
    if cur.strip():
        parts.append(cur.strip())
    return parts


async def tts_chunk(text, path):
    await edge_tts.Communicate(text, VOICE).save(path)


def make_voiceover(script, tmp):
    files = []
    for i, c in enumerate(chunk(script)):
        p = os.path.join(tmp, f"v{i}.mp3")
        for attempt in range(3):
            try:
                asyncio.run(tts_chunk(c, p)); break
            except Exception:
                if attempt == 2: raise
                time.sleep(5)
        files.append(p)
    lst = os.path.join(tmp, "audio.txt")
    open(lst, "w").write("".join(f"file '{f}'\n" for f in files))
    out = os.path.join(tmp, "voice.mp3")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:a", "libmp3lame", "-b:a", "128k", out])
    return out


def fetch_image(prompt, idx, tmp):
    path = os.path.join(tmp, f"img{idx}.jpg")
    q = urllib.parse.quote(prompt + ", cinematic, no text, no logos")
    for attempt in range(3):
        try:
            r = requests.get(f"https://image.pollinations.ai/prompt/{q}",
                             params={"width": W, "height": H, "nologo": "true", "seed": idx * 7 + attempt},
                             timeout=120)
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            # cover-crop to 16:9
            scale = max(W / im.width, H / im.height)
            im = im.resize((int(im.width * scale), int(im.height * scale)))
            l, t = (im.width - W) // 2, (im.height - H) // 2
            im.crop((l, t, l + W, t + H)).save(path, quality=90)
            return path
        except Exception:
            time.sleep(8)
    Image.new("RGB", (W, H), (20 + idx * 15 % 80, 30, 60)).save(path)   # plain fallback card
    return path


def build_video(images, audio, tmp):
    total = duration(audio)
    per = total / len(images)
    frames = max(1, int(per * FPS))
    clips = []
    for i, img in enumerate(images):
        out = os.path.join(tmp, f"clip{i}.mp4")
        vf = (f"scale={W*2}:{H*2},zoompan=z='min(zoom+0.0003,1.12)':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},format=yuv420p")
        run(["ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{per:.2f}", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "23", out])
        clips.append(out)
    lst = os.path.join(tmp, "clips.txt")
    open(lst, "w").write("".join(f"file '{c}'\n" for c in clips))
    final = os.path.join(tmp, "final.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", final])
    return final


def youtube():
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds)


def upload(job, video, thumb):
    yt = youtube()
    desc = job["description"] + "\n\nइस वीडियो में AI-जनित आवाज़ और चित्रों का उपयोग किया गया है।"
    body = {
        "snippet": {"title": job["title"][:100], "description": desc[:4900], "tags": job["tags"],
                    "categoryId": "27", "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }
    media = MediaFileUpload(video, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    try:
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
    except HttpError as e:
        if "containsSyntheticMedia" not in str(e):
            raise
        body["status"].pop("containsSyntheticMedia")   # older API surface: retry without the flag
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
    except Exception as e:
        print("Thumbnail skipped:", e)
    return f"https://www.youtube.com/watch?v={vid}"


def main():
    job = api({"action": "next"}).get("job")
    if not job:
        print("No job."); return
    print("Job:", job["id"], job["title"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            audio = make_voiceover(job["script"], tmp)
            images = [fetch_image(p, i, tmp) for i, p in enumerate(job["image_prompts"])]
            video = build_video(images, audio, tmp)
            url = upload(job, video, images[0])
        api({"action": "done", "row": job["row"], "url": url})
        print("Published:", url)
    except Exception as e:
        traceback.print_exc()
        api({"action": "fail", "row": job["row"], "error": str(e)[:300]})
        sys.exit(1)


if __name__ == "__main__":
    main()
