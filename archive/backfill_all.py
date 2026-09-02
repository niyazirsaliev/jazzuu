"""Retired production entry point; retained only so historical imports work."""
import sys


def main():
    print('backfill_all.py is retired; run connector.py once or loop instead', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())