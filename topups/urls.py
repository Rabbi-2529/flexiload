from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import offer_check
from .views import (
    LoginView,
    LogoutView,
    MeView,
    SimProfileViewSet,
    TopupTransactionViewSet,
)


router = DefaultRouter()
router.register("sim-profiles", SimProfileViewSet, basename="sim-profile")
router.register("transactions", TopupTransactionViewSet, basename="transaction")

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("offers/check/", offer_check.offer_check_api, name="offer-check"),
    path("", include(router.urls)),
]
