from __future__ import annotations

from django.core.management.base import BaseCommand
from django.core.management import call_command


class Command(BaseCommand):
    help = "Retention launcher: retention_healthcheck -> backfill_delete_at -> purge_by_delete_at 순서로 실행합니다."

    def add_arguments(self, parser):
        # 공통
        parser.add_argument("--dry-run", action="store_true", help="backfill/purge를 dry-run으로 실행")
        parser.add_argument("--limit", type=int, default=5000, help="backfill/purge 공통 limit (기본 5000)")
        parser.add_argument("--chunk-size", type=int, default=500, help="backfill iterator/bulk_update 배치 크기(기본 500)")

        # healthcheck 옵션(네 파일에 존재)
        parser.add_argument("--json", action="store_true", help="healthcheck를 JSON으로 출력")
        parser.add_argument("--skip-total", action="store_true", help="healthcheck에서 total count 생략")
        parser.add_argument(
            "--models",
            type=str,
            default="",
            help="대상 모델 선택(콤마): ConsentLog,ChatQueryLog,QaragFeedback,Feedback (비우면 전부)",
        )

        # purge 옵션(네 purge_by_delete_at.py에 존재)
        parser.add_argument("--repair-early", action="store_true", help="purge 전에 delete_at early 교정")
        parser.add_argument(
            "--force-without-created-field",
            action="store_true",
            help="created_at 없는 모델도 delete_at<=now면 삭제 허용(권장 X)",
        )

    def handle(self, *args, **opts):
        dry_run: bool = bool(opts["dry_run"])
        limit: int = int(opts["limit"] or 5000)
        chunk_size: int = int(opts["chunk_size"] or 500)

        models_raw: str = str(opts.get("models") or "").strip()
        as_json: bool = bool(opts["json"])
        skip_total: bool = bool(opts["skip_total"])

        repair_early: bool = bool(opts["repair_early"])
        force_without_created: bool = bool(opts["force_without_created_field"])

        self.stdout.write("[retention_runall] start")

        # 1) healthcheck
        hc_kwargs = {}
        if models_raw:
            hc_kwargs["models"] = models_raw
        if as_json:
            hc_kwargs["json"] = True
        if skip_total:
            hc_kwargs["skip_total"] = True
        call_command("retention_healthcheck", **hc_kwargs)

        # 2) backfill (네 backfill_delete_at.py 옵션과 정확히 호환)
        bf_kwargs = {
            "dry_run": dry_run,
            "limit": limit if limit is not None else 0,
            "chunk_size": chunk_size,
        }
        if models_raw:
            bf_kwargs["models"] = models_raw
        call_command("backfill_delete_at", **bf_kwargs)

        # 3) purge
        pg_kwargs = {
            "dry_run": dry_run,
            "limit": limit,
        }
        if models_raw:
            pg_kwargs["models"] = models_raw
        if repair_early:
            pg_kwargs["repair_early"] = True
        if force_without_created:
            pg_kwargs["force_without_created_field"] = True
        call_command("purge_by_delete_at", **pg_kwargs)

        self.stdout.write("[retention_runall] done")
