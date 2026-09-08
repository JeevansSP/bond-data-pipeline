```
Internal business case for turning the bonds-research warehouse into a commercial data product —
the RBI/SEBI regulatory demand driver, competitive positioning against CRISIL/ICRA/CARE, go-to-market
channels and revenue estimate, and the source-licensing risk that gates the whole plan.

2026-07-26_173715 : transferred into docs/ from bond-data-business-case.md (authored 2026-07-26); rendered to PDF for circulation
2026-09-07_234200 : withdrew the "full history of rating changes" claim in asset 4 — verified against the warehouse, all 8,958 credit_rating rows are a single 18-Jul-2026 snapshot with zero changes captured; the PDF is now out of date and needs re-rendering
```

# Turning Our Bond Research Data Into a Revenue Product

**Internal business case — for discussion**
Date: 26 July 2026

---

## The short version

Over the past few years, while building our bond research work, we have quietly accumulated a database that Indian financial institutions are now **legally required** to have.

In September 2023, the RBI changed the rules on how banks value the bonds they hold. From 1 April 2024, every commercial bank must re-price its bond portfolio at least once a quarter, and the rules name the exact price sources they must use. In November 2025, the RBI extended the same obligation to co-operative banks — roughly 1,700 more institutions, most of them small, none of them able to afford the existing vendors.

The price sources named in those RBI rules are the same sources our database is built on.

We are not proposing to invent a market. The market was created by regulation, the buyers are legally obliged to buy, and the current suppliers are three large firms charging enterprise prices. **There is a clear gap at the bottom and middle of that market, and we are already sitting on the inventory.**

There is one serious risk, covered at the end. I want a decision on whether we spend the next 6–8 weeks testing it.

---

## What we actually have

In plain terms, four assets:

**1. Official daily prices for every Indian government bond.**
Every trading day, an official body called FBIL publishes the closing price for all central and state government bonds. This is not an estimate — it is the number banks are required to use in their books. We have captured this every day since February 2023: about 4 million daily price records covering 7,100 individual bonds, including bonds issued by all 30 states.

**2. Twenty-four years of actual trading history.**
Separately, we have every day's real trading activity in government bonds — actual prices paid, volumes, number of trades — going back to **February 2002**. This is the hard-to-replicate asset. Nobody rebuilds a 24-year archive; you either started collecting it or you didn't. We did.

**3. Proof that our data is correct.**
This is the part I'd draw your attention to specifically. We compared our two independent sources against each other — 129,000 overlapping observations — and the official published price and the actual traded price agree to within about 1.7 paise on a ₹100 bond, at the median.

That single number changes what we're selling. We are not asking a bank to trust us. We can hand them measured evidence of accuracy before they sign anything. In your language: we have the audit evidence, not just the assertion.

**4. Credit ratings for corporate bonds — a snapshot today, a history from here on.**
For about 9,000 corporate bonds we hold the current credit rating with the agency that assigned it and the date it was assigned. **Correction (7 Sep 2026): an earlier draft of this paper said we hold the full history of rating changes. We do not.** The database is built to record every change — each rating carries a validity window and a new value closes the old one — but the ratings were first captured on 18 July 2026 and not one change has been captured since, so every ISIN still has exactly one version on file. What we have is a dated snapshot; the migration history everyone wants accrues from the capture date forward, and is worth selling as such in roughly a year. Nothing about this is unfixable, but it must not be pitched as history until it is.

**What we do not have, and I want to be straight about it:** our corporate bond coverage is broad but shallow outside the ratings history. Our corporate trading data is only eight days old — worthless for now. One half-yearly feed is ten months stale. Two smaller feeds are calendar listings, not data products. I would not put any of that in front of a customer.

---

## Why anyone pays for this

Four separate regulators have created the demand. None of it depends on us persuading anyone that bond data is interesting.

| Who | What the rule says | How many |
|---|---|---|
| Commercial banks | Must re-price bond holdings quarterly using named official sources (RBI, effective Apr 2024) | ~40 relevant |
| Co-operative banks | Same obligation, newly imposed (RBI, Nov 2025) | ~1,700 |
| Alternative investment funds | Quarterly independent valuation required by SEBI | 1,700+ registered |
| Portfolio managers | Client reporting at market value | ~490 |
| Insurance companies | Actively lobbying IRDAI to move to bond-by-bond valuation, which would need exactly our data | ~50, holding ₹74 lakh crore |

The co-operative bank segment is the interesting one. Seventeen hundred institutions were handed a new quarterly compliance obligation eight months ago. They cannot afford a CRISIL or Bloomberg contract. Nobody is serving them. They are the definition of an underserved regulated buyer.

---

## Who we would be competing with, and why we can win

