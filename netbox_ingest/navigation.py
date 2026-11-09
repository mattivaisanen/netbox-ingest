"""One entry under NetBox's Plugins menu: no new top-level menu."""
from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenuButton, PluginMenuItem

menu_items = (
    PluginMenuItem(
        link='plugins:netbox_ingest:source_list',
        link_text=_('Ingest Sources'),
        permissions=['netbox_ingest.view_source'],
        buttons=(
            PluginMenuButton(
                link='plugins:netbox_ingest:source_add', title=_('Add'), icon_class='mdi mdi-plus-thick',
                permissions=['netbox_ingest.add_source'],
            ),
        ),
    ),
    PluginMenuItem(
        link='plugins:netbox_ingest:mapping_list',
        link_text=_('Ingest Mappings'),
        permissions=['netbox_ingest.view_mapping'],
    ),
    PluginMenuItem(
        link='plugins:netbox_ingest:run_list',
        link_text=_('Ingest Runs'),
        permissions=['netbox_ingest.view_run'],
    ),
)
