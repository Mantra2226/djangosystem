from django import forms
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
                self.fields['parent_work_order'].required = True
                self.fields['parent_work_order'].label = "Source Bulk Batch (Parent WO)"
            if 'quantity_produced' in self.fields:
                self.fields['quantity_produced'].label = "Target Pack Count (Units/Tins)"
                self.fields['quantity_produced'].help_text = "Total discrete containers to fill."
        else:
            if 'parent_work_order' in self.fields:
                self.fields['parent_work_order'].required = False
            if 'quantity_produced' in self.fields:
                self.fields['quantity_produced'].label = "Bulk Yield Target (kg/L)"
                self.fields['quantity_produced'].help_text = "Total bulk weight/volume to mix."
