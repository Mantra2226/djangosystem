"""
PDF GENERATION SERVICE (core/utils/pdf_generator.py)

Generates professional, downloadable PDF documents for commercial sales invoices
and credit notes using ReportLab.
"""

import io
from decimal import Decimal
from reportlab.lib.pagesizes import letter  # pyright: ignore[reportMissingImports]
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT


def generate_invoice_pdf(sales_invoice) -> io.BytesIO:
    """
    Generates a commercial invoice PDF document in memory.

    Sections:
    - Document Header: Company Title ("Glass Putty Manufacturing ERP"), Document Title,
      Invoice Number, Date, Due Date, Status, Customer Details.
    - Invoicing Policy & Sales Order reference.
    - Line Items Table: Product Name, Billed Quantity, Unit Price, Tax Rate (%), Tax Amount, Total Price.
    - Financial Summary: Subtotal, Total Tax, Grand Total (total_amount), Payment Status, Total Paid, Remaining Balance.

    Returns:
        io.BytesIO: In-memory buffer containing PDF binary stream.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'CompanyTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#1B365D'),
        alignment=TA_LEFT,
        spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Heading2'],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor('#4A5568'),
        alignment=TA_LEFT,
        spaceAfter=12
    )
    meta_style = ParagraphStyle(
        'MetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#2D3748')
    )
    bold_meta_style = ParagraphStyle(
        'BoldMetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=13,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor('#1A202C')
    )
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontSize=9,
        leading=11,
        fontName='Helvetica-Bold',
        textColor=colors.white,
        alignment=TA_CENTER
    )
    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2D3748'),
        alignment=TA_LEFT
    )
    table_cell_right_style = ParagraphStyle(
        'TableCellRight',
        parent=styles['Normal'],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2D3748'),
        alignment=TA_RIGHT
    )

    story = []

    # 1. Header & Title
    story.append(Paragraph("Glass Putty Manufacturing ERP", title_style))
    story.append(Paragraph("COMMERCIAL SALES INVOICE", subtitle_style))
    story.append(Spacer(1, 6))

    # Customer & Metadata Section in 2 columns
    customer = getattr(sales_invoice, 'customer', None)
    customer_name = customer.customer_name if customer else "N/A"
    contact_info = customer.contact_info if customer and customer.contact_info else "N/A"
    shipping_addr = customer.shipping_address if customer and customer.shipping_address else "N/A"

    so = getattr(sales_invoice, 'sales_order', None)
    so_ref = so.order_number if so else "N/A"
    invoicing_policy = so.get_invoicing_policy_display() if so else "N/A"
    inv_date = str(sales_invoice.invoice_date) if sales_invoice.invoice_date else "N/A"
    due_date = str(sales_invoice.due_date) if sales_invoice.due_date else "N/A"
    status_disp = sales_invoice.get_status_display() if hasattr(sales_invoice, 'get_status_display') else str(sales_invoice.status)

    left_info = [
        Paragraph("<b>Billed To:</b>", bold_meta_style),
        Paragraph(f"<b>Customer:</b> {customer_name}", meta_style),
        Paragraph(f"<b>Contact:</b> {contact_info}", meta_style),
        Paragraph(f"<b>Address:</b> {shipping_addr}", meta_style),
    ]

    right_info = [
        Paragraph(f"<b>Invoice #:</b> {sales_invoice.invoice_number}", bold_meta_style),
        Paragraph(f"<b>Invoice Date:</b> {inv_date}", meta_style),
        Paragraph(f"<b>Due Date:</b> {due_date}", meta_style),
        Paragraph(f"<b>Sales Order Ref:</b> {so_ref}", meta_style),
        Paragraph(f"<b>Invoicing Policy:</b> {invoicing_policy}", meta_style),
        Paragraph(f"<b>Status:</b> {status_disp}", bold_meta_style),
    ]

    info_data = [[left_info, right_info]]
    info_table = Table(info_data, colWidths=[270, 270])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    # 2. Line Items Table
    headers = [
        Paragraph("Product Name", table_header_style),
        Paragraph("Qty", table_header_style),
        Paragraph("Unit Price", table_header_style),
        Paragraph("Tax Rate", table_header_style),
        Paragraph("Tax Amount", table_header_style),
        Paragraph("Total Price", table_header_style)
    ]
    table_data = [headers]

    lines = list(sales_invoice.lines.all().select_related('product'))
    if lines:
        for line in lines:
            prod_name = line.product.name if line.product else "N/A"
            qty = f"{Decimal(str(line.quantity or 0)):.2f}"
            unit_price = f"${Decimal(str(line.unit_price or 0)):.2f}"
            tax_rate = f"{Decimal(str(line.tax_rate or 0)):.2f}%"
            tax_amt = f"${Decimal(str(line.tax_amount or 0)):.2f}"
            tot_price = f"${Decimal(str(line.total_price or 0)):.2f}"

            table_data.append([
                Paragraph(prod_name, table_cell_style),
                Paragraph(qty, table_cell_right_style),
                Paragraph(unit_price, table_cell_right_style),
                Paragraph(tax_rate, table_cell_right_style),
                Paragraph(tax_amt, table_cell_right_style),
                Paragraph(tot_price, table_cell_right_style),
            ])
    else:
        # Fallback if no line items exist yet
        table_data.append([
            Paragraph("No itemized lines recorded.", table_cell_style),
            Paragraph("0.00", table_cell_right_style),
            Paragraph("$0.00", table_cell_right_style),
            Paragraph("0.00%", table_cell_right_style),
            Paragraph("$0.00", table_cell_right_style),
            Paragraph(f"${Decimal(str(sales_invoice.total_amount or 0)):.2f}", table_cell_right_style),
        ])

    items_table = Table(table_data, colWidths=[180, 60, 75, 65, 75, 85])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B365D')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 14))

    # 3. Financial Summary Table
    subtotal_val = f"${Decimal(str(sales_invoice.subtotal or 0)):.2f}"
    tax_val = f"${Decimal(str(sales_invoice.tax_amount or 0)):.2f}"
    total_val = f"${Decimal(str(sales_invoice.total_amount or 0)):.2f}"
    paid_val = f"${Decimal(str(getattr(sales_invoice, 'total_paid', Decimal('0.00')) or 0)):.2f}"
    bal_val = f"${Decimal(str(getattr(sales_invoice, 'remaining_balance', Decimal('0.00')) or 0)):.2f}"

    summary_data = [
        [Paragraph("<b>Subtotal:</b>", table_cell_style), Paragraph(subtotal_val, table_cell_right_style)],
        [Paragraph("<b>Total Tax:</b>", table_cell_style), Paragraph(tax_val, table_cell_right_style)],
        [Paragraph("<b>Grand Total:</b>", bold_meta_style), Paragraph(f"<b>{total_val}</b>", table_cell_right_style)],
        [Paragraph("<b>Total Paid:</b>", table_cell_style), Paragraph(paid_val, table_cell_right_style)],
        [Paragraph("<b>Remaining Balance:</b>", bold_meta_style), Paragraph(f"<b>{bal_val}</b>", table_cell_right_style)],
        [Paragraph("<b>Payment Status:</b>", bold_meta_style), Paragraph(f"<b>{status_disp}</b>", table_cell_right_style)],
    ]

    summary_table = Table(summary_data, colWidths=[120, 90])
    summary_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#EDF2F7')),
        ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#1B365D')),
    ]))

    # Align summary table to right using wrapper
    summary_wrapper = Table([[Paragraph("", meta_style), summary_table]], colWidths=[330, 210])
    summary_wrapper.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(summary_wrapper)

    doc.build(story)
    buffer.seek(0)
    return buffer


def generate_credit_note_pdf(credit_note) -> io.BytesIO:
    """
    Generates a credit note / adjustment memo PDF document in memory.

    Sections:
    - Document Header: Company Title ("Glass Putty Manufacturing ERP"), Document Title ("CREDIT NOTE"),
      Credit Note Number, Issue Date, Customer Details, Original Invoice Reference, Reason.
    - Credited Line Items Table: Product Name, Quantity Credited, Unit Price, Tax Rate, Tax Amount, Subtotal, Total Credited.
    - Financial Summary: Subtotal, Total Tax, Total Credited Amount.

    Returns:
        io.BytesIO: In-memory buffer containing PDF binary stream.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'CompanyTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#7B1113'),
        alignment=TA_LEFT,
        spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Heading2'],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor('#4A5568'),
        alignment=TA_LEFT,
        spaceAfter=12
    )
    meta_style = ParagraphStyle(
        'MetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#2D3748')
    )
    bold_meta_style = ParagraphStyle(
        'BoldMetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=13,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor('#1A202C')
    )
    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontSize=9,
        leading=11,
        fontName='Helvetica-Bold',
        textColor=colors.white,
        alignment=TA_CENTER
    )
    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2D3748'),
        alignment=TA_LEFT
    )
    table_cell_right_style = ParagraphStyle(
        'TableCellRight',
        parent=styles['Normal'],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2D3748'),
        alignment=TA_RIGHT
    )

    story = []

    # 1. Header & Title
    story.append(Paragraph("Glass Putty Manufacturing ERP", title_style))
    story.append(Paragraph("CREDIT NOTE / ADJUSTMENT MEMO", subtitle_style))
    story.append(Spacer(1, 6))

    # Customer & Metadata Section in 2 columns
    customer = getattr(credit_note, 'customer', None)
    if not customer and credit_note.invoice:
        customer = credit_note.invoice.customer
    customer_name = customer.customer_name if customer else "N/A"
    contact_info = customer.contact_info if customer and customer.contact_info else "N/A"

    orig_inv = getattr(credit_note, 'invoice', None)
    inv_ref = orig_inv.invoice_number if orig_inv else "N/A"
    issue_date = str(credit_note.issue_date) if credit_note.issue_date else "N/A"
    reason_text = credit_note.reason if credit_note.reason else "Standard Return / RMA Adjustment"
    status_disp = credit_note.get_status_display() if hasattr(credit_note, 'get_status_display') else str(credit_note.status)

    left_info = [
        Paragraph("<b>Credit Issued To:</b>", bold_meta_style),
        Paragraph(f"<b>Customer:</b> {customer_name}", meta_style),
        Paragraph(f"<b>Contact:</b> {contact_info}", meta_style),
        Paragraph(f"<b>Reason / RMA:</b> {reason_text}", meta_style),
    ]

    right_info = [
        Paragraph(f"<b>Credit Note #:</b> {credit_note.credit_note_number}", bold_meta_style),
        Paragraph(f"<b>Issue Date:</b> {issue_date}", meta_style),
        Paragraph(f"<b>Original Invoice Ref:</b> {inv_ref}", meta_style),
        Paragraph(f"<b>Status:</b> {status_disp}", bold_meta_style),
    ]

    info_data = [[left_info, right_info]]
    info_table = Table(info_data, colWidths=[270, 270])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    # 2. Line Items Table
    headers = [
        Paragraph("Product Name", table_header_style),
        Paragraph("Qty Returned", table_header_style),
        Paragraph("Unit Price", table_header_style),
        Paragraph("Tax Amount", table_header_style),
        Paragraph("Subtotal", table_header_style),
        Paragraph("Total Credited", table_header_style)
    ]
    table_data = [headers]

    lines = list(credit_note.lines.all().select_related('product'))
    if lines:
        for line in lines:
            prod_name = line.product.name if line.product else "N/A"
            qty = f"{Decimal(str(line.quantity or 0)):.2f}"
            unit_price = f"${Decimal(str(line.unit_price or 0)):.2f}"
            tax_amt = f"${Decimal(str(line.tax_amount or 0)):.2f}"
            subtot = f"${Decimal(str(line.subtotal or 0)):.2f}"
            tot_price = f"${Decimal(str(line.total_price or 0)):.2f}"

            table_data.append([
                Paragraph(prod_name, table_cell_style),
                Paragraph(qty, table_cell_right_style),
                Paragraph(unit_price, table_cell_right_style),
                Paragraph(tax_amt, table_cell_right_style),
                Paragraph(subtot, table_cell_right_style),
                Paragraph(tot_price, table_cell_right_style),
            ])
    else:
        table_data.append([
            Paragraph("No itemized lines recorded.", table_cell_style),
            Paragraph("0.00", table_cell_right_style),
            Paragraph("$0.00", table_cell_right_style),
            Paragraph("$0.00", table_cell_right_style),
            Paragraph("$0.00", table_cell_right_style),
            Paragraph(f"${Decimal(str(credit_note.total_amount or 0)):.2f}", table_cell_right_style),
        ])

    items_table = Table(table_data, colWidths=[175, 75, 70, 70, 75, 75])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7B1113')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FFF5F5')]),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 14))

    # 3. Financial Summary Table
    subtotal_val = f"${Decimal(str(credit_note.subtotal or 0)):.2f}"
    tax_val = f"${Decimal(str(credit_note.tax_amount or 0)):.2f}"
    total_val = f"${Decimal(str(credit_note.total_amount or 0)):.2f}"

    summary_data = [
        [Paragraph("<b>Credited Subtotal:</b>", table_cell_style), Paragraph(subtotal_val, table_cell_right_style)],
        [Paragraph("<b>Credited Tax:</b>", table_cell_style), Paragraph(tax_val, table_cell_right_style)],
        [Paragraph("<b>Total Credited Amount:</b>", bold_meta_style), Paragraph(f"<b>{total_val}</b>", table_cell_right_style)],
    ]

    summary_table = Table(summary_data, colWidths=[130, 80])
    summary_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#FED7D7')),
        ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#7B1113')),
    ]))

    summary_wrapper = Table([[Paragraph("", meta_style), summary_table]], colWidths=[330, 210])
    summary_wrapper.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(summary_wrapper)

    doc.build(story)
    buffer.seek(0)
    return buffer

