# Third-party notices

Jazzuu itself is MIT-licensed. Third-party components retain their own licenses.

## Bundled asset

- `viewer/app/fonts/DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` are from the
  DejaVu font project. Their license is bundled verbatim at
  `viewer/app/fonts/LICENSE-DejaVu.txt`.
- Jazzuu logo and app-icon artwork was supplied by the project maintainer and is
  distributed under the repository MIT license.
- `viewer/app/static/app.css` was generated from Tailwind CSS 3.4.17, licensed
  MIT; its license is bundled at `viewer/app/static/LICENSE-Tailwind.txt`. The
  authenticated viewer loads no third-party runtime code.

## Direct viewer dependencies

Direct versions are pinned in `viewer/requirements.txt`; the full resolved set
and package hashes are in `viewer/requirements.lock`.

| package | upstream | license |
|---|---|---|
| FastAPI | https://github.com/fastapi/fastapi | MIT |
| Uvicorn | https://github.com/encode/uvicorn | BSD-3-Clause |
| Pillow | https://github.com/python-pillow/Pillow | MIT-CMU |

## Connector dependencies

The full resolved set and hashes are in
`archive/requirements-diarization.lock`.

| package | upstream | license |
|---|---|---|
| sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0 |
| NumPy | https://github.com/numpy/numpy | BSD-3-Clause |

Jazzuu does not redistribute diarization model weights. Operators must supply
their own licensed segmentation and speaker-embedding ONNX files through the
read-only model mount.

All three Dockerfiles pin the official Python 3.12 slim image by digest. The
connector installs exact ffmpeg `7:5.1.8-0+deb12u1` from the immutable Debian
snapshot dated 2026-03-01; package notices remain under `/usr/share/doc/`.

## Optional semantic-search model

The repository contains no embedding weights. `semantic_search/model_manifest.json`
records the expected `BAAI/bge-m3` source, model digest, dimension, and MIT model
card license. The operator-managed Ollama service owns and downloads that model.

## External services

PLAUD and Tilmech are external integrations. Jazzuu does not redistribute their
software or credentials. Their operators must accept and comply with the
corresponding upstream terms separately.
