"""Сервис хранения записей звонков.
Загружает WAV/MP3 файлы в MinIO (S3-совместимое хранилище) и генерирует presigned URL."""

import logging
import os
from datetime import timedelta

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# MinIO / S3 config (добавить в settings при необходимости)
MINIO_ENDPOINT = os.getenv("KURO_MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("KURO_MINIO_ACCESS_KEY", "kurotrack")
MINIO_SECRET_KEY = os.getenv("KURO_MINIO_SECRET_KEY", "kurotrack123")
MINIO_BUCKET = os.getenv("KURO_MINIO_BUCKET", "recordings")


class RecordingService:
    """Управление записями звонков."""

    def __init__(self):
        self._s3_client = None

    async def upload_recording(self, file_path: str, call_id: str) -> str | None:
        """Загружает файл записи в MinIO. Возвращает URL для доступа."""
        if not os.path.exists(file_path):
            logger.warning("Recording file not found: %s", file_path)
            return None

        # Определяем расширение и content-type
        ext = os.path.splitext(file_path)[1].lower()
        content_type = "audio/wav" if ext == ".wav" else "audio/mpeg"
        object_name = f"{call_id}{ext}"

        try:
            # Используем httpx для прямой загрузки в MinIO через S3 API
            with open(file_path, "rb") as f:
                file_data = f.read()

            async with httpx.AsyncClient() as client:
                url = f"{MINIO_ENDPOINT}/{MINIO_BUCKET}/{object_name}"
                response = await client.put(
                    url,
                    content=file_data,
                    headers={"Content-Type": content_type},
                    auth=(MINIO_ACCESS_KEY, MINIO_SECRET_KEY),
                    timeout=60.0,
                )

                if response.status_code < 300:
                    recording_url = f"{MINIO_ENDPOINT}/{MINIO_BUCKET}/{object_name}"
                    logger.info("Recording uploaded: %s", recording_url)
                    return recording_url
                else:
                    logger.warning("Upload failed: %d", response.status_code)
                    return None

        except Exception:
            logger.exception("Failed to upload recording %s", file_path)
            return None

    def get_local_path(self, uniqueid: str, tracking_did: str) -> str:
        """Формирует путь к локальному файлу записи (как в MixMonitor)."""
        return f"/var/spool/asterisk/monitor/{uniqueid}_{tracking_did}.wav"


recording_service = RecordingService()
