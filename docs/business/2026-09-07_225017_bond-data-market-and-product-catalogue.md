```
Market research on the Indian bond / bond-data market as of September 2026, plus the complete
catalogue of sellable products derivable from the warehouse as it actually stands today — 23
products in four tiers, each mapped to the tables behind it, the buyer, the regulatory hook, the
build effort and an indicative price band. Supersedes the product framing in
2026-07-26_173715_bond-data-business-case.md and the product matrix in
2026-07-27_222030_rbi-investment-directions-demand-map.md, both of which understated the
corporate side and overstated the rating asset.

2026-09-07_225017 : initial version — market sizing and competitor scan (web, Sept 2026); full warehouse re-inventory against live DB; 23-product catalogue; three material corrections to the July docs (rating "history" is a snapshot, CCIL ingest silently dead since 14-Aug, SDL issuer normalization regressed)
2026-09-08_080330 : backlog items 5, 7, 8 and 9 built — P8 liquidity/staleness table, P14 RBI auction results (per-security cut-offs, all three release layouts), P10 sovereign classification + RBI valuation engine, P23 trade-derived spread matrix. New: bonds.states / bonds.sovereign / bonds.curves / bonds.valuation, three tables, four CLI commands, a "Derived products" DQ dimension. Two placeholders flagged in-code and here: the rating-graded spread grid and the active-market threshold are the right *shape* but not the published figures.
2026-09-08_000239 : §2.3 defects re-diagnosed and closed out — every one of items 1-8 is now fixed in code and repaired in the warehouse (see the backlog table in §6). Corrections to the 2026-09-07 draft: BSE was stale to 2026-08-14 too (reported as current); the `invalid_isin` warning was 2 ISINs per run, not 10, and all 21 warehouse-wide are check-digit-only with zero malformed; the GSEC-bucket "contamination" was 233 state UDAY securities mis-typed by a worksheet title, not 4,234 mixed ISINs — the central G-Sec universe is 544 in the master and 298 in `valuations`.
```

# The Indian bond data market, and everything we can sell into it

**Date:** 7 September 2026
**Warehouse figures:** live queries against local Postgres, 2026-09-07 22:50 IST
**Market figures:** public sources, retrieved 7 September 2026 (listed in §7)

---

## 0. Executive summary

**The market.** India's debt market is ~₹240 lakh crore (~US$2.8tn); the corporate slice is
₹53.64 lakh crore (~22.5%) and issued a record ₹9.9 lakh crore in FY25, up 28% YoY. Demand for
bond *data* is not discretionary: nine RBI Investment Directions (Nov 2025) name FBIL prices and
the CG yield curve as mandatory valuation inputs for every class of bank, and a SEBI circular of
6 February 2026 forced 1,992 registered AIFs to report independently-valued unit NAVs to
depositories from 1 May 2026. Bottom-up, roughly **4,400 regulated institutions** now carry a
named or de facto bond-valuation data obligation.

**Our position has changed materially since July.** Two of the four "non-negotiable build items"
from the July demand map are done, and the corporate side went from unsellable to our
second-largest asset:

| Asset | July 2026 | Today | Change |
|---|---|---|---|
| Sovereign valuations | 4.01M rows, from 2023-02-13 | **5.64M rows, from 2021-03-16** | +41%, +2 yrs depth |
| Yield curves | **"No — build"** | **372,768 rows, 3 curves, 200-pt grid to 50y** | built |
| Corporate trades | 832 rows / 8 days ("not sellable") | **4,860,275 transactions, from 2014-04-02** | shippable |
| NSE CBM daily corporate tape | not inventoried | **600,632 rows, 34,156 ISINs, from 2008-01-01** | new asset |
| Securities master | 39,005 | 56,470 | +45% |

**Three corrections to the July docs**, all material to what we can honestly pitch:

1. **The "rating-change history" does not exist yet.** All 8,958 `credit_rating` rows have exactly
   one version, `valid_from` 2026-07-18/20, and *zero* closed rows. The SCD-2 machinery is in
   place; no migration has been captured. It is a snapshot today, and becomes a migration feed
   only with elapsed time. The business case sold this as history — that claim must be withdrawn.
2. **The CCIL trade pipeline has been silently dead since 14 August 2026.** Every run since has
   returned `rows_ingested = 0` with status `success`. Last real data: 2026-08-14. Our single most
   differentiated asset (24-year sovereign tape) is 3½ weeks stale and the audit trail says
   everything is fine. Fix before any demo.
3. **SDL issuer normalization has regressed.** 63 distinct issuer strings where there should be
   ~30 — `State Government (AP)` *and* `State Government (ANDHRA PRADESH)`, `(ML)`/`(MG)` for
   Meghalaya, `(NL)`/`(NG)` for Nagaland, `(PY)`/`(PD)`/`(PUDUCHERRY)`. Commit 5cd87a3 fixed this;
   a later ingest re-broke it. Blocks any per-state product.

**Recommendation.** Lead with two products, not one: the **sovereign price + curve feed** (mass
market, ~1,800 banks, reachable through NUCFDC and core-banking vendors) and the **corporate
trade tape + liquidity scores** (premium, ~60 banks + auditors + 1,992 AIFs, and the only dataset
that answers the 15-day rule and the Level 1/2/3 active-market test). Everything else in §3 is an
add-on to one of those two.

---

## 1. The market

### 1.1 Size and shape

| Metric | Value | As of |
|---|---|---|
| Total Indian debt market | ~₹240 lakh crore (~US$2.76–2.8tn) | early 2026 |
| Corporate bonds outstanding | ₹53.64 lakh crore (~US$627bn), 22.5% of total | mid-2026 |
| Corporate issuance, FY25 | ₹9.9 lakh crore, +28% YoY (record) | FY25 |
| Corporate secondary daily turnover | growth of ~₹2,000 crore/day in FY25 | RBI, FY25 |
| 10-year G-Sec yield | 6.7–6.8% | mid-2026 |
| Repo rate | 5.25% (−25bp Feb 2026, −25bp Apr 2026) | Apr 2026 |
| Municipal bonds | ₹4,540 crore across 31 issuances by 22 municipal corporations | Mar 2026 |

