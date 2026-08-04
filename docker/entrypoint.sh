#!/bin/sh
set -eu

python -m euvieouvi.bootstrap
flask --app euvieouvi.wsgi db upgrade
python -m euvieouvi.sync.reconcile
exec "$@"
