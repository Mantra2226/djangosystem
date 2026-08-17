from django import forms
from django.core.exceptions import ValidationError
from .models import WorkOrder

class WorkOrderForm(forms.ModelForm):
    """
    Custom ModelForm for WorkOrder instances in Django Admin.
    Dynamically adjusts field requirements, labels, and help texts based on
    whether the WorkOrder is classified as PRODUCTION or PACKAGING.
    """
    class Meta:
        model = WorkOrder
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Resolve current category classification
        category = None
        if self.instance and self.instance.pk and self.instance.category:
            category = self.instance.category
        elif self.initial and self.initial.get('category'):
            category = self.initial.get('category')
        elif self.instance and getattr(self.instance, 'product', None):
            if self.instance.product.product_type == 'FINISHED':
                category = 'PACKAGING'
            elif self.instance.product.product_type == 'INTERMEDIATE':
                category = 'PRODUCTION'

        if category == 'PACKAGING':
            if 'parent_work_order' in self.fields:
                self.fields['parent_work_order'].required = False
                self.fields['parent_work_order'].label = "Source Bulk Batch (Parent WO)"
            if 'quantity_produced' in self.fields:
                self.fields['quantity_produced'].label = "Target Pack Count (Units/Tins)"
                self.fields['quantity_produced'].help_text = "Total discrete containers to fill."
            if 'actual_quantity_produced' in self.fields:
                self.fields['actual_quantity_produced'].label = "Actual Quantity Produced (Units/Tins)"
                self.fields['actual_quantity_produced'].help_text = "Actual count of filled containers produced by operator to save to inventory."
        else:
            if 'parent_work_order' in self.fields:
                self.fields['parent_work_order'].required = False
            if 'quantity_produced' in self.fields:
                self.fields['quantity_produced'].label = "Bulk Yield Target (kg/L)"
                self.fields['quantity_produced'].help_text = "Total bulk weight/volume to mix."
            if 'actual_quantity_produced' in self.fields:
                self.fields['actual_quantity_produced'].label = "Actual Quantity Produced (kg/L)"
                self.fields['actual_quantity_produced'].help_text = "Actual bulk weight/volume produced by operator to save to inventory."

    def clean(self):
        cleaned_data = super().clean()
        qty = cleaned_data.get('quantity_produced')
        actual_qty = cleaned_data.get('actual_quantity_produced')
        category = cleaned_data.get('category') or getattr(self.instance, 'category', None)
        product = cleaned_data.get('product') or getattr(self.instance, 'product', None)

        if not category and product:
            if product.product_type == 'INTERMEDIATE':
                category = 'PRODUCTION'
            elif product.product_type == 'FINISHED':
                category = 'PACKAGING'

        if qty is None or qty <= 0:
            field_name = 'quantity_produced'
            label = "Bulk Yield Target (kg/L)" if category == 'PRODUCTION' else "Target Pack Count (Units/Tins)"
            self.add_error(field_name, f"{label} must be a positive number greater than 0.00.")

        if actual_qty is not None and actual_qty < 0:
            self.add_error('actual_quantity_produced', "Actual Quantity Produced cannot be negative.")

        return cleaned_data

    def add_error(self, field, error):
        """
        Safely captures validation errors raised against model fields that are
        not present on the form (e.g. status, target_quantity) and attaches them as non-field errors
        (or maps target_quantity to quantity_produced) to prevent ValueError crashes in Django admin.
        """
        if isinstance(error, ValidationError) and hasattr(error, 'error_dict'):
            for f, error_list in list(error.error_dict.items()):
                if f in self.fields:
                    target_field = f
                elif f == 'target_quantity' and 'quantity_produced' in self.fields:
                    target_field = 'quantity_produced'
                else:
                    target_field = None
                self.add_error(target_field, error_list)
            return

        if field is not None and field not in self.fields:
            if field == 'target_quantity' and 'quantity_produced' in self.fields:
                field = 'quantity_produced'
            else:
                field = None
        if not hasattr(self, 'cleaned_data'):
            self.cleaned_data = {}
        super().add_error(field, error)
