$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv-local\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Local environment is missing. Create .venv-local and install requirements first."
}

# Force development-safe settings even when deployment values exist in .env.
$env:DJANGO_DEBUG = "1"
$env:ALLOWED_HOSTS = "localhost,127.0.0.1"
$env:SECURE_SSL_REDIRECT = "0"
$env:SESSION_COOKIE_SECURE = "0"
$env:CSRF_COOKIE_SECURE = "0"
$env:USE_GCS_UPLOADS = "0"
$env:VDB_USE_CHROMA = "0"
$env:SQLITE_DB_PATH = Join-Path $projectRoot ".local-server\db.sqlite3"
$env:VECTOR_DB_PATH = Join-Path $projectRoot ".local-server\vector_store.sqlite3"
$env:MEDIA_ROOT = Join-Path $projectRoot ".local-server\uploads"
$env:CHROMA_MEDIA_DIR = Join-Path $projectRoot ".local-server\chroma_media"
# Local development must not wait indefinitely for unavailable Vertex credentials/network.
# Both indexing and retrieval use the same deterministic local embedding space.
$env:LOCAL_EMBEDDINGS = "1"
$env:RESPECT_ROBOTS = "0"

New-Item -ItemType Directory -Force -Path $env:MEDIA_ROOT | Out-Null

# The repository's bundled Chroma database can be read-only depending on how
# the workspace was unpacked.  Keep a writable local copy for image/table
# indexing and preserve any bundled sample data on the first run.
if (-not (Test-Path -LiteralPath $env:CHROMA_MEDIA_DIR)) {
    $bundledChroma = Join-Path $projectRoot "chroma"
    if (Test-Path -LiteralPath $bundledChroma) {
        Copy-Item -LiteralPath $bundledChroma -Destination $env:CHROMA_MEDIA_DIR -Recurse
    }
    else {
        New-Item -ItemType Directory -Force -Path $env:CHROMA_MEDIA_DIR | Out-Null
    }
}

Push-Location $projectRoot
try {
    & $pythonPath manage.py runserver 127.0.0.1:8000 --noreload
}
finally {
    Pop-Location
}
