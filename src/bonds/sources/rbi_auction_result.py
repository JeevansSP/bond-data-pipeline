"""RBI "Full Auction Result" press-release parser — primary-market cut-offs and amounts.

The auction *calendar* (:mod:`bonds.sources.rbi`) says an auction will happen. This says what it
cleared at: notified versus accepted amount, the cut-off price and yield, and (for T-Bills) the
weighted average. That cut-off is the primary-market pricing reference the secondary curve is
judged against, and it is the one thing ``rbi_auctions`` never captured.

Three release layouts, one shape. Each is a **transposed** table: the header row names the
securities and every later row is one metric across them.

    Auction Results | New GS 2031 | 7.71% GS 2066     <- header: one column per security
    I.  | Notified Amount        | 21,000  | 11,000
    III.| Cut-off price(₹)/Yield | 100.00  | 100.58
        | (YTM: 6.5300%)         | (YTM: 7.6618%)     <- continuation of the row above

They differ in ways the parser has to absorb rather than assume away:

* **G-Sec** and **T-Bill** releases number their rows (``I.``…``VI.``) in a separate leading
  cell, publish a cut-off *price* and carry the yield in a ``(YTM: …)`` continuation row.
  T-Bill releases add a weighted-average row with a ``(WAY: …)`` continuation.
* **SDL** releases have no numbering, publish ``Cut-off Yield (%)`` and ``Cut off Price (₹)`` as
  separate rows, and split the partial allotment into ``(i) Percentage`` / ``(ii) No.``.
* Bid counts and amounts arrive as ``(i)``/``(ii)`` sub-rows under a section heading
  ("Competitive Bids Received"), so the label alone is ambiguous — the parser tracks which
  section it is in.

Alignment is **positional**, never by absolute cell index. The layouts disagree on how many
cells precede the data: the G-Sec header is ``[Auction Results, New GS 2031, 7.71% GS 2066]``
(3 cells) while its notified-amount row is ``[I., Notified Amount, 21,000, 11,000]`` (4). Keying
on absolute indices shifts every value one security along and drops the last one.

PDFs of the same releases are bot-blocked (F5 challenge on rbidocs.rbi.org.in); the full text is
in the HTML, which is what this reads.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum, auto
from typing import Final, cast

from lxml.html import HtmlElement, fromstring

from bonds.logging import get_logger
from bonds.models import RbiAuctionResultRecord

logger = get_logger(__name__)

_TABLE_HINT: Final = "notified amount"
_ROMAN_PREFIX_RE: Final = re.compile(r"^[IVX]+\s*\.?\s*$")
_NUMBER_RE: Final = re.compile(r"-?[\d,]+\.?\d*")
_YIELD_PAREN_RE: Final = re.compile(r"\((?:YTM|WAY)\s*:\s*([\d.]+)\s*%?\)", re.IGNORECASE)
_CAPTION_PREFIXES: Final = ("auction result", "notified", "amount in")


class _Section(StrEnum):
    """Which ``(i)``/``(ii)`` sub-rows the parser is currently inside."""

    NONE = auto()
    RECEIVED = auto()
    ACCEPTED = auto()
    PARTIAL = auto()


# Label prefix -> (record field, value kind). Matched by prefix so trailing units and footnote
# markers in the published text do not have to be enumerated.
_DIRECT_LABELS: Final[tuple[tuple[str, str, str], ...]] = (
    ("notified amount", "notified_amount_cr", "float"),
    ("tenor", "tenor_note", "text"),
    ("cut-off price", "cut_off_price", "float"),
    ("cut off price", "cut_off_price", "float"),
    ("cut-off yield", "cut_off_yield", "float"),
    ("cut off yield", "cut_off_yield", "float"),
    ("weighted average price", "wavg_price", "float"),
    ("weighted average yield", "wavg_yield", "float"),
    ("partial allotment percentage", "partial_allotment_pct", "float"),
)

_SUB_ROW_FIELDS: Final[dict[tuple[_Section, str], tuple[str, str]]] = {
    (_Section.RECEIVED, "number"): ("bids_received_count", "int"),
    (_Section.RECEIVED, "amount"): ("bids_received_amount_cr", "float"),
    (_Section.ACCEPTED, "number"): ("bids_accepted_count", "int"),
    (_Section.ACCEPTED, "amount"): ("bids_accepted_amount_cr", "float"),
    (_Section.PARTIAL, "percentage"): ("partial_allotment_pct", "float"),
}


def parse_auction_results(
    content: bytes,
    *,
    prid: str,
    source: str,
    auction_date: dt.date | None = None,
    auction_type: str | None = None,
) -> list[RbiAuctionResultRecord]:
    """Parse one "Full Auction Result" release into a record per security.

    Returns an empty list when the page carries no results table — an announcement rather than a
    result, or a layout this parser does not recognise. The caller decides whether that is a skip
    or a failure; a partial record passed off as complete would be worse than nothing.
    """
    doc = cast("HtmlElement", fromstring(content))
    records: list[RbiAuctionResultRecord] = []
    for table in _result_tables(doc):
        rows = [
            [_clean(cell) for cell in cast("list[HtmlElement]", tr.xpath("./td|./th"))]
            for tr in cast("list[HtmlElement]", table.xpath(".//tr"))
        ]
        rows = [row for row in rows if any(row)]
        securities = _security_columns(rows)
        if securities:
            records.extend(_walk(rows, securities, prid, source, auction_date, auction_type))
    if records:
        logger.info("rbi_result.parsed", prid=prid, securities=len(records))
    else:
        logger.info("rbi_result.no_table", prid=prid)
    return records


def _result_tables(doc: HtmlElement) -> list[HtmlElement]:
    """Every *innermost* results table on the page.

    An SDL release does not have one results table — it has one per group of four securities
    (a full auction day runs to a dozen states), and they all sit nested inside wrapper tables
    that also match on "notified amount". Taking the first match therefore read the outer
    wrapper, whose 200-row span mixed every state's rows together and produced totals rather
    than per-security figures.

    So: keep a matching table only when it contains no matching table of its own, and
    concatenate. G-Sec and T-Bill releases have exactly one such table, so the same rule serves
    all three layouts.
    """
    matching = [
        table
        for table in cast("list[HtmlElement]", doc.xpath("//table"))
        if _TABLE_HINT in " ".join(table.text_content().split()).lower()
    ]
    return [
        table
        for table in matching
        if not any(
            other is not table and _TABLE_HINT in " ".join(other.text_content().split()).lower()
            for other in cast("list[HtmlElement]", table.xpath(".//table"))
        )
    ]


def _clean(element: HtmlElement) -> str:
    return " ".join(element.text_content().split()).replace("\xa0", " ")


def _label(cells: list[str]) -> tuple[str, int]:
    """Return ``(normalised label, index of the first data cell)``.

    A leading roman numeral occupies its own cell in the G-Sec/T-Bill layouts and is absent in
    the SDL one, so the label is "the first non-numeral, non-empty cell" rather than a fixed
    column, and the data begins after it.
    """
    for i, cell in enumerate(cells):
        if not cell or _ROMAN_PREFIX_RE.match(cell):
            continue
        return cell.lower().strip(" .:"), i + 1
    return "", len(cells)


def _security_columns(rows: list[list[str]]) -> list[str]:
    """The security labels, in published column order.

    Found relative to the notified-amount row — the one label every layout carries — because the
    securities are named differently in every release ("New GS 2031", "91-Day",
    "ASSAM SGS 2046") and there is no keyword to match on.
    """
    for index, cells in enumerate(rows):
        if not _label(cells)[0].startswith("notified amount"):
            continue
        for candidate in range(index - 1, -1, -1):
            names = [
                cell
                for cell in rows[candidate]
                if cell
                and not cell.startswith("(")
                and not cell.lower().startswith(_CAPTION_PREFIXES)
            ]
            # One name is enough: an auction of a single security has a header of
            # ["Auction Results", "6.94% GS 2036"], and the caption filter above has already
            # removed the "Auction Results" half. Requiring two names silently dropped every
            # single-security G-Sec auction.
            if names:
                return names
        return []
    return []


def _data_values(cells: list[str], count: int) -> list[str]:
    """The ``count`` data cells of a metric row, positionally after its label."""
    values = cells[_label(cells)[1] :]
    return (values + [""] * count)[:count]


def _as_float(text: str) -> float | None:
    match = _NUMBER_RE.search(text.replace(" ", ""))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _as_int(text: str) -> int | None:
    value = _as_float(text)
    return int(value) if value is not None else None


def _section_for(label: str) -> _Section | None:
    """Whether ``label`` opens a section whose ``(i)``/``(ii)`` sub-rows follow.

    Non-competitive sections reset to ``NONE``: their sub-rows carry the same ``(i) No.`` labels
    as the competitive ones, and attributing retail non-competitive bids to the competitive
    fields would overstate institutional demand.
    """
    if "bids received" in label:
        return _Section.NONE if "non" in label else _Section.RECEIVED
    if "bids accepted" in label:
        return _Section.NONE if "non" in label else _Section.ACCEPTED
    if label.startswith("partial allotment") and "percentage of" in label:
        return _Section.PARTIAL
    return None


def _field_for(label: str, section: _Section) -> tuple[str, str] | None:
    """Map a metric row's label (in its section) to the record field it fills."""
    for prefix, field, kind in _DIRECT_LABELS:
        if label.startswith(prefix):
            return field, kind
    if section is not _Section.NONE and label.startswith(("(i)", "(ii)")):
        body = label.split(")", 1)[1].strip()
        for keyword in ("number", "no", "amount", "percentage"):
            if body.startswith(keyword):
                canonical = "number" if keyword in {"number", "no"} else keyword
                return _SUB_ROW_FIELDS.get((section, canonical))
    return None


