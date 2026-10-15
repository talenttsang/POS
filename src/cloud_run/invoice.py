"""Invoice PDF for one sale, laid out like the BigCommerce print invoice the
other brand uses: company block, billing / shipping details, order facts,
an items table, totals and comments.

The company shown on the invoice is set here (INVOICE_COMPANY and
INVOICE_ADDRESS environment variables override it per deployment).
"""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF
from fpdf.enums import XPos, YPos

COMPANY = os.environ.get("INVOICE_COMPANY", "Frontier Beauty Supplies Limited")
ADDRESS = os.environ.get("INVOICE_ADDRESS", "A1-201 4300 Steeles Ave E, Markham, ON L3R 0Y5")
TZ = ZoneInfo("America/Toronto")
FONTS = Path(__file__).parent / "fonts"

INK = (20, 23, 26)
MUTED = (120, 126, 132)
LINE = (60, 60, 60)
BOX = (190, 194, 198)


def money(value, currency):
    return f"${float(value):,.2f} {currency}"


def long_date(dt):
    """Oct 15th 2026, as the reference invoice prints it."""
    local = dt.astimezone(TZ)
    d = local.day
    suffix = "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return f"{local.strftime('%b')} {d}{suffix} {local.year}"


class Invoice(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="pt", format="Letter")
        self.add_font("Sans", "", str(FONTS / "DejaVuSans.ttf"))
        self.add_font("Sans", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
        self.set_margins(54, 48, 54)
        self.set_auto_page_break(auto=True, margin=54)

    def text(self, size, bold=False, colour=INK):
        self.set_font("Sans", "B" if bold else "", size)
        self.set_text_color(*colour)

    def rule(self, gap_before=8, gap_after=10, width=1.0):
        self.ln(gap_before)
        self.set_draw_color(*LINE)
        self.set_line_width(width)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(gap_after)

    def row(self, cells, heights=None, align=None, bold=False, size=9.5):
        """One table row. cells: list of (width, text). Wraps text in every cell."""
        self.text(size, bold)
        x0, y0 = self.get_x(), self.get_y()
        h = 0
        for i, (w, t) in enumerate(cells):
            lines = self.multi_cell(w, 12, t, dry_run=True, output="LINES", align=(align or ["L"] * len(cells))[i])
            h = max(h, 12 * max(1, len(lines)))
        if y0 + h > self.page_break_trigger:
            self.add_page()
            y0 = self.get_y()
        x = x0
        for i, (w, t) in enumerate(cells):
            self.set_xy(x, y0)
            self.multi_cell(w, 12, t, align=(align or ["L"] * len(cells))[i], new_x=XPos.RIGHT, new_y=YPos.TOP)
            x += w
        self.set_xy(x0, y0 + h)


def build_invoice_pdf(sale, items, venue, bill_to=None):
    """sale: a pos_transactions row; items: its pos_transaction_items rows;
    venue: the pos_events row it was sold under (may be None);
    bill_to: optional customer lines typed by the cashier."""
    cur = sale["currency"]
    number = sale["transaction_number"]
    pdf = Invoice()
    pdf.set_title(f"{COMPANY} - Invoice for order #{number}")
    pdf.set_author(COMPANY)
    pdf.add_page()

    # Company block (the reference shows the brand logo here; this brand uses its name).
    pdf.text(20, bold=True)
    pdf.cell(0, 26, COMPANY, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(8)
    pdf.text(13, bold=True, colour=MUTED)
    pdf.cell(0, 16, f"{COMPANY} invoice for order #{number}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)
    pdf.text(10, bold=True)
    pdf.cell(0, 13, ADDRESS, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)

    # Billing and shipping details, side by side.
    col = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
    top = pdf.get_y()
    bill = [l.strip() for l in (bill_to or "").splitlines() if l.strip()] or ["Walk-in customer"]
    ship = ["Picked up in store"]
    if venue:
        ship.append(venue["name"])
        if venue.get("address"):
            ship.append(venue["address"])
    for i, (title, lines) in enumerate((("Billing Details", bill), ("Shipping Details", ship))):
        x = pdf.l_margin + i * col
        pdf.set_xy(x, top)
        pdf.text(13, bold=True)
        pdf.cell(col, 18, title, new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.ln(6)
        for n, line in enumerate(lines):
            pdf.set_x(x)
            pdf.text(10, bold=(n == 0))
            pdf.multi_cell(col - 12, 13, line, align="L", new_x=XPos.LEFT, new_y=YPos.NEXT)
        if i == 0:
            left_bottom = pdf.get_y()
    pdf.set_y(max(left_bottom, pdf.get_y()) + 14)

    # Order facts in two columns of label / value.
    paid = money(sale["amount_paid"], cur)
    facts = [
        ("Order:", f"#{number}", "Order Date:", long_date(sale["sold_at"])),
        ("Payment Method:", f"Cash ({paid})", "Shipping Method:", "Pickup In Store"),
    ]
    label_w, value_w = 110, col - 110
    for l1, v1, l2, v2 in facts:
        y = pdf.get_y()
        pdf.set_xy(pdf.l_margin, y)
        pdf.text(10, bold=True); pdf.cell(label_w, 14, l1)
        pdf.text(10); pdf.cell(value_w, 14, v1)
        pdf.text(10, bold=True); pdf.cell(label_w, 14, l2)
        pdf.text(10); pdf.cell(value_w, 14, v2, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    pdf.rule(gap_before=6, gap_after=8)

    # Items table.
    pdf.text(14, bold=True)
    pdf.cell(0, 18, "Order Items", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)
    full = pdf.w - pdf.l_margin - pdf.r_margin
    widths = [34, 118, full - 34 - 118 - 95 - 95, 95, 95]
    aligns = ["R", "L", "L", "R", "R"]
    pdf.row(list(zip(widths, ["Qty", "Code/SKU", "Product Name", "Price", "Total"])), align=aligns, bold=True)
    pdf.ln(4)
    for it in items:
        pdf.row(list(zip(widths, [str(it["quantity"]), it["sku"] or "", it["product_name"] or "",
                                  money(it["unit_price"], cur), money(it["line_total"], cur)])), align=aligns)
        pdf.ln(4)

    pdf.rule(gap_before=4, gap_after=10)

    # Totals, right aligned like the reference.
    totals = [("Subtotal:", money(sale["subtotal"], cur), False)]
    if float(sale["discount_total"] or 0) > 0:
        totals.append(("Discount:", "-" + money(sale["discount_total"], cur), False))
    totals.append(("Grand total:", money(sale["grand_total"], cur), True))
    if sale.get("refund_amount"):
        reason = f" ({sale['refund_reason']})" if sale.get("refund_reason") else ""
        totals.append((f"Refunded{reason}:", "-" + money(sale["refund_amount"], cur), False))
    for label, value, bold in totals:
        pdf.text(10, bold=bold)
        pdf.set_x(pdf.w - pdf.r_margin - 300)
        pdf.cell(190, 16, label, align="R")
        pdf.cell(110, 16, value, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)

    # Comments.
    pdf.ln(10)
    pdf.text(14, bold=True)
    pdf.cell(0, 18, "Comments", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)
    comments = [f"Sold by {sale['staff_name']}"]
    if sale.get("note"):
        comments.append(sale["note"])
    if sale.get("status") == "refunded":
        comments.append("This sale has been refunded in full.")
    pdf.text(10)
    for c in comments:
        pdf.set_x(pdf.l_margin + 36)
        pdf.multi_cell(full - 36, 14, c, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # The reference prints inside a thin box; draw one around what we wrote.
    bottom = pdf.get_y() + 24
    pdf.set_draw_color(*BOX)
    pdf.set_line_width(0.8)
    pdf.rect(pdf.l_margin - 18, 34, full + 36, bottom - 34)
    return bytes(pdf.output())
