"""Protecao das credenciais gravadas no arquivo de configuracao.

Usa a DPAPI do Windows (CryptProtectData): a chave fica amarrada a conta de
usuario do Windows, entao o JSON copiado para outra maquina - ou lido por
outro usuario - nao entrega as senhas. Nao ha chave para guardar em lugar
nenhum, que eh justamente a vantagem.

Isso passou a ser necessario com o OAuth: um refresh token vale mais que uma
senha, porque da acesso continuo a conta ate ser revogado.

Fora do Windows (ou se a DPAPI falhar) o valor eh gravado como estava antes,
em texto puro - o programa continua funcionando, so sem essa protecao.
"""
from __future__ import annotations

import base64
import logging

PREFIXO = "dpapi:"

logger = logging.getLogger("sincronizador")


def disponivel() -> bool:
    """A DPAPI pode ser usada nesta maquina?"""
    return _api() is not None


_cache = []   # [modulo_ctypes ou None] - resolvido uma vez so


def _api():
    if _cache:
        return _cache[0]
    api = None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt = ctypes.windll.crypt32
        kernel = ctypes.windll.kernel32

        def _blob(dados: bytes) -> DATA_BLOB:
            buf = ctypes.create_string_buffer(dados, len(dados))
            return DATA_BLOB(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

        def _bytes(blob: DATA_BLOB) -> bytes:
            return ctypes.string_at(blob.pbData, blob.cbData)

        def _liberar(blob: DATA_BLOB) -> None:
            if blob.pbData:
                kernel.LocalFree(blob.pbData)

        api = (ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar)
    except Exception:
        api = None
    _cache.append(api)
    return api


def proteger(valor: str) -> str:
    """Texto puro -> 'dpapi:<base64>'. Devolve o proprio valor se nao der."""
    if not valor or valor.startswith(PREFIXO):
        return valor
    api = _api()
    if api is None:
        return valor
    ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar = api
    entrada = _blob(valor.encode("utf-8"))
    saida = DATA_BLOB()
    ok = crypt.CryptProtectData(ctypes.byref(entrada), None, None, None, None,
                                0, ctypes.byref(saida))
    if not ok:
        logger.warning("Nao foi possivel proteger a credencial (DPAPI).")
        return valor
    try:
        return PREFIXO + base64.b64encode(_bytes(saida)).decode("ascii")
    finally:
        _liberar(saida)


def revelar(valor: str) -> str:
    """'dpapi:<base64>' -> texto puro. Valor sem o prefixo volta como esta."""
    if not valor or not valor.startswith(PREFIXO):
        return valor
    api = _api()
    if api is None:
        logger.warning("Credencial protegida, mas a DPAPI nao esta disponivel.")
        return ""
    ctypes, crypt, DATA_BLOB, _blob, _bytes, _liberar = api
    try:
        bruto = base64.b64decode(valor[len(PREFIXO):])
    except Exception:
        return ""
    entrada = _blob(bruto)
    saida = DATA_BLOB()
    ok = crypt.CryptUnprotectData(ctypes.byref(entrada), None, None, None, None,
                                  0, ctypes.byref(saida))
    if not ok:
        # tipico de config copiada de outra maquina ou de outro usuario
        logger.warning("Credencial gravada por outro usuario/maquina: "
                       "sera preciso informa-la de novo.")
        return ""
    try:
        return _bytes(saida).decode("utf-8", "replace")
    finally:
        _liberar(saida)


def protegido(valor: str) -> bool:
    return bool(valor) and valor.startswith(PREFIXO)
