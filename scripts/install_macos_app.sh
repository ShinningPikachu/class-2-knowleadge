#!/bin/bash
set -euo pipefail

open_after_install=1
if [[ "${1:-}" == "--no-open" ]]; then
  open_after_install=0
  shift
fi
if [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--no-open]" >&2
  exit 2
fi

project_root="$(cd "$(dirname "$0")/.." && pwd)"
source_app="$project_root/macos/Build/Class Knowledge.app"
install_directory="${CLASS_KNOWLEDGE_INSTALL_DIR:-${HOME}/Applications}"
installed_app="$install_directory/Class Knowledge.app"
staging_app="$install_directory/.Class Knowledge.app.installing.$$"
backup_app="$install_directory/.Class Knowledge.app.previous.$$"
install_completed=0

cleanup() {
  rm -rf "$staging_app"
  if [[ -d "$backup_app" ]]; then
    rm -rf "$installed_app"
    mv "$backup_app" "$installed_app"
  elif [[ "$install_completed" -eq 0 ]]; then
    rm -rf "$installed_app"
  fi
}

"$project_root/scripts/build_macos_app.sh"
mkdir -p "$install_directory"
if [[ -e "$staging_app" || -e "$backup_app" ]]; then
  echo "An earlier installation attempt left a staging or backup bundle in $install_directory." >&2
  echo "Move those .Class Knowledge.app.* bundles aside, then run this installer again." >&2
  exit 1
fi
trap cleanup EXIT

/usr/bin/ditto --norsrc --noextattr --noqtn "$source_app" "$staging_app"
/usr/bin/codesign --verify --deep --strict "$staging_app"

if [[ -e "$installed_app" ]]; then
  mv "$installed_app" "$backup_app"
fi
mv "$staging_app" "$installed_app"
/usr/bin/codesign --verify --deep --strict "$installed_app"
install_completed=1
rm -rf "$backup_app"
trap - EXIT

if [[ "$open_after_install" -eq 1 ]]; then
  open -a "$installed_app"
  echo "Installed and opened $installed_app"
else
  echo "Installed $installed_app"
fi
