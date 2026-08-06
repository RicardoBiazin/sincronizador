"""Dropbox, OneDrive e Google Drive contra servidores falsos das APIs."""
import logging, os, shutil, sys, tempfile, time

sys.path.insert(0, r"c:\DEV\sincronizador")

import apis_falsas as F
from sincronizador import config as cfgmod, endpoints as ep, engine, oauth

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("t"); log.setLevel(logging.INFO)

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


def contar(nome_log_alvo, fn):
    recs = []
    h = logging.Handler(); h.emit = lambda r: recs.append(r.getMessage())
    log.addHandler(h)
    try:
        fn()
    finally:
        log.removeHandler(h)
    return recs


base = tempfile.mkdtemp(prefix="sinc_oa_")
src = os.path.join(base, "origem")
os.makedirs(os.path.join(src, "sub"))
DADOS = {"a.txt": b"conteudo A", "sub/b.bin": bytes(range(256)) * 40}
for n, d in DADOS.items():
    p = os.path.join(src, n.replace("/", os.sep))
    with open(p, "wb") as f:
        f.write(d)
    os.utime(p, (1_600_000_000, 1_600_000_000))

MTIME = 1_600_000_000


def aponta_token(nome_provedor, porta):
    oauth.PROVEDORES[nome_provedor].token_url = \
        "http://127.0.0.1:%d/oauth/token" % porta


# =========================================================================
print("\n=== Dropbox ===")
est_db = F.estado_base(arquivos={}, pastas={"/envio"}, sessoes={}, paginas=[],
                       envios_simples=0, envios_em_sessao=0)
srv_db, porta_db = F.subir(F.DropboxFake, est_db)
aponta_token("dropbox", porta_db)
url_db = "http://127.0.0.1:%d" % porta_db

rem_db = cfgmod.Remote(options={
    "client_id": "APPKEY", "refresh_token": "REFRESH-BOM",
    "api_url": url_db + "/2", "content_url": url_db + "/2"})
job = cfgmod.Job(name="t_db", mode="espelho", source=src, dest="envio",
                 source_type="local", dest_type="dropbox", validate=True)
job.dest_remote = rem_db

st = engine.run_job(job, log, workers=4)
check(st.copied == 2 and not st.errors, f"enviou 2 (c={st.copied} err={st.errors})")
check(sorted(est_db["arquivos"]) == ["/envio/a.txt", "/envio/sub/b.bin"],
      f"caminhos no Dropbox: {sorted(est_db['arquivos'])}")
check(est_db["arquivos"]["/envio/sub/b.bin"][0] == DADOS["sub/b.bin"],
      "binario integro")
check(est_db["arquivos"]["/envio/a.txt"][1] == "2020-09-13T12:26:40Z",
      f"client_modified enviado ({est_db['arquivos']['/envio/a.txt'][1]})")
check(est_db["renovacoes"] >= 1, "obteve token pelo refresh token")
check(not st.validation_failed, f"validacao ok ({st.validation_failed})")

e = ep.make_endpoint("envio", "dropbox", rem_db)
achados = sorted(e.scan())
check(achados == ["a.txt", "sub/b.bin"], f"scan com paginacao (cursor): {achados}")
check(abs(e.scan()["a.txt"].mtime - MTIME) < 1, "data volta na listagem")
check(e.preserves_mtime is True, "Dropbox preserva a data")
e.probe()

st = engine.run_job(job, log, workers=4)
check(st.copied == 0 and st.updated == 0, f"2a passada nao reenvia (c={st.copied} u={st.updated})")

recs = contar(None, lambda: engine.run_job(job, log, workers=4))
check(any("ate 4 arquivo(s) por vez" in m for m in recs), "usa 4 threads")

# envio em sessao (arquivo grande)
import sincronizador.backends_oauth as bo
antes_sessao = est_db["envios_em_sessao"]
grande = os.path.join(src, "grande.dat")
with open(grande, "wb") as f:
    f.write(b"G" * (3 * 1024 * 1024))
os.utime(grande, (MTIME, MTIME))
limite_original = bo.LIMITE_ENVIO_DROPBOX
pedaco_original = bo.PEDACO
bo.LIMITE_ENVIO_DROPBOX = 1024 * 1024      # forca o caminho de sessao
bo.PEDACO = 512 * 1024
st = engine.run_job(job, log)
bo.LIMITE_ENVIO_DROPBOX = limite_original
bo.PEDACO = pedaco_original
check(est_db["envios_em_sessao"] == antes_sessao + 1,
      f"arquivo grande foi por upload_session ({est_db['envios_em_sessao']})")
