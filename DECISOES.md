# DECISOES.md

Case técnico — Engenheiro(a) de Dados (AV DATA, via SELLETO RH). Tudo aqui é reproduzível com `python src/reconcilia.py`;
os números citados vêm de `metricas.json` e de `relatorio_divergencias.csv`.

**Resultado em uma linha:** dos 150 pedidos que aparecem no log, 30 estão divergentes no destino (31 linhas no relatório, porque o
pedido 10050 tem dois problemas), e o faturamento do relatório está R$ 11.544,00 acima do real. Outros 19 pedidos *parecem*
problema, mas não são — por isso não estão no relatório (seção 2).

---

## 1. Decisões tomadas

**D1 — A ordem dos eventos vem do `atualizado_em`, nunca da posição no arquivo.**
230 das 474 linhas têm timestamp anterior ao da linha de cima, então "o último que chegou" daria o estado errado.
Descartei "último a chegar" e "maior status na ordem NOVO→PAGO→ENVIADO→ENTREGUE" (ver C6).

**D2 — Evento repetido com as 6 colunas idênticas é descartado (5 eventos).**
A fila admite entrega duplicada, e um evento idêntico não muda o estado. Já dois eventos *diferentes* no mesmo instante
**não** são duplicata: são conflito (C1).

**D3 — Estado atual = evento mais recente. Se for `D`, o pedido não existe.**
A decisão vale por pedido, olhando só o *último* evento. Isso evita dois falsos positivos clássicos: (a) tratar qualquer `D`
como exclusão definitiva (id reaproveitado, C3) e (b) exigir um evento `I` (4 pedidos só têm `U`, C5).

**D4 — O fuso do destino foi *testado*, não suposto.**
O destino grava horário sem fuso e a origem grava `-03:00`. Testei duas hipóteses contra a origem (mesmo pedido, mesmo
instante): "destino em UTC" casa em 97,8% das linhas; "destino em horário de Brasília" casa em 0%. Conclusão: UTC. O código
repete esse teste a cada execução e aborta se nenhuma hipótese for clara. Sem isso, comparar os textos de `atualizado_em`
marcaria praticamente todos os pedidos como divergentes (diferença de 3 h).

**D5 — Normalização antes de comparar.** `status` com `strip` + maiúsculas (18 das 137 linhas do destino vêm como `" PAGO"`,
`"pago"` etc. — formatação, não divergência). Dinheiro vira centavos inteiros para não depender de ponto flutuante.

**D6 — A divergência é medida em `id_cliente`, `status` e `valor_total`, não em `atualizado_em`.**
Timestamp diferente com conteúdo igual vai para `observacoes_nao_divergencias.csv` (C4). O timestamp só serve para dizer *por
quê* o destino está errado ("desatualizado", "mesmo instante"…) na coluna `causa_provavel`.

**D7 — Taxonomia (a classificação é minha, ver `PRIORIDADE_TIPO` em `src/reconcilia.py`):**

| Tipo | Significado |
|---|---|
| `AUSENTE_NO_DESTINO` | existe na origem, não chegou ao destino |
| `EXCLUIDO_PRESENTE_NO_DESTINO` | foi excluído (`D`) na origem e continua no destino |
| `DUPLICADO_NO_DESTINO` | mais de uma linha para o mesmo pedido |
| `STATUS_DIVERGENTE` / `CLIENTE_DIVERGENTE` | campo diferente |
| `VALOR_DIVERGENTE` | valor diferente acima da tolerância |
| `VALOR_DIVERGENTE_CENTAVOS` | diferença ≤ R$ 0,05 (parametrizável) |
| `VALOR_AUSENTE_NA_ORIGEM` | o log veio com valor em branco e o destino gravou 0,00 |
| `ORFAO_NO_DESTINO` | pedido do destino sem nenhum evento na origem (0 casos, mas a regra existe) |

Uma linha por *(pedido, campo)*. Cada linha tem `severidade`, `confianca` e `causa_provavel`. Pedido com dois campos errados
gera duas linhas, mas conta uma vez no total de pedidos divergentes.

**D8 — Destino com várias linhas para o mesmo pedido (4 pedidos).**
Reporto `DUPLICADO_NO_DESTINO` (1 linha por pedido) e uso como referência para os outros campos a linha que bate com a origem.
Assim o pedido 10032, que tem uma linha `NOVO` velha e uma `CANCELADO` correta, não vira também `STATUS_DIVERGENTE`.

