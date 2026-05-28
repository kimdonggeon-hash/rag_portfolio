from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.core.management.base import BaseCommand

try:
    import chromadb
except Exception:
    chromadb = None  # type: ignore


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _build_search_text(doc: str, meta: Dict[str, Any]) -> str:
    parts: List[str] = []
    if doc:
        parts.append(str(doc))

    for k in ("caption", "tags", "orig_name", "original_name", "orig_stem", "basename", "title", "name"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())

    p = meta.get("path") or meta.get("filepath")
    if isinstance(p, str) and p.strip():
        parts.append(Path(p.strip()).name)

    seen = set()
    out: List[str] = []
    for x in parts:
        nx = _norm(x)
        if not nx or nx in seen:
            continue
        seen.add(nx)
        out.append(nx)
    return " ".join(out)


class Command(BaseCommand):
    help = (
        "Backfill image metadatas in Chroma safely.\n"
        "- Default: update metadatas only (NO documents update) to avoid re-embedding/dimension mismatch.\n"
        "- Supports Cloud Run Jobs sharding via CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not update, only print summary.")
        parser.add_argument("--collection", default="media_images", help="Chroma collection name (default: media_images)")
        parser.add_argument("--chroma-dir", default=os.environ.get("CHROMA_MEDIA_DIR", "chroma_media"))
        parser.add_argument("--page", type=int, default=500, help="Get page size (limit) for streaming reads.")
        parser.add_argument("--batch", type=int, default=200, help="Update batch size.")
        parser.add_argument("--skip-deleted", action="store_true", help="Skip ids listed in meta/deleted_image_ids.json")

        # ⚠️ 기본 False: documents 업데이트는 임베딩 차원 문제를 유발할 수 있으니 opt-in
        parser.add_argument(
            "--update-documents",
            action="store_true",
            help="Also update documents field (may trigger embedding dimension checks). Use only if you know what you're doing.",
        )

    def handle(self, *args, **opts):
        if chromadb is None:
            raise RuntimeError("chromadb가 설치되어 있어야 합니다. (pip install chromadb)")

        chroma_dir = opts["chroma_dir"]
        col_name = opts["collection"]
        dry = bool(opts["dry_run"])
        page = max(1, int(opts["page"]))
        batch = max(1, int(opts["batch"]))
        update_documents = bool(opts["update_documents"])

        # Cloud Run Jobs 샤딩 (Task count > 1일 때만)
        # 각 Task는 자기 구간(offset range)만 처리
        task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0") or "0")
        task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1") or "1")
        if task_count < 1:
            task_count = 1
        if task_index < 0:
            task_index = 0
        if task_index >= task_count:
            task_index = 0

        deleted_ids = set()
        if opts["skip_deleted"]:
            try:
                from django.core.files.storage import default_storage
                key = "meta/deleted_image_ids.json"
                if default_storage.exists(key):
                    with default_storage.open(key, "rb") as f:
                        data = json.loads(f.read().decode("utf-8"))
                    if isinstance(data, list):
                        deleted_ids = {str(x) for x in data}
            except Exception:
                deleted_ids = set()

        client = chromadb.PersistentClient(path=chroma_dir)

        # get_or_create는 컬렉션이 없을 때 새로 만들 수 있으니,
        # 이름 오타 방지 차원에서 get_collection 우선 시도
        try:
            col = client.get_collection(name=col_name)
        except Exception:
            col = client.get_or_create_collection(name=col_name)

        try:
            total = int(col.count())
        except Exception:
            # count가 실패하면 최소한 한 페이지라도 읽어보는 쪽으로 진행
            total = 0

        # 샤딩 범위 계산
        # total이 0이면(알 수 없으면) 전체 스캔 모드로 동작
        if total > 0 and task_count > 1:
            start = (total * task_index) // task_count
            end = (total * (task_index + 1)) // task_count
        else:
            start, end = 0, total if total > 0 else 10**18  # 사실상 끝까지

        self.stdout.write(
            f"[cfg] collection={col_name} chroma_dir={chroma_dir} dry_run={dry} "
            f"page={page} batch={batch} update_documents={update_documents} "
            f"task_index={task_index} task_count={task_count} range=[{start},{end}) total={total}"
        )

        updated = 0
        skipped = 0
        scanned = 0

        update_ids: List[str] = []
        update_metas: List[Dict[str, Any]] = []
        update_docs: List[str] = []

        def flush_updates():
            nonlocal updated
            if not update_ids:
                return
            if dry:
                updated += len(update_ids)
                update_ids.clear()
                update_metas.clear()
                update_docs.clear()
                return

            if update_documents:
                col.update(ids=update_ids, metadatas=update_metas, documents=update_docs)
            else:
                # ✅ 핵심: documents를 건드리지 않음 (차원 불일치 회피)
                col.update(ids=update_ids, metadatas=update_metas)

            updated += len(update_ids)
            update_ids.clear()
            update_metas.clear()
            update_docs.clear()

        offset = start
        while offset < end:
            limit = page
            if end < 10**18:
                limit = min(limit, end - offset)
            if limit <= 0:
                break

            got = col.get(include=["metadatas", "documents"], limit=limit, offset=offset)
            ids: List[str] = list(got.get("ids") or [])
            metas: List[Dict[str, Any]] = list(got.get("metadatas") or [])
            docs: List[str] = list(got.get("documents") or [])

            if not ids:
                break

            # 길이 보정
            if len(metas) < len(ids):
                metas += [{} for _ in range(len(ids) - len(metas))]
            if len(docs) < len(ids):
                docs += ["" for _ in range(len(ids) - len(docs))]

            for pid, meta0, doc0 in zip(ids, metas, docs):
                scanned += 1
                spid = str(pid)

                if deleted_ids and spid in deleted_ids:
                    skipped += 1
                    continue

                meta = dict(meta0 or {})
                doc0 = doc0 or ""

                path = (meta.get("path") or meta.get("filepath") or "").strip()
                if not path:
                    skipped += 1
                    continue

                basename = Path(path).name
                stem = Path(basename).stem

                changed = False

                # "비어있을 때만" 채우기
                if not meta.get("basename"):
                    meta["basename"] = basename
                    changed = True
                if not (meta.get("orig_name") or meta.get("original_name")):
                    meta["orig_name"] = basename
                    changed = True
                if not meta.get("orig_stem"):
                    meta["orig_stem"] = stem
                    changed = True

                # doc_new는 기본적으로 metadata 기반으로만 활용 (documents 업데이트는 opt-in)
                doc_new = doc0
                if not doc_new.strip():
                    c = meta.get("caption")
                    if isinstance(c, str) and c.strip():
                        doc_new = c.strip()
                    else:
                        doc_new = stem

                st0 = meta.get("search_text")
                if not (isinstance(st0, str) and st0.strip() and len(st0.strip()) >= 6):
                    meta["search_text"] = _build_search_text(doc_new, meta)
                    changed = True

                # documents를 업데이트할 때만 변경 체크
                if update_documents and doc_new != doc0:
                    changed = True

                if not changed:
                    continue

                update_ids.append(spid)
                update_metas.append(meta)
                if update_documents:
                    update_docs.append(doc_new)

                if len(update_ids) >= batch:
                    flush_updates()

            offset += len(ids)

        flush_updates()

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. scanned={scanned}, updated={updated}, skipped={skipped}, dry_run={dry}, "
                f"documents_updated={update_documents}"
            )
        )
