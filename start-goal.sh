#!/usr/bin/env bash
# Lance le service goal (nœud « goal » de neron.server.yaml) hors systemd.
#
# Délègue au lanceur unique de neronOS (system/scripts/start-module.sh), qui
# fait la même chose que l'unité neron@goal.service. Racine NéronOS cherchée dans
# cet ordre :
#   1. NERON_ROOT ;
#   2. le neronOS qui contient ce dépôt (server/goal, clone ou lien posé par
#      system/scripts/setup-modules.sh) ;
#   3. un dépôt neronOS voisin (ex. /srv/Neron/neronOS à côté de /srv/Neron/neronGoal) ;
#   4. /etc/neronOS.
# L'ancienne version visait /srv/neron/neronCore et un setup-shim.sh disparus.
set -euo pipefail

# Chemins calculés sur le chemin LOGIQUE (dirname), pas avec « .. » : si ce
# dépôt est un lien sous server/, « .. » serait résolu depuis la cible du lien.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$HERE")"
for candidate in "${NERON_ROOT:-}" "$(dirname "$PARENT")" "$PARENT/neronOS" /etc/neronOS; do
  [ -n "$candidate" ] || continue
  if [ -x "$candidate/system/scripts/start-module.sh" ]; then
    ROOT="$(cd "$candidate" && pwd)"
    break
  fi
done
[ -n "${ROOT:-}" ] || {
  echo "neronOS introuvable (NERON_ROOT, $HERE/../.., $HERE/../neronOS, /etc/neronOS)" >&2
  exit 1
}

NERON_ROOT="$ROOT" exec "$ROOT/system/scripts/start-module.sh" goal
