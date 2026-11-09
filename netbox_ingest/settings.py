"""Plugin settings: PLUGINS_CONFIG['netbox_ingest'], with defaults from
IngestConfig.default_settings."""
from netbox.plugins.utils import get_plugin_config

PLUGIN = 'netbox_ingest'


def get_setting(key):
    return get_plugin_config(PLUGIN, key)
