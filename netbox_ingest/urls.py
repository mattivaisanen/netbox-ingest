from django.urls import include, path

from utilities.urls import get_model_urls

from . import views  # noqa: F401 - registers the model views

APP = 'netbox_ingest'

urlpatterns = [
    path('sources/', include(get_model_urls(APP, 'source', detail=False))),
    path('sources/<int:pk>/', include(get_model_urls(APP, 'source'))),
    path('mappings/', include(get_model_urls(APP, 'mapping', detail=False))),
    path('mappings/<int:pk>/', include(get_model_urls(APP, 'mapping'))),
    path('fields/', include(get_model_urls(APP, 'mappingfield', detail=False))),
    path('fields/<int:pk>/', include(get_model_urls(APP, 'mappingfield'))),
    path('runs/', include(get_model_urls(APP, 'run', detail=False))),
    path('runs/<int:pk>/', include(get_model_urls(APP, 'run'))),
]
