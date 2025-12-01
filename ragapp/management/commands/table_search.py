from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from ragapp.services.vertex_embed import embed_texts
from ragapp.services.chroma_media import search_table_by_text_embedding

class Command(BaseCommand):
    help = "텍스트 쿼리로 table_rows 컬렉션 검색"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("query", type=str, help="예: 'APAC 8월 매출'")
        parser.add_argument("--k", type=int, default=10, help="반환 수")

    def handle(self, *args, **opts):
        q = opts["query"]
        k = int(opts["k"])
        qv = embed_texts([q])[0]
        res = search_table_by_text_embedding(text_embedding=qv, k=k)

        ids = res.get("ids", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        docs = res.get("documents", [[]])[0]
        self.stdout.write(self.style.NOTICE(f"검색: '{q}' → top-{k}"))
        for i, (pid, meta, doc) in enumerate(zip(ids, metas, docs), 1):
            table = meta.get("table")
            self.stdout.write(f"{i:>2}. {pid} | table={table} | {doc}")
