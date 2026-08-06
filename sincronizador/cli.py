"""Entrada de linha de comando (modo silencioso para o Agendador do Windows).

Uso:
  Sincronizador.exe                 -> abre a interface grafica
  Sincronizador.exe --all           -> roda todas as tarefas ativas (silencioso)
  Sincronizador.exe --run "Nome"    -> roda uma tarefa especifica
  Sincronizador.exe --list          -> lista as tarefas
  Sincronizador.exe --config CAMINHO -> usa outro arquivo de configuracao
"""
from __future__ import annotations

import argparse
import os
import sys

from . import config as cfgmod
from . import engine
from . import notify


def _lock_path() -> str:
    return os.path.join(cfgmod.app_dir(), "sincronizador.lock")


def _pid_alive(pid: int) -> bool:
    """True se existe um processo em execucao com esse PID (Windows)."""
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False  # processo nao existe -> lock orfao
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def _acquire_lock(logger) -> bool:
    """Evita duas SINCRONIZACOES simultaneas. Detecta locks orfaos pelo PID."""
    p = _lock_path()
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                pid = int((f.read().strip() or "0"))
        except Exception:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            logger.error("Ja existe uma sincronizacao em andamento (processo %d). Abortando.", pid)
            return False
        # lock orfao (processo morreu sem liberar): assume o controle
    try:
        with open(p, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def _release_lock() -> None:
    try:
        os.remove(_lock_path())
    except OSError:
        pass


def run_silent(config_path: str, job_name: str | None, run_all: bool) -> int:
    cfg = cfgmod.load_config(config_path)
    logger = notify.setup_logger(cfg.log_dir, to_console=True)
    notify.cleanup_old_logs(cfg.log_dir, cfg.log_keep_days)

    if not _acquire_lock(logger):
        return 2
    try:
        if run_all:
            jobs = [j for j in cfg.jobs if j.enabled]
        else:
            job = cfg.get_job(job_name)
            if not job:
                logger.error("Tarefa '%s' nao encontrada.", job_name)
                return 3
            jobs = [job]

        if not jobs:
            logger.warning("Nenhuma tarefa para executar.")
            return 0

        results = engine.run_jobs(jobs, logger, workers=cfg.parallel)
        notify.maybe_notify(cfg.email, results, logger)
        return 1 if any(r.errors or r.validation_failed for r in results) else 0
    finally:
        _release_lock()


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="Sincronizador",
                                     description="Sincronizacao de pastas e arquivos.")
    parser.add_argument("--all", action="store_true", help="Roda todas as tarefas ativas.")
    parser.add_argument("--run", metavar="NOME", help="Roda uma tarefa especifica.")
    parser.add_argument("--list", action="store_true", help="Lista as tarefas configuradas.")
    parser.add_argument("--config", metavar="ARQUIVO", default=cfgmod.DEFAULT_CONFIG_PATH,
                        help="Arquivo de configuracao JSON.")
    parser.add_argument("--gui", action="store_true", help="Forca abrir a interface grafica.")
    args = parser.parse_args(argv)

    if args.list:
        cfg = cfgmod.load_config(args.config)
        if not cfg.jobs:
            print("Nenhuma tarefa configurada.")
        for j in cfg.jobs:
            flag = "on " if j.enabled else "off"
            print(f"[{flag}] {j.name}  ({j.mode})  {j.source} -> {j.dest}")
        return 0

    if args.all or args.run:
        return run_silent(args.config, args.run, args.all)

    # sem argumentos ou --gui: abre a interface
    from . import gui
    gui.launch(args.config)
    return 0
