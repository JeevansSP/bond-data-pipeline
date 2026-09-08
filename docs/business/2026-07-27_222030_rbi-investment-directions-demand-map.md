```
Nine-way map of the RBI (Classification, Valuation and Operation of Investment Portfolio)
Directions, 2025 — all issued 28 November 2025 — showing which data-dependent mandates apply to
which class of institution, and what that means for the sellable-data pitch. Completes the
regulatory reading begun with the Commercial Bank / UCB / RCB Directions; the six remaining
Directions (SFB, Payments Banks, LAB, RRB, AIFI, NBFC) were read for this doc.

2026-07-27_222030 : initial version — six new Directions read (SFB 193, PB 214, LAB 236, RRB 260, AIFI 329, NBFC 354); consolidated with the earlier CB/UCB/RCB findings
```

# RBI investment Directions — nine-way regulatory demand map

**Scope and provenance.** All nine Directions in the family, issued 28 November 2025, are now
read. CB/UCB/RCB clause references come from the earlier close reading (26–27 July 2026); the six
below were read from the full texts on rbi.org.in on 27 July 2026 (versions "updated as on
18 May 2026"; NBFC as on 1 July 2026). PDF downloads are bot-blocked (F5 challenge on
rbidocs.rbi.org.in), but the complete texts are embedded in the rbi.org.in HTML pages.

| Class | Reference | Full-text page id | Regime |
|---|---|---|---|
| Commercial Banks (CB) | RBI/DOR/2025-26/162 | 13148 | 2023-style: HTM/AFS/FVTPL + SPPI + L1/2/3 |
| Small Finance Banks (SFB) | RBI/DOR/2025-26/193 | 13116 | Same as CB |
| Payments Banks (PB) | RBI/DOR/2025-26/214 | 13094 | Same as CB, tiny eligible universe |
| Local Area Banks (LAB) | RBI/DOR/2025-26/236 | 13071 | Same as CB |
| Regional Rural Banks (RRB) | RBI/DOR/2025-26/260 | 13046 | Legacy HTM/AFS/HFT, no L1/2/3 |
| Urban Co-op Banks (UCB) | RBI/DOR/2025-26/284 | 13021 | Co-op categories, no L1/2/3 |
| Rural Co-op Banks (RCB) | RBI/DOR/2025-26/309 | 12995 | Co-op categories, no L1/2/3 |
| AIFIs (EXIM, NABARD, SIDBI, NHB, NaBFID) | RBI/DOR/2025-26/329 | 12975 | Legacy HTM/AFS/HFT, no L1/2/3 |
| NBFCs (incl. HFCs, CICs) | RBI/DOR/2025-26/354 | 12950 | **No valuation framework — defers to accounting standards** |

Full-text URL pattern: `https://rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=<page id>`.

---

## 1. The headline findings from the six new Directions

### 1a. The FBIL price + CG-curve + 15-day-rule stack is universal across every *bank* class

Every one of the eight bank-class Directions — regardless of which accounting regime it uses —
carries the identical core valuation stack:

| Mandate | CB | SFB | PB | LAB | RRB | AIFI | UCB | RCB |
|---|---|---|---|---|---|---|---|---|
| Quoted → FBIL prices, exchange/platform/FIMMDA fallback | 74 | 72 | 73 | 73 | 52 | 46 | 58 | 35 |
| Unquoted CG (and SDL) → FBIL prices/YTM | 76 | 74 | 75 | 75 | 54 | 47(1), 49 | 59(1), 60 | 36(1), 37 |
| T-Bills at carrying cost | 75 | 73 | 74 | 74 | 53 | 47(2) | 59(2) | 36(2) |
| Other approved securities: CG + 25bp | 77 | 75 | 76 | 76 | 55 | — | 61 | 38 |
| Rated corp bonds: CG equivalent-maturity + ≥50bp, rating-graded | 78(1)(i)(a) | 76(1)(i)(a) | 77(1)(i)(a) | 77(1)(i)(a) | 56(2)(i) | 50(3)(i) | 62(3)(i) | 39(1) |
| Unrated ≥ rated equivalent maturity | 78(1)(i)(b) | 76(1)(i)(b) | 77(1)(i)(b) | 77(1)(i)(b) | 56(2) | 50(3)(ii) | 62(3)(ii) | 39(2) |
| **15-day traded-price cap — corp bonds** | 78(1)(i)(c) | 76(1)(i)(c) | 77(1)(i)(c) | 77(1)(i)(c) | 56(2)(ii) | 50(3)(iii) | 62(3)(iii) | 39(4) |
| **15-day traded-price cap — preference shares** | 79(1) | 77(1) | 78(1) | 78(1) | 58(1) | 53(6) | — | — |
| UDAY → FBIL directly | 78(2)(i) | 76(2)(i) | 77(2)(i) | 77(2)(i) | — | 51(1) | — | — |
| DISCOM state-guaranteed +75bp / other DISCOM +100bp / state-serviced +50bp | 78(2)(ii)-(iv) | 76(2)(ii)-(iv) | 77(2)(ii)-(iv) | 77(2)(ii)-(iv) | — | 51(2)-(4) | — | — |
| Special GoI securities (oil/fertiliser/recap etc.) +25bp | 78(3) | 76(3) | 77(3) | 77(3) | 57 | 48 | 63 | — |
| ZCB via ZCYC + FIMMDA/FBIL zero-coupon spreads | 78(4) | 76(4) | 77(4) | 77(4) | **absent** | 52(2) | 66 | 43 |
| STRIPS via FBIL zero-coupon yields | 88(5)(ii) | 86(5)(ii) | 94(3)(ii) | 87(4)(ii) | **absent** | 64(1)-(2) | 100(2) | — |
| CP at carrying cost | 82 | 80 | 81 | 81 | 61 | 56 | 65 | 42 |
| CD at carrying cost | 82 | **absent** | **absent** | **absent** | 61 | **absent** | 65 | 42 |

