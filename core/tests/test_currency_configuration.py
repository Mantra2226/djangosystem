"""
TEST CURRENCY CONFIGURATION & LOCALIZATION (core/tests/test_currency_configuration.py)

Comprehensive test suite verifying Kenyan Shillings (KSh / KES) currency formatting
across settings, template filters, OpenPyXL export masks, dashboard views, and admin displays.
"""

from decimal import Decimal
import io
import openpyxl

from django.test import TestCase, Client, override_settings
from django.conf import settings
from django.urls import reverse
from django.contrib.auth.models import User
from django.template import Template, Context

from core.templatetags.currency_filters import currency
from core.services.excel_export_service import (
    NUMBER_FORMAT_CURRENCY,
    export_queryset_to_excel,
    build_multi_sheet_workbook,
    format_cell_value,
)
from core.models import (
    Customer, SalesInvoice, Product, Inventory, MaterialVarianceRecord,
    WorkOrder, BillOfMaterial
)
from core.admin import format_admin_currency, ProductAdmin, InventoryAdmin, CustomerAdmin


class CurrencyConfigurationTests(TestCase):
    """Verifies settings, template tags, Excel masks, dashboard, and admin currency formatting."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='currency_admin',
            email='admin@glassputty.co.ke',
            password='Password123!'
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_global_currency_settings(self):
        """Verify settings define Kenyan Shilling constants."""
        self.assertTrue(hasattr(settings, 'CURRENCY_SYMBOL'))
        self.assertTrue(hasattr(settings, 'CURRENCY_CODE'))
        self.assertEqual(settings.CURRENCY_SYMBOL, 'KSh')
        self.assertEqual(settings.CURRENCY_CODE, 'KES')

    def test_currency_filter_formats_positive_values(self):
        """Verify positive numbers are formatted with KSh, commas, and 2 decimals."""
        self.assertEqual(currency(1250.5), "KSh 1,250.50")
        self.assertEqual(currency(Decimal('1250.50')), "KSh 1,250.50")
        self.assertEqual(currency(1000000), "KSh 1,000,000.00")
        self.assertEqual(currency("765.00"), "KSh 765.00")

    def test_currency_filter_formats_negative_values(self):
        """Verify negative numbers are cleanly formatted as -KSh X.XX without awkward signs."""
        self.assertEqual(currency(-137.55), "-KSh 137.55")
        self.assertEqual(currency(Decimal('-137.55')), "-KSh 137.55")
        self.assertEqual(currency(Decimal('500.00'), arg='neg'), "-KSh 500.00")
        self.assertEqual(currency(-500, arg='neg'), "-KSh 500.00")

    def test_currency_filter_handles_none_and_zero(self):
        """Verify None/empty returns '-' and zero returns 'KSh 0.00'."""
        self.assertEqual(currency(None), "-")
        self.assertEqual(currency(""), "-")
        self.assertEqual(currency(0), "KSh 0.00")
        self.assertEqual(currency(Decimal('0.00')), "KSh 0.00")

    def test_currency_filter_in_template(self):
        """Verify template rendering with currency filter."""
        template_str = "{% load currency_filters %}{{ revenue|currency }} | {{ margin|currency }}"
        t = Template(template_str)
        rendered = t.render(Context({'revenue': Decimal('54000.75'), 'margin': Decimal('-250.00')}))
        self.assertEqual(rendered, "KSh 54,000.75 | -KSh 250.00")

    def test_excel_export_currency_number_format(self):
        """Verify OpenPyXL cell format mask matches text-prefix KSh specification."""
        wb = openpyxl.Workbook()
        ws = wb.active
        cell = ws['A1']
        format_cell_value(cell, Decimal('1500.00'), 'currency')

        self.assertEqual(cell.value, 1500.0)
        self.assertEqual(cell.number_format, NUMBER_FORMAT_CURRENCY)
        self.assertIn('"KSh "', NUMBER_FORMAT_CURRENCY)
        self.assertIn('[Red]-"KSh "', NUMBER_FORMAT_CURRENCY)

    def test_format_admin_currency_helper(self):
        """Verify admin helper handles positive, negative, zero, and show_plus flags."""
        self.assertEqual(format_admin_currency(Decimal('500.00')), "KSh 500.00")
        self.assertEqual(format_admin_currency(Decimal('-137.55')), "-KSh 137.55")
        self.assertEqual(format_admin_currency(Decimal('0.00')), "KSh 0.00")
        self.assertEqual(format_admin_currency(None), "KSh 0.00")
        self.assertEqual(format_admin_currency(Decimal('45.00'), show_plus=True), "+KSh 45.00")
        self.assertEqual(format_admin_currency(Decimal('-45.00'), show_plus=True), "-KSh 45.00")

    def test_dashboard_renders_ksh_symbol(self):
        """Verify GET /reports/ renders KSh symbols in KPI cards and contains no hardcoded $."""
        url = reverse('admin_reports_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')

        # Verify KSh symbol is rendered on the dashboard
        self.assertIn("KSh", content)
        self.assertIn("Total Revenue", content)
        self.assertIn("Gross Profit", content)
        self.assertIn("Total COGM", content)

        # Verify no hardcoded dollar amounts like '$' in KPI values
        self.assertNotIn("kpi-value\">$", content)

    def test_admin_displays_ksh(self):
        """Verify ModelAdmin custom display methods render with KSh."""
        product = Product.objects.create(
            name="Glass Putty Standard 500g",
            sku="GP-STD-500G",
            product_type="FINISHED",
            category="PUTTY",
            unit_of_measurement="kg",
            selling_price=Decimal('450.00')
        )
        inv = Inventory.objects.create(
            product=product,
            quantity_available=Decimal('100.00'),
            unit_cost=Decimal('280.00'),
            location="Nairobi Central Warehouse"
        )
        customer = Customer.objects.create(
            customer_name="East Africa Glaziers Ltd",
            contact_info="orders@eaglaziers.co.ke"
        )

        from django.contrib.admin.sites import AdminSite
        site = AdminSite()

        prod_admin = ProductAdmin(Product, site)
        self.assertEqual(prod_admin.get_selling_price(product), "KSh 450.00")

        inv_admin = InventoryAdmin(Inventory, site)
        self.assertEqual(inv_admin.get_unit_cost(inv), "KSh 280.00")
        self.assertEqual(inv_admin.get_total_valuation(inv), "KSh 28,000.00")

        cust_admin = CustomerAdmin(Customer, site)
        self.assertEqual(cust_admin.get_total_receivables(customer), "KSh 0.00")
        self.assertEqual(cust_admin.get_available_credit(customer), "KSh 0.00")

    def test_excel_export_column_headers_have_ksh(self):
        """Verify preview and export endpoints specify KSh in column headers."""
        url = reverse('export_financial_analytics_excel') + '?preview=1'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        sheet1_headers = data['sheets'][0]['headers']
        self.assertIn('Amount (KSh)', sheet1_headers)
        
        sheet2_headers = data['sheets'][1]['headers']
        self.assertIn('Subtotal (KSh)', sheet2_headers)
        self.assertIn('Total (KSh)', sheet2_headers)
        self.assertIn('Remaining Balance (KSh)', sheet2_headers)
