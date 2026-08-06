"""Azure Blob contra o emulador Azurite e GCS contra o gcp-storage-emulator.
Servicos de verdade falando HTTP, com os SDKs oficiais.
"""
import hashlib, logging, os, shutil, sys, tempfile

sys.path.insert(0, r"c:\DEV\sincronizador")
from sincronizador import config as cfgmod, endpoints as ep, engine

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("t"); log.setLevel(logging.INFO)

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


base = tempfile.mkdtemp(prefix="sinc_ag_")
src = os.path.join(base, "origem")
os.makedirs(os.path.join(src, "sub"))
dados = {"a.txt": b"conteudo A", "sub/b.bin": bytes(range(256)) * 40}
for n, d in dados.items():
    p = os.path.join(src, n.replace("/", os.sep))
    with open(p, "wb") as f:
        f.write(d)
    os.utime(p, (1_600_000_000, 1_600_000_000))

AZURITE_CS = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
              "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVE"
              "rCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
              "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")

# ===========================================================================
print("\n=== Azure Blob Storage (Azurite) ===")
from azure.storage.blob import BlobServiceClient
svc = BlobServiceClient.from_connection_string(AZURITE_CS)
# o Azurite persiste em disco: comeca sempre de um container limpo
try:
    svc.delete_container("meucontainer")
except Exception:
    pass
for _ in range(30):
    try:
        svc.create_container("meucontainer")
        break
    except Exception:
        import time as _t; _t.sleep(1)   # aguarda a exclusao concluir

rem = cfgmod.Remote(options={"container": "meucontainer",
                             "connection_string": AZURITE_CS})
job = cfgmod.Job(name="t_az", mode="espelho", source=src, dest="backup/dados",
                 source_type="local", dest_type="azureblob", validate=True)
job.dest_remote = rem

st = engine.run_job(job, log, workers=4)
check(st.copied == 2 and not st.errors, f"enviou 2 blobs (c={st.copied} err={st.errors})")

cc = svc.get_container_client("meucontainer")
nomes = sorted(b.name for b in cc.list_blobs())
check(nomes == ["backup/dados/a.txt", "backup/dados/sub/b.bin"],
      f"prefixo aplicado: {nomes}")
check(cc.download_blob("backup/dados/sub/b.bin").readall() == dados["sub/b.bin"],
      "binario integro no Azure")
props = cc.get_blob_client("backup/dados/a.txt").get_blob_properties()
check(abs(float((props.metadata or {}).get("sincmtime", 0)) - 1_600_000_000) < 1,
      f"data original no metadado ({props.metadata})")
check(not st.validation_failed, f"validacao ok ({st.validation_failed})")

# a listagem do Azure traz metadados: a data volta de graca
epa = ep.make_endpoint("backup/dados", "azureblob", rem)
info = epa.scan()["a.txt"]
check(abs(info.mtime - 1_600_000_000) < 1,
      f"scan recupera a data original sem HEAD ({info.mtime})")
check(epa.preserves_mtime is True, "Azure Blob preserva a data")

st = engine.run_job(job, log, workers=4)
check(st.copied == 0 and st.updated == 0,
      f"2a passada nao reenvia (c={st.copied} u={st.updated})")

recs = []
h = logging.Handler(); h.emit = lambda r: recs.append(r.getMessage())
log.addHandler(h); engine.run_job(job, log, workers=4); log.removeHandler(h)
check(any("ate 4 arquivo(s) por vez" in m for m in recs), "Azure Blob usa 4 threads")

# mudanca de mesmo tamanho, detectada pela data (que o Azure preserva)
alvo = os.path.join(src, "a.txt")
with open(alvo, "wb") as f:
    f.write(b"CONTEUDO X")
os.utime(alvo, (1_700_000_000, 1_700_000_000))
st = engine.run_job(job, log)
check(st.updated == 1, f"detectou alteracao pela data (u={st.updated})")
check(cc.download_blob("backup/dados/a.txt").readall() == b"CONTEUDO X",
      "conteudo atualizado no Azure")

# hash da listagem
epa2 = ep.make_endpoint("backup/dados", "azureblob", rem)
i2 = epa2.scan()["a.txt"]
h_local = hashlib.md5(b"CONTEUDO X").hexdigest()
got = epa2.content_hash("a.txt", i2)
check(got in ("", h_local), f"content_hash coerente ({got or 'vazio'})")

# espelho apaga
os.remove(os.path.join(src, "sub", "b.bin"))
st = engine.run_job(job, log)
nomes = sorted(b.name for b in cc.list_blobs())
check(nomes == ["backup/dados/a.txt"], f"espelho apagou no Azure: {nomes}")

