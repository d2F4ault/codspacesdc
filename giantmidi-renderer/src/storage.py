from __future__ import annotations

import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List, Tuple

from .config import config

logger = logging.getLogger(__name__)


class StorageProvider(ABC):
    @abstractmethod
    def upload_file(self, local_path: Path) -> str:
        """Upload local file and return destination identifier/URL."""
        pass

    @abstractmethod
    def get_total_usage_bytes(self) -> int:
        """Return total bytes stored under managed prefix."""
        pass

    @abstractmethod
    def prune_oldest(self, target_bytes: int) -> List[str]:
        """Delete oldest files until total storage falls below target_bytes."""
        pass


class S3StorageProvider(StorageProvider):
    """
    S3-compatible storage provider for Cloudflare R2, Backblaze B2, or AWS S3.
    """

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket_name = config.s3_bucket_name
        self.prefix = config.storage_prefix.rstrip("/") + "/" if config.storage_prefix else ""

        boto_cfg = BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
        )

        self.s3 = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint_url,
            aws_access_key_id=config.s3_access_key_id,
            aws_secret_access_key=config.s3_secret_access_key,
            region_name=config.s3_region_name or "auto",
            config=boto_cfg,
        )

    def upload_file(self, local_path: Path) -> str:
        max_bytes = int(config.max_storage_gb * 1024 * 1024 * 1024)
        target_bytes = int(config.prune_target_gb * 1024 * 1024 * 1024)
        file_size = local_path.stat().st_size

        current_usage = self.get_total_usage_bytes()
        logger.info(
            f"Current storage usage: {current_usage / (1024**3):.2f} GB / {config.max_storage_gb:.2f} GB"
        )

        # Prune if the incoming file would breach the storage ceiling
        if current_usage + file_size > max_bytes:
            logger.warning(
                f"Storage ceiling reached ({current_usage / (1024**3):.2f} GB + {file_size / (1024**2):.1f} MB > "
                f"{config.max_storage_gb:.2f} GB). Pruning oldest files..."
            )
            # Reserve headroom for the incoming file so post-upload usage <= target_bytes
            prune_to_bytes = max(0, target_bytes - file_size)
            pruned = self.prune_oldest(prune_to_bytes)
            logger.info(f"Pruned {len(pruned)} old video(s) to maintain free quota headroom.")

        key = f"{self.prefix}{local_path.name}"
        logger.info(f"Uploading {local_path.name} ({file_size / (1024**2):.2f} MB) to s3://{self.bucket_name}/{key}...")

        with open(local_path, "rb") as f:
            self.s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=f,
                ContentType="video/mp4",
            )

        storage_url = f"s3://{self.bucket_name}/{key}"
        logger.info(f"Upload successful: {storage_url}")
        return storage_url

    def get_total_usage_bytes(self) -> int:
        paginator = self.s3.get_paginator("list_objects_v2")
        total = 0
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                total += obj.get("Size", 0)
        return total

    def prune_oldest(self, target_bytes: int) -> List[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        items: List[Tuple[str, int, Any]] = []
        total = 0

        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                size = obj.get("Size", 0)
                total += size
                items.append((obj["Key"], size, obj["LastModified"]))

        if total <= target_bytes:
            return []

        # Sort by modification time ascending (oldest first)
        items.sort(key=lambda x: x[2])
        pruned_keys: List[str] = []

        for key, size, _ in items:
            logger.info(f"Pruning older object: {key} ({size / (1024**2):.1f} MB)")
            try:
                self.s3.delete_object(Bucket=self.bucket_name, Key=key)
                pruned_keys.append(key)
                total -= size
                if total <= target_bytes:
                    break
            except Exception as e:
                logger.error(f"Failed to delete {key}: {e}")

        return pruned_keys


class LocalStorageProvider(StorageProvider):
    """Fallback local storage provider for local testing and validation without cloud keys."""

    def __init__(self) -> None:
        self.out_dir = config.output_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def upload_file(self, local_path: Path) -> str:
        dest = self.out_dir / local_path.name
        if local_path.resolve() != dest.resolve():
            shutil.copy2(str(local_path), str(dest))
        logger.info(f"Stored locally at {dest}")
        return str(dest)

    def get_total_usage_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.out_dir.glob("*.mp4") if f.is_file())

    def prune_oldest(self, target_bytes: int) -> List[str]:
        files = sorted(
            [f for f in self.out_dir.glob("*.mp4") if f.is_file()],
            key=lambda x: x.stat().st_mtime,
        )
        total = sum(f.stat().st_size for f in files)
        pruned = []
        for f in files:
            if total <= target_bytes:
                break
            size = f.stat().st_size
            f.unlink(missing_ok=True)
            pruned.append(f.name)
            total -= size
        return pruned


def get_storage_provider() -> StorageProvider:
    if config.storage_provider == "s3":
        try:
            return S3StorageProvider()
        except Exception as e:
            logger.error(f"Failed to initialize S3 storage: {e}")
            raise
    return LocalStorageProvider()
