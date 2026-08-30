from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from apps.core import views as core_views
from apps.core.sitemap import sitemaps
from apps.inquiries import views as inquiry_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", core_views.healthz, name="healthz"),
    path("favicon.ico", core_views.favicon, name="favicon"),
    path("sitemap.xml", sitemap, {"sitemaps": sitemaps}, name="sitemap"),
    path("robots.txt", core_views.robots_txt, name="robots"),
    # Customer control panel. Auth routes are scoped under /manage/ so the
    # public site keeps the root namespace. login_required redirects here.
    path(
        "manage/login/",
        auth_views.LoginView.as_view(template_name="manage/login.html"),
        name="login",
    ),
    path(
        "manage/logout/",
        auth_views.LogoutView.as_view(next_page="login"),
        name="logout",
    ),
    path("manage/", include("apps.manage.urls")),
    path("forfragan/", include("apps.inquiries.urls")),
    path("nyhetsbrev/", inquiry_views.newsletter_signup, name="newsletter"),
    path("analytics/", include("apps.analytics.urls")),
    path("faq/", include("apps.faq.urls")),
    path("", include("apps.areas.urls")),
    path("", include("apps.services.urls")),
    path("", include("apps.website.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
