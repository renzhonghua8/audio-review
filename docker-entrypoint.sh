#!/bin/sh
set -eu
python app.py --install-model
exec python app.py
