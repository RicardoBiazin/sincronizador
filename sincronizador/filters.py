"""Filtros de inclusao/exclusao de arquivos por padrao glob.

Os padroes sao comparados contra o caminho relativo (com '/') e tambem
contra o nome do arquivo isolado, para facilitar regras como '*.tmp'.
"""
from __future__ import annotations

import fnmatch
from typing import Iterable, List


def _match_any(relpath: str, patterns: Iterable[str]) -> bool:
    name = relpath.rsplit("/", 1)[-1]
    for pat in patterns:
        p = pat.replace("\\", "/")
        if fnmatch.fnmatch(relpath, p) or fnmatch.fnmatch(name, p):
            return True
        # padrao de pasta: "pasta/" ou "pasta" deve casar com pasta/qualquer
        if fnmatch.fnmatch(relpath, p.rstrip("/") + "/*"):
            return True
    return False


def allowed(relpath: str, include: List[str], exclude: List[str]) -> bool:
    """Retorna True se o caminho passa pelos filtros."""
    relpath = relpath.replace("\\", "/")
    if exclude and _match_any(relpath, exclude):
        return False
    if include:
        return _match_any(relpath, include)
    return True
