from __future__ import annotations

import re
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_URL_RE = re.compile(r"(https?://[^\s<]+)", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(\w+)?\n([\s\S]*?)```", re.MULTILINE)

def _linkify(s: str) -> str:
    def repl(m):
        url = m.group(1)
        u = escape(url)
        return (
            f'<a href="{u}" target="_blank" rel="noopener noreferrer nofollow" '
            f'referrerpolicy="strict-origin-when-cross-origin">{u}</a>'
        )
    return _URL_RE.sub(repl, s)

@register.filter(name="board_render")
def board_render(text: str) -> str:
    """
    - 기본은 전부 escape
    - ```code``` 블록은 <pre><code>로 변환 (안전)
    - 나머지는 링크 자동 + <br>
    """
    raw = text or ""
    # 먼저 code fence를 토큰화
    parts = []
    last = 0
    for m in _FENCE_RE.finditer(raw):
        parts.append(("text", raw[last:m.start()]))
        lang = (m.group(1) or "").strip()
        code = m.group(2) or ""
        parts.append(("code", (lang, code)))
        last = m.end()
    parts.append(("text", raw[last:]))

    out = []
    for kind, payload in parts:
        if kind == "code":
            lang, code = payload
            c = escape(code)
            cls = f"language-{escape(lang)}" if lang else ""
            out.append(f'<pre class="board-code"><code class="{cls}">{c}</code></pre>')
        else:
            t = escape(payload)
            t = _linkify(t)
            t = t.replace("\n", "<br>")
            out.append(t)

    return mark_safe("".join(out))
