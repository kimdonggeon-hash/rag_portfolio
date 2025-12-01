import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

EXCLUDE_DIRS = {
    ".venv", "portfolio", "venv", "env",
    "migrations", "__pycache__", ".git", ".history",
    "Lib", "Scripts", "Include", "site-packages",
}

# ✅ Django ORM에 가까운 패턴만: ModelClass.objects.xxx(...)
ORM = re.compile(
    r"\b[A-Z]\w*\.objects\.(get|filter|first|exists|all|exclude|create|update|delete)\(",
    re.I,
)

# ✅ "DB 접근"에 가까운 패턴만 잡기 (오탐 줄이기)
# - connection.cursor()/connections['x'].cursor()
# - cursor.execute() 같은 형태
# - call_command(...) (특히 migrate/collectstatic 등)
DB2 = re.compile(
    r"""
    \b(connection|connections)\b\s*(?:\[[^\]]+\])?\s*\.\s*cursor\s*\(   # connection.cursor(
    | \.\s*execute\s*\(                                                # .execute(
    | \bcall_command\s*\(                                              # call_command(
    """,
    re.IGNORECASE | re.VERBOSE,
)

READY_DEF = re.compile(r"def\s+ready\s*\(", re.I)


def is_in_venv(p: Path) -> bool:
    """
    상위 폴더 중 pyvenv.cfg가 있으면 가상환경 내부로 간주
    """
    for parent in [p] + list(p.parents)[:15]:
        if (parent / "pyvenv.cfg").exists():
            return True
    return False


def is_excluded(p: Path) -> bool:
    # 경로 구성 요소에 제외 디렉토리명이 포함되면 제외
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return True
    # 혹시 이름이 특이한 venv도 pyvenv.cfg로 자동 제외
    if is_in_venv(p.parent):
        return True
    return False


def scan_file(p: Path):
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return

    # 1) 모듈 최상단(첫 def/class 전)에서 "DB 접근 패턴" 찾기
    for i, line in enumerate(lines, 1):
        if re.match(r"^\s*(def|class)\s+", line):
            break

        s = line.strip()
        if not s or s.startswith("#"):
            continue

        # (선택) import 라인은 노이즈가 많으면 아예 제외해도 됨
        # if s.startswith("import ") or s.startswith("from "):
        #     continue

        if DB2.search(line):
            print(f"\n[TOPLEVEL DB2] {p} : L{i}\n  {s}")

    # 2) apps.py 의 ready() 근처에서 ORM 호출 찾기
    if p.name == "apps.py":
        start = None
        for i, line in enumerate(lines, 1):
            if READY_DEF.search(line):
                start = i
                break

        if start:
            end = min(start + 200, len(lines))
            for j in range(start, end):
                s = lines[j].strip()
                if not s or s.startswith("#"):
                    continue
                if ORM.search(lines[j]):
                    print(f"\n[APPS READY ORM] {p} : L{j+1}\n  {s}")


def main():
    for p in ROOT.rglob("*.py"):
        # ✅ 스캐너 자신 제외 (가장 중요)
        if p.resolve() == SELF:
            continue
        if is_excluded(p):
            continue
        scan_file(p)


if __name__ == "__main__":
    main()
