"""KalKiKhoj worker: fetch a queued job from Apps Script, build a documentary-style video
(free Hindi TTS + Pixabay stock videos/photos matched to each part of the narration + ffmpeg),
upload to YouTube, report back."""
import asyncio, base64, io, json, math, os, re, subprocess, sys, tempfile, time, traceback

import edge_tts
import requests
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

WEBAPP = os.environ["WEBAPP_URL"]
TOKEN = os.environ["WORKER_TOKEN"]
VOICE = os.environ.get("TTS_VOICE") or "hi-IN-SwaraNeural"
PRIVACY = os.environ.get("PRIVACY") or "public"
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "")
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
W, H, FPS = 1280, 720, 25
SCENE_WORDS = 28      # narration words per scene (~10 s of Hindi speech)
MAX_SHOT = 7.0        # seconds; longer scenes get several different visuals
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


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
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-600:])


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


# ---------- Gemini (scene plan + thumbnail image) ----------
def gemini_call(model, body, timeout=180):
    r = requests.post(GEMINI_URL.format(model), headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"Gemini {model} HTTP {r.status_code}: {r.text[:200]}")
    cand = (r.json().get("candidates") or [{}])[0]
    return (cand.get("content") or {}).get("parts") or []


def gemini_json(prompt):
    last = None
    for model in [m for m in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL) if m]:
        for attempt in range(2):
            try:
                parts = gemini_call(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}})
                text = "".join(p.get("text", "") for p in parts).strip()
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
                try:
                    return json.loads(text)
                except ValueError:
                    return json.loads(text[text.index("{"):text.rindex("}") + 1])
            except Exception as e:
                last = e
                log(f"  Gemini attempt failed: {str(e)[:200]}")
                time.sleep(15)
    raise RuntimeError(f"Gemini unavailable: {last}")


def gemini_image(prompt, path):
    parts = gemini_call(GEMINI_IMAGE_MODEL, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                             "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}})
    for p in parts:
        data = (p.get("inlineData") or p.get("inline_data") or {}).get("data")
        if data:
            save_cover(base64.b64decode(data), path)
            return
    raise RuntimeError("no image in Gemini response")


# ---------- narration ----------
def split_scenes(script):
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", script) if s.strip()]
    scenes, cur = [], []
    for s in sentences:
        cur.append(s)
        if len(" ".join(cur).split()) >= SCENE_WORDS:
            scenes.append(" ".join(cur)); cur = []
    if cur:
        if scenes and len(" ".join(cur).split()) < SCENE_WORDS // 2:
            scenes[-1] += " " + " ".join(cur)
        else:
            scenes.append(" ".join(cur))
    return scenes


def tts(text, path):
    for attempt in range(3):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE).save(path))
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)


def make_voiceover(scenes, tmp):
    """One audio file per scene, so each visual can be cut to exactly match its narration."""
    wavs, durs = [], []
    for i, text in enumerate(scenes):
        if i % 10 == 0:
            log(f"Voiceover scene {i + 1}-{min(i + 10, len(scenes))}/{len(scenes)}")
        mp3, wav = os.path.join(tmp, f"v{i}.mp3"), os.path.join(tmp, f"v{i}.wav")
        tts(text, mp3)
        run(["ffmpeg", "-y", "-i", mp3, "-af", "apad=pad_dur=0.3", "-ar", "24000", "-ac", "1", wav])
        wavs.append(wav); durs.append(duration(wav))
    lst = os.path.join(tmp, "audio.txt")
    open(lst, "w").write("".join(f"file '{f}'\n" for f in wavs))
    out = os.path.join(tmp, "voice.wav")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    log(f"Voiceover ready ({sum(durs) / 60:.1f} min, {len(scenes)} scenes)")
    return out, durs


# ---------- visual plan ----------
def search_words(text, n=4):
    return " ".join(re.sub(r"[^A-Za-z0-9 ]", " ", text.split(",")[0]).split()[:n])


