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
POLLINATIONS_KEY = os.environ.get("POLLINATIONS_KEY", "")   # optional, from enter.pollinations.ai
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")           # optional free stock-photo fallback
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")         # optional free stock-photo fallback (pixabay.com/api/docs)
POLLINATIONS_DOWN = False   # set after one image fails completely, so the rest don't wait for a dead service
W, H, FPS = 1280, 720, 25


def log(msg):
    print(time.strftime("[%H:%M:%S UTC]"), msg, flush=True)


def api(payload, tries=1):
    payload["token"] = TOKEN
    for i in range(tries):
        try:
            r = requests.post(WEBAPP, json=payload, timeout=120)
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise RuntimeError("Apps Script: " + j["error"])
            return j
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"API call '{payload.get('action')}' failed ({e}), retrying...")
            time.sleep(30 * (i + 1))


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
    parts = chunk(script)
    for i, c in enumerate(parts):
        log(f"Voiceover part {i + 1}/{len(parts)}")
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
    log(f"Voiceover ready ({duration(out) / 60:.1f} min)")
    return out


def save_cover(data, path):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    # cover-crop to 16:9
    scale = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * scale), int(im.height * scale)))
    l, t = (im.width - W) // 2, (im.height - H) // 2
    im.crop((l, t, l + W, t + H)).save(path, quality=90)


def from_pollinations(prompt, idx, path):
    global POLLINATIONS_DOWN
    if POLLINATIONS_DOWN:
        return False
    q = urllib.parse.quote(prompt + ", cinematic, no text, no logos")
    headers = {"Authorization": f"Bearer {POLLINATIONS_KEY}"} if POLLINATIONS_KEY else {}
    for attempt in range(3):
        try:
            r = requests.get(f"https://image.pollinations.ai/prompt/{q}", headers=headers,
                             params={"width": W, "height": H, "nologo": "true", "seed": idx * 7 + attempt},
                             timeout=120)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
            save_cover(r.content, path)
            return True
        except Exception as e:
            log(f"  image {idx + 1} pollinations attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(15 * (attempt + 1))
    POLLINATIONS_DOWN = True
    log("  pollinations looks down, using stock photos for the remaining images")
    return False


def search_words(prompt):
    # stock photo search works best with a few plain words from the start of the prompt
    return re.sub(r"[^A-Za-z0-9 ]", " ", prompt.split(",")[0]).split()[:6]


def from_pixabay(prompt, idx, path):
    words = search_words(prompt)
    try:
        hits = []
        for q in (" ".join(words), " ".join(words[:3])):   # retry with fewer words if nothing matched
            r = requests.get("https://pixabay.com/api/", timeout=60,
                             params={"key": PIXABAY_KEY, "q": q[:100] or "india", "image_type": "photo",
                                     "orientation": "horizontal", "safesearch": "true", "per_page": 5})
            r.raise_for_status()
            hits = r.json().get("hits") or []
            if hits:
                break
        if not hits:
            raise RuntimeError("no results for: " + " ".join(words))
        img = requests.get(hits[idx % len(hits)]["largeImageURL"], timeout=60)
        img.raise_for_status()
        save_cover(img.content, path)
        return True
    except Exception as e:
        log(f"  image {idx + 1} pixabay failed: {e}")
        return False


def from_pexels(prompt, idx, path):
    words = search_words(prompt)
    try:
        r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": PEXELS_KEY},
                         params={"query": " ".join(words) or "india", "orientation": "landscape", "per_page": 5},
                         timeout=60)
        r.raise_for_status()
        photos = r.json().get("photos") or []
        if not photos:
            raise RuntimeError("no results for: " + " ".join(words))
        img = requests.get(photos[idx % len(photos)]["src"]["large2x"], timeout=60)
        img.raise_for_status()
        save_cover(img.content, path)
        return True
    except Exception as e:
        log(f"  image {idx + 1} pexels failed: {e}")
        return False


