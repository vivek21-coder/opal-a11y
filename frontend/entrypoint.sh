#!/bin/sh

set -eu

# Replace the placeholder with the actual backend URL in all built JS/HTML files
# On Alpine (BusyBox sed), -i requires no argument
find /usr/share/nginx/html -type f \( -name '*.html' -o -name '*.js' \) \
  -exec sed -i "s|__BACKEND_URL__|${BACKEND_URL:-}|g" {} +

# Start nginx
exec nginx -g 'daemon off;'
