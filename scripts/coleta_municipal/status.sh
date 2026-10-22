#!/bin/bash
# Resumo da coleta: hosts por estado, .md não commitados por município, disco.
AQUI=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(cd "$AQUI/../.." && pwd)}
LOGS=${LOGS:-$REPO/../leis-coleta-logs}
HOSTS=${HOSTS:-$AQUI/hosts.txt}
cd "$LOGS" || exit 1
conta() { ls $1 2>/dev/null | wc -l; }
echo "hosts: $(wc -l < "$HOSTS") | iniciados: $(conta 'sapl.*.log') | concluídos: $(conta '*.done') | commitados: $(conta '*.commitado') | erro: $(conta '*.erro')"
echo "em execução: $(pgrep -fc 'scripts/baixar_organicas_sapl.py')"
echo "--- .md ainda não commitados, por município"
git -C "$REPO" ls-files --others --exclude-standard municipais -- '*.md' | cut -d/ -f2-3 | sort | uniq -c | sort -rn | head -20
echo "--- motivos de retenção"
grep -ho 'FALHA [^:[]*' sapl.*.log 2>/dev/null | sort | uniq -c | sort -rn | head -8
echo "--- últimos commits"; tail -n 5 _commits.log 2>/dev/null
df -h "$REPO" | tail -1
