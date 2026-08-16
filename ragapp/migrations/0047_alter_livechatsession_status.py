from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ragapp", "0046_dailyusage_admin_count"),
    ]

    operations = [
        migrations.AlterField(
            model_name="livechatsession",
            name="status",
            field=models.CharField(
                choices=[
                    ("ended_need_save", "저장 필요"),
                    ("saved", "저장 완료"),
                    ("waiting", "대기"),
                    ("active", "진행"),
                    ("ended", "종료"),
                    ("done", "기록 완료"),
                    ("deleted", "삭제됨"),
                ],
                db_index=True,
                default="waiting",
                max_length=32,
            ),
        ),
    ]
