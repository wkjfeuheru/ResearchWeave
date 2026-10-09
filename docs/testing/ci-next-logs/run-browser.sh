#!/bin/bash
set -eu
docker run --rm --name openharness-ci-browser-e2e-9p1npvvz \
  --security-opt systempaths=unconfined --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  -v /tmp/openharness-ci-next-9p1npvvz/source:/tmp/openharness-ci-next-9p1npvvz/source \
  -v /dev/shm/openharness-ci-next-9p1npvvz-node:/dev/shm/openharness-ci-next-9p1npvvz-node:ro \
  -v /dev/shm/openharness-ci-next-9p1npvvz-browsers:/ms-playwright:ro \
  -v /usr/bin/node:/usr/local/bin/node:ro \
  -v /usr/lib/node_modules/npm:/opt/npm:ro \
  -v /home/jason/.local/share/uv/python/cpython-3.11.17-linux-x86_64-gnu:/home/jason/.local/share/uv/python/cpython-3.11.17-linux-x86_64-gnu:ro \
  -v /home/jason/.local/share/uv/python/cpython-3.11.17-linux-x86_64-gnu:/home/jason/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu:ro \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e PYTHONPATH=/tmp/openharness-ci-next-9p1npvvz/source/src \
  -e OPENHARNESS_TEST_BROWSER=/ms-playwright/chromium-1243/chrome-linux64/chrome \
  -e PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
  -e PATH=/usr/local/bin:/tmp/openharness-ci-next-9p1npvvz/source/tools/sandbox/node_modules/.bin:/usr/bin:/bin \
  -w /tmp/openharness-ci-next-9p1npvvz/source/frontend/web openharness-ci-browser:next-9p1npvvz \
  node /opt/npm/bin/npm-cli.js run test:e2e
