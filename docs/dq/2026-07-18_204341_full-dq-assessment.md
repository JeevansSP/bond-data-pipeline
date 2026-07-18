```
Full data-quality assessment of the loaded pipeline (securities, trades, valuations, and calendar
tables) across ~4.6M rows, judged for fitness for the sovereign HTM ladder backtest.

2026-07-18_204341 : initial assessment (full history: CCIL trades 2002-2026, FBIL valuations 2023-2026)
```

# Full DQ assessment — 2026-07-18

**Verdict: the sovereign dataset is backtest-ready.** Zero duplicate keys, zero sovereign
referential orphans, full instrument-maturity coverage for T-Bills/STRIPS, and — the strongest
signal — CCIL traded prices and FBIL end-of-day marks agree to a **median 1.7 paise / 100 face**
across 128,752 matched instrument-days. The only issues found are confined to **corporate** data
(secondary for a sovereign-first model).

## Scope

| Table | Rows | Span |
|-------|-----:|------|
| securities | 16,426 | current master |
| trades (CCIL sovereign) | 644,179 | 2002-02-15 → 2026-07-17 |
| trades (NSE corporate) | 153 | 2026-07-17 |
| valuations (FBIL) | 3,983,368 | 2023-02-13 → 2026-07-17 |
| public_issues (SEBI) | 429 | 2009 → 2026 |
| rbi_auctions | 17 | recent calendar |

## 1. Completeness ✅

- **CCIL trades**: 236–294 trading days every year 2002–2025 (2026 partial, 129 days YTD) — no year
  is thin or missing. Record volume grows realistically (11k/yr in 2002 → 57k/yr in 2025).
- **FBIL valuations**: 825 distinct business days over its full published range; 70 weekday gaps,
  all consistent with Indian market holidays (~20/yr × 3.4yr). No unexplained gaps.
- FBIL has no data before 2023-02-13 (verified by probing 2020–2022 → empty). The 2002–2023
  sovereign price history is covered by CCIL trade prices.

## 2. Validity ✅ (anomalies flagged, not silent)

- **valuations**: GSEC price ∈ [87.1, 129.0], YTM ∈ [1.0, 8.6]; SDL price ∈ [82.8, 120.9],
  YTM ∈ [5.2, 8.1]. **0 YTM out of [0,25], 0 non-positive prices, 0 null YTM.** 25 null GSEC prices
  (0.03%).
- **trades**: null rate 0% on ltp/wap across all segments. Extreme values exist (GSEC ltp up to
  14,191; yields down to −50) but are exactly the transposed price/yield and when-issued rows the
  DQ layer already flags (see §8) — raw data is preserved and marked, not silently trusted.
- **securities**: null maturities are all legitimate year-only descriptions (GSEC/SDL/SGB from
  CCIL) or the corporate placeholder below; 0 absurd sovereign maturities.

## 3. Uniqueness ✅

`trades (isin,date,source,segment)` = 0 dups · `valuations (isin,quote_date,source)` = 0 ·
`securities (isin)` = 0.

## 4. Referential integrity — sovereign ✅ / corporate ⚠

- Sovereign (CCIL) trade ISINs missing from `securities`: **0**.
- FBIL valuation ISINs missing from `securities`: **0**.
- **131 NSE corporate trade ISINs are not in the securities master** (BondCentral/CDSL universe
  doesn't cover every OTC-listed corp). Corporate only — no sovereign impact.

## 5. Cross-source consistency ✅ (headline result)

CCIL trade VWAP vs FBIL published price, same ISIN & day, G-Sec + SDL:

- **128,752 matched instrument-days.**
- |CCIL WAP − FBIL price|: **median 0.017**, mean 0.158, p99 1.70, max 12.32 (per 100 face).
- |CCIL WAY − FBIL YTM|: mean 0.038, p99 0.45 (%).
- 683 pairs (0.5%) with price diff > 2 — illiquid/stale prints; immaterial given the median.

Two independent sources agreeing this tightly is strong mutual validation of both price series.

## 6. Backtest fitness (sovereign HTM ladder) ✅

| Segment | maturity known | yield source |
|---------|---------------:|--------------|
| T-Bill | 1,828 / 1,828 (100%) | CCIL trades |
| STRIPS | 1,903 / 1,903 (100%) | CCIL trades |
| SDL | 6,953 / 9,038 (77%) | FBIL 2023+ |
| G-Sec | 150 / 446 (34%) | FBIL 2023+ |

- **7,407 sovereign instruments are still outstanding** (maturity > today) — the ladder's
  investable universe.
- G-Sec/SDL exact maturities come from FBIL (current instruments); matured ones carry only a year
  from CCIL, which is fine — the ladder is built from currently-tradeable instruments, which have
  exact maturities.

## 7. Issues to address (all CORPORATE — none block the sovereign backtest)

1. **CDSL placeholder maturity `1999-12-31` stored literally** (47 CORP rows) — the CDSL parser
   should treat this sentinel as NULL. *Real parser bug; corporate-only.*
2. **131 NSE-traded corporate ISINs absent from the securities master** — derive securities from
   NSE trades (as we now do for CCIL), or accept as a known corp-universe gap.
3. **2,633 / 3,170 CORP rows have null coupon** — BondCentral/CDSL don't supply it; enrich from
   another source if corporate modelling is ever needed.

## 8. DQ layer self-check (recorded `data_quality_checks`)

Over the full history the automated checks flagged: `invalid_isin` 197 (genuinely malformed source
ISINs, verified against a reference algorithm), `ltp_out_of_range` 126 and `lty_out_of_range` 94
(transposed-column STRIPS + when-issued rows), `row_count_drift` 1, `unclassified_auction_type` 2.
All are expected, understood, and non-blocking — the DQ layer is catching real anomalies rather
than sitting idle.
