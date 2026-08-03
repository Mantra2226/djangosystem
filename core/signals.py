from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Inventory, ProcurementOrder, ProductionOrder
from .services import check_and_auto_resume_on_hold_orders

@receiver(post_save, sender=Inventory)
def inventory_stock_updated_signal(sender, instance, **kwargs):
    """Triggers auto-resume evaluation when inventory level changes."""
    if instance.product:
        check_and_auto_resume_on_hold_orders(product=instance.product)

@receiver(post_save, sender=ProcurementOrder)
def procurement_delivered_signal(sender, instance, **kwargs):
    """Triggers auto-resume evaluation when procurement arrives."""
    if instance.status == 'DELIVERED' and instance.product:
        check_and_auto_resume_on_hold_orders(product=instance.product)

@receiver(post_save, sender=ProductionOrder)
def production_order_completed_signal(sender, instance, **kwargs):
    """Triggers auto-resume evaluation when sub-assembly or finished run completes."""
    if instance.status == 'COMPLETED' and instance.product:
        check_and_auto_resume_on_hold_orders(product=instance.product)
