from rest_framework.routers import DefaultRouter

from apps.attachments.api.v1 import views

router = DefaultRouter()
router.register("attachments", views.AttachmentViewSet, basename="attachment")

urlpatterns = router.urls
