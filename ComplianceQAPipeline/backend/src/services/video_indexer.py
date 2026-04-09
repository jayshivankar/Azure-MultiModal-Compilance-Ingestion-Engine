"""
Video Indexer Service — Brand Guardian AI (v2.1)

Upload Strategy:
  Instead of posting a large video file directly to the Azure Video Indexer
  API (which causes TCP connection resets for files > ~50 MB), we use a
  two-step approach:

    1. Download the YouTube video locally with yt-dlp  (max 720p, ~50-200 MB)
    2. Upload the file to Azure Blob Storage            (chunked, highly reliable)
    3. Generate a short-lived SAS URL                  (4-hour expiry)
    4. Submit the SAS URL to Azure Video Indexer        (videoUrl param, not file)
    5. Clean up the local temp file and the blob

  This approach completely avoids the `ConnectionResetError` caused by
  uploading large files in a single multipart POST request to api.videoindexer.ai.

Bot-bypass strategy for cloud/Docker environments:
  • cookies.txt: Netscape format exported from a real browser session.
    Set YOUTUBE_COOKIES_FILE env var to the absolute path.
  • player_client chain: tv_embedded → web_embedded → android
  • 720p max format: keeps file sizes manageable (~80-200 MB)
"""

import datetime
import logging
import os
import time
from urllib.parse import quote

import requests
import yt_dlp
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobClient,
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

logger = logging.getLogger("video-indexer")

# ── Constants ────────────────────────────────────────────────────────────────
BLOB_CONTAINER    = os.getenv("AZURE_BLOB_CONTAINER", "vi-uploads")
VI_POLL_INTERVAL  = 30           # seconds between VI status polls
VI_MAX_WAIT_SECS  = 60 * 30     # 30-minute hard limit on VI processing
REQUEST_TIMEOUT   = (30, 120)    # (connect_timeout, read_timeout) in seconds
UPLOAD_TIMEOUT    = (30, 600)    # (connect_timeout, read_timeout) for large uploads


