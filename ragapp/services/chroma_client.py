# ragapp/services/chroma_client.py
from __future__ import annotations
from typing import List, Tuple
from ragapp.services.chroma_store import chroma_collection, chroma_count

# ✅ Chroma API 비슷하게 흉내내는 얇은 호환 래퍼
class _CompatCollection:
    name = "sqlite_collection"
    # chroma의 .count()와 유사하게 제공
    def count(self) -> int:
        return chroma_count()
    # add/query는 실제 컬렉션 객체가 처리하므로 이 객체 자체는 최소 역할만

class _CompatClient:
    def list_collections(self) -> List[_CompatCollection]:
        return [_CompatCollection()]

    def get_or_create_collection(self, name: str):
        # 컬렉션 이름은 무시하고 SQLite 단일 컬렉션으로 연결
        return chroma_collection()

    def get_collection(self, name: str):
        return chroma_collection()

_client = _CompatClient()

def get_chroma_client():
    return _client

def list_collections() -> List[Tuple[str, int]]:
    return [("sqlite_collection", chroma_count())]

def get_collection(name: str):
    return chroma_collection()