Two consequences for the pitch:

- **One data product serves every regulated bank class in India.** FBIL G-Sec/SDL prices+YTM,
  the FBIL CG yield curve by equivalent maturity, the ZCYC with zero-coupon spreads, and
  per-ISIN traded prices for the 15-day rule cover the valuation mandate of ~40 commercial
  banks, ~11 SFBs, ~6 PBs, ~2 LABs, 43 RRBs, 5 AIFIs and ~1,700 co-operative banks. The clause
  numbers shift; the data requirement does not.
- **The 15-day traded-price rule is in all eight, and in six of them it also covers preference
  shares.** Corporate trade-level prices across recognised exchanges/platforms remain the single
  most broadly mandated dataset we do not yet have at depth (NSE corp trades: 8 days old).

### 1b. The Level 1/2/3 population is ~60 institutions, not 1,700

The fair-value-hierarchy finding (active-market test = per-instrument trade frequency and
volume; disclosure effective from audited FY ending 31 March 2026; Level-3 unrealised gains
deducted from CET1, no dividends) exists **only in the four CB-style Directions**:

| | Active market defn | L1/2/3 disclosure | Level-3 CET1/dividend penalty |
|---|---|---|---|
| CB | 4(1) | 86 (eff. FYE 31-Mar-2026) | 87 |
| SFB | 4(1) | 84 (same date) | 85 |
| PB | 4(20)-(22) | 84 (same date) | 85 |
| LAB | 4(20)-(22) | 85 (same date) | 86 |
| RRB / AIFI / UCB / RCB | — | **absent** | **absent** |

All four extend the same L1/2/3 + Level-3 penalty to their **derivatives** portfolios (CB 110,
SFB 108-109, PB 117-118, LAB 109-110), and all four carry the same proviso: the Level-3 CET1
deduction does not apply to SPPI-compliant investments risk-weighted ≤50%.

So the "trade frequency and volume as audit evidence" product — the one our CCIL
`no_of_trades`/`trade_value`/`wap` asset answers directly — has a buyer population of roughly
**40 CBs + 11 SFBs + 6 PBs + 2 LABs ≈ 60 institutions**, plus their auditors. It is still the
strongest single finding (new obligation, first audited year just closed, capital penalty), but
it is a *premium product for the top of the market*, not the mass-market co-op play. The
mass-market play (1,700 co-ops + 43 RRBs) is the FBIL price/curve feed at monthly/quarterly
valuation cadence.

### 1c. NBFC: the Direction most likely to differ — and it differs by being empty

The NBFC Direction (354, the smallest file) contains **no prescriptive valuation framework at
all**. Para 6: Ind AS NBFCs follow Ind AS and the separate Financial Statements Presentation &
Disclosures Directions; investments are classified "as per the applicable accounting standards"
(paras 8-9). Only **non-Ind AS NBFCs** get valuation rules (paras 14-21), and they are minimal:
quoted current investments at lower-of-cost-or-market **aggregated category-wise** (15), unquoted
G-Sec/government-guaranteed at carrying cost (18), unquoted MF at NAV (19), CP at carrying cost
(20), long-term per accounting standards (21). There is **no FBIL mandate, no yield curve, no
15-day rule, no L1/2/3, no rating-based valuation grid** anywhere in it.

Implications:

- **Do not pitch "RBI compliance" to NBFCs on the back of this Direction.** The compliance
  driver does not exist for them in this family.