**D9 — Faturamento = soma de `valor_total` dos pedidos que existem, incluindo cancelados.**
O enunciado não define. Reporto também a visão sem cancelados (R$ 237.962,07 real × R$ 239.613,06 no relatório, +R$ 1.650,99).

**D10 — Duas implementações do estado atual.** Python (pandas) e SQL (`sql/estado_atual.sql`, SQLite). A cada execução o
programa compara as duas (id, existência, cliente, status, valor) e aborta se divergirem. Se uma regra estiver errada, ela
costuma estar errada nas duas do mesmo jeito — então isso pega *erro de implementação*, não *erro de decisão*.

**D11 — O impacto financeiro fecha.** `diferença total = Σ impacto por tipo` (R$ 11.544,00); o código falha se houver
diferença de valor sem divergência que a explique.

---

## 2. Casos sem resposta única

Para cada um: o que decidi, o que descartei, e quanto o número final muda se a decisão estiver errada. Base = 30 pedidos.

**C1 — Empate de timestamp com conteúdo diferente (pedidos 10054, 10105, 10149).**
Dois eventos do mesmo pedido no mesmo minuto, um dizendo `PAGO 4096,05` e outro `ENVIADO 3792,64` (e análogos). O arquivo não diz
qual veio depois. *Decisão:* se o destino coincide com um dos candidatos, não reporto divergência e listo como
`AMBIGUO_EMPATE_DE_TIMESTAMP`; se não coincidisse com nenhum, reportaria com `confianca = BAIXA`. *Fragilidade assumida:* é
raciocínio parcialmente circular (usei o destino para desempatar). Nos três casos o destino carrega o candidato de **maior
valor**; se o outro fosse o correto, o relatório estaria **R$ 1.063,73 acima**. *Descartado:* escolher pela ordem do arquivo
(arbitrário: marcaria 2 falsos positivos) e pela "ordem natural" do status (ver C6). *Sensibilidade:* +3 pedidos (33).

**C2 — Valor em branco no log (pedidos 10064, 10098, 10119) e 0,00 no destino.**
Branco no log é "não informado", não "zero". *Decisão:* o valor efetivo herda o último valor conhecido do pedido
(R$ 1.195,80 / 2.033,93 / 2.606,28) e o destino zerado é divergência, com `confianca = MEDIA` porque o valor real é desconhecido.
*Descartado:* tratar como 0,00 (concordaria com o destino e esconderia R$ 5.836,01) e apagar o evento (perderia o `status` que veio
nele). *Sensibilidade:* se o valor realmente fosse 0,00, são −3 pedidos (27) e a diferença do faturamento sobe para R$ 17.380,01.
Quem resolve isso é o dono do sistema de origem, não os dados.

**C3 — `id_pedido` reaproveitado depois de exclusão (10011, 10029, 10126).**
Há `D` e, semanas depois, um novo `I` com outro cliente e outro valor. É um pedido novo com o mesmo id ou um erro?
*Decisão:* vale o último evento; o destino está coerente com ele. *Descartado:* "D é definitivo" (+3 falsos positivos).
Observação: o número "150 pedidos" conta *ids*; se cada reuso for um pedido distinto, são 153.

**C4 — Timestamp do destino diferente com conteúdo igual (10023, 10053, 10060, 10129).**
Em 10023 o destino parou numa atualização anterior (que repetia os mesmos dados). Em 10053, 10060 e 10129 o destino é de 12 a 37 dias
**posterior** ao último evento do pedido no log (o log vai até 19/07). Ou o log perdeu eventos, ou houve reprocessamento no destino.
*Decisão:* cliente, status e valor batem, então não é divergência de negócio; ficam como observação. *Sensibilidade:* +4 (34).

**C5 — Diferenças de centavos (10020, 10031, 10045, 10141).**
R$ 0,01 a R$ 0,03 para mais ou para menos, sem padrão, em eventos de criação e de atualização. Pode ser arredondamento em
alguma das cargas ou pode ser dado alterado. *Decisão:* reporto, mas num tipo à parte com `severidade = BAIXA`, para o negócio poder
ignorar. Impacto: −R$ 0,02. *Sensibilidade:* −4 (26) se o tipo for retirado do relatório.