def plan_visuals(job, scenes):
    """English stock-footage search words for every scene + a thumbnail prompt."""
    prompts = job.get("image_prompts") or [job.get("thumb_idea") or "india documentary"]
    fallback = [search_words(prompts[i * len(prompts) // len(scenes)]) for i in range(len(scenes))]
    plan = {"queries": fallback, "thumbnail_prompt": job.get("thumb_idea") or job["title"], "thumbnail_query": fallback[0]}
    if not GEMINI_KEY:
        log("No GEMINI_API_KEY, using the image prompts from the Sheet as search words")
        return plan
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenes))
    try:
        out = gemini_json(
            "You are the editor of a Hindi YouTube documentary. For EACH numbered narration segment below, give an English "
            "search query (2-4 concrete visual words) to find matching stock B-roll footage on Pixabay, e.g. 'indian city traffic', "
            "'robot arm factory', 'student using laptop', 'ancient temple ruins'. Show what is being talked about; no names of real "
            "people or brands, no abstract words.\n"
            f"Also give thumbnail_prompt: a vivid English prompt for an eye-catching 16:9 YouTube thumbnail image about '{job['title']}' "
            "(no text, no logos, no real people), and thumbnail_query: 2-4 words to find a thumbnail photo.\n"
            f'Return ONLY JSON: {{"queries":["..." x {len(scenes)}],"thumbnail_prompt":"","thumbnail_query":""}}\n\n'
            "SEGMENTS:\n" + numbered)
        q = [str(x).strip() for x in (out.get("queries") or [])]
        plan["queries"] = [q[i] if i < len(q) and q[i] else fallback[i] for i in range(len(scenes))]
        plan["thumbnail_prompt"] = out.get("thumbnail_prompt") or plan["thumbnail_prompt"]
        plan["thumbnail_query"] = out.get("thumbnail_query") or plan["thumbnail_query"]
        log(f"Scene plan from Gemini: {len(q)} queries, e.g. {plan['queries'][:3]}")
    except Exception as e:
        log(f"Gemini scene plan failed ({e}), using the image prompts from the Sheet instead")
    return plan


# ---------- Pixabay ----------
PIXABAY_CACHE, USED = {}, set()


def pixabay(kind, q):
    key = (kind, q)
    if key not in PIXABAY_CACHE:
        url = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
        params = {"key": PIXABAY_KEY, "q": q[:100], "safesearch": "true", "per_page": 20}
        if kind == "photo":
            params.update(image_type="photo", orientation="horizontal")
        hits = []
        for attempt in range(3):
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429:   # 100 requests/min limit
                time.sleep(30); continue
            r.raise_for_status()
            hits = r.json().get("hits") or []
            break
        PIXABAY_CACHE[key] = hits
        time.sleep(0.7)
    return PIXABAY_CACHE[key]


def save_cover(data, path):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    # cover-crop to 16:9
    scale = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * scale), int(im.height * scale)))
    l, t = (im.width - W) // 2, (im.height - H) // 2
    im.crop((l, t, l + W, t + H)).save(path, quality=90)


def landscape_video(hit):
    for size in ("medium", "large", "small"):
        v = (hit.get("videos") or {}).get(size) or {}
        if v.get("url") and v.get("width", 0) >= v.get("height", 1) and v.get("width", 0) >= 960:
            return v["url"]
    return None


def fetch_visual(query, want_video, n, tmp):
    """Returns (kind, path) where kind is 'video', 'photo' or 'card'."""
    words = query.split()
    for q in dict.fromkeys([query, " ".join(words[:2]), words[0] if words else ""]):
        if not q:
            continue
        for kind in (("video", "photo") if want_video else ("photo", "video")):
            try:
                for hit in pixabay(kind, q):
                    if (kind, hit["id"]) in USED:
                        continue
                    src = landscape_video(hit) if kind == "video" else hit.get("largeImageURL")
                    if not src:
                        continue
                    data = requests.get(src, timeout=120)
                    data.raise_for_status()
                    USED.add((kind, hit["id"]))
                    if kind == "video":
                        path = os.path.join(tmp, f"src{n}.mp4")
                        open(path, "wb").write(data.content)
                    else:
                        path = os.path.join(tmp, f"src{n}.jpg")
                        save_cover(data.content, path)
                    return kind, path
            except Exception as e:
                log(f"  shot {n + 1}: pixabay {kind} '{q}' failed: {e}")
    path = os.path.join(tmp, f"src{n}.jpg")
    Image.new("RGB", (W, H), (20 + n * 15 % 80, 30, 60)).save(path)   # plain fallback card
    return "card", path


# ---------- rendering ----------
ENC = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
       "-pix_fmt", "yuv420p", "-video_track_timescale", "12800"]


def render_shot(kind, src, frames, n, tmp):
    out = os.path.join(tmp, f"shot{n:03d}.mp4")
    if kind == "video":
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf, "-frames:v", str(frames)] + ENC + [out])
    else:
        # slow zoom in / zoom out / pan, so photos feel alive
        z, x, y = [("1+0.15*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
                   ("1.15-0.15*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
                   ("1.15", "(iw-iw/zoom)*on/{N}", "ih/2-(ih/zoom/2)")][n % 3]
        z, x = z.format(N=frames), x.format(N=frames)
        vf = (f"scale={W * 2}:{H * 2},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},setsar=1")
        run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames)] + ENC + [out])
    return out