class VideoIndexerService:
    def __init__(self):
        self.account_id      = os.getenv("AZURE_VI_ACCOUNT_ID")
        self.location        = os.getenv("AZURE_VI_LOCATION", "trial")
        self.subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        self.resource_group  = os.getenv("AZURE_RESOURCE_GROUP")
        self.vi_name         = os.getenv("AZURE_VI_NAME")
        self.storage_conn    = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        self.credential      = DefaultAzureCredential()
        self.cookies_file    = os.getenv("YOUTUBE_COOKIES_FILE")

        # ── yt-dlp bot-mitigation settings (fully configurable via ECS env vars) ──
        # Rate throttle: mimic organic download speeds. Default: no limit (local dev).
        # Production recommendation: "5M" to avoid looking like an automated scraper.
        self.yt_limit_rate: str | None = os.getenv("YOUTUBE_LIMIT_RATE")          # e.g. "5M"

        # User-Agent: should match the browser you used to export cookies.txt.
        # If not set, falls back to the hardcoded Chrome UA below.
        self.yt_user_agent: str | None = os.getenv("YOUTUBE_USER_AGENT")

        # Sleep intervals: add random pauses between fragment requests.
        # Default: 2–8 seconds (already present in code; can be overridden via env).
        try:
            self.yt_sleep_interval: int = int(os.getenv("YOUTUBE_SLEEP_INTERVAL", "2"))
            self.yt_max_sleep_interval: int = int(os.getenv("YOUTUBE_MAX_SLEEP_INTERVAL", "8"))
        except ValueError:
            self.yt_sleep_interval = 2
            self.yt_max_sleep_interval = 8

        # Token cache: {scope: (token, expiry_timestamp)}
        self._token_cache: dict[str, tuple[str, float]] = {}

    # =========================================================================
    # Azure Auth helpers (with token caching)
    # =========================================================================

    def get_access_token(self, scope: str = "https://management.azure.com/.default") -> str:
        """Returns a cached ARM bearer token; refreshes 5 minutes before expiry."""
        cached = self._token_cache.get(scope)
        if cached:
            token, expiry = cached
            if time.time() < expiry - 300:   # refresh 5 min early
                return token

        try:
            token_obj = self.credential.get_token(scope)
            # Azure tokens expire in 3600s; we cache for 55 min to be safe
            self._token_cache[scope] = (token_obj.token, time.time() + 3300)
            return token_obj.token
        except Exception as e:
            logger.error(f"[Auth] Failed to get token for scope={scope}: {e}")
            raise

    def get_account_token(self, arm_token: str) -> str:
        """Exchanges ARM token for a Video Indexer Account access token."""
        url = (
            f"https://management.azure.com/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.VideoIndexer/accounts/{self.vi_name}"
            f"/generateAccessToken?api-version=2024-01-01"
        )
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {arm_token}"},
            json={"permissionType": "Contributor", "scope": "Account"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to get VI Account Token [{resp.status_code}]: {resp.text[:400]}")
        return resp.json().get("accessToken")

    # =========================================================================
    # YouTube download (bot-bypass aware, 720p max)
    # =========================================================================

    def download_youtube_video(self, url: str, output_path: str = "temp_video.mp4") -> str:
        """
        Downloads a YouTube video to disk, limited to 720p to keep file sizes
        manageable (avoids upload timeouts and connection resets).

        Bot-bypass: cookies.txt + tv_embedded player client + configurable
        rate-limiting and sleep intervals (all tunable via ECS Task env vars).

        Env vars that control bot-mitigation (all optional):
          YOUTUBE_COOKIES_FILE    — path to a Netscape cookies.txt
          YOUTUBE_USER_AGENT      — override the User-Agent header
          YOUTUBE_LIMIT_RATE      — e.g. "5M" to throttle to 5 MB/s
          YOUTUBE_SLEEP_INTERVAL  — min seconds between fragment requests (default: 2)
          YOUTUBE_MAX_SLEEP_INTERVAL — max seconds (default: 8)
        """
        logger.info(f"[Downloader] Starting download (max 720p): {url}")

        # Resolve user-agent: prefer env-configured one, fall back to Chrome UA.
        _default_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        user_agent = self.yt_user_agent or _default_ua

        ydl_opts: dict = {
            # 720p cap: keeps file sizes to ~80-200 MB which upload reliably
            "format": (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
                "/best[height<=720][ext=mp4]"
                "/best[height<=720]"
                "/best"
            ),
            "outtmpl": output_path,
            "quiet": False,
            "no_warnings": False,
            "merge_output_format": "mp4",
            "extractor_args": {
                "youtube": {
                    # tv_embedded is least likely to be flagged; web_embedded + android as fallback
                    "player_client": ["tv_embedded", "web_embedded", "android"],
                    "player_skip": ["configs"],
                }
            },
            "http_headers": {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
            "retries": 5,
            "fragment_retries": 5,
            # Randomised sleeps prevent a predictable robotic request pattern.
            "sleep_interval": self.yt_sleep_interval,
            "max_sleep_interval": self.yt_max_sleep_interval,
            # Cap single-file download at 500 MB as a safety net
            "max_filesize": 500 * 1024 * 1024,
        }

        # Rate throttle — mimic human download speeds to avoid bot flags.
        if self.yt_limit_rate:
            ydl_opts["limit_rate"] = self.yt_limit_rate
            logger.info(f"[Downloader] Rate-limit active: {self.yt_limit_rate}/s")
        else:
            logger.info("[Downloader] No rate limit set (YOUTUBE_LIMIT_RATE not configured).")

        logger.info(
            f"[Downloader] Bot-mitigation config — "
            f"UA={'custom' if self.yt_user_agent else 'default Chrome'} | "
            f"sleep={self.yt_sleep_interval}–{self.yt_max_sleep_interval}s | "
            f"rate={self.yt_limit_rate or 'unlimited'}"
        )

        # Cookie injection
        if self.cookies_file and os.path.exists(self.cookies_file):
            ydl_opts["cookiefile"] = self.cookies_file
            logger.info(f"[Downloader] Using cookies file: {self.cookies_file}")
        else:
            logger.warning(
                "[Downloader] YOUTUBE_COOKIES_FILE not set or file missing. "
                "Bot detection bypass is significantly degraded on AWS datacenter IPs. "
                "Export cookies using 'Get cookies.txt LOCALLY' extension and inject via "
                "YOUTUBE_COOKIES_B64 (Secrets Manager) or YOUTUBE_COOKIES_FILE."
            )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            logger.info("[Downloader] Download complete.")
            return output_path
        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            if "Sign in to confirm" in err or "bot" in err.lower() or "429" in err:
                raise RuntimeError(
                    "YouTube bot-detection triggered (HTTP 403/429 or 'Sign in' prompt). "
                    "AWS datacenter IPs are often pre-blocked. "
                    "Ensure YOUTUBE_COOKIES_B64 is set with a fresh session cookie. "
                    "Consider enabling YOUTUBE_LIMIT_RATE=5M and refreshing cookies."
                ) from e
            raise RuntimeError(f"YouTube download failed: {err}") from e


    # =========================================================================
    # Azure Blob Storage: upload + SAS URL generation
    # =========================================================================

    def _ensure_container(self, service_client: BlobServiceClient) -> None:
        """Creates the blob container if it does not exist."""
        try:
            service_client.create_container(BLOB_CONTAINER)
            logger.info(f"[Blob] Created container '{BLOB_CONTAINER}'")
        except Exception:
            pass  # container already exists — ignore

    def upload_to_blob(self, file_path: str, blob_name: str) -> str:
        """
        Uploads a local file to Azure Blob Storage and returns the blob name.
        Uses chunked upload (azure-storage-blob SDK handles this automatically).
        """
        if not self.storage_conn:
            raise RuntimeError(
                "AZURE_STORAGE_CONNECTION_STRING is not set. "
                "Required for reliable video uploads via Blob Storage."
            )

        logger.info(f"[Blob] Uploading {file_path} → container={BLOB_CONTAINER} blob={blob_name}")
        service_client = BlobServiceClient.from_connection_string(self.storage_conn)
        self._ensure_container(service_client)

        blob_client: BlobClient = service_client.get_blob_client(
            container=BLOB_CONTAINER, blob=blob_name
        )
        file_size = os.path.getsize(file_path)
        logger.info(f"[Blob] File size: {file_size / (1024*1024):.1f} MB")

        with open(file_path, "rb") as data:
            blob_client.upload_blob(data, overwrite=True)

        logger.info(f"[Blob] Upload complete: {blob_name}")
        return blob_name

    def generate_sas_url(self, blob_name: str, expiry_hours: int = 4) -> str:
        """Generates a time-limited SAS URL for a blob (default 4 hours)."""
        if not self.storage_conn:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set.")

        service_client = BlobServiceClient.from_connection_string(self.storage_conn)
        account_name = service_client.account_name
        account_key  = service_client.credential.account_key

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=BLOB_CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(hours=expiry_hours),
        )
        url = (
            f"https://{account_name}.blob.core.windows.net"
            f"/{BLOB_CONTAINER}/{quote(blob_name)}?{sas_token}"
        )
        logger.info(f"[Blob] SAS URL generated (expires in {expiry_hours}h)")
        return url

    def delete_blob(self, blob_name: str) -> None:
        """Deletes a blob after it has been indexed (best-effort cleanup)."""
        try:
            service_client = BlobServiceClient.from_connection_string(self.storage_conn)
            blob_client = service_client.get_blob_client(
                container=BLOB_CONTAINER, blob=blob_name
            )
            blob_client.delete_blob()
            logger.info(f"[Blob] Blob deleted: {blob_name}")
        except Exception as e:
            logger.warning(f"[Blob] Could not delete blob {blob_name}: {e}")

    # =========================================================================
    # Azure Video Indexer: submit via URL (NOT file upload)
    # =========================================================================

    def upload_video(self, sas_url: str, video_name: str) -> str:
        """
        Submits a video to Azure Video Indexer using a URL-based submission
        (videoUrl parameter) rather than a raw file upload.

        This avoids the ConnectionResetError that occurs when posting large
        binary files directly to api.videoindexer.ai over a single HTTP connection.
        """
        arm_token = self.get_access_token()
        vi_token  = self.get_account_token(arm_token)

        api_url = (
            f"https://api.videoindexer.ai/{self.location}"
            f"/Accounts/{self.account_id}/Videos"
        )
        params = {
            "accessToken":    vi_token,
            "name":           video_name,
            "privacy":        "Private",
            "indexingPreset": "Default",
            "videoUrl":       sas_url,    # ← URL-based submission (no file upload)
        }

        logger.info(f"[VI] Submitting video via URL to Azure Video Indexer …")
        resp = requests.post(api_url, params=params, timeout=REQUEST_TIMEOUT)

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Azure VI submission failed [{resp.status_code}]: {resp.text[:400]}"
            )

        video_id = resp.json().get("id")
        if not video_id:
            raise RuntimeError(f"Azure VI returned no video ID. Response: {resp.text[:400]}")
        logger.info(f"[VI] Accepted. Azure VI video_id={video_id}")
        return video_id

    # =========================================================================
    # Azure Video Indexer: polling with token caching + timeout
    # =========================================================================

    def wait_for_processing(self, video_id: str) -> dict:
        """
        Polls Azure Video Indexer every 30 seconds until processing completes.

        Improvements over v1:
          • Token caching: re-uses the ARM and VI tokens within their valid window
          • Hard timeout: raises after VI_MAX_WAIT_SECS (30 min) to avoid hanging
          • Better error reporting for Failed/Quarantined states
        """
        logger.info(f"[VI] Waiting for video_id={video_id} (max {VI_MAX_WAIT_SECS//60} min) …")
        started_at = time.time()

        while True:
            elapsed = time.time() - started_at
            if elapsed > VI_MAX_WAIT_SECS:
                raise RuntimeError(
                    f"Azure VI processing timed out after {VI_MAX_WAIT_SECS//60} minutes. "
                    f"video_id={video_id}. Check Azure portal for status."
                )

            arm_token = self.get_access_token()
            vi_token  = self.get_account_token(arm_token)

            url = (
                f"https://api.videoindexer.ai/{self.location}"
                f"/Accounts/{self.account_id}/Videos/{video_id}/Index"
            )
            try:
                resp = requests.get(
                    url,
                    params={"accessToken": vi_token},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning(f"[VI] Poll request failed: {e}. Retrying in {VI_POLL_INTERVAL}s …")
                time.sleep(VI_POLL_INTERVAL)
                continue

            data  = resp.json()
            state = data.get("state")

            if state == "Processed":
                logger.info(f"[VI] Processed ✓ (elapsed: {elapsed:.0f}s)")
                return data
            elif state == "Failed":
                raise RuntimeError(
                    f"Azure Video Indexer processing Failed for video_id={video_id}. "
                    f"Check your Azure Video Indexer dashboard for details."
                )
            elif state == "Quarantined":
                raise RuntimeError(
                    f"Azure VI quarantined video_id={video_id} "
                    "(copyright / content policy violation)."
                )

            logger.info(
                f"[VI] Status={state} | elapsed={elapsed:.0f}s | "
                f"retrying in {VI_POLL_INTERVAL}s …"
            )
            time.sleep(VI_POLL_INTERVAL)

    # =========================================================================
    # Extract transcript + OCR from VI response
    # =========================================================================

    def extract_data(self, vi_json: dict) -> dict:
        """Parses Video Indexer JSON into transcript, ocr_text, video_metadata."""
        transcript_lines = []
        for v in vi_json.get("videos", []):
            for seg in v.get("insights", {}).get("transcript", []):
                text = (seg.get("text") or "").strip()
                if text:
                    transcript_lines.append(text)

        ocr_lines: list[str] = []
        for v in vi_json.get("videos", []):
            for entry in v.get("insights", {}).get("ocr", []):
                text = (entry.get("text") or "").strip()
                if text and text not in ocr_lines:
                    ocr_lines.append(text)

        duration = (
            vi_json
            .get("summarizedInsights", {})
            .get("duration", {})
            .get("seconds")
        )

        return {
            "transcript":     " ".join(transcript_lines),
            "ocr_text":       ocr_lines,
            "video_metadata": {
                "duration_seconds": duration,
                "platform":         "youtube",
            },
        }