#!/usr/bin/env python3
"""Retired: viewer-side reprocess queue authority was removed.

The connector-owned authenticated UDS control service commits canonical intent
into archive.pipeline_jobs and the connector drains it.  This module must never
be scheduled or imported as a worker.
"""
import sys


def main():
    print("reprocess_worker retired; use connector pipeline control", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
