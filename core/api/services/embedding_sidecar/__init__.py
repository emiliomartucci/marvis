"""Shared embedding sidecar package (F1).

A standalone FastAPI service (``marvis-embedder.service``, loopback :8109) that
serves Granite embeddings to the whole tenant fleet from ONE warm process,
instead of every tenant loading its own ~390MB ONNX graph. See
``docs/plans/2026-07-02-feat-shared-embedding-sidecar-plan.md``.
"""
