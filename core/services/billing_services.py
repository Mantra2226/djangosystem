"""
BILLING & CREDIT NOTE SERVICES MODULE (core/services/billing_services.py)
Handles customer bulk payment FIFO allocations, credit note applications, and ledger syncing.
"""

from decimal import Decimal
from django.db import transaction
from django.utils import timezone


def preview_customer_bulk_allocation(customer, total_received):
    """
    Stage 1 Dry-Run Simulation:
    Calculates exact FIFO distribution across open customer invoices (POSTED, PARTIALLY_PAID)
    without performing any database mutations.
    """
    total_received = Decimal(str(total_received))
    remaining_funds = total_received

    open_invoices = customer.sales_invoices.filter(
        status__in=['POSTED', 'PARTIALLY_PAID']
    ).order_by('invoice_date', 'invoice_id')

    allocations = []
    total_allocated = Decimal('0.00')

    for invoice in open_invoices:
        already_paid = invoice.total_paid
        balance_before = invoice.total_amount - already_paid
        if balance_before <= Decimal('0.00'):
            continue

        allocated = min(remaining_funds, balance_before) if remaining_funds > Decimal('0.00') else Decimal('0.00')
        if allocated > Decimal('0.00'):
            balance_after = max(Decimal('0.00'), balance_before - allocated)
            projected_status = 'PAID' if balance_after == Decimal('0.00') else 'PARTIALLY_PAID'
            allocations.append({
                'invoice_id': invoice.invoice_id,
                'invoice_number': invoice.invoice_number,
                'invoice_date': invoice.invoice_date,
                'total_amount': invoice.total_amount,
                'already_paid': already_paid,
                'balance_before': balance_before,
                'allocated_amount': allocated,
                'balance_after': balance_after,
                'projected_status': projected_status,
            })
            total_allocated += allocated
            remaining_funds -= allocated

        if remaining_funds <= Decimal('0.00'):
            break

    unallocated_amount = max(Decimal('0.00'), total_received - total_allocated)
    return {
        'customer_id': customer.customer_id,
        'customer_name': customer.customer_name,
        'total_received': total_received,
        'total_allocated': total_allocated,
        'unallocated_amount': unallocated_amount,
        'allocations': allocations,
    }


def execute_customer_bulk_allocation(customer, total_received, payment_method='BANK_TRANSFER', reference='', payment_date=None):
    """
    Stage 2 Atomic Execution:
    Acquires row-level locks on candidate customer invoices, creates SalesInvoicePayments,
    transitions invoice statuses, auto-posts General Ledger revenue entries, and returns settlement breakdown.
    """
    from core.models import SalesInvoicePayments

    total_received = Decimal(str(total_received))
    remaining_funds = total_received

    method_map = {
        'BANK_TRANSFER': 'TRANSFER',
        'CREDIT_CARD': 'CARD',
        'CARD': 'CARD',
        'TRANSFER': 'TRANSFER',
        'CASH': 'CASH',
        'CHEQUE': 'TRANSFER',
    }
    model_method = method_map.get(str(payment_method).upper(), payment_method)

    allocations = []
    total_allocated = Decimal('0.00')

    with transaction.atomic():
        open_invoices = customer.sales_invoices.select_for_update().filter(
            status__in=['POSTED', 'PARTIALLY_PAID']
        ).order_by('invoice_date', 'invoice_id')

        for invoice in open_invoices:
            already_paid = invoice.total_paid
            balance_before = invoice.total_amount - already_paid
            if balance_before <= Decimal('0.00'):
                continue

            allocated = min(remaining_funds, balance_before)
            if allocated > Decimal('0.00'):
                ref = reference if reference else f"BULK-{invoice.invoice_number}"
                payment = SalesInvoicePayments(
                    invoice=invoice,
                    amount=allocated,
                    payment_method=model_method,
                    reference_number=ref
                )
                if payment_date:
                    payment.paid_at = payment_date
                payment.save()

                invoice.refresh_from_db()
                invoice.update_payment_status(save=True)

                balance_after = max(Decimal('0.00'), balance_before - allocated)
                allocations.append({
                    'invoice_id': invoice.invoice_id,
                    'invoice_number': invoice.invoice_number,
                    'invoice_date': invoice.invoice_date,
                    'total_amount': invoice.total_amount,
                    'already_paid': already_paid,
                    'balance_before': balance_before,
                    'allocated_amount': allocated,
                    'balance_after': balance_after,
                    'final_status': invoice.status,
                })
                total_allocated += allocated
                remaining_funds -= allocated

            if remaining_funds <= Decimal('0.00'):
                break

    unallocated_amount = max(Decimal('0.00'), total_received - total_allocated)

    # Event 1: Auto-generate CreditNote if there is an unallocated surplus credit balance
    if unallocated_amount > Decimal('0.00'):
        from core.models import CreditNote
        latest_inv = open_invoices.last() if open_invoices.exists() else None
        surplus_cn = CreditNote.objects.create(
            customer=customer,
            invoice=latest_inv,
            issue_date=payment_date or timezone.now().date(),
            status='POSTED',
            subtotal=unallocated_amount,
            total_amount=unallocated_amount,
            reason=f"Surplus credit balance from bulk deposit ({payment_method} ref: {reference or 'N/A'})"
        )
        surplus_cn._sync_finance_entry()

    return {
        'customer_id': customer.customer_id,
        'customer_name': customer.customer_name,
        'total_received': total_received,
        'total_allocated': total_allocated,
        'unallocated_amount': unallocated_amount,
        'allocations': allocations,
    }


