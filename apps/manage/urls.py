from django.urls import path

from apps.assistant import history_views, oauth_views
from apps.assistant import views as assistant_views
from apps.tools import views as tools_views

from . import (
    area_views,
    block_views,
    faq_views,
    import_views,
    inquiry_views,
    menu_views,
    service_views,
    stats_views,
    views,
)

app_name = "manage"

urlpatterns = [
    # Hemsidekollen - i /manage/ under testfasen, blir publik senare.
    path("verktyg/hemsidekollen/", tools_views.hemsidekollen, name="hemsidekollen"),
    path(
        "verktyg/hemsidekollen/<int:pk>/",
        tools_views.hemsidekollen_report,
        name="hemsidekollen_report",
    ),
    path("", views.dashboard, name="dashboard"),
    # Versionshistorik (apps/assistant)
    path(
        "historik/<slug:app_label>/<slug:model_name>/<int:pk>/",
        history_views.object_history,
        name="history",
    ),
    path(
        "historik/version/<int:version_id>/aterstall/",
        history_views.revert_version,
        name="history_revert",
    ),
    # AI-redaktören: granska utkast och hantera anslutningen
    path("ai/", assistant_views.chat, name="assistant_chat"),
    path("ai/samtal/<int:pk>/", assistant_views.chat, name="assistant_chat_job"),
    path("ai/skicka/", assistant_views.chat_send, name="assistant_chat_send"),
    path("ai/sok/", assistant_views.mention_search, name="assistant_mention_search"),
    path("ai/samtal/<int:pk>/skicka/", assistant_views.chat_send, name="assistant_chat_send_job"),
    path("ai/samtal/<int:pk>/status/", assistant_views.chat_poll, name="assistant_chat_poll"),
    path("ai/utkast/", assistant_views.job_list, name="assistant_jobs"),
    path("ai/jobb/<int:pk>/", assistant_views.job_detail, name="assistant_job"),
    path("ai/jobb/<int:pk>/bulk/", assistant_views.job_bulk, name="assistant_job_bulk"),
    path("ai/jobb/<int:pk>/angra/", assistant_views.job_undo, name="assistant_job_undo"),
    path("ai/jobb/<int:pk>/radera/", assistant_views.job_delete, name="assistant_job_delete"),
    path(
        "ai/utkast/<int:pk>/beslut/",
        assistant_views.change_decide,
        name="assistant_change_decide",
    ),
    path(
        "ai/utkast/<int:pk>/forhandsgranska/",
        assistant_views.change_preview,
        name="assistant_change_preview",
    ),
    path(
        "ai/utkast/<int:pk>/forhandsgranska/sida/",
        assistant_views.change_preview_frame,
        name="assistant_change_preview_frame",
    ),
    path("ai/skrivguide/", assistant_views.style_guide, name="assistant_style_guide"),
    path("ai/koppling/", assistant_views.connection, name="assistant_connection"),
    path("ai/godkann-app/", oauth_views.consent, name="oauth_consent"),
    path(
        "ai/appar/<int:pk>/koppla-bort/",
        assistant_views.oauth_disconnect,
        name="assistant_oauth_disconnect",
    ),
    path(
        "ai/appar/koppla-bort-alla/",
        assistant_views.oauth_disconnect_all,
        name="assistant_oauth_disconnect_all",
    ),
    path("ai/koppling/ny/", assistant_views.token_create, name="assistant_token_create"),
    path(
        "ai/koppling/<int:pk>/aterkalla/",
        assistant_views.token_revoke,
        name="assistant_token_revoke",
    ),
    # Visitor statistics
    path("statistik/", stats_views.stats, name="stats"),
    # Block pages (block editor)
    path("pages/", block_views.page_list, name="page_list"),
    path("pages/new/", block_views.page_form, name="page_new"),
    path("pages/<int:pk>/", block_views.page_detail, name="page_detail"),
    path("pages/<int:pk>/edit/", block_views.page_form, name="page_edit"),
    path("pages/<int:pk>/delete/", block_views.page_delete, name="page_delete"),
    path("pages/<int:pk>/blocks/add/", block_views.block_add, name="block_add"),
    path("blocks/<int:pk>/", block_views.block_edit, name="block_edit"),
    path("blocks/<int:pk>/toggle/", block_views.block_toggle, name="block_toggle"),
    path("blocks/<int:pk>/move/", block_views.block_move, name="block_move"),
    path("blocks/<int:pk>/delete/", block_views.block_delete, name="block_delete"),
    # Media library
    path("media/", views.media_library, name="media_library"),
    path("media/upload/", views.media_upload, name="media_upload"),
    path("media/<int:pk>/update/", views.media_update, name="media_update"),
    path("media/<int:pk>/delete/", views.media_delete, name="media_delete"),
    path("media/<int:pk>/optimize/", views.media_optimize, name="media_optimize"),
    path("media/<int:pk>/restore/", views.media_restore, name="media_restore"),
    # Menus (header + footer)
    path("lankar/", views.link_report, name="link_report"),
    path("lankar/val/", views.link_options, name="link_options"),
    path("lankar/kontrollera/", views.link_check, name="link_check"),
    path("menus/", menu_views.menus_overview, name="menus_overview"),
    path("menus/footer/columns/new/", menu_views.footer_column_form, name="footer_column_new"),
    path(
        "menus/footer/columns/<int:pk>/",
        menu_views.footer_column_form,
        name="footer_column_edit",
    ),
    path(
        "menus/footer/columns/<int:pk>/delete/",
        menu_views.footer_column_delete,
        name="footer_column_delete",
    ),
    path("menus/<int:menu_pk>/items/new/", menu_views.item_form, name="menu_item_new"),
    path("menus/items/<int:pk>/", menu_views.item_form, name="menu_item_edit"),
    path("menus/items/<int:pk>/delete/", menu_views.item_delete, name="menu_item_delete"),
    path("menus/items/<int:pk>/toggle/", menu_views.item_toggle, name="menu_item_toggle"),
    path("menus/items/<int:pk>/move/", menu_views.item_move, name="menu_item_move"),
    # Site settings
    path("settings/", views.settings_view, name="settings"),
    # Services
    path("services/", service_views.services_overview, name="services_overview"),
    path("services/categories/new/", service_views.category_form, name="category_new"),
    path("services/categories/<int:pk>/", service_views.category_form, name="category_edit"),
    path(
        "services/categories/<int:pk>/delete/",
        service_views.category_delete,
        name="category_delete",
    ),
    path("services/items/new/", service_views.service_form, name="service_new"),
    path("services/items/<int:pk>/", service_views.service_form, name="service_edit"),
    path("services/items/<int:pk>/delete/", service_views.service_delete, name="service_delete"),
    path("services/audiences/new/", service_views.audience_form, name="audience_new"),
    path("services/audiences/<int:pk>/", service_views.audience_form, name="audience_edit"),
    path(
        "services/audiences/<int:pk>/delete/",
        service_views.audience_delete,
        name="audience_delete",
    ),
    # Serviceområden
    path("serviceomraden/", area_views.areas_overview, name="areas_overview"),
    path("serviceomraden/new/", area_views.area_form, name="area_new"),
    path("serviceomraden/<int:pk>/", area_views.area_form, name="area_edit"),
    path("serviceomraden/<int:pk>/delete/", area_views.area_delete, name="area_delete"),
    path("serviceomraden/<int:pk>/toggle/", area_views.area_toggle, name="area_toggle"),
    # Inquiries
    path("inquiries/", inquiry_views.inquiry_list, name="inquiry_list"),
    path("inquiries/<int:pk>/", inquiry_views.inquiry_detail, name="inquiry_detail"),
    path("inquiries/<int:pk>/status/", inquiry_views.inquiry_status, name="inquiry_status"),
    # FAQ
    path("faq/", faq_views.faq_overview, name="faq_overview"),
    path("faq/new/", faq_views.faq_section_new, name="faq_section_new"),
    path("faq/<slug:slug>/", faq_views.faq_detail, name="faq_detail"),
    path("faq/<slug:slug>/edit/", faq_views.faq_section_edit, name="faq_section_edit"),
    path("faq/<slug:slug>/delete/", faq_views.faq_section_delete, name="faq_section_delete"),
    path("faq/<slug:slug>/items/new/", faq_views.faq_item_new, name="faq_item_new"),
    path("faq/items/<int:pk>/", faq_views.faq_item_edit, name="faq_item_edit"),
    path("faq/items/<int:pk>/delete/", faq_views.faq_item_delete, name="faq_item_delete"),
    path("faq/items/<int:pk>/move/", faq_views.faq_item_move, name="faq_item_move"),
    # Superuser tools
    path("import/", import_views.import_view, name="import_view"),
]
