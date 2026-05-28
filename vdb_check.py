import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ.get("DJANGO_SETTINGS_MODULE") or "ragsite.settings")

import django
django.setup()

from ragapp.services.vdb_store import vdb_info, vdb_query_hybrid
from ragapp.services.news_services import _embed_texts

print("VDB:", vdb_info())

q = "에이즈"
emb = _embed_texts([q])[0]

res = vdb_query_hybrid(query_text=q, q_emb=emb, topk=10, where=None, vec_topk=20, kw_topk=20)

docs = (res.get("documents") or [[]])[0]
metas = (res.get("metadatas") or [[]])[0]
dists = (res.get("distances") or [[]])[0]
ids_  = (res.get("ids") or [[]])[0]

print("hits:", len(docs))
for i in range(min(10, len(docs))):
    m = metas[i] if i < len(metas) else {}
    src = (m.get("source") or m.get("source_name") or "").strip()
    title = (m.get("title") or m.get("file_name") or m.get("doc_id") or m.get("url") or "").strip()
    dist = dists[i] if i < len(dists) else None
    print(f"\n[{i+1}] dist={dist} source={src} title={title}")
    print((docs[i] or "")[:200].replace("\n", " "))
