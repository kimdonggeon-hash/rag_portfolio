# ragapp/signals_faq.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from ragapp.models import FaqEntry
from ragapp.qa_data import invalidate_qa_cache


@receiver(post_save, sender=FaqEntry)
def _faq_saved(sender, instance, **kwargs):
    invalidate_qa_cache()


@receiver(post_delete, sender=FaqEntry)
def _faq_deleted(sender, instance, **kwargs):
    invalidate_qa_cache()