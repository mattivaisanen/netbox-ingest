from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.APIRootView = views.IngestRootView
router.register('sources', views.SourceViewSet)
router.register('mappings', views.MappingViewSet)
router.register('fields', views.MappingFieldViewSet)
router.register('runs', views.RunViewSet)

urlpatterns = router.urls
