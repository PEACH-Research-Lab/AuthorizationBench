#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python3}"

"$PYTHON" -m unittest -q \
  authorization_eva_sandbox.tests.test_artifact_gate \
  authorization_eva_sandbox.tests.test_canonical_vignette \
  authorization_eva_sandbox.tests.test_expansion_consistency \
  authorization_eva_sandbox.tests.test_grounding_checks \
  authorization_eva_sandbox.tests.test_service_base_contracts \
  authorization_eva_sandbox.tests.test_stage3_shortlist \
  authorization_eva_sandbox.tests.test_trigger_flip \
  authorization_eva_sandbox.tests.test_world_verification
