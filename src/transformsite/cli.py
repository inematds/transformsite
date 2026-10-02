"""CLI `transformsite`."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__


def _cfg(args):
    from .config import load

    cfg = load(getattr(args, "project", None))
    if getattr(args, "llm", None):
        cfg.llm.provider = args.llm
    return cfg


# ---------- init ----------
def cmd_init(args):
    dest = Path(args.dir)
    if dest.exists() and any(dest.iterdir()) and not args.force:
        sys.exit(f"{dest} não está vazia (use --force)")
    tpl = resources.files("transformsite") / "scaffold" / args.template
    if not tpl.is_dir():
        sys.exit(f"template desconhecido: {args.template}")
    subs = {
        "{{name}}": args.name or dest.name,
        "{{org}}": args.org or dest.name,
        "{{url}}": args.url or "",
        "{{contact}}": args.contact or "contato@exemplo.com",
    }

    def copy(src, dst: Path):
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                copy(item, target)
            elif item.name != "__pycache__":
                data = item.read_text(encoding="utf-8")
                for k, v in subs.items():
                    data = data.replace(k, v)
                target.write_text(data, encoding="utf-8")

    dest.mkdir(parents=True, exist_ok=True)
    copy(tpl, dest)
    if not (dest / ".env").exists() and (dest / ".env.example").exists():
        shutil.copy(dest / ".env.example", dest / ".env")
    (dest / ".gitignore").write_text(".env\ndata/\nkb/*.sqlite*\n__pycache__/\n", encoding="utf-8")
    print(f"projeto criado em {dest}")
    print("próximos passos:")
    print(f"  cd {dest}")
    print("  transformsite ingest           # lê o site/documentos")
    print("  transformsite chat             # conversa no terminal")
    print("  transformsite serve            # web + painel + canais configurados")


# ---------- ingest / inventory ----------
def cmd_ingest(args):
    from .kb.ingest import ingest

    cfg = _cfg(args)
    t = time.time()
    res = ingest(cfg, urls=args.url or None, max_pages=args.max_pages, force=args.force)
    res["seconds"] = round(time.time() - t, 1)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_inventory(args):
    from .inventory import inventory

    cfg = None
    try:
        cfg = _cfg(args)
    except SystemExit:
        pass
    out = Path(args.out) if args.out else (cfg.root / "inventario" if cfg else Path("inventario"))
    res = inventory(args.url or (cfg.kb.urls if cfg else []), out, args.max_pages, sitemaps=args.sitemap or (cfg.kb.sitemaps if cfg else []),
                    exclude=(cfg.kb.exclude if cfg else []))
    print(json.dumps(res, ensure_ascii=False, indent=2))


# ---------- serviços ----------
def cmd_validate(args):
    from .services.schema import ServiceError, lint_service, load_service
    from .tools import default_registry

    cfg = None
    try:
        cfg = _cfg(args)
    except SystemExit:
        pass
    reg = default_registry(cfg)
    paths = []
    for p in args.paths or [str(cfg.services_dir if cfg else "services")]:
        pp = Path(p)
        paths += sorted(pp.glob("*.y*ml")) if pp.is_dir() else [pp]
    errors = 0
    for p in paths:
        try:
            svc = load_service(p)
            errs = lint_service(svc, reg)
        except ServiceError as e:
            errs = [str(e)]
            svc = None
        status = "ok" if not errs else "ERRO"
        review = f" [{svc.review}]" if svc else ""
        print(f"{status:4} {p.name}{review}")
        for e in errs:
            print(f"     - {e}")
        errors += len(errs)
    print(f"{len(paths)} arquivo(s), {errors} erro(s)")
    sys.exit(1 if errors else 0)


# ---------- conversa ----------
def _make_agent(cfg):
    from .engine import Agent

    agent = Agent(cfg)
    for e in agent.load_errors:
        print(f"[aviso] serviço ignorado: {e}", file=sys.stderr)
    return agent


def cmd_chat(args):
    from .messages import Capabilities, InMsg, render_text

    cfg = _cfg(args)
    agent = _make_agent(cfg)
    caps = Capabilities(buttons=0, lists=0)
    user = args.user or "terminal"
    print(f"transformsite chat — {cfg.org} ({len(agent.services)} serviço(s), LLM: {cfg.llm.provider}). Ctrl+D para sair.")
    last_choices: list[dict] = []
    while True:
        try:
            text = input("\nvocê> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        choice = None
        if text.isdigit() and last_choices and 1 <= int(text) <= len(last_choices):
            choice = last_choices[int(text) - 1]["id"]
        outs = agent.handle(InMsg("cli", user, text, choice_id=choice, meta={"name": user}))
        last_choices = []
        for o in outs:
            t, _, mode = render_text(o, caps)
            if o.choices:
                last_choices = o.choices
            print(f"bot> {t}")


def cmd_ask(args):
    from .engine import Agent
    from .messages import Capabilities, InMsg, render_text

    cfg = _cfg(args)
    agent = Agent(cfg)
    outs = agent.handle(InMsg("cli", f"ask-{time.time()}", " ".join(args.text)))
    for o in outs:
        print(render_text(o, Capabilities())[0])


# ---------- eval / report / privacy ----------
def cmd_eval(args):
    from .evals import run_golden, run_scenarios

    cfg = _cfg(args)
    rc = 0
    if args.golden or not args.scenarios:
        path = Path(args.golden or cfg.root / "tests" / "golden.jsonl")
        if path.exists():
            res = run_golden(cfg, path, limit=args.limit, out=Path(args.out) if args.out else None)
            print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
            rc |= 0 if res["summary"].get("pass") else 1
    if args.scenarios or not args.golden:
        sdir = Path(args.scenarios or cfg.root / "tests" / "cenarios")
        if sdir.exists():
            res = run_scenarios(cfg, sdir, channels=args.channels.split(",") if args.channels else None)
            for r in res["results"]:
                mark = "ok  " if r["ok"] else "FAIL"
                print(f"{mark} {r['channel']:9} {r['name']}" + ("" if r["ok"] else f" — {r['error']}"))
            print(f"{res['passed']}/{res['total']} cenários passaram")
            rc |= 0 if res["passed"] == res["total"] else 1
    sys.exit(rc)


def cmd_report(args):
    from .report import report

    cfg = _cfg(args)
    res = report(cfg, since_days=args.since, out=Path(args.out) if args.out else None)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_privacy(args):
    from .store import Store

    cfg = _cfg(args)
    st = Store(cfg.db_path)
    if args.action == "forget":
        ch, _, uid = args.user.partition(":")
        print(f"{st.forget(ch, uid)} mensagem(ns) apagada(s)")
    else:
        print(f"{st.purge(cfg.retention.transcripts_days)} mensagem(ns) mais antigas que {cfg.retention.transcripts_days} dias apagadas")


def cmd_convert(args):
    from .convert import convert_file

    cfg = None
    try:
        cfg = _cfg(args)
    except SystemExit:
        pass
    out_dir = Path(args.out) if args.out else (cfg.services_dir if cfg else Path("services"))
    for src in args.sources:
        res = convert_file(src, out_dir, cfg=cfg, use_llm=not args.no_llm)
        print(f"{res['path']}  ({res['slots']} campos, {res['todos']} TODO, review: pending)")


def cmd_serve(args):
    from .server import serve

    cfg = _cfg(args)
    serve(cfg, host=args.host, port=args.port, channels=args.channels.split(",") if args.channels else None)


def main(argv=None):
    p = argparse.ArgumentParser(prog="transformsite", description="Site + base de conhecimento → agente de chat com serviços.")
    p.add_argument("--version", action="version", version=f"transformsite {__version__}")
    p.add_argument("-C", "--project", help="pasta do projeto (padrão: procura transformsite.yaml)")
    p.add_argument("--llm", help="sobrescreve llm.provider (ollama|openai_compat|claude_cli|codex_cli|fake)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="cria um projeto novo")
    s.add_argument("dir")
    s.add_argument("--template", default="default")
    s.add_argument("--name")
    s.add_argument("--org")
    s.add_argument("--url", help="site da empresa")
    s.add_argument("--contact", help="e-mail de contato humano")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("inventory", help="lista páginas e formulários do site (Fase 0)")
    s.add_argument("--url", action="append")
    s.add_argument("--sitemap", action="append")
    s.add_argument("--max-pages", type=int, default=200)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("ingest", help="lê o site/documentos e monta o índice")
    s.add_argument("--url", action="append")
    s.add_argument("--max-pages", type=int)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("validate", help="valida services/*.yaml")
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("convert", help="converte formulário legado (HTML/PDF/URL) em service.yaml")
    s.add_argument("sources", nargs="+")
    s.add_argument("--out")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_convert)

    s = sub.add_parser("chat", help="conversa no terminal")
    s.add_argument("--user")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("ask", help="uma pergunta, uma resposta")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("eval", help="mede qualidade: golden set + cenários")
    s.add_argument("--golden")
    s.add_argument("--scenarios")
    s.add_argument("--channels", help="cli,telegram,whatsapp,email,web")
    s.add_argument("--limit", type=int)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("serve", help="sobe web widget, painel e canais")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--channels", help="lista separada por vírgula (padrão: os configurados)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("report", help="métricas em JSON/CSV")
    s.add_argument("--since", type=int, default=7, help="dias")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("privacy", help="LGPD: esquecer usuário ou expurgar por retenção")
    s.add_argument("action", choices=["forget", "purge"])
    s.add_argument("--user", help="canal:id (forget)")
    s.set_defaults(fn=cmd_privacy)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
