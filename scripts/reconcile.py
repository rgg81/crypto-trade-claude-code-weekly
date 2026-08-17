"""Back-compat shim: the reconciliation helper now lives in the package.

`futures_fund/orchestration.py` must not import from `scripts/` — that directory has no
`__init__.py` and is not part of the built wheel (hatchling packages `futures_fund` only), so a
non-editable install would fail to import the gate outright rather than degrade.
"""
from futures_fund.reconcile import _symbols_from, unexplained_opens

__all__ = ["unexplained_opens", "_symbols_from"]
