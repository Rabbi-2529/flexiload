from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView


urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dashboard:home", permanent=False)),
    path("dashboard/", include("topups.dashboard_urls")),
    path("admin/", admin.site.urls),
    path("api/", include("topups.urls")),
]
