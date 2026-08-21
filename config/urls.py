"""URL layout.

The version lives in the path — `/api/v1/...` — rather than in a header or a
query parameter. Header versioning reads cleaner on paper and is worse in
practice: the log line does not say which version was called, you cannot try it
from a browser, CDN caching gets confusing, and pinning a mobile client becomes
awkward. The path solves all four.

`/health` and `/ready` sit outside the version prefix on purpose: they are
infrastructure, not part of the contract.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from core.views import health, ready

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health, name="health"),
    path("ready", ready, name="ready"),
    path(
        "api/v1/",
        include(
            [
                path("auth/", include("apps.users.api.v1.auth_urls")),
                path("", include("apps.address.api.v1.urls")),
                path("", include("apps.customers.api.v1.urls")),
            ]
        ),
    ),
]

# The schema names every field and business rule in the system. It is served in
# development for the client generator, and only behind an IP restriction in
# production.
if settings.DEBUG:
    urlpatterns += [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path("docs/", SpectacularRedocView.as_view(url_name="schema"), name="docs"),
    ]
