#!/bin/bash
# Coleta SAPL em paralelo: um processo por host da lista, com pausa se o disco ficar abaixo do limite.
# Retomável: hosts com .done são pulados; .md já gravados são pulados pelo próprio coletor.
#
#   scripts/coleta_municipal/coletar_sapl.sh                 # lista padrão (hosts.txt)
#   HOSTS=minha-lista.txt PAR=6 scripts/coleta_municipal/coletar_sapl.sh
set -u
AQUI=$(cd "$(dirname "$0")" && pwd)
export REPO=${REPO:-$(cd "$AQUI/../.." && pwd)}
export LOGS=${LOGS:-$REPO/../leis-coleta-logs}
export MIN_GB=${MIN_GB:-60}
HOSTS=${HOSTS:-$AQUI/hosts.txt}
PAR=${PAR:-8}
mkdir -p "$LOGS"

coletar() {
  host=$1
  [ -f "$LOGS/$host.done" ] && return 0
  while [ "$(df --output=avail -BG "$REPO" | tail -1 | tr -dc 0-9)" -lt "$MIN_GB" ]; do
    echo "$(date -Is) disco baixo, aguardando" >> "$LOGS/$host.log"; sleep 300
  done
  cd "$REPO" || return 1
  echo "$(date -Is) inicio" >> "$LOGS/$host.log"
  python3 -I scripts/baixar_organicas_sapl.py --descobrir "$host" --modo legislacao --rps 1 >> "$LOGS/$host.log" 2>&1
  rc=$?
  echo "$(date -Is) fim rc=$rc" >> "$LOGS/$host.log"
  # rc=1 também é conclusão normal quando houve normas retidas: vale a linha de resumo "IBGE ... novos".
  if [ $rc -eq 0 ] || awk '/ inicio$/{s=""} /IBGE .* novos/{s=1} END{exit !s}' "$LOGS/$host.log"; then
    rm -f "$LOGS/$host.erro"; touch "$LOGS/$host.done"
  else
    touch "$LOGS/$host.erro"
  fi
}
export -f coletar
xargs -a "$HOSTS" -P "$PAR" -I{} bash -c 'coletar {}'
echo "$(date -Is) coleta concluída" >> "$LOGS/_coleta.log"
