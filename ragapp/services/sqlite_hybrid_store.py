# ragapp/services/sqlite_hybrid_store.py
from __future__ import annotations

import json
import logging
import os
import sqlite3
import shutil
import threading
import time
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 0) DB 경로/연결/초기화 + (GCP) remote snapshot restore/sync
# ─────────────────────────────────────────────────────────────


def _normalize_vector_path(raw: str | os.PathLike | None) -> str:
    base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
    if not raw:
        return str(base / "vector_store.sqlite3")

    s = str(raw)
    if any(ord(ch) < 32 for ch in s):
        log.warning("VECTOR_DB_PATH에 제어문자가 포함되어 기본 경로로 되돌립니다: %r", s)
        return str(base / "vector_store.sqlite3")

    try:
        p = Path(os.path.expanduser(os.path.expandvars(s)))
        return str(p.resolve())
    except Exception as e:
        log.warning("VECTOR_DB_PATH 정규화 실패(%s) → 기본 경로 사용: %r", e, s)
        return str(base / "vector_store.sqlite3")


def _normalize_remote_path(raw: str | os.PathLike | None) -> Optional[str]:
    if not raw:
        return None
    s = str(raw)
    if not s.strip():
        return None
    if any(ord(ch) < 32 for ch in s):
        log.warning("VECTOR_DB_REMOTE_PATH에 제어문자가 포함되어 무시합니다: %r", s)
        return None
    try:
        p = Path(os.path.expanduser(os.path.expandvars(s.strip())))
        # remote는 /mnt/gcs 같은 절대경로가 대부분이므로 resolve 실패도 허용
        try:
            return str(p.resolve())
        except Exception:
            return str(p)
    except Exception as e:
        log.warning("VECTOR_DB_REMOTE_PATH 정규화 실패(%s): %r", e, s)
        return None


_VECTOR_DB_PATH = _normalize_vector_path(
    os.environ.get("VECTOR_DB_PATH") or getattr(settings, "VECTOR_DB_PATH", None)
)

_VECTOR_DB_REMOTE_PATH = _normalize_remote_path(
    os.environ.get("VECTOR_DB_REMOTE_PATH") or getattr(settings, "VECTOR_DB_REMOTE_PATH", None)
)

# restore는 기본적으로 "로컬 DB가 없을 때만" 수행
# 필요하면 VECTOR_DB_RESTORE_ALWAYS=1 로 강제 복원
_VECTOR_DB_RESTORE_ALWAYS = str(
    os.environ.get("VECTOR_DB_RESTORE_ALWAYS", getattr(settings, "VECTOR_DB_RESTORE_ALWAYS", "0"))
).lower() in ("1", "true", "yes", "y", "on")


def db_path() -> str:
    return _VECTOR_DB_PATH


def remote_db_path() -> Optional[str]:
    return _VECTOR_DB_REMOTE_PATH


def _safe_copy_best_effort(src: str, dst: str) -> None:
    """
    src -> dst 복사.
    - dst가 /mnt/gcs(gcsfuse)일 수 있어서 os.replace/rename이 실패할 수 있음
    - 가능한 한 원자적으로 하되, 실패 시 copyfile로 폴백
    """
    sp = Path(src)
    dp = Path(dst)

    if not sp.exists():
        raise FileNotFoundError(f"src not found: {src}")

    # 목적지 디렉토리 생성(실패해도 계속 시도)
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    tmp = str(dp) + f".__tmp__{os.getpid()}_{int(time.time() * 1000)}"
    try:
        # 1) tmp로 복사
        try:
            shutil.copy2(str(sp), tmp)
        except Exception:
            shutil.copyfile(str(sp), tmp)

        # 2) 원자적 교체 시도
        os.replace(tmp, str(dp))
        return
    except Exception as e_replace:
        # gcsfuse에서 rename/replace가 막힐 수 있음 → 폴백
        try:
            try:
                if Path(tmp).exists():
                    Path(tmp).unlink(missing_ok=True)  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass

        # 최후 폴백: 목적지에 직접 overwrite copy
        try:
            shutil.copyfile(str(sp), str(dp))
            return
        except Exception as e_copy:
            raise RuntimeError(f"copy failed (replace={e_replace}, copy={e_copy})") from e_copy


