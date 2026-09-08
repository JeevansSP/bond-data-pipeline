```
Inventory of the bonds-research warehouse data assets, scoped and ranked for potential commercial
resale (e.g. a daily FTP/API feed) — row counts, coverage, freshness, and uniqueness per table,
plus the licensing question that needs answering before any of this ships to a paying customer.

2026-07-26_170805 : initial inventory (sovereign valuations 4.01M rows, CCIL trades 645k rows back to 2002, corp ratings history, cross-source QA stat)
2026-09-07_234200 : corrected §2b — the rating "history" is a single 18-Jul-2026 snapshot with zero captured changes, not a migration feed; see 2026-09-07_225017_bond-data-market-and-product-catalogue.md for the current inventory
```

# Sellable data inventory — 2026-07-26

**Bottom line up front:** the sovereign bond dataset (FBIL official marks + CCIL actual trades +
issuer master, reconciled against each other) is a real, differentiated commercial product. The
corporate-bond side has one useful asset (a dated rating snapshot — see the 2026-09-07
correction in §2b, it is *not* the rating-change history this line originally claimed) sitting
inside an otherwise-thin dataset. Two smaller feeds (SEBI issuance, RBI auctions) are calendar stubs, not
data products, in their current state.

**Before anything ships to a paying customer: read every source site's terms of use.** This
data all originates from FBIL, CCIL, RBI, NSE, SEBI, and CDSL — quasi-government and exchange
sources that typically restrict systematic redistribution or resale in their terms, even when
scraping the page itself isn't blocked. Nothing in this repo has checked those terms. That's a
legal review, not a formality — treat it as a blocker on packaging, not a footnote.

All figures below are live queries against the warehouse as of 2026-07-26.

---

## 1. Sovereign bond dataset — the core product

### 1a. Daily valuations (FBIL) — `valuations` table

| | |
|---|---|
| **Source** | FBIL (Financial Benchmarks India) — official daily published G-Sec/SDL price & YTM marks |
| **Rows** | 4,011,266 |
| **Distinct ISINs** | 7,113 (150 G-Sec, 6,963 SDL — all ~30 issuing states) |
| **Date range** | 2023-02-13 → 2026-07-24 |
| **Cadence** | Daily (business days; Saturdays attempted, Sundays skipped) |
| **Freshness** | Current as of last night's run |
| **Fields** | `isin, quote_date, source, instrument_type, description, coupon, maturity_date, price, ytm` |
| **Grain** | One row per (ISIN, quote_date, source) |

This is the official end-of-day mark for every actively-quoted G-Sec and SDL — the number desks
use for MTM, not a derived estimate. 6,963 distinct SDL ISINs across every state is a breadth
most vendors bundle at a premium.

### 1b. Secondary-market trades (CCIL) — `trades` table, `source='ccil'`

| | |
|---|---|
| **Source** | CCIL (Clearing Corp of India) — NDS-OM trade-by-trade history, aggregated to daily per-ISIN |
| **Rows** | 644,882 |
| **Distinct ISINs** | 12,655 |
| **Date range** | 2002-02-15 → 2026-07-24 (**24+ years**) |
| **Cadence** | Daily |
| **Fields** | `isin, trade_date, source, segment, descriptor, ltp, lty, no_of_trades, trade_value, wap, way` |
| **Grain** | One row per (ISIN, trade_date, segment) |
| **By segment** | SDL 267,853 rows / 8,434 ISINs · GSEC 251,610 / 430 · TBILL 86,006 / 1,845 · STRIPS 38,406 / 1,904 · SGB 1,007 / 42 |

This is *actual traded* price/yield/volume, not a quoted mark — VWAP, last-traded price/yield,
trade count and turnover per ISIN per day. The 24-year depth (back to 2002) is the hard-to-
replicate part: nobody re-scrapes CCIL's historical archive for fun, and it's the only source
in this warehouse with pre-2010 sovereign secondary-market data at all.

### 1c. Cross-source reconciliation — the QA evidence that makes this sellable

| | |
|---|---|
| **Matched (ISIN, date) pairs** (CCIL traded vs FBIL quoted) | 129,335 |
| **Median absolute price difference** | 0.0166 (per ₹100 face) |
| **99th-percentile difference** | 1.70 |

This is the number that turns "two scraped feeds" into "a validated dataset": FBIL's official
mark and CCIL's actual trade price agree to under 2 paise at the median across 129k matched
observations. A buyer can be shown this stat as evidence of fitness, not just asked to trust it.

### 1d. Sovereign issuer master — `securities` table, sovereign rows

| Source | Instrument | Rows | Coupon filled | Maturity filled |
|---|---|---|---|---|
| fbil | SDL | 6,963 | 6,963 | 6,963 |
| ccil | SDL | 2,085 | 2,085 | 0 |
| ccil | STRIPS | 1,903 | 1,903 | 1,903 |
| ccil | TBILL | 1,831 | 1,831 | 1,831 |
| ccil | GSEC | 297 | 195 | 1 |
| fbil | GSEC | 150 | 150 | 150 |

CCIL is the *only* source for T-Bills, STRIPS, SGBs, and matured/historical G-Secs and SDLs that
FBIL/BondCentral never carry — this master fills a gap no single official source covers alone.
State issuers are normalized to all ~30 issuing states/UTs (post-cleanup; previously fragmented
into 162 spelling variants — now fixed).

