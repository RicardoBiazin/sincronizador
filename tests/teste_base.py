"""Testa o refactor de base do Sincronizador."""
import io, logging, os, shutil, sys, tempfile, time

sys.path.insert(0, r"c:\DEV\sincronizador")
from sincronizador import config as cfgmod, endpoints as ep, engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("t")


# --- endpoint ficticio tipo object storage: nao preserva data --------------
class FakeObjEndpoint(ep.Endpoint):
    preserves_mtime = False
    parallel_safe = True
    has_dirs = False
    STORES = {}

    def __init__(self, root, remote):
        self.data = FakeObjEndpoint.STORES.setdefault(root, {})
        self.bucket = remote.opt("bucket", "?")

    def scan(self):
        # devolve sempre a data do "upload", como S3/GCS fazem
        return {k: ep.FileInfo(size=len(v[0]), mtime=v[1]) for k, v in self.data.items()}

    def open_read(self, rel):
        return io.BytesIO(self.data[rel][0])

    def write(self, rel, fobj, size, mtime):
        self.data[rel] = (fobj.read(), 1_700_000_000.0)  # data do upload, nao a original

    def delete(self, rel):
        self.data.pop(rel, None)

    def move_to_backup(self, rel, backup_base):
        pass

    def probe(self):
        pass


ep.register(ep.EndpointSpec(
    kind="fakeobj", label="Object storage (teste)",
    factory=lambda path, remote: FakeObjEndpoint(path, remote),
    path_label="Prefixo:",
    fields=[ep.Field("bucket", "Bucket", required=True),
            ep.Field("region", "Regiao")],
))


class FakeSerialEndpoint(FakeObjEndpoint):
    parallel_safe = False


ep.register(ep.EndpointSpec(kind="fakeserial", label="Serial (teste)",
                            factory=lambda p, r: FakeSerialEndpoint(p, r)))

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


base = tempfile.mkdtemp(prefix="sinc_t_")
src = os.path.join(base, "origem")
dst = os.path.join(base, "destino")
os.makedirs(os.path.join(src, "sub"))
for n, txt in [("a.txt", b"conteudo A"), ("sub/b.txt", b"conteudo B" * 100)]:
    p = os.path.join(src, n.replace("/", os.sep))
    with open(p, "wb") as f:
        f.write(txt)
    os.utime(p, (1_600_000_000, 1_600_000_000))

# --- 1: registro descobre os tipos ----------------------------------------
print("\n[1] registro de tipos")
check("fakeobj" in cfgmod.endpoint_types(), "tipo novo aparece em endpoint_types()")
check([f.key for f in ep.endpoint_fields("fakeobj")] == ["bucket", "region"],
      "campos do tipo novo vem do registro")
check(ep.get_spec("fakeobj").path_label == "Prefixo:", "rotulo do caminho vem do registro")
try:
    ep.make_endpoint("x", "naoexiste", cfgmod.Remote())
    check(False, "tipo desconhecido levanta erro")
except ValueError:
    check(True, "tipo desconhecido levanta erro")

# --- 2: local -> local, segunda passada nao recopia ------------------------
print("\n[2] local -> local (espelho)")
job = cfgmod.Job(name="t_local", mode="espelho", source=src, dest=dst,
                 source_type="local", dest_type="local", validate=True)
st = engine.run_job(job, log, workers=4)
check(st.copied == 2 and not st.errors, f"copiou 2 arquivos (copiou={st.copied}, erros={st.errors})")
check(st.validated == 2, f"validou 2 (validou={st.validated}, falhas={st.validation_failed})")
st = engine.run_job(job, log, workers=4)
check(st.copied == 0 and st.updated == 0, f"2a passada nao recopia (c={st.copied} u={st.updated})")

# --- 3: destino que nao preserva data (o bug que o refactor corrige) -------
print("\n[3] local -> object storage sem data (compare=auto)")
job2 = cfgmod.Job(name="t_obj", mode="espelho", source=src, dest="bkt/pasta",
                  source_type="local", dest_type="fakeobj", validate=True,
                  compare="auto")
job2.dest_remote = cfgmod.Remote(options={"bucket": "bkt"})
st = engine.run_job(job2, log)
check(st.copied == 2, f"1a passada envia 2 (copiou={st.copied}, erros={st.errors})")
st = engine.run_job(job2, log)
check(st.copied == 0 and st.updated == 0,
      f"2a passada NAO reenvia mesmo sem data preservada (c={st.copied} u={st.updated})")
check(not st.validation_failed, f"validacao passa sem data (falhas={st.validation_failed})")

