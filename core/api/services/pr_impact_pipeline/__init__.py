"""
KG PR-Impact data pipeline subpackage.

D2 module set (shipped 2026-05-16):

- `languages.py` — LanguageSpec registry (Python / TS / TSX)
- `parser.py`    — tree-sitter wrappers + function extraction helpers
- `differ.py`    — git diff parsing + hunk → function attribution
- `writer.py`    — graph_edges + pr_function_touches UPSERTs (single-writer)
- `dispatcher.py`— BackgroundTasks job runner (D4 expands retry/replay)

Read-side query layer lives in `api/services/kg/pr_impact.py` (sub-02 §5).
The split keeps the populator subprocess imports lean (no FastAPI deps)
while the read layer can depend on the full API surface.

References:
- docs/plans/2026-05-16-feat-kg-pr-impact-view-plan.md §11
- docs/plans/sub/2026-05-16-kg-pr-impact-01-backend-data-pipeline.md §4.2 + §D2
"""

__version__ = "1.0.0"
