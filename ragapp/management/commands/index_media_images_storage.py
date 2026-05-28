# ragapp/management/commands/index_media_images_storage.py

from __future__ import annotations

import os
import tempfile
from typing import Iterable, List, Optional
from pathlib import PurePosixPath

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandParser

from ragapp.services.vertex_embed import embed_image_file
from ragapp.services.chroma_media import add_image_item

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}


def _is_image_key(key: str) -> bool:
    suf = PurePosixPath(key).suffix.lower()
    return suf in IMAGE_EXTS


def _iter_storage_keys(prefix: str) -> Iterable[str]:
    """
    default_storage에서 prefix 아래 파일들을 재귀적으로 나열.
    - GoogleCloudStorage(django-storages)면 bucket.list_blobs(prefix=...)가 가장 확실/빠름
    - 아니면 listdir 기반 재귀로 폴백
    """
    prefix = (prefix or "").lstrip("/")

    # 1) GCS 백엔드면 list_blobs 사용
    bucket = getattr(default_storage, "bucket", None)
    if bucket is not None:
        for blob in bucket.list_blobs(prefix=prefix):
            name = (blob.name or "").strip()
            if not name:
                continue
            if name.endswith("/"):
                continue
            yield name
        return

    # 2) 일반 백엔드 폴백: listdir 재귀
    stack = [prefix]
    while stack:
        cur = stack.pop()
        try:
            dirs, files = default_storage.listdir(cur)
        except Exception:
            continue

        for f in files:
            key = f"{cur.rstrip('/')}/{f}".lstrip("/")
            yield key

        for d in dirs:
            nxt = f"{cur.rstrip('/')}/{d}".lstrip("/")
            stack.append(nxt)


class Command(BaseCommand):
    help = "GCS/스토리지(prefix) 아래 이미지를 순회하며 Chroma(media_images)에 인덱싱합니다."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--prefix",
            type=str,
            default="images/",
            help="스토리지 키 prefix (예: images/ 또는 uploads/images/)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="테스트용 최대 처리 개수(0이면 전체)",
        )
        parser.add_argument(
            "--caption-from-name",
            action="store_true",
            help="파일명을 캡션으로 사용",
        )

    def handle(self, *args, **opts):
        prefix = (opts["prefix"] or "images/").strip().lstrip("/")
        limit = int(opts["limit"] or 0)
        caption_from_name = bool(opts["caption_from_name"])

        total = 0
        ok = 0

        for key in _iter_storage_keys(prefix):
            if not _is_image_key(key):
                continue

            total += 1
            if limit > 0 and total > limit:
                break

            try:
                # 스토리지에서 읽어 /tmp 임시파일로 저장 후 임베딩
                with default_storage.open(key, "rb") as f:
                    data = f.read()

                name = PurePosixPath(key).name
                stem = PurePosixPath(key).stem
                suf = PurePosixPath(key).suffix.lower() or ".jpg"

                with tempfile.NamedTemporaryFile(prefix="img_", suffix=suf, delete=True) as tmp:
                    tmp.write(data)
                    tmp.flush()
                    vec = embed_image_file(tmp.name)

                pid = add_image_item(
                    path=key,  # ✅ 반드시 "스토리지 키"
                    embedding=vec,
                    caption=(stem if caption_from_name else ""),
                    original_name=name,
                )
                ok += 1
                self.stdout.write(self.style.SUCCESS(f"[+]{pid}  ({key})"))

            except Exception as e:
                self.stderr.write(self.style.WARNING(f"[skip]{key}: {e}"))

        self.stdout.write(self.style.NOTICE(f"완료: {ok}/{total} 파일 인덱싱 (prefix={prefix})"))
