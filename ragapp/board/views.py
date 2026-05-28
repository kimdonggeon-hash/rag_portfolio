# ragapp/board/views.py
from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import (
    BoardCommentWriteForm,
    BoardPostWriteForm,
    BoardReportForm,
    GuestPasswordForm,        # (다른 템플릿/뷰에서 쓸 수 있어 유지)
    GuestNamePasswordForm,
)
from .models import BoardCategory, BoardComment, BoardPost, BoardReport
from .ratelimit import client_fingerprint, hit, replay_guard
from .utils import build_fp_from_request


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────

def _is_staff(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _allow_guest_posts() -> bool:
    return bool(getattr(settings, "BOARD_ALLOW_GUEST_POSTS", True))


def _post_unlock_key(pk: int) -> str:
    return f"board:post_unlock:{pk}"


def _comment_unlock_key(pk: int) -> str:
    return f"board:comment_unlock:{pk}"


def _base_post_qs(request):
    """
    ✅ 기본 접근 제어 쿼리셋
    - 운영자: 전체
    - 일반: 게시됨 + 미삭제 (※ 비밀글도 목록에 "제목만" 노출시키기 위해 포함)
      → 상세/본문 열람은 PostDetailView에서 별도 게이트(비밀번호 인증)
    """
    qs = BoardPost.objects.select_related("category").all()
    u = getattr(request, "user", None)
    if not _is_staff(u):
        qs = qs.filter(is_published=True, is_deleted=False)
    return qs


def _categories_for_sidebar(request):
    qs = BoardPost.objects.all()
    if not _is_staff(getattr(request, "user", None)):
        qs = qs.filter(is_published=True, is_deleted=False)

    counts = qs.values("category").annotate(c=Count("id"))
    mapping = {row["category"]: row["c"] for row in counts}

    cats = list(BoardCategory.objects.all())
    out = []
    for c in cats:
        out.append({
            "name": c.name,
            "slug": c.slug,
            "count": int(mapping.get(c.id, 0)),
            "is_notice": bool(c.is_notice),
        })
    return out


def _can_view_post(request, post: BoardPost) -> bool:
    """
    ✅ 비밀글 열람 규칙 (LG식)
    - 운영자: 항상 열람
    - 비밀글이 아니면: 열람
    - 비밀글이면:
        1) 회원 작성글(author_id 있음): 작성자 본인만(로그인) / 운영자
        2) 비회원 작성글(author_id 없음): 세션 unlock(비번 인증 성공) / 운영자
    """
    u = getattr(request, "user", None)
    if _is_staff(u):
        return True

    if not getattr(post, "is_secret", False):
        return True

    # 회원 작성 비밀글: 작성자만
    if getattr(post, "author_id", None):
        return bool(u and getattr(u, "is_authenticated", False) and post.author_id == u.id)

    # 비회원 작성 비밀글: unlock 세션 필요
    return bool(request.session.get(_post_unlock_key(post.pk)))


def _safe_next_or_empty(request, nxt: str) -> str:
    nxt = (nxt or "").strip()
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return ""


def _redirect_to_post_auth_for_view(request, post: BoardPost):
    """
    비밀글 열람을 위한 인증 화면으로 이동.
    next에 현재 URL을 담아서 인증 성공 시 다시 돌아오게 한다.
    """
    params = urlencode({
        "mode": "view",
        "next": request.get_full_path(),
    })
    return redirect(f"{reverse('board:post_auth', kwargs={'pk': post.pk})}?{params}")


# ─────────────────────────────────────────────────────────────
# list / detail
# ─────────────────────────────────────────────────────────────

class PostListView(ListView):
    model = BoardPost
    template_name = "ragapp/board/post_list.html"
    context_object_name = "posts"
    paginate_by = 10

    def get_queryset(self):
        qs = _base_post_qs(self.request)

        cat = (self.request.GET.get("cat") or "").strip()
        if cat:
            qs = qs.filter(category__slug=cat)

        q = (self.request.GET.get("q") or "").strip()
        if q:
            u = getattr(self.request, "user", None)
            if _is_staff(u):
                qs = qs.filter(Q(title__icontains=q) | Q(body__icontains=q) | Q(guest_name__icontains=q))
            else:
                # ✅ 일반/비회원: 비밀글은 본문 검색 금지 (제목/작성자명만), 일반글은 본문 검색 OK
                qs = qs.filter(
                    Q(title__icontains=q)
                    | Q(guest_name__icontains=q)
                    | (Q(body__icontains=q) & Q(is_secret=False))
                )

        # ✅ 공지 분리: (필터 없을 때만) 본문 리스트에서는 공지 제외
        if not cat and not q:
            qs = qs.exclude(category__is_notice=True)

        return qs.order_by("-pinned", "-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["categories"] = _categories_for_sidebar(self.request)
        ctx["cat_active"] = (self.request.GET.get("cat") or "").strip()
        ctx["allow_guest_posts"] = _allow_guest_posts()
        ctx["tab"] = "all"
        ctx["is_staff"] = _is_staff(getattr(self.request, "user", None))

        # ✅ 상단 공지 섹션
        q = (self.request.GET.get("q") or "").strip()
        cat = (self.request.GET.get("cat") or "").strip()
        if not q and not cat:
            notice_qs = (
                _base_post_qs(self.request)
                .filter(category__is_notice=True)
                .order_by("-pinned", "-created_at")[:5]
            )
            ctx["notices"] = list(notice_qs)
        else:
            ctx["notices"] = []

        # ✅ page 이동해도 q/cat 유지하기 (page만 제거)
        qd = self.request.GET.copy()
        qd.pop("page", None)
        ctx["qparams"] = qd.urlencode()
        return ctx


class PostDetailView(DetailView):
    model = BoardPost
    template_name = "ragapp/board/post_detail.html"
    context_object_name = "post"

    def get_queryset(self):
        return _base_post_qs(self.request)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        post: BoardPost = self.object
        u = getattr(request, "user", None)

        # ✅ 비밀글이면: 허용 사용자 아니면 인증 화면(게스트 비밀글) or 404(회원 비밀글 타인)
        if getattr(post, "is_secret", False) and not _can_view_post(request, post):
            # 회원 작성 비밀글은 작성자/운영자만 (타인은 인증으로 못 품)
            if getattr(post, "author_id", None) and not _is_staff(u):
                raise Http404
            # 비회원 작성 비밀글은 인증 화면으로
            return _redirect_to_post_auth_for_view(request, post)

        # ✅ 열람 허용 후에만 조회수 증가
        BoardPost.objects.filter(pk=post.pk).update(view_count=F("view_count") + 1)
        post.refresh_from_db(fields=["view_count"])

        context = self.get_context_data(object=post)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        post: BoardPost = ctx["post"]
        user = getattr(self.request, "user", None)

        can_edit = False
        if _is_staff(user):
            can_edit = True
        elif post.author_id and user and user.is_authenticated and post.author_id == user.id:
            can_edit = True
        elif post.is_guest and self.request.session.get(_post_unlock_key(post.pk)):
            can_edit = True

        ctx["can_edit"] = can_edit
        ctx["is_staff"] = _is_staff(user)
        ctx["allow_guest_posts"] = _allow_guest_posts()
        ctx["categories"] = _categories_for_sidebar(self.request)

        cqs = BoardComment.objects.filter(post=post)
        if not _is_staff(user):
            cqs = cqs.filter(is_hidden=False, is_deleted=False)
        ctx["comments"] = list(cqs.order_by("created_at"))

        ctx["comment_form"] = BoardCommentWriteForm(user=user)
        ctx["report_form"] = BoardReportForm()
        return ctx


# ─────────────────────────────────────────────────────────────
# create / update
# ─────────────────────────────────────────────────────────────

class PostCreateView(CreateView):
    model = BoardPost
    form_class = BoardPostWriteForm
    template_name = "ragapp/board/post_form.html"

    def dispatch(self, request, *args, **kwargs):
        u = getattr(request, "user", None)
        # ✅ 게스트 글쓰기 OFF여도 로그인 사용자는 허용 (비회원만 차단)
        if not _allow_guest_posts() and not (u and getattr(u, "is_authenticated", False)):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw["user"] = getattr(self.request, "user", None)
        return kw

    def form_valid(self, form):
        user = getattr(self.request, "user", None)
        fp = client_fingerprint(self.request)

        blocked, _ = hit(f"post:hour:{fp}", limit=5, window_sec=3600)
        if blocked:
            form.add_error(None, "글 작성이 너무 빠릅니다. 잠시 후 다시 시도해 주세요.")
            return self.form_invalid(form)

        blocked2, _ = hit(f"post:burst:{fp}", limit=1, window_sec=20)
        if blocked2:
            form.add_error(None, "연속 요청이 감지되었습니다. 20초 정도 후 다시 시도해 주세요.")
            return self.form_invalid(form)

        title = (form.cleaned_data.get("title") or "").strip()
        body = (form.cleaned_data.get("body") or "").strip()
        h = hashlib.sha1(f"{fp}|{title}|{body}".encode("utf-8")).hexdigest()
        if replay_guard("post", h, ttl_sec=300):
            form.add_error(None, "같은 내용이 반복 제출된 것 같아요. 잠시 후 다시 시도해 주세요.")
            return self.form_invalid(form)

        obj: BoardPost = form.save(commit=False)
        obj.creator_fp = build_fp_from_request(self.request)

        # ✅ 공지 카테고리는 운영자만 (악용 차단)
        if obj.category and getattr(obj.category, "is_notice", False) and not _is_staff(user):
            form.add_error("category", "공지 카테고리는 운영자만 작성할 수 있습니다.")
            return self.form_invalid(form)

        if user and user.is_authenticated:
            obj.author = user
            obj.guest_name = ""
            obj.guest_pw_hash = ""
        else:
            obj.author = None
            obj.guest_name = (self.request.POST.get("guest_name") or "").strip()
            pw = (self.request.POST.get("guest_password") or "").strip()

            if not obj.guest_name:
                form.add_error(None, "비회원 이름을 입력해 주세요.")
                return self.form_invalid(form)
            if not pw:
                form.add_error(None, "비회원 비밀번호를 입력해 주세요.")
                return self.form_invalid(form)

            obj.set_guest_password(pw)

        # ✅ 일반 유저는 pin/첨부 조작 불가
        if not _is_staff(user):
            obj.pinned = False
            obj.allow_comments = True
            obj.is_published = True
            obj.is_deleted = False
            obj.deleted_at = None
            obj.attachment = None
        else:
            if obj.category and getattr(obj.category, "is_notice", False):
                obj.pinned = True

        obj.save()

        # ✅ 비회원이 "비밀글"을 올리면 바로 본인 세션에서 열람 가능하게 unlock (LG식 UX)
        if getattr(obj, "is_secret", False) and (not (user and user.is_authenticated)):
            self.request.session[_post_unlock_key(obj.pk)] = True
            try:
                self.request.session.set_expiry(60 * 60)  # 1h
            except Exception:
                pass

        return redirect("board:detail", pk=obj.pk)


class PostUpdateView(UpdateView):
    model = BoardPost
    form_class = BoardPostWriteForm
    template_name = "ragapp/board/post_form.html"

    def dispatch(self, request, *args, **kwargs):
        post = get_object_or_404(BoardPost, pk=kwargs.get("pk"))
        user = getattr(request, "user", None)

        # 삭제된 글은 운영자만
        if getattr(post, "is_deleted", False) and not _is_staff(user):
            raise Http404

        if _is_staff(user):
            return super().dispatch(request, *args, **kwargs)

        # ✅ 비밀글(회원 작성): 작성자만 수정 가능
        if getattr(post, "is_secret", False) and getattr(post, "author_id", None):
            if user and user.is_authenticated and post.author_id == user.id:
                return super().dispatch(request, *args, **kwargs)
            raise Http404

        # 일반 글 or 비회원 글(비밀 포함): 기존 로직대로
        if post.author_id and user and user.is_authenticated and post.author_id == user.id:
            return super().dispatch(request, *args, **kwargs)

        if post.is_guest and request.session.get(_post_unlock_key(post.pk)):
            return super().dispatch(request, *args, **kwargs)

        if post.author_id:
            raise Http404

        # 비회원 글(세션 unlock 없음) → auth로
        return redirect(
            f"{reverse('board:post_auth', kwargs={'pk': post.pk})}"
            f"?{urlencode({'next': request.get_full_path()})}"
        )

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw["user"] = getattr(self.request, "user", None)
        return kw

    def form_valid(self, form):
        user = getattr(self.request, "user", None)
        obj: BoardPost = form.save(commit=False)

        # ✅ 공지 카테고리는 운영자만
        if obj.category and getattr(obj.category, "is_notice", False) and not _is_staff(user):
            form.add_error("category", "공지 카테고리는 운영자만 설정할 수 있습니다.")
            return self.form_invalid(form)

        if not _is_staff(user):
            obj.pinned = False
            obj.allow_comments = True
            obj.is_published = True
            obj.attachment = None
        else:
            if obj.category and getattr(obj.category, "is_notice", False):
                obj.pinned = True

        obj.save()
        return redirect("board:detail", pk=obj.pk)


# ─────────────────────────────────────────────────────────────
# guest auth (unlock session)
# ─────────────────────────────────────────────────────────────

def post_auth(request, pk: int):
    post = get_object_or_404(BoardPost, pk=pk)

    # 회원 작성글은 guest auth로 풀 수 없음
    if post.author_id:
        raise Http404

    # ✅ post_auth 브루트포스 방지(가벼운 레이트리밋)
    fp = client_fingerprint(request)
    if hit(f"postauth:burst:{fp}", limit=1, window_sec=2)[0]:
        raise Http404
    if hit(f"postauth:minute:{fp}", limit=12, window_sec=60)[0]:
        raise Http404

    mode = (request.GET.get("mode") or request.POST.get("mode") or "").strip()  # view | edit
    nxt = (request.GET.get("next") or request.POST.get("next") or "").strip()
    nxt = _safe_next_or_empty(request, nxt)

    if request.method == "POST":
        form = GuestNamePasswordForm(request.POST)
        if form.is_valid():
            name = (form.cleaned_data.get("guest_name") or "").strip()
            pw = (form.cleaned_data.get("password") or "").strip()

            if name != (post.guest_name or ""):
                form.add_error("guest_name", "이름이 일치하지 않습니다.")
            elif not post.check_guest_password(pw):
                form.add_error("password", "비밀번호가 맞지 않습니다.")
            else:
                request.session[_post_unlock_key(post.pk)] = True
                try:
                    request.session.set_expiry(60 * 60)
                except Exception:
                    pass

                if nxt:
                    return redirect(nxt)

                # mode가 view면 상세로, edit이면 수정으로 보내는 게 UX가 자연스러움
                if mode == "edit":
                    return redirect("board:edit", pk=post.pk)
                return redirect("board:detail", pk=post.pk)
    else:
        form = GuestNamePasswordForm()

    title = "비밀글 열람" if mode == "view" else "비회원 글 수정/삭제"

    return render(request, "ragapp/board/auth.html", {
        "form": form,
        "kind": "post",
        "title": title,
        "target": post,
        "next": nxt,
        "mode": mode,
        "categories": _categories_for_sidebar(request),
        "allow_guest_posts": _allow_guest_posts(),
    })


def comment_auth(request, comment_id: int):
    c = get_object_or_404(BoardComment, pk=comment_id)

    # ✅ 댓글이 달린 글이 비밀글이면: 열람권한(운영자/작성자/세션unlock) 있어야만
    p = get_object_or_404(BoardPost, pk=c.post_id)
    if getattr(p, "is_secret", False) and not _can_view_post(request, p):
        raise Http404

    if c.author_id:
        raise Http404

    nxt = (request.GET.get("next") or request.POST.get("next") or "").strip()
    nxt = _safe_next_or_empty(request, nxt)

    if request.method == "POST":
        form = GuestNamePasswordForm(request.POST)
        if form.is_valid():
            name = (form.cleaned_data.get("guest_name") or "").strip()
            pw = (form.cleaned_data.get("password") or "").strip()

            if name != (c.guest_name or ""):
                form.add_error("guest_name", "이름이 일치하지 않습니다.")
            elif not c.check_guest_password(pw):
                form.add_error("password", "비밀번호가 맞지 않습니다.")
            else:
                request.session[_comment_unlock_key(c.pk)] = True
                try:
                    request.session.set_expiry(60 * 60)
                except Exception:
                    pass

                if nxt:
                    return redirect(nxt)
                return redirect("board:detail", pk=c.post_id)
    else:
        form = GuestNamePasswordForm()

    return render(request, "ragapp/board/auth.html", {
        "form": form,
        "kind": "comment",
        "title": "비회원 댓글 수정/삭제",
        "target": c,
        "next": nxt,
        "categories": _categories_for_sidebar(request),
        "allow_guest_posts": _allow_guest_posts(),
    })


@require_http_methods(["GET", "POST"])
def comment_update(request, comment_id: int):
    c = get_object_or_404(BoardComment.objects.select_related("post"), pk=comment_id)
    post = get_object_or_404(_base_post_qs(request), pk=c.post_id)

    # ✅ 비밀글 댓글 수정은 글 열람권한이 있어야 가능
    if getattr(post, "is_secret", False) and not _can_view_post(request, post):
        raise Http404

    user = getattr(request, "user", None)
    is_staff = _is_staff(user)
    is_owner = bool(c.author_id and user and getattr(user, "is_authenticated", False) and c.author_id == user.id)
    guest_unlocked = bool(c.is_guest and request.session.get(_comment_unlock_key(c.pk)))

    if getattr(c, "is_deleted", False) and not is_staff:
        raise Http404

    if not (is_staff or is_owner or c.is_guest):
        raise Http404

    needs_guest_auth = bool(c.is_guest and (not is_staff) and (not guest_unlocked) and (not is_owner))
    err = ""

    if request.method == "POST":
        body = (request.POST.get("body") or "").strip()
        if not body:
            err = "내용을 입력해 주세요."

        if not err and needs_guest_auth:
            gn = (request.POST.get("guest_name") or "").strip()
            pw = (request.POST.get("guest_pw") or request.POST.get("guest_password") or "").strip()

            if not gn or not pw:
                err = "비회원 이름/비밀번호를 입력해 주세요."
            elif (c.guest_name or "").strip() != gn:
                err = "비회원 이름이 일치하지 않습니다."
            elif not c.check_guest_password(pw):
                err = "비회원 비밀번호가 맞지 않습니다."
            else:
                request.session[_comment_unlock_key(c.pk)] = True
                try:
                    request.session.set_expiry(60 * 60)
                except Exception:
                    pass
                needs_guest_auth = False

        if not err:
            c.body = body
            update_fields = ["body"]
            if hasattr(c, "updated_at"):
                c.updated_at = timezone.now()
                update_fields.append("updated_at")
            c.save(update_fields=update_fields)

            return redirect(f"{reverse('board:detail', kwargs={'pk': post.pk})}#c{c.pk}")

    return render(request, "ragapp/board/comment_form.html", {
        "post": post,
        "comment": c,
        "needs_guest_auth": needs_guest_auth,
        "err": err,
        "categories": _categories_for_sidebar(request),
        "allow_guest_posts": _allow_guest_posts(),
        "is_staff": is_staff,
    })


# ─────────────────────────────────────────────────────────────
# staff moderate
# ─────────────────────────────────────────────────────────────

def post_moderate(request, pk: int):
    user = getattr(request, "user", None)
    if not _is_staff(user):
        raise Http404

    post = get_object_or_404(BoardPost, pk=pk)
    if request.method != "POST":
        return redirect("board:detail", pk=pk)

    action = (request.POST.get("action") or "").strip()

    if action == "toggle_publish":
        post.is_published = not post.is_published
        post.save(update_fields=["is_published", "updated_at"])

    elif action == "toggle_pin":
        post.pinned = not post.pinned
        if post.category and post.category.is_notice:
            post.pinned = True
        post.save(update_fields=["pinned", "updated_at"])

    elif action == "toggle_comments":
        post.allow_comments = not post.allow_comments
        post.save(update_fields=["allow_comments", "updated_at"])

    elif action == "delete":
        post.is_deleted = True
        post.deleted_at = timezone.now()
        post.is_published = False
        post.pinned = False
        post.save(update_fields=["is_deleted", "deleted_at", "is_published", "pinned", "updated_at"])

    elif action == "restore":
        post.is_deleted = False
        post.deleted_at = None
        post.is_published = True
        if post.category and post.category.is_notice:
            post.pinned = True
        post.save(update_fields=["is_deleted", "deleted_at", "is_published", "pinned", "updated_at"])

    return redirect("board:detail", pk=pk)


def comment_moderate(request, comment_id: int):
    user = getattr(request, "user", None)
    if not _is_staff(user):
        raise Http404

    c = get_object_or_404(BoardComment, pk=comment_id)
    if request.method != "POST":
        return redirect("board:detail", pk=c.post_id)

    action = (request.POST.get("action") or "").strip()

    if action == "toggle_hide":
        c.is_hidden = not c.is_hidden
        c.hidden_at = timezone.now() if c.is_hidden else None
        c.save(update_fields=["is_hidden", "hidden_at", "updated_at"])

    elif action == "delete":
        c.is_deleted = True
        c.deleted_at = timezone.now()
        c.save(update_fields=["is_deleted", "deleted_at", "updated_at"])

    elif action == "restore":
        c.is_deleted = False
        c.deleted_at = None
        c.save(update_fields=["is_deleted", "deleted_at", "updated_at"])

    return redirect("board:detail", pk=c.post_id)


# ─────────────────────────────────────────────────────────────
# delete (safe + works for guest without pre-auth too)
# ─────────────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
def post_delete(request, pk: int):
    post = get_object_or_404(BoardPost, pk=pk)
    user = getattr(request, "user", None)

    is_staff = _is_staff(user)
    is_owner = bool(post.author_id and user and user.is_authenticated and post.author_id == user.id)
    guest_unlocked = bool(post.is_guest and request.session.get(_post_unlock_key(post.pk)))

    can_enter = bool(is_staff or is_owner or post.is_guest)
    if not can_enter:
        raise Http404

    needs_guest_auth = bool(post.is_guest and not is_staff and not is_owner and not guest_unlocked)

    if request.method == "POST":
        if needs_guest_auth:
            guest_name = (request.POST.get("guest_name") or "").strip()
            pw = (request.POST.get("guest_pw") or "").strip()

            err = None
            if not guest_name or not pw:
                err = "비회원 이름과 비밀번호를 모두 입력해 주세요."
            else:
                saved_name = (post.guest_name or "").strip()
                if guest_name != saved_name or not post.check_guest_password(pw):
                    err = "비회원 이름 또는 비밀번호가 맞지 않습니다."

            if err:
                return render(request, "ragapp/board/post_confirm_delete.html", {
                    "post": post,
                    "categories": _categories_for_sidebar(request),
                    "allow_guest_posts": _allow_guest_posts(),
                    "needs_guest_pw": True,
                    "needs_guest_name": True,
                    "err": err,
                })

        post.is_deleted = True
        post.deleted_at = timezone.now()
        post.is_published = False
        post.pinned = False
        post.save(update_fields=["is_deleted", "deleted_at", "is_published", "pinned", "updated_at"])
        return redirect("board:list")

    return render(request, "ragapp/board/post_confirm_delete.html", {
        "post": post,
        "categories": _categories_for_sidebar(request),
        "allow_guest_posts": _allow_guest_posts(),
        "needs_guest_pw": needs_guest_auth,
        "needs_guest_name": needs_guest_auth,
    })


@require_http_methods(["GET", "POST"])
def comment_delete(request, comment_id: int):
    c = get_object_or_404(BoardComment.objects.select_related("post"), pk=comment_id)
    post = c.post
    user = getattr(request, "user", None)

    # ✅ 비밀글 댓글 삭제는 글 열람권한이 있어야 가능
    if getattr(post, "is_secret", False) and not _can_view_post(request, post):
        raise Http404

    is_staff = _is_staff(user)
    is_owner = bool(c.author_id and user and user.is_authenticated and c.author_id == user.id)
    guest_unlocked = bool(c.is_guest and request.session.get(_comment_unlock_key(c.pk)))

    can_enter = bool(is_staff or is_owner or c.is_guest)
    if not can_enter:
        raise Http404

    needs_guest_auth = bool(c.is_guest and not is_staff and not is_owner and not guest_unlocked)

    if request.method == "POST":
        if needs_guest_auth:
            guest_name = (request.POST.get("guest_name") or "").strip()
            pw = (request.POST.get("guest_pw") or "").strip()

            err = None
            if not guest_name or not pw:
                err = "비회원 이름과 비밀번호를 모두 입력해 주세요."
            else:
                saved_name = (c.guest_name or "").strip()
                if guest_name != saved_name or not c.check_guest_password(pw):
                    err = "비회원 이름 또는 비밀번호가 맞지 않습니다."

            if err:
                return render(request, "ragapp/board/comment_confirm_delete.html", {
                    "comment": c,
                    "post": post,
                    "categories": _categories_for_sidebar(request),
                    "allow_guest_posts": _allow_guest_posts(),
                    "needs_guest_pw": True,
                    "needs_guest_name": True,
                    "err": err,
                })

        c.is_deleted = True
        c.deleted_at = timezone.now()
        c.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
        return redirect("board:detail", pk=post.pk)

    return render(request, "ragapp/board/comment_confirm_delete.html", {
        "comment": c,
        "post": post,
        "categories": _categories_for_sidebar(request),
        "allow_guest_posts": _allow_guest_posts(),
        "needs_guest_pw": needs_guest_auth,
        "needs_guest_name": needs_guest_auth,
    })


# ─────────────────────────────────────────────────────────────
# comments
# ─────────────────────────────────────────────────────────────

def comment_create(request, pk: int):
    post = get_object_or_404(_base_post_qs(request), pk=pk)

    # ✅ 비밀글은 열람권한 없으면 댓글 생성 불가(직접 POST로 뚫는 것 방지)
    if getattr(post, "is_secret", False) and not _can_view_post(request, post):
        raise Http404

    if request.method != "POST":
        return redirect("board:detail", pk=post.pk)

    if not post.allow_comments and not _is_staff(getattr(request, "user", None)):
        return redirect("board:detail", pk=post.pk)

    user = getattr(request, "user", None)
    form = BoardCommentWriteForm(request.POST, user=user)

    if not form.is_valid():
        cqs = BoardComment.objects.filter(post=post)
        if not _is_staff(user):
            cqs = cqs.filter(is_hidden=False, is_deleted=False)

        return render(request, "ragapp/board/post_detail.html", {
            "post": post,
            "comments": list(cqs.order_by("created_at")),
            "comment_form": form,
            "report_form": BoardReportForm(),
            "can_edit": (
                _is_staff(user)
                or (post.author_id and user and user.is_authenticated and post.author_id == user.id)
                or (post.is_guest and request.session.get(_post_unlock_key(post.pk)))
            ),
            "is_staff": _is_staff(user),
            "categories": _categories_for_sidebar(request),
            "allow_guest_posts": _allow_guest_posts(),
        })

    fp = client_fingerprint(request)

    blocked, _ = hit(f"cmt:hour:{fp}", limit=20, window_sec=3600)
    if blocked:
        return redirect("board:detail", pk=post.pk)

    blocked2, _ = hit(f"cmt:burst:{fp}", limit=1, window_sec=10)
    if blocked2:
        return redirect("board:detail", pk=post.pk)

    if not (user and user.is_authenticated):
        gn = (request.POST.get("guest_name") or "").strip()
        pw = (request.POST.get("guest_password") or "").strip()
        if not gn or not pw:
            form.add_error(None, "비회원 댓글은 이름/비밀번호가 필요합니다.")
            cqs = BoardComment.objects.filter(post=post)
            if not _is_staff(user):
                cqs = cqs.filter(is_hidden=False, is_deleted=False)
            return render(request, "ragapp/board/post_detail.html", {
                "post": post,
                "comments": list(cqs.order_by("created_at")),
                "comment_form": form,
                "report_form": BoardReportForm(),
                "can_edit": (
                    _is_staff(user)
                    or (post.author_id and user and user.is_authenticated and post.author_id == user.id)
                    or (post.is_guest and request.session.get(_post_unlock_key(post.pk)))
                ),
                "is_staff": _is_staff(user),
                "categories": _categories_for_sidebar(request),
                "allow_guest_posts": _allow_guest_posts(),
            })

    c: BoardComment = form.save(commit=False)
    c.post = post
    c.creator_fp = build_fp_from_request(request)

    if user and user.is_authenticated:
        c.author = user
        c.guest_name = ""
        c.guest_pw_hash = ""
    else:
        c.author = None
        c.guest_name = (request.POST.get("guest_name") or "").strip()
        pw = (request.POST.get("guest_password") or "").strip()
        c.set_guest_password(pw)

    c.save()
    return redirect("board:detail", pk=post.pk)


# ─────────────────────────────────────────────────────────────
# report (post/comment)
# ─────────────────────────────────────────────────────────────

def report_post(request, pk: int):
    post = get_object_or_404(_base_post_qs(request), pk=pk)

    # ✅ 비밀글은 열람권한 없으면 신고도 불가(직접 POST 방지)
    if getattr(post, "is_secret", False) and not _can_view_post(request, post):
        raise Http404

    if request.method != "POST":
        return redirect("board:detail", pk=pk)

    form = BoardReportForm(request.POST)
    if not form.is_valid() or (form.cleaned_data.get("website") or "").strip():
        return redirect("board:detail", pk=pk)

    fp = client_fingerprint(request)
    if hit(f"rep:day:{fp}", limit=15, window_sec=86400)[0] or hit(f"rep:burst:{fp}", limit=1, window_sec=20)[0]:
        return redirect(f"{reverse('board:detail', kwargs={'pk': pk})}?reported=0")

    reason = form.cleaned_data.get("reason") or "spam"
    msg = (form.cleaned_data.get("message") or "").strip()

    h = hashlib.sha1(f"{fp}|post|{pk}|{reason}|{msg}".encode("utf-8")).hexdigest()
    if replay_guard("report", h, ttl_sec=600):
        return redirect(f"{reverse('board:detail', kwargs={'pk': pk})}?reported=1")

    r = BoardReport(
        target_type=BoardReport.TargetType.POST,
        post=post,
        reason=reason,
        message=msg,
        reporter_fp=fp,
    )
    u = getattr(request, "user", None)
    if u and u.is_authenticated:
        r.reporter = u
    r.save()

    return redirect(f"{reverse('board:detail', kwargs={'pk': pk})}?reported=1")


def report_comment(request, comment_id: int):
    c = get_object_or_404(BoardComment, pk=comment_id)
    post = get_object_or_404(_base_post_qs(request), pk=c.post_id)

    if getattr(post, "is_secret", False) and not _can_view_post(request, post):
        raise Http404

    if request.method != "POST":
        return redirect("board:detail", pk=c.post_id)

    form = BoardReportForm(request.POST)
    if not form.is_valid() or (form.cleaned_data.get("website") or "").strip():
        return redirect("board:detail", pk=c.post_id)

    fp = client_fingerprint(request)
    if hit(f"rep:day:{fp}", limit=15, window_sec=86400)[0] or hit(f"rep:burst:{fp}", limit=1, window_sec=20)[0]:
        return redirect(f"{reverse('board:detail', kwargs={'pk': c.post_id})}?reported=0")

    reason = form.cleaned_data.get("reason") or "spam"
    msg = (form.cleaned_data.get("message") or "").strip()

    h = hashlib.sha1(f"{fp}|comment|{comment_id}|{reason}|{msg}".encode("utf-8")).hexdigest()
    if replay_guard("report", h, ttl_sec=600):
        return redirect(f"{reverse('board:detail', kwargs={'pk': c.post_id})}?reported=1")

    r = BoardReport(
        target_type=BoardReport.TargetType.COMMENT,
        comment=c,
        reason=reason,
        message=msg,
        reporter_fp=fp,
    )
    u = getattr(request, "user", None)
    if u and u.is_authenticated:
        r.reporter = u
    r.save()

    return redirect(f"{reverse('board:detail', kwargs={'pk': c.post_id})}?reported=1")


# ─────────────────────────────────────────────────────────────
# staff mine
# ─────────────────────────────────────────────────────────────

@never_cache
def staff_mine(request):
    """
    /board/mine/
    운영자 개인 활동 로그 (내 글/내 댓글)
    """
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    u = request.user

    posts = (
        BoardPost.objects
        .filter(author=u)
        .select_related("category")
        .order_by("-created_at")[:200]
    )

    comments = (
        BoardComment.objects
        .filter(author=u)
        .select_related("post", "post__category")
        .order_by("-created_at")[:200]
    )

    return render(request, "ragapp/board/mine.html", {
        "posts": list(posts),
        "comments": list(comments),
        "posts_count": posts.count(),
        "comments_count": comments.count(),
        "is_staff": True,
        "allow_guest_posts": _allow_guest_posts(),
        "categories": _categories_for_sidebar(request),
        "tab": "mine",
    })


@never_cache
@require_GET
def mine_summary_api(request):
    """
    /board/api/mine/summary/
    숫자(내 글/내 댓글/권한) 폴링용
    """
    u = getattr(request, "user", None)
    if not _is_staff(u):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    posts = BoardPost.objects.filter(author=u).count()
    comments = BoardComment.objects.filter(author=u).count()

    role = "Superuser" if getattr(u, "is_superuser", False) else "Staff"
    return JsonResponse({"ok": True, "posts": posts, "comments": comments, "role": role})


@require_POST
def staff_report_action(request):
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    rid = (request.POST.get("rid") or "").strip()
    to = (request.POST.get("to") or "").strip()  # "resolved" | "rejected"
    note = (request.POST.get("admin_note") or "").strip()[:800]
    nxt = (request.POST.get("next") or "").strip()
    nxt = _safe_next_or_empty(request, nxt)

    if not rid.isdigit() or to not in ("resolved", "rejected"):
        return HttpResponseBadRequest("bad request")

    r = get_object_or_404(BoardReport, pk=int(rid))
    r.status = to
    if hasattr(r, "admin_note"):
        r.admin_note = note
    if hasattr(r, "handled_by"):
        r.handled_by = getattr(request, "user", None)
    if hasattr(r, "handled_at"):
        r.handled_at = timezone.now()
    r.save()

    if nxt:
        return redirect(nxt)
    return redirect("board:reports")


@require_POST
def staff_reports_bulk(request):
    if not _is_staff(getattr(request, "user", None)):
        raise Http404

    to = (request.POST.get("to") or "").strip()  # "resolved" | "rejected"
    status = (request.POST.get("status") or "open").strip()
    q = (request.POST.get("q") or "").strip()
    auto_only = (request.POST.get("auto") or "0").strip() == "1"

    if to not in ("resolved", "rejected"):
        return HttpResponseBadRequest("bad request")

    qs = (
        BoardReport.objects
        .select_related("post", "comment", "comment__post", "handled_by", "reporter")
        .all()
    )

    if status in ("open", "resolved", "rejected"):
        qs = qs.filter(status=status)

    if q:
        qs = qs.filter(
            Q(message__icontains=q) |
            Q(admin_note__icontains=q) |
            Q(reason__icontains=q) |
            Q(reporter_fp__icontains=q)
        )

    if auto_only:
        qs = qs.filter(reason__startswith="AUTO")

    ids = list(qs.order_by("-created_at").values_list("id", flat=True)[:200])

    now = timezone.now()
    BoardReport.objects.filter(id__in=ids).update(
        status=to,
        handled_by=getattr(request, "user", None),
        handled_at=now,
    )

    return redirect(f"{reverse('board:reports')}?status={status}&q={q}&auto={'1' if auto_only else '0'}")