**Packaging shape**: a daily file per ISIN with official mark + traded price/yield/volume +
issuer/maturity/coupon reference, for the full G-Sec/SDL/T-Bill/STRIPS/SGB universe. This is the
product to lead with.

---

## 2. Corporate bond dataset — one strong asset inside a thin dataset

### 2a. Corporate securities master — `securities` table, `source='bondcentral'`

| | |
|---|---|
| **Rows** | 24,901 ISINs |
| **Coupon filled** | 18,122 (73%) |
| **Maturity filled** | 24,691 (99%) |
| **Issuer filled** | 23,935 (96%) |
| **Fields** | `isin, description, issuer, coupon, interest_type, maturity_date, face_value, source` |

Breadth is good (nearly 25k corporate ISINs), but coupon coverage has a real gap: ~6,800 ISINs
have no coupon on file. Most of that gap is *not fixable* from this source — it's genuinely
market-linked (variable/index/equity/commodity-linked, ~4,500 ISINs) or zero-coupon paper
(~2,075), where "no coupon" is the correct value, not a hole. Fewer than 200 ISINs are an actual
data gap.

### 2b. Credit rating history (SCD-2) — `security_attribute_history` table

| Attribute | Rows | Distinct ISINs |
|---|---|---|
| `secured_unsecured` | 24,901 | 24,901 |
| `security_status` | 24,901 | 24,901 |
| `credit_rating` | 8,958 | 8,958 |
| `credit_rating_agency` | 8,958 | 8,958 |
| `credit_rating_date` | 8,945 | 8,945 |

**Correction, 2026-09-07: this is a snapshot, not a history.** The paragraph below originally
claimed a slowly-changing rating history. The SCD-2 machinery is real — each value carries a
`valid_from`/`valid_to` window and a change closes the open row — but re-querying the warehouse
shows all 8,958 `credit_rating` rows (and all 24,901 `security_status` / `secured_unsecured`
rows) have exactly **one** version each, every one `valid_from` 2026-07-18/20, with **zero**
closed rows. No rating change has been captured because none has occurred since capture began.

What we actually hold is 8,958 rated ISINs with agency and rating date attached, as of
18 July 2026 — a real base, and the *only* base from which the migration table can accrue, but
it is not yet the migration table. Sell it as a dated snapshot; revisit in ~12 months. (By
contrast, the `nse_cbr` attributes *are* genuine history: 32,209 closed `next_coupon_date` rows
and 43 closed `day_count_convention` rows are captured changes.)

### 2c. CDSL amount-outstanding history — `security_attribute_history`, CDSL rows

| Attribute | Rows |
|---|---|
| `amount_outstanding_cr` | 2,877 |
| `amount_issued_cr` | 2,877 |
| `payment_frequency` | 2,877 |

**Caveat: stale.** CDSL is a half-yearly snapshot (31-Mar / 30-Sep) and the last one loaded is
**2025-09-30** — over 10 months old as of this writing. Not a daily feed in its current state;
would need the next snapshot (30-Sep-2026) pulled and, ideally, the ingest scheduled to catch it
automatically.

### 2d. Corporate secondary trades (NSE) — `trades` table, `source='nse'`

| | |
|---|---|
| **Rows** | 832 |
| **Distinct ISINs** | 445 |
| **Date range** | 2026-07-17 → 2026-07-24 (**8 days**) |

**Not sellable yet.** This just started ingesting; a week of history is a rounding error next to
the sovereign side's 24 years. Revisit once it's accumulated months of history.

---

## 3. Calendar/reference feeds — not products in current form

| Feed | Rows | Range | Verdict |
|---|---|---|---|
| SEBI public issue calendar | 431 | 2009-02-02 → 2026-06-08 | Thin but a genuine 17-year series; could be a minor add-on, not a standalone product |
| RBI auction calendar | 29 | 2026-07-09 → 2026-07-24 | **Calendar stub, not data** — titles/dates/links only, no cut-off yields or notified/accepted amounts (a documented future extension, not yet built) |

---

## 4. What would need to happen before this is a real feed product

1. **Legal/licensing review of every source** (FBIL, CCIL, RBI, NSE, SEBI, CDSL) — see the
   warning at the top. This gates everything else.
2. **Corporate coupon gap**: enrichment already narrowed this to genuine gaps (~170 ISINs); low
   effort, mostly done.
3. **CDSL refresh**: pull the 2026-09-30 snapshot when it publishes; consider adding it to the
   nightly catch-up so it's never >6 months stale again.
4. **NSE corporate trades**: let it accumulate — not a decision, just time.
5. **Packaging**: decide the actual delivery shape (flat CSV/Parquet per day vs. a queryable API)
   and whether the product is "sovereign only" (strongest, cleanest) or "sovereign + corporate
   ratings history" (broader, two audiences).

---

## Appendix: table-level row counts (raw, for reference)

| Table | Rows |
|---|---|
| `valuations` | 4,000,102 |
| `trades` | 645,163 |
| `security_attribute_history` | 85,294 |
| `securities` | 39,005 |
| `public_issues` | 429 |
| `rbi_auctions` | 23 |
