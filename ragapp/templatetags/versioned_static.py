# ragapp/templatetags/versioned_static.py
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlencode

from django import template
from django.conf import settings
from django.templatetags.static import static
from django.contrib.staticfiles import finders

try:
    # staticfiles 사용 가능하면 활용 (collectstatic 환경)
    from django.contrib.staticfiles.storage import staticfiles_storage
except Exception:  # pragma: no cover
    staticfiles_storage = None  # type: ignore

register = template.Library()


def _guess_file_mtime(path: str) -> str | None:
    """
    static 경로에서 실제 파일 mtime을 보려고 시도.
    실패하면 None 반환.
    """
    # 1순위: staticfiles_storage.path() (collectstatic 이후)
    if staticfiles_storage is not None:
        try:
            fs_path = staticfiles_storage.path(path)
            return str(int(os.path.getmtime(fs_path)))
        except Exception:
            pass

    # 2순위: BASE_DIR 기준으로 ragapp/static/... 추정
    base_dir = Path(getattr(settings, "BASE_DIR", "."))
    candidates = [
        base_dir / "ragapp" / "static" / path,
        base_dir / "static" / path,
    ]
    for p in candidates:
        try:
            if p.exists():
                return str(int(p.stat().st_mtime))
        except Exception:
            continue

    return None


@register.simple_tag
def versioned_static(path: str, version: str | None = None) -> str:
    """
    사용 예:
      {% versioned_static 'ragapp/javascript/livechat_admin.js' %}
      {% versioned_static 'ragapp/css/news.css' '20251119-ui1' %}

    동작 규칙:
      1) version 인자를 넣으면 그 값을 그대로 ?v=... 로 사용
      2) 없으면 settings.STATIC_VERSION 이 있으면 그 값을 사용
      3) 없고, DEBUG=True 이면 파일 mtime(없으면 현재 시간)으로 사용
      4) 최종적으로도 버전을 못 정하면 그냥 static()만 반환
    """
    base_url = static(path)

    # 1) 템플릿에서 명시적으로 넘겨준 버전이 우선
    v = version

    # 2) 설정에서 전역 버전 지정한 경우 (운영 배포용)
    if not v:
        v = getattr(settings, "STATIC_VERSION", None)

    # 3) 개발 환경(DEBUG=True)에서는 파일 변경 시점 기준으로 자동 버전
    if not v and getattr(settings, "DEBUG", False):
        v = _guess_file_mtime(path) or str(int(time.time()))

    # 4) 그래도 없으면 그냥 원래 URL 반환
    if not v:
        return base_url

    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}v={v}"

@register.simple_tag
def versioned_static(path: str) -> str:
    """
    사용법:
        <script src="{% versioned_static 'ragapp/javascript/livechat_admin.js' %}"></script>

    동작:
        - STATICFILES_FINDERS 로 실제 파일 경로를 찾고
        - mtime(수정시간)을 v=123456789 형식으로 쿼리에 붙여서 반환
        - 파일을 못 찾으면 그냥 static() 결과만 반환
    """
    url = static(path)

    try:
        full_path = finders.find(path)
        if not full_path:
            return url

        mtime = int(os.path.getmtime(full_path))  # 초 단위

        # 이미 ?가 있으면 & 로 붙이고, 없으면 ? 로 시작
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urlencode({'v': mtime})}"
    except Exception:
        # 에러 나면 그냥 평소 static URL로
        return url