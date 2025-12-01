from __future__ import annotations
from django import template

register = template.Library()

@register.filter
def get_item(obj, key):
    """
    템플릿에서 딕셔너리/리스트/객체에서 동적 키로 값 꺼내기.
    사용: {{ row|get_item:col }}
    """
    try:
        if obj is None or key is None:
            return ""

        # 리스트/튜플 인덱싱도 지원
        if isinstance(obj, (list, tuple)):
            try:
                idx = int(key)
            except Exception:
                return ""
            return obj[idx] if 0 <= idx < len(obj) else ""

        # 딕셔너리 키 조회 (문자/숫자 키 모두 시도)
        if isinstance(obj, dict):
            return obj.get(key, obj.get(str(key), ""))

        # 객체 속성 조회
        return getattr(obj, str(key), "")
    except Exception:
        return ""
