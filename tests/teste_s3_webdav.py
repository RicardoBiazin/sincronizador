"""Testes de integracao dos backends de nuvem.

S3    -> moto (implementacao S3 de verdade, em processo)
WebDAV-> wsgidav servindo uma pasta temporaria, via HTTP de verdade
"""
import logging, os, shutil, sys, tempfile, threading, time

sys.path.insert(0, r"c:\DEV\sincronizador")
from sincronizador import config as cfgmod, endpoints as ep, engine

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("t")
log.setLevel(logging.INFO)

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


def povoar(raiz):
    os.makedirs(os.path.join(raiz, "sub"), exist_ok=True)
    dados = {"a.txt": b"conteudo A", "sub/b.bin": bytes(range(256)) * 40,
             "sub/c.txt": b"terceiro"}
    for n, d in dados.items():
        p = os.path.join(raiz, n.replace("/", os.sep))
        with open(p, "wb") as f:
            f.write(d)
        os.utime(p, (1_600_000_000, 1_600_000_000))
    return dados


base = tempfile.mkdtemp(prefix="sinc_nuvem_")
src = os.path.join(base, "origem")
os.makedirs(src)
dados = povoar(src)

# ===========================================================================
# S3 (moto)
# ===========================================================================
print("\n=== Amazon S3 (moto) ===")
import boto3
from moto import mock_aws

mock = mock_aws()
mock.start()
boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="meu-bucket")

rem = cfgmod.Remote(user="AK", password="SK",
                    options={"bucket": "meu-bucket", "region": "us-east-1"})
job = cfgmod.Job(name="t_s3", mode="espelho", source=src, dest="backup/dados",
                 source_type="local", dest_type="s3", validate=True)
job.dest_remote = rem

st = engine.run_job(job, log, workers=4)
check(st.copied == 3 and not st.errors, f"enviou 3 objetos (c={st.copied} err={st.errors})")

cli = boto3.client("s3", region_name="us-east-1")
keys = sorted(o["Key"] for o in cli.list_objects_v2(Bucket="meu-bucket")["Contents"])
check(keys == ["backup/dados/a.txt", "backup/dados/sub/b.bin", "backup/dados/sub/c.txt"],
      f"prefixo aplicado nas chaves: {keys}")
body = cli.get_object(Bucket="meu-bucket", Key="backup/dados/a.txt")["Body"].read()
check(body == dados["a.txt"], "conteudo enviado confere")
meta = cli.head_object(Bucket="meu-bucket", Key="backup/dados/a.txt")["Metadata"]
check(abs(float(meta.get("sincmtime", 0)) - 1_600_000_000) < 1,
      f"data original gravada no metadado ({meta})")

check(not st.validation_failed, f"validacao passou (falhas={st.validation_failed})")

st = engine.run_job(job, log, workers=4)
check(st.copied == 0 and st.updated == 0,
      f"2a passada nao reenvia (c={st.copied} u={st.updated})")

# paralelismo: S3 aguenta varias threads
recs = []
h = logging.Handler(); h.emit = lambda r: recs.append(r.getMessage())
log.addHandler(h)
engine.run_job(job, log, workers=4)
check(any("ate 4 arquivo(s) por vez" in m for m in recs), "S3 mantem 4 threads")
log.removeHandler(h)

# head_mtime: le a data original de volta e compara por data
job.dest_remote.options["head_mtime"] = True
job.compare = "auto"
st = engine.run_job(job, log)
check(st.copied == 0 and st.updated == 0 and not st.validation_failed,
      f"head_mtime: reconhece pela data original (c={st.copied} u={st.updated} v={st.validation_failed})")
ep_s3 = ep.make_endpoint("backup/dados", "s3", job.dest_remote)
info = ep_s3.scan()["a.txt"]
check(abs(info.mtime - 1_600_000_000) < 1, f"head_mtime devolve a data original ({info.mtime})")
check(ep_s3.preserves_mtime is True, "head_mtime liga preserves_mtime")
job.dest_remote.options["head_mtime"] = False

# hash vem da listagem (ETag), sem baixar nada
import hashlib
esperado = hashlib.md5(dados["a.txt"]).hexdigest()
ep_s3b = ep.make_endpoint("backup/dados", "s3", job.dest_remote)
i2 = ep_s3b.scan()["a.txt"]
check(ep_s3b.content_hash("a.txt", i2) == esperado,
      "content_hash sai da listagem (ETag) sem baixar")

# comparacao por conteudo detecta alteracao de mesmo tamanho
job.compare = "conteudo"
alvo = os.path.join(src, "a.txt")
with open(alvo, "wb") as f:
    f.write(b"CONTEUDO X")
os.utime(alvo, (1_600_000_000, 1_600_000_000))
st = engine.run_job(job, log)
check(st.updated == 1, f"compare=conteudo pega mudanca de mesmo tamanho/data (u={st.updated})")
novo = cli.get_object(Bucket="meu-bucket", Key="backup/dados/a.txt")["Body"].read()
check(novo == b"CONTEUDO X", "conteudo atualizado no S3")
job.compare = "auto"

# espelho apaga no destino
os.remove(os.path.join(src, "sub", "c.txt"))
st = engine.run_job(job, log)
keys = sorted(o["Key"] for o in cli.list_objects_v2(Bucket="meu-bucket")["Contents"])
check("backup/dados/sub/c.txt" not in keys, f"espelho apagou no S3 ({st.deleted} apagado)")

