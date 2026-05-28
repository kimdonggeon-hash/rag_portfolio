# ragapp/management/commands/reindex_gcs_images.py
from __future__ import annotations

import os
import re
import sys
import hashlib
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.conf import settings

# google-cloud-storage
try:
    from google.cloud import storage  # type: ignore
except Exception:
    storage = None  # type: ignore

from ragapp.services.chroma_media import images_coll
from ragapp.services.vertex_embed import embed_image_file

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")


def _sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _is_image_blob(name: str, content_type: str = "") -> bool:
    n = (name or "").lower()
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return True
    return any(n.endswith(ext) for ext in IMAGE_EXTS)


def _mktemp_path(suffix: str = ".img") -> str:
    """
    Windows 안전: NamedTemporaryFile을 '열린 핸들'로 유지하면 download_to_filename이 실패할 수 있어,
    mkstemp()로 파일 경로만 만든 뒤 fd를 닫고 그 경로를 사용한다.
    """
    fd, p = tempfile.mkstemp(suffix=suffix)
    try:
        os.close(fd)
    except Exception:
        pass
    return p


def _detect_collection_dim(coll: Any) -> Optional[int]:
    """
    Chroma 컬렉션에서 embeddings 1개를 읽어 차원을 추정.
    (버전별로 include=['embeddings'] 지원이 다를 수 있어 예외는 무시)
    """
    try:
        got = coll.get(include=["embeddings"], limit=1)
        embs = (got.get("embeddings") or [])
        if embs and isinstance(embs, list) and isinstance(embs[0], list):
            return len(embs[0])
    except Exception:
        return None
    return None


