web: sh -c "python manage.py migrate --noinput && python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p ${PORT} ragsite.asgi:application"
diag: python manage.py chroma_diag
reindex: sh -c "python3 manage.py media_reindex_storage_images --prefix 'images/' --apply --caption-from-name --skip-existing && python3 -c 'from ragapp.services.chroma_media import sync_media_up; print(sync_media_up(prune=True))'"
