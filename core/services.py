from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError
from .models import Product, BillOfMaterial, BOMItem

def explode_material_requirements(product, target_quantity=Decimal('1.0000'), visited=None):
    """
    Recursively flattens a multi-level Bill of Materials down to its 
    foundational raw materials and unconfigured sub-assemblies.
    """
    if visited is None:
        visited = set()
    
    # Emergency Loop Break: Safely halts if a circular loop slips through admin validation
    if product.pk in visited:
        raise ValidationError(
            f"Infinite Loop Recurrence Error: Circular dependency detected at '{product.name}'."
        )
    
    requirements = defaultdict(Decimal)
    
    # Grab the active recipe for this current item depth level
    active_bom = product.boms.filter(is_active=True).first()
    
    # Base Case: If it's a RAW item or has no active recipe, it's a foundational material
    if not active_bom or product.product_type == 'RAW':
        requirements[product] += Decimal(target_quantity)
        return requirements
    
    visited.add(product.pk)
    
    # Recursive Step: Dig through nested blueprints
    for item in active_bom.items.select_related('component'):
        sub_component = item.component
        extended_quantity = item.quantity_required * Decimal(target_quantity)
        
        # Keep digging down the branch
        sub_requirements = explode_material_requirements(
            product=sub_component, 
            target_quantity=extended_quantity, 
            visited=visited.copy()
        )
        
        # Roll the child totals back up into our local accumulator
        for component_product, accumulated_qty in sub_requirements.items():
            requirements[component_product] += accumulated_qty
            
    return requirements