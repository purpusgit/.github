#!/usr/bin/env bash
# Fixture harness. Never executed by the input-validation step —
# that step only validates the PATH. Present because the check requires
# the file to exist in the checked-out tree.
set -euo pipefail
echo "fixture harness"
