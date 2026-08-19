import os
import io
import json
import time
import subprocess
import tempfile

import requests
from PIL import Image
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")
IG_ACCOUNT_ID = os.environ.get("IG_ACCOUNT_ID")
GDRIVE_STORY_FOLDER_ID = os.environ.get("GDRIVE_STORY_FOLDER_ID")
GDRIVE_SERVICE_ACCOUNT_JSON = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")

CLOUDINARY_CLOUD_NAME = "dnbjvccgy"
CLOUDINARY_UPLOAD_PRESET = "vamit5_reels"

STATE_FILE = "state.json"
DELTA_FILE = "state_delta_story.json"
FRAME_W = 1080
FRAME_H = 1920


def check_response(resp):
    if not resp.ok:
        print("API error response:", resp.text)
    resp.raise_for_status()
    return resp


def run_with_retries(func, attempts=5, delays=(5, 10, 20, 40)):
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
            time.sleep(delays[min(attempt - 1, len(delays) - 1)])
    raise last_exc


def get_drive_service():
    info = json.loads(GDRIVE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_story_media(drive):
    files = []
    page_token = None
    while True:
        response = run_with_retries(lambda: drive.files().list(
            q=f"'{GDRIVE_STORY_FOLDER_ID}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, createdTime)",
            pageToken=page_token,
            pageSize=1000,
        ).execute())
        for f in response.get("files", []):
            mime = f.get("mimeType", "")
            if mime.startswith("video/"):
                f["kind"] = "video"
                files.append(f)
            elif mime.startswith("image/"):
                f["kind"] = "image"
                files.append(f)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    files.sort(key=lambda f: (f["createdTime"], f["id"]))
    return files


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {}
    state.setdefault("story_last_index", -1)
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def apply_delta():
    if not os.path.exists(DELTA_FILE):
        return
    with open(DELTA_FILE, "r", encoding="utf-8") as fh:
        delta = json.load(fh)
    state = load_state()
    state["story_last_index"] = delta["index"]
    save_state(state)
    os.remove(DELTA_FILE)


def download_file(drive, file_id, dest_path):
    def attempt():
        request = drive.files().get_media(fileId=file_id)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    run_with_retries(attempt)


def compress_video_for_story(input_path, output_path):
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf",
        f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def resize_image_for_story(input_path, output_path):
    img = Image.open(input_path)
    img = img.convert("RGB")
    img.thumbnail((FRAME_W, FRAME_H), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (FRAME_W, FRAME_H), (0, 0, 0))
    offset = ((FRAME_W - img.width) // 2, (FRAME_H - img.height) // 2)
    canvas.paste(img, offset)
    canvas.save(output_path, "JPEG", quality=90)


def upload_to_cloudinary(path, resource_type):
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{resource_type}/upload"

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


def publish_story(media_url, kind):
    create_url = f"https://graph.instagram.com/v21.0/{IG_ACCOUNT_ID}/media"

    def create_container():
        data = {
            "media_type": "STORIES",
            "access_token": IG_ACCESS_TOKEN,
        }
        if kind == "video":
            data["video_url"] = media_url
        else:
            data["image_url"] = media_url
        resp = requests.post(create_url, data=data, timeout=60)
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
        status_code = run_with_retries(check_status, attempts=2, delays=(5,))
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError("Instagram story container processing failed")
    else:
        raise RuntimeError("Timed out waiting for Instagram to process the story media")

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
    files = list_story_media(drive)
    if not files:
        raise RuntimeError("Nema fotografija ni video fajlova u Story Google Drive folderu")

    state = load_state()
    last_index = state.get("story_last_index", -1)
    next_index = (last_index + 1) % len(files)
    chosen = files[next_index]

    with tempfile.TemporaryDirectory() as tmp:
        local_path = os.path.join(tmp, chosen["id"])
        download_file(drive, chosen["id"], local_path)

        if chosen["kind"] == "video":
            upload_path = os.path.join(tmp, "story.mp4")
            compress_video_for_story(local_path, upload_path)
            resource_type = "video"
        else:
            upload_path = os.path.join(tmp, "story.jpg")
            resize_image_for_story(local_path, upload_path)
            resource_type = "image"

        media_url = upload_to_cloudinary(upload_path, resource_type)
        result = publish_story(media_url, chosen["kind"])
        print("Story objavljena:", result)

    delta = {"index": next_index}
    with open(DELTA_FILE, "w", encoding="utf-8") as fh:
        json.dump(delta, fh)


if __name__ == "__main__":
    import sys
    if "--apply-delta" in sys.argv:
        apply_delta()
    else:
        main()
