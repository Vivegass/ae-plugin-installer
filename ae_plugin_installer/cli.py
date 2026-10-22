r"""
Консольная версия установщика.

    python -m ae_plugin_installer.cli [архивы или папки] [--dry-run] [--yes] [--ae 2026|all]
                                      [--password P] [--sandbox DIR]

Без аргументов обрабатывает все архивы в текущей папке. Для записи в Program Files
перезапускает себя с правами администратора (UAC) в новом окне.
"""

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import core
from .core import Action, Plan

C_RESET, C_DIM, C_RED, C_GREEN, C_YELLOW, C_CYAN, C_BOLD = (
    "\033[0m", "\033[2m", "\033[91m", "\033[92m", "\033[93m", "\033[96m", "\033[1m")


class ConsoleUI:
    """Ход установки в консоли (интерфейс ui, который ждёт core.execute)."""

    def __init__(self, assume_yes=False):
        self.assume_yes = assume_yes

    def step(self, text):
        print(f"\n{C_BOLD}{text.splitlines()[0]}{C_RESET}")

    def ok(self, msg):
        print(f"  {C_GREEN}✓ {msg}{C_RESET}")

    def info(self, msg):
        print(f"  {C_DIM}{msg}{C_RESET}")

    def warn(self, msg):
        print(f"  {C_YELLOW}{msg}{C_RESET}")

    def error(self, msg):
        print(f"  {C_RED}✗ {msg}{C_RESET}")

    def ask(self, question, default=True):
        if self.assume_yes:
            return default
        ans = input(f"  {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes", "д", "да")

    def manual(self, text):
        print(f"{indent(text)}\n  {C_YELLOW}✋ сделайте этот шаг вручную{C_RESET}")
        if not self.assume_yes:
            input("  Нажмите Enter, когда будет готово...")

    def remaining(self, texts):
        print(f"\n{C_YELLOW}{C_BOLD}Осталось сделать вручную:{C_RESET}")
        for t in texts:
            print(indent(t))


def indent(text, prefix="    "):
    return "\n".join(prefix + l for l in text.split("\n"))


def describe(a: Action, plan: Plan, ae_roots):
    name = a.item.relative_to(plan.root)
    if a.kind in ("run", "reg"):
        return f"{C_YELLOW}{core.action_verb(a)}{C_RESET} {name}"
    targets = core.action_targets(a, ae_roots)
    if not targets:
        return f"{C_RED}скопировать {name} → After Effects не найден!{C_RESET}"
    lines = []
    for t, exists, odd in targets:
        note = ""
        if exists:
            what = "файлы будут перезаписаны" if a.item.is_dir() else "будет заменён"
            note = f" {C_YELLOW}(уже есть — {what}){C_RESET}"
        if odd:
            note += f" {C_RED}⚠ папка не похожа на папку Adobe!{C_RESET}"
        guessed = f" {C_DIM}(по типу файла){C_RESET}" if a.dest.guessed else ""
        lines.append(f"{core.action_verb(a)} {C_BOLD}{name}{C_RESET} → {t}{guessed}{note}")
    return "\n      ".join(lines)


def print_plan(plan: Plan, ae_roots):
    print(f"\n{C_CYAN}{C_BOLD}══ {plan.archive.name}{C_RESET}")
    if plan.instruction:
        print(f"{C_DIM}Инструкция: {plan.instruction.relative_to(plan.root)}{C_RESET}")
    for w in plan.warnings:
        print(f"{C_YELLOW}⚠ {w}{C_RESET}")
    for step in plan.steps:
        if step.text:
            print(f"{C_DIM}{indent(step.text)}{C_RESET}")
        if step.actions:
            for a in step.actions:
                print(f"  {C_GREEN}→{C_RESET} {describe(a, plan, ae_roots)}")
                if a.kind == "reg":
                    print(f"{C_DIM}{indent(core.read_text(a.item).strip(), '      | ')}{C_RESET}")
        elif step.text:
            print(f"  {C_YELLOW}✋ этот шаг нужно сделать вручную{C_RESET}")


_ae_choice = None


def choose_ae(args):
    """Версии After Effects, в которые ставить (спрашивает один раз, если их несколько)."""
    global _ae_choice
    if _ae_choice is not None:
        return _ae_choice
    found = core.find_ae_installs()
    if not found:
        _ae_choice = []
    elif args.ae:
        _ae_choice = found if args.ae.lower() == "all" else [p for p in found if args.ae.lower() in p.name.lower()]
    elif len(found) == 1 or args.yes or args.dry_run:
        _ae_choice = [found[-1]]
    else:
        print(f"\n{C_BOLD}Найдено несколько версий After Effects:{C_RESET}")
        for i, p in enumerate(found, 1):
            print(f"  {i}. {p.name}")
        ans = input("Куда ставить? Номер, a — во все, Enter — самая новая: ").strip().lower()
        if ans == "a":
            _ae_choice = found
        elif ans.isdigit() and 1 <= int(ans) <= len(found):
            _ae_choice = [found[int(ans) - 1]]
        else:
            _ae_choice = [found[-1]]
    return _ae_choice


