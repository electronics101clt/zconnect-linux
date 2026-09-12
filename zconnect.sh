#!/bin/bash
# Z Connect launcher
cd "$(dirname "$(readlink -f "$0")")"
exec python3 zconnect.py "$@"
