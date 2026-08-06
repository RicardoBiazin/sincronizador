"""Interface grafica (Tkinter) para configurar e rodar as tarefas."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import fields as dcfields
from tkinter import ttk, filedialog, messagebox

from . import __author__, __version__
from . import config as cfgmod
from . import endpoints as epmod
from . import engine
from . import notify


# ---------------------------------------------------------------------------
# Temas de cores
# ---------------------------------------------------------------------------
# cada tema: bg (fundo), fg (texto), field (campos), log_bg, log_fg
THEMES = {
    "Claro":   {"bg": "#f2f2f2", "fg": "#1a1a1a", "field": "#ffffff",
                "log_bg": "#ffffff", "log_fg": "#222222"},
    "Escuro":  {"bg": "#1e1e1e", "fg": "#e6e6e6", "field": "#2b2b2b",
                "log_bg": "#111111", "log_fg": "#d6d6d6"},
    "Azul":    {"bg": "#e8f0fb", "fg": "#10243d", "field": "#ffffff",
                "log_bg": "#0e2233", "log_fg": "#cfe6ff"},
    "Verde":   {"bg": "#eaf5ec", "fg": "#12331b", "field": "#ffffff",
                "log_bg": "#10240f", "log_fg": "#cdeccb"},
    "Sepia":   {"bg": "#f4ecd8", "fg": "#3b2f1c", "field": "#fffdf5",
                "log_bg": "#2a2317", "log_fg": "#e9dcc0"},
}


def center_over(win, master):
    """Posiciona 'win' centralizada sobre a janela 'master'."""
    try:
        win.update_idletasks()
        master.update_idletasks()
        mw, mh = master.winfo_width(), master.winfo_height()
        mx, my = master.winfo_rootx(), master.winfo_rooty()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = mx + (mw - w) // 2
        y = my + (mh - h) // 2
        # nao deixa sair da tela
        x = max(0, min(x, win.winfo_screenwidth() - w))
        y = max(0, min(y, win.winfo_screenheight() - h))
        win.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass


def apply_theme(root, theme_name: str, accent: str):
    t = THEMES.get(theme_name, THEMES["Claro"])
    style = ttk.Style()
    try:
        style.theme_use("clam")  # 'clam' respeita cores customizadas
    except tk.TclError:
        pass
    bg, fg, field = t["bg"], t["fg"], t["field"]
    root.configure(bg=bg)
    style.configure(".", background=bg, foreground=fg, fieldbackground=field)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("TLabelframe", background=bg, foreground=fg)
    style.configure("TLabelframe.Label", background=bg, foreground=fg)
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.configure("TButton", background=field, foreground=fg)
    style.map("TButton",
              background=[("active", accent)],
              foreground=[("active", "#ffffff")])
    style.configure("Treeview", background=field, foreground=fg, fieldbackground=field)
    style.map("Treeview",
              background=[("selected", accent)],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=bg, foreground=fg)
    style.configure("Footer.TLabel", background=bg, foreground=accent)
    return t


# ---------------------------------------------------------------------------
# Handler de log que joga as mensagens numa fila (para a caixa de texto)
# ---------------------------------------------------------------------------
class QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


# ---------------------------------------------------------------------------
# Editor de um endpoint (origem ou destino)
# ---------------------------------------------------------------------------
#: campos que existem como atributo em config.Remote; o resto vai em options
_REMOTE_ATTRS = {f.name for f in dcfields(cfgmod.Remote)} - {"options"}


class EndpointFrame(ttk.LabelFrame):
    """Editor de um endpoint. Os campos de conexao sao montados a partir do
    EndpointSpec do tipo escolhido - tipo novo registrado em endpoints.py
    aparece aqui sozinho, sem mexer na interface."""

    def __init__(self, master, title, path, kind, remote: cfgmod.Remote):
        super().__init__(master, text=title, padding=8)
        self.remote = remote
        if kind not in epmod.endpoint_kinds():
            kind = "local"

        # valores digitados, por chave de campo; sobrevivem a troca de tipo
        self._values = {k: getattr(remote, k) for k in _REMOTE_ATTRS}
        self._values.update(remote.options or {})
        self._vars = {}

        self._labels = {k: epmod.get_spec(k).label for k in epmod.endpoint_kinds()}
        self._by_label = {v: k for k, v in self._labels.items()}

        ttk.Label(self, text="Tipo:").grid(row=0, column=0, sticky="w")
        self.kind = tk.StringVar(value=kind)
        self._kind_label = tk.StringVar(value=self._labels[kind])
        cb = ttk.Combobox(self, textvariable=self._kind_label,
                          values=list(self._by_label), state="readonly", width=24)
        cb.grid(row=0, column=1, sticky="w", padx=4)
        cb.bind("<<ComboboxSelected>>", self._on_kind)
        self.test_btn = ttk.Button(self, text="Testar", width=8, command=self._test)
        self.test_btn.grid(row=0, column=3, padx=4)

        self.path_lbl = ttk.Label(self, text="Caminho:")
        self.path_lbl.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.path = tk.StringVar(value=path)
        ttk.Entry(self, textvariable=self.path, width=44).grid(
            row=1, column=1, columnspan=2, sticky="we", pady=(6, 0))
        self.browse_btn = ttk.Button(self, text="...", width=3, command=self._browse)
        self.browse_btn.grid(row=1, column=3, padx=4, pady=(6, 0))

        self.note = ttk.Label(self, text="", wraplength=430, foreground="#666666")
        self.note.grid(row=2, column=0, columnspan=4, sticky="w")

        self.rframe = ttk.Frame(self)
        self.rframe.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))

        self.columnconfigure(1, weight=1)
        self._build_fields()

    # -- montagem dinamica ---------------------------------------------------
    def _on_kind(self, _evt=None):
        self._stash()
        self.kind.set(self._by_label[self._kind_label.get()])
        self._build_fields()

    def _stash(self):
        """Guarda o que esta na tela antes de trocar de tipo."""
        for key, var in self._vars.items():
            self._values[key] = var.get()

    def _build_fields(self):
        for child in self.rframe.winfo_children():
            child.destroy()
        self._vars = {}

        spec = epmod.get_spec(self.kind.get())
        self.path_lbl.configure(text=spec.path_label)
        self.browse_btn.configure(state="normal" if spec.path_browse else "disabled")

        faltando = epmod.missing_requirements(self.kind.get())
        aviso = ""
        if faltando:
            aviso = "Requer: pip install " + " ".join(faltando)
        elif spec.note:
            aviso = spec.note
        self.note.configure(text=aviso,
                            foreground="#b00020" if faltando else "#666666")

        for row, f in enumerate(spec.fields):
            self._build_field(self.rframe, row, f)

    def _build_field(self, parent, row, f):
        cur = self._values.get(f.key, f.default)
        if f.kind == "oauth":
            var = tk.StringVar(value="" if cur is None else str(cur))
            ttk.Label(parent, text=f.label + ":").grid(row=row, column=0, sticky="w")
            linha = ttk.Frame(parent)
            linha.grid(row=row, column=1, columnspan=3, sticky="w", padx=4, pady=1)
            estado = ttk.Label(linha, text="")
            botao = ttk.Button(linha, width=14)
            limpar = ttk.Button(linha, text="Desconectar", width=12)

            def atualizar():
                ligada = bool(var.get())
                estado.configure(
                    text="conectada" if ligada else "nao conectada",
                    foreground="#177d3c" if ligada else "#b00020")
                botao.configure(text="Reconectar..." if ligada else "Conectar...")
                limpar.configure(state="normal" if ligada else "disabled")

            botao.configure(command=lambda: self._conectar(f, var, atualizar, botao))
            limpar.configure(command=lambda: (var.set(""), atualizar()))
            botao.pack(side="left")
            limpar.pack(side="left", padx=4)
            estado.pack(side="left", padx=6)
            atualizar()
            self._vars[f.key] = var
            return
        if f.kind == "bool":
            var = tk.BooleanVar(value=bool(cur))
            ttk.Checkbutton(parent, text=f.label, variable=var).grid(
                row=row, column=1, sticky="w", padx=4, pady=1)
        else:
            texto = "" if cur in (None, 0, False) and f.kind == "int" else cur
            var = tk.StringVar(value="" if texto is None else str(texto))
            ttk.Label(parent, text=f.label + ":").grid(row=row, column=0, sticky="w")
            ttk.Entry(parent, textvariable=var, width=f.width,
                      show="*" if f.kind == "password" else "").grid(
                row=row, column=1, sticky="w", padx=4, pady=1)
            if f.kind in ("file", "dir"):
                ttk.Button(parent, text="...", width=3,
                           command=lambda v=var, k=f.kind: self._pick(v, k)).grid(
                    row=row, column=2, padx=2)
        if f.help:
            ttk.Label(parent, text=f.help, foreground="#777777").grid(
                row=row, column=3, sticky="w", padx=4)
        self._vars[f.key] = var

    def _conectar(self, campo, var, atualizar, botao):
        """Abre o navegador para autorizar a conta e guarda o refresh token."""
        from . import oauth
        self._stash()
        client_id = str(self._values.get("client_id", "")).strip()
        if not client_id:
            messagebox.showwarning(
                "Conectar", "Informe primeiro o Client ID do aplicativo.",
                parent=self)
            return
        prov = oauth.provedor(campo.provedor)
        if not messagebox.askokcancel(
                "Conectar ao " + prov.rotulo,
                "O navegador vai abrir para voce autorizar o acesso.\n\n"
                "No cadastro do aplicativo, o endereco de redirecionamento\n"
                "precisa ser exatamente:\n\n    %s\n\nContinuar?"
                % oauth.redirect_uri(), parent=self):
            return

        botao.configure(state="disabled", text="Aguardando...")
        dados = {"client_id": client_id,
                 "client_secret": str(self._values.get("client_secret", "")),
                 "tenant": str(self._values.get("tenant", "")) or "common"}

        def trabalho():
            try:
                tokens = oauth.autorizar(campo.provedor, **dados)
                erro = None
            except Exception as e:
                tokens, erro = None, str(e)
            self.after(0, lambda: pronto(tokens, erro))

        def pronto(tokens, erro):
            botao.configure(state="normal")
            if erro:
                atualizar()
                messagebox.showerror("Conectar", "Nao deu certo:\n\n" + erro,
                                     parent=self)
                return
            var.set(tokens["refresh_token"])
            self._values[campo.key] = tokens["refresh_token"]
            atualizar()
            messagebox.showinfo("Conectar", "Conta conectada.", parent=self)

        threading.Thread(target=trabalho, daemon=True).start()

    def _pick(self, var, kind):
        p = filedialog.askdirectory() if kind == "dir" else filedialog.askopenfilename()
        if p:
            var.set(p)

    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.path.set(d)

    # -- teste de conexao ----------------------------------------------------
    def _test(self):
        path, kind, rem = self.collect()
        self.test_btn.configure(state="disabled", text="...")

        def work():
            try:
                ep = epmod.make_endpoint(path, kind, rem)
                try:
                    ep.probe()
                finally:
                    ep.close()
                erro = None
            except Exception as e:
                erro = str(e)
            self.after(0, lambda: self._test_done(erro))

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, erro):
        self.test_btn.configure(state="normal", text="Testar")
        if erro:
            messagebox.showerror("Conexao", "Falhou:\n\n" + erro, parent=self)
        else:
            messagebox.showinfo("Conexao", "Conexao OK.", parent=self)

    # -- leitura -------------------------------------------------------------
    def collect(self):
        self._stash()
        spec = epmod.get_spec(self.kind.get())
        rem = cfgmod.Remote()
        options = {}
        for f in spec.fields:
            raw = self._values.get(f.key, f.default)
            val = self._coerce(f, raw)
            if f.key in _REMOTE_ATTRS:
                setattr(rem, f.key, val)
            else:
                options[f.key] = val
        rem.options = options
        return self.path.get().strip(), self.kind.get(), rem

    @staticmethod
    def _coerce(f, raw):
        if f.kind == "bool":
            return bool(raw)
        if f.kind == "int":
            s = str(raw).strip()
            return int(s) if s.isdigit() else 0
        if f.kind == "password":
            return "" if raw is None else str(raw)
        return "" if raw is None else str(raw).strip()


# ---------------------------------------------------------------------------
# Dialogo de edicao de tarefa
# ---------------------------------------------------------------------------
class JobDialog(tk.Toplevel):
    def __init__(self, master, job: cfgmod.Job):
        super().__init__(master)
        self.title("Tarefa de sincronizacao")
        self.result = None
        self.job = job
        self.transient(master)
        self.grab_set()

        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)

        ttk.Label(top, text="Nome:").grid(row=0, column=0, sticky="w")
        self.name = tk.StringVar(value=job.name)
        ttk.Entry(top, textvariable=self.name, width=40).grid(row=0, column=1, sticky="we", pady=2)

        ttk.Label(top, text="Modo:").grid(row=1, column=0, sticky="w")
        self.mode = tk.StringVar(value=job.mode)
        ttk.Combobox(top, textvariable=self.mode, values=list(cfgmod.MODES),
                     state="readonly", width=18).grid(row=1, column=1, sticky="w", pady=2)

        self.enabled = tk.BooleanVar(value=job.enabled)
        ttk.Checkbutton(top, text="Ativa", variable=self.enabled).grid(row=1, column=1, sticky="e")

        self.src = EndpointFrame(top, "Origem", job.source, job.source_type, job.source_remote)
        self.src.grid(row=2, column=0, columnspan=2, sticky="we", pady=6)
        self.dst = EndpointFrame(top, "Destino", job.dest, job.dest_type, job.dest_remote)
        self.dst.grid(row=3, column=0, columnspan=2, sticky="we", pady=6)

        filt = ttk.LabelFrame(top, text="Filtros (um padrao por linha, ex: *.tmp)", padding=6)
        filt.grid(row=4, column=0, columnspan=2, sticky="we", pady=6)
        ttk.Label(filt, text="Incluir (vazio = tudo):").grid(row=0, column=0, sticky="nw")
        self.include = tk.Text(filt, width=30, height=4)
        self.include.grid(row=1, column=0, padx=4)
        self.include.insert("1.0", "\n".join(job.include))
        ttk.Label(filt, text="Excluir:").grid(row=0, column=1, sticky="nw")
        self.exclude = tk.Text(filt, width=30, height=4)
        self.exclude.grid(row=1, column=1, padx=4)
        self.exclude.insert("1.0", "\n".join(job.exclude))

        ver = ttk.LabelFrame(top, text="Versionamento / backup", padding=6)
        ver.grid(row=5, column=0, columnspan=2, sticky="we", pady=6)
        self.versioning = tk.BooleanVar(value=job.versioning)
        ttk.Checkbutton(ver, text="Guardar copia antes de sobrescrever/apagar",
                        variable=self.versioning).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(ver, text="Pasta de backup:").grid(row=1, column=0, sticky="w")
        self.backup = tk.StringVar(value=job.backup_dir)
        ttk.Entry(ver, textvariable=self.backup, width=40).grid(row=1, column=1, sticky="we")
        ttk.Label(ver, text="Manter backups por (dias, 0=sempre):").grid(row=2, column=0, sticky="w")
        self.keep = tk.StringVar(value=str(job.keep_versions_days))
        ttk.Entry(ver, textvariable=self.keep, width=8).grid(row=2, column=1, sticky="w")

        cmpf = ttk.LabelFrame(top, text="Como detectar que um arquivo mudou", padding=6)
        cmpf.grid(row=6, column=0, columnspan=2, sticky="we", pady=(0, 6))
        self.compare = tk.StringVar(value=getattr(job, "compare", "auto"))
        ttk.Combobox(cmpf, textvariable=self.compare, values=list(cfgmod.COMPARE_MODES),
                     state="readonly", width=12).grid(row=0, column=0, sticky="w")
        ttk.Label(cmpf, wraplength=420, foreground="#666666",
                  text="auto: usa a data quando os dois lados a preservam, senao so o "
                       "tamanho. data: sempre tamanho+data. tamanho: rapido. "
                       "conteudo: confere o hash (lento e seguro).").grid(
            row=0, column=1, sticky="w", padx=6)

        val = ttk.LabelFrame(top, text="Validacao ao final", padding=6)
        val.grid(row=7, column=0, columnspan=2, sticky="we", pady=(0, 6))
        self.validate = tk.BooleanVar(value=job.validate)
        ttk.Checkbutton(val, text="Validar arquivos apos sincronizar (tamanho e data)",
                        variable=self.validate).grid(row=0, column=0, sticky="w")
        self.validate_hash = tk.BooleanVar(value=job.validate_hash)
        ttk.Checkbutton(val, text="Validacao por conteudo (hash) - mais lento/seguro",
                        variable=self.validate_hash).grid(row=1, column=0, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=8, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)

        top.columnconfigure(1, weight=1)
        center_over(self, master)

    def _save(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning("Atencao", "Informe um nome para a tarefa.", parent=self)
            return
        j = self.job
        j.name = name
        j.mode = self.mode.get()
        j.enabled = self.enabled.get()
        j.source, j.source_type, j.source_remote = self.src.collect()
        j.dest, j.dest_type, j.dest_remote = self.dst.collect()
        j.include = [ln.strip() for ln in self.include.get("1.0", "end").splitlines() if ln.strip()]
        j.exclude = [ln.strip() for ln in self.exclude.get("1.0", "end").splitlines() if ln.strip()]
        j.versioning = self.versioning.get()
        j.backup_dir = self.backup.get().strip()
        j.keep_versions_days = int(self.keep.get()) if self.keep.get().strip().isdigit() else 0
        j.validate = self.validate.get()
        j.validate_hash = self.validate_hash.get()
        j.compare = self.compare.get()
        if not j.source or not j.dest:
            messagebox.showwarning("Atencao", "Informe origem e destino.", parent=self)
            return
        self.result = j
        self.destroy()


# ---------------------------------------------------------------------------
# Dialogo de e-mail
# ---------------------------------------------------------------------------
class EmailDialog(tk.Toplevel):
    def __init__(self, master, email: cfgmod.Email):
        super().__init__(master)
        self.title("Notificacao por e-mail")
        self.result = None
        self.email = email
        self.transient(master)
        self.grab_set()
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)

        self.enabled = tk.BooleanVar(value=email.enabled)
        ttk.Checkbutton(f, text="Enviar e-mail de notificacao", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w")

        rows = [
            ("Servidor SMTP:", "host"), ("Porta:", "port"),
            ("Usuario:", "user"), ("Senha:", "pwd"),
            ("De (remetente):", "from"), ("Para (virgula):", "to"),
        ]
        self.vars = {}
        self.vars["host"] = tk.StringVar(value=email.smtp_host)
        self.vars["port"] = tk.StringVar(value=str(email.smtp_port))
        self.vars["user"] = tk.StringVar(value=email.smtp_user)
        self.vars["pwd"] = tk.StringVar(value=email.smtp_password)
        self.vars["from"] = tk.StringVar(value=email.from_addr)
        self.vars["to"] = tk.StringVar(value=", ".join(email.to_addrs))
        for i, (label, key) in enumerate(rows, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w")
            show = "*" if key == "pwd" else ""
            ttk.Entry(f, textvariable=self.vars[key], width=36, show=show).grid(row=i, column=1, sticky="we", pady=1)

        self.tls = tk.BooleanVar(value=email.use_tls)
        ttk.Checkbutton(f, text="Usar TLS (porta 587)", variable=self.tls).grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Notificar:").grid(row=8, column=0, sticky="w")
        self.notify_on = tk.StringVar(value=email.notify_on)
        ttk.Combobox(f, textvariable=self.notify_on, values=["sempre", "erros"],
                     state="readonly", width=10).grid(row=8, column=1, sticky="w")

        btns = ttk.Frame(f)
        btns.grid(row=9, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
        f.columnconfigure(1, weight=1)
        center_over(self, master)

    def _save(self):
        e = self.email
        e.enabled = self.enabled.get()
        e.smtp_host = self.vars["host"].get().strip()
        e.smtp_port = int(self.vars["port"].get()) if self.vars["port"].get().strip().isdigit() else 587
        e.smtp_user = self.vars["user"].get().strip()
        e.smtp_password = self.vars["pwd"].get()
        e.from_addr = self.vars["from"].get().strip()
        e.to_addrs = [a.strip() for a in self.vars["to"].get().split(",") if a.strip()]
        e.use_tls = self.tls.get()
        e.notify_on = self.notify_on.get()
        self.result = e
        self.destroy()


# ---------------------------------------------------------------------------
# Dialogo de aparencia (tema + cor de destaque)
# ---------------------------------------------------------------------------
class ThemeDialog(tk.Toplevel):
    def __init__(self, master, theme: str, accent: str):
        super().__init__(master)
        self.title("Aparencia")
        self.result = None
        self.transient(master)
        self.grab_set()
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Tema de cores:").grid(row=0, column=0, sticky="w")
        self.theme = tk.StringVar(value=theme if theme in THEMES else "Claro")
        ttk.Combobox(f, textvariable=self.theme, values=list(THEMES.keys()),
                     state="readonly", width=16).grid(row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(f, text="Cor de destaque:").grid(row=1, column=0, sticky="w")
        self.accent = accent
        self.swatch = tk.Label(f, text="       ", bg=accent, relief="ridge")
        self.swatch.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(f, text="Escolher...", command=self._pick).grid(row=1, column=2, padx=4)

        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btns, text="Aplicar", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)

        center_over(self, master)

    def _pick(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(color=self.accent, parent=self)
        if c and c[1]:
            self.accent = c[1]
            self.swatch.configure(bg=self.accent)

    def _save(self):
        self.result = (self.theme.get(), self.accent)
        self.destroy()


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------
class MainWindow:
    def __init__(self, root, config_path):
        self.root = root
        self.config_path = config_path
        self.cfg = cfgmod.load_config(config_path)
        self.log_queue: "queue.Queue" = queue.Queue()
        self.running = False

        root.title(f"Sincronizador {__version__}")
        root.geometry("820x600")
        root.minsize(700, 480)

        # aplica o tema salvo
        self.theme_colors = apply_theme(root, self.cfg.theme, self.cfg.accent)

        # --- rodape e status: empacotados PRIMEIRO para ficarem sempre visiveis
        #     no fundo, mesmo com escala de tela (DPI) alta ---
        footer = ttk.Label(
            root,
            text=f"Sincronizador v{__version__}   •   Criado por {__author__}",
            style="Footer.TLabel", anchor="center")
        footer.pack(fill="x", side="bottom", pady=(2, 4))
        ttk.Separator(root, orient="horizontal").pack(fill="x", side="bottom")

        self.status = tk.StringVar(value="Pronto.")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom")

        toolbar = ttk.Frame(root, padding=6)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Nova", command=self.add_job).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Editar", command=self.edit_job).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Remover", command=self.remove_job).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Rodar selecionada", command=self.run_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Rodar todas", command=self.run_all).pack(side="left", padx=2)
        self.stop_btn = ttk.Button(toolbar, text="Parar", command=self._cancel, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="E-mail", command=self.edit_email).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Aparencia", command=self.edit_theme).pack(side="left", padx=2)

        ttk.Label(toolbar, text="Arquivos/vez:").pack(side="left", padx=(10, 2))
        self.parallel_var = tk.IntVar(value=self.cfg.parallel)
        sp = ttk.Spinbox(toolbar, from_=1, to=16, width=3, textvariable=self.parallel_var,
                         command=self._save_parallel)
        sp.pack(side="left")
        sp.bind("<FocusOut>", lambda e: self._save_parallel())

        cols = ("modo", "origem", "destino", "ativa")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (110, 260, 260, 60)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w)
        self.tree.pack(fill="x", padx=6)
        self.tree.bind("<Double-1>", lambda e: self.edit_job())

        # --- area de progresso (barra + velocidade/tempo/ETA) ---
        prog = ttk.Frame(root, padding=(6, 4))
        prog.pack(fill="x")
        self.progressbar = ttk.Progressbar(prog, orient="horizontal", mode="determinate", maximum=100)
        self.progressbar.pack(fill="x")
        self.progress_text = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.progress_text, style="Footer.TLabel").pack(anchor="w", pady=(2, 0))

        ttk.Label(root, text="Registro:").pack(anchor="w", padx=6, pady=(6, 0))
        self.logbox = tk.Text(root, height=14, wrap="word",
                              bg=self.theme_colors["log_bg"], fg=self.theme_colors["log_fg"],
                              insertbackground=self.theme_colors["log_fg"])
        self.logbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.progress = None  # engine.Progress durante a execucao
        self.cancel_event = None
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh()
        self.root.after(200, self._drain_log)
        self.root.after(300, self._tick_progress)

    def _cancel(self):
        if self.running and self.cancel_event is not None:
            self.cancel_event.set()
            self.status.set("Parando... (aguardando o arquivo atual terminar)")
            self.stop_btn.configure(state="disabled")

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "Sair",
                    "Uma sincronização está em andamento.\n\n"
                    "Deseja pará-la e sair? Os arquivos já copiados são mantidos; "
                    "o restante não será copiado."):
                return
            if self.cancel_event is not None:
                self.cancel_event.set()
        self.root.destroy()

    def _save_parallel(self):
        try:
            v = max(1, min(16, int(self.parallel_var.get())))
        except (tk.TclError, ValueError):
            return
        if v != self.cfg.parallel:
            self.cfg.parallel = v
            cfgmod.save_config(self.cfg, self.config_path)

    # ------- lista -------
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for j in self.cfg.jobs:
            self.tree.insert("", "end", values=(j.mode, j.source, j.dest, "sim" if j.enabled else "nao"))

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    def add_job(self):
        dlg = JobDialog(self.root, cfgmod.Job())
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.jobs.append(dlg.result)
            self._save_and_refresh()

    def edit_job(self):
        i = self._selected_index()
        if i is None:
            return
        import copy
        dlg = JobDialog(self.root, copy.deepcopy(self.cfg.jobs[i]))
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.jobs[i] = dlg.result
            self._save_and_refresh()

    def remove_job(self):
        i = self._selected_index()
        if i is None:
            return
        if messagebox.askyesno("Confirmar", f"Remover a tarefa '{self.cfg.jobs[i].name}'?"):
            del self.cfg.jobs[i]
            self._save_and_refresh()

    def edit_email(self):
        import copy
        dlg = EmailDialog(self.root, copy.deepcopy(self.cfg.email))
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.email = dlg.result
            cfgmod.save_config(self.cfg, self.config_path)
            self.status.set("Configuracao de e-mail salva.")

    def edit_theme(self):
        dlg = ThemeDialog(self.root, self.cfg.theme, self.cfg.accent)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.theme, self.cfg.accent = dlg.result
            cfgmod.save_config(self.cfg, self.config_path)
            self.theme_colors = apply_theme(self.root, self.cfg.theme, self.cfg.accent)
            self.logbox.configure(bg=self.theme_colors["log_bg"],
                                  fg=self.theme_colors["log_fg"],
                                  insertbackground=self.theme_colors["log_fg"])
            self.status.set(f"Tema aplicado: {self.cfg.theme}")

    def _save_and_refresh(self):
        cfgmod.save_config(self.cfg, self.config_path)
        self.refresh()

    # ------- execucao -------
    def run_selected(self):
        i = self._selected_index()
        if i is None:
            messagebox.showinfo("Info", "Selecione uma tarefa.")
            return
        self._run([self.cfg.jobs[i]])

    def run_all(self):
        jobs = [j for j in self.cfg.jobs if j.enabled]
        if not jobs:
            messagebox.showinfo("Info", "Nenhuma tarefa ativa.")
            return
        self._run(jobs)

    def _run(self, jobs):
        if self.running:
            messagebox.showinfo("Info", "Ja existe uma sincronizacao em andamento.")
            return
        self.running = True
        self.status.set("Sincronizando...")
        self.logbox.delete("1.0", "end")
        self.progress = engine.Progress()
        self.cancel_event = threading.Event()
        self.progressbar["value"] = 0
        self.stop_btn.configure(state="normal")

        logger = notify.setup_logger(self.cfg.log_dir, to_console=False)
        qh = QueueHandler(self.log_queue)
        qh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        logger.addHandler(qh)

        from .cli import _acquire_lock, _release_lock
        workers = self.cfg.parallel
        cancel = self.cancel_event

        def worker():
            if not _acquire_lock(logger):
                self.log_queue.put(
                    "__DONE__ Ja existe uma sincronizacao em andamento (outra "
                    "janela ou tarefa agendada). Tente novamente em instantes.")
                return
            try:
                results = engine.run_jobs(jobs, logger, progress=self.progress,
                                          workers=workers, cancel=cancel)
                notify.maybe_notify(self.cfg.email, results, logger)
                errs = sum(len(r.errors) for r in results)
                vfail = sum(len(r.validation_failed) for r in results)
                vok = sum(r.validated for r in results)
                if any(r.cancelled for r in results) or cancel.is_set():
                    self.log_queue.put(
                        f"__DONE__ INTERROMPIDA pelo usuario. Copiados ate parar: "
                        f"{sum(r.copied + r.updated for r in results)} arquivo(s).")
                else:
                    self.log_queue.put(
                        f"__DONE__ Concluido. Erros: {errs} | Validados: {vok} | "
                        f"Falhas de validacao: {vfail}")
            except Exception as e:
                self.log_queue.put(f"__DONE__ Falha: {e}")
            finally:
                _release_lock()

        threading.Thread(target=worker, daemon=True).start()

    def _drain_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg.startswith("__DONE__"):
                    self.running = False
                    self.stop_btn.configure(state="disabled")
                    self.status.set(msg.replace("__DONE__", "").strip())
                    if self.progress is not None and not self.cancel_event.is_set():
                        self.progressbar["value"] = 100
                else:
                    self.logbox.insert("end", msg + "\n")
                    self.logbox.see("end")
        except queue.Empty:
            pass
        self.root.after(200, self._drain_log)

    @staticmethod
    def _fmt_time(seconds):
        if seconds is None:
            return "--:--"
        seconds = int(seconds)
        h, r = divmod(seconds, 3600)
        m, s = divmod(r, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_bytes(n):
        n = float(n)
        for u in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} PB"

    def _tick_progress(self):
        if self.progress is not None:
            s = self.progress.snapshot()
            if s["total_known"]:
                self.progressbar["value"] = s["pct"]
                partes = [
                    f"Tempo: {self._fmt_time(s['elapsed'])}",
                    f"Velocidade: {self._fmt_bytes(s['speed'])}/s",
                    f"Faltam: {self._fmt_time(s['eta'])}",
                    f"{s['pct']:.0f}%  "
                    f"({s['files_done']}/{s['files_total']} arq., "
                    f"{self._fmt_bytes(s['bytes_done'])}/{self._fmt_bytes(s['bytes_total'])})",
                ]
            else:
                if not s["finished"]:
                    self.progressbar["value"] = (self.progressbar["value"] + 3) % 100
                partes = [
                    f"Tempo: {self._fmt_time(s['elapsed'])}",
                    f"Velocidade: {self._fmt_bytes(s['speed'])}/s",
                    f"{s['files_done']} arq. ({self._fmt_bytes(s['bytes_done'])})",
                ]
            if s["finished"]:
                self.progressbar["value"] = 100
                partes.append("Concluido")
            self.progress_text.set("   |   ".join(partes))
        self.root.after(300, self._tick_progress)


def launch(config_path: str = cfgmod.DEFAULT_CONFIG_PATH):
    from . import singleton
    if singleton.already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Sincronizador",
            "O Sincronizador já está em execução nesta máquina.\n"
            "Use a janela que já está aberta.")
        root.destroy()
        return
    root = tk.Tk()
    MainWindow(root, config_path)  # o tema eh aplicado dentro da MainWindow
    root.mainloop()