# volta: Azure -> local
volta = os.path.join(base, "volta_az")
jv = cfgmod.Job(name="t_az_volta", mode="espelho", source="backup/dados", dest=volta,
                source_type="azureblob", dest_type="local", validate=True)
jv.source_remote = rem
st = engine.run_job(jv, log, workers=4)
check(st.copied == 1 and not st.errors, f"baixou do Azure (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "a.txt"), "rb") as f:
    check(f.read() == b"CONTEUDO X", "conteudo baixado do Azure confere")

try:
    ruim = cfgmod.Remote(options={"container": "naoexiste", "connection_string": AZURITE_CS})
    e = ep.make_endpoint("", "azureblob", ruim); e.probe(); ok = False
except Exception:
    ok = True
check(ok, "probe() falha em container inexistente")

# ===========================================================================
print("\n=== Google Cloud Storage (gcp-storage-emulator) ===")
from gcp_storage_emulator.server import create_server
gsrv = create_server("127.0.0.1", 9023, in_memory=True, default_bucket="meu-bucket")
gsrv.start()
os.environ["STORAGE_EMULATOR_HOST"] = "http://127.0.0.1:9023"

try:
    grem = cfgmod.Remote(options={"bucket": "meu-bucket", "project": "teste"})
    jg = cfgmod.Job(name="t_gcs", mode="espelho", source=src, dest="backup/dados",
                    source_type="local", dest_type="gcs", validate=True)
    jg.dest_remote = grem

    st = engine.run_job(jg, log, workers=4)
    check(st.copied == 1 and not st.errors, f"enviou ao GCS (c={st.copied} err={st.errors})")

    from google.cloud import storage
    gcli = storage.Client(project="teste")
    gb = gcli.bucket("meu-bucket")
    chaves = sorted(b.name for b in gcli.list_blobs(gb))
    check(chaves == ["backup/dados/a.txt"], f"prefixo aplicado no GCS: {chaves}")
    check(gb.blob("backup/dados/a.txt").download_as_bytes() == b"CONTEUDO X",
          "conteudo integro no GCS")
    meta = gb.get_blob("backup/dados/a.txt").metadata or {}
    check(abs(float(meta.get("sincmtime", 0)) - 1_700_000_000) < 1,
          f"data original no metadado do GCS ({meta})")
    check(not st.validation_failed, f"validacao GCS ok ({st.validation_failed})")

    epg = ep.make_endpoint("backup/dados", "gcs", grem)
    ig = epg.scan()["a.txt"]
    check(abs(ig.mtime - 1_700_000_000) < 1,
          f"scan do GCS recupera a data original ({ig.mtime})")
    check(epg.preserves_mtime is True, "GCS preserva a data")
    hg = epg.content_hash("a.txt", ig)
    check(hg == hashlib.md5(b"CONTEUDO X").hexdigest(),
          f"content_hash do GCS vem do md5Hash da listagem ({hg})")

    st = engine.run_job(jg, log, workers=4)
    check(st.copied == 0 and st.updated == 0,
          f"2a passada GCS nao reenvia (c={st.copied} u={st.updated})")

    # volta: GCS -> local
    voltag = os.path.join(base, "volta_gcs")
    jgv = cfgmod.Job(name="t_gcs_volta", mode="espelho", source="backup/dados",
                     dest=voltag, source_type="gcs", dest_type="local", validate=True)
    jgv.source_remote = grem
    st = engine.run_job(jgv, log, workers=4)
    check(st.copied == 1 and not st.errors, f"baixou do GCS (c={st.copied} err={st.errors})")
    with open(os.path.join(voltag, "a.txt"), "rb") as f:
        check(f.read() == b"CONTEUDO X", "conteudo baixado do GCS confere")

    # espelho apaga no GCS
    os.remove(os.path.join(src, "a.txt"))
    st = engine.run_job(jg, log)
    check(not list(gcli.list_blobs(gb)), f"espelho apagou no GCS ({st.deleted} apagado)")
finally:
    gsrv.stop()

# ===========================================================================
shutil.rmtree(base, ignore_errors=True)
for n in ("t_az", "t_az_volta", "t_gcs", "t_gcs_volta"):
    try: os.remove(engine._state_path(n))
    except OSError: pass

print("\n" + ("AZURE E GCS: TODOS OS TESTES PASSARAM" if not falhas else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.stdout.flush()
os._exit(1 if falhas else 0)
