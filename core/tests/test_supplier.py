from django.test import TestCase
from django.contrib.admin.sites import site

from core.models import Supplier, Product, PurchaseOrder, PurchaseInvoice
from core.admin import SupplierAdmin, ProductAdmin, PurchaseOrderAdmin, PurchaseInvoiceAdmin


class SupplierCodeAndAdminTests(TestCase):
    """
    Unit and integration tests for unique supplier code generation
    and Supplier admin configuration hiding supplier_id.
    """

    def test_supplier_code_auto_generated(self):
        """Creating a supplier automatically generates a code in format SUP-0001."""
        s1 = Supplier.objects.create(name="Alpha Chemicals", contact_info="alpha@test.com")
        self.assertIsNotNone(s1.supplier_code)
        self.assertTrue(s1.supplier_code.startswith("SUP-"))
        self.assertEqual(s1.supplier_code, "SUP-0001")

    def test_supplier_code_increments_sequentially(self):
        """Subsequent suppliers increment sequentially: SUP-0001, SUP-0002, etc."""
        s1 = Supplier.objects.create(name="Alpha Chemicals", contact_info="alpha@test.com")
        s2 = Supplier.objects.create(name="Beta Metals", contact_info="beta@test.com")
        s3 = Supplier.objects.create(name="Gamma Plastics", contact_info="gamma@test.com")

        self.assertEqual(s1.supplier_code, "SUP-0001")
        self.assertEqual(s2.supplier_code, "SUP-0002")
        self.assertEqual(s3.supplier_code, "SUP-0003")

    def test_supplier_str_representation(self):
        """Supplier __str__ displays code and name."""
        s = Supplier.objects.create(name="Delta Packaging", contact_info="delta@test.com")
        self.assertEqual(str(s), f"{s.supplier_code} - Delta Packaging")

    def test_supplier_admin_list_display_hides_id_and_shows_code(self):
        """SupplierAdmin must show supplier_code and must NOT include supplier_id in list_display."""
        admin_instance = site._registry.get(Supplier)
        self.assertIsNotNone(admin_instance, "Supplier must be registered in admin site.")

        list_display = admin_instance.list_display
        self.assertIn('supplier_code', list_display, "supplier_code must be in list_display.")
        self.assertNotIn('supplier_id', list_display, "supplier_id must be hidden from list_display.")
        self.assertIn('name', list_display)
        self.assertIn('contact_info', list_display)

    def test_supplier_admin_search_fields(self):
        """SupplierAdmin search_fields includes supplier_code, name, contact_info and excludes supplier_id."""
        admin_instance = site._registry.get(Supplier)
        search_fields = admin_instance.search_fields

        self.assertIn('supplier_code', search_fields)
        self.assertIn('name', search_fields)
        self.assertIn('contact_info', search_fields)
        self.assertNotIn('supplier_id', search_fields)

    def test_supplier_admin_readonly_fields(self):
        """SupplierAdmin makes supplier_code readonly."""
        admin_instance = site._registry.get(Supplier)
        self.assertIn('supplier_code', admin_instance.readonly_fields)

    def test_related_admins_support_supplier_code_search(self):
        """ProductAdmin, PurchaseOrderAdmin, and PurchaseInvoiceAdmin support searching by supplier code."""
        prod_admin = site._registry.get(Product)
        self.assertIn('supplier__supplier_code', prod_admin.search_fields)

        po_admin = site._registry.get(PurchaseOrder)
        self.assertIn('supplier__supplier_code', po_admin.search_fields)

        pi_admin = site._registry.get(PurchaseInvoice)
        self.assertIn('supplier__supplier_code', pi_admin.search_fields)

