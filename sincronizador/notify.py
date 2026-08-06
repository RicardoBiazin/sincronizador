"""Log em arquivo e notificacao por e-mail."""
from __future__ import annotations

import glob
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import List

from . import config as cfgmod


def setup_logger(log_dir: str, to_console: bool = True) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("sincronizador")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    day = time.strftime("%Y-%m-%d")
    fh = RotatingFileHandler(
        os.path.join(log_dir, f"sincronizador-{day}.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if to_console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


def cleanup_old_logs(log_dir: str, keep_days: int) -> None:
    if keep_days <= 0 or not os.path.isdir(log_dir):
        return
    cutoff = time.time() - keep_days * 86400
    for f in glob.glob(os.path.join(log_dir, "sincronizador-*.log*")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except OSError:
            pass


def send_email(email: "cfgmod.Email", subject: str, body: str, logger) -> None:
    if not email.enabled or not email.to_addrs or not email.smtp_host:
        return
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email.from_addr or email.smtp_user
    msg["To"] = ", ".join(email.to_addrs)

    try:
        if email.smtp_port == 465:
            server = smtplib.SMTP_SSL(email.smtp_host, email.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=30)
            if email.use_tls:
                server.starttls()
        if email.smtp_user:
            server.login(email.smtp_user, email.smtp_password)
        server.sendmail(msg["From"], email.to_addrs, msg.as_string())
        server.quit()
        logger.info("E-mail de notificacao enviado para %s", msg["To"])
    except Exception as e:
        logger.error("Falha ao enviar e-mail: %s", e)


def maybe_notify(email: "cfgmod.Email", results: List, logger) -> None:
    """Envia e-mail conforme a politica (sempre / erros)."""
    if not email.enabled:
        return
    has_error = any(r.errors or r.validation_failed for r in results)
    if email.notify_on == "erros" and not has_error:
        return

    status = "COM ERROS" if has_error else "OK"
    lines = [f"Sincronizacao {status} - {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for r in results:
        lines.append(r.summary())
        for e in r.errors:
            lines.append(f"    ! {e}")
        for v in r.validation_failed:
            lines.append(f"    ! validacao: {v}")
    body = "\n".join(lines)
    subject = f"[Sincronizador] {status} ({len(results)} tarefa(s))"
    send_email(email, subject, body, logger)