- NBFC demand for our data is real but comes from **Ind AS 113 fair-value measurement** (the
  same Level 1/2/3 logic, in force for large NBFCs since FY 2018-19 via the Companies (Ind AS)
  Rules) — an accounting obligation, not a new RBI one. Different pitch, no regulatory-deadline
  urgency, competitive space already served by auditors' pricing sources.
- Non-Ind AS NBFCs (the small ones, below the ₹250-crore net-worth Ind AS threshold) do need
  *market values for quoted current investments* (para 15) — our FBIL/CCIL data serves that,
  but no source is named, so there is no mandate moat.
- Two operational notes: NBFCs must report OTC corp bond trades within 15 minutes to
  NSE/BSE/MCX-SX (13) — context on where corp trade data originates; and the AIF
  debtor-company/priority-distribution restrictions (25-27) are the bulk of what is genuinely
  new here, none of it data-serviceable by us.

### 1d. RRBs and AIFIs stayed on the legacy framework

Neither got the 2023-style regime:

- **RRB (260)**: legacy HTM/AFS/HFT (26); hard HTM cap of 25% of total investments (29) with
  SLR-in-HTM bounded by 19.5% of NDTL (30) and non-SLR barred from HTM (31); HFT must be sold
  within 90 days (33); HFT marked monthly, AFS quarterly (46-47); equity daily-preferred/weekly-
  minimum (59(1)); no ZCYC, no STRIPS provisions, no SPPI, no L1/2/3. IFR **kept at 2% of
  AFS+HFT** (104, as substituted 18-May-2026), Tier-2 eligible uncapped (105(4)). Non-SLR regime
  is the strictest of the family: no unrated and/or unlisted non-SLR at all (76(1)),
  investment-grade floor (76(2)), incremental non-SLR ≤5% of incremental deposits (77), no
  financing of NBFCs (76(4)). Board rating-migration review **twice a year** (87(4)); issuer
  tracking quarterly/half-yearly (19). Rating confirmed from the CRA **website** (4(2)).
  Sponsor-bank quarterly portfolio review (24) and NABARD reporting with fixed deadlines
  (98-100).
- **AIFI (329)**: same legacy trinity (17); HTM ≤25% of total investments (18); HFT 90-day rule
  (22); AFS quarterly, HFT **monthly** (41, 44) — a frequency downgrade vs CB's daily HFT;
  no SPPI, no L1/2/3. ZCYC/STRIPS machinery **is** present (52, 64). Investment Reserve Account
  retained and Tier-2 eligible within 1.25% of RWA (105-108) — no sunset, unlike CB's IFR.
  HTM sales beyond 5% of opening book trigger disclosure only (33), not restriction. Rating via
  CRA **website** (4(3)). NHB runs a June-30 half-year calendar unlike the other four (71,
  101). Applicability confirmed as exactly EXIM, NABARD, SIDBI, NHB, NaBFID (3).

### 1e. Payments Banks: perfectly served by the sovereign product, but tiny

PB investment rules (86-93) bind the real book: **≥75% of demand-deposit balances in ≤1-year
SLR-eligible G-Secs/T-Bills** (86), ≤25% in SCB deposits (87), combined ≥100% (88); HTM
restricted to own-funds investments (90); derivatives only for FX hedging (93); no when-issued
or short sales (91). T-Bills sit at carrying cost, so the actually-priced sovereign sliver is
small; the corporate valuation machinery (77-83) is live only for own-funds positions behind
strict gates. Six institutions, modest data need, fully covered by what we already have — a
checklist segment, not a revenue segment.

### 1f. The 18 May 2026 amendment split the family on IFR

Same-day amendments, opposite directions — useful as evidence that RBI actively maintains this
family per class:

| Class | IFR/IRA status |
|---|---|
| CB | Discontinued w.e.f. 18-May-2026 (105) |
| LAB | Discontinued w.e.f. 18-May-2026 (104; 105-107 deleted) |
| SFB | **Kept, substituted 18-May-2026: ≥2% of AFS+FVTPL(incl HFT)** (103) |
| PB | **Kept, substituted 18-May-2026: ≥2% of AFS+FVTPL(incl HFT)** (112) |
| RRB | **Kept, substituted 18-May-2026: ≥2% of AFS+HFT** (104) |
| AIFI | IRA retained, Tier 2 within 1.25% RWA, no sunset (105-108) |
| UCB / RCB | Mandatory 5% (153(4) / 115(1)) |

Every "kept" row is an annual balance-sheet-date sizing exercise over the fair-valued portfolio
— a small recurring data hook (client positions × our prices).

### 1g. Rating provenance splits cleanly down the middle

