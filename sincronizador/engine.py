"""Motor de sincronizacao: espelho, incremental e bidirecional.

- espelho:     origem -> destino; destino fica identico (apaga o que sobra).
- incremental: origem -> destino; apenas adiciona/atualiza, nunca apaga.
- bidirecional: propaga mudancas nos dois sentidos usando um "snapshot" do
  ultimo sync para distinguir criacao de exclusao. Conflitos: vence o mais
  recente (o perdedor vai para o versionamento, se ativo).
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config as cfgmod
from .endpoints import FileInfo, make_endpoint, same_file
from .filters import allowed


# ---------------------------------------------------------------------------
# Progresso (velocidade, tempo decorrido e ETA) - compartilhado com a GUI
# ---------------------------------------------------------------------------
class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self.job = ""
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.total_known = False   # False no bidirecional (nao da p/ prever ETA)
        self.started = 0.0
        self.current = ""
        self.finished = False

    def reset(self, job_name: str):
        with self._lock:
            self.job = job_name
            self.files_total = 0
            self.files_done = 0
            self.bytes_total = 0
            self.bytes_done = 0
            self.total_known = False
            self.started = time.time()
            self.current = ""
            self.finished = False

    def set_totals(self, files: int, nbytes: int, known: bool = True):
        with self._lock:
            self.files_total = files
            self.bytes_total = nbytes
            self.total_known = known
            if not self.started:
                self.started = time.time()

    def add_done(self, size: int, name: str = ""):
        with self._lock:
            self.files_done += 1
            self.bytes_done += size
            if name:
                self.current = name

    def set_current(self, name: str):
        with self._lock:
            self.current = name

    def done(self):
        with self._lock:
            self.finished = True

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self.started) if self.started else 0.0
            speed = (self.bytes_done / elapsed) if elapsed > 0 else 0.0
            eta = None
            if self.total_known and speed > 0 and self.bytes_total > 0:
                eta = max(0.0, (self.bytes_total - self.bytes_done) / speed)
            pct = 0.0
            if self.total_known and self.bytes_total > 0:
                pct = min(100.0, 100.0 * self.bytes_done / self.bytes_total)
            elif self.total_known and self.files_total > 0:
                pct = min(100.0, 100.0 * self.files_done / self.files_total)
            return {
                "job": self.job, "elapsed": elapsed, "speed": speed, "eta": eta,
                "pct": pct, "files_total": self.files_total,
                "files_done": self.files_done, "bytes_total": self.bytes_total,
                "bytes_done": self.bytes_done, "total_known": self.total_known,
                "current": self.current, "finished": self.finished,
            }


@dataclass
class Stats:
    job: str = ""
    copied: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: int = 0
    bytes: int = 0
    errors: List[str] = field(default_factory=list)
    validated: int = 0
    validation_failed: List[str] = field(default_factory=list)
    cancelled: bool = False
    started: float = 0.0
    finished: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.validation_failed and not self.cancelled

    def summary(self) -> str:
        dur = (self.finished - self.started) if self.finished else 0
        speed = (self.bytes / dur) if dur > 0 else 0
        cancel = " [INTERROMPIDA]" if self.cancelled else ""
        return (f"[{self.job}]{cancel} copiados={self.copied} atualizados={self.updated} "
                f"apagados={self.deleted} conflitos={self.conflicts} "
                f"transferido={_human(self.bytes)} erros={len(self.errors)} "
                f"validados={self.validated} falhas_validacao={len(self.validation_failed)} "
                f"tempo={dur:.1f}s velocidade={_human(int(speed))}/s")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# ---------------------------------------------------------------------------
# Snapshot para modo bidirecional
# ---------------------------------------------------------------------------
def _state_path(job_name: str) -> str:
    d = os.path.join(cfgmod.app_dir(), "state")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in job_name)
    return os.path.join(d, safe + ".json")


def _load_state(job_name: str) -> Dict[str, FileInfo]:
    p = _state_path(job_name)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # formato antigo: [tamanho, data]; novo: [tamanho, data, etag]
        return {k: FileInfo(size=v[0], mtime=v[1],
                            etag=v[2] if len(v) > 2 else "")
                for k, v in raw.items()}
    except Exception:
        return {}


def _save_state(job_name: str, state: Dict[str, FileInfo]) -> None:
    p = _state_path(job_name)
    raw = {k: [v.size, v.mtime, v.etag] for k, v in state.items()}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Politica de comparacao (o que significa "o arquivo mudou")
# ---------------------------------------------------------------------------
class Comparer:
    """Decide se dois arquivos sao iguais, conforme job.compare e o que os
    endpoints envolvidos conseguem garantir.

    Existe porque nem todo destino aceita gravar a data original do arquivo:
    object storage (S3, GCS, Azure, B2) so devolve a data do upload. Comparar
    por data nesses casos faria o programa reenviar tudo em toda execucao.
    """

    def __init__(self, job, src, dst, logger=None):
        mode = getattr(job, "compare", "auto") or "auto"
        self.mode = mode
        self.hash_check = (mode == "conteudo")
        if mode == "data":
            self.use_mtime = True
        elif mode in ("tamanho", "conteudo"):
            self.use_mtime = False
        else:  # auto
            self.use_mtime = bool(src.preserves_mtime and dst.preserves_mtime)
        if logger is not None:
            if self.hash_check:
                logger.info("  comparacao: tamanho + conteudo (hash) - mais lento")
            elif not self.use_mtime:
                extra = "" if mode == "tamanho" else \
                    " (algum dos lados nao preserva a data original)"
                logger.info("  comparacao: so por tamanho%s", extra)

    def equal(self, fa: FileInfo, fb: FileInfo) -> bool:
        """Compara dois metadados (sem tocar no conteudo)."""
        return same_file(fa, fb, compare_mtime=self.use_mtime)

    def equal_files(self, rel: str, fa: FileInfo, fb: FileInfo, ea, eb) -> bool:
        """Como equal(), mas confere o hash quando compare='conteudo'."""
        if not self.equal(fa, fb):
            return False
        if not self.hash_check:
            return True
        try:
            ha, hb, _alg = _hashes_para_comparar(ea, eb, rel, fa, fb)
            return ha == hb
        except Exception:
            return False   # na duvida, trata como diferente e recopia

    def mtime_trustworthy(self) -> bool:
        """A data serve para desempatar conflito no modo bidirecional?"""
        return self.use_mtime


# ---------------------------------------------------------------------------
# Execucao de um job
# ---------------------------------------------------------------------------
def run_job(job: "cfgmod.Job", logger, progress: Optional[Progress] = None,
            workers: int = 1, cancel=None) -> Stats:
    st = Stats(job=job.name, started=time.time())
    if progress is not None:
        progress.reset(job.name)
    try:
        src = make_endpoint(job.source, job.source_type, job.source_remote)
        dst = make_endpoint(job.dest, job.dest_type, job.dest_remote)
    except Exception as e:
        st.errors.append(f"Falha ao conectar: {e}")
        logger.error("Falha ao conectar: %s", e)
        st.finished = time.time()
        return st

    # paralelismo depende do que cada endpoint aguenta: pastas locais e APIs
    # HTTP suportam varias threads; FTP/SFTP usam uma conexao por vez.
    if not (src.parallel_safe and dst.parallel_safe):
        workers = 1
    workers = max(1, int(workers))
    logger.info("Iniciando tarefa '%s' (%s): %s [%s] -> %s [%s]  (ate %d arquivo(s) por vez)",
                job.name, job.mode, job.source, job.source_type,
                job.dest, job.dest_type, workers)

    # nunca sincronizar/validar as proprias pastas de trabalho do programa
    # (logs, state, backup) que ficam dentro da origem/destino
    src_skip = _skip_prefixes(job.source, job.source_type, job)
    dst_skip = _skip_prefixes(job.dest, job.dest_type, job)

    # limpa temporarios (.sinctmp) que possam ter sobrado de execucoes interrompidas
    _cleanup_temp(dst, logger)

    cmp = Comparer(job, src, dst, logger)

    with src, dst:
        try:
            src_files = _scan_job(src, job, src_skip)
            dst_files = _scan_job(dst, job, dst_skip)
        except Exception as e:
            st.errors.append(f"Falha ao listar arquivos: {e}")
            logger.error("Falha ao listar arquivos: %s", e)
            st.finished = time.time()
            return st

        if job.mode == "bidirecional":
            _sync_bidirectional(job, src, dst, src_files, dst_files, st, logger,
                                src_skip, dst_skip, progress, cancel, cmp)
        else:
            _sync_oneway(job, src, dst, src_files, dst_files, st, logger,
                         mirror=(job.mode == "espelho"), workers=workers,
                         progress=progress, cancel=cancel, cmp=cmp)

        if cancel is not None and cancel.is_set():
            st.cancelled = True
            logger.warning("Sincronizacao INTERROMPIDA pelo usuario.")

        if job.validate and not st.cancelled:
            _validate(job, src, dst, st, logger, src_skip, dst_skip, cmp)

    st.finished = time.time()
    if progress is not None:
        progress.done()
    logger.info(st.summary())
    return st


# ---------------------------------------------------------------------------
# Exclusao automatica das pastas de trabalho do proprio programa
# ---------------------------------------------------------------------------
def _managed_dirs(job: "cfgmod.Job") -> set:
    dirs = {
        os.path.join(cfgmod.app_dir(), "backup"),
        os.path.join(cfgmod.app_dir(), "state"),
        os.path.join(cfgmod.app_dir(), "logs"),
    }
    if job.backup_dir:
        dirs.add(job.backup_dir)
    return dirs


def _managed_files(job: "cfgmod.Job") -> set:
    d = cfgmod.app_dir()
    return {
        os.path.join(d, "sincronizador.config.json"),
        os.path.join(d, "sincronizador.lock"),
    }


def _rel_inside(path: str, root: str):
    """Caminho relativo com '/' se 'path' estiver dentro de 'root', senao None."""
    try:
        rel = os.path.relpath(os.path.abspath(path), root)
    except ValueError:  # discos diferentes no Windows
        return None
    rel = rel.replace(os.sep, "/")
    if rel == "." or rel == ".." or rel.startswith("../"):
        return None
    return rel


def _skip_prefixes(root_path: str, kind: str, job: "cfgmod.Job") -> list:
    """Itens (relpath) a ignorar por serem do proprio programa.
    Pastas terminam com '/'; arquivos sao o relpath exato."""
    if kind != "local" or not root_path:
        return []
    root = os.path.abspath(root_path)
    out = []
    for d in _managed_dirs(job):
        rel = _rel_inside(d, root)
        if rel:
            out.append(rel.rstrip("/") + "/")
    for f in _managed_files(job):
        rel = _rel_inside(f, root)
        if rel:
            out.append(rel)
    return out


def _is_managed(rel: str, prefixes: list) -> bool:
    r = rel.replace("\\", "/")
    for p in prefixes:
        if p.endswith("/"):
            if r.startswith(p):
                return True
        elif r == p:
            return True
    return False


def _scan_job(endpoint, job: "cfgmod.Job", prefixes: list) -> dict:
    return {k: v for k, v in endpoint.scan().items()
            if not k.endswith(".sinctmp")
            and allowed(k, job.include, job.exclude) and not _is_managed(k, prefixes)}


def _cleanup_temp(endpoint, logger) -> None:
    """Remove arquivos temporarios .sinctmp deixados por execucoes interrompidas."""
    if not getattr(endpoint, "is_local", False):
        return
    root = getattr(endpoint, "root", None)
    if not root or not os.path.isdir(root):
        return
    removed = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".sinctmp"):
                try:
                    os.remove(os.path.join(dirpath, fn))
                    removed += 1
                except OSError:
                    pass
    if removed:
        logger.info("  limpou %d arquivo(s) temporario(s) de execucao anterior.", removed)


def _backup_base(job: "cfgmod.Job", which: str) -> str:
    base = job.backup_dir or os.path.join(cfgmod.app_dir(), "backup")
    return os.path.join(base, _safe(job.name), which)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in s)


def _copy(src, dst, rel: str, info: FileInfo, st: Stats, logger, action: str,
          lock=None) -> None:
    try:
        fobj = src.open_read(rel)
        try:
            dst.write(rel, fobj, info.size, info.mtime)
        finally:
            try:
                fobj.close()
            except Exception:
                pass
        if lock is not None:
            with lock:
                st.bytes += info.size
                if action.startswith("novo"):
                    st.copied += 1
                else:
                    st.updated += 1
        else:
            st.bytes += info.size
            if action.startswith("novo"):
                st.copied += 1
            else:
                st.updated += 1
        logger.info("  %s: %s (%s)", action, rel, _human(info.size))
    except Exception as e:
        if lock is not None:
            with lock:
                st.errors.append(f"{rel}: {e}")
        else:
            st.errors.append(f"{rel}: {e}")
        logger.error("  ERRO ao copiar %s: %s", rel, e)


def _delete(dst, rel: str, st: Stats, logger, job, side: str) -> None:
    try:
        if job.versioning:
            dst.move_to_backup(rel, _backup_base(job, side + "_apagados"))
        dst.delete(rel)
        st.deleted += 1
        logger.info("  apagado (%s): %s", side, rel)
    except Exception as e:
        st.errors.append(f"delete {rel}: {e}")
        logger.error("  ERRO ao apagar %s: %s", rel, e)


# ---------------------------------------------------------------------------
# Uma via (espelho / incremental)
# ---------------------------------------------------------------------------
def _sync_oneway(job, src, dst, src_files, dst_files, st, logger, mirror: bool,
                 workers: int = 1, progress: Optional[Progress] = None, cancel=None,
                 cmp: Optional["Comparer"] = None):
    if cmp is None:
        cmp = Comparer(job, src, dst)

    def cancelled():
        return cancel is not None and cancel.is_set()

    # monta a lista de copias (novos + atualizados)
    tasks = []  # (rel, info, action, need_version)
    for rel, info in sorted(src_files.items()):
        if rel not in dst_files:
            tasks.append((rel, info, "novo", False))
        elif not cmp.equal_files(rel, info, dst_files[rel], src, dst):
            tasks.append((rel, info, "atualizado", job.versioning))

    if progress is not None:
        progress.set_totals(len(tasks), sum(t[1].size for t in tasks), known=True)

    def do_task(t, lock):
        if cancelled():
            return
        rel, info, action, need_version = t
        if progress is not None:
            progress.set_current(rel)
        if need_version:
            try:
                dst.move_to_backup(rel, _backup_base(job, "destino_versoes"))
            except Exception:
                pass
        _copy(src, dst, rel, info, st, logger, action, lock)
        if progress is not None:
            progress.add_done(info.size, rel)

    if workers <= 1 or len(tasks) <= 1:
        for t in tasks:
            if cancelled():
                break
            do_task(t, None)
    else:
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda t: do_task(t, lock), tasks))

    if mirror and not cancelled():
        for rel in sorted(dst_files):
            if cancelled():
                break
            if rel not in src_files:
                _delete(dst, rel, st, logger, job, "destino")


# ---------------------------------------------------------------------------
# Bidirecional
# ---------------------------------------------------------------------------
def _sync_bidirectional(job, a, b, a_files, b_files, st, logger, a_skip=None,
                        b_skip=None, progress: Optional[Progress] = None, cancel=None,
                        cmp: Optional["Comparer"] = None):
    """a = origem, b = destino. Simetrico."""
    global _bidi_progress
    _bidi_progress = progress
    if cmp is None:
        cmp = Comparer(job, a, b)
    if not cmp.mtime_trustworthy():
        logger.warning("  atencao: sem data confiavel nos dois lados, conflitos "
                       "serao resolvidos a favor da origem.")
    if progress is not None:
        # nao da p/ prever o total no bidirecional (decisoes dependem do snapshot)
        progress.set_totals(0, 0, known=False)
    prev = _load_state(job.name)
    all_rel = set(a_files) | set(b_files) | set(prev)

    for rel in sorted(all_rel):
        if cancel is not None and cancel.is_set():
            break
        in_a = rel in a_files
        in_b = rel in b_files
        in_prev = rel in prev
        fa = a_files.get(rel)
        fb = b_files.get(rel)

        changed_a = in_a and (not in_prev or not cmp.equal(fa, prev[rel]))
        changed_b = in_b and (not in_prev or not cmp.equal(fb, prev[rel]))

        # ----- ambos presentes -----
        if in_a and in_b:
            if cmp.equal_files(rel, fa, fb, a, b):
                continue
            if changed_a and not changed_b:
                _push(job, a, b, rel, fa, st, logger, "A->B", version_side="destino_versoes", vend=b)
            elif changed_b and not changed_a:
                _push(job, b, a, rel, fb, st, logger, "B->A", version_side="origem_versoes", vend=a)
            else:
                # conflito real: vence o mais recente, perdedor para backup.
                # Sem data confiavel nao da para saber quem eh mais novo:
                # nesse caso a origem vence (criterio previsivel).
                st.conflicts += 1
                if not cmp.mtime_trustworthy() or fa.mtime >= fb.mtime:
                    logger.warning("  CONFLITO %s: vence A (%s)", rel,
                                   "mais novo" if cmp.mtime_trustworthy() else "origem")
                    _push(job, a, b, rel, fa, st, logger, "A->B (conflito)",
                          version_side="destino_versoes", vend=b, force_version=True)
                else:
                    logger.warning("  CONFLITO %s: B mais novo, vence B", rel)
                    _push(job, b, a, rel, fb, st, logger, "B->A (conflito)",
                          version_side="origem_versoes", vend=a, force_version=True)
            continue

        # ----- so em A -----
        if in_a and not in_b:
            if in_prev and not changed_a:
                # existia e nao mudou em A, sumiu em B -> apagar em A
                _delete(a, rel, st, logger, job, "origem")
            else:
                # novo em A (ou mudou) -> copiar para B
                _push(job, a, b, rel, fa, st, logger, "A->B (novo)", version_side="destino_versoes", vend=b)
            continue

        # ----- so em B -----
        if in_b and not in_a:
            if in_prev and not changed_b:
                _delete(b, rel, st, logger, job, "destino")
            else:
                _push(job, b, a, rel, fb, st, logger, "B->A (novo)", version_side="origem_versoes", vend=a)
            continue

        # so no snapshot antigo (sumiu dos dois lados) -> nada a fazer

    # snapshot so eh salvo se a sincronizacao terminou (senao registraria estado parcial)
    if cancel is None or not cancel.is_set():
        new_state = _rescan_union(a, b, job, a_skip or [], b_skip or [])
        _save_state(job.name, new_state)
    else:
        logger.warning("  snapshot NAO atualizado (sincronizacao interrompida).")
    _bidi_progress = None


# referencia de progresso usada pelo _push no modo bidirecional (sequencial)
_bidi_progress: Optional[Progress] = None


def _push(job, src, dst, rel, info, st, logger, tag, version_side, vend, force_version=False):
    if _bidi_progress is not None:
        _bidi_progress.set_current(rel)
    if job.versioning and (force_version or rel in _existing(vend)):
        try:
            vend.move_to_backup(rel, _backup_base(job, version_side))
        except Exception:
            pass
    _copy(src, dst, rel, info, st, logger, tag)
    if _bidi_progress is not None:
        _bidi_progress.add_done(info.size, rel)


# cache leve de existencia no destino para versionamento no bidirecional
_exist_cache: Dict[int, set] = {}


def _existing(endpoint) -> set:
    key = id(endpoint)
    if key not in _exist_cache:
        try:
            _exist_cache[key] = set(endpoint.scan().keys())
        except Exception:
            _exist_cache[key] = set()
    return _exist_cache[key]


def _rescan_union(a, b, job, a_skip=None, b_skip=None) -> Dict[str, FileInfo]:
    _exist_cache.clear()
    try:
        af = _scan_job(a, job, a_skip or [])
    except Exception:
        af = {}
    try:
        bf = _scan_job(b, job, b_skip or [])
    except Exception:
        bf = {}
    merged = dict(bf)
    merged.update(af)  # origem tem prioridade no registro do snapshot
    return merged


# ---------------------------------------------------------------------------
# Validacao pos-sincronizacao
# ---------------------------------------------------------------------------
def _hash_pronto(endpoint, rel: str, info: Optional[FileInfo] = None) -> str:
    """MD5 que o servico ja' informou na listagem, ou "" se nao informou.

    S3/Azure/GCS entregam o MD5 do objeto junto com os metadados, entao para
    esses nao ha o que baixar - e o algoritmo e' escolha deles, nao nossa.
    """
    try:
        return endpoint.content_hash(rel, info) or ""
    except Exception:
        return ""


def _hash_calculado(endpoint, rel: str, algoritmo: str) -> str:
    """Le o arquivo e calcula o hash no algoritmo pedido."""
    import hashlib
    h = hashlib.new(algoritmo)
    fobj = endpoint.open_read(rel)
    try:
        while True:
            chunk = fobj.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    finally:
        try:
            fobj.close()
        except Exception:
            pass
    return h.hexdigest()


def _hashes_para_comparar(ea, eb, rel: str, fa=None, fb=None) -> tuple[str, str, str]:
    """Devolve (hash_a, hash_b, algoritmo) comparaveis entre os dois lados.

    Por que negociar em vez de fixar MD5: MD5 tem colisao construivel, ou seja,
    e' possivel fabricar dois arquivos DIFERENTES com o mesmo hash. Para
    "mudou desde a ultima vez?" isso e' irrelevante; para a VALIDACAO final,
    que existe para afirmar "o destino e' igual a origem", um destino hostil
    poderia satisfazer a checagem com conteudo trocado.

    Nao da' para simplesmente usar sha256: quando um lado e' object storage, o
    hash vem pronto na listagem e e' MD5 - trocar o algoritmo forcaria baixar o
    arquivo inteiro dos dois lados a cada validacao. Entao a regra e':

      - algum lado so' tem MD5 pronto  -> MD5 nos dois (sem download extra);
      - nenhum lado tem hash pronto    -> sha256 nos dois, que ja' vamos ler
                                          os bytes de qualquer forma.

    O custo de sha256 sobre bytes que ja' estao sendo lidos e' desprezivel.
    """
    pronto_a = _hash_pronto(ea, rel, fa)
    pronto_b = _hash_pronto(eb, rel, fb)

    if pronto_a or pronto_b:
        # Um lado imposto em MD5 obriga o outro a acompanhar.
        ha = pronto_a or _hash_calculado(ea, rel, "md5")
        hb = pronto_b or _hash_calculado(eb, rel, "md5")
        return ha, hb, "md5"

    return (_hash_calculado(ea, rel, "sha256"),
            _hash_calculado(eb, rel, "sha256"),
            "sha256")


def _hash_of(endpoint, rel: str, info: Optional[FileInfo] = None) -> str:
    """MD5 de um lado so'. Mantido para quem compara um endpoint isolado."""
    return _hash_pronto(endpoint, rel, info) or _hash_calculado(endpoint, rel, "md5")