def relaunch_as_admin(argv):
    params = subprocess.list2cmdline(["-m", "ae_plugin_installer.cli"] + argv)
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(core.APP_DIR), 1)
    except Exception:
        return False
    return r > 32


def collect_archives(paths):
    result = []
    for p in paths:
        p = Path(p).resolve()
        if p.is_dir():
            result += sorted(x for x in p.iterdir() if x.is_file() and core.is_archive(x))
        elif p.is_file() and core.is_archive(p):
            result.append(p)
        else:
            print(f"{C_YELLOW}Пропускаю (не архив): {p}{C_RESET}")
    return result


def main(argv=None):
    os.system("")  # включает ANSI-цвета в консоли Windows
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(prog="ae-plugin-installer-cli",
                                 description="Установщик плагинов и скриптов After Effects из архивов")
    ap.add_argument("paths", nargs="*", help="архивы или папки с архивами (по умолчанию — текущая папка)")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    ap.add_argument("--yes", action="store_true", help="не задавать вопросов")
    ap.add_argument("--ae", help="версия After Effects: 2026, 2025... или all")
    ap.add_argument("--password", help="пароль от архивов")
    ap.add_argument("--sandbox", help="копировать в эту папку вместо реальных (для проверки)")
    ap.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 1
    finally:
        if args.pause:
            input("\nНажмите Enter, чтобы закрыть окно...")


def run(args):
    if args.sandbox:
        core.set_log_file(Path(args.sandbox) / "install_log.txt")
    archives = collect_archives(args.paths or [Path.cwd()])
    if not archives:
        print("Архивы не найдены. Перетащите архив на install.bat или укажите путь.")
        return 1

    if not (args.dry_run or args.sandbox or core.is_admin()):
        print("Для записи в Program Files нужны права администратора — перезапускаюсь...")
        # новое окно закроется само, поэтому без --yes просим его подождать Enter
        argv = [str(a) for a in archives] + (["--yes"] if args.yes else ["--pause"])
        for opt in ("ae", "password"):
            if getattr(args, opt):
                argv += [f"--{opt}", getattr(args, opt)]
        if relaunch_as_admin(argv):
            args.pause = False
            return 0
        print(f"{C_YELLOW}Не удалось получить права администратора, пробую без них.{C_RESET}")

    print(f"{C_DIM}Распаковка: {core.extractor_summary()}{C_RESET}")

    ui = ConsoleUI(args.yes)
    if not args.dry_run and not args.sandbox and core.ae_running():
        print(f"{C_YELLOW}After Effects запущен. Закройте его: занятые файлы не заменятся, "
              f"а новые плагины появятся только после перезапуска.{C_RESET}")
        if not ui.ask("Продолжить всё равно?", default=False):
            return 1

    results = []
    for archive in archives:
        with tempfile.TemporaryDirectory(prefix="aeinst_") as tmp:
            root = Path(tmp)
            try:
                core.extract(archive, root, args.password)
                core.extract_nested(root, args.password)
            except RuntimeError as e:
                print(f"\n{C_RED}✗ {archive.name}: не удалось распаковать — {e}{C_RESET}")
                results.append((archive.name, "ошибка распаковки"))
                continue
            plan = core.analyze(archive, root)
            print_plan(plan, choose_ae(args))
            if not plan.actions:
                results.append((archive.name, "нечего устанавливать"))
                continue
            if args.dry_run:
                results.append((archive.name, "план"))
                continue
            print()
            if not ui.ask(f"{C_BOLD}Установить {archive.name}?{C_RESET}"):
                results.append((archive.name, "пропущен"))
                continue
            ok = core.execute(plan, ui, choose_ae(args), args.sandbox)
            results.append((archive.name, "установлен" if ok else "с ошибками"))

    print(f"\n{C_BOLD}Итог:{C_RESET}")
    for name, status in results:
        color = C_GREEN if status in ("установлен", "план") else C_YELLOW
        print(f"  {color}{status:<20}{C_RESET} {name}")
    return 0 if all(s in ("установлен", "план", "пропущен") for _, s in results) else 1


if __name__ == "__main__":
    sys.exit(main())
