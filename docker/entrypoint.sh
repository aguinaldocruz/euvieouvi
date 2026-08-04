#!/bin/sh
set -eu

python -m euvieouvi.bootstrap
flask --app euvieouvi.wsgi db upgrade
exec "$@"