**C6 — Status que "volta" de um estado final (56 pedidos).**
`CANCELADO → PAGO`, `ENTREGUE → ENVIADO`… não faz sentido em um fluxo normal. *Decisão:* não impus máquina de estados; confio no
timestamp. Evidência a favor: em 26 pedidos o *último* evento é desse tipo, e em 23 dos 24 que estão no destino o destino
concorda com o timestamp (o destino é um sistema independente). Fica em `transicoes_de_status_suspeitas.csv` para o negócio
decidir se é erro de cadastro. Se existisse uma regra "estado final não muda", o conteúdo "correto" desses pedidos mudaria.

**C7 — Cliente trocado dentro do histórico (10004, 10027, 10114, 10128)** e **cliente diferente no destino (10001, 10003, 10140).**
Troca de cliente no log é tratada como atualização normal; vale a do último evento (o destino concorda nos 4). Nos 3 pedidos em
que o destino tem *outro* cliente, o horário é o mesmo da origem, então não é atraso: é dado alterado na carga.

**C8 — Pedido sem evento de criação (10013, 10059, 10081, 10103).**
A captura começou depois do pedido. O estado vem do primeiro evento visto; os 4 coincidem com o destino. Não é divergência.
O histórico inicial deles é desconhecido, e isso não afeta o estado atual.

---

## 3. Premissas

- O log é a fonte da verdade do que *deveria* estar no destino; o destino é o que se audita.
- `id_pedido` é a identidade do pedido; `D` carrega o último estado do pedido e é seguido, no máximo, de um novo `I`.
- O destino foi carregado depois do último evento do log (ele tem horários até 21/07; o log, até 19/07). Por isso "destino desatualizado"
  não é atraso de carga: os eventos que as 6 linhas desatualizadas não refletem aconteceram de 9 a 50 dias antes do último evento do log.
- Moeda única, duas casas decimais.
- O enunciado não diz como `atualizado_em` do destino é gravado (D4) nem como medir faturamento (D9); assumi o que os dados provam.

## 4. Limitações

- **Escala.** Medi com os dados replicados 1000× (474 mil eventos, 150 mil pedidos): carga 4,9 s; reconstrução 4,2 s em 1 processo e
  2,8 s com 2 (só 2 núcleos nesta máquina; a divisão é por hash do `id_pedido`, que não muda o resultado); **comparação 45,6 s**.
  O gargalo é a comparação, que ainda tem laços em Python sobre todos os pedidos. Acima disso o caminho é mover a comparação para
  SQL (anti-joins) em DuckDB/BigQuery/Spark e processar de forma incremental (CDC) em vez de reler o histórico inteiro.
- Tudo cabe em memória. Sem controle de memória para históricos de GB.
- O SQL usa subconsulta correlacionada para "último valor conhecido" (SQLite não tem `IGNORE NULLS`); em um warehouse trocaria por
  `LAST_VALUE(... IGNORE NULLS)`.
- Os desempates (C1) dependem do destino; não resolvem o caso em que o destino também está errado.
- Não valido regras de negócio (máquina de estados, valores negativos, clientes inexistentes) nem o schema dos arquivos além do básico.
- A inferência de fuso precisa de volume mínimo de pedidos coincidentes; num arquivo minúsculo ela aborta em vez de chutar.
- Não há como saber, só pelos dados, **por que** cada divergência aconteceu; `causa_provavel` é hipótese a confirmar no código do ETL.

## 5. Uso de IA

Usei **Claude (Anthropic) via Claude Code, modelo Sonnet 5.5**, nas etapas: (1) leitura do enunciado e exploração inicial dos dados;
(2) escrita do código Python/SQL e dos 20 testes; (3) primeira versão destes documentos. As hipóteses foram sempre checadas contra os dados
(o teste de fuso, a evidência de C6, o benchmark de 1000×) e o resultado final foi conferido por duas implementações independentes
(pandas e SQL). Declaro o uso conforme pedido no enunciado e me comprometo a explicar na entrevista qualquer trecho do código.

## 6. Pontos do enunciado/dados que vale apontar

- O enunciado não menciona que o destino não tem fuso, que há `valor_total` em branco (3 eventos) nem que há `D` seguido de `I`.
- O prazo e a entrega pedem repositório Git; o repositório vai compactado (`.zip` com a pasta `.git`) junto com o e-mail.
