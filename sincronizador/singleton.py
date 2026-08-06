"""Garante que so exista uma instancia do Sincronizador por vez na maquina.

Usa um mutex nomeado do Windows: ele e liberado automaticamente quando o
processo termina (mesmo que feche de forma abrupta), evitando 'locks' presos.
"""
from __future__ import annotations

ERROR_ALREADY_EXISTS = 183
_handle = None  # mantido vivo enquanto o processo existir


def already_running(name: str = "Sincronizador_UnicaInstancia") -> bool:
    """Retorna True se ja existe outra instancia rodando nesta maquina."""
    global _handle
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _handle = kernel32.CreateMutexW(None, False, name)
        return kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        # em caso de falha (ex.: outro SO), nao bloqueia o uso
        return False
