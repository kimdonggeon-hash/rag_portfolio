# ragapp/management/commands/vdb_to_ragchunk.py
from __future__ import annotations

import json
import sqlite3
import hashlib
from array import array
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from ragapp.models import RagChunk


def _default_vdb_path() -> str:
    """
    vdb_store.py와 동일한 우선순위를 따라 벡터 SQLite(vector_store.sqlite3) 경로를 잡는다.
    """
    p = getattr(settings, "VECTOR_DB_PATH", None) or getattr(settings, "VDB_PATH", None)
    if not p:
        p = str(Path(getattr(settings, "BASE_DIR", Path.cwd())) / "sqlite3" / "vector_store.sqlite3")
    return str(p)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _to_f32_bytes(vec: Iterable[Any]) -> bytes:
    # numpy 없이도 float32 bytes로 안전하게 직렬화
    f = array("f", (float(x) for x in vec))
    return f.tobytes()


class Command(BaseCommand):
    help = "vector_store.sqlite3(embeddings 테이블) 데이터를 Django RagChunk 모델로 미러(복사)합니다."

    def add_arguments(self, parser):
        parser.add_argument("--vdb-path", default="", help="벡터 SQLite 경로 (기본: settings.VECTOR_DB_PATH)")
        parser.add_argument("--batch-size", type=int, default=500, help="배치 처리 크기 (기본 500)")
        parser.add_argument("--limit", type=int, default=0, help="최대 처리 개수 (0이면 전체)")
        parser.add_argument("--dry-run", action="store_true", help="실제 저장 없이 개수만 점검")
        parser.add_argument("--clear-first", action="store_true", help="시작 전에 RagChunk를 전부 삭제하고 다시 적재(주의)")
        parser.add_argument("--verbose-meta", action="store_true", help="meta에 _vdb_* 정보를 조금 더 넣음")

    def handle(self, *args, **opts):
        vdb_path = (opts.get("vdb_path") or "").strip() or _default_vdb_path()
        vdb_path = str(Path(vdb_path).expanduser().resolve())

        batch_size = max(10, int(opts.get("batch_size") or 500))
        limit = int(opts.get("limit") or 0)
        dry_run = bool(opts.get("dry_run"))
        clear_first = bool(opts.get("clear_first"))
        verbose_meta = bool(opts.get("verbose_meta"))

        if not Path(vdb_path).exists():
            raise CommandError(f"벡터 SQLite 파일이 없습니다: {vdb_path}")

        # vdb(embeddings) 총량 확인
        conn = sqlite3.connect(vdb_path)
        try:
            cur = conn.cursor()
            # 테이블 존재 확인
            try:
                cur.execute("SELECT 1 FROM embeddings LIMIT 1;")
            except Exception as e:
                raise CommandError(f"embeddings 테이블을 찾지 못했습니다. 스키마를 확인하세요. ({e})")

            cur.execute("SELECT COUNT(*) FROM embeddings;")
            total = int(cur.fetchone()[0] or 0)
        finally:
            conn.close()

        self.stdout.write(self.style.SUCCESS(f"[vdb_to_ragchunk] vdb_path={vdb_path} embeddings_count={total}"))
        if total <= 0:
            self.stdout.write(self.style.WARNING("embeddings 테이블이 비어 있습니다. (인덱싱이 아직 안 된 상태일 수 있어요)"))
            return

        if dry_run:
            self.stdout.write(self.style.SUCCESS("dry-run: 저장은 하지 않습니다."))
            return

        if clear_first:
            deleted, _ = RagChunk.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"clear-first: RagChunk {deleted}건 삭제 완료"))

        # 실제 이관
        conn = sqlite3.connect(vdb_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, doc, meta, embedding, dim, updated_at FROM embeddings;")

            attempted = 0
            made_total = 0

            while True:
                rows: List[Tuple[Any, Any, Any, Any, Any, Any]] = cur.fetchmany(batch_size)
                if not rows:
                    break

                objs: List[RagChunk] = []
                for (rid, doc, meta_s, emb_s, dim, updated_at) in rows:
                    if limit and attempted >= limit:
                        break

                    attempted += 1

                    vdb_id = (rid or "").strip() if isinstance(rid, str) else str(rid or "").strip()
                    if not vdb_id:
                        continue

                    text = doc if isinstance(doc, str) else ("" if doc is None else str(doc))

                    # meta
                    meta: Dict[str, Any] = {}
                    if isinstance(meta_s, str) and meta_s.strip():
                        try:
                            meta = json.loads(meta_s) if meta_s.strip().startswith(("{", "[")) else {}
                            if not isinstance(meta, dict):
                                meta = {"_raw_meta": meta}
                        except Exception:
                            meta = {"_raw_meta": meta_s}
                    elif meta_s is not None:
                        meta = {"_raw_meta": meta_s}

                    # embedding
                    vec: List[float] = []
                    if isinstance(emb_s, str) and emb_s.strip():
                        try:
                            raw = json.loads(emb_s)
                            if isinstance(raw, list):
                                vec = [float(x) for x in raw]
                        except Exception:
                            vec = []

                    if not vec:
                        # 임베딩이 없으면 RagChunk로도 의미가 없어서 스킵
                        continue

                    dim_i = int(dim or len(vec) or 0)
                    if dim_i <= 0:
                        continue

                    emb_bytes = _to_f32_bytes(vec)

                    title = str(meta.get("title") or meta.get("source") or "")[:500]
                    url = str(meta.get("url") or meta.get("final_url") or "")
                    doc_id = str(meta.get("doc_id") or vdb_id)[:191]

                    if verbose_meta:
                        meta = dict(meta)
                        meta["_vdb_id"] = vdb_id
                        meta["_vdb_updated_at"] = updated_at

                    objs.append(
                        RagChunk(
                            unique_hash=_sha256_hex(vdb_id),  # ✅ idempotent key
                            doc_id=doc_id,
                            url=url,
                            title=title,
                            text=text,
                            meta=meta,
                            embedding=emb_bytes,
                            dim=dim_i,
                        )
                    )

                if not objs:
                    if limit and attempted >= limit:
                        break
                    continue

                with transaction.atomic():
                    # ✅ unique_hash 충돌(이미 들어간 건) 자동 스킵 → 재실행해도 안전
                    RagChunk.objects.bulk_create(objs, ignore_conflicts=True)

                made_total += len(objs)

                if limit and attempted >= limit:
                    break

            after = RagChunk.objects.count()
            self.stdout.write(
                self.style.SUCCESS(
                    f"완료: 시도 {attempted}건 / 배치생성(시도) {made_total}건 / RagChunk 현재 총 {after}건"
                )
            )
            self.stdout.write("Django Admin에서 /admin/ragapp/ragchunk/ 확인해 보세요.")
        finally:
            conn.close()
