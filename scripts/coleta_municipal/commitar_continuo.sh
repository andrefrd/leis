#!/bin/bash
# Laço: a cada 10 min commita os hosts concluídos (um por vez) e faz push da branch.
# Rode UMA instância só, no mesmo checkout da coleta.
AQUI=$(cd "$(dirname "$0")" && pwd)
export REPO=${REPO:-$(cd "$AQUI/../.." && pwd)}
export LOGS=${LOGS:-$REPO/../leis-coleta-logs}
export BRANCH=${BRANCH:-feat/municipais-sapl}
INTERVALO=${INTERVALO:-600}
cd "$REPO"
while true; do
  for e in "$LOGS"/*.erro; do
    [ -e "$e" ] || continue
    h=$(basename "$e" .erro)
    awk '/ inicio$/{s=""} /IBGE .* novos/{s=1} END{exit !s}' "$LOGS/$h.log" && mv "$e" "$LOGS/$h.done"
  done
  for d in "$LOGS"/*.done; do
    [ -e "$d" ] || continue
    h=$(basename "$d" .done)
    [ -f "$LOGS/$h.commitado" ] && continue
    bash "$AQUI/commitar_host.sh" "$h" 2>&1 | tail -1 | sed "s/^/$(date -Is) /" >> "$LOGS/_commits.log"
  done
  git push -q origin "$BRANCH" >> "$LOGS/_commits.log" 2>&1
  sleep "$INTERVALO"
done
