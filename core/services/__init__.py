"""
CORE SERVICES PACKAGE (core/services/__init__.py)

Re-exports all services for backwards compatibility:
- Production reconciliation & stock consumption engine
- MRP shortage evaluation, explosion, and auto-drafting POs
- Customer bulk payment FIFO allocation & credit note offset engine
"""

from .production_reconciliation import (
    ProductionReconciliationError,
    ProductionReconciliationEngine,
)
from .mrp_services import (
    explode_material_requirements,
    evaluate_mrp_shortages,
    resolve_raw_autodraft_po,
    resolve_raw_direct_procurement,
    resolve_raw_hold_inbound,
    resolve_intermediate_build,
    resolve_intermediate_hold_active,
    resolve_intermediate_partial_batch,
    check_and_auto_resume_on_hold_orders,
)
from .billing_services import (
    preview_customer_bulk_allocation,
    execute_customer_bulk_allocation,
    apply_customer_credit_notes_to_invoice,
    apply_credit_note_to_open_invoices,
)

__all__ = [
    'ProductionReconciliationError',
    'ProductionReconciliationEngine',
    'explode_material_requirements',
    'evaluate_mrp_shortages',
    'resolve_raw_autodraft_po',
    'resolve_raw_direct_procurement',
    'resolve_raw_hold_inbound',
    'resolve_intermediate_build',
    'resolve_intermediate_hold_active',
    'resolve_intermediate_partial_batch',
    'check_and_auto_resume_on_hold_orders',
    'preview_customer_bulk_allocation',
    'execute_customer_bulk_allocation',
    'apply_customer_credit_notes_to_invoice',
    'apply_credit_note_to_open_invoices',
]
