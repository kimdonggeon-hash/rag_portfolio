import os, json, sqlite3
from collections import Counter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")

import django
django.setup()

from ragapp.services.vdb_store import vdb_info

info = vdb_info()
db = info.get("path")
print("VDB:", info)
print("DB PATH:", db)

con = sqlite3.connect(db)
cur = con.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("TABLES:", tables)

def pick_text_col(cols):
    for c in ("text", "doc", "document", "content", "body", "chunk"):
        if c in cols:
            return c
    return None

def pick_id_col(cols):
    for c in ("id", "doc_id", "vdb_id", "uid"):
        if c in cols:
            return c
    return None

def try_parse_meta(x):
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", "replace")
        except Exception:
            x = str(x)
    s = str(x)
    # meta가 JSON 문자열로 저장되는 케이스
    try:
        j = json.loads(s)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {"__raw__": s}

candidates = []
for t in tables:
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})").fetchall()]
    if "meta" in cols:
        txt = pick_text_col(cols)
        if txt:
            candidates.append((t, cols, txt, pick_id_col(cols)))

print("\nCANDIDATES(meta+text):")
for t, cols, txt, idc in candidates:
    print(f"- {t}  text_col={txt}  id_col={idc}  cols={cols}")

if not candidates:
    print("\n❌ meta+text 테이블을 못 찾음. sqlite_hybrid_store 스키마가 다를 수 있음.")
    raise SystemExit(0)

# 후보 중 첫 번째를 우선 검사(필요하면 아래에서 바꿔도 됨)
t, cols, text_col, id_col = candidates[0]
print(f"\nUSING TABLE: {t} (text={text_col}, id={id_col})")

# 최근 5000개만 훑어서 source 분포 확인
sel_cols = ["meta", text_col] + ([id_col] if id_col else [])
q = f"SELECT {', '.join(sel_cols)} FROM {t} ORDER BY rowid DESC LIMIT 5000"
rows = cur.execute(q).fetchall()

src_cnt = Counter()
has_file_name = 0

for row in rows:
    meta = try_parse_meta(row[0])
    src = (meta.get("source") or meta.get("source_name") or meta.get("kind") or "").strip() if isinstance(meta, dict) else ""
    if not src:
        src = "(empty)"
    src_cnt[src] += 1
    if isinstance(meta, dict) and (meta.get("file_name") or meta.get("original_name")):
        has_file_name += 1

print("\nSOURCE COUNTS (recent 5000):")
for k, v in src_cnt.most_common():
    print(f"- {k}: {v}")

print("\nfile_name/meta 포함 row 수:", has_file_name)
