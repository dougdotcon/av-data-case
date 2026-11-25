# Reconciliação de pedidos — case de Engenharia de Dados (AV DATA)

Reconstrói o estado atual de cada pedido a partir do log de eventos (`data/pedidos_origem.csv`), compara com a tabela analítica
(`data/pedidos_destino.csv`) e gera o relatório de divergências.

## Como rodar

```bash
pip install -r requirements.txt
python src/reconcilia.py          # ~1 s; mostra barra de progresso e grava os arquivos abaixo na raiz
python -m pytest -q               # 20 testes
```

Opções: `--origem`, `--destino`, `--saida`, `--workers N` (processos para volumes grandes), `--tolerancia-centavos 5`,
`--sem-verificacao-sql`.

## Entregáveis

| Arquivo | Conteúdo |
|---|---|
| `relatorio_divergencias.csv` | uma linha por (pedido, campo) divergente, com tipo, valores, severidade, confiança e causa provável |
| `RESUMO.md` | meia página para o gerente comercial |
| `DECISOES.md` | decisões, casos sem resposta única, premissas, limitações, uso de IA |
| `observacoes_nao_divergencias.csv` | casos que parecem problema mas não são (e por quê) |
| `transicoes_de_status_suspeitas.csv` | status que "volta" de CANCELADO/ENTREGUE (informativo) |
| `impacto_financeiro.csv`, `metricas.json` | decomposição do impacto em R$ e métricas da execução |

## Estrutura

```
src/reconcilia.py        pipeline (carga → estado atual → verificação em SQL → comparação → relatórios)
sql/estado_atual.sql     2ª implementação, independente, do estado atual (conferida a cada execução)
tests/test_reconcilia.py regras de negócio e números dos dados reais
docs/                    enunciado original
```