check(est_db["arquivos"]["/envio/grande.dat"][0] == b"G" * (3 * 1024 * 1024),
      "arquivo grande remontado corretamente")
os.remove(grande)

# token revogado durante o uso: o MESMO endpoint ja tem o token antigo em
# maos, entao a proxima chamada leva 401 e precisa renovar sozinha
vivo = ep.make_endpoint("envio", "dropbox", rem_db)
vivo.scan()                              # guarda o token atual na sessao
est_db["token_valido"] = "TOKEN-NOVO-1"  # o provedor invalida o anterior
antes = est_db["nao_autorizadas"]
achados = sorted(vivo.scan())
check(est_db["nao_autorizadas"] > antes and achados,
      f"401 dispara renovacao e a chamada refaz ({achados})")

# espelho apaga
os.remove(os.path.join(src, "a.txt"))
st = engine.run_job(job, log)
check("/envio/a.txt" not in est_db["arquivos"], f"espelho apagou ({st.deleted})")

# volta: Dropbox -> local
volta = os.path.join(base, "volta_db")
jv = cfgmod.Job(name="t_db_volta", mode="espelho", source="envio", dest=volta,
                source_type="dropbox", dest_type="local", validate=True)
jv.source_remote = rem_db
st = engine.run_job(jv, log, workers=4)
check(st.copied == 1 and not st.errors, f"baixou (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "sub", "b.bin"), "rb") as f:
    check(f.read() == DADOS["sub/b.bin"], "download integro")

srv_db.shutdown()

# =========================================================================
print("\n=== OneDrive (Microsoft Graph) ===")
os.utime(os.path.join(src, "sub", "b.bin"), (MTIME, MTIME))
with open(os.path.join(src, "a.txt"), "wb") as f:
    f.write(DADOS["a.txt"])
os.utime(os.path.join(src, "a.txt"), (MTIME, MTIME))

est_od = F.estado_base(arquivos={}, pastas=set(), ids={}, sessoes={},
                       envios_simples=0, envios_em_sessao=0, patches=0)
srv_od, porta_od = F.subir(F.GraphFake, est_od)
aponta_token("microsoft", porta_od)

rem_od = cfgmod.Remote(options={
    "client_id": "APPID", "refresh_token": "REFRESH-BOM", "tenant": "common",
    "graph_url": "http://127.0.0.1:%d" % porta_od})
job_od = cfgmod.Job(name="t_od", mode="espelho", source=src, dest="Backup/PC",
                    source_type="local", dest_type="onedrive", validate=True)
job_od.dest_remote = rem_od

st = engine.run_job(job_od, log, workers=4)
check(st.copied == 2 and not st.errors, f"enviou 2 (c={st.copied} err={st.errors})")
check(sorted(est_od["arquivos"]) == ["Backup/PC/a.txt", "Backup/PC/sub/b.bin"],
      f"caminhos no OneDrive: {sorted(est_od['arquivos'])}")
check({"Backup", "Backup/PC", "Backup/PC/sub"} <= est_od["pastas"],
      f"criou a arvore de pastas: {sorted(est_od['pastas'])}")
check(est_od["arquivos"]["Backup/PC/a.txt"][1] == "2020-09-13T12:26:40Z",
      f"lastModifiedDateTime aplicado ({est_od['arquivos']['Backup/PC/a.txt'][1]})")
check(est_od["patches"] >= 2, f"PATCH de fileSystemInfo apos o envio ({est_od['patches']})")
check(not st.validation_failed, f"validacao ok ({st.validation_failed})")

e = ep.make_endpoint("Backup/PC", "onedrive", rem_od)
achados = sorted(e.scan())
check(achados == ["a.txt", "sub/b.bin"], f"scan recursivo com nextLink: {achados}")
check(abs(e.scan()["a.txt"].mtime - MTIME) < 1, "data volta na listagem")
e.probe()

st = engine.run_job(job_od, log, workers=4)
check(st.copied == 0 and st.updated == 0, f"2a passada nao reenvia (c={st.copied} u={st.updated})")

# arquivo grande -> createUploadSession + pedacos
antes_sessao = est_od["envios_em_sessao"]
grande = os.path.join(src, "grande.dat")
with open(grande, "wb") as f:
    f.write(b"O" * (5 * 1024 * 1024))
os.utime(grande, (MTIME, MTIME))
pedaco_original = bo.PEDACO
bo.PEDACO = 1024 * 1024
st = engine.run_job(job_od, log)
bo.PEDACO = pedaco_original
check(est_od["envios_em_sessao"] == antes_sessao + 1,
      f"grande foi por createUploadSession ({est_od['envios_em_sessao']})")
check(est_od["arquivos"]["Backup/PC/grande.dat"][0] == b"O" * (5 * 1024 * 1024),
      "arquivo grande remontado a partir dos pedacos")
check(est_od["arquivos"]["Backup/PC/grande.dat"][1] == "2020-09-13T12:26:40Z",
      "data definida na propria sessao de envio")
os.remove(grande)

vivo = ep.make_endpoint("Backup/PC", "onedrive", rem_od)
vivo.scan()
est_od["token_valido"] = "TOKEN-NOVO-2"
antes = est_od["nao_autorizadas"]
achados = sorted(vivo.scan())
check(est_od["nao_autorizadas"] > antes and "a.txt" in achados,
      f"401 dispara renovacao e refaz ({achados})")

volta = os.path.join(base, "volta_od")
jv = cfgmod.Job(name="t_od_volta", mode="espelho", source="Backup/PC", dest=volta,
                source_type="onedrive", dest_type="local", validate=True)
jv.source_remote = rem_od
st = engine.run_job(jv, log, workers=4)
check(st.copied == 3 and not st.errors, f"baixou 3 (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "sub", "b.bin"), "rb") as f:
    check(f.read() == DADOS["sub/b.bin"], "download integro")

os.remove(os.path.join(src, "a.txt"))
st = engine.run_job(job_od, log)
check("Backup/PC/a.txt" not in est_od["arquivos"], f"espelho apagou ({st.deleted})")

srv_od.shutdown()

# =========================================================================
print("\n=== Google Drive ===")
with open(os.path.join(src, "a.txt"), "wb") as f:
    f.write(DADOS["a.txt"])
os.utime(os.path.join(src, "a.txt"), (MTIME, MTIME))

est_gd = F.estado_base(itens={}, seq=0, criados=0, atualizados=0, pastas_criadas=0)
srv_gd, porta_gd = F.subir(F.DriveFake, est_gd)
aponta_token("google", porta_gd)
url_gd = "http://127.0.0.1:%d" % porta_gd

rem_gd = cfgmod.Remote(options={
    "client_id": "CID", "client_secret": "CSEC", "refresh_token": "REFRESH-BOM",
    "api_url": url_gd + "/drive/v3", "upload_url": url_gd + "/upload/drive/v3"})
job_gd = cfgmod.Job(name="t_gd", mode="espelho", source=src, dest="Backup/PC",
                    source_type="local", dest_type="gdrive", validate=True)
job_gd.dest_remote = rem_gd

st = engine.run_job(job_gd, log)
check(st.copied == 2 and not st.errors, f"enviou 2 (c={st.copied} err={st.errors})")
nomes = sorted(v["name"] for v in est_gd["itens"].values()
               if v["mimeType"] != F.DriveFake.PASTA)
check(nomes == ["a.txt", "b.bin"], f"arquivos criados: {nomes}")
pastas = sorted(v["name"] for v in est_gd["itens"].values()
                if v["mimeType"] == F.DriveFake.PASTA)
check(pastas == ["Backup", "PC", "sub"], f"pastas criadas por id: {pastas}")
alvo = [v for v in est_gd["itens"].values() if v["name"] == "a.txt"][0]
check(alvo["modifiedTime"].startswith("2020-09-13T12:26:40"),
      f"modifiedTime enviado ({alvo['modifiedTime']})")
check(alvo["dados"] == DADOS["a.txt"], "conteudo correto no multipart")
check(not st.validation_failed, f"validacao ok ({st.validation_failed})")

e = ep.make_endpoint("Backup/PC", "gdrive", rem_gd)
achados = sorted(e.scan())
check(achados == ["a.txt", "sub/b.bin"], f"scan por id com paginacao: {achados}")
info = e.scan()["a.txt"]
check(abs(info.mtime - MTIME) < 1, "data volta na listagem")
check(e.content_hash("a.txt", info) == "md5-10", "md5Checksum vira content_hash")
e.probe()

st = engine.run_job(job_gd, log)
check(st.copied == 0 and st.updated == 0, f"2a passada nao reenvia (c={st.copied} u={st.updated})")
check(est_gd["pastas_criadas"] == 3, f"nao recria pastas ({est_gd['pastas_criadas']})")

# alteracao -> PATCH no mesmo id, sem duplicar arquivo
antes_ids = set(est_gd["itens"])
with open(os.path.join(src, "a.txt"), "wb") as f:
    f.write(b"CONTEUDO NOVO")
os.utime(os.path.join(src, "a.txt"), (1_700_000_000, 1_700_000_000))
st = engine.run_job(job_gd, log)
check(st.updated == 1 and set(est_gd["itens"]) == antes_ids,
      f"atualizou pelo id, sem duplicar (u={st.updated})")
alvo = [v for v in est_gd["itens"].values() if v["name"] == "a.txt"][0]
check(alvo["dados"] == b"CONTEUDO NOVO", "conteudo atualizado")

# documento Google (sem binario) deve ser ignorado
est_gd["seq"] += 1
pai_pc = [k for k, v in est_gd["itens"].items() if v["name"] == "PC"][0]
est_gd["itens"]["doc1"] = {"name": "Planilha", "parent": pai_pc,
                           "mimeType": "application/vnd.google-apps.spreadsheet",
                           "dados": b""}
e2 = ep.make_endpoint("Backup/PC", "gdrive", rem_gd)
achados = sorted(e2.scan())
check("Planilha" not in achados and e2.ignorados() == 1,
      f"ignora documentos nativos do Google ({achados}, {e2.ignorados()})")
del est_gd["itens"]["doc1"]

vivo = ep.make_endpoint("Backup/PC", "gdrive", rem_gd)
vivo.scan()
est_gd["token_valido"] = "TOKEN-NOVO-3"
antes = est_gd["nao_autorizadas"]
achados = sorted(vivo.scan())
check(est_gd["nao_autorizadas"] > antes and "a.txt" in achados,
      f"401 dispara renovacao e refaz ({achados})")

volta = os.path.join(base, "volta_gd")
jv = cfgmod.Job(name="t_gd_volta", mode="espelho", source="Backup/PC", dest=volta,
                source_type="gdrive", dest_type="local", validate=True)
jv.source_remote = rem_gd
st = engine.run_job(jv, log)
check(st.copied == 2 and not st.errors, f"baixou 2 (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "a.txt"), "rb") as f:
    check(f.read() == b"CONTEUDO NOVO", "download integro")

os.remove(os.path.join(src, "a.txt"))
st = engine.run_job(job_gd, log)
restantes = sorted(v["name"] for v in est_gd["itens"].values()
                   if v["mimeType"] != F.DriveFake.PASTA)
check(restantes == ["b.bin"], f"espelho apagou no Drive: {restantes}")

srv_gd.shutdown()

# =========================================================================
print("\n=== Credenciais na configuracao ===")
p = os.path.join(base, "cfg.json")
cfg = cfgmod.AppConfig(jobs=[job, job_od, job_gd])
cfgmod.save_config(cfg, p)
bruto = open(p, encoding="utf-8").read()
check("REFRESH-BOM" not in bruto, "refresh token nao aparece em texto puro")
check("CSEC" not in bruto, "client_secret nao aparece em texto puro")
check("CID" in bruto, "client_id (nao secreto) fica legivel")
back = cfgmod.load_config(p)
check(back.jobs[0].dest_remote.opt("refresh_token") == "REFRESH-BOM",
      "refresh token volta decifrado")
check(back.jobs[2].dest_remote.opt("client_secret") == "CSEC",
      "client_secret volta decifrado")

shutil.rmtree(base, ignore_errors=True)
for n in ("t_db", "t_db_volta", "t_od", "t_od_volta", "t_gd", "t_gd_volta"):
    try: os.remove(engine._state_path(n))
    except OSError: pass

print("\n" + ("BACKENDS OAUTH: TODOS OS TESTES PASSARAM" if not falhas
              else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.stdout.flush()
os._exit(1 if falhas else 0)