def _validate(job, src, dst, st: Stats, logger, src_skip=None, dst_skip=None,
              cmp: Optional["Comparer"] = None) -> None:
    """Confere se os arquivos ficaram iguais entre origem e destino.

    - espelho:      destino deve conter exatamente os arquivos da origem.
    - incremental:  todo arquivo da origem deve existir no destino (destino pode ter extras).
    - bidirecional: os dois lados devem ter o mesmo conjunto e mesmos arquivos.
    Compara tamanho (+ data); se validate_hash, compara tambem o conteudo (md5).
    """
    if cmp is None:
        cmp = Comparer(job, src, dst)
    logger.info("  validando arquivos sincronizados%s...",
                " (por conteudo/hash)" if job.validate_hash else "")
    try:
        a = _scan_job(src, job, src_skip or [])
        b = _scan_job(dst, job, dst_skip or [])
    except Exception as e:
        st.validation_failed.append(f"falha ao reler para validacao: {e}")
        logger.error("  ERRO na validacao: %s", e)
        return

    criterio = "tamanho/data" if cmp.use_mtime else "tamanho"

    def check_pair(rel, ea, eb, fa, fb):
        if not cmp.equal(fa, fb):
            st.validation_failed.append(
                f"{rel}: {criterio} diferentes ({fa.size} vs {fb.size})")
            logger.error("  VALIDACAO FALHOU: %s (%s)", rel, criterio)
            return
        if job.validate_hash:
            try:
                ha, hb, alg = _hashes_para_comparar(ea, eb, rel, fa, fb)
                if ha != hb:
                    st.validation_failed.append(f"{rel}: conteudo ({alg}) diferente")
                    logger.error("  VALIDACAO FALHOU: %s (%s)", rel, alg)
                    return
            except Exception as e:
                st.validation_failed.append(f"{rel}: erro ao calcular hash ({e})")
                return
        st.validated += 1

    if job.mode == "incremental":
        for rel, fa in a.items():
            if rel not in b:
                st.validation_failed.append(f"{rel}: ausente no destino")
                logger.error("  VALIDACAO FALHOU: %s ausente no destino", rel)
            else:
                check_pair(rel, src, dst, fa, b[rel])
    elif job.mode == "espelho":
        for rel, fa in a.items():
            if rel not in b:
                st.validation_failed.append(f"{rel}: ausente no destino")
                logger.error("  VALIDACAO FALHOU: %s ausente no destino", rel)
            else:
                check_pair(rel, src, dst, fa, b[rel])
        for rel in b:
            if rel not in a:
                st.validation_failed.append(f"{rel}: sobrou no destino (deveria ter sido apagado)")
                logger.error("  VALIDACAO FALHOU: %s sobrou no destino", rel)
    else:  # bidirecional
        for rel in set(a) | set(b):
            if rel not in a:
                st.validation_failed.append(f"{rel}: ausente na origem")
                logger.error("  VALIDACAO FALHOU: %s ausente na origem", rel)
            elif rel not in b:
                st.validation_failed.append(f"{rel}: ausente no destino")
                logger.error("  VALIDACAO FALHOU: %s ausente no destino", rel)
            else:
                check_pair(rel, src, dst, a[rel], b[rel])

    if not st.validation_failed:
        logger.info("  validacao OK: %d arquivo(s) conferido(s).", st.validated)
    else:
        logger.error("  validacao com %d falha(s).", len(st.validation_failed))


# ---------------------------------------------------------------------------
# Rodar varios jobs
# ---------------------------------------------------------------------------
def run_jobs(jobs: List["cfgmod.Job"], logger, progress: Optional[Progress] = None,
             workers: int = 1, cancel=None) -> List[Stats]:
    results = []
    for job in jobs:
        if cancel is not None and cancel.is_set():
            break
        if not job.enabled:
            logger.info("Tarefa '%s' desativada, pulando.", job.name)
            continue
        results.append(run_job(job, logger, progress=progress, workers=workers, cancel=cancel))
    return results