def apply_customer_credit_notes_to_invoice(invoice):
    """
    Auto-applies available open CreditNotes for the customer to deduct the grand total / balance
    of newly issued or active SalesInvoices in chronological FIFO order.
    Creates audit-trail SalesInvoicePayments and updates CreditNote applied balances.
    """
    from core.models import CreditNote, SalesInvoicePayments

    if not invoice or not invoice.customer or invoice.status not in ['POSTED', 'PARTIALLY_PAID']:
        return []

    remaining_invoice_balance = invoice.remaining_balance
    if remaining_invoice_balance <= Decimal('0.00'):
        return []

    applied_records = []
    with transaction.atomic():
        open_credit_notes = CreditNote.objects.select_for_update().filter(
            customer=invoice.customer,
            status='POSTED'
        ).order_by('issue_date', 'credit_note_id')

        for cn in open_credit_notes:
            avail_credit = cn.remaining_credit
            if avail_credit <= Decimal('0.00'):
                continue

            deduction = min(remaining_invoice_balance, avail_credit)
            if deduction > Decimal('0.00'):
                ref = f"CREDIT-APPLIED-{cn.credit_note_number}"
                SalesInvoicePayments.objects.create(
                    invoice=invoice,
                    amount=deduction,
                    payment_method='TRANSFER',
                    reference_number=ref
                )
                cn.applied_amount = (cn.applied_amount or Decimal('0.00')) + deduction
                cn.save(update_fields=['applied_amount'])

                applied_records.append({
                    'credit_note_number': cn.credit_note_number,
                    'deduction': deduction,
                    'cn_remaining': cn.remaining_credit
                })

                remaining_invoice_balance -= deduction
                if remaining_invoice_balance <= Decimal('0.00'):
                    break

        if applied_records:
            invoice.refresh_from_db()
            invoice.update_payment_status(save=True)

    return applied_records


def apply_credit_note_to_open_invoices(credit_note):
    """
    When a new CreditNote is issued/approved (e.g. from RMA Return or Surplus Deposit),
    immediately attempts to apply its credit to settle any open unpaid customer invoices in FIFO order.
    """
    from core.models import SalesInvoicePayments

    if not credit_note or not credit_note.customer or credit_note.status != 'POSTED':
        return []

    avail_credit = credit_note.remaining_credit
    if avail_credit <= Decimal('0.00'):
        return []

    applied_records = []
    with transaction.atomic():
        open_invoices = credit_note.customer.sales_invoices.select_for_update().filter(
            status__in=['POSTED', 'PARTIALLY_PAID']
        ).order_by('invoice_date', 'invoice_id')

        for invoice in open_invoices:
            invoice_bal = invoice.remaining_balance
            if invoice_bal <= Decimal('0.00'):
                continue

            deduction = min(avail_credit, invoice_bal)
            if deduction > Decimal('0.00'):
                ref = f"CREDIT-APPLIED-{credit_note.credit_note_number}"
                SalesInvoicePayments.objects.create(
                    invoice=invoice,
                    amount=deduction,
                    payment_method='TRANSFER',
                    reference_number=ref
                )
                invoice.refresh_from_db()
                invoice.update_payment_status(save=True)

                credit_note.applied_amount = (credit_note.applied_amount or Decimal('0.00')) + deduction
                avail_credit -= deduction

                applied_records.append({
                    'invoice_number': invoice.invoice_number,
                    'deduction': deduction,
                })

                if avail_credit <= Decimal('0.00'):
                    break

        credit_note.save(update_fields=['applied_amount'])

    return applied_records
