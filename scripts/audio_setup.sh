#!/usr/bin/env bash
# Manual debug helper for the virtual audio devices (SPEC §9.1 / Phase 1).
# Usage: scripts/audio_setup.sh [create|remove|status]
set -euo pipefail

mod_id_for() { # $1 = grep pattern over `pactl list short modules`
  pactl list short modules | awk -v pat="$1" '$0 ~ pat {print $1}'
}

do_create() {
  if ! pactl list short sinks | grep -q $'^[^\\t]*\\tphathom_speaker\\t'; then
    pactl load-module module-null-sink sink_name=phathom_speaker sink_properties=device.description=Phathom_Speaker
  else echo "phathom_speaker exists, reusing"; fi
  if ! pactl list short sinks | grep -q $'^[^\\t]*\\tphathom_voice\\t'; then
    pactl load-module module-null-sink sink_name=phathom_voice sink_properties=device.description=Phathom_Voice
  else echo "phathom_voice exists, reusing"; fi
  if ! pactl list short sources | grep -q $'^[^\\t]*\\tphathom_mic\\t'; then
    pactl load-module module-remap-source master=phathom_voice.monitor source_name=phathom_mic source_properties=device.description=Phathom_Mic
  else echo "phathom_mic exists, reusing"; fi
}

do_remove() {
  # Unload ONLY phathom modules (match sink/source names in the module args).
  ids=$(pactl list short modules | grep -E "phathom_(speaker|voice|mic)" | awk '{print $1}' || true)
  if [ -z "${ids}" ]; then echo "no phathom modules loaded"; return; fi
  # shellcheck disable=SC2086
  echo "${ids}" | while read -r id; do pactl unload-module "${id}" && echo "unloaded ${id}"; done
}

do_status() {
  echo "--- sinks ---"; pactl list short sinks
  echo "--- sources ---"; pactl list short sources
  echo "--- defaults ---"; pactl get-default-sink; pactl get-default-source
}

cmd="${1:-status}"
case "${cmd}" in
  create) do_create ;;
  remove) do_remove ;;
  status) do_status ;;
  *) echo "usage: $0 [create|remove|status]" >&2; exit 1 ;;
esac
