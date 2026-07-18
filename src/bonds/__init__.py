"""Daily data pipelines for the Indian bond market.

Pillars:
    1. Current universe (daily upsert).
    2. Attribute-change history (SCD-2: price, yield, rating, ...).
    3. Sovereign valuation history (FBIL per-ISIN price/YTM) feeding a ladder backtest.

See ``docs/research/`` for the source-by-source data mapping this package is built on.
"""

__version__ = "0.1.0"
