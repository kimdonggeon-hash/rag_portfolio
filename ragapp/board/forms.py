# ragapp/board/forms.py
from __future__ import annotations

import os
from django import forms
from .models import BoardPost, BoardComment

# ✅ PII guard
from ragapp.pii import guard_text, summarize_hits

_ALLOWED_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_IMG_BYTES = 3 * 1024 * 1024  # 3MB

REPORT_REASONS = [
    ("spam", "스팸/광고"),
    ("abuse", "욕설/혐오/폭력"),
    ("illegal", "불법/위험 정보"),
    ("privacy", "개인정보 노출"),
    ("other", "기타"),
]


class BoardReportForm(forms.Form):
    reason = forms.ChoiceField(
        label="사유",
        choices=REPORT_REASONS,
        widget=forms.Select(attrs={"class": "board-select"}),
    )
    message = forms.CharField(
        label="추가 설명(선택)",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "추가 설명(선택)",
                "class": "board-textarea",
            }
        ),
    )

    # bot honeypot
    website = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ✅ id 중복 방지: 신고 모달 전용 id로 고정 (name=website 등 필드명은 그대로 유지)
        id_map = {
            "website": "id_report_website",
            "reason": "id_report_reason",
            "message": "id_report_message",
        }
        for field_name, new_id in id_map.items():
            f = self.fields.get(field_name)
            if not f:
                continue
            attrs = dict(getattr(f.widget, "attrs", {}) or {})
            attrs["id"] = new_id
            f.widget.attrs = attrs


class GuestNamePasswordForm(forms.Form):
    guest_name = forms.CharField(
        label="비회원 이름",
        max_length=60,
        widget=forms.TextInput(attrs={
            "class": "board-control",
            "autocomplete": "off",
            "placeholder": "작성할 때 입력한 이름",
        }),
    )
    password = forms.CharField(
        label="비회원 비밀번호",
        max_length=128,
        widget=forms.PasswordInput(attrs={
            "class": "board-control",
            "autocomplete": "current-password",
            "placeholder": "작성할 때 입력한 비밀번호",
        }),
    )


class GuestPasswordForm(forms.Form):
    password = forms.CharField(
        label="비밀번호",
        widget=forms.PasswordInput(attrs={"placeholder": "작성 시 입력한 비밀번호"}),
        max_length=64,
    )