def _walk(
    rows: list[list[str]],
    securities: list[str],
    prid: str,
    source: str,
    auction_date: dt.date | None,
    auction_type: str | None,
) -> list[RbiAuctionResultRecord]:
    """Walk the metric rows, accumulating one field dict per security column."""
    count = len(securities)
    fields: list[dict[str, object]] = [{} for _ in securities]
    section = _Section.NONE
    # Which field the last cut-off / weighted-average row filled, so a "(YTM: …)" continuation
    # row knows which yield it carries.
    pending_yield_field: str | None = None

    for cells in rows:
        populated = [cell for cell in cells if cell]
        if populated and all(_YIELD_PAREN_RE.search(cell) for cell in populated):
            # A "(YTM: 6.53%)" / "(WAY: 5.25%)" row continues the row above, and every cell in it
            # is data — there is no label to skip past. It is right-aligned under the security
            # columns, so take the trailing `count` cells: filtering the blanks out first would
            # collapse the columns and hand one security's yield to another whenever a release
            # leaves a cell empty (a fresh issue quoted on yield alone, a devolved line).
            if pending_yield_field:
                for i, cell in enumerate(cells[-count:] if count <= len(cells) else cells):
                    if match := _YIELD_PAREN_RE.search(cell):
                        fields[i][pending_yield_field] = float(match.group(1))
            pending_yield_field = None
            continue

        label = _label(cells)[0]
        if not label:
            continue

        # Direct value first, section heading second. "Partial Allotment Percentage of
        # Competitive Bids" is *both*: G-Sec and T-Bill releases put the percentage inline on
        # that row, while SDL releases leave it blank and follow with an "(i) Percentage"
        # sub-row. Checking the heading first consumed the row and lost every G-Sec percentage;
        # checking the value first and falling through when the row is empty serves both.
        target = _field_for(label, section)
        wrote = False
        if target is not None:
            field, kind = target
            for i, cell in enumerate(_data_values(cells, count)):
                if not cell:
                    continue
                value: object | None
                if kind == "text":
                    value = cell
                elif kind == "int":
                    value = _as_int(cell)
                else:
                    value = _as_float(cell)
                if value is not None:
                    fields[i][field] = value
                    wrote = True

        if not wrote and (opened := _section_for(label)) is not None:
            section = opened
            pending_yield_field = None
            continue
        if target is None:
            continue

        # An SDL release publishes the yield on its own row, so it is already set; a later
        # parenthetical must not overwrite a directly-published figure.
        if field == "cut_off_price" and "cut_off_yield" not in fields[0]:
            pending_yield_field = "cut_off_yield"
        elif field == "wavg_price" and "wavg_yield" not in fields[0]:
            pending_yield_field = "wavg_yield"
        else:
            pending_yield_field = None

    return [
        RbiAuctionResultRecord(
            prid=prid,
            security=security,
            source=source,
            auction_date=auction_date,
            auction_type=auction_type,
            **fields[i],  # type: ignore[arg-type]
        )
        for i, security in enumerate(securities)
        if fields[i]
    ]
