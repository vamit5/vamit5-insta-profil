import os
import io
import json
import time
import subprocess
import tempfile
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID = os.environ["IG_ACCOUNT_ID"]
GDRIVE_NOTEXT_FOLDER_ID = os.environ["GDRIVE_NOTEXT_FOLDER_ID"]
GDRIVE_TEXT_FOLDER_ID = os.environ["GDRIVE_TEXT_FOLDER_ID"]
GDRIVE_SERVICE_ACCOUNT_JSON = os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"]

CLOUDINARY_CLOUD_NAME = "dnbjvccgy"
CLOUDINARY_UPLOAD_PRESET = "vamit5_reels"

STATE_FILE = "state.json"
MIN_CLIP_SECONDS = 9
MIN_TOTAL_SECONDS = 15
COOLDOWN_DAYS = 20
VIDEO_MIME_PREFIX = "video/"

FRAME_W = 1080
FRAME_H = 1920
TEXT_ZONE_TOP_FRAC = 0.55
TEXT_ZONE_BOTTOM_FRAC = 0.80
MAX_TEXT_WIDTH_FRAC = 0.84
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

CAPTION = (
    'Komentariši "VAMIT" i dobijaš link ka 7-dana besplatnom testu VAMIT-5 App. #joinvamit5\n\n'
    '@vamit5.athletes\n'
    '@vamit5.uniform'
)

OVERLAY_TEXTS = [
    "Testiraj VAMIT-5 App 7 dana besplatno",
    "Komentariši VAMIT i dobijaš 7 dana besplatan VAMIT-5 App",
    "Sagori do 800 kalorija za 40 minuta uz VAMIT-5",
]


def check_response(resp):
    if not resp.ok:
        print("API error response:", resp.text)
    resp.raise_for_status()
    return resp


def run_with_retries(func, attempts=3, delay=8):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status < 500:
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(delay)
    raise last_exc


def get_drive_service():
    info = json.loads(GDRIVE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_videos_in_folder(drive, folder_id, folder_tag):
    files = []
    page_token = None
    while True:
        response = run_with_retries(lambda: drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, createdTime, videoMediaMetadata)",
            pageToken=page_token,
            pageSize=1000,
        ).execute())
        for f in response.get("files", []):
            if f.get("mimeType", "").startswith(VIDEO_MIME_PREFIX):
                f["folder"] = folder_tag
                files.append(f)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {}
    state.setdefault("history", {})
    state.setdefault("text_index", 0)
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def rank_files(files, history, now):
    never = []
    cooldown_ok = []
    fallback = []
    for f in files:
        h = history.get(f["id"])
        if not h or h.get("times_posted", 0) == 0:
            never.append(f)
        else:
            last_posted = datetime.fromisoformat(h["last_posted"])
            age_days = (now - last_posted).total_seconds() / 86400
            if age_days >= COOLDOWN_DAYS:
                cooldown_ok.append((age_days, f))
            else:
                fallback.append((age_days, f))
    if never:
        never.sort(key=lambda f: f["createdTime"])
        return never
    if cooldown_ok:
        cooldown_ok.sort(key=lambda t: -t[0])
        return [f for _, f in cooldown_ok]
    fallback.sort(key=lambda t: -t[0])
    return [f for _, f in fallback]


def download_file(drive, file_id, dest_path):
    def attempt():
        request = drive.files().get_media(fileId=file_id)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    run_with_retries(attempt)


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def get_duration(drive_file, local_path):
    meta = drive_file.get("videoMediaMetadata") or {}
    duration_millis = meta.get("durationMillis")
    if duration_millis:
        return int(duration_millis) / 1000.0
    return ffprobe_duration(local_path)


def merge_clips(paths, output_path):
    inputs = []
    filter_parts = []
    for i, p in enumerate(paths):
        inputs += ["-i", p]
        filter_parts.append(
            f"[{i}:v]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
            f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(paths)))
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={len(paths)}:v=1:a=0[outv]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-maxrate", "3500k", "-bufsize", "7000k",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def compress_for_upload(input_path, output_path, keep_audio):
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf",
        f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-maxrate", "3500k", "-bufsize", "7000k",
    ]
    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    subprocess.run(cmd, check=True)


