"""Source connectors — one module per upstream data provider.

Each connector is responsible for fetching raw data, landing it in the on-disk data lake, and
parsing it into source-agnostic domain records (:mod:`bonds.models`). The full mapping of each
source's endpoints and quirks lives in ``docs/research/<website>.md``.

Implemented:
    fbil        Sovereign valuation price/yield (G-Sec, SDL, STRIPS, ZCYC) — the price engine.
    bondcentral Corporate securities-master universe (~25.5k ISINs) + credit rating.
    cdsl        Corporate issued/outstanding half-yearly snapshots.
    sebi        Corporate-bond public-issue calendar (primary market).
    rbi         Sovereign auction calendar (announcements + dates + links).
    nse         Exchange corporate-bond trade feed (Akamai cookie-primed).
    ccil        G-Sec / NDS-OM individual trades (Liferay portlet; market-hours only).
    ccil_historical  CCIL G-Sec Historical Trades — downloadable trade-by-trade CSV
                     (G-Sec/SDL/T-Bill), any date range. The active CCIL trade source.
"""

from bonds.sources.base import DataUnavailable, Source, SourceError

__all__ = ["DataUnavailable", "Source", "SourceError"]
