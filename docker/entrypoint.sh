#!/bin/sh
set -eu

python -m euvieouvi.bootstrap
exec "$@"

