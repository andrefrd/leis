#!/bin/bash
# Commita os .md novos de um host SAPL concluído: stage → auditorias → um commit por município.
#   scripts/coleta_municipal/commitar_host.sh sapl.contagem.mg.leg.br
set -euo pipefail
host=$1
AQUI=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(cd "$AQUI/../.." && pwd)}
LOGS=${LOGS:-$REPO/../leis-coleta-logs}
BRANCH=${BRANCH:-feat/municipais-sapl}
cd "$REPO"
[ "$(git branch --show-current)" = "$BRANCH" ] || { echo "$host: branch atual não é $BRANCH"; exit 1; }
# Índice precisa estar limpo: senão o commit misturaria municípios.
git diff --cached --quiet || { echo "$host: há arquivos staged; limpe com git reset"; exit 1; }

git ls-files --others --exclude-standard -z municipais -- '*.md' \
  | xargs -0 -r grep -lZ -F "$host" -- > "$LOGS/$host.arquivos" || true
n=$(tr -cd '\0' < "$LOGS/$host.arquivos" | wc -c)
[ "$n" -gt 0 ] || { echo "$host: nada novo"; touch "$LOGS/$host.commitado"; exit 0; }

# Uma chamada só: cada git add regrava o índice inteiro (1,1 milhão de arquivos).
git add --pathspec-from-file="$LOGS/$host.arquivos" --pathspec-file-nul
python3 -I scripts/auditar.py --staged > "$LOGS/$host.auditar.txt" 2>&1 \
  || { echo "$host: auditar.py falhou (ver $host.auditar.txt)"; git reset -q; exit 2; }
python3 -I scripts/auditar_municipal.py --staged --saida "$LOGS/$host.aud-mun.json" > /dev/null 2>&1 \
  || { echo "$host: auditar_municipal falhou (ver $host.aud-mun.json)"; git reset -q; exit 3; }

primeiro=$(tr '\0' '\n' < "$LOGS/$host.arquivos" | head -1)
pasta=$(echo "$primeiro" | cut -d/ -f1-3)
uf=$(echo "$pasta" | cut -d/ -f2)
municipio=$(sed -n 's/^municipio: *//p' "$primeiro" | tr -d '"')
falhas=$(grep -c FALHA "$LOGS/$host.log" || true)
porcat=$(tr '\0' '\n' < "$LOGS/$host.arquivos" | cut -d/ -f4 | sort | uniq -c | sort -rn | awk '{printf "- %s: %s\n", $2, $1}')

git commit -q -m "feat: acrescenta $n normas de $municipio/$uf via SAPL

Fonte: https://$host (SAPL da Câmara Municipal), coletadas com
scripts/baixar_organicas_sapl.py --descobrir $host --modo legislacao --rps 1.

$porcat

auditar.py --staged e auditar_municipal.py --staged sem erros.
Retidas para revisão (não gravadas): $falhas registros em falhas.jsonl
(sem texto integral, sem artigo reconhecível, granularidade).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
touch "$LOGS/$host.commitado"
echo "$host: $n normas commitadas em $pasta"