The relevant structural fact for us is not the size of the market but its **fragmentation of
obligation**: the same handful of price sources (FBIL, CCIL, exchange tapes, FIMMDA matrices) are
mandated to thousands of institutions of wildly different size, and the incumbent vendors are
priced for the top 50 of them.

### 1.2 Demand drivers — who is legally obliged to hold this data

| Buyer class | Obligation | Count | Vintage |
|---|---|---|---|
| Commercial banks | Quarterly+ revaluation at FBIL prices; L1/2/3 disclosure from FYE 31-Mar-2026; Level-3 CET1 penalty | ~40 | RBI Directions, Nov 2025 |
| SFB / PB / LAB | Same regime as CB, incl. L1/2/3 | ~19 | Nov 2025 |
| RRBs | Legacy HTM/AFS/HFT; monthly HFT, quarterly AFS; FBIL prices | ~43 | Nov 2025 |
| AIFIs (EXIM, NABARD, SIDBI, NHB, NaBFID) | Legacy trinity; ZCYC/STRIPS machinery present | 5 | Nov 2025 |
| Urban + rural co-op banks | Same FBIL price/curve/15-day stack, no L1/2/3; IFR 5% | ~1,700 | Nov 2025 |
| **AIFs** | **Independent valuation; unit NAV reported to depositories from 1 May 2026**; Cat I/II ≥ half-yearly by independent valuer, Cat III NAV monthly/quarterly | **1,992** (₹6.45 lakh crore AUM, ~30% 5-yr CAGR) | **SEBI circular 6 Feb 2026** |
| Portfolio managers | Client reporting at market value; RFQ transaction quotas | ~490 | standing |
| Insurers | Master circular under Actuarial/Finance/Investment Regs 2024; actively lobbying IRDAI for **security-level** (bond-by-bond) valuation — which would need exactly our data | ~50 (₹74 lakh crore) | live lobbying |
| OBPPs | Scope **expanded 14 Aug 2026** to IFSCA-regulated products and 54EC / Sec-85 tax-saving bonds | 29+ | SEBI, Aug 2026 |
| Mutual funds | Waterfall valuation: traded securities valued on traded yields, outliers identified by valuation agencies | ~45 AMCs | SEBI, standing |

**Bottom-up addressable count:** ~1,800 banks + 1,992 AIFs + ~490 PMS + ~50 insurers + 29 OBPPs ≈
**4,360 institutions**. At an average realised ₹1–3 lakh/year that is a **₹44–130 crore annual
India bond-valuation data pool** — of which the incumbents hold the top by value and almost none
of it by count. *(Estimate: our arithmetic on public institution counts, not a published figure.)*

### 1.3 Two new demand events since the July docs

- **SEBI, 6 February 2026** (HO/19/34/11(8)2025-AFD-POD1/I/4335/2026): AIFs must report the value
  of units to depositories, effective **1 May 2026**. This is the first hard, dated,
  independent-valuation obligation on 1,992 funds — a buyer population comparable to the co-op
  banks, with far more money and far shorter procurement cycles. It was not in the July analysis.
- **SEBI, 14 August 2026**: OBPP framework modified — platforms may now offer IFSCA-regulated
  products and 54EC/Sec-85 tax-saving bonds, may run them in a separate section or portal, and
  must align compliance officers with the Stock Brokers Regulations 2026. Every new product line
  on an OBPP is a new place our liquidity/yield data has to appear.

Also live but not yet an obligation: **SEBI's 13 May 2026 consultation on municipal debt
(ILMDS)** — pooled issuance, refinancing, ₹10,000 face values, ESG-linked municipal debt. If it
lands, a retail municipal market appears where no price data exists. We already hold ~350
municipal/civic ISINs.

### 1.4 Competitors

| Who | What they sell | Where they are weak for us |
|---|---|---|
| **CRISIL** (Funds & Fixed Income Research) | Daily valuations for all outstanding bonds to subscribers; published SDL and bond valuation methodologies; AMFI-appointed MF valuation agency | Enterprise pricing; MF channel legally closed to us; sells the *mark*, not the *evidence* |
| **ICRA Analytics** | Same segment; AMFI-appointed valuation agency; Moody's majority-owned | Same |
| **CARE** | Same segment | Same |
| **FIMMDA** | Daily/fortnightly corporate **yield and spread matrices** (AAA→BBB−, 0.5–15y) — spreads to AA− from traded levels or **polls**, below AA− fixed for 3 months from 3-month traded levels | Poll-based and fixed-for-quarters below AA−; our tape is trade-derived and daily. A real differentiator, not a competitor to displace |
| **NSE Data & Analytics / BSE** | Licensed exchange feeds via authorised vendors (TrueData, Global Datafeeds, Refinitiv); published domestic pricing file | They sell raw tape, not valuation-ready or reconciled datasets |
| **CCIL** | NDS-OM tape, SDL index, factsheets | Same — infrastructure, not product |
| **NSE Indices (IISL)** | Nifty 10-Year SDL, Nifty SDL Dec-2026 etc., licensed for ETFs; **changed SDL index methodology effective 7 Aug 2026** | Index-level only; no state-level or custom-tenor granularity |
| **Cbonds** | 900k+ bonds, 350–420 pricing sources, global; buys local feeds as a standing model; India country page and India Corporate USD indices; first to publish SGB as % of gold reference | Thin Indian depth — precisely the gap we fill wholesale |
| **Bloomberg / LSEG** | Everything, at global prices | Not competing for a ₹2-lakh co-op bank |

**Price benchmarks we can actually cite.** Cbonds' Datarade listings start at **US$450/month
(~₹4.8 lakh/yr)** for global bond pricing across 420 sources and **US$350/month (~₹3.7 lakh/yr)**
for global fixed-income reference data on 850k issues. That is the ceiling reference for a
*global multi-country* feed — which anchors an India-only, India-deep feed sensibly in the
**₹1.5–6 lakh/yr** band for a single small institution, and makes ₹25 lakh–₹1 crore defensible
only for vendor/wholesale redistribution rights.