class BoardPostWriteForm(forms.ModelForm):
    guest_name = forms.CharField(
        label="닉네임",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"placeholder": "닉네임 (비회원)", "autocomplete": "off"}),
    )
    guest_password = forms.CharField(
        label="비밀번호",
        required=False,
        max_length=64,
        widget=forms.PasswordInput(attrs={"placeholder": "비회원 비밀번호", "autocomplete": "new-password"}),
    )

    # ✅ bot honeypot (id만 폼별로 유니크)
    website = forms.CharField(required=False, widget=forms.HiddenInput(attrs={"id": "id_post_website"}))

    # ✅ (보너스) 운영자만 이미지 1장 첨부
    attachment = forms.FileField(required=False, label="첨부 이미지(운영자 전용)")

    class Meta:
        model = BoardPost
        # ✅ is_secret 추가 (작성 시 “비밀글(운영자만 보기)” 선택 가능)
        fields = (
            "category",
            "title",
            "body",
            "is_secret",
            "pinned",
            "allow_comments",
            "is_published",
            "attachment",
        )
        labels = {
            "category": "카테고리",
            "title": "제목",
            "body": "내용",
            "is_secret": "비밀글",
            "pinned": "상단 고정",
            "allow_comments": "댓글 허용",
            "is_published": "공개",
            "attachment": "첨부 이미지(운영자 전용)",  # 모델 라벨보다 폼 라벨 우선
        }
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "제목", "autocomplete": "off"}),
            "body": forms.Textarea(attrs={"placeholder": "내용을 입력하세요", "rows": 16}),
            # ✅ 체크박스(원하면 class만)
            "is_secret": forms.CheckboxInput(attrs={"class": "board-checkbox"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user

        # ✅ 라벨 뒤에 붙는 ":" 제거 (Pinned: 같은 콜론 제거)
        self.label_suffix = ""

        is_auth = bool(user and getattr(user, "is_authenticated", False))
        is_staff = bool(is_auth and (user.is_staff or user.is_superuser))

        # ✅ is_secret 라벨/헬프텍스트(선택)
        if "is_secret" in self.fields:
            self.fields["is_secret"].required = False
            self.fields["is_secret"].label = "비밀글"
            # 템플릿에서 따로 도움말 문구를 넣고 있으면 굳이 안 넣어도 됨
            # self.fields["is_secret"].help_text = "체크하면 운영자만 볼 수 있어요."

        if not is_staff:
            # 일반/비회원: 운영 필드 숨김 + 첨부 금지 (비밀글은 허용)
            for f in ("pinned", "allow_comments", "is_published", "attachment"):
                self.fields.pop(f, None)
        else:
            # 운영자: guest 입력 숨김
            self.fields["guest_name"].widget = forms.HiddenInput()
            self.fields["guest_password"].widget = forms.HiddenInput()

            # ✅ 혹시 모델 verbose_name이 영문이더라도 강제로 한글 라벨 고정
            if "pinned" in self.fields:
                self.fields["pinned"].label = "상단 고정"
            if "allow_comments" in self.fields:
                self.fields["allow_comments"].label = "댓글 허용"
            if "is_published" in self.fields:
                self.fields["is_published"].label = "공개"
            if "attachment" in self.fields:
                self.fields["attachment"].label = "첨부 이미지(운영자 전용)"
            if "is_secret" in self.fields:
                self.fields["is_secret"].label = "비밀글"

        # ✅ 혹시 외부에서 widget 재정의가 있었어도 id는 유지(안전)
        if "website" in self.fields:
            attrs = dict(getattr(self.fields["website"].widget, "attrs", {}) or {})
            attrs.setdefault("id", "id_post_website")
            self.fields["website"].widget.attrs = attrs

    def clean(self):
        cleaned = super().clean()

        if (cleaned.get("website") or "").strip():
            raise forms.ValidationError("잘못된 요청입니다.")

        user = self._user
        is_auth = bool(user and getattr(user, "is_authenticated", False))
        is_staff = bool(is_auth and (user.is_staff or user.is_superuser))

        if not is_staff and not is_auth:
            name = (self.data.get("guest_name") or "").strip()
            pw = (self.data.get("guest_password") or "").strip()
            if len(name) < 2:
                self.add_error("guest_name", "닉네임은 2글자 이상으로 입력해 주세요.")
            if len(pw) < 4:
                self.add_error("guest_password", "비밀번호는 4글자 이상으로 입력해 주세요.")

        title = (cleaned.get("title") or "").strip()
        body = (cleaned.get("body") or "").strip()
        if len(title) < 2:
            self.add_error("title", "제목은 2글자 이상으로 입력해 주세요.")
        if len(body) < 5:
            self.add_error("body", "내용은 5글자 이상으로 입력해 주세요.")

        # ✅ PII 입력 방지: 일반/비회원만 차단
        if not is_staff:
            ok_t, hits_t = guard_text(title)
            if not ok_t:
                self.add_error("title", summarize_hits(hits_t))

            ok_b, hits_b = guard_text(body)
            if not ok_b:
                self.add_error("body", summarize_hits(hits_b))

        # ✅ 운영자 첨부 이미지 검증(확장자/용량)
        if is_staff:
            f = cleaned.get("attachment")
            if f:
                ext = os.path.splitext(getattr(f, "name", "") or "")[1].lower()
                if ext not in _ALLOWED_IMG_EXT:
                    self.add_error("attachment", "이미지 파일만 허용됩니다(png/jpg/gif/webp).")
                size = getattr(f, "size", 0) or 0
                if size > _MAX_IMG_BYTES:
                    self.add_error("attachment", "이미지는 3MB 이하만 허용됩니다.")

        return cleaned


class BoardCommentWriteForm(forms.ModelForm):
    guest_name = forms.CharField(
        label="닉네임",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"placeholder": "닉네임 (비회원)", "autocomplete": "off"}),
    )
    guest_password = forms.CharField(
        label="비밀번호",
        required=False,
        max_length=64,
        widget=forms.PasswordInput(attrs={"placeholder": "비회원 비밀번호", "autocomplete": "new-password"}),
    )

    # honeypot (id만 유니크)
    website = forms.CharField(required=False, widget=forms.HiddenInput(attrs={"id": "id_comment_website"}))

    class Meta:
        model = BoardComment
        fields = ("body",)
        labels = {"body": "댓글"}
        widgets = {"body": forms.Textarea(attrs={"placeholder": "댓글을 입력하세요", "rows": 4})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user
        self.label_suffix = ""

        # ✅ 혹시 외부에서 widget 재정의가 있었어도 id는 유지(안전)
        if "website" in self.fields:
            attrs = dict(getattr(self.fields["website"].widget, "attrs", {}) or {})
            attrs.setdefault("id", "id_comment_website")
            self.fields["website"].widget.attrs = attrs

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get("website") or "").strip():
            raise forms.ValidationError("잘못된 요청입니다.")

        user = self._user
        is_auth = bool(user and getattr(user, "is_authenticated", False))
        is_staff = bool(is_auth and (user.is_staff or user.is_superuser))

        if not is_staff and not is_auth:
            name = (self.data.get("guest_name") or "").strip()
            pw = (self.data.get("guest_password") or "").strip()
            if len(name) < 2:
                self.add_error(None, "비회원 댓글은 닉네임(2글자+)이 필요해요.")
            if len(pw) < 4:
                self.add_error(None, "비회원 댓글은 비밀번호(4글자+)가 필요해요.")

        body = (cleaned.get("body") or "").strip()
        if len(body) < 1:
            self.add_error("body", "댓글 내용을 입력해 주세요.")

        # ✅ PII 입력 방지: 일반/비회원만 차단
        if not is_staff:
            ok, hits = guard_text(body)
            if not ok:
                self.add_error("body", summarize_hits(hits))

        return cleaned


class GuestCredentialForm(forms.Form):
    guest_name = forms.CharField(
        label="닉네임",
        max_length=20,
        widget=forms.TextInput(attrs={"placeholder": "작성할 때 입력한 닉네임", "autocomplete": "off"}),
    )
    password = forms.CharField(
        label="비밀번호",
        max_length=64,
        widget=forms.PasswordInput(attrs={"placeholder": "작성 시 입력한 비밀번호", "autocomplete": "current-password"}),
    )
