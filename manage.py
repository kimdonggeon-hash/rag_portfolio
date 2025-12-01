#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ragsite.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django가 설치되어 있지 않거나 가상환경이 잘못되었습니다."
        ) from exc
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
