from django.shortcuts import render, redirect
from django.contrib import messages
from .models import (
    DispatchRecord, ProcurementOrder, Product, Supplier, Inventory, 
    ProductionOrder, Invoice, Return, LossRecord, FinanceEntry, 
    Employee, Customer, WorkOrder, WorkOrderInstruction, SalesOrder
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
        quantity_lost = request.POST.get('quantity_lost')
        loss_date = request.POST.get('loss_date')
        reason = request.POST.get('reason')
        LossRecord.objects.create(product_id=product_id, quantity_lost=quantity_lost, loss_date=loss_date, reason=reason)
        messages.success(request, 'Loss record added successfully!')
    loss_records = LossRecord.objects.all()
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