class Command(BaseCommand):
    help = "Reindex images from GCS (uploads/images/) into Chroma image collection (media_images, 1408-dim)."

    def add_arguments(self, parser):
        parser.add_argument("--bucket", default="", help="GCS bucket name (default: settings.GS_BUCKET_NAME)")
        parser.add_argument("--prefix", default="uploads/images/", help="GCS prefix to scan (default: uploads/images/)")
        parser.add_argument("--limit", type=int, default=0, help="Max number of images to scan (0 = no limit)")
        parser.add_argument("--batch", type=int, default=12, help="Upsert batch size (default: 12)")
        parser.add_argument("--dry-run", action="store_true", help="List/prepare only; do not upsert")
        parser.add_argument("--skip-existing", action="store_true", help="Skip if id already exists in collection")
        parser.add_argument("--force-dim", type=int, default=1408, help="Expected embedding dim (default: 1408)")
        parser.add_argument("--media-url", default="", help="MEDIA_URL override (default: settings.MEDIA_URL or /uploads/)")
        parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    def handle(self, *args, **opts):
        if storage is None:
            raise RuntimeError("google-cloud-storage 패키지가 필요합니다. pip install google-cloud-storage")

        bucket = (opts.get("bucket") or "").strip() or getattr(settings, "GS_BUCKET_NAME", "")
        prefix = (opts.get("prefix") or "uploads/images/").strip().lstrip("/")
        limit = int(opts.get("limit") or 0)
        batch = int(opts.get("batch") or 12)
        dry_run = bool(opts.get("dry_run"))
        skip_existing = bool(opts.get("skip_existing"))
        expected_dim = int(opts.get("force_dim") or 1408)
        verbose = bool(opts.get("verbose"))

        media_url = (opts.get("media_url") or "").strip()
        if not media_url:
            media_url = (getattr(settings, "MEDIA_URL", "") or "/uploads/").strip()
        if not media_url.endswith("/"):
            media_url += "/"

        if not bucket:
            raise RuntimeError("bucket 이름이 비어있습니다. --bucket 또는 settings.GS_BUCKET_NAME을 확인하세요.")

        # 1) Chroma 이미지 컬렉션 고정
        coll = images_coll()
        coll_name = getattr(coll, "name", None) or getattr(coll, "_name", None) or "unknown"

        # 2) 기존 컬렉션 차원 검사(가능하면)
        existing_dim = _detect_collection_dim(coll)
        if existing_dim is not None and existing_dim != expected_dim:
            raise RuntimeError(
                f"Chroma 컬렉션({coll_name})의 기존 임베딩 차원={existing_dim} 이고 "
                f"이번 재인덱싱 기대 차원={expected_dim} 입니다.\n"
                f"→ 컬렉션을 잘못 잡았거나(384 컬렉션), 예전에 다른 차원으로 만들어진 컬렉션입니다.\n"
                f"→ CHROMA_IMAGES_COLLECTION=media_images로 고정하고, 필요하면 해당 컬렉션을 비우고 다시 만드세요."
            )

        self.stdout.write(
            f"[reindex] bucket={bucket} prefix={prefix} limit={limit or 'ALL'} batch={batch}\n"
            f"[reindex] MEDIA_URL={media_url} (used to build url meta)\n"
            f"[reindex] chroma_collection={coll_name} current_count={coll.count()}"
        )

        # 3) GCS 목록
        client = storage.Client()
        bkt = client.bucket(bucket)

        scanned = 0
        upserted = 0

        ids: List[str] = []
        embs: List[List[float]] = []
        metas: List[Dict[str, Any]] = []
        docs: List[str] = []

        def flush():
            nonlocal upserted
            if not ids:
                return
            if dry_run:
                self.stdout.write(f"[dry-run] would upsert +{len(ids)} (sample_id={ids[0]})")
            else:
                coll.upsert(ids=ids, embeddings=embs, metadatas=metas, documents=docs)
                upserted += len(ids)
                self.stdout.write(f"[upsert] +{len(ids)} (total={upserted})")
            ids.clear()
            embs.clear()
            metas.clear()
            docs.clear()

        # iterator
        for blob in client.list_blobs(bucket, prefix=prefix):
            if limit and scanned >= limit:
                break

            name = (blob.name or "").strip()
            if not name:
                continue

            if not _is_image_blob(name, getattr(blob, "content_type", "") or ""):
                continue

            # name: uploads/images/2026/01/xxx.png
            # storage_key: images/2026/01/xxx.png  (uploads/ 제거)
            rel = name[len("uploads/"):] if name.startswith("uploads/") else name
            rel = rel.lstrip("/")

            # 네 메타 규칙에 맞게 images/... 만 남기고 싶으면 여기서 한번 더 정규화
            # (현재 reindex 결과는 images/... 형태로 잘 나오고 있으니 이 방식 유지)
            storage_key = rel  # images/...
            path = rel

            # id는 안정적으로: storage_key 기반 해시 + 파일크기(있으면)
            size = int(getattr(blob, "size", 0) or 0)
            pid = f"img:{_sha256(storage_key)}:{size}"

            # skip-existing 옵션
            if skip_existing:
                try:
                    got = coll.get(ids=[pid], include=[])
                    if (got.get("ids") or []):
                        if verbose:
                            self.stdout.write(f"[skip] exists pid={pid} key={storage_key}")
                        scanned += 1
                        continue
                except Exception:
                    pass

            # 다운로드 (Windows 안전)
            suffix = Path(name).suffix or ".img"
            tmp_path = _mktemp_path(suffix=suffix)
            try:
                blob.download_to_filename(tmp_path)
            except Exception as e:
                # 실패하면 tmp 정리
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                self.stderr.write(f"[warn] download failed key={name}: {e}")
                scanned += 1
                continue

            # 임베딩
            try:
                vec = embed_image_file(tmp_path, mime=getattr(blob, "content_type", None), dim=expected_dim)
                if len(vec) != expected_dim:
                    raise RuntimeError(f"embedding dim mismatch: got={len(vec)} expected={expected_dim}")
            except Exception as e:
                self.stderr.write(f"[warn] embed failed key={name}: {e}")
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                scanned += 1
                continue
            finally:
                # 임시파일 삭제
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

            filename = Path(storage_key).name
            url = f"{media_url}{storage_key}".replace("//", "/")
            if not url.startswith("/"):
                url = "/" + url

            meta: Dict[str, Any] = {
                "kind": "image",
                "source": "gcs",
                "filename": filename,
                "path": path,
                "filepath": path,
                "storage_key": storage_key,
                "url": url,
            }

            ids.append(pid)
            embs.append(vec)
            metas.append(meta)
            docs.append(filename)

            scanned += 1
            if verbose:
                self.stdout.write(f"[scan] {scanned} pid={pid} url={url}")

            if len(ids) >= batch:
                flush()

        flush()
        self.stdout.write(f"[done] scanned={scanned} upserted={upserted} final_count={coll.count()}")
