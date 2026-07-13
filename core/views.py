from django.shortcuts import render, redirect
from django.contrib import messages
from .models import DispatchRecord, ProcurementOrder, Product, Supplier, Inventory, ProductionOrder, Invoice, Return, LossRecord, FinanceEntry, Employee, Customer, WorkOrder, WorkOrderInstruction
# Create your views here.

def index(request):
    if not request.user.is_authenticated:
        return render(request, 'core/login.html')
    return render(request, 'core/index.html')

def login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        # Simple authentication logic (for demonstration purposes only)
        if username == 'admin' and password == 'password':
            messages.success(request, 'Login successful!')
            return render(request, 'core/home.html')
        else:
            messages.error(request, 'Invalid credentials')
            return redirect(request, 'core/index.html')
    return render(request, 'core/home.html')

def supplier_form(request):
    if request.method == 'POST':
        supplier_id = request.POST.get('supplier_id')
        contact_info = request.POST.get('contact_info')
        payment_terms = request.POST.get('payment_terms')
        Supplier.objects.create(supplier_id=supplier_id, contact_info=contact_info, payment_terms=payment_terms)
    messages.success(request, 'Supplier added successfully!')
    suppliers = Supplier.objects.all()
    return render(request, 'core/supplier_form.html', {'suppliers': suppliers})

def customer_form(request):
    if request.method == 'POST':
        customer_id = request.POST.get('customer_id')
        contact_info = request.POST.get('contact_info')
        Customer.objects.create(customer_id=customer_id, contact_info=contact_info)
    messages.success(request, 'Customer added successfully!')
    customers = Customer.objects.all()
    return render(request, 'core/customer_form.html', {'customers': customers})

def dispatch_form(request):
    if request.method == 'POST':
        production_order_id = request.POST.get('production_order_id')
        customer_id = request.POST.get('customer_id')
        quantity_dispatched = request.POST.get('quantity_dispatched')
        dispatch_date = request.POST.get('dispatch_date')
        delivery_date = request.POST.get('delivery_date')
        DispatchRecord.objects.create(production_order_id=production_order_id, customer_id=customer_id, quantity_dispatched=quantity_dispatched, dispatch_date=dispatch_date, delivery_date=delivery_date)
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
        loss_type = request.POST.get('loss_type')
        amount = request.POST.get('amount')
        loss_date = request.POST.get('loss_date')
        LossRecord.objects.create(loss_type=loss_type, amount=amount, loss_date=loss_date)
    messages.success(request, 'Loss record added successfully!')
    loss_records = LossRecord.objects.all()
    return render(request, 'core/loss_record_form.html', {'loss_records': loss_records})

def procurement_form(request):
    if request.method == 'POST':
        material_id = request.POST.get('material_id')
        supplier_id = request.POST.get('supplier_id')
        quantity_ordered = request.POST.get('quantity_ordered')
        price_per_unit = request.POST.get('price_per_unit')
        order_date = request.POST.get('order_date')
        ProcurementOrder.objects.create(material_id=material_id, supplier_id=supplier_id, quantity_ordered=quantity_ordered, price_per_unit=price_per_unit, order_date=order_date)
    messages.success(request, 'Procurement order added successfully!')
    procurement_orders = ProcurementOrder.objects.all()
    return render(request, 'core/procurement_form.html', {'procurement_orders': procurement_orders})

def product_form(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        supplier_id = request.POST.get('supplier_id')
        cost_per_unit = request.POST.get('cost_per_unit')
        Product.objects.create(name=name, supplier_id=supplier_id, cost_per_unit=cost_per_unit)
    messages.success(request, 'Product added successfully!')
    inventory_items = Inventory.objects.all()
    return render(request, 'core/product_form.html', {'inventory_items': inventory_items})

def production(request):
    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        employee_id = request.POST.get('employee_id')
        work_order_id = request.POST.get('work_order_id')
        quantity_consumed = request.POST.get('quantity_consumed')
        quantity_produced = request.POST.get('quantity_produced')
        production_start_date = request.POST.get('production_start_date')
        ProductionOrder.objects.create(product_id=product_id, employee_id=employee_id, work_order_id=work_order_id, quantity_consumed=quantity_consumed, quantity_produced=quantity_produced, production_start_date=production_start_date)
    messages.success(request, 'Production order added successfully!')
    production_orders = ProductionOrder.objects.all()
    return render(request, 'core/production_form.html', {'production_orders': production_orders})

def return_form(request):
    if request.method == 'POST':
        dispatch_id = request.POST.get('dispatch_id')
        customer_id = request.POST.get('customer_id')
        quantity_returned = request.POST.get('quantity_returned')
        reason_for_return = request.POST.get('reason_for_return')
        quality_control_status = request.POST.get('quality_control_status')
        Return.objects.create(dispatch_id=dispatch_id, customer_id=customer_id, quantity_returned=quantity_returned, reason_for_return=reason_for_return, quality_control_status=quality_control_status)
    messages.success(request, 'Return record added successfully!')
    return_records = Return.objects.all()
    return render(request, 'core/return_form.html', {'return_records': return_records})

def generate_work_order_instructions(work_order):
    templates = WorkOrderInstruction.objects.filter(work_order_id=work_order.work_order_id)    
    for template in templates:
        WorkOrderInstruction.objects.create(
            work_order_id=work_order,
            step_number=template.step_number,
            machine=template.machine,
            instruction_text=template.instruction_text,
            estimated_time_minutes=template.estimated_time_minutes
        ).material_id.set(template.material_id.all())
    