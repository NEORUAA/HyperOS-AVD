"""Embed the same offline, read-only KernelSU status view in both OS4 modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAMILIES = {'core': 'hyperos_avd_native_compat', 'apps': 'hyperos_avd_app_compat'}


def files(family):
    if family not in FAMILIES:
        raise RuntimeError('Unknown compatibility WebUI family.')
    template = ROOT / 'modules/compat-webui'
    result = {}
    for name in ('index.html', 'app.js', 'style.css', 'status.sh'):
        path = template / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Missing offline module status asset: ' + name)
        body = path.read_bytes().replace(b'@MODULE_ID@', FAMILIES[family].encode())
        target = 'webui-status.sh' if name == 'status.sh' else 'webroot/' + name
        result[target] = body
    return result
