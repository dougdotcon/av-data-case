"""Testes das regras de negócio. Cada teste cobre uma decisão registrada em DECISOES.md."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import reconcilia as r  # noqa: E402

CAB_O = "id_pedido,id_cliente,status,valor_total,atualizado_em,operacao\n"
CAB_D = "id_pedido,id_cliente,status,valor_total,atualizado_em\n"


def rodar(tmp_path, origem: str, destino: str, workers=1, **kw):
    fo, fd = tmp_path / "o.csv", tmp_path / "d.csv"
    fo.write_text(CAB_O + origem)
    fd.write_text(CAB_D + destino)
    o = r.carregar_origem(fo)
    d = r.carregar_destino(fd)
    info = r.inferir_fuso_destino(o, d)
    d = r.aplicar_fuso_destino(d, info["horas_para_utc"])
    est = r.reconstruir_estado(o, workers=workers, progresso=False, **kw)
    div, obs, m = r.comparar(est, d)
    return est, div, obs, m, fo


# destino grava em UTC sem fuso: 10:00-03:00 == 13:00 no destino
BASE_O = "1,50,NOVO,100.00,2026-06-01T10:00:00-03:00,I\n"
BASE_D = "1,50,NOVO,100.00,2026-06-01 13:00:00\n"


def test_caso_limpo_nao_gera_divergencia(tmp_path):
    _, div, _, _, _ = rodar(tmp_path, BASE_O, BASE_D)
    assert div.empty


def test_fuso_do_destino_e_inferido_como_utc(tmp_path):
    fo, fd = tmp_path / "o.csv", tmp_path / "d.csv"
    fo.write_text(CAB_O + BASE_O)
    fd.write_text(CAB_D + BASE_D)
    info = r.inferir_fuso_destino(r.carregar_origem(fo), r.carregar_destino(fd))
    assert info["fuso"] == "UTC"


def test_status_com_caixa_e_espaco_nao_e_divergencia(tmp_path):
    d = "1,50, novo ,100.00,2026-06-01 13:00:00\n"
    _, div, _, _, _ = rodar(tmp_path, BASE_O, d)
    assert div.empty


def test_eventos_fora_de_ordem_valem_pelo_timestamp_e_nao_pela_posicao_no_arquivo(tmp_path):
    o = ("1,50,PAGO,100.00,2026-06-02T10:00:00-03:00,U\n" + BASE_O)           # U mais novo aparece ANTES do I
    d = "1,50,PAGO,100.00,2026-06-02 13:00:00\n"
    est, div, _, _, _ = rodar(tmp_path, o, d)
    assert est.loc[0, "status"] == "PAGO" and div.empty


def test_evento_duplicado_exato_e_descartado(tmp_path):
    est, div, obs, _, _ = rodar(tmp_path, BASE_O + BASE_O, BASE_D)
    assert div.empty and int(est.loc[0, "n_eventos_duplicados"]) == 1
    assert "EVENTO_DUPLICADO_NO_LOG" in set(obs["tipo_observacao"])


def test_pedido_sem_evento_de_criacao_nao_e_divergencia(tmp_path):
    o = "1,50,PAGO,100.00,2026-06-02T10:00:00-03:00,U\n"
    d = "1,50,PAGO,100.00,2026-06-02 13:00:00\n"
    _, div, obs, _, _ = rodar(tmp_path, o, d)
    assert div.empty and "SEM_EVENTO_DE_CRIACAO" in set(obs["tipo_observacao"])


def test_exclusao_nao_propagada_vira_divergencia(tmp_path):
    o = BASE_O + "1,50,NOVO,100.00,2026-06-03T10:00:00-03:00,D\n"
    _, div, _, _, _ = rodar(tmp_path, o, BASE_D)
    assert list(div["tipo_divergencia"]) == [r.T_EXCLUIDO]


def test_exclusao_propagada_corretamente_nao_e_divergencia(tmp_path):
    o = BASE_O + "1,50,NOVO,100.00,2026-06-03T10:00:00-03:00,D\n2,51,NOVO,10.00,2026-06-01T10:00:00-03:00,I\n"
    d = "2,51,NOVO,10.00,2026-06-01 13:00:00\n"
    _, div, _, _, _ = rodar(tmp_path, o, d)
    assert div.empty


def test_id_reutilizado_apos_exclusao_nao_e_pedido_excluido(tmp_path):
    o = (BASE_O + "1,50,NOVO,100.00,2026-06-03T10:00:00-03:00,D\n"
         + "1,60,NOVO,300.00,2026-06-10T10:00:00-03:00,I\n")
    d = "1,60,NOVO,300.00,2026-06-10 13:00:00\n"
    _, div, obs, _, _ = rodar(tmp_path, o, d)
    assert div.empty and "ID_REUTILIZADO_APOS_EXCLUSAO" in set(obs["tipo_observacao"])


def test_pedido_ausente_no_destino(tmp_path):
    _, div, _, _, _ = rodar(tmp_path, BASE_O + "2,51,NOVO,10.00,2026-06-01T10:00:00-03:00,I\n", BASE_D)
    assert list(div["tipo_divergencia"]) == [r.T_AUSENTE] and list(div["id_pedido"]) == ["2"]


def test_valor_em_branco_herda_ultimo_valor_e_destino_zerado_e_divergencia(tmp_path):
    o = BASE_O + "1,50,PAGO,,2026-06-02T10:00:00-03:00,U\n"
    d = "1,50,PAGO,0.00,2026-06-02 13:00:00\n"
    est, div, _, _, _ = rodar(tmp_path, o, d)
    assert int(est.loc[0, "valor_cent"]) == 10000                       # herdado, não zero
    assert list(div["tipo_divergencia"]) == [r.T_VALOR_NULO] and div.loc[0, "confianca"] == "MEDIA"


def test_valor_em_branco_e_destino_com_valor_herdado_nao_diverge(tmp_path):
    o = BASE_O + "1,50,PAGO,,2026-06-02T10:00:00-03:00,U\n"
    d = "1,50,PAGO,100.00,2026-06-02 13:00:00\n"
    _, div, _, _, _ = rodar(tmp_path, o, d)
    assert div.empty


def test_empate_de_timestamp_onde_destino_coincide_com_um_candidato_nao_e_divergencia(tmp_path):
    o = (BASE_O + "1,50,PAGO,120.00,2026-06-02T10:00:00-03:00,U\n"
         + "1,50,ENVIADO,100.00,2026-06-02T10:00:00-03:00,U\n")
    for status, valor in (("PAGO", "120.00"), ("ENVIADO", "100.00")):          # qualquer ordem de chegada serve
        d = f"1,50,{status},{valor},2026-06-02 13:00:00\n"
        _, div, obs, _, _ = rodar(tmp_path, o, d)
        assert div.empty and "AMBIGUO_EMPATE_DE_TIMESTAMP" in set(obs["tipo_observacao"])


def test_empate_de_timestamp_onde_destino_nao_coincide_vira_divergencia_de_baixa_confianca(tmp_path):
    o = (BASE_O + "1,50,PAGO,120.00,2026-06-02T10:00:00-03:00,U\n"
         + "1,50,ENVIADO,100.00,2026-06-02T10:00:00-03:00,U\n")
    d = "1,50,CANCELADO,100.00,2026-06-02 13:00:00\n"
    _, div, _, _, _ = rodar(tmp_path, o, d)
    assert list(div["tipo_divergencia"]) == [r.T_STATUS] and div.loc[0, "confianca"] == "BAIXA"


def test_destino_duplicado_e_classificado_e_nao_conta_status_errado_duas_vezes(tmp_path):
    o = BASE_O + "1,50,PAGO,100.00,2026-06-02T10:00:00-03:00,U\n"
    d = "1,50,NOVO,100.00,2026-06-02 13:00:00\n1,50,PAGO,100.00,2026-06-02 13:00:00\n"
    _, div, _, m, _ = rodar(tmp_path, o, d)
    assert list(div["tipo_divergencia"]) == [r.T_DUPLICADO]
    assert int(m.loc[0, "impacto_cent"]) == 10000                          # a linha extra soma em dobro


def test_centavos_sao_classificados_a_parte(tmp_path):
    d = "1,50,NOVO,100.01,2026-06-01 13:00:00\n"
    _, div, _, _, _ = rodar(tmp_path, BASE_O, d)
    assert list(div["tipo_divergencia"]) == [r.T_CENTAVOS]


def test_timestamp_diferente_com_conteudo_igual_nao_e_divergencia(tmp_path):
    outros_o = "".join(f"{i},51,NOVO,10.00,2026-06-01T10:00:00-03:00,I\n" for i in range(2, 8))   # permitem inferir o fuso
    outros_d = "".join(f"{i},51,NOVO,10.00,2026-06-01 13:00:00\n" for i in range(2, 8))
    o = BASE_O + outros_o
    d = "1,50,NOVO,100.00,2026-06-20 13:00:00\n" + outros_d
    _, div, obs, _, _ = rodar(tmp_path, o, d)
    assert div.empty and "TIMESTAMP_DESTINO_POSTERIOR_A_ORIGEM" in set(obs["tipo_observacao"])


def test_paralelo_e_serial_dao_o_mesmo_estado(tmp_path):
    o = "".join(f"{i},50,NOVO,10.00,2026-06-01T10:00:00-03:00,I\n{i},50,PAGO,12.00,2026-06-02T10:00:00-03:00,U\n"
                for i in range(1, 201))
    fo = tmp_path / "o.csv"; fo.write_text(CAB_O + o)
    ev = r.carregar_origem(fo)
    a = r.reconstruir_estado(ev, workers=1, progresso=False)
    b = r.reconstruir_estado(ev, workers=2, min_linhas_paralelo=1, progresso=False)
    cols = ["id_pedido", "existe", "id_cliente", "status", "valor_cent", "ts_utc"]
    pd.testing.assert_frame_equal(a[cols], b[cols])


def test_sql_e_pandas_concordam_nos_dados_reais():
    raiz = Path(__file__).resolve().parent.parent
    o = r.carregar_origem(raiz / "data" / "pedidos_origem.csv")
    est = r.reconstruir_estado(o, progresso=False)
    assert r.verificar_paridade_sql(est, r.estado_via_sql(raiz / "data" / "pedidos_origem.csv")) == []


def test_resultado_nos_dados_reais_e_estavel(tmp_path):
    """Trava os números reportados no RESUMO.md: se mudar uma regra, este teste avisa."""
    raiz = Path(__file__).resolve().parent.parent
    assert r.main(["--origem", str(raiz / "data" / "pedidos_origem.csv"), "--destino", str(raiz / "data" / "pedidos_destino.csv"),
                   "--saida", str(tmp_path), "--workers", "1"]) == 0
    div = pd.read_csv(tmp_path / "relatorio_divergencias.csv", dtype=str)
    assert div["id_pedido"].nunique() == 30 and len(div) == 31
    assert div["tipo_divergencia"].value_counts().to_dict() == {
        "AUSENTE_NO_DESTINO": 5, "EXCLUIDO_PRESENTE_NO_DESTINO": 5, "DUPLICADO_NO_DESTINO": 4, "STATUS_DIVERGENTE": 4,
        "VALOR_DIVERGENTE_CENTAVOS": 4, "CLIENTE_DIVERGENTE": 3, "VALOR_AUSENTE_NA_ORIGEM": 3, "VALOR_DIVERGENTE": 3}
    # não pode haver falso positivo nos casos que parecem problema mas não são
    nao_div = {"10011", "10029", "10126", "10013", "10059", "10081", "10103", "10054", "10105", "10149",
               "10006", "10061", "10076", "10096", "10108", "10023", "10053", "10060", "10129"}
    assert not nao_div & set(div["id_pedido"])
