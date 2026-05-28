#!/usr/bin/env sh
set -e

PY="${PY:-python}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

REMOTE="${CHROMA_MEDIA_REMOTE_DIR:-/mnt/gcs/ragdata/chroma_media_v2}"
LOCAL="${CHROMA_MEDIA_DIR:-/tmp/chroma_media}"
COLL="${CHROMA_IMAGES_COLLECTION:-media_images_v2}"

echo "[job] PY=$PY"
echo "[job] REMOTE=$REMOTE"
echo "[job] LOCAL=$LOCAL"
echo "[job] COLL=$COLL"

mkdir -p "$LOCAL"

# 1) remote -> local (있으면)
if [ -d "$REMOTE" ]; then
  cp -r "$REMOTE"/. "$LOCAL"/ || true
fi

# 2) tmp 찌꺼기 제거 (gcsfuse/중단 작업 흔적)
rm -f "$LOCAL"/chroma.sqlite3.__tmp__* || true

# 3) 로컬에서 작업하도록 강제
export CHROMA_MEDIA_DIR="$LOCAL"
export CHROMA_IMAGES_COLLECTION="$COLL"

# reindex + meta backfill (필요한 커맨드만 유지)
"$PY" manage.py reindex_gcs_images --prefix "uploads/images/" --limit 2000
"$PY" manage.py backfill_image_meta --skip-deleted --collection "$COLL" --chroma-dir "$LOCAL"

# 4) local -> remote
mkdir -p "$REMOTE"
cp -r "$LOCAL"/. "$REMOTE"/ || true

echo "[job] done"