# com compare='data' o mesmo cenario reenvia tudo (comportamento antigo)
job2.compare = "data"
st = engine.run_job(job2, log)
check(st.updated == 2, f"compare='data' reenvia tudo, como antes (u={st.updated})")

# --- 4: compare='conteudo' pega alteracao de mesmo tamanho -----------------
print("\n[4] compare=conteudo")
job3 = cfgmod.Job(name="t_hash", mode="espelho", source=src, dest=dst,
                  source_type="local", dest_type="local", compare="conteudo",
                  validate=False)
engine.run_job(job3, log)
alvo = os.path.join(src, "a.txt")
with open(alvo, "wb") as f:
    f.write(b"CONTEUDO X")           # mesmo tamanho, mesma data
os.utime(alvo, (1_600_000_000, 1_600_000_000))
st = engine.run_job(job3, log)
check(st.updated == 1, f"detecta mudanca de mesmo tamanho/data (u={st.updated})")
with open(os.path.join(dst, "a.txt"), "rb") as f:
    check(f.read() == b"CONTEUDO X", "conteudo chegou correto no destino")

# --- 5: paralelismo decidido pela capacidade do endpoint -------------------
print("\n[5] paralelismo por capacidade")
class Cap:
    def __init__(self, r):
        self.recs = r
    def __call__(self, rec):
        self.recs.append(rec.getMessage())

recs = []
h = logging.Handler(); h.emit = lambda rec: recs.append(rec.getMessage())
log.addHandler(h)
jp = cfgmod.Job(name="t_par", mode="espelho", source=src, dest="b2/p",
                source_type="local", dest_type="fakeobj", validate=False)
jp.dest_remote = cfgmod.Remote(options={"bucket": "b2"})
engine.run_job(jp, log, workers=4)
check(any("ate 4 arquivo(s) por vez" in m for m in recs),
      "destino paralelizavel mantem 4 threads")
recs.clear()
js = cfgmod.Job(name="t_ser", mode="espelho", source=src, dest="b3/p",
                source_type="local", dest_type="fakeserial", validate=False)
engine.run_job(js, log, workers=4)
check(any("ate 1 arquivo(s) por vez" in m for m in recs),
      "destino serial cai para 1 thread")
log.removeHandler(h)

# --- 6: snapshot bidirecional com etag -------------------------------------
print("\n[6] snapshot com etag (formato novo e antigo)")
engine._save_state("t_snap", {"x": ep.FileInfo(size=5, mtime=1.0, etag="abc")})
got = engine._load_state("t_snap")
check(got["x"].etag == "abc", "etag sobrevive ao snapshot")
import json
with open(engine._state_path("t_snap"), "w") as f:
    json.dump({"x": [5, 1.0]}, f)            # formato antigo, sem etag
check(engine._load_state("t_snap")["x"].etag == "", "snapshot antigo ainda carrega")

# --- 7: bidirecional continua funcionando ----------------------------------
print("\n[7] bidirecional")
d1 = os.path.join(base, "bi1"); d2 = os.path.join(base, "bi2")
os.makedirs(d1); os.makedirs(d2)
with open(os.path.join(d1, "um.txt"), "wb") as f: f.write(b"um")
with open(os.path.join(d2, "dois.txt"), "wb") as f: f.write(b"dois")
jb = cfgmod.Job(name="t_bi", mode="bidirecional", source=d1, dest=d2,
                source_type="local", dest_type="local", validate=True)
st = engine.run_job(jb, log)
check(os.path.exists(os.path.join(d2, "um.txt")) and os.path.exists(os.path.join(d1, "dois.txt")),
      "bidirecional trocou os dois arquivos")
check(not st.validation_failed, f"validacao bidirecional ok ({st.validation_failed})")
st = engine.run_job(jb, log)
check(st.copied == 0 and st.updated == 0, f"bidirecional estavel na 2a passada (c={st.copied} u={st.updated})")

# --- 8: config round-trip ---------------------------------------------------
print("\n[8] persistencia da configuracao")
cfg = cfgmod.AppConfig(jobs=[job2])
p = os.path.join(base, "cfg.json")
cfgmod.save_config(cfg, p)
back = cfgmod.load_config(p)
check(back.jobs[0].dest_remote.opt("bucket") == "bkt", "options sobrevive ao salvar/carregar")
check(back.jobs[0].compare == "data", "compare sobrevive ao salvar/carregar")

shutil.rmtree(base, ignore_errors=True)
for n in ("t_local", "t_obj", "t_hash", "t_par", "t_ser", "t_snap", "t_bi"):
    try: os.remove(engine._state_path(n))
    except OSError: pass

print("\n" + ("TODOS OS TESTES PASSARAM" if not falhas else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.exit(1 if falhas else 0)
