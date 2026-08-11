"""
EXECUTIVE REPORTING ANALYTICS ENGINE (core/reports.py)

Provides consolidated analytical data calculation engines for:
1. Profit & Loss (P&L) and Gross Margin Analysis
2. Cost of Goods Manufactured (COGM) Breakdown
3. Production Yield Efficiency & Scrap/Waste Analysis
4. Inventory Health, Low-Stock Alerts, and Supplier OTIF Performance
5. Accounts Receivable (A/R) and Accounts Payable (A/P) Aging Reports
"""

from decimal import Decimal
from django.db.models import Sum, F, Count, Q, Avg
from django.utils import timezone
from datetime import timedelta
from .models import (
    SalesInvoice, PurchaseInvoice, DispatchRecord, Inventory, Product,
    WorkOrder, WorkOrderInstruction, WorkOrderMaterialLine, MaterialVarianceRecord,
    ProcurementOrder, FinanceEntry, PurchaseOrder, SalesOrder
)

def get_profit_and_loss_summary(start_date=None, end_date=None):
    """
    Calculates consolidated Profit & Loss (P&L) financial metrics:
    - Total Revenue from Sales Invoices and Income Ledger Entries
    - Cost of Goods Sold (COGS) based on dispatched product valuation
    - Operating Expenses from expense ledger entries
    - Gross Profit, Net Income, and Gross Profit Margin %
    """
    inv_qs = SalesInvoice.objects.all()
    fin_qs = FinanceEntry.objects.all()
    disp_qs = DispatchRecord.objects.filter(status='delivered')

    if start_date:
        inv_qs = inv_qs.filter(invoice_date__gte=start_date)
        fin_qs = fin_qs.filter(entry_date__gte=start_date)
        disp_qs = disp_qs.filter(dispatch_date__gte=start_date)
    if end_date:
        inv_qs = inv_qs.filter(invoice_date__lte=end_date)
        fin_qs = fin_qs.filter(entry_date__lte=end_date)
        disp_qs = disp_qs.filter(dispatch_date__lte=end_date)

    # 1. Total Revenue
    sales_revenue = inv_qs.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    other_revenue = fin_qs.filter(entry_type='INCOME').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_revenue = sales_revenue + other_revenue

    # 2. Cost of Goods Sold (COGS)
    cogs = Decimal('0.00')
    for disp in disp_qs.select_related('product'):
        prod_cost = Decimal('0.00')
        inv = disp.product.stock.first()
        if inv and inv.unit_cost:
            prod_cost = inv.unit_cost
        cogs += (disp.quantity_dispatched or Decimal('0.00')) * prod_cost

    cogs = cogs.quantize(Decimal('0.01'))

    # 3. Operating Expenses
    operating_expenses = fin_qs.filter(entry_type='EXPENSE').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # 4. Profit & Margins
    gross_profit = total_revenue - cogs
    net_income = gross_profit - operating_expenses

    gross_margin_pct = Decimal('0.00')
    if total_revenue > Decimal('0.00'):
        gross_margin_pct = ((gross_profit / total_revenue) * Decimal('100.00')).quantize(Decimal('0.01'))

    return {
        'total_revenue': total_revenue,
        'sales_revenue': sales_revenue,
        'cogs': cogs,
        'gross_profit': gross_profit,
        'operating_expenses': operating_expenses,
        'net_income': net_income,
        'gross_margin_pct': gross_margin_pct,
    }