def fetch_image(prompt, idx, tmp):
    """Returns (path, real) - real is False when only a plain colour card could be made."""
    path = os.path.join(tmp, f"img{idx}.jpg")
    if from_pollinations(prompt, idx, path):
        return path, True
    if PIXABAY_KEY and from_pixabay(prompt, idx, path):
        log(f"  image {idx + 1}: used a Pixabay stock photo")
        return path, True
    if PEXELS_KEY and from_pexels(prompt, idx, path):
        log(f"  image {idx + 1}: used a Pexels stock photo")
        return path, True
    log(f"  image {idx + 1}: using plain fallback card")
    Image.new("RGB", (W, H), (20 + idx * 15 % 80, 30, 60)).save(path)   # plain fallback card
    return path, False


def build_video(images, audio, tmp):
    total = duration(audio)
    per = total / len(images)
    frames = max(1, int(per * FPS))
    clips = []
    for i, img in enumerate(images):
        log(f"Rendering clip {i + 1}/{len(images)}")
        out = os.path.join(tmp, f"clip{i}.mp4")
        vf = (f"scale={W*2}:{H*2},zoompan=z='min(zoom+0.0003,1.12)':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},format=yuv420p")
        run(["ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{per:.2f}", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "23", out])
        clips.append(out)
    lst = os.path.join(tmp, "clips.txt")
    open(lst, "w").write("".join(f"file '{c}'\n" for c in clips))
    final = os.path.join(tmp, "final.mp4")
    log("Joining clips with voiceover")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", final])
    log(f"Video ready ({os.path.getsize(final) / 1e6:.0f} MB)")
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

    def send():
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                log(f"Uploading to YouTube: {int(status.progress() * 100)}%")
        return resp

    log(f"Uploading to YouTube ({PRIVACY})")
    try:
        resp = send()
    except HttpError as e:
        if "containsSyntheticMedia" not in str(e):
            raise
        body["status"].pop("containsSyntheticMedia")   # older API surface: retry without the flag
        resp = send()
    vid = resp["id"]
    log(f"Uploaded, video id {vid}")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
    except Exception as e:
        print("Thumbnail skipped:", e)
    return f"https://www.youtube.com/watch?v={vid}"


def main():
    log("Asking the Brain for a job...")
    job = api({"action": "next"}, tries=3).get("job")
    if not job:
        log("No job. (No QUEUE_RENDER row in the Sheet, today's publish limit reached, or pipeline paused.)")
        return
    log(f"Job picked: {job['id']} | row {job['row']} | {job['title']}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            log("Step 1/4: voiceover")
            audio = make_voiceover(job["script"], tmp)
            prompts = job.get("image_prompts") or []
            if not prompts:
                # rows filled by hand / older code may have an empty column T
                base = job.get("thumb_idea") or job["title"]
                prompts = [f"{base}, scene {i + 1}" for i in range(6)]
                log("No image prompts in the Sheet (column T), using the thumbnail idea instead")
            log(f"Step 2/4: images ({len(prompts)})")
            images, real = [], 0
            for i, p in enumerate(prompts):
                log(f"Image {i + 1}/{len(prompts)}")
                path, ok = fetch_image(p, i, tmp)
                images.append(path); real += ok
            if real < len(prompts) / 2:
                # don't publish a video made mostly of blank cards; the Brain re-queues it for the next run
                raise RuntimeError(f"Only {real}/{len(prompts)} images could be fetched (image service down)")
            log("Step 3/4: rendering video")
            video = build_video(images, audio, tmp)
            log("Step 4/4: uploading")
            url = upload(job, video, images[0])
    except Exception as e:
        traceback.print_exc()
        log("FAILED, telling the Brain so it can retry later")
        api({"action": "fail", "row": job["row"], "error": str(e)[:300]}, tries=3)
        sys.exit(1)
    # Outside the try: once the video is on YouTube, never report "fail" (that would re-queue and upload it twice).
    log(f"Published: {url}")
    try:
        api({"action": "done", "row": job["row"], "url": url}, tries=5)
    except Exception:
        traceback.print_exc()
        log(f"!! Video is LIVE but the Sheet was not updated. Set row {job['row']} to PUBLISHED and paste {url} in column N.")
        sys.exit(1)
    log("Sheet updated to PUBLISHED. Done.")


if __name__ == "__main__":
    main()