Three firms own this market today: **CRISIL**, **ICRA Analytics** and **CARE**. They are large, credible and expensive. CRISIL's bond valuation tool is already used by most large fund houses, insurers and pension funds.

We should not try to beat them at their own game. Two reasons: they have decades of trust, and for mutual funds specifically the market is legally closed — the valuation providers are appointed by the industry body, and we cannot become one by building a better database.

We win on four things they are weak at:

1. **Depth.** Our 24-year trading archive is genuinely rare. Nobody sells it.
2. **State government bonds.** Everyone focuses on central government bonds. We cover 6,963 state bonds across all 30 states. This is the neglected half of the market.
3. **Independent verification.** Rather than competing to be the valuation provider, we sell the *cross-check* — "here is independent evidence that the valuation you're using is right." That is a different product, and it doesn't put us head-to-head with anyone.
4. **Price.** We can be viable at a tenth to a twentieth of what the incumbents charge, because we're not carrying their cost base.

---

## Where the money is, realistically

Four channels, in order of revenue per unit of effort:

**Software vendors (best economics).** Rather than sell to 1,700 co-operative banks one at a time, sell to the handful of software companies whose systems those banks already run. They need a compliant valuation feed to make their products meet the new RBI rules. One contract reaches hundreds of banks and we carry no support burden. Estimated ₹25 lakh–₹1 crore per vendor per year.

**Global data companies (wholesale).** Firms like Cbonds buy local data feeds as a standing business model — they source pricing from over 400 local providers worldwide. They want Indian depth they don't have. One contract, no end-customer support at all. Estimated ₹20 lakh–₹1 crore.

**Bond investment startups (fastest to close).** There are 29 SEBI-registered online bond platforms, having facilitated over ₹10,000 crore. They are venture-funded, decide in weeks rather than quarters, and new ones are still registering. They need our credit rating history and our trading data to show retail investors whether a bond is liquid. Estimated ₹5–15 lakh each, maybe 8–10 winnable. Small money, but they become our reference customers — which is what makes the bank conversations credible.

**Co-operative banks direct (volume).** ₹50,000–₹3 lakh each, 1,700 of them. Real money in aggregate, but a brutal sales problem one-by-one. Better reached through the software vendors above.

**A conservative first-year target:** one software vendor deal, one wholesale deal, two bond platforms, and fifteen direct bank subscriptions — roughly **₹80 lakh of recurring revenue**, against work we have already substantially done.

---

## The one thing that could kill this

I want this to be the part you read most carefully, because it is a governance question, not a technical one.

All of this data originates from six sources: FBIL, CCIL, RBI, NSE, SEBI and CDSL. We have collected it from publicly accessible pages. **Publicly visible is not the same as licensed for resale.**

Two of those sources are not government bodies at all. NSE operates an entire subsidiary — NSE Data & Analytics — whose business is licensing exactly this kind of data, and their published policy prohibits redistribution without a signed agreement. CCIL is similar. In other words, on our most valuable dataset we would be competing directly with a business the data owner already runs. That is precisely the situation where a rights holder notices and acts.

For the government sources, Indian law does not put government publications in the public domain the way US law does — Crown-style copyright applies for 60 years.

To be clear about what this is and isn't: **this is not a data privacy problem.** There is no personal data anywhere in this database, so the DPDP Act is not our exposure. Our exposure is contractual and intellectual property — breach of the source's terms of use, and copyright in their compiled data.

**My recommendation: we treat licensing as the first gate, not a later formality.** Specifically:

- Get indicative redistribution pricing from CCIL, NSE and FBIL **before** we choose a go-to-market. Their pricing structure determines which channel is viable. If they charge per end subscriber, then 1,700 small bank subscriptions may not survive the licence fee while a handful of large vendor contracts will. That is a strategy-defining answer and we don't have it yet.
- Have a technology and IP lawyer review the six sources' terms against our intended use. A few hours of proper legal time.
- Do not build a single sales deck until both are done.

If the licence economics work, we have a product with regulatory tailwind and a defensible data moat. If they don't, we find out for the cost of some phone calls and a legal opinion rather than after we've signed customers we can't legally serve.

---

## What I'm asking for

1. **Approval to open licensing conversations** with CCIL, NSE Data & Analytics and FBIL — as a company, not informally.
2. **A legal budget** for an IP and technology lawyer to review the six sources' terms.
3. **Six to eight weeks** to run this in parallel with a small validation exercise: two or three conversations with online bond platforms to confirm they would pay for the rating history and liquidity data.
4. **A decision point at the end of that**, at which we either commit properly or shelve it with a clear reason.

I am not asking for engineering resources yet. The data already exists and is running nightly. What I'm asking for is permission to find out whether we are allowed to sell it, and confirmation that someone wants to buy it — in that order.