def get_cogm_report(start_date=None, end_date=None):
    """
    Calculates Cost of Goods Manufactured (COGM):
    - Direct Raw Material Consumption Cost
    - Material Scrap & Waste Variance Impact
    - Estimated Direct Labor Cost ($25.00/hr standard labor rate)
    """
    wo_qs = WorkOrder.objects.filter(status='COMPLETED')
    mvr_qs = MaterialVarianceRecord.objects.all()

    if start_date:
        wo_qs = wo_qs.filter(production_start_date__gte=start_date)
        mvr_qs = mvr_qs.filter(recorded_at__gte=start_date)
    if end_date:
        wo_qs = wo_qs.filter(production_start_date__lte=end_date)
        mvr_qs = mvr_qs.filter(recorded_at__lte=end_date)

    # 1. Direct Material Consumption Cost
    raw_material_cost = Decimal('0.00')
    for wo in wo_qs.prefetch_related('material_lines__component__stock'):
        for line in wo.material_lines.all():
            actual_qty = line.quantity_actual or Decimal('0.00')
            inv = line.component.stock.first()
            unit_cost = inv.unit_cost if inv else Decimal('0.00')
            raw_material_cost += actual_qty * unit_cost

    raw_material_cost = raw_material_cost.quantize(Decimal('0.01'))

    # 2. Scrap & Variance Financial Impact
    unfavourable_scrap = mvr_qs.filter(variance_classification='UNFAVOURABLE').aggregate(total=Sum('financial_impact'))['total'] or Decimal('0.00')
    favourable_savings = mvr_qs.filter(variance_classification='FAVOURABLE').aggregate(total=Sum('financial_impact'))['total'] or Decimal('0.00')

    # 3. Labor Cost (Standard $25/hr)
    total_labor_minutes = WorkOrderInstruction.objects.filter(
        work_order__in=wo_qs, status='COMPLETED'
    ).aggregate(total=Sum('estimated_time_minutes'))['total'] or 0

    labor_cost = (Decimal(str(total_labor_minutes)) / Decimal('60.00') * Decimal('25.00')).quantize(Decimal('0.01'))

    total_cogm = raw_material_cost + unfavourable_scrap - favourable_savings + labor_cost

    return {
        'completed_orders_count': wo_qs.count(),
        'raw_material_cost': raw_material_cost,
        'unfavourable_scrap': unfavourable_scrap,
        'favourable_savings': favourable_savings,
        'labor_cost': labor_cost,
        'total_labor_hours': round(total_labor_minutes / 60.0, 2),
        'total_cogm': total_cogm,
    }


def get_production_yield_and_scrap_report(start_date=None, end_date=None):
    """
    Calculates Production Yield Efficiency Rates & Machine Utilization Metrics.
    """
    wo_qs = WorkOrder.objects.all()
    mvr_qs = MaterialVarianceRecord.objects.all()

    if start_date:
        wo_qs = wo_qs.filter(production_start_date__gte=start_date)
        mvr_qs = mvr_qs.filter(recorded_at__gte=start_date)
    if end_date:
        wo_qs = wo_qs.filter(production_start_date__lte=end_date)
        mvr_qs = mvr_qs.filter(recorded_at__lte=end_date)

    completed_wo = wo_qs.filter(status='COMPLETED')
    total_completed = completed_wo.count()

    total_target_qty = Decimal('0.00')
    total_produced_qty = Decimal('0.00')

    for wo in completed_wo.prefetch_related('production_runs'):
        total_target_qty += wo.target_quantity
        total_produced_qty += wo.quantity_produced or Decimal('0.00')

    yield_rate_pct = Decimal('100.00')
    if total_target_qty > Decimal('0.00'):
        yield_rate_pct = ((total_produced_qty / total_target_qty) * Decimal('100.00')).quantize(Decimal('0.01'))

    # Variance counts
    unfavourable_count = mvr_qs.filter(variance_classification='UNFAVOURABLE').count()
    favourable_count = mvr_qs.filter(variance_classification='FAVOURABLE').count()
    exact_count = mvr_qs.filter(variance_classification='EXACT').count()

    # Workstation / Machine breakdown
    machine_workload = WorkOrderInstruction.objects.values('machine').annotate(
        total_time=Sum('estimated_time_minutes'),
        step_count=Count('instruction_id')
    ).order_by('-total_time')

    return {
        'total_completed_runs': total_completed,
        'total_target_qty': total_target_qty,
        'total_produced_qty': total_produced_qty,
        'yield_rate_pct': yield_rate_pct,
        'unfavourable_count': unfavourable_count,
        'favourable_count': favourable_count,
        'exact_count': exact_count,
        'machine_workload': list(machine_workload),
    }


