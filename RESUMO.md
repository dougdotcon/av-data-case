# Resumo para a área comercial

**Os números do relatório não batem, e o motivo é concreto.** Dos 150 pedidos do sistema, **30 (20%) estão errados no relatório**.
O faturamento do relatório é **R$ 308.232,91**; o real é **R$ 296.688,91**. Ou seja, o relatório mostra **R$ 11.544,00 a mais (3,9%)**.

Esse número parece pequeno, mas esconde erros maiores que se anulam em parte: o relatório erra, somando para cima e para baixo,
cerca de **R$ 45,6 mil**.

| Problema | Pedidos | Efeito no faturamento do relatório |
|---|---|---|
| Pedidos **excluídos** no sistema que continuam aparecendo | 5 | R$ 16.270,76 a mais |
| Pedidos que aparecem **duas vezes** | 4 | R$ 12.166,63 a mais |
| Pedidos que **não aparecem** | 5 | R$ 11.173,64 a menos |
| Pedidos com **valor zerado** (o sistema não informou o valor) | 3 | R$ 5.836,01 a menos* |
| Pedidos com **valor desatualizado** | 3 | R$ 116,28 a mais |
| Diferença de **centavos** | 4 | R$ 0,02 a menos |
| **Status errado** (ex.: aparece "enviado", mas foi cancelado) | 4** | sem efeito no total, mas errado por pedido |
| **Cliente errado** | 3 | sem efeito no total, mas errado por cliente |

\* Estimado pelo último valor conhecido de cada pedido; o valor real precisa ser confirmado no sistema.
\*\* Um desses pedidos (10050) também está com valor desatualizado, por isso a soma da coluna "Pedidos" é 31 e não 30.

**Se o relatório já desconsidera pedidos cancelados**, a diferença cai para R$ 1.650,99 (R$ 239.613,06 contra R$ 237.962,07): os
erros para cima e para baixo quase se anulam, mas continuam errados pedido a pedido.

**O que não é erro:** 19 pedidos parecem estranhos mas estão corretos (por exemplo, número de pedido reutilizado depois de uma exclusão,
ou evento repetido no sistema). Não os contei, para o relatório não acusar problema que não existe. A lista está em
`observacoes_nao_divergencias.csv`.

**O que resolver primeiro: os pedidos excluídos que continuam no relatório.** É o maior valor (R$ 16,3 mil, pedidos que
nem existem mais entrando no faturamento) e a causa é uma só: o processo que alimenta o relatório não aplica as exclusões. Corrigir
isso resolve também todas as exclusões futuras. Em seguida, evitar linhas duplicadas, que é uma correção simples (uma linha por pedido)
e vale R$ 12,2 mil.

**Dependem de decisão do negócio:** (1) o valor real dos 3 pedidos sem valor; (2) 3 pedidos em que o sistema registrou duas versões no
mesmo minuto — se o relatório estiver com a versão errada, ele pode estar até R$ 1.063,73 acima do real.