### 1.5 Where we are actually differentiated

1. **Depth nobody re-scrapes**: 24 years of sovereign tape (2002→), 18 years of corporate daily
   tape (2008→), 12 years of trade-level corporate transactions (2014→).
2. **Trade-derived, not poll-derived**: 4.86M individual corporate transactions with time, price,
   yield, venue and settlement. FIMMDA polls below AA−; we have the prints.
3. **Cross-source reconciliation as a shipped artefact**: 158,189 FBIL↔CCIL matched (ISIN, date)
   pairs, median absolute price difference **0.0367** per ₹100 and p99 **1.90**; plus **4,362,848**
   matched corporate_trades↔NSE-CBM (ISIN, date) pairs. Nobody sells the cross-check.
4. **Bitemporal truth**: 3,466 superseded valuation rows preserved — we can answer "what did the
   official mark say on the day you booked it", which is an audit and dispute product.
5. **The neglected half**: 8,872 SDL ISINs across every state and UT, with an SDL ZCYC. Everyone
   else optimises for central G-Secs.

---

## 2. What we hold today

All figures are live queries, 2026-09-07. Sovereign prices/curves current through the last
business day (2026-09-04); FBIL runs for 2026-09-07 had not yet published at query time.

| Table | Rows | Grain | Coverage | Depth | Fresh? |
|---|---|---|---|---|---|
| `valuations` | **5,639,456** | ISIN × date × source, append-only bitemporal | SDL 7,469 ISINs · STRIPS 1,815 · GSEC 298 (central only, post-repair) | 2021-03-16 → | ✅ 2026-09-04 |
| `corporate_trades` | **4,860,275** | one row per transaction | NSE 23,275 ISINs / 2,367 issuers / ₹14,201cr · BSE 9,926 / 1,842 / ₹1,460cr | NSE 2014-04-02 → · BSE 2020-11-17 → | ✅ all current (BSE was stale to 2026-08-14; backfilled 2026-09-07) |
| `trades` | **1,252,456** | ISIN × date × segment summary | nse_cbm 34,156 ISINs · CCIL SDL 8,461 / GSEC 432 / TBILL 1,855 / STRIPS 2,002 / SGB 42 · NSE OTC 1,035 | nse_cbm 2008-01-01 → · CCIL 2002-02-15 → | ✅ all current (CCIL was stale to 2026-08-14; backfilled 2026-09-07) |
| `yield_curves` | **372,768** | curve × tenor × date, bitemporal | gsec_par 860 days / 200 tenors (0.25–50y) · gsec_zcyc 859 / 200 · sdl_zcyc 768 / 56 (0.25–14y) | par 2021-03-16 → · ZCYC 2023-02-13 → | ✅ 2026-09-04 |
| `securities` | **56,470** | one row per ISIN | CORP 42,907 · SDL 8,872 · STRIPS 2,017 · TBILL 1,855 · GSEC 777 · SGB 42 | current state | ✅ daily |
| `security_attribute_history` | **187,815** | SCD-2 per attribute | see §2.1 | 2019-12-17 → | mixed |
| `public_issues` | 433 | one row per public issue | company, open/close, base & final size | 2009-02-02 → | ✅ daily |
| `rbi_auctions` | 99 | auction notice | title/date/links only — no cut-offs or amounts | 2026-07-09 → | ✅ daily |
| `security_liquidity` | **26,599** | ISIN × as-of date (derived) | 13,370 ISINs per date; 956 active-market | per as-of date | ✅ built 2026-09-08 |
| `corporate_spread_matrix` | **71** | quote date × rating × tenor (derived) | 13 rating notches × FIMMDA's 12 buckets | per quote date | ✅ built 2026-09-08 |
| `rbi_auction_results` | **36** | prid × security | G-Sec, T-Bill and SDL cut-offs | current | ✅ built 2026-09-08 |
| `ingestion_runs` / `etl_file_metrics` / `data_quality_checks` | 27,219 / 19,708 / 61,396 | audit trail | per run / per file / per check | — | ✅ |

### 2.1 Attribute history — what is real history and what is a snapshot

| Attribute | Source | Rows | ISINs | Closed rows (= actual changes captured) |
|---|---|---|---|---|
| `next_coupon_date` | nse_cbr | 39,456 | 7,247 | **32,209** ✅ |
| `issuance_date` | nse_cbr | 20,216 | 20,204 | 12 |
| `day_count_convention` | nse_cbr | 19,946 | 19,903 | 43 |
| `coupon_frequency` | nse_cbr | 7,761 | 7,739 | 22 |
| `listing_status` | nse_cbr | 15,142 | 15,142 | 0 |
| `security_status` | bondcentral | 24,901 | 24,901 | **0** |
| `secured_unsecured` | bondcentral | 24,901 | 24,901 | **0** |
| `credit_rating` (+ agency, date) | bondcentral | 8,958 | 8,958 | **0** ← snapshot, not history |
| `amount_outstanding_cr` / `amount_issued_cr` / `payment_frequency` | cdsl | 2,877 each | 2,877 | 0 — **stale to 2025-09-30**, one run ever |

### 2.2 Measured liquidity structure of the corporate universe

From `corporate_trades` (3,001 distinct trading days). This table *is* a product — it is the
Level 1/2/3 active-market test:

| Distinct trading days in last 12m | ISINs |
|---|---|
| 1 | 1,293 |
| 2–5 | 1,908 |
| 6–20 | 1,828 |
| 21–60 | 1,077 |
| 61–150 | 524 |
| **150+** | **192** |

ISINs with any print in the last **15 days: 1,367** · 30 days: 2,096 · 90 days: 3,734. Of the
8,958 rated ISINs, **5,109 have traded at least once** in our tape.

### 2.3 Known defects — all closed 2026-09-08

Every defect in the 2026-09-07 draft is now fixed in code and repaired in the warehouse.
`bonds dq assess` reports **0 errors**. The re-diagnosis corrected three of them.

