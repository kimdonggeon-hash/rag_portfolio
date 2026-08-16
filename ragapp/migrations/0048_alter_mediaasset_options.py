from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("ragapp", "0047_alter_livechatsession_status")]

    operations = [
        migrations.AlterModelOptions(
            name="mediaasset",
            options={"verbose_name": "미디어 자산", "verbose_name_plural": "미디어 자산"},
        ),
    ]
