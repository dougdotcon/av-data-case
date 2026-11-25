#!/usr/bin/env python3
"""Reconciliação entre o log de eventos de pedidos (origem) e a tabela analítica (destino).

Fluxo (ver DECISOES.md para o "porquê" de cada regra):
  1. carrega e normaliza os dois arquivos (texto, caixa, fuso, valores em centavos);
  2. reconstrói o estado atual de cada pedido a partir do log (em blocos, em paralelo se o volume pedir);
  3. confere a reconstrução com uma 2a implementação independente, em SQL (SQLite);
  4. compara estado reconstruído x destino e classifica as divergências;
  5. grava relatorio_divergencias.csv, observacoes_nao_divergencias.csv, impacto_financeiro.csv, metricas.json.

Uso:  python src/reconcilia.py            (usa data/ e grava na raiz do repositório)
      python src/reconcilia.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

RAIZ = Path(__file__).resolve().parent.parent
COLS_EVENTO = ["id_pedido", "id_cliente", "status", "valor_total", "atualizado_em", "operacao"]
COLS_DESTINO = ["id_pedido", "id_cliente", "status", "valor_total", "atualizado_em"]
OPERACOES = {"I", "U", "D"}
STATUS_TERMINAIS = {"CANCELADO", "ENTREGUE"}
TOLERANCIA_CENTAVOS_PADRAO = 5          # diferença de valor <= R$ 0,05 é classificada como "centavos"
MIN_LINHAS_PARALELO = 50_000            # abaixo disso o custo de criar processos é maior que o ganho

# tipos de divergência (a classificação é nossa; ver DECISOES.md)
T_AUSENTE = "AUSENTE_NO_DESTINO"
T_EXCLUIDO = "EXCLUIDO_PRESENTE_NO_DESTINO"
T_DUPLICADO = "DUPLICADO_NO_DESTINO"
T_ORFAO = "ORFAO_NO_DESTINO"
T_STATUS = "STATUS_DIVERGENTE"
T_VALOR = "VALOR_DIVERGENTE"
T_CENTAVOS = "VALOR_DIVERGENTE_CENTAVOS"
T_VALOR_NULO = "VALOR_AUSENTE_NA_ORIGEM"
T_CLIENTE = "CLIENTE_DIVERGENTE"
PRIORIDADE_TIPO = [T_EXCLUIDO, T_AUSENTE, T_ORFAO, T_DUPLICADO, T_VALOR_NULO, T_VALOR, T_CENTAVOS, T_STATUS, T_CLIENTE]
SEVERIDADE = {T_AUSENTE: "ALTA", T_EXCLUIDO: "ALTA", T_DUPLICADO: "ALTA", T_ORFAO: "ALTA", T_VALOR: "ALTA",
              T_VALOR_NULO: "ALTA", T_STATUS: "MEDIA", T_CLIENTE: "MEDIA", T_CENTAVOS: "BAIXA"}


# --------------------------------------------------------------------------- utilidades
def fmt_valor(cent) -> str:
    return "" if cent is None or pd.isna(cent) else f"{int(cent) / 100:.2f}"


def _para_centavos(s: pd.Series) -> pd.Series:
    """Texto -> centavos inteiros (evita erro de ponto flutuante). Vazio vira <NA> ("não informado")."""
    vazio = s.str.strip() == ""
    num = pd.to_numeric(s.where(~vazio, None), errors="coerce")
    invalido = num.isna() & ~vazio
    if invalido.any():
        raise ValueError(f"valor_total não numérico em {int(invalido.sum())} linha(s): {s[invalido].head(3).tolist()}")
    return (num * 100).round().astype("Int64")


def _exigir_colunas(df: pd.DataFrame, cols: list[str], nome: str) -> None:
    faltando = set(cols) - set(df.columns)
    if faltando:
        raise ValueError(f"{nome}: colunas ausentes {sorted(faltando)}")


# --------------------------------------------------------------------------- carga
def carregar_origem(caminho: Path) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    _exigir_colunas(df, COLS_EVENTO, "origem")
    for c in COLS_EVENTO:
        df[c] = df[c].str.strip()
    df["status"] = df["status"].str.upper()
    df["operacao"] = df["operacao"].str.upper()
    if not df["operacao"].isin(OPERACOES).all():
        raise ValueError(f"operacao fora de {OPERACOES}: {df.loc[~df.operacao.isin(OPERACOES), 'operacao'].unique()}")
    if not df["atualizado_em"].str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True).all():
        raise ValueError("origem: há timestamp sem offset de fuso; premissa do código violada")
    df["ts_utc"] = pd.to_datetime(df["atualizado_em"], utc=True)
    df["valor_cent"] = _para_centavos(df["valor_total"])
    df["_ordem"] = np.arange(len(df))          # posição no arquivo: só serve de desempate determinístico
    return df


def carregar_destino(caminho: Path) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    _exigir_colunas(df, COLS_DESTINO, "destino")
    for c in COLS_DESTINO:
        df[c] = df[c].str.strip()
    df["status"] = df["status"].str.upper()
    df["valor_cent"] = _para_centavos(df["valor_total"])
    df["ts_naive"] = pd.to_datetime(df["atualizado_em"])
    df["_ordem"] = np.arange(len(df))
    return df


def inferir_fuso_destino(origem: pd.DataFrame, destino: pd.DataFrame) -> dict:
    """O destino grava timestamp SEM fuso. Em vez de supor, testamos: qual hipótese faz o timestamp do destino
    coincidir com algum evento do mesmo pedido na origem (que tem offset explícito)?"""
    fmt = "%Y-%m-%d %H:%M:%S"
    chaves = set(zip(origem["id_pedido"], origem["ts_utc"].dt.strftime(fmt)))
    hipoteses = {"UTC": 0, "America/Sao_Paulo (UTC-3)": 3}      # horas a somar ao horário do destino para virar UTC
    taxas = {}
    for nome, h in hipoteses.items():
        ts = (destino["ts_naive"] + pd.Timedelta(hours=h)).dt.strftime(fmt)
        taxas[nome] = float(np.mean([(i, t) in chaves for i, t in zip(destino["id_pedido"], ts)]))
    melhor = max(taxas, key=taxas.get)
    outra = [v for k, v in taxas.items() if k != melhor][0]
    if taxas[melhor] < 0.8 or taxas[melhor] - outra < 0.3:
        raise ValueError(f"não foi possível inferir o fuso do destino com segurança: {taxas}")
    return {"fuso": melhor, "horas_para_utc": hipoteses[melhor], "taxas_de_casamento": taxas}


def aplicar_fuso_destino(destino: pd.DataFrame, horas_para_utc: int) -> pd.DataFrame:
    destino = destino.copy()
    destino["ts_utc"] = (destino["ts_naive"] + pd.Timedelta(hours=horas_para_utc)).dt.tz_localize("UTC")
    return destino


# --------------------------------------------------------------------------- reconstrução do estado
def _reconstruir_bloco(ev: pd.DataFrame) -> pd.DataFrame:
    """Reconstrói o estado atual de um conjunto de pedidos. Os pedidos são independentes entre si, então
    qualquer partição por id_pedido pode ser processada em paralelo sem alterar o resultado."""
    n_brutos = ev.groupby("id_pedido").size()
    chave = ["id_pedido", "id_cliente", "status", "valor_cent", "ts_utc", "operacao"]
    ev = ev.drop_duplicates(chave)                                               # (1) evento duplicado exato
    ev = ev.sort_values(["id_pedido", "ts_utc", "_ordem"], kind="stable").reset_index(drop=True)  # (2) ordem do evento
    g = ev.groupby("id_pedido", sort=False)
    ev["valor_efetivo"] = g["valor_cent"].ffill()                                # (3) valor em branco herda o anterior
    ev["ultimo_ts"] = g["ts_utc"].transform("max")
    topo = ev[ev["ts_utc"] == ev["ultimo_ts"]]                                   # eventos no instante mais recente
    vencedor = topo.groupby("id_pedido", sort=False).tail(1).set_index("id_pedido")
    cands = topo.drop_duplicates(["id_pedido", "operacao", "id_cliente", "status", "valor_efetivo"])
    n_cand = cands.groupby("id_pedido").size()
    empates: dict[str, list] = {}
    for r in cands[cands["id_pedido"].map(n_cand) > 1].itertuples():
        empates.setdefault(r.id_pedido, []).append((r.operacao, r.id_cliente, r.status,
                                                    None if pd.isna(r.valor_efetivo) else int(r.valor_efetivo)))
    seq = g["operacao"].agg("".join)
    prev = g["status"].shift()
    regride = prev.isin(STATUS_TERMINAIS) & (ev["status"] != prev) & (ev["operacao"] != "I")
    regressoes = (prev + "→" + ev["status"])[regride].groupby(ev.loc[regride, "id_pedido"]).agg(" | ".join)
    n_dedup = ev.groupby("id_pedido").size()
    est = pd.DataFrame({
        "id_pedido": vencedor.index,
        "existe": (vencedor["operacao"] != "D").to_numpy(),
        "operacao_final": vencedor["operacao"].to_numpy(),
        "id_cliente": vencedor["id_cliente"].to_numpy(),
        "status": vencedor["status"].to_numpy(),
        "valor_cent": vencedor["valor_efetivo"].to_numpy(),
        "valor_ausente_origem": vencedor["valor_cent"].isna().to_numpy(),
        "ts_utc": vencedor["ts_utc"].to_numpy(),
    })
    ids = est["id_pedido"]
    est["n_cand"] = ids.map(n_cand).astype(int).to_numpy()
    est["candidatos"] = ids.map(empates).astype(object).to_numpy()
    est["n_eventos_brutos"] = ids.map(n_brutos).astype(int).to_numpy()
    est["n_eventos_duplicados"] = (est["n_eventos_brutos"] - ids.map(n_dedup).astype(int)).to_numpy()
    s = ids.map(seq)
    est["sem_evento_criacao"] = (~s.str.contains("I")).to_numpy()
    est["recriado_apos_exclusao"] = s.str.contains("D.*I", regex=True).to_numpy()
    est["evento_apos_exclusao"] = s.str.contains("D[^I]*U", regex=True).to_numpy()
    est["regressoes_status"] = ids.map(regressoes).to_numpy()
    est["cliente_mudou"] = (ids.map(g["id_cliente"].nunique()) > 1).to_numpy()
    est["ts_utc"] = pd.to_datetime(est["ts_utc"], utc=True)
    return est.reset_index(drop=True)


def reconstruir_estado(origem: pd.DataFrame, workers: int = 1, min_linhas_paralelo: int = MIN_LINHAS_PARALELO,
                       progresso: bool = True) -> pd.DataFrame:
    if workers <= 1 or len(origem) < min_linhas_paralelo:
        return _reconstruir_bloco(origem).sort_values("id_pedido").reset_index(drop=True)
    n_blocos = workers * 4
    balde = pd.util.hash_pandas_object(origem["id_pedido"], index=False).to_numpy() % n_blocos
    blocos = [g for _, g in origem.groupby(balde)]
    partes = []
    with ProcessPoolExecutor(max_workers=workers) as ex, \
            tqdm(total=len(blocos), desc="Reconstruindo estado", unit="bloco", disable=not progresso) as barra:
        for parte in ex.map(_reconstruir_bloco, blocos):
            partes.append(parte)
            barra.update(1)
    return pd.concat(partes, ignore_index=True).sort_values("id_pedido").reset_index(drop=True)


def estado_via_sql(caminho_origem: Path) -> pd.DataFrame:
    """2a implementação, independente, das mesmas regras (sql/estado_atual.sql). Serve de verificação cruzada."""
    bruto = pd.read_csv(caminho_origem, dtype=str, keep_default_na=False)
    con = sqlite3.connect(":memory:")
    try:
        bruto[COLS_EVENTO].to_sql("origem_raw", con, index=False)
        consulta = (RAIZ / "sql" / "estado_atual.sql").read_text(encoding="utf-8")
        return pd.read_sql_query(consulta, con)
    finally:
        con.close()


def verificar_paridade_sql(estado: pd.DataFrame, via_sql: pd.DataFrame) -> list[str]:
    a = estado.set_index("id_pedido")
    b = via_sql.set_index("id_pedido")
    problemas = []
    if set(a.index) != set(b.index):
        problemas.append(f"conjunto de ids difere: {sorted(set(a.index) ^ set(b.index))[:10]}")
    for i in sorted(set(a.index) & set(b.index)):
        ra, rb = a.loc[i], b.loc[i]
        va = None if pd.isna(ra["valor_cent"]) else int(ra["valor_cent"])
        vb = None if pd.isna(rb["valor_cent"]) else int(rb["valor_cent"])
        if (bool(ra["existe"]), ra["id_cliente"], ra["status"], va) != (bool(rb["existe"]), rb["id_cliente"], rb["status"], vb):
            problemas.append(f"{i}: pandas={tuple(ra[['existe','id_cliente','status','valor_cent']])} sql={tuple(rb[['existe','id_cliente','status','valor_cent']])}")
    return problemas


# --------------------------------------------------------------------------- comparação
def _causa(ts_o, ts_d) -> str:
    if ts_d < ts_o:
        return "destino desatualizado: não refletiu o evento mais recente do log"
    if ts_d == ts_o:
        return "mesmo instante, conteúdo diferente (alteração na carga ou conflito)"
    return "timestamp do destino posterior ao último evento do log"


def comparar(estado: pd.DataFrame, destino: pd.DataFrame, tol_cent: int = TOLERANCIA_CENTAVOS_PADRAO):
    """Retorna (divergencias, observacoes, impacto_por_id, estado_final)."""
    # --- linha de referência do destino por pedido (destino pode ter várias linhas para o mesmo id)
    n_linhas = destino.groupby("id_pedido").size().rename("n_linhas_d")
    soma_dest = destino.groupby("id_pedido")["valor_cent"].sum().rename("soma_dest_cent")
    dd = destino.drop_duplicates(["id_pedido", "id_cliente", "status", "valor_cent", "ts_utc"])
    n_distintas = dd.groupby("id_pedido").size().rename("n_distintas_d")
    o = estado.rename(columns={"id_cliente": "cli_o", "status": "st_o", "valor_cent": "val_o", "ts_utc": "ts_o"})
    dd = dd.merge(o[["id_pedido", "cli_o", "st_o", "val_o"]], on="id_pedido", how="left")
    dd["_casa"] = ((dd["id_cliente"] == dd["cli_o"]) & (dd["status"] == dd["st_o"])
                   & (dd["valor_cent"].fillna(-1) == dd["val_o"].fillna(-2)))
    empates = {i: set((c, s, v) for _, c, s, v in lst) for i, lst in
               estado.loc[estado["n_cand"] > 1].set_index("id_pedido")["candidatos"].items()}
    dd["_casa_empate"] = False
    for ix in dd.index[dd["id_pedido"].isin(empates)]:
        r = dd.loc[ix]
        v = None if pd.isna(r["valor_cent"]) else int(r["valor_cent"])
        dd.loc[ix, "_casa_empate"] = (r["id_cliente"], r["status"], v) in empates[r["id_pedido"]]
    dd["_ref"] = dd["_casa"] | dd["_casa_empate"]
    ref = dd.sort_values(["id_pedido", "_ref", "ts_utc", "_ordem"]).groupby("id_pedido").tail(1)
    ref = ref.rename(columns={"id_cliente": "cli_d", "status": "st_d", "valor_cent": "val_d", "ts_utc": "ts_d"})
    ref = ref[["id_pedido", "cli_d", "st_d", "val_d", "ts_d", "_casa_empate"]]

    m = estado.rename(columns={"id_cliente": "cli_o", "status": "st_o", "valor_cent": "val_o", "ts_utc": "ts_o"}) \
              .merge(ref, on="id_pedido", how="outer", indicator=True) \
              .merge(n_linhas, on="id_pedido", how="left").merge(soma_dest, on="id_pedido", how="left") \
              .merge(n_distintas, on="id_pedido", how="left")
    m["existe"] = m["existe"].fillna(False).astype(bool)

    # --- empate de timestamp resolvido pelo destino: adota o candidato que o destino carrega e sinaliza
    emp = (m["n_cand"].fillna(0) > 1) & m["_casa_empate"].fillna(False).astype(bool) & (m["_merge"] == "both")
    for c_o, c_d in (("cli_o", "cli_d"), ("st_o", "st_d"), ("val_o", "val_d")):
        m.loc[emp, c_o] = m.loc[emp, c_d]
    m["empate_resolvido"] = emp

    div, obs = [], []

    def add(i, tipo, campo, vo, vd, dif="", conf="ALTA", causa="", det=""):
        div.append({"id_pedido": i, "tipo_divergencia": tipo, "campo": campo, "valor_origem": vo, "valor_destino": vd,
                    "diferenca_valor": dif, "severidade": SEVERIDADE[tipo], "confianca": conf,
                    "causa_provavel": causa, "detalhe": det})

    # existência
    for r in m[(m["_merge"] == "left_only") & m["existe"]].itertuples():
        add(r.id_pedido, T_AUSENTE, "existencia", f"existe ({r.st_o}; {fmt_valor(r.val_o)})", "ausente",
            causa="pedido existente no sistema nunca chegou ao destino")
    for r in m[(m["_merge"] == "both") & ~m["existe"]].itertuples():
        add(r.id_pedido, T_EXCLUIDO, "existencia", f"excluído em {r.ts_o:%Y-%m-%d %H:%M} UTC", f"presente ({r.st_d}; {fmt_valor(r.val_d)})",
            causa="exclusão (D) não foi propagada ao destino")
    for r in m[(m["_merge"] == "right_only")].itertuples():
        add(r.id_pedido, T_ORFAO, "existencia", "nunca visto no log", f"presente ({r.st_d}; {fmt_valor(r.val_d)})",
            causa="pedido do destino sem nenhum evento na origem")
    for r in m[m["n_linhas_d"].fillna(1) > 1].itertuples():
        add(r.id_pedido, T_DUPLICADO, "linhas_no_destino", "1", str(int(r.n_linhas_d)),
            dif=fmt_valor(int(r.soma_dest_cent) - int(r.val_d)),
            causa="mais de uma linha para o mesmo pedido no destino (soma em dobro no faturamento)",
            det=("linhas extras com conteúdo diferente da linha correta (versão antiga não foi substituída)"
                 if r.n_distintas_d > 1 else "linhas idênticas (carga repetida)"))

    # campos do pedido (só onde existe dos dois lados)
    ambos = m[(m["_merge"] == "both") & m["existe"]]
    for r in ambos.itertuples():
        causa = _causa(r.ts_o, r.ts_d)
        conf = "BAIXA" if r.n_cand > 1 and not r.empate_resolvido else "ALTA"
        det = ""
        if r.n_cand > 1 and not r.empate_resolvido:
            det = "empate de timestamp na origem e o destino não coincide com nenhum candidato"
        if r.cli_o != r.cli_d:
            add(r.id_pedido, T_CLIENTE, "id_cliente", r.cli_o, r.cli_d, conf=conf, causa=causa, det=det)
        if r.st_o != r.st_d:
            add(r.id_pedido, T_STATUS, "status", r.st_o, r.st_d, conf=conf, causa=causa, det=det)
        if pd.isna(r.val_o) and pd.isna(r.val_d):
            pass
        elif r.valor_ausente_origem:
            if pd.isna(r.val_o) or r.val_d != r.val_o:
                add(r.id_pedido, T_VALOR_NULO, "valor_total", f"{fmt_valor(r.val_o)} (último valor conhecido; log veio em branco)",
                    fmt_valor(r.val_d), dif=fmt_valor(r.val_d - r.val_o) if pd.notna(r.val_o) else "", conf="MEDIA",
                    causa="valor nulo no log gravado como 0,00 no destino",
                    det="o valor real do pedido é desconhecido; usamos o último valor informado como referência")
        elif r.val_o != r.val_d:
            dif = int(r.val_d - r.val_o)
            tipo = T_CENTAVOS if abs(dif) <= tol_cent else T_VALOR
            add(r.id_pedido, tipo, "valor_total", fmt_valor(r.val_o), fmt_valor(r.val_d), dif=fmt_valor(dif), conf=conf,
                causa=causa if tipo == T_VALOR else "diferença de centavos (arredondamento/ajuste na carga?)", det=det)

    divergencias = pd.DataFrame(div, columns=["id_pedido", "tipo_divergencia", "campo", "valor_origem", "valor_destino",
                                              "diferenca_valor", "severidade", "confianca", "causa_provavel", "detalhe"])
    if len(divergencias):
        divergencias = divergencias.sort_values(["tipo_divergencia", "id_pedido", "campo"]).reset_index(drop=True)
    ids_div = set(divergencias["id_pedido"])

    # --- observações: coisas que parecem problema mas NÃO são divergência (ou que o negócio deve saber)
    for r in ambos[ambos["empate_resolvido"]].itertuples():
        alts = [f"{op}|{st}|{fmt_valor(v)}" for op, _, st, v in estado.set_index("id_pedido").loc[r.id_pedido, "candidatos"]]
        obs.append((r.id_pedido, "AMBIGUO_EMPATE_DE_TIMESTAMP", "eventos conflitantes no mesmo instante: " + " vs ".join(alts)
                    + "; o destino coincide com um deles, então não reportado como divergência"))
    so_ts = ambos[(~ambos["id_pedido"].isin(ids_div)) & (~ambos["empate_resolvido"]) & (ambos["ts_o"] != ambos["ts_d"])]
    for r in so_ts.itertuples():
        tipo = "TIMESTAMP_DESTINO_DESATUALIZADO_SEM_IMPACTO" if r.ts_d < r.ts_o else "TIMESTAMP_DESTINO_POSTERIOR_A_ORIGEM"
        obs.append((r.id_pedido, tipo, f"origem {r.ts_o:%Y-%m-%d %H:%M} UTC; destino {r.ts_d:%Y-%m-%d %H:%M} UTC; cliente/status/valor idênticos"))
    for r in estado.itertuples():
        if r.sem_evento_criacao:
            obs.append((r.id_pedido, "SEM_EVENTO_DE_CRIACAO", "log começa em U (captura iniciou depois do pedido); estado reconstruído do 1º evento visto"))
        if r.recriado_apos_exclusao:
            obs.append((r.id_pedido, "ID_REUTILIZADO_APOS_EXCLUSAO", "pedido excluído (D) e criado de novo (I) com o mesmo id; vale o último evento"))
        if r.evento_apos_exclusao:
            obs.append((r.id_pedido, "EVENTO_APOS_EXCLUSAO", "há atualização (U) depois de D sem nova criação (I)"))
        if r.n_eventos_duplicados:
            obs.append((r.id_pedido, "EVENTO_DUPLICADO_NO_LOG", f"{r.n_eventos_duplicados} evento(s) repetido(s) idêntico(s) descartado(s)"))
        if r.cliente_mudou:
            obs.append((r.id_pedido, "CLIENTE_ALTERADO_NO_LOG", "id_cliente muda ao longo do histórico; vale o do último evento"))
        if isinstance(r.regressoes_status, str):
            obs.append((r.id_pedido, "STATUS_SAIU_DE_ESTADO_FINAL", r.regressoes_status))
    observacoes = pd.DataFrame(obs, columns=["id_pedido", "tipo_observacao", "detalhe"]).sort_values(["tipo_observacao", "id_pedido"]).reset_index(drop=True)

    # --- impacto financeiro por pedido: (soma do que o destino tem) - (valor real)
    real = np.where(m["existe"], m["val_o"].fillna(0).astype("float"), 0.0)
    impacto = (m["soma_dest_cent"].fillna(0).astype("float") - real)
    m["impacto_cent"] = impacto.astype("int64")
    tipo_prim = {}
    for i, grp in divergencias.groupby("id_pedido"):
        tipo_prim[i] = min(grp["tipo_divergencia"], key=PRIORIDADE_TIPO.index)
    m["tipo_principal"] = m["id_pedido"].map(tipo_prim)
    sem_causa = m[(m["impacto_cent"] != 0) & m["tipo_principal"].isna()]
    if len(sem_causa):
        raise AssertionError(f"impacto financeiro sem divergência correspondente: {sem_causa['id_pedido'].tolist()}")
    return divergencias, observacoes, m


# --------------------------------------------------------------------------- métricas e saída
def calcular_metricas(estado, destino_raw, m, divergencias, observacoes, info_fuso) -> tuple[dict, pd.DataFrame]:
    real_total = int(np.where(m["existe"], m["val_o"].fillna(0).astype("float"), 0).sum())
    rel_total = int(destino_raw["valor_cent"].sum())
    ativos = m[m["existe"]]
    real_nc = int(ativos.loc[ativos["st_o"] != "CANCELADO", "val_o"].fillna(0).sum())
    rel_nc = int(destino_raw.loc[destino_raw["status"] != "CANCELADO", "valor_cent"].sum())
    por_tipo_pedidos = (divergencias.groupby("tipo_divergencia")["id_pedido"].nunique().sort_values(ascending=False).to_dict())
    por_tipo_linhas = divergencias["tipo_divergencia"].value_counts().to_dict()
    imp = (m.dropna(subset=["tipo_principal"]).groupby("tipo_principal")
           .agg(qtd_pedidos=("id_pedido", "nunique"), impacto_cent=("impacto_cent", "sum")).reset_index())
    imp["impacto_rs"] = (imp["impacto_cent"] / 100).round(2)
    imp["_o"] = imp["tipo_principal"].map(PRIORIDADE_TIPO.index)
    imp = imp.sort_values("_o").drop(columns=["_o", "impacto_cent"]).rename(columns={"tipo_principal": "tipo_divergencia"})
    # incerteza dos empates: se o OUTRO candidato fosse o correto, quanto o destino estaria acima/abaixo
    incerteza = 0
    est_i = estado.set_index("id_pedido")
    for r in m[m["empate_resolvido"]].itertuples():
        alts = [v for _, _, _, v in est_i.loc[r.id_pedido, "candidatos"] if v is not None and v != r.val_d]
        if alts:
            incerteza += int(r.val_d) - alts[0]
    return {
        "fuso_destino": info_fuso,
        "pedidos_distintos_no_log": int(len(estado)),
        "pedidos_existentes_na_origem": int(estado["existe"].sum()),
        "pedidos_excluidos_na_origem": int((~estado["existe"]).sum()),
        "linhas_destino": int(len(destino_raw)),
        "pedidos_distintos_no_destino": int(destino_raw["id_pedido"].nunique()),
        "pedidos_divergentes": int(divergencias["id_pedido"].nunique()),
        "linhas_no_relatorio": int(len(divergencias)),
        "divergencias_por_tipo_pedidos": por_tipo_pedidos,
        "divergencias_por_tipo_linhas": por_tipo_linhas,
        "faturamento_real_rs": real_total / 100,
        "faturamento_relatorio_rs": rel_total / 100,
        "diferenca_rs": (rel_total - real_total) / 100,
        "faturamento_real_sem_cancelados_rs": real_nc / 100,
        "faturamento_relatorio_sem_cancelados_rs": rel_nc / 100,
        "diferenca_sem_cancelados_rs": (rel_nc - real_nc) / 100,
        "incerteza_empates_rs": incerteza / 100,
        "observacoes_por_tipo": observacoes["tipo_observacao"].value_counts().to_dict(),
        "impacto_por_tipo": imp.to_dict(orient="records"),
        "conferencia_decomposicao_rs": float(imp["impacto_rs"].sum()),
    }, imp


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--origem", type=Path, default=RAIZ / "data" / "pedidos_origem.csv")
    p.add_argument("--destino", type=Path, default=RAIZ / "data" / "pedidos_destino.csv")
    p.add_argument("--saida", type=Path, default=RAIZ, help="pasta onde os relatórios são gravados")
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="processos para volumes grandes")
    p.add_argument("--tolerancia-centavos", type=int, default=TOLERANCIA_CENTAVOS_PADRAO)
    p.add_argument("--sem-verificacao-sql", action="store_true", help="pula a conferência cruzada em SQL")
    a = p.parse_args(argv)
    a.saida.mkdir(parents=True, exist_ok=True)

    etapas = tqdm(total=6, desc="Reconciliação", unit="etapa", file=sys.stderr)
    origem = carregar_origem(a.origem); etapas.update(1)
    destino = carregar_destino(a.destino)
    info_fuso = inferir_fuso_destino(origem, destino)
    destino = aplicar_fuso_destino(destino, info_fuso["horas_para_utc"]); etapas.update(1)
    estado = reconstruir_estado(origem, workers=a.workers); etapas.update(1)
    if not a.sem_verificacao_sql:
        problemas = verificar_paridade_sql(estado, estado_via_sql(a.origem))
        if problemas:
            raise AssertionError("pandas e SQL discordam:\n" + "\n".join(problemas[:10]))
    etapas.update(1)
    divergencias, observacoes, m = comparar(estado, destino, a.tolerancia_centavos); etapas.update(1)
    metricas, impacto = calcular_metricas(estado, destino, m, divergencias, observacoes, info_fuso)
    divergencias.to_csv(a.saida / "relatorio_divergencias.csv", index=False, encoding="utf-8")
    transicoes = observacoes["tipo_observacao"] == "STATUS_SAIU_DE_ESTADO_FINAL"   # volumoso: arquivo à parte
    observacoes[~transicoes].to_csv(a.saida / "observacoes_nao_divergencias.csv", index=False, encoding="utf-8")
    observacoes[transicoes].to_csv(a.saida / "transicoes_de_status_suspeitas.csv", index=False, encoding="utf-8")
    impacto.to_csv(a.saida / "impacto_financeiro.csv", index=False, encoding="utf-8")
    (a.saida / "metricas.json").write_text(json.dumps(metricas, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    etapas.update(1); etapas.close()

    print(f"\nPedidos no log: {metricas['pedidos_distintos_no_log']} (existentes {metricas['pedidos_existentes_na_origem']}, "
          f"excluídos {metricas['pedidos_excluidos_na_origem']}) | divergentes: {metricas['pedidos_divergentes']} "
          f"({metricas['linhas_no_relatorio']} linhas)")
    for t, n in metricas["divergencias_por_tipo_pedidos"].items():
        print(f"  {t:32s} {n:3d}")
    print(f"Faturamento real R$ {metricas['faturamento_real_rs']:,.2f} | relatório R$ {metricas['faturamento_relatorio_rs']:,.2f} "
          f"| diferença R$ {metricas['diferenca_rs']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