def get_inventory_health_and_otif_report():
    """
    Calculates Inventory Health, Low-Stock Alerts, and Supplier OTIF (On-Time In-Full) Delivery %.
    """
    inventory_items = Inventory.objects.select_related('product', 'product__supplier').all()
    
    total_valuation = Decimal('0.00')
    low_stock_items = []

    for inv in inventory_items:
        val = (inv.quantity_available or Decimal('0.00')) * (inv.unit_cost or Decimal('0.00'))
        total_valuation += val

        # Reorder threshold alert (stock <= 10 units)
        if inv.quantity_available <= Decimal('10.00'):
            low_stock_items.append({
                'product_name': inv.product.name,
                'sku': inv.product.sku,
                'category': inv.product.category,
                'available': inv.quantity_available,
                'location': inv.location,
                'supplier_name': inv.product.supplier.name if inv.product.supplier else 'N/A'
            })

    # Supplier OTIF (On-Time In-Full) Delivery %
    delivered_procurements = ProcurementOrder.objects.filter(status='DELIVERED')
    total_delivered = delivered_procurements.count()
    
    # On-Time deliveries where delivery_date <= order_date + 7 days
    ontime_count = 0
    for proc in delivered_procurements:
        if proc.delivery_date and proc.order_date:
            order_d = proc.order_date
            deliv_d = proc.delivery_date.date() if hasattr(proc.delivery_date, 'date') else proc.delivery_date
            if deliv_d <= order_d + timedelta(days=7):
                ontime_count += 1

    otif_pct = Decimal('100.00')
    if total_delivered > 0:
        otif_pct = ((Decimal(str(ontime_count)) / Decimal(str(total_delivered))) * Decimal('100.00')).quantize(Decimal('0.01'))

    return {
        'total_inventory_items': inventory_items.count(),
        'total_valuation': total_valuation.quantize(Decimal('0.01')),
        'low_stock_count': len(low_stock_items),
        'low_stock_items': low_stock_items,
        'total_delivered_procurements': total_delivered,
        'otif_pct': otif_pct,
    }


def get_ar_ap_aging_report():
    """
    Calculates Accounts Receivable (A/R) Aging and Accounts Payable (A/P) Aging Buckets:
    - Current (0-30 days)
    - 31-60 days
    - 61-90 days
    - 90+ days
    """
    today = timezone.now().date()

    # Accounts Receivable (Sales Invoices unpaid/partial)
    unpaid_sales_invoices = SalesInvoice.objects.filter(status__in=['Unpaid', 'Partial']).select_related('customer')
    
    ar_aging = {
        'current_30': Decimal('0.00'),
        'days_31_60': Decimal('0.00'),
        'days_61_90': Decimal('0.00'),
        'days_90_plus': Decimal('0.00'),
        'total_ar': Decimal('0.00'),
        'details': []
    }

    for inv in unpaid_sales_invoices:
        bal = inv.remaining_balance
        if bal <= Decimal('0.00'):
            continue

        days_overdue = (today - inv.invoice_date).days if inv.invoice_date else 0
        ar_aging['total_ar'] += bal

        bucket = 'Current (0-30)'
        if days_overdue <= 30:
            ar_aging['current_30'] += bal
        elif days_overdue <= 60:
            ar_aging['days_31_60'] += bal
            bucket = '31-60 Days'
        elif days_overdue <= 90:
            ar_aging['days_61_90'] += bal
            bucket = '61-90 Days'
        else:
            ar_aging['days_90_plus'] += bal
            bucket = '90+ Days'

        ar_aging['details'].append({
            'invoice_number': inv.invoice_number,
            'customer_name': inv.customer.customer_name if inv.customer else 'N/A',
            'balance': bal,
            'invoice_date': inv.invoice_date,
            'days_overdue': days_overdue,
            'bucket': bucket
        })

    # Accounts Payable (Purchase Invoices unpaid/partial)
    unpaid_purchase_invoices = PurchaseInvoice.objects.filter(status__in=['UNPAID', 'PARTIAL']).select_related('supplier')
    
    ap_aging = {
        'current_30': Decimal('0.00'),
        'days_31_60': Decimal('0.00'),
        'days_61_90': Decimal('0.00'),
        'days_90_plus': Decimal('0.00'),
        'total_ap': Decimal('0.00'),
        'details': []
    }

    for inv in unpaid_purchase_invoices:
        bal = inv.remaining_balance
        if bal <= Decimal('0.00'):
            continue

        days_overdue = (today - inv.invoice_date).days if inv.invoice_date else 0
        ap_aging['total_ap'] += bal

        bucket = 'Current (0-30)'
        if days_overdue <= 30:
            ap_aging['current_30'] += bal
        elif days_overdue <= 60:
            ap_aging['days_31_60'] += bal
            bucket = '31-60 Days'
        elif days_overdue <= 90:
            ap_aging['days_61_90'] += bal
            bucket = '61-90 Days'
        else:
            ap_aging['days_90_plus'] += bal
            bucket = '90+ Days'

        ap_aging['details'].append({
            'invoice_number': inv.invoice_number,
            'supplier_name': inv.supplier.name if inv.supplier else 'N/A',
            'balance': bal,
            'invoice_date': inv.invoice_date,
            'days_overdue': days_overdue,
            'bucket': bucket
        })

    return {
        'ar_aging': ar_aging,
        'ap_aging': ap_aging,
    }