| # | Defect (as first reported) | What it actually was | Fix |
|---|---|---|---|
| 1 | CCIL ingest returned 0 rows under status `success` since 2026-08-14 | Confirmed, and **BSE was down the same way** (reported as current — wrong). Root cause differs per source: CCIL returned a valid header-only CSV, which the connector passed through as an empty batch; BSE returned an empty `Table` that got **landed in the data lake**, so every nightly retry re-read the empty file and skipped again. Both were invisible because the scheduler had moved to 13:00, before either source publishes | CCIL: empty CSV → `DataUnavailable` (SKIPPED, retried); rows-but-unparseable → `SourceError` (FAILED). BSE: an empty payload is never landed, and a landed empty is treated as a cache miss (447 stale artifacts cleared). **16 CCIL days / 2,483 rows and 15 BSE days / 30,064 trades recovered** |
| 2 | `credit_rating` is a snapshot, not history (0 closed rows) | Confirmed | Claim withdrawn from the business case and the sellable-data inventory |
| 3 | SDL issuer strings re-fragmented: 63 variants | Confirmed. FBIL emitted raw two-letter codes while CCIL normalised to full state names; the July repair fixed rows without unifying the code paths | One shared vocabulary (`bonds.states`) for both connectors, closed-vocabulary DQ check, migration repair. **63 → 31 issuers** |
| 4 | `GSEC` bucket mixes central G-Sec with 4,234 state-prefix ISINs | **Mis-diagnosed.** Not a mixed bucket: 233 state UDAY/SDL-SPL securities (and 3,936 valuation rows, all on 2021-03-16) were typed central because FBIL's pre-2023 combined workbooks put them on the G-Sec workbook's "Special" sheet. The real central universe is 544 in the master, 298 in `valuations` | The ISIN form (central `IN00…`, state `IN10…`-`IN49…`) now outranks the worksheet title; DQ check added; **233 securities + 3,936 rows repaired** |
| 5 | CDSL 11 months stale, one run ever | Confirmed | `catch-up` now re-attempts the due 31-Mar/30-Sep snapshot until it publishes; an unpublished report date records SKIPPED, not FAILED |
| 6 | `rbi_auctions` has no cut-off yields or amounts | Confirmed — a build, not a defect | Left open as backlog item 7 (P14) |
| 7 | `deal_type` unnormalised | Confirmed, and **four more columns** were case-fragmented the same way (`seller_deal_type`, `buyer_deal_type`, `listed`, `settlement_status`) | All five canonicalised at the model boundary and repaired in place |
| 8 | "10 `invalid_isin`, 6 STRIPS `price_out_of_range`" | **Mis-counted.** `invalid_isin` was 2 ISINs per run (10 was the number of *check rows* over 10 days); warehouse-wide there are 21, and **all 21 are check-digit-only with zero malformed** — real securities as published (FBIL prints `IN1520250085`, NSE prints `INEO81J07036` with a letter O). The STRIPS warning was ~1,070 rows/night, not 6: the par price band was simply wrong for zero-coupon strips, which legitimately trade down to ₹0.98 per ₹100 | ISIN **shape** stays ERROR (0 today); a bad check digit is a WARN naming the identifiers. STRIPS get their own deep-discount band |

**One further defect found and fixed in the audit layer.** A `success` row was deliberately
sticky so a later failure could not erase a real load's row count — but that also meant a
`rows=0` success could never be corrected, so even after the connector fixes those dates stayed
unreachable. Zero-row successes are now overwritable; a genuine load is still protected.

**And one I introduced and backed out.** I first enforced "a date-series load that writes zero
rows is a skip" centrally in the pipeline runner. That is wrong: `work()` returns rows *written*,
and a bitemporal upsert legitimately writes zero when re-running a day already loaded and
unchanged — it wrongly skipped 84 healthy curve-days. Only the connector can tell an empty source
response from an idempotent no-op, so the invariant lives in the connectors, and the DQ check now
asks the precise question instead: *is there a day whose run says `success` but whose table holds
no rows?*

---|---|---|
| 1 | **CCIL ingest returns 0 rows, status `success`, since 2026-08-14** | Flagship asset stale; audit trail lies. Blocks P3, P12 |
| 2 | **`credit_rating` is a snapshot, not history** (0 closed rows) | Withdraw the migration claim from all material |
| 3 | **SDL issuer strings re-fragmented: 63 variants** | Blocks P11 (state products) |
| 4 | `GSEC` valuation bucket mixes central G-Sec (125/day) with state-prefix ISINs from FBIL's special/UDAY sheets (4,234 distinct ISINs) | Cannot label the feed "central government" until sub-typed — and the sub-type *is* P10 |
| 5 | CDSL 11 months stale, one run ever; next snapshot 30-Sep-2026 | Amount-outstanding product unshippable; 3 weeks to fix |
| 6 | `rbi_auctions` has no cut-off yields or notified/accepted amounts | P14 is a stub |
| 7 | `deal_type` unnormalised: blank (3.77M), `DIRECT`/`Direct`, `BROKERED`/`Brokered`, `IST`, one `SELECT` | Cosmetic but visible in any sample file |
| 8 | Live DQ warnings: 10 `invalid_isin` (bondcentral), 10 `unclassified_auction_type`, 6 STRIPS `price_out_of_range` | Small; fix before sending samples |

---

## 3. The product catalogue

23 products. Effort is engineering-days from today's warehouse. Price bands are indicative
annual, per customer, for a single institution unless the row says otherwise.

### Tier 1 — shippable now

