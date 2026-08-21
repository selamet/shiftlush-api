from django.urls import path

from apps.companies.api.v1 import views

urlpatterns = [
    # Singular, and no id: the only company a request can address is its own.
    path("company", views.CompanyView.as_view(), name="company"),
]
