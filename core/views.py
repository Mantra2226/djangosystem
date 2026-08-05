from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from .models import (
    DispatchRecord, ProcurementOrder, Product, Supplier, Inventory, 
    ProductionOrder, SalesInvoice, Return, MaterialVarianceRecord, FinanceEntry, 
    Employee, Customer, WorkOrder, WorkOrderInstruction, SalesOrder,
    PurchaseOrder
)

def index(request):
    if not request.user.is_authenticated:
        return render(request, 'core/login.html')
    return render(request, 'core/index.html')

def login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        if username == 'admin' and password == 'password':
            messages.success(request, 'Login successful!')
            return render(request, 'core/home.html')
        else:
            messages.error(request, 'Invalid credentials')
            return redirect('index')
    return render(request, 'core/login.html')

def supplier_form(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        contact_info = request.POST.get('contact_info')
        Supplier.objects.create(name=name, contact_info=contact_info)
        messages.success(request, 'Supplier added successfully!')
    suppliers = Supplier.objects.all()
    return render(request, 'core/supplier_form.html', {'suppliers': suppliers})

def customer_form(request):
    if request.method == 'POST':
        customer_name = request.POST.get('customer_name')
        contact_info = request.POST.get('contact_info')
        shipping_address = request.POST.get('shipping_address')
        Customer.objects.create(customer_name=customer_name, contact_info=contact_info, shipping_address=shipping_address)
        messages.success(request, 'Customer added successfully!')
    customers = Customer.objects.all()
    return render(request, 'core/customer_form.html', {'customers': customers})

def dispatch_form(request):
    if request.method == 'POST':
        sales_order_id = request.POST.get('sales_order_id')
        product_id = request.POST.get('product_id')
        quantity_dispatched = request.POST.get('quantity_dispatched')
        dispatch_date = request.POST.get('dispatch_date')
        status = request.POST.get('status', 'pending')
        DispatchRecord.objects.create(
            sales_order_id=sales_order_id, product_id=product_id, 
            quantity_dispatched=quantity_dispatched, dispatch_date=dispatch_date, status=status
        )
        messages.success(request, 'Dispatch record added successfully!')
    dispatch_records = DispatchRecord.objects.all()
    return render(request, 'core/dispatch_form.html', {'dispatch_records': dispatch_records})

def finance_entry_form(request):
    if request.method == 'POST':
        entry_type = request.POST.get('entry_type')
        amount = request.POST.get('amount')
        entry_date = request.POST.get('entry_date')
        category = request.POST.get('category')
        FinanceEntry.objects.create(entry_type=entry_type, amount=amount, entry_date=entry_date, category=category)
        messages.success(request, 'Finance entry added successfully!')
    finance_entries = FinanceEntry.objects.all()
    return render(request, 'core/financial_entry_form.html', {'finance_entries': finance_entries})

def loss_record_form(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        quantity_variance = request.POST.get('quantity_lost') or request.POST.get('quantity_variance')
        reason = request.POST.get('reason')
        MaterialVarianceRecord.objects.create(
            product_id=product_id,
            quantity_variance=quantity_variance,
            notes=reason
        )
        messages.success(request, 'Material variance record added successfully!')
    loss_records = MaterialVarianceRecord.objects.all()
    return render(request, 'core/loss_record_form.html', {'loss_records': loss_records})

def procurement_form(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        supplier_id = request.POST.get('supplier_id')
        quantity = request.POST.get('quantity')
        price_per_unit = request.POST.get('price_per_unit')
        order_date = request.POST.get('order_date')
        delivery_date = request.POST.get('delivery_date')
        status = request.POST.get('status', 'PENDING')
        ProcurementOrder.objects.create(
            product_id=product_id, supplier_id=supplier_id, quantity=quantity, 
            price_per_unit=price_per_unit, order_date=order_date, delivery_date=delivery_date, status=status
        )
        messages.success(request, 'Procurement order added successfully!')
    procurement_orders = ProcurementOrder.objects.all()
    return render(request, 'core/procurement_form.html', {'procurement_orders': procurement_orders})

def product_form(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        supplier_id = request.POST.get('supplier_id')
        product_type = request.POST.get('product_type', 'FINISHED')
        category = request.POST.get('category', 'General')
        unit_of_measurement = request.POST.get('unit_of_measurement', 'pcs')
        Product.objects.create(
            name=name, supplier_id=supplier_id, 
            product_type=product_type, category=category, unit_of_measurement=unit_of_measurement
        )
        messages.success(request, 'Product added successfully!')
    inventory_items = Inventory.objects.all()
    return render(request, 'core/product_form.html', {'inventory_items': inventory_items})

def production(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        work_order_id = request.POST.get('work_order_id')
        quantity = request.POST.get('quantity')
        status = request.POST.get('status', 'IN_PROGRESS')
        ProductionOrder.objects.create(
            product_id=product_id, work_order_id=work_order_id, quantity=quantity, status=status
        )
        messages.success(request, 'Production order added successfully!')
    production_orders = ProductionOrder.objects.all()
    return render(request, 'core/production_form.html', {'production_orders': production_orders})

def return_form(request):
    if request.method == 'POST':
        dispatch_id = request.POST.get('dispatch_id')
        customer_id = request.POST.get('customer_id')
        quantity_returned = request.POST.get('quantity_returned')
        reason_for_return = request.POST.get('reason_for_return')
        quality_control_status = request.POST.get('quality_control_status', 'PENDING')
        Return.objects.create(
            dispatch_id=dispatch_id, customer_id=customer_id, quantity_returned=quantity_returned,
            reason_for_return=reason_for_return, quality_control_status=quality_control_status
        )
        messages.success(request, 'Return record added successfully!')
    return_records = Return.objects.all()
    return render(request, 'core/return_form.html', {'return_records': return_records})

def generate_work_order_instructions(work_order):
    pass


@staff_member_required
def po_products_json(request):
    """
    JSON endpoint used by the ProcurementOrder admin form's client-side JS.

    Returns the list of products linked to a given Purchase Order so the
    product <select> dropdown can be dynamically filtered in real time as
    soon as the operator picks a PO from the autocomplete widget.

    URL: /admin/core/procurementorder/po-products/?po_id=<int>
    Response: { "products": [ {"id": <int>, "name": "<str>"}, ... ] }
    """
    po_id = request.GET.get("po_id")

    if not po_id:
        # No PO specified — return empty list; JS will show placeholder text
        return JsonResponse({"products": []})

    # Look up the PO and collect the raw-material products on its line items
    po = PurchaseOrder.objects.filter(pk=po_id).first()
    if not po:
        return JsonResponse({"products": []})

    # Fetch only the products actually listed on this PO's items
    products = (
        Product.objects
        .filter(po_items__purchase_order=po)
        .distinct()
        .values("product_id", "name", "sku")
    )

    product_list = [
        {
            "id":   p["product_id"],
            # Include SKU so operators can identify items at a glance
            "name": f"{p['name']} ({p['sku']})" if p["sku"] else p["name"],
        }
        for p in products
    ]

    return JsonResponse({"products": product_list})


def mrp_resolve_action(request):
    """
    HTTP POST Handler for executing tailored MRP resolution pathways from Admin/Dashboard.
    """
    if request.method == 'POST':
        from django.contrib import messages
        from django.shortcuts import redirect
        from .models import ProductionOrder
        from .services import (
            resolve_raw_autodraft_po,
            resolve_raw_direct_procurement,
            resolve_raw_hold_inbound,
            resolve_intermediate_build,
            resolve_intermediate_hold_active,
            resolve_intermediate_partial_batch
        )

        po_id = request.POST.get('production_order_id')
        component_id = request.POST.get('component_id')
        shortfall_qty = request.POST.get('shortfall_qty', '0.00')
        action = request.POST.get('resolution_action')
        max_producible = request.POST.get('max_producible', '0.00')

        po = ProductionOrder.objects.filter(pk=po_id).first()
        if not po:
            messages.error(request, "Production order not found.")
            return redirect(request.META.get('HTTP_REFERER', '/admin/'))

        try:
            if action == 'raw_autodraft_po':
                new_po = resolve_raw_autodraft_po(po, component_id, shortfall_qty)
                messages.success(request, f"Auto-drafted Purchase Order #{new_po.po_number}.")
            elif action == 'raw_direct_procurement':
                proc = resolve_raw_direct_procurement(po, component_id, shortfall_qty)
                messages.success(request, f"Spawned direct Procurement Order #{proc.procurement_order_id}.")
            elif action == 'raw_hold_inbound':
                resolve_raw_hold_inbound(po, component_id)
                messages.info(request, "Order status held for inbound PO stock.")
            elif action == 'intermediate_build':
                wo, child_po = resolve_intermediate_build(po, component_id, shortfall_qty)
                messages.success(request, f"Spawned child Sub-Assembly Work Order #{wo.pk} (Production Run #{child_po.pk}).")
            elif action == 'intermediate_hold_active':
                resolve_intermediate_hold_active(po, component_id)
                messages.info(request, "Linked order to active intermediate shop floor run.")
            elif action == 'intermediate_partial_batch':
                resolve_intermediate_partial_batch(po, max_producible)
                messages.success(request, f"Down-scaled production batch to {max_producible} units.")
            else:
                messages.warning(request, "Unknown resolution action.")
        except Exception as e:
            messages.error(request, f"MRP Resolution Error: {str(e)}")

        redirect_url = request.META.get('HTTP_REFERER') or f'/admin/core/productionorder/{po_id}/change/'
        return redirect(redirect_url)

    return redirect('/admin/')