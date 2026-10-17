from django.urls import path

from . import dashboard_views
from . import offer_check


app_name = "dashboard"

urlpatterns = [
    path("", dashboard_views.home, name="home"),
    path("login/", dashboard_views.dashboard_login, name="login"),
    path("logout/", dashboard_views.dashboard_logout, name="logout"),
    path("modems/", dashboard_views.modems, name="modems"),
    path("modems/scan/", dashboard_views.scan_modems, name="scan_modems"),
    path("modems/connect/", dashboard_views.connect_modem, name="connect_modem"),
    path(
        "modems/<int:profile_id>/verify/",
        dashboard_views.verify_modem,
        name="verify_modem",
    ),
    path("topup/", dashboard_views.topup, name="topup"),
    path("transactions/", dashboard_views.transactions, name="transactions"),
    path("report/", dashboard_views.transaction_report, name="report"),
    path("offers/", offer_check.offer_check_page, name="offers"),
    path("offers/buy/", offer_check.offer_buy, name="offer_buy"),
    path(
        "transactions/<uuid:transaction_id>/",
        dashboard_views.transaction_detail,
        name="transaction_detail",
    ),
   path("messages/", dashboard_views.modem_messages, name="messages"),
    path(
        "messages/fetch-all/",
        dashboard_views.fetch_all_modem_messages,
        name="fetch_all_messages",
    ),
    path(
        "messages/sync/",
        dashboard_views.sync_modem_messages,
        name="sync_messages",
    ),
    path(
        "messages/<int:profile_id>/clear/",
        dashboard_views.clear_modem_messages,
        name="clear_messages",
    ),
    path("api-docs/", dashboard_views.api_docs, name="api_docs"),
]
