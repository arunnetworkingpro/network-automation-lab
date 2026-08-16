#!/usr/bin/env bash
# Run an Ansible playbook against the fabric, from the jump box.
#
# Why not straight from the Pi: CML's external connector is 'protected' and
# 'snooped'. It drops frames whose source or destination is not the jump's own
# address, so the Pi cannot route through the jump into 10.0.0.0/8 -- a static
# route on the Pi is necessary but not sufficient, and clearing the flags on the
# connector did not lift it either. The jump has direct fabric access, so Ansible
# runs there. That is what a jump box is for.
#
# This syncs ansible/ to the jump, regenerates the inventory in --onjump form, and
# runs the playbook. Idempotent; safe to re-run.
#
#   ./scripts/run_ansible.sh playbooks/validate.yml
#   ./scripts/run_ansible.sh playbooks/validate.yml --limit leaves

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JUMP="$(grep -oP 'external_ip:\s*\K[0-9.]+' "$ROOT/topology/dc-fabric.yml")"
PLAYBOOK="${1:-playbooks/validate.yml}"
shift || true

DEVICE_PASS="$(grep '^LAB_DEVICE_PASS=' "$HOME/.cml.env" | cut -d= -f2-)"
if [ -z "$DEVICE_PASS" ]; then
    echo "LAB_DEVICE_PASS missing from ~/.cml.env -- run scripts/gen_configs.py first" >&2
    exit 1
fi

echo "==> regenerating inventory (--onjump: no ProxyCommand)"
"$ROOT/.venv/bin/python" "$ROOT/scripts/gen_inventory.py" --onjump

echo "==> syncing to ${JUMP}"
tar cz -C "$ROOT" ansible | ssh -o BatchMode=yes "arun@${JUMP}" \
    'rm -rf ~/fabric && mkdir -p ~/fabric && tar xz -C ~/fabric'

echo "==> running ${PLAYBOOK}"
# The password goes over the ssh session as an env var, never onto the jump's disk
# and never into its shell history.
ssh -o BatchMode=yes "arun@${JUMP}" \
    "cd ~/fabric/ansible && export LAB_DEVICE_PASS='${DEVICE_PASS}' \
     && export ANSIBLE_CONFIG=\$PWD/ansible.cfg \
     && ansible-playbook ${PLAYBOOK} $*"