_RESTORE_LOCK = threading.Lock()
_RESTORED_ONCE = False
_LAST_RESTORE_CHECK_MONO = 0.0


def restore_db_from_remote_snapshot(force: bool = False) -> bool:
    """
    (GCP) remote snapshot -> local(/tmp) 복원.
    - 기본: 로컬 DB가 없을 때만 복원
    - force=True 또는 VECTOR_DB_RESTORE_ALWAYS=1 이면 무조건 복원 시도
    """
    rp = _VECTOR_DB_REMOTE_PATH
    lp = _VECTOR_DB_PATH

    if not rp:
        return False

    rpp = Path(rp)
    if not rpp.exists():
        return False

    lpp = Path(lp)
    need = force or _VECTOR_DB_RESTORE_ALWAYS or (not lpp.exists()) or (lpp.exists() and lpp.stat().st_size == 0)

    if not need:
        return False

    try:
        # remote -> local 은 local에서 원자적 교체가 가능
        lpp.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        tmp_local = str(lpp) + f".__tmp__{os.getpid()}_{int(time.time() * 1000)}"
        try:
            shutil.copy2(str(rpp), tmp_local)
        except Exception:
            shutil.copyfile(str(rpp), tmp_local)
        os.replace(tmp_local, str(lpp))
        log.info("Vector DB restored from remote snapshot: %s -> %s", rp, lp)
        return True
    except Exception as e:
        log.warning("restore_db_from_remote_snapshot 실패: %s", e)
        return False


