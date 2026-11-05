#!/bin/sh
# `mypeople` for the bundled runtime (bundle-runtime.sh installs this as runtime/bin/mypeople).
# boss-supervisor.sh gates the Boss on `mypeople auth-check`; with no `mypeople` on its PATH the gate is open from the first second and a
# Boss is spawned before any login ("Not logged in" as its first turn, kept in its persisted session). Runs the package out of the bundle:
# bin/ (here), python/bin/python3, pylib/mypeople.
D=$(cd "$(dirname "$0")/.." && pwd)
PYTHONPATH="$D/pylib${PYTHONPATH:+:$PYTHONPATH}" exec "$D/python/bin/python3" -c 'import sys; from mypeople.cli import main; sys.exit(main())' "$@"