def wrap_text(text, font, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fit_text_lines(text, max_width, start_size=68, min_size=34):
    size = start_size
    font = None
    lines = None
    while size >= min_size:
        font = ImageFont.truetype(FONT_PATH, size)
        lines = wrap_text(text, font, max_width)
        if len(lines) <= 3:
            return font, lines, size
        size -= 4
    font = ImageFont.truetype(FONT_PATH, min_size)
    lines = wrap_text(text, font, max_width)
    return font, lines, min_size


def build_text_overlay(text, out_path):
    max_text_width = int(FRAME_W * MAX_TEXT_WIDTH_FRAC)
    font, lines, size = fit_text_lines(text, max_text_width)

    line_height = int(size * 1.3)
    pad_x, pad_y = 28, 18
    widest_line = max(font.getlength(line) for line in lines)
    box_width = min(FRAME_W - 40, int(widest_line) + pad_x * 2)
    box_height = line_height * len(lines) + pad_y * 2

    zone_top = int(FRAME_H * TEXT_ZONE_TOP_FRAC)
    zone_bottom = int(FRAME_H * TEXT_ZONE_BOTTOM_FRAC)
    zone_center = (zone_top + zone_bottom) // 2
    box_top = max(zone_top, min(zone_bottom - box_height, zone_center - box_height // 2))
    box_left = (FRAME_W - box_width) // 2

    img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [box_left, box_top, box_left + box_width, box_top + box_height],
        radius=20, fill=(0, 0, 0, 150),
    )

    y = box_top + pad_y
    for line in lines:
        w = font.getlength(line)
        x = box_left + (box_width - w) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height

    img.save(out_path)


def apply_text_overlay(base_path, overlay_png_path, out_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", base_path,
        "-i", overlay_png_path,
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        out_path,
    ]
    subprocess.run(cmd, check=True)


def upload_to_cloudinary(path):
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"

    def attempt():
        with open(path, "rb") as fh:
            response = requests.post(
                url,
                files={"file": fh},
                data={"upload_preset": CLOUDINARY_UPLOAD_PRESET},
                timeout=600,
            )
        check_response(response)
        return response.json()["secure_url"]

    return run_with_retries(attempt)


def publish_to_instagram(video_url):
    create_url = f"https://graph.instagram.com/v21.0/{IG_ACCOUNT_ID}/media"

    def create_container():
        resp = requests.post(create_url, data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": CAPTION,
            "access_token": IG_ACCESS_TOKEN,
        }, timeout=60)
        check_response(resp)
        return resp.json()["id"]

    creation_id = run_with_retries(create_container)

    status_url = f"https://graph.instagram.com/v21.0/{creation_id}"

    def check_status():
        status_resp = requests.get(status_url, params={
            "fields": "status_code",
            "access_token": IG_ACCESS_TOKEN,
        }, timeout=60)
        check_response(status_resp)
        return status_resp.json().get("status_code")

    for _ in range(60):
        time.sleep(10)
        status_code = run_with_retries(check_status, attempts=2)
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError("Instagram container processing failed")
    else:
        raise RuntimeError("Timed out waiting for Instagram to process the video")

    publish_url = f"https://graph.instagram.com/v21.0/{IG_ACCOUNT_ID}/media_publish"

    def publish():
        publish_resp = requests.post(publish_url, data={
            "creation_id": creation_id,
            "access_token": IG_ACCESS_TOKEN,
        }, timeout=60)
        check_response(publish_resp)
        return publish_resp.json()

    return run_with_retries(publish)


def main():
    drive = get_drive_service()
    notext_files = list_videos_in_folder(drive, GDRIVE_NOTEXT_FOLDER_ID, "notext")
    text_files = list_videos_in_folder(drive, GDRIVE_TEXT_FOLDER_ID, "text")
    all_files = notext_files + text_files
    if not all_files:
        raise RuntimeError("Nema video fajlova ni u jednom Google Drive folderu")

    state = load_state()
    history = state["history"]
    now = datetime.now(timezone.utc)

    global_rank = rank_files(all_files, history, now)
    start_file = global_rank[0]
    folder = start_file["folder"]

    folder_files = [f for f in all_files if f["folder"] == folder]
    merge_order = rank_files(folder_files, history, now)
    if merge_order[0]["id"] != start_file["id"]:
        merge_order = [start_file] + [f for f in merge_order if f["id"] != start_file["id"]]

    with tempfile.TemporaryDirectory() as tmp:
        chosen_paths = []
        chosen_files = []
        total_duration = 0.0
        for f in merge_order:
            local_path = os.path.join(tmp, f["id"])
            download_file(drive, f["id"], local_path)
            duration = get_duration(f, local_path)
            chosen_paths.append(local_path)
            chosen_files.append(f)
            total_duration += duration

            if len(chosen_paths) == 1 and duration >= MIN_CLIP_SECONDS:
                break
            if len(chosen_paths) > 1 and total_duration >= MIN_TOTAL_SECONDS:
                break

        if len(chosen_paths) == 1:
            base_path = os.path.join(tmp, "solo.mp4")
            compress_for_upload(chosen_paths[0], base_path, keep_audio=True)
        else:
            base_path = os.path.join(tmp, "merged.mp4")
            merge_clips(chosen_paths, base_path)

        if folder == "text":
            text = OVERLAY_TEXTS[state["text_index"] % len(OVERLAY_TEXTS)]
            overlay_png = os.path.join(tmp, "overlay.png")
            build_text_overlay(text, overlay_png)
            final_path = os.path.join(tmp, "final.mp4")
            apply_text_overlay(base_path, overlay_png, final_path)
            upload_path = final_path
            state["text_index"] = (state["text_index"] + 1) % len(OVERLAY_TEXTS)
        else:
            upload_path = base_path

        video_url = upload_to_cloudinary(upload_path)
        result = publish_to_instagram(video_url)
        print("Objavljeno:", result)

    now_iso = now.isoformat()
    for f in chosen_files:
        entry = history.get(f["id"], {"name": f["name"], "times_posted": 0})
        entry["name"] = f["name"]
        entry["last_posted"] = now_iso
        entry["times_posted"] = entry.get("times_posted", 0) + 1
        history[f["id"]] = entry
    save_state(state)


if __name__ == "__main__":
    main()