| # | Product | Built from | Buyer | Regulatory hook | Price band |
|---|---|---|---|---|---|
| **P1** | **Sovereign EOD valuation feed** — official FBIL price + YTM per ISIN, daily, G-Sec/SDL/STRIPS, 5.5 years deep | `valuations` (5.64M) | All 8 bank classes (~1,800), AIFs, insurers | CB 74/76, SFB 72/74, UCB 58/59, RCB 35/36 etc. | ₹1.5–4 L |
| **P2** | **Sovereign yield-curve feed** — G-Sec Par, GOI ZCYC, SDL ZCYC; 200-point quarterly grid to 50y | `yield_curves` (373k) | All 8 bank classes | CG-equivalent-maturity valuation; ZCB via ZCYC; STRIPS via zero-coupon yields | ₹1–3 L (bundle with P1) |
| **P3** | **Sovereign trade tape & liquidity** — LTP, LTY, WAP, WAY, trade count, turnover per ISIN/day, back to 2002 | `trades` (CCIL, 647k) | CB/SFB/PB/LAB (~60) + auditors; global aggregators | Active-market test for L1/2/3; Level-3 CET1 penalty | ₹4–12 L · wholesale ₹20L–1cr |
| **P4** | **Corporate trade-level tape** — every RFQ/OTC-reported transaction: time, price, yield, venue, deal type, settlement, put/call date | `corporate_trades` (4.86M) | Banks (all 8 classes), AIFs, MFs, OBPPs, auditors | **15-day traded-price cap — present in all 8 Directions**; MF waterfall; AIF NAV | ₹4–12 L · wholesale ₹20L–1cr |
| **P5** | **Corporate daily tape (18-year archive)** — per-ISIN daily summary, 34,156 ISINs from 2008 | `trades` (nse_cbm, 601k) | Research, quants, aggregators, credit desks | — (commercial, not mandated) | ₹3–8 L |
| **P6** | **Independent price-verification pack** — reconciliation report + matched-pair dataset: FBIL↔CCIL (158,189 pairs, median 0.0367, p99 1.90) and corporate↔CBM (4.36M pairs) | `valuations` × `trades` × `corporate_trades` + `data_quality_checks` | Bank audit/finance functions, statutory auditors, AIF valuers | Evidence for the valuation *they already buy elsewhere* — non-competing with CRISIL | ₹2–6 L, or per-report ₹50k–2 L |
| **P7** | **Reference/securities master** — 56,470 ISINs, 6 instrument types, coupon, maturity, issuer, face value, interest type, plus day-count convention, coupon frequency, next coupon date, issuance date, listing status (with real change history on coupon dates) | `securities` + `security_attribute_history` (nse_cbr) | Everyone; mandatory bundle with P1/P4 | Accrual and day-count computation | ₹1–3 L (bundle) |

### Tier 2 — small build on data we already hold

