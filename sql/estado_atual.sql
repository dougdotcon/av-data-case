-- Estado atual de cada pedido, reconstruído a partir do log de eventos (SQLite >= 3.25).
-- Tabela de entrada: origem_raw (as 6 colunas do CSV, todas como texto, na ordem do arquivo).
--
-- Regras (as mesmas do código Python; os dois caminhos são comparados a cada execução):
--   1. normaliza texto (TRIM / UPPER) e converte o timestamp para UTC (strftime aplica o offset);
--   2. remove eventos duplicados EXATOS (mesmas 6 colunas), mantendo a 1a ocorrência;
--   3. valor_total em branco = "não informado": herda o último valor conhecido do pedido;
--   4. estado atual = evento de maior timestamp; empate -> o último que apareceu no arquivo;
--   5. pedido existe se o último evento não é exclusão (D).
WITH base AS (
    SELECT rowid                                         AS ordem,
           TRIM(id_pedido)                               AS id_pedido,
           TRIM(id_cliente)                              AS id_cliente,
           UPPER(TRIM(status))                           AS status,
           CASE WHEN TRIM(valor_total) = '' THEN NULL
                ELSE CAST(ROUND(CAST(TRIM(valor_total) AS REAL) * 100) AS INTEGER) END AS valor_cent,
           strftime('%Y-%m-%d %H:%M:%S', TRIM(atualizado_em)) AS ts_utc,
           UPPER(TRIM(operacao))                         AS operacao
    FROM origem_raw
),
dedup AS (
    SELECT *
    FROM (SELECT *,
                 ROW_NUMBER() OVER (PARTITION BY id_pedido, id_cliente, status, valor_cent, ts_utc, operacao
                                    ORDER BY ordem) AS rn
          FROM base)
    WHERE rn = 1
),
com_valor AS (
    SELECT d.*,
           (SELECT x.valor_cent
              FROM dedup x
             WHERE x.id_pedido = d.id_pedido
               AND x.valor_cent IS NOT NULL
               AND (x.ts_utc < d.ts_utc OR (x.ts_utc = d.ts_utc AND x.ordem <= d.ordem))
             ORDER BY x.ts_utc DESC, x.ordem DESC
             LIMIT 1) AS valor_efetivo
    FROM dedup d
),
ranqueado AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY id_pedido ORDER BY ts_utc DESC, ordem DESC) AS rk
    FROM com_valor
)
SELECT id_pedido,
       id_cliente,
       status,
       valor_efetivo AS valor_cent,
       ts_utc,
       operacao,
       CASE WHEN operacao <> 'D' THEN 1 ELSE 0 END AS existe
FROM ranqueado
WHERE rk = 1
ORDER BY id_pedido;
