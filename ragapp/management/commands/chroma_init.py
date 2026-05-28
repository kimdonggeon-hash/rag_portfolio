# ragapp/management/commands/chroma_init.py
from __future__ import annotations

from datetime import datetime
from django.core.management.base import BaseCommand

from ragapp.services import chroma_store as CS
from ragapp.services.vertex_embed import embed_texts


class Command(BaseCommand):
    help = "Chroma 벡터 DB 초기화(시드 문서 2개 추가) - 모델/차원 분리 컬렉션 기준"

    def handle(self, *args, **kwargs):
        # ✅ 실제 컬렉션(모델+차원 분리) 기준으로 작업
        col = CS.chroma_collection()

        base = getattr(CS.settings, "CHROMA_COLLECTION", "chroma_default")
        actual = getattr(col, "name", str(base))
        db_dir = CS.settings.CHROMA_DB_DIR

        # (선택) 디버깅용: 현재 임베딩 모델/차원
        try:
            embed_model = CS._embed_model_name()  # 내부 유틸이지만 진단/운영 편의상 사용
        except Exception:
            embed_model = ""
        try:
            embed_dim = CS._want_embed_dim()
        except Exception:
            embed_dim = None

        self.stdout.write(
            self.style.NOTICE(
                f"Dir={db_dir}\n"
                f"Collection(base)={base}\n"
                f"Collection(actual)={actual}\n"
                f"EmbedModel={embed_model or '(unknown)'}\n"
                f"EmbedDim={embed_dim if embed_dim is not None else '(unknown)'}"
            )
        )

        # ✅ 시드 문서 2개 (upsert라서 여러 번 돌려도 안전)
        now = datetime.utcnow().isoformat()

        ids = ["seed:minimal:1", "seed:minimal:2"]
        docs = [
            "Seed document #1 (minimal). This collection is used for RAG retrieval tests.",
            "Seed document #2 (minimal). If you see this, Chroma upsert + embeddings work.",
        ]
        metas = [
            {"source": "seed", "title": "Chroma Seed 1", "ingested_at": now},
            {"source": "seed", "title": "Chroma Seed 2", "ingested_at": now},
        ]

        # 임베딩 생성 → 업서트
        embs = embed_texts(docs)
        CS.chroma_upsert(ids=ids, docs=docs, metas=metas, embs=embs)

        # ✅ actual 컬렉션 기준 count
        cnt = CS.chroma_count(col)

        self.stdout.write(self.style.SUCCESS(f"Inserted(upserted): {len(ids)}"))
        self.stdout.write(self.style.SUCCESS(f"Collection(actual): {actual}"))
        self.stdout.write(self.style.SUCCESS(f"Count now(actual): {cnt}"))