| # | Product | Built from | Buyer | Hook | Effort | Price band |
|---|---|---|---|---|---|---|
| **P8** ✅ | **Corporate liquidity scores** *(built — `security_liquidity`)* — per ISIN: days since last print, days traded (1/3/12m), turnover, print count, staleness flag, active-market verdict | P4 + P5, derived | ~60 CB-class banks + Big-4 auditors + AIFs + OBPPs | L1/2/3 active-market test (frequency + volume) | 3–5 d | ₹3–10 L |
| **P9** ✅ | **15-day valuation-cap service** *(built — `traded_within_15d` + the valuation engine)* — upload holdings, get last traded price within 15 days per ISIN with venue/volume evidence; falls through to spread grid when absent | P4 + P10 | All 8 bank classes | The single most broadly mandated data need in the family | 5–8 d | ₹2–6 L |
| **P10** ✅ | **RBI spread-grid valuation engine** *(built — `bonds.sovereign` + `bonds.valuation`; rating grid is a placeholder, see §6)* — CG curve at equivalent maturity + rating-graded ≥50bp; special-security tagging (UDAY direct-to-FBIL, DISCOM state-guaranteed +75bp, other DISCOM +100bp, state-serviced +50bp, oil/fertiliser/recap +25bp, other approved +25bp) | P2 + P7 + new special-security tag table | All 8 bank classes; the actual compliance deliverable | Whole corporate/other-approved valuation chapter | 10–15 d (tagging is the work) | ₹4–15 L |
| **P11** | **State (SDL) spread & curve pack** — per-state yield curves, state-vs-CG spread series, state-vs-state comparison, 8,872 ISINs | `valuations` + `yield_curves` + fixed issuer normalization | AMCs, insurers, state treasuries, research, media | Nobody else does state-level | 3 d (after defect #3) | ₹2–6 L |
| **P12** | **Index family** — sovereign and **state-level** SDL indices, custom tenor buckets, licensed for passive products | P1 + P3 | AMCs, ETF issuers, OBPPs | NSE Indices changed SDL methodology 7-Aug-2026; CCIL runs one SDL index — none go state-level | 15–20 d + governance | licence ₹10–50 L/product |
| **P13** | **Point-in-time / restatement history** — bitemporal replay: "what was the official mark as known on date X", 3,466 captured restatements | `valuations` incl. superseded | Auditors, litigation/dispute support, quant backtests | Audit defensibility; RBI portfolio restatement | 2 d (API surface) | ₹2–8 L |
| **P14** ✅ | **Primary market calendar + auction results** *(built — `rbi_auction_results`)* — public issues (433, 17-year series) and RBI auctions **with cut-off yields and notified/accepted amounts** (not currently captured) | `public_issues`, `rbi_auctions` + new parser | OBPPs, banks' treasury, research | Primary-market pricing reference | 5–8 d | ₹1–3 L (add-on only) |
| **P15** | **Rating snapshot now, migration feed later** — 8,958 rated ISINs with agency + rating date; 5,109 of them traded | `security_attribute_history` | Credit desks, AIFs, OBPPs, banks | Rating-graded valuation; Board rating-migration review (quarterly CB/SFB/PB/LAB/AIFI/UCB, half-yearly RRB/RCB) | 0 d snapshot; migration accrues with time | ₹1–3 L snapshot; ₹4–10 L once migrations exist |
| **P16** | **Amount outstanding / issue size** — 2,877 ISINs, half-yearly CDSL | `security_attribute_history` (cdsl) | Index construction, liquidity weighting, credit exposure | — | 2 d (schedule the 30-Sep-2026 snapshot) | ₹1–2 L (add-on) |

### Tier 3 — applications and channel products (higher price, real build)

| # | Product | What it is | Buyer | Effort | Price band |
|---|---|---|---|---|---|
| **P17** | **White-label valuation-compliance module** | P1+P2+P10+P9 as an embeddable service inside a core-banking or treasury product; one integration reaches hundreds of banks and we carry no end-user support | C-Edge (TCS–SBI JV), Nelito FinCraft (110+ banks / 6,000+ sites), Virmati (45+ banks), Finacle SaaS for UCBs, TCS BaNCS co-op, **NUCFDC "Bank in a Box"** | 20–30 d | **₹25 L–1 cr / vendor / yr** |
| **P18** | **AIF & PMS independent-valuation pack** | Security-level prices + liquidity evidence + as-of-date attestation, sized for a fund administrator's NAV cycle | 1,992 AIFs (₹6.45 lakh crore), ~490 PMS, fund admins | 10–15 d | ₹2–8 L direct; ₹15–40 L via administrator |
| **P19** | **Auditor evidence pack** | L1/2/3 classification support: per-ISIN active-market verdict with trade frequency/volume, plus P6 reconciliation, as a signed quarterly deliverable | Big-4 + statutory auditors of the ~60 CB-class institutions | 8–12 d | ₹5–20 L / firm |
| **P20** | **OBPP retail data API** | Liquidity badge, last traded price/yield, yield-to-maturity, rating, coupon calendar, comparable-bond yields — the numbers a retail bond app must display | 29+ OBPPs, newly permitted IFSCA + 54EC/Sec-85 products | 10–15 d | ₹5–15 L each |
| **P21** | **Wholesale/research licence** | Full history bulk licence: 24-yr sovereign + 18-yr corporate + 12-yr transaction tape, Parquet/SFTP | Cbonds and peers (buy local feeds as a business model), quant funds, academia, IMF/BIS-type research | 5 d packaging | **₹20 L–1 cr / yr** |
| **P22** | **Municipal bond dataset** | ~350 municipal/civic ISINs with prices, prints and reference data; a land-grab ahead of SEBI's ILMDS reform (pooled issuance, ₹10k face value, ESG-linked) | OBPPs, ESG funds, municipal issuers, SEBI/RBI research | 8–12 d | ₹1–4 L today; option value high |
| **P23** ✅ | **Trade-derived corporate spread matrix** *(built — `corporate_spread_matrix`)* | Our own daily rating × maturity spread grid computed from 4.86M actual prints — FIMMDA's is poll-based to AA− and fixed-for-3-months below AA− | Banks, AIFs, MFs, auditors; a genuine methodological differentiator | 15–20 d | ₹5–20 L |

### Tier 4 — not products (and why)

| Item | Verdict |
|---|---|
| **SGB** (42 ISINs, 1,023 trade rows) | Add-on curiosity. Cbonds already publishes SGB as % of gold reference; the scheme is in run-off |
| **T-Bill valuation** | Every one of the nine Directions carries T-Bills **at carrying cost** — there is no valuation demand. Keep the 1,855-ISIN universe as reference only |
| `ingestion_runs` / `etl_file_metrics` | Internal — but the 61,396 persisted DQ verdicts are the *substance* behind P6 and should be shown, not sold |
| **NSE live OTC segment** (3,874 rows, 7 weeks) | Same "wait" verdict the corporate tape had in July. Revisit at 6 months |
| **Mutual fund valuation channel** | Legally closed — AMFI appoints CRISIL and ICRA. Do not build for it |
| **NBFCs as a compliance pitch** | Their Direction (354) contains no valuation framework at all. Sell to them on Ind AS 113, or not at all |

---

## 4. Channels, ranked by revenue per unit of effort

1. **NUCFDC** — the UCB umbrella organisation. **~650 UCBs (about half the sector) have already
   joined**, and its "Bank in a Box" bundles core banking, payments, cloud, cybersecurity and
   **regulatory compliance** through one platform; new Mumbai office and SOC launched August 2026.
   This is the single highest-leverage conversation available to us: one integration, ~1,700 banks
   that individually cannot afford CRISIL. Product: **P17**.
2. **Core-banking / treasury vendors** — C-Edge, Nelito, Virmati, Finacle SaaS-for-UCBs, TCS BaNCS.
   Same economics, no support burden. Product: **P17**.
3. **Wholesale to global aggregators** — Cbonds and peers buy local feeds by design; their India
   depth is thin. One contract, zero end-user support. Product: **P21**, **P3**, **P4**.
4. **AIF/PMS and fund administrators** — a dated 1-May-2026 obligation across 1,992 funds with
   procurement cycles measured in weeks. Product: **P18**.
5. **Audit firms** — sell once, reach the ~60 institutions they audit, and they become the
   reference that makes bank conversations credible. Product: **P19**, **P6**.
6. **OBPPs** — fastest close, smallest cheque, best reference logos; scope just expanded. Product:
   **P20**.
7. **Co-op banks direct** — real aggregate money, brutal one-by-one sales. Only after (1) or (2).

---

## 5. Packaging and pricing

- **Delivery**: daily Parquet + CSV over SFTP for feeds (P1–P5, P7), REST API for lookups
  (P8, P9, P13, P20), one-off bulk drop for history (P21), and a scheduled PDF/XLSX deliverable
  for evidence products (P6, P19). Start with SFTP — every bank IT function already knows how to
  consume it and no procurement asks about our uptime.
- **Bundles**:
  - **Sovereign Core** = P1 + P2 + P7 → the mass-market feed, ~₹2–5 L
  - **Compliance Suite** = Sovereign Core + P9 + P10 + P8 → the actual RBI deliverable, ~₹8–20 L
  - **Corporate Pro** = P4 + P5 + P8 + P15 + P23 → premium, ~₹15–35 L
  - **Full History** = everything, bulk, redistribution rights → P21, ₹20 L–1 cr
- **Anchor**: Cbonds lists global bond pricing from US$450/month and global reference data from
  US$350/month. Our pitch is *narrower but deeper* — price Sovereign Core below their entry point
  and Corporate Pro above it.

---

## 6. Build backlog — closed

Everything on the 2026-09-07 backlog is now built. `bonds dq assess` reports 0 errors; 383 unit
and 55 integration tests pass.

| # | Work | Status |
|---|---|---|
| 1 | Fix the CCIL ingest; make a mis-recorded empty day retryable | **Done** (2026-09-08). CCIL + BSE connectors fixed, 447 poisoned lake artifacts cleared, zero-row successes made correctable, 31 days recovered. `Freshness` DQ dimension catches both signatures on night one |
| 2 | Re-fix SDL issuer normalization + a vocabulary DQ check | **Done.** `bonds.states` shared by both connectors; closed 31-state vocabulary enforced; 63 → 31 issuers |
| 3 | Withdraw the rating-migration claim | **Done.** Corrected in the business case and the sellable-data inventory |
| 4 | Sub-type the `GSEC` bucket | **Done**, and re-diagnosed — 233 mis-typed state securities, not a mixed bucket. ISIN form now outranks the worksheet title |
| **5** | **Liquidity/staleness derived table (P8)** | **Done.** `security_liquidity`: last print, prints and distinct trading days over 1/3/12m, turnover, plus the two regulatory verdicts — `traded_within_15d` (clause 78(1)(i)(c)) and `active_market` (clause 4(1)). 13,370 ISINs per as-of date; **956 currently meet the active-market test**. Point-in-time by construction, so "was this market active on 31 March" is answerable a quarter later |
| 6 | Schedule the CDSL snapshot into catch-up | **Done.** Re-attempts the due 31-Mar/30-Sep report until it publishes |
| **7** | **RBI auction results parser (P14)** | **Done.** `rbi_auction_results`: notified vs accepted amount, cut-off price and yield, weighted average, partial allotment, and a derived bid-cover ratio — per security. All three release layouts parse against the published figures: G-Sec (incl. single-security days), T-Bill (3 tenors + WAY), SDL (30 state loans split across sibling tables inside a wrapper). `rbi_auctions` is no longer a calendar stub |
| **8** | **Special-security tagging + spread grid (P10)** | **Done.** `bonds.sovereign` classifies UDAY / DISCOM / state-serviced / SPECIAL_GOI / oil / FCI / fertiliser / recap / FRB / IIB from the description, disambiguating the shared "SPL" token by ISIN form. `bonds.curves` interpolates any maturity off the published quarter-year grid; `bonds.valuation` applies the rule — CG at equivalent maturity plus the class or rating spread, with the 15-day traded cap. **Grid caveat below** |
| **9** | **Trade-derived spread matrix (P23)** | **Done.** `corporate_spread_matrix` on FIMMDA's own tenor buckets, so the comparison is cell by cell. 71 cells on 2026-09-04 from a 30-day window; monotonic in rating (AAA 52–169bp, AA ~240–300, A ~440–500, BBB+ ~760–1000). Thin cells are kept, not suppressed — `trade_count` and `isin_count` are how a caller sees why a cell is odd, and one cell currently prices through the CG curve on 27 prints |
| 10 | Normalize `deal_type`; clear the ISIN and STRIPS warnings | **Done.** Five categoricals canonicalised at the model boundary; ISIN shape (ERROR) split from check digit (WARN); STRIPS given a deep-discount price band |

### Two placeholders that must not ship as fact

Both are flagged in-code at the point of use, and both are the right *shape* with the wrong
numbers:

1. **`DEFAULT_RATING_SPREAD_BP`** (the rating-graded corporate grid). Clause 78(1)(i)(a) is
   recorded in our regulatory notes only as "CG equivalent-maturity + ≥50bp, rating-graded" —
   the per-rating figures were never extracted from the Direction text. The defaults honour the
   50bp floor and step monotonically down a full AAA→D scale. **Read the clause and replace them
   before any engine output reaches a client**, or pass your own grid to `price_corporate`.
2. **`ACTIVE_MARKET_MIN_DAYS_TRADED_3M` / `_MIN_PRINTS_3M`.** Clause 4(1) defines an active
   market qualitatively and names no threshold — deliberately, because it is an accounting
   judgement the bank and its auditor make per instrument. 15 trading days and 30 prints per
   quarter separate the ~1,000 instruments that trade regularly from the ~11,000 that print a
   handful of times a year, which is a defensible starting point and nothing more. Every client
   must set and justify their own.

Two bugs worth recording, both found by validating against the real data rather than by testing
the happy path:

- **`'WITHDRAWN'` was matching as an `A` rating** — the bare `A` alternative matched the A inside
  the word — which valued 143 withdrawn-rating ISINs as single-A paper. And a trailing `\b`
  after the +/- modifier silently dropped every notch (`CRISIL AA+` read as `AA`, understating
  the spread on 760 ISINs). All 66 published rating strings now map correctly: 62 to on-grid
  notches, 4 to "no rating".
- **A central guard I added and backed out.** Enforcing "a date-series load writing zero rows is
  a skip" in the pipeline runner is wrong: `work()` returns rows *written*, and a bitemporal
  upsert legitimately writes zero when re-running an unchanged day. It wrongly skipped 84 healthy
  curve-days. The invariant belongs in the connectors, which know what the *source* returned.

### Operational note found while fixing item 1

The installed launchd agent runs at **13:00**, not the 21:00 the repo shipped — a deliberate
change (the machine is reliably awake at midday) whose stated rationale was "catch-up gap-fills
it". That reasoning holds only for sources that signal *no data* correctly. It held for FBIL
(HTTP 500 → skip → retried) and silently failed for CCIL and BSE. The shipped plist and
`scripts/README.md` now carry the 13:00 schedule **and** that dependency.

---

## 7. Caveats and sources

**Caveats.** Warehouse figures are live queries at 2026-09-07 22:50 IST and will drift; §2.3 and
§6 were revised on 2026-09-08 after the defects were fixed, so the row counts in §2 predate those
repairs (the repairs re-typed rows, they did not add or remove any). Institution
counts are public press/regulator figures, not counted from primary registers. Price bands are our
estimates anchored on the one published benchmark we could find (Cbonds/Datarade list prices) plus
the July business case — no incumbent publishes list pricing for Indian bond valuation services.
The regulatory clause mapping is carried over from `2026-07-27_222030_rbi-investment-directions-demand-map.md`
and was not re-verified today. Source redistribution rights are out of scope for this document by
instruction; §3 assumes they are obtainable.

**Sources (retrieved 7 September 2026).**

- Market size and issuance: [IndiaBonds — size of the Indian bond market](https://www.indiabonds.com/bonduni/blogs/size-of-the-indian-bond-market/) · [GoldenPi — India's bond market in numbers, June 2026](https://goldenpi.com/blog/bond-news/indias-bond-market-in-june-2026/) · [NSE–Assocham corporate bond report](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE_Assocham_Corporate_Bond_Report_2024.pdf) · [ICSI — deepening the corporate bond market in India](https://www.icsi.edu/media/webmodules/CSJ/January_2026/42.pdf) · [Business Standard — corporate bond secondary turnover FY25](https://www.business-standard.com/amp/finance/news/corp-bond-secondary-mkt-daily-turnover-grows-around-2k-cr-in-fy25-rbi-125052900897_1.html)
- AIF valuation: [SEBI circular, 6 Feb 2026 (via NSDL)](https://nsdl.co.in/downloadables/pdf/04_SEBI_Circular_dated_6_February_2026.pdf) · [Angel One — 1 May 2026 deadline for AIF independent NAV reporting](https://www.angelone.in/news/mutual-funds/sebi-sets-may-1-2026-deadline-for-aifs-to-report-independent-navs-to-depositories) · [Treelife — AIF valuation in India](https://treelife.in/finance/aif-valuation-in-india/) · [AIF directory — 1,884 registered AIFs](https://aifindia.net/directory)
- OBPP: [Business Standard — SEBI allows OBPPs to offer GIFT-IFSC bonds, 14 Aug 2026](https://www.business-standard.com/markets/news/sebi-allows-online-bond-platforms-to-offer-gift-ifsc-regulated-bonds-126081401595_1.html) · [LiveLaw Biz — SEBI modifies OBPP framework](https://www.livelawbiz.com/amp/securities-law/sebi/securities-and-exchange-board-of-india-allows-online-bond-platforms-offer-ifsca-regulated-products-tax-specified-bonds-546254) · [GoldenPi — what is an OBPP](https://goldenpi.com/blog/bond-news/what-is-an-obpp-online-bond-platform-provider/)
- Municipal reform: [Business Today — can SEBI's new rules make municipal bonds safer](https://www.businesstoday.in/amp/personal-finance/investment/story/can-sebis-new-rules-make-municipal-bonds-safer-for-retail-investors-531370-2026-05-14) · [CorpLawUpdates — SEBI 2026 municipal debt consultation](https://www.corplawupdates.in/updates/sebi-2026-municipal-debt-securities-consultation)
- Insurers: [Business Standard — insurers urge IRDAI to revamp bond valuation](https://www.business-standard.com/markets/news/indian-insurers-urge-irdai-to-revamp-bond-valuation-to-boost-debt-market-125070400589_1.html) · [TaxGuru — IRDAI (Actuarial, Finance and Investment Functions) Amendment Regulations 2026](https://taxguru.in/corporate-law/irdai-actuarial-finance-investment-functions-insurers-amendment-regulations-2026.html)
- Competitors and benchmarks: [CRISIL bond valuation methodology](https://www.crisil.com/content/dam/crisil/our-businesses/india_research/fixed-income-research/valuations/crisil-security-valuation/Bond-valuation.pdf) · [CRISIL SDL valuation methodology](https://www.crisil.com/content/dam/crisil/our-businesses/india_research/fixed-income-research/valuations/crisil-security-valuation/state-development-loans-valuation.pdf) · [FIMMDA daily yield/spread matrix methodology](https://www.fimmda.org/uploads/general/CB_Meth_for_computing_daily_yield_spread_matrix_Version_12-Apr-18.pdf) · [FIMMDA corporate bond valuation methodology, Mar 2023](https://fimmda.org/UploadPopupPageFiles/FIMMDA_CB_Methodology_Document_March2023.pdf) · [Cbonds bond data API](https://cbonds.com/api/) · [Datarade — Cbonds global bond pricing, from US$450/mo](https://datarade.ai/data-products/market-data-api-global-coverage-350-pricing-sources-cbonds) · [Datarade — Cbonds global reference data, from US$350/mo](https://datarade.ai/data-products/reference-bond-data-api-global-coverage-600k-issues-cbonds) · [Cbonds India country page](https://cbonds.com/country/India-bond/) · [NSE data sharing and usage policy](https://www.nseindia.com/static/market-data/nse-data-policy) · [NSE domestic market data pricing file, Mar 2026](https://nsearchives.nseindia.com/web/mediaattachment/2026-03/NSE_Pricing_file_-_Domestic_clients_20260309171343.pdf)
- Indices: [NIFTY 10-Year SDL index](https://www.niftyindices.com/indices/fixed-income/sdl-indices/nifty-10-year-sdl-index) · [NSE Indices SDL methodology change effective 7 Aug 2026](https://tradersunion.com/news/financial-news/show/2906569-nse-nifty-sdl-index-changes-2026/) · [CCIL SDL index methodology](https://www.ccilindia.com/documents/43866/1292813/CCIL%20SDL%20Index%20Methodology_1716298378363.pdf)
- Mutual fund valuation: [Business Standard — SEBI waterfall approach for debt valuation](https://www.business-standard.com/amp/article/pti-stories/sebi-asks-mfs-to-adopt-waterfall-approach-for-money-mkt-debt-securities-valuation-119092500603_1.html)
- Channel: [NUCFDC](https://nucfdc.in/) · [NUCFDC brochure 2026](https://nucfdc.in/uploads/images/nucfdc-brochure_2026_.pdf) · [PIB — umbrella organisation for UCBs](https://www.pib.gov.in/PressReleaseIframePage.aspx?PRID=2085686&reg=48&lang=2) · ["Bank in a Box" launch](https://edunovations.com/currentaffairs/national/bank-in-a-box-ucbs/) · [C-Edge treasury management](https://cedge.in/treasury-management-solution/) · [Nelito FinCraft core banking](https://www.nelito.com/digital-banking/core-banking-system.html) · [Virmati iCBS](https://www.virmati.com/profile/core-banking-system.html) · [Finacle SaaS for urban co-operative banks](https://www.finacle.com/solution/finacle-digital-banking-saas-urban-cooperative-banks-india/)
