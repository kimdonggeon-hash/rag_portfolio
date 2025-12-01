# ragapp/apps.py
from django.apps import AppConfig

class RagappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ragapp"
    verbose_name = "RAG App"
