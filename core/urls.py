from django.urls import include, path
from core import views
from . import views
from django.views.generic import RedirectView

urlpatterns = [
    path('', RedirectView.as_view(url='login/')),
    path('login/', views.login, name='login'),
    path('index/', views.index, name='index'),
    path('supplier_form/', views.supplier_form, name='supplier_form'),
    path('customer_form/', views.customer_form, name='customer_form'),  
    path('dispatch_form/', views.dispatch_form, name='dispatch_form'),
    path('finance_entry_form/', views.finance_entry_form, name='finance_entry_form'),
    path('loss_record_form/', views.loss_record_form, name='loss_record_form'),
    path('procurement_form/', views.procurement_form, name='procurement_form'),
    path('product_form/', views.product_form, name='product_form'),
    path('return_form/', views.return_form, name='return_form'),
]   