def build_video(scenes, durs, plan, audio, tmp):
    shots, t, n, real = [], 0.0, 0, 0
    total_shots = sum(max(1, math.ceil(d / MAX_SHOT)) for d in durs)
    for i, d in enumerate(durs):
        k = max(1, math.ceil(d / MAX_SHOT))
        start_f, end_f = round(t * FPS), round((t + d) * FPS)   # cumulative frames keep picture and voice in sync
        t += d
        for j in range(k):
            frames = (end_f - start_f) * (j + 1) // k - (end_f - start_f) * j // k
            kind, src = fetch_visual(plan["queries"][i], n % 3 != 2, n, tmp)   # mostly video clips, every 3rd a photo
            real += kind != "card"
            log(f"Shot {n + 1}/{total_shots}: {kind:5s} '{plan['queries'][i]}' ({frames / FPS:.1f}s)")
            shots.append(render_shot(kind, src, max(frames, 1), n, tmp))
            n += 1
    if real < 0.6 * n:
        raise RuntimeError(f"Only {real}/{n} shots found real footage (Pixabay down or key missing)")
    lst = os.path.join(tmp, "shots.txt")
    open(lst, "w").write("".join(f"file '{s}'\n" for s in shots))
    final = os.path.join(tmp, "final.mp4")
    log("Joining shots with voiceover")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio, "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", final])
    log(f"Video ready ({duration(final) / 60:.1f} min, {n} shots, {os.path.getsize(final) / 1e6:.0f} MB)")
    return final, shots


def make_thumbnail(plan, shots, tmp):
    """Gemini image if possible, else a Pixabay photo, else a frame from the video. Returns (path, source)."""
    path = os.path.join(tmp, "thumb.jpg")
    if GEMINI_KEY:
        try:
            gemini_image(plan["thumbnail_prompt"] + ". 16:9 YouTube thumbnail, dramatic lighting, no text, no logos.", path)
            return path, "Gemini"
        except Exception as e:
            log(f"Gemini thumbnail failed ({str(e)[:200]}), using a stock photo")
    try:
        for hit in pixabay("photo", plan["thumbnail_query"]):
            if ("photo", hit["id"]) not in USED:
                r = requests.get(hit["largeImageURL"], timeout=60)
                r.raise_for_status()
                save_cover(r.content, path)
                return path, "Pixabay"
    except Exception as e:
        log(f"Pixabay thumbnail failed ({e}), using a video frame")
    run(["ffmpeg", "-y", "-ss", "1", "-i", shots[0], "-frames:v", "1", "-q:v", "3", path])
    return path, "video frame"


# ---------- YouTube ----------
def youtube():
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds)


def upload(job, video, thumb):
    yt = youtube()
    desc = (job["description"] + "\n\nइस वीडियो में AI-जनित आवाज़ का उपयोग किया गया है।"
            "\nVisuals: Pixabay")
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
        log("Thumbnail set")
    except Exception as e:
        log(f"Thumbnail skipped (channel may need phone verification): {e}")
    return f"https://www.youtube.com/watch?v={vid}"


def main():
    if not PIXABAY_KEY:
        sys.exit("PIXABAY_API_KEY secret is missing: add it in GitHub > Settings > Secrets > Actions.")
    log("Asking the Brain for a job...")
    job = api({"action": "next"}, tries=3).get("job")
    if not job:
        log("No job. (No QUEUE_RENDER row in the Sheet, today's publish limit reached, or pipeline paused.)")
        return
    log(f"Job picked: {job['id']} | row {job['row']} | {job['title']}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scenes = split_scenes(job["script"])
            if not scenes:
                raise RuntimeError("Script (column F) is empty")
            log(f"Step 1/5: voiceover ({len(scenes)} scenes)")
            audio, durs = make_voiceover(scenes, tmp)
            log("Step 2/5: matching visuals to the narration")
            plan = plan_visuals(job, scenes)
            log("Step 3/5: fetching footage and rendering")
            video, shots = build_video(scenes, durs, plan, audio, tmp)
            log("Step 4/5: thumbnail")
            thumb, thumb_src = make_thumbnail(plan, shots, tmp)
            log(f"Thumbnail from {thumb_src}")
            log("Step 5/5: uploading")
            url = upload(job, video, thumb)
    except Exception as e:
        traceback.print_exc()
        log("FAILED, telling the Brain so it can retry later")
        api({"action": "fail", "row": job["row"], "error": str(e)[:300]}, tries=3)
        sys.exit(1)
    # Outside the try: once the video is on YouTube, never report "fail" (that would re-queue and upload it twice).
    log(f"Published: {url}")
    try:
        api({"action": "done", "row": job["row"], "url": url, "title": job["title"],
             "thumb_prompt": plan["thumbnail_prompt"], "thumb_source": thumb_src}, tries=5)
    except Exception:
        traceback.print_exc()
        log(f"!! Video is LIVE but the Sheet was not updated. Set row {job['row']} to PUBLISHED and paste {url} in column N.")
        sys.exit(1)
    log("Sheet updated to PUBLISHED. Done.")


if __name__ == "__main__":
    main()
