# scripts/migrate_chroma_to_sqlite.py
import os
import sys
from pathlib import Path

# ✅ 프로젝트 루트를 파이썬 경로에 추가 (ragsite를 찾게 함)
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import chromadb
from chromadb.config import Settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")
import django
django.setup()

from ragapp.services.chroma_store import chroma_collection
from ragapp.models import RagChunk

# 필요하면 경로 수정: 기존 Chroma 폴더들
PERSIST_DIRS = [Path("chroma_db_new"), Path("chroma_db")]
COLLECTION_NAMES = None  # None이면 모든 컬렉션 이관

def move_one_collection(client, name):
    print(f"[migrate] collection: {name}")
    coll = client.get_or_create_collection(name=name)
    target = chroma_collection()
    LIMIT = 500
    offset = 0
    moved = 0
    while True:
        batch = coll.get(include=["embeddings", "documents", "metadatas"],
                         limit=LIMIT, offset=offset)
        docs = batch.get("documents") or []
        embs = batch.get("embeddings") or []
        metas = batch.get("metadatas") or []
        if not docs:
            break
        target.add(documents=docs, metadatas=metas, embeddings=embs)
        moved += len(docs)
        offset += LIMIT
        print(f"  + moved {moved}")
    print(f"[done] {name}: {moved} chunks")

def main():
    before = RagChunk.objects.count()
    for p in PERSIST_DIRS:
        if not p.exists():
            continue
        print(f"== reading from: {p.resolve()}")
        client = chromadb.PersistentClient(
            settings=Settings(persist_directory=str(p))
        )
        names = COLLECTION_NAMES or [c.name for c in client.list_collections()]
        for n in names:
            move_one_collection(client, n)
    after = RagChunk.objects.count()
    print(f"SQLite rows: {before} -> {after}")

if __name__ == "__main__":
    main()
