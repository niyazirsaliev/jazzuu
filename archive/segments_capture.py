"""Retired unsafe production entry point; retained only so imports work."""
import sys


def main():
    print('segments_capture.py is retired; transcript ingestion is connector-owned', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())