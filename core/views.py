from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import csrf_exempt
from .models import (
    DispatchRecord, ProcurementOrder, Product, Supplier, Inventory, 
    ProductionOrder, SalesInvoice, Return, MaterialVarianceRecord, FinanceEntry, 
    Employee, Customer, WorkOrder, WorkOrderInstruction, SalesOrder,
    PurchaseOrder, WorkOrderMaterialLine, StockTransaction, BillOfMaterial, BOMItem
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
        selling_price = request.POST.get('selling_price')
        
        create_kwargs = {
            'name': name,
            'supplier_id': supplier_id if product_type == 'RAW' else None,
            'product_type': product_type,
            'category': category,
            'unit_of_measurement': unit_of_measurement
        }
        if selling_price and product_type in ['FINISHED', 'INTERMEDIATE']:
            create_kwargs['selling_price'] = selling_price

        Product.objects.create(**create_kwargs)
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
    """
    Populates standard 4-step process instruction blueprints for a newly created WorkOrder
    if no custom instructions currently exist.
    """
    if not work_order or not work_order.pk:
        return

    from .models import WorkOrderInstruction

    if work_order.instructions.exists():
        return

    is_packaging = (work_order.category == 'PACKAGING') or (
        work_order.product and work_order.product.product_type == 'FINISHED'
    )

    if is_packaging:
        default_steps = [
            (1, "Line Prep & Staging", "Packaging Line #1", "Inspect container cleanliness & verify bulk source batch availability.", 15),
            (2, "Container Filling", "Volumetric Filling Station", "Fill discrete containers to target unit pack count.", 45),
            (3, "Sealing & Labeling", "Automated Capper / Labeler", "Cap, seal, and apply product barcode labels to filled containers.", 30),
            (4, "Palletizing & Warehouse Transfer", "Pallet Wrapper", "Palletize finished goods and transfer to warehouse inventory.", 20),
        ]
    else:
        # PRODUCTION / Bulk Mixing
        default_steps = [
            (1, "Vessel Setup & Cleanliness Check", "Mixing Vessel A", "Verify vessel cleanliness, valve seals, and raw material staging.", 15),
            (2, "Raw Material Component Charge", "Raw Component Hopper", "Charge raw material components into mixing vessel per active BOM recipe.", 30),
            (3, "Agitation & Thermal Reaction Processing", "High-Shear Agitator", "Run agitation and thermal processing to target viscosity and homogeneity.", 60),
            (4, "QA Viscosity Sampling & Bulk Transfer", "Bulk Holding Tank", "Perform QA viscosity sampling and transfer bulk yield to holding tank.", 30),
        ]

    for step_num, step_name, machine, text, est_minutes in default_steps:
        WorkOrderInstruction.objects.get_or_create(
            work_order=work_order,
            step_number=step_num,
            defaults={
                'product': work_order.product,
                'step_name': step_name,
                'machine': machine,
                'instruction_text': text,
                'estimated_time_minutes': est_minutes,
                'status': 'IN_PROGRESS'
            }
        )


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


@csrf_exempt
@staff_member_required
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


@staff_member_required
def reports_dashboard_view(request):
    """
    Executive Reporting Analytics Dashboard.
    Provides consolidated operational metrics across Financial P&L, COGM, Yield/Scrap, 
    Inventory Health/OTIF, and Accounts Receivable/Payable Aging.
    """
    from datetime import datetime
    from .reports import (
        get_profit_and_loss_summary,
        get_cogm_report,
        get_production_yield_and_scrap_report,
        get_inventory_health_and_otif_report,
        get_ar_ap_aging_report
    )

    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    start_date = None
    end_date = None

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    pnl = get_profit_and_loss_summary(start_date, end_date)
    cogm = get_cogm_report(start_date, end_date)
    yield_scrap = get_production_yield_and_scrap_report(start_date, end_date)
    inventory_otif = get_inventory_health_and_otif_report()
    aging = get_ar_ap_aging_report()

    context = {
        'start_date': start_date_str or '',
        'end_date': end_date_str or '',
        'pnl': pnl,
        'cogm': cogm,
        'yield_scrap': yield_scrap,
        'inventory_otif': inventory_otif,
        'ar_aging': aging['ar_aging'],
        'ap_aging': aging['ap_aging'],
    }
    return render(request, 'core/reports_dashboard.html', context)


# =============================================================================
# RESTFUL JSON API ENDPOINTS (Utilizing core/serializers.py)
# =============================================================================

import json
from django.views.decorators.csrf import csrf_protect
from .serializers import (
    ProductSerializer, InventorySerializer, WorkOrderSerializer,
    ProductionOrderSerializer, ProcurementOrderSerializer, SalesOrderSerializer
)

@csrf_exempt
def api_products_list_create(request):
    """
    RESTful JSON API Endpoint for Product resources.
    GET /api/products/ - Returns list of serialized products.
    POST /api/products/ - Validates payload via ProductSerializer and creates product.
    """
    if request.method == 'GET':
        product_type = request.GET.get('type')
        qs = Product.objects.select_related('supplier').prefetch_related('stock').all()
        if product_type:
            qs = qs.filter(product_type=product_type.upper())
        data = ProductSerializer.serialize_queryset(qs)
        return JsonResponse({"status": "success", "data": data}, status=200)

    elif request.method == 'POST':
        try:
            raw_body = request.body.decode('utf-8')
            payload = json.loads(raw_body) if raw_body else request.POST.dict()
        except Exception:
            payload = request.POST.dict()

        validated_data = ProductSerializer.validate_and_deserialize(payload)
        product = Product.objects.create(**validated_data)
        serialized = ProductSerializer.serialize(product)
        return JsonResponse({"status": "success", "data": serialized}, status=201)

    return JsonResponse({"status": "error", "message": "Method not allowed."}, status=405)


def api_work_orders_list(request):
    """GET /api/work-orders/ - Returns list of serialized Work Orders."""
    qs = WorkOrder.objects.select_related('product', 'bill_of_material', 'parent_work_order').prefetch_related('employee', 'material_lines__component').all()
    data = WorkOrderSerializer.serialize_queryset(qs)
    return JsonResponse({"status": "success", "data": data}, status=200)


def api_inventory_list(request):
    """GET /api/inventory/ - Returns list of serialized Inventory stock levels."""
    qs = Inventory.objects.select_related('product', 'product__supplier').all()
    data = InventorySerializer.serialize_queryset(qs)
    return JsonResponse({"status": "success", "data": data}, status=200)


def api_production_orders_list(request):
    """GET /api/production-orders/ - Returns list of serialized Production Orders."""
    qs = ProductionOrder.objects.select_related('product', 'work_order').all()
    data = ProductionOrderSerializer.serialize_queryset(qs)
    return JsonResponse({"status": "success", "data": data}, status=200)


def api_sales_orders_list(request):
    """GET /api/sales-orders/ - Returns list of serialized Customer Sales Orders."""
    qs = SalesOrder.objects.select_related('customer').prefetch_related('items__product').all()
    data = SalesOrderSerializer.serialize_queryset(qs)
    return JsonResponse({"status": "success", "data": data}, status=200)


def api_procurement_orders_list(request):
    """GET /api/procurements/ - Returns list of serialized Procurement Orders."""
    qs = ProcurementOrder.objects.select_related('purchase_order', 'product', 'purchase_order__supplier').all()
    data = ProcurementOrderSerializer.serialize_queryset(qs)
    return JsonResponse({"status": "success", "data": data}, status=200)


# =============================================================================
# SHOP-FLOOR REAL-TIME DRF VIEWSETS
# =============================================================================

from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .permissions import (
    IsProductionSupervisor,
    IsShopFloorOperatorOrSupervisor,
    IsWorkOrderActiveForLogging
)
from .serializers import (
    MaterialLogSerializer,
    MaterialLineDRFSerializer,
    WorkOrderDetailDRFSerializer,
    WorkOrderCompletionSerializer
)


class WorkOrderViewSet(mixins.ListModelMixin,
                       mixins.RetrieveModelMixin,
                       viewsets.GenericViewSet):
    """
    DRF ViewSet for Work Order management and real-time shop-floor operational logging.
    """
    serializer_class = WorkOrderDetailDRFSerializer

    def get_queryset(self):
        queryset = WorkOrder.objects.select_related(
            'product', 'bill_of_material', 'parent_work_order'
        ).prefetch_related('material_lines__component', 'employee')
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        category_param = self.request.query_params.get('category')
        if category_param:
            queryset = queryset.filter(category=category_param)
        return queryset

    def get_permissions(self):
        """
        Applies role-based permissions and lifecycle object guards based on action.
        """
        if self.action in ['start_production', 'resolve_shortage', 'complete_order']:
            permission_classes = [IsProductionSupervisor]
        elif self.action == 'log_material':
            permission_classes = [IsShopFloorOperatorOrSupervisor, IsWorkOrderActiveForLogging]
        else:
            permission_classes = [IsShopFloorOperatorOrSupervisor]
        return [permission() for permission in permission_classes]

    @action(detail=True, methods=['post'], url_path='log-material')
    def log_material(self, request, pk=None):
        """
        Shop-Floor Operator endpoint for logging actual consumed material on active Work Orders.
        Executes Phase 2 incremental delta deductions against warehouse inventory under row locks.
        """
        work_order = self.get_object()  # Enforces IsWorkOrderActiveForLogging object permission
        serializer = MaterialLogSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        component_id = serializer.validated_data['component_id']
        quantity_actual = serializer.validated_data['quantity_actual']

        with transaction.atomic():
            line = WorkOrderMaterialLine.objects.select_for_update().filter(
                work_order=work_order,
                component_id=component_id
            ).first()

            if not line:
                return Response(
                    {"error": f"Material line for component ID {component_id} not found on Work Order #{work_order.pk}."},
                    status=status.HTTP_404_NOT_FOUND
                )

            deducted = line.deducted_quantity or Decimal('0.00')
            if quantity_actual < deducted:
                is_supervisor = request.user.is_superuser or request.user.groups.filter(name='Production Supervisor').exists()
                if not is_supervisor:
                    return Response(
                        {"detail": "Decreasing logged quantity below already deducted stock requires supervisor authorization."},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            delta = quantity_actual - deducted

            if delta != Decimal('0.00'):
                raw_inv, _ = Inventory.objects.select_for_update().get_or_create(
                    product=line.component,
                    defaults={'quantity_available': Decimal('0.00'), 'quantity_allocated': Decimal('0.00')}
                )

                if delta > Decimal('0.00'):
                    # Deduct from allocation pool first if available, otherwise from available pool
                    if raw_inv.quantity_allocated >= delta:
                        raw_inv.quantity_allocated -= delta
                    else:
                        excess = delta - raw_inv.quantity_allocated
                        raw_inv.quantity_allocated = Decimal('0.00')
                        raw_inv.quantity_available -= excess
                    trans_type = 'PRODUCTION_CONSUMPTION'
                    trans_qty = -delta
                    notes = f"Shop-Floor API consumption: deducted {delta} units for Work Order #{work_order.pk} ({line.component.name})"
                else:
                    # Refund abs(delta) back to inventory.quantity_available
                    refund = abs(delta)
                    raw_inv.quantity_available += refund
                    trans_type = 'ADJUSTMENT_IN'
                    trans_qty = refund
                    notes = f"Shop-Floor API consumption: refunded {refund} units for Work Order #{work_order.pk} ({line.component.name})"

                raw_inv.save(update_fields=['quantity_available', 'quantity_allocated'])

                StockTransaction.objects.create(
                    product=line.component,
                    quantity=trans_qty,
                    transaction_type=trans_type,
                    work_order=work_order,
                    notes=notes
                )

                line.deducted_quantity = (line.deducted_quantity or Decimal('0.00')) + delta

            line.quantity_actual = quantity_actual
            line.save()

        line.refresh_from_db()
        return Response(MaterialLineDRFSerializer(line).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='start-production')
    def start_production(self, request, pk=None):
        """
        Production Supervisor endpoint for moving DRAFT / ON_HOLD_SHORTAGE orders to IN_PROGRESS.
        Validates BOM readiness and allocates warehouse stock.
        """
        work_order = self.get_object()
        try:
            success, message = work_order.start_production()
            work_order.refresh_from_db()
            response_data = {
                'status': 'success' if success else 'shortage_detected',
                'message': message,
                'work_order': WorkOrderDetailDRFSerializer(work_order).data
            }
            return Response(response_data, status=status.HTTP_200_OK if success else status.HTTP_400_BAD_REQUEST)
        except ValidationError as e:
            error_msg = e.messages if hasattr(e, 'messages') else (e.message_dict if hasattr(e, 'message_dict') else str(e))
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='resolve-shortage')
    def resolve_shortage(self, request, pk=None):
        """
        Production Supervisor endpoint for executing interactive shortage resolution pathways.
        Supported choices: TOP_UP_BULK, DOWNSCALE_TARGET, HOLD_FOR_EXISTING.
        """
        work_order = self.get_object()
        choice = request.data.get('choice')
        valid_choices = ['TOP_UP_BULK', 'HOLD_FOR_EXISTING', 'DOWNSCALE_TARGET']
        if choice not in valid_choices:
            return Response(
                {'error': f"Invalid resolution choice '{choice}'. Must be one of {valid_choices}."},
                status=status.HTTP_400_BAD_REQUEST
            )

        existing_bulk_wo_id = request.data.get('existing_bulk_wo_id')
        try:
            work_order.resolve_bulk_shortage(choice, existing_bulk_wo_id=existing_bulk_wo_id)
            work_order.refresh_from_db()
            return Response({
                'status': 'success',
                'message': f"Successfully executed shortage resolution '{choice}' for Work Order #{work_order.work_order_code or work_order.pk}.",
                'work_order': WorkOrderDetailDRFSerializer(work_order).data
            }, status=status.HTTP_200_OK)
        except ValidationError as e:
            error_msg = e.messages if hasattr(e, 'messages') else (e.message_dict if hasattr(e, 'message_dict') else str(e))
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='complete-order')
    def complete_order(self, request, pk=None):
        """
        Production Supervisor endpoint for completing an active Work Order.
        Triggers Phase 3 stock reconciliation and releases unconsumed allocations.
        """
        work_order = self.get_object()
        current_status = (work_order.status or '').upper().strip()
        if current_status != 'IN_PROGRESS':
            return Response(
                {'error': f"Cannot complete Work Order #{work_order.pk}: current status is '{work_order.status}'. Only 'IN_PROGRESS' orders can be completed."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = WorkOrderCompletionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                work_order.instructions.filter(status='IN_PROGRESS').update(status='COMPLETED')
                work_order.actual_quantity_produced = serializer.validated_data['actual_quantity_produced']
                work_order.scrap_quantity = serializer.validated_data.get('scrap_quantity', Decimal('0.00'))
                scrap_reason = serializer.validated_data.get('scrap_reason', '')
                if scrap_reason:
                    work_order.scrap_reason = scrap_reason
                work_order.status = 'COMPLETED'
                if not work_order.production_end_date:
                    work_order.production_end_date = timezone.now()
                work_order.save(update_fields=[
                    'status', 'production_end_date', 'actual_quantity_produced',
                    'scrap_quantity', 'scrap_reason'
                ])
                work_order.process_inventory()

            work_order.refresh_from_db()
            return Response({
                'status': 'success',
                'message': f"Work Order #{work_order.work_order_code or work_order.pk} completed successfully.",
                'work_order': WorkOrderDetailDRFSerializer(work_order).data
            }, status=status.HTTP_200_OK)
        except ValidationError as e:
            error_msg = e.messages if hasattr(e, 'messages') else (e.message_dict if hasattr(e, 'message_dict') else str(e))
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)