def _int_env_at_import(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:
        return default


_RESTORE_RECHECK_SEC = max(0, _int_env_at_import("VECTOR_DB_RESTORE_RECHECK_SEC", 20))


def _remote_newer_than_local() -> bool:
    """remote 스냅샷이 local보다 최신인지(다른 Cloud Run 인스턴스가 새로 썼는지) 확인."""
    rp = _VECTOR_DB_REMOTE_PATH
    lp = _VECTOR_DB_PATH
    if not rp:
        return False
    try:
        rpp, lpp = Path(rp), Path(lp)
        if not rpp.exists():
            return False
        if not lpp.exists():
            return True
        # 여유(1s)를 둬서 동시 mtime 오차로 인한 불필요한 복원을 방지
        return rpp.stat().st_mtime > (lpp.stat().st_mtime + 1.0)
    except Exception:
        return False


def _maybe_restore_once() -> None:
    """
    ✅ Cloud Run은 인스턴스가 여러 개 뜰 수 있는데, 예전엔 프로세스당 딱 한 번만
    remote 스냅샷을 복원했다. 그래서 인스턴스 A가 문서를 업로드해도, 이미 떠 있던
    인스턴스 B는 재시작 전까지 그 문서를 영영 못 봤다(검색에 반영 안 되는 버그의 원인).
    이제는 일정 주기(VECTOR_DB_RESTORE_RECHECK_SEC, 기본 20초)마다 remote가 더
    최신인지 다시 확인해서, 다른 인스턴스가 쓴 변경사항도 따라잡는다.
    """
    global _RESTORED_ONCE, _LAST_RESTORE_CHECK_MONO

    if not _VECTOR_DB_REMOTE_PATH:
        return

    now = time.monotonic()
    if _RESTORED_ONCE and (now - _LAST_RESTORE_CHECK_MONO) < _RESTORE_RECHECK_SEC:
        return

    with _RESTORE_LOCK:
        now = time.monotonic()
        if _RESTORED_ONCE and (now - _LAST_RESTORE_CHECK_MONO) < _RESTORE_RECHECK_SEC:
            return
        _LAST_RESTORE_CHECK_MONO = now

        if not _RESTORED_ONCE:
            restore_db_from_remote_snapshot(force=False)
            _RESTORED_ONCE = True
        elif _remote_newer_than_local():
            restore_db_from_remote_snapshot(force=True)


def sync_db_to_remote_snapshot(conn: Optional[sqlite3.Connection] = None) -> bool:
    """
    (GCP) local(/tmp) -> remote(/mnt/gcs) 스냅샷 동기화.
    - WAL 체크포인트(TRUNCATE)로 -wal 내용을 메인 DB에 반영 후 복사
    - 실패해도 예외를 밖으로 던지지 않게(서비스 안정성 우선)
    """
    rp = _VECTOR_DB_REMOTE_PATH
    lp = _VECTOR_DB_PATH
    if not rp:
        return False

    try:
        # WAL 모드일 수 있으니 체크포인트
        try:
            c = conn or _sqlite_conn()
            c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            try:
                c.commit()
            except Exception:
                pass
        except Exception:
            pass

        _safe_copy_best_effort(lp, rp)
        return True
    except Exception as e:
        log.warning("sync_db_to_remote_snapshot 실패: %s", e)
        return False


# ─────────────────────────────────────────────────────────────
# (GCP) remote snapshot sync throttling (debounce)
# ─────────────────────────────────────────────────────────────


def _int_env(name: str, default: int) -> int:
    v = os.environ.get(name, getattr(settings, name, None))
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


_SYNC_INTERVAL_SEC = max(0, _int_env("VECTOR_DB_SYNC_INTERVAL_SEC", 0))
_SYNC_EVERY_WRITES = max(0, _int_env("VECTOR_DB_SYNC_EVERY_WRITES", 0))

_SYNC_LOCK = threading.Lock()
_LAST_SYNC_MONO = 0.0
_PENDING_WRITES = 0


def maybe_sync_db_to_remote_snapshot(conn: Optional[sqlite3.Connection] = None, *, force: bool = False) -> bool:
    global _LAST_SYNC_MONO, _PENDING_WRITES

    if not _VECTOR_DB_REMOTE_PATH:
        return False

    now = time.monotonic()

    with _SYNC_LOCK:
        _PENDING_WRITES += 1

        if force:
            do_sync = True
        else:
            due_by_time = (_SYNC_INTERVAL_SEC > 0) and ((now - _LAST_SYNC_MONO) >= float(_SYNC_INTERVAL_SEC))
            due_by_writes = (_SYNC_EVERY_WRITES > 0) and (_PENDING_WRITES >= int(_SYNC_EVERY_WRITES))

            if _SYNC_INTERVAL_SEC == 0 and _SYNC_EVERY_WRITES == 0:
                do_sync = True
            else:
                do_sync = bool(due_by_time or due_by_writes)

        if not do_sync:
            return False

        pending_before = _PENDING_WRITES
        _PENDING_WRITES = 0
        _LAST_SYNC_MONO = now

    ok = sync_db_to_remote_snapshot(conn)

    if not ok:
        with _SYNC_LOCK:
            _PENDING_WRITES = max(_PENDING_WRITES, pending_before)
            _LAST_SYNC_MONO = 0.0

    return ok


def _sqlite_conn() -> sqlite3.Connection:
    _maybe_restore_once()

    p = Path(_VECTOR_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(p), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass

    _ensure_schema(conn)
    return conn


_HAS_URL_COL: Optional[bool] = None
_HAS_TS_COL: Optional[bool] = None
_SCHEMA_DONE = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    vector_docs:
      - id/doc/meta_json/emb_json 기본
      - url: canonical key(=url_key) 저장(없으면 "")
      - ts_key: url 없을 때 title+source 키 저장(없으면 "")
    """
    global _SCHEMA_DONE, _HAS_URL_COL, _HAS_TS_COL
    if _SCHEMA_DONE and _HAS_URL_COL is not None and _HAS_TS_COL is not None:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_docs (
            id TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            emb_json  TEXT NOT NULL
        )
        """
    )

    # 컬럼 추가: url, ts_key
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(vector_docs)").fetchall()]
        low = {(c or "").lower() for c in cols}

        if "url" not in low:
            try:
                conn.execute("ALTER TABLE vector_docs ADD COLUMN url TEXT;")
            except Exception:
                pass

        if "ts_key" not in low:
            try:
                conn.execute("ALTER TABLE vector_docs ADD COLUMN ts_key TEXT;")
            except Exception:
                pass

        cols2 = [r[1] for r in conn.execute("PRAGMA table_info(vector_docs)").fetchall()]
        low2 = {(c or "").lower() for c in cols2}

        _HAS_URL_COL = ("url" in low2)
        _HAS_TS_COL = ("ts_key" in low2)

        if _HAS_URL_COL:
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vector_docs_url ON vector_docs(url);")
            except Exception:
                pass
        if _HAS_TS_COL:
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vector_docs_tskey ON vector_docs(ts_key);")
            except Exception:
                pass
    except Exception:
        _HAS_URL_COL = False
        _HAS_TS_COL = False

    # FTS5 (optional)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS vector_docs_fts
            USING fts5(
                id UNINDEXED,
                doc,
                tokenize='unicode61'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vector_docs_id ON vector_docs(id);")
    except Exception as e:
        log.warning("FTS5 테이블 생성 실패(키워드 검색은 LIKE 폴백): %s", e)

    _SCHEMA_DONE = True


def _has_fts(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vector_docs_fts'"
        ).fetchone()
        return bool(row)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# 1) 공통 유틸
# ─────────────────────────────────────────────────────────────


def _cosine_dist(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    sim = dot / (na * nb)
    return 1.0 - float(sim)


def _safe_json_loads(s: str, default):
    try:
        return json.loads(s)
    except Exception:
        return default


def _where_source_ok(meta: Dict[str, Any], where: Optional[Dict[str, Any]]) -> bool:
    if not where or not isinstance(where, dict):
        return True

    src = (meta.get("source") or meta.get("source_name") or "").strip()
    cond = where.get("source")
    if cond is None:
        return True

    if isinstance(cond, dict) and "$in" in cond:
        try:
            return src in [str(x) for x in (cond.get("$in") or [])]
        except Exception:
            return True
    return src == str(cond)


def _norm_space(s: str) -> str:
    return " ".join((s or "").strip().split())


def _norm_title(s: str) -> str:
    # 제목 비교는 공격적으로 하지 말고 최소한만
    return _norm_space(s).lower()


def _norm_source(s: str) -> str:
    return _norm_space(s).lower()


# ─────────────────────────────────────────────────────────────
# URL 후보 수집 + canonical url_key 생성 (핵심)
# ─────────────────────────────────────────────────────────────


def _strip_fragment(u: str) -> str:
    return (u or "").split("#", 1)[0].strip()


def _drop_tracking_params(query: str) -> str:
    """
    '추적용' 파라미터만 제거 (의미있는 id/p 같은 건 유지).
    """
    if not query:
        return ""
    parts = [p for p in query.split("&") if p]
    keep = []
    for p in parts:
        k = p.split("=", 1)[0].strip().lower()
        if not k:
            continue
        if k.startswith("utm_"):
            continue
        if k in ("gclid", "fbclid", "yclid", "mc_cid", "mc_eid", "igshid"):
            continue
        keep.append(p)
    return "&".join(keep)


def _canon_url(url: str) -> str:
    """
    너무 공격적이지 않게 canonical key 생성.
    - fragment 제거
    - host lower
    - default port 제거
    - tracking 파라미터 제거 + 정렬
    - path trailing slash 정리(루트 제외)
    결과는 lower-case 문자열로 반환 (equality 검색용)
    """
    u = _strip_fragment((url or "").strip())
    if not u:
        return ""

    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    except Exception:
        return u.lower()

    try:
        sp = urlsplit(u)
        scheme = (sp.scheme or "").lower()
        netloc = (sp.netloc or "").strip()
        path = sp.path or ""
        query = sp.query or ""

        if not scheme or not netloc:
            # 스킴이 없거나 netloc이 없으면 일단 lower만
            return u.lower()

        # netloc lower + default port 제거
        host = netloc.lower()
        if host.endswith(":80") and scheme == "http":
            host = host[:-3]
        if host.endswith(":443") and scheme == "https":
            host = host[:-4]

        # path 정리 (루트 제외 trailing slash 제거)
        if path != "/" and path.endswith("/"):
            path = path[:-1]

        # query: tracking만 제거하고 남은건 정렬
        query2 = _drop_tracking_params(query)
        if query2:
            qsl = parse_qsl(query2, keep_blank_values=True)
            qsl.sort(key=lambda kv: (kv[0].lower(), kv[1]))
            query2 = urlencode(qsl, doseq=True)

        out = urlunsplit((scheme, host, path, query2, ""))
        return out.lower()
    except Exception:
        return u.lower()


def _url_candidates_from_meta(meta: Dict[str, Any]) -> List[str]:
    """
    URL 후보는 최대한 많이 모으되, 중복판정은 _canon_url() 결과로 한다.
    """
    cand: List[str] = []

    def add(v: Any) -> None:
        if not v:
            return
        s = str(v).strip()
        if s:
            cand.append(s)

    # 흔한 키들
    add(meta.get("final_url"))
    add(meta.get("canonical_url"))
    add(meta.get("og_url"))
    add(meta.get("og:url"))
    add(meta.get("url"))
    add(meta.get("link"))

    # nested 가능성도 조금만
    og = meta.get("og") if isinstance(meta.get("og"), dict) else None
    if isinstance(og, dict):
        add(og.get("url"))

    # 중복 제거(원문 기준) + 길이 제한
    out: List[str] = []
    seen = set()
    for u in cand:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= 10:
            break
    return out


def _url_key_for_meta(meta: Dict[str, Any]) -> str:
    """
    메타에서 후보 URL을 모아서 canonical key 1개를 만든다.
    우선순위: final_url > canonical_url/og_url > url/link
    """
    for raw in _url_candidates_from_meta(meta):
        key = _canon_url(raw)
        if key:
            return key
    return ""


def _ts_key_for_meta(meta: Dict[str, Any]) -> str:
    """
    URL이 없을 때 2차 중복 스킵용: source + title
    """
    title = ""
    source = ""
    try:
        title = str(meta.get("title") or "")
    except Exception:
        title = ""
    try:
        source = str(meta.get("source_name") or meta.get("source") or "")
    except Exception:
        source = ""

    t = _norm_title(title)
    s = _norm_source(source)
    if not t or not s:
        return ""
    return f"{s}|{t}"


# ─────────────────────────────────────────────────────────────
# 2) Upsert
# ─────────────────────────────────────────────────────────────


def upsert_docs(
    ids: List[str],
    docs: List[str],
    metas: List[Dict[str, Any]],
    embs: List[List[float]],
) -> None:
    if not ids or not docs:
        return

    n = min(len(ids), len(docs), len(metas), len(embs))
    ids = ids[:n]
    docs = docs[:n]
    metas = metas[:n]
    embs = embs[:n]

    # url_key/ts_key 생성
    url_keys = [_url_key_for_meta(metas[i] if isinstance(metas[i], dict) else {}) for i in range(n)]
    ts_keys = [_ts_key_for_meta(metas[i] if isinstance(metas[i], dict) else {}) for i in range(n)]

    rows_4 = [
        (ids[i], docs[i], json.dumps(metas[i], ensure_ascii=False), json.dumps(embs[i]))
        for i in range(n)
    ]
    rows_5 = [
        (ids[i], docs[i], json.dumps(metas[i], ensure_ascii=False), json.dumps(embs[i]), url_keys[i])
        for i in range(n)
    ]
    rows_6 = [
        (ids[i], docs[i], json.dumps(metas[i], ensure_ascii=False), json.dumps(embs[i]), url_keys[i], ts_keys[i])
        for i in range(n)
    ]

    with _sqlite_conn() as conn:
        if _HAS_URL_COL and _HAS_TS_COL:
            conn.executemany(
                "REPLACE INTO vector_docs (id, doc, meta_json, emb_json, url, ts_key) VALUES (?, ?, ?, ?, ?, ?)",
                rows_6,
            )
        elif _HAS_URL_COL:
            conn.executemany(
                "REPLACE INTO vector_docs (id, doc, meta_json, emb_json, url) VALUES (?, ?, ?, ?, ?)",
                rows_5,
            )
        else:
            conn.executemany(
                "REPLACE INTO vector_docs (id, doc, meta_json, emb_json) VALUES (?, ?, ?, ?)",
                rows_4,
            )

        if _has_fts(conn):
            conn.executemany("DELETE FROM vector_docs_fts WHERE id = ?", [(i,) for i in ids])
            conn.executemany(
                "INSERT INTO vector_docs_fts (id, doc) VALUES (?, ?)",
                [(ids[i], docs[i]) for i in range(n)],
            )

        maybe_sync_db_to_remote_snapshot(conn)


# ─────────────────────────────────────────────────────────────
# 3) Query
# ─────────────────────────────────────────────────────────────


@dataclass
class Hit:
    id: str
    doc: str
    meta: Dict[str, Any]
    distance: float  # 작을수록 좋음


def query_vector(
    q_emb: List[float],
    topk: int,
    where: Optional[Dict[str, Any]] = None,
) -> List[Hit]:
    topk = max(1, int(topk))

    with _sqlite_conn() as conn:
        rows = list(conn.execute("SELECT id, doc, meta_json, emb_json FROM vector_docs"))

    hits: List[Hit] = []
    for rid, doc, mjson, ejson in rows:
        meta = _safe_json_loads(mjson or "{}", {})
        if not _where_source_ok(meta, where):
            continue
        emb = _safe_json_loads(ejson or "[]", [])
        if not isinstance(emb, list) or not emb:
            continue
        dist = _cosine_dist(q_emb, emb)
        hits.append(Hit(id=str(rid), doc=str(doc), meta=meta, distance=float(dist)))

    hits.sort(key=lambda h: h.distance)
    return hits[:topk]


def query_vector_only(
    *,
    q_emb: List[float],
    topk: int,
    where: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    hits = query_vector(q_emb=q_emb, topk=topk, where=where)
    return {
        "documents": [[h.doc for h in hits]],
        "metadatas": [[h.meta for h in hits]],
        "distances": [[h.distance for h in hits]],
        "ids": [[h.id for h in hits]],
    }


def _fts_query_raw(conn: sqlite3.Connection, query_text: str, limit: int) -> List[Tuple[str, float]]:
    limit = max(1, int(limit))
    q = (query_text or "").strip()
    if not q:
        return []

    try:
        rows = conn.execute(
            """
            SELECT f.id, bm25(f) AS rank
            FROM vector_docs_fts f
            WHERE f MATCH ?
            ORDER BY rank ASC
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()
        return [(str(r[0]), float(r[1])) for r in rows]
    except Exception:
        return []


def query_keyword(
    query_text: str,
    topk: int,
    where: Optional[Dict[str, Any]] = None,
) -> List[Hit]:
    topk = max(1, int(topk))
    q = (query_text or "").strip()
    if not q:
        return []

    with _sqlite_conn() as conn:
        if _has_fts(conn):
            cand = _fts_query_raw(conn, q, limit=topk * 5)
            if not cand:
                return []

            id_list = [c[0] for c in cand]
            placeholders = ",".join("?" for _ in id_list)
            rows = conn.execute(
                f"SELECT id, doc, meta_json FROM vector_docs WHERE id IN ({placeholders})",
                tuple(id_list),
            ).fetchall()

            by_id = {str(r[0]): (str(r[1]), _safe_json_loads(r[2] or "{}", {})) for r in rows}

            hits: List[Hit] = []
            for doc_id, rank in cand:
                if doc_id not in by_id:
                    continue
                doc, meta = by_id[doc_id]
                if not _where_source_ok(meta, where):
                    continue
                hits.append(Hit(id=doc_id, doc=doc, meta=meta, distance=float(rank)))
            return hits[:topk]

        rows = list(conn.execute("SELECT id, doc, meta_json FROM vector_docs"))
        hits2: List[Hit] = []
        for rid, doc, mjson in rows:
            meta = _safe_json_loads(mjson or "{}", {})
            if not _where_source_ok(meta, where):
                continue
            if q in (doc or ""):
                hits2.append(Hit(id=str(rid), doc=str(doc), meta=meta, distance=0.0))
        return hits2[:topk]


def _rrf_fuse(a: List[Hit], b: List[Hit], *, k: int = 60, topk: int = 10) -> List[Hit]:
    topk = max(1, int(topk))

    score: Dict[str, float] = {}
    meta_by_id: Dict[str, Dict[str, Any]] = {}
    doc_by_id: Dict[str, str] = {}

    def add_list(lst: List[Hit]) -> None:
        for rank, h in enumerate(lst, start=1):
            score[h.id] = score.get(h.id, 0.0) + 1.0 / (k + rank)
            meta_by_id[h.id] = h.meta
            doc_by_id[h.id] = h.doc

    add_list(a)
    add_list(b)

    if not score:
        return []

    fused = sorted(score.items(), key=lambda kv: kv[1], reverse=True)[:topk]
    out: List[Hit] = []
    for doc_id, sc in fused:
        dist = 1.0 / (float(sc) + 1e-9)
        out.append(Hit(id=doc_id, doc=doc_by_id.get(doc_id, ""), meta=meta_by_id.get(doc_id, {}), distance=dist))
    out.sort(key=lambda h: h.distance)
    return out


def query_hybrid(
    *,
    query_text: str,
    q_emb: List[float],
    topk: int,
    where: Optional[Dict[str, Any]] = None,
    vec_topk: Optional[int] = None,
    kw_topk: Optional[int] = None,
) -> Dict[str, Any]:
    topk = max(1, int(topk))
    vt = max(1, int(vec_topk or topk))
    kt = max(1, int(kw_topk or topk))

    vec_hits = query_vector(q_emb=q_emb, topk=vt, where=where)
    kw_hits = query_keyword(query_text=query_text, topk=kt, where=where)
    fused = _rrf_fuse(vec_hits, kw_hits, topk=topk)

    return {
        "documents": [[h.doc for h in fused]],
        "metadatas": [[h.meta for h in fused]],
        "distances": [[h.distance for h in fused]],
        "ids": [[h.id for h in fused]],
    }


# ─────────────────────────────────────────────────────────────
# 4) Dedup helpers: url_key -> title+source(ts_key)
# ─────────────────────────────────────────────────────────────


def url_exists(url: str) -> bool:
    """
    canonical url_key 기준 중복 체크.
    """
    ukey = _canon_url(url)
    if not ukey:
        return False

    with _sqlite_conn() as conn:
        # 1) url 컬럼이 있으면 빠른 equality
        if _HAS_URL_COL:
            try:
                row = conn.execute("SELECT 1 FROM vector_docs WHERE url = ? LIMIT 1", (ukey,)).fetchone()
                if row is not None:
                    return True
            except Exception:
                pass

        # 2) 폴백: meta_json substring 후보 → JSON 파싱 확정
        try:
            rows = conn.execute(
                "SELECT meta_json FROM vector_docs WHERE instr(lower(meta_json), ?) > 0 LIMIT 120",
                (ukey,),
            ).fetchall()
            for (mj,) in (rows or []):
                if not mj:
                    continue
                try:
                    meta = json.loads(mj) if isinstance(mj, str) else mj
                    if isinstance(meta, dict):
                        key2 = _url_key_for_meta(meta)
                        if key2 and key2 == ukey:
                            return True
                except Exception:
                    continue
        except Exception:
            pass

    return False


def title_source_exists(title: str, source: str) -> bool:
    """
    URL이 없을 때 2차 중복 체크(ts_key).
    """
    t = _norm_title(title)
    s = _norm_source(source)
    if not t or not s:
        return False
    ts_key = f"{s}|{t}"

    with _sqlite_conn() as conn:
        if _HAS_TS_COL:
            try:
                row = conn.execute("SELECT 1 FROM vector_docs WHERE ts_key = ? LIMIT 1", (ts_key,)).fetchone()
                if row is not None:
                    return True
            except Exception:
                pass

        # 폴백: meta_json substring 후보 → 파싱 확정
        try:
            rows = conn.execute(
                "SELECT meta_json FROM vector_docs WHERE instr(lower(meta_json), ?) > 0 LIMIT 160",
                (t,),
            ).fetchall()
            for (mj,) in (rows or []):
                if not mj:
                    continue
                try:
                    meta = json.loads(mj) if isinstance(mj, str) else mj
                    if not isinstance(meta, dict):
                        continue
                    ts2 = _ts_key_for_meta(meta)
                    if ts2 and ts2 == ts_key:
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    return False


def vector_dedup_exists(meta: Dict[str, Any]) -> bool:
    """
    최종 통합 중복판정:
      1) url_key 있으면 url_exists(url)
      2) url_key 없으면 title+source(ts_key)
    """
    if not isinstance(meta, dict):
        return False

    url_key = _url_key_for_meta(meta)
    if url_key:
        # url_exists는 raw url을 받도록 되어있지만,
        # 여기서는 이미 canonical key를 가졌으니 그대로 우회 체크:
        with _sqlite_conn() as conn:
            if _HAS_URL_COL:
                try:
                    row = conn.execute("SELECT 1 FROM vector_docs WHERE url = ? LIMIT 1", (url_key,)).fetchone()
                    return row is not None
                except Exception:
                    pass
        # 안전 폴백
        return url_exists(meta.get("final_url") or meta.get("canonical_url") or meta.get("url") or "")

    # URL이 없으면 title+source
    title = str(meta.get("title") or "")
    source = str(meta.get("source_name") or meta.get("source") or "")
    return title_source_exists(title, source)
