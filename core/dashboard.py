"""
DASHBOARD CALLBACK FOR DJANGO UNFOLD (core/dashboard.py)

Calculates real-time manufacturing, inventory valuation, and commercial billing KPIs
for the Glass Putty Manufacturing Command Center admin index page.
"""

from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum
from .models import WorkOrder, ProductionOrder, Inventory, SalesInvoice, FinanceEntry, Product


def dashboard_callback(request, context):
    """
    Computes key performance indicators (KPIs) across shop-floor operations,
    warehouse inventory, and accounts receivable to populate the Unfold dashboard.
    """
    # 1. Shop-Floor Production Metrics
    active_work_orders = WorkOrder.objects.filter(status__in=['IN_PROGRESS', 'DRAFT'])
    active_work_orders_count = active_work_orders.count()
    in_progress_count = WorkOrder.objects.filter(status='IN_PROGRESS').count()
    
    # 2. MRP Shortages & Attention Alerts
    shortage_count = WorkOrder.objects.filter(status__in=['ON_HOLD_SHORTAGE', 'AWAITING_RESOLUTION']).count()
    
    # 3. Finished Goods Stock (Glass Putty 5kg Tins, etc.)
    finished_goods_qty = Inventory.objects.filter(
        product__product_type='FINISHED'
    ).aggregate(total=Sum('quantity_available'))['total'] or Decimal('0.00')
    
    # 4. Total Warehouse Inventory Valuation
    all_inventories = Inventory.objects.select_related('product').all()
    total_warehouse_valuation = sum(
        (inv.total_valuation for inv in all_inventories),
        Decimal('0.00')
    )
    
    # 5. Outstanding Accounts Receivable
    open_invoices = SalesInvoice.objects.filter(status__in=['POSTED', 'PARTIALLY_PAID']).prefetch_related('sales_payments')
    total_receivables = sum(
        (inv.remaining_balance for inv in open_invoices),
        Decimal('0.00')
    )
    
    # 6. Month-to-Date Sales Revenue
    now = timezone.now().date()
    start_of_month = now.replace(day=1)
    mtd_revenue = FinanceEntry.objects.filter(
        entry_type='REVENUE',
        category='SALES',
        entry_date__gte=start_of_month
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # Structured KPI card definitions for Unfold dashboard
    kpi_cards = [
        {
            "title": "Active Production Runs",
            "metric": f"{in_progress_count} In-Progress",
            "footer": f"{active_work_orders_count} Total Active Batches",
            "icon": "precision_manufacturing",
        },
        {
            "title": "MRP Shortage Alerts",
            "metric": f"{shortage_count} Orders On Hold",
            "footer": "Requires Supervisor Attention" if shortage_count > 0 else "All Material Lines Satisfied",
            "icon": "warning",
        },
        {
            "title": "Finished Goods Stock",
            "metric": f"{finished_goods_qty:,.0f} Tins",
            "footer": "Ready for Customer Dispatch",
            "icon": "inventory_2",
        },
        {
            "title": "Total Warehouse Valuation",
            "metric": f"${total_warehouse_valuation:,.2f}",
            "footer": "Raw Materials + Packaging + FG",
            "icon": "account_balance",
        },
        {
            "title": "Open Accounts Receivable",
            "metric": f"${total_receivables:,.2f}",
            "footer": f"{open_invoices.count()} Unsettled Invoices",
            "icon": "receipt_long",
        },
        {
            "title": "Revenue (Month-to-Date)",
            "metric": f"${mtd_revenue:,.2f}",
            "footer": f"Since {start_of_month.strftime('%B 1, %Y')}",
            "icon": "trending_up",
        },
    ]

    context.update({
        "kpi_cards": kpi_cards,
        "active_work_orders_count": active_work_orders_count,
        "shortage_count": shortage_count,
        "total_warehouse_valuation": total_warehouse_valuation,
        "total_receivables": total_receivables,
        "mtd_revenue": mtd_revenue,
    })
    return context