# volta na outra direcao: S3 -> local
print("\n=== S3 -> local ===")
volta = os.path.join(base, "volta")
jb = cfgmod.Job(name="t_s3_volta", mode="espelho", source="backup/dados", dest=volta,
                source_type="s3", dest_type="local", validate=True)
jb.source_remote = rem
st = engine.run_job(jb, log, workers=4)
check(st.copied == 2 and not st.errors, f"baixou 2 do S3 (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "sub", "b.bin"), "rb") as f:
    check(f.read() == dados["sub/b.bin"], "binario voltou intacto do S3")
check(not st.validation_failed, f"validacao do download ok ({st.validation_failed})")

# probe
bad = cfgmod.Remote(user="AK", password="SK", options={"bucket": "nao-existe"})
try:
    e = ep.make_endpoint("", "s3", bad); e.probe(); ok = False
except Exception:
    ok = True
check(ok, "probe() falha em bucket inexistente")

mock.stop()

# ===========================================================================
# WebDAV (wsgidav de verdade, por HTTP)
# ===========================================================================
print("\n=== WebDAV (wsgidav) ===")
from wsgidav.wsgidav_app import WsgiDAVApp
from wsgidav.fs_dav_provider import FilesystemProvider
from cheroot import wsgi as cheroot_wsgi

dav_root = os.path.join(base, "davroot")
os.makedirs(os.path.join(dav_root, "area"))
conf = {
    "provider_mapping": {"/": FilesystemProvider(dav_root, readonly=False)},
    "simple_dc": {"user_mapping": {"*": {"ric": {"password": "senha"}}}},
    "http_authenticator": {"accept_basic": True, "accept_digest": False,
                           "default_to_digest": False},
    "verbose": 0,
    "logging": {"enable": False},
    "property_manager": True,
    "lock_storage": True,
}
srv = cheroot_wsgi.Server(("127.0.0.1", 0), WsgiDAVApp(conf))
srv.prepare()
porta = srv.bind_addr[1]
threading.Thread(target=srv.serve, daemon=True).start()
time.sleep(0.4)
print("  servidor WebDAV em 127.0.0.1:%d" % porta)

dav_rem = cfgmod.Remote(user="ric", password="senha",
                        options={"base_url": "http://127.0.0.1:%d" % porta,
                                 "verify_tls": True})
jd = cfgmod.Job(name="t_dav", mode="espelho", source=src, dest="area/envio",
                source_type="local", dest_type="webdav", validate=True)
jd.dest_remote = dav_rem

st = engine.run_job(jd, log)
check(st.copied == 2 and not st.errors, f"enviou 2 arquivos por WebDAV (c={st.copied} err={st.errors})")
disco = os.path.join(dav_root, "area", "envio")
check(os.path.exists(os.path.join(disco, "a.txt")) and
      os.path.exists(os.path.join(disco, "sub", "b.bin")),
      "arquivos e subpasta criados no servidor")
with open(os.path.join(disco, "sub", "b.bin"), "rb") as f:
    check(f.read() == dados["sub/b.bin"], "binario integro via WebDAV")
check(not st.validation_failed, f"validacao WebDAV ok ({st.validation_failed})")

st = engine.run_job(jd, log)
check(st.copied == 0 and st.updated == 0,
      f"2a passada WebDAV nao reenvia (c={st.copied} u={st.updated})")

epd = ep.make_endpoint("area/envio", "webdav", dav_rem)
check(epd.preserves_mtime is False, "WebDAV declara que nao preserva a data")
achados = sorted(epd.scan())
check(achados == ["a.txt", "sub/b.bin"], f"scan recursivo WebDAV: {achados}")

# download WebDAV -> local
volta2 = os.path.join(base, "volta_dav")
jd2 = cfgmod.Job(name="t_dav_volta", mode="espelho", source="area/envio", dest=volta2,
                 source_type="webdav", dest_type="local", validate=True)
jd2.source_remote = dav_rem
st = engine.run_job(jd2, log)
check(st.copied == 2 and not st.errors, f"baixou 2 por WebDAV (c={st.copied} err={st.errors})")
with open(os.path.join(volta2, "a.txt"), "rb") as f:
    check(f.read() == b"CONTEUDO X", "conteudo baixado por WebDAV confere")

# espelho apaga no WebDAV
os.remove(os.path.join(src, "a.txt"))
st = engine.run_job(jd, log)
check(not os.path.exists(os.path.join(disco, "a.txt")),
      f"espelho apagou no WebDAV ({st.deleted} apagado)")

# probe com senha errada
ruim = cfgmod.Remote(user="ric", password="errada",
                     options={"base_url": "http://127.0.0.1:%d" % porta})
try:
    e = ep.make_endpoint("area", "webdav", ruim); e.probe(); ok = False
except Exception:
    ok = True
check(ok, "probe() WebDAV falha com senha errada")

srv.stop()

# ===========================================================================
shutil.rmtree(base, ignore_errors=True)
for n in ("t_s3", "t_s3_volta", "t_dav", "t_dav_volta"):
    try: os.remove(engine._state_path(n))
    except OSError: pass

print("\n" + ("TODOS OS TESTES DE NUVEM PASSARAM" if not falhas else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.stdout.flush()
# cheroot deixa threads de trabalho vivas; encerra sem esperar por elas
os._exit(1 if falhas else 0)