Confirmation source for secondary-market acquisitions: **monthly bulletin** for CB (4(6)), SFB
(4(6)), PB (4(6)), LAB (4(6)); **CRA website** for UCB (4(7)), RCB (4(1)), RRB (4(2)), AIFI
(4(3)). Freshness is uniform family-wide: rating letter ≤1 month, rationale ≤1 year at issue
opening. Rating-migration Board review: quarterly for CB/SFB/PB/LAB/AIFI, twice-yearly for RRB
(87(4)), quarterly for UCB (119(4)), half-yearly for RCB (82(4)). Investment-grade floors and
unrated bans run family-wide (strictest at RRB). The bondcentral-sourced provenance problem
(secondary source, 36% ISIN coverage) applies to all of it.

---

## 2. Consolidated buyer-population × product matrix

| Product | Buyer classes with a named mandate | Institutions (approx.) | We have it? |
|---|---|---|---|
| FBIL G-Sec/SDL daily prices+YTM per ISIN | All 8 bank classes | ~1,800 | **Yes** (4.0M rows) |
| FBIL CG yield curve by equivalent maturity | All 8 | ~1,800 | **No — build** |
| ZCYC + FIMMDA/FBIL zero-coupon spreads | CB, SFB, PB, LAB, AIFI, UCB, RCB (not RRB) | ~1,760 | **No — build** |
| Corp trade-level prices, all exchanges (15-day rule) | All 8 | ~1,800 | Thin (8 days NSE) |
| Trade frequency+volume per ISIN (L1/2/3 evidence) | CB, SFB, PB, LAB (+ NBFC via Ind AS 113, unmandated) | ~60 (+auditors) | **Yes** (CCIL 645k rows; corp side missing) |
| Rating history / migration feed | All 8 (review cadence varies) | ~1,800 | Partial (8,958 ISINs, provenance gap) |
| SPPI-relevant instrument terms (convertibility, coupon type, call/put) | CB, SFB, PB, LAB only | ~60 | **No — build** |
| Special-security identification (UDAY/DISCOM/oil/fertiliser/recap + servicing entity) | CB, SFB, PB, LAB, AIFI (+RRB specials at +25bp) | ~110 | **No — build** |
| Sovereign short-end (≤1yr) universe for PB DDB rule | PB | 6 | **Yes** |

Population notes: institution counts are from public RBI/press figures, not from the Directions
themselves — none of the nine texts enumerates or counts institutions (each applicability
clause is a single sentence). RRB count (~43) reflects the 2025 amalgamations.

## 3. What changes in the pitch

1. **Lead stays the same, framing tightens.** The Level 1/2/3 evidence product remains the
   sharpest wedge (new obligation, first audited year FYE 31-Mar-2026 just closed, CET1
   penalty), but it is a ~60-institution premium product. Pitch it to CBs/SFBs and the Big-4
   audit firms, not to the co-op long tail.
2. **The mass-market feed now has confirmed regulatory cover in every bank class.** The
   FBIL-price + CG-curve product's addressable base is all ~1,800 banks; RRBs (43, freshly
   amalgamated, sponsor-bank-supervised, NABARD-reporting) slot naturally into the same
   software-vendor channel as the co-ops and were not previously counted.
3. **Drop NBFCs from the regulatory-compliance narrative.** Their Direction imposes no valuation
   data need. Keep NBFCs only in the Ind AS 113 / bond-platform storyline.
4. **The two curves are now non-negotiable build items.** CG-by-equivalent-maturity is required
   by all eight bank classes; ZCYC by seven. Without them we serve the sovereign-mark use case
   but not the corporate-valuation use case anywhere.
5. **Payments Banks and LABs are checklist segments** (six and two institutions) — worth a slide
   row for completeness ("every bank class covered"), not a sales motion.
6. **New secondary hooks**: IFR/IRA annual sizing (SFB/PB/RRB 2%, UCB/RCB 5%, AIFI 1.25%-RWA
   Tier-2 ceiling); AIFI/RRB monthly-HFT + quarterly-AFS cadence matches a daily feed trivially;
   preference-share 15-day rule adds a small dataset (exchange-traded preference shares) to the
   corporate-trades build.

## 4. Caveats

- The six new Directions were extracted by parallel close-reads of the full rbi.org.in texts;
  clause numbers above are as printed in the "updated as on 18 May 2026" versions (NBFC:
  1 July 2026). CB/UCB/RCB clauses are from the earlier session's reading and were not re-verified
  today.
- Drafting artifacts observed (evidence of template cloning, worth knowing before quoting
  clauses to a client): PB paras 106/107 are verbatim duplicates; PB 68 cross-references the
  *SFB* Financial Statements Directions; PB 82 points SR valuation at the *Commercial Banks*
  credit-risk-transfer Directions; AIFI chapter numbering skips XII; LAB 40(4)(iii) retains a
  CB cross-reference.
- Institution counts are external context, not Direction text.
- The licensing gate from the business case (FBIL/CCIL/NSE redistribution rights) is unchanged
  by any of this and still blocks packaging.
