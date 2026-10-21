#!/bin/sh
set -eu

python -m euvieouvi.bootstrap
flask --app euvieouvi.wsgi db upgrade
python -m euvieouvi.security_migrate
python -m euvieouvi.sync.reconcile
exec "$@"
