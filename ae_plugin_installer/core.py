r"""
Ядро: распаковка архива, поиск и разбор инструкции, план установки и его выполнение.

Модуль не печатает и ничего не спрашивает сам: всё, что видит пользователь, идёт
через объект ui (см. cli.ConsoleUI и gui.RemoteUI) с методами
step / ok / info / warn / error / ask / manual / remaining.
"""

import ctypes
import datetime
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab"}
JUNK_EXT = {".html", ".htm", ".url", ".pdf", ".txt", ".nfo", ".md", ".lnk", ".dmg", ".pkg", ".diz", ".rtf"}
PLUGIN_EXT = {".aex", ".prm", ".jsx", ".jsxbin", ".ffx", ".zxp", ".blaceplugin"}
INSTALLER_EXT = {".exe", ".msi"}
MAC_DIRS = {"mac", "macos", "osx", "mac os", "mac os x", "__macosx"}
PLATFORM_DIRS = MAC_DIRS | {"win", "windows", "win64", "x64", "pc"}

MEDIACORE = r"C:\Program Files\Adobe\Common\Plug-ins\7.0\MediaCore"
CEP_EXT = r"C:\Program Files (x86)\Common Files\Adobe\CEP\extensions"

SPAM_RE = re.compile(
    r"https?://|t\.me/|telegram|cgperec|cgplugin|subscribe|подпис|бесплатн|donate|usdt|website|сайт:|={5,}|\ufffd{3,}",
    re.I)
STEP_RE = re.compile(r"^\s*(?:step|шаг)?\s*(\d{1,2})\s*[.)\]:]+(?=\s|$)", re.I)
VERB_RE = re.compile(
    r"cop(?:y|ie)|move|paste|place|put|drag|install|extract|unzip|run|launch|"
    r"скопир|копир|перемест|вставь|помест|перетащ|установ|распак|запуст", re.I)
LABEL_RE = re.compile(r"^\s*(win(?:dows)?|pc|mac(?:\s*os)?(?:\s*x)?|osx)\s*[:\-–]\s*", re.I)
ABS_RE = re.compile(r"(?:%[A-Za-z_]+%|(?<![A-Za-z])[A-Za-z]:)[\\/][^\r\n<>|*?\"]*")
AE_REL_RE = re.compile(r"Adobe After Effects|Support Files[\\/]|Program Files", re.I)
PATH_CUT_RE = re.compile(
    r"(?<!Adobe)\s+(?:and|then|or|if|after|и|затем|или|если|после)\s+|[,;\"«»“”]|\.\s|\s{2,}|\t", re.I)
UNINSTALL_RE = re.compile(r"unin|unist|удал", re.I)

# у собранного exe __file__ лежит во временной папке — журнал пишем рядом с самим exe;
# при запуске из исходников — в корень проекта
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
LOG_FILE = APP_DIR / "install_log.txt"
# у GUI нет консоли — без этого флага каждый вызов 7z/reg/tasklist мигает чёрным окном
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- data

@dataclass
class Dest:
    kind: str            # "abs" — абсолютный путь, "ae" — путь относительно папки After Effects
    path: str
    guessed: bool = False

    def resolve(self, ae_roots):
        if self.kind == "abs":
            return [Path(self.path)]
        return [root / self.path for root in ae_roots]


@dataclass
class Action:
    kind: str            # copy | run | reg | zxp
    item: Path
    dest: Dest | None = None


@dataclass
class Step:
    num: int | None      # None — текст без номера (вступление или вся инструкция целиком)
    text: str
    actions: list = field(default_factory=list)


@dataclass
class Plan:
    archive: Path
    root: Path
    instruction: Path | None
    steps: list
    warnings: list = field(default_factory=list)

    @property
    def actions(self):
        return [a for s in self.steps for a in s.actions]

    def to_dict(self):
        return {
            "archive": str(self.archive), "root": str(self.root),
            "instruction": str(self.instruction) if self.instruction else None,
            "warnings": self.warnings,
            "steps": [{"num": s.num, "text": s.text,
                       "actions": [{"kind": a.kind, "item": str(a.item),
                                    "dest": vars(a.dest) if a.dest else None} for a in s.actions]}
                      for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d):
        steps = [Step(s["num"], s["text"],
                      [Action(a["kind"], Path(a["item"]), Dest(**a["dest"]) if a["dest"] else None)
                       for a in s["actions"]])
                 for s in d["steps"]]
        return cls(Path(d["archive"]), Path(d["root"]),
                   Path(d["instruction"]) if d["instruction"] else None, steps, d["warnings"])


# ---------------------------------------------------------------- archives

def find_7z():
    for p in (shutil.which("7z"), shutil.which("7za"),
              r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if p and Path(p).exists():
            return p
    return None


def is_archive(p: Path):
    name = p.name.lower()
    m = re.search(r"\.part(\d+)\.rar$", name)
    if m and int(m.group(1)) > 1:
        return False
    return p.suffix.lower() in ARCHIVE_EXT


def extract(archive: Path, dest: Path, password=None):
    sz = find_7z()
    if sz:
        cmd = [sz, "x", "-y", "-bso0", "-bsp0", f"-o{dest}", "-xr!*.dmg", "-xr!*.pkg",
               f"-p{password or ''}", str(archive)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if r.returncode not in (0, 1):
            msg = (r.stderr or r.stdout).strip()
            if "password" in msg.lower():
                msg = "архив защищён паролем — укажите --password"
            raise RuntimeError(msg)
        return
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not info.flag_bits & 0x800:  # имя не в UTF-8 — обычно cp866 из Windows
                    try:
                        info.filename = info.filename.encode("cp437").decode("cp866")
                    except UnicodeError:
                        pass
                z.extract(info, dest, pwd=password.encode() if password else None)
        return
    try:
        shutil.unpack_archive(str(archive), str(dest))
    except (shutil.ReadError, ValueError):
        raise RuntimeError("не найден 7-Zip — без него поддерживаются только zip и tar")


def extract_nested(root: Path, password=None):
    """Распаковывает вложенные архивы рядом с ними (один уровень)."""
    for p in list(root.rglob("*")):
        if p.is_file() and is_archive(p) and not is_mac(p.relative_to(root)):
            target = p.with_name(p.stem)
            if not target.exists():
                try:
                    extract(p, target, password)
                except RuntimeError:
                    pass


# ---------------------------------------------------------------- analysis

def is_mac(rel: Path):
    return any(part.lower() in MAC_DIRS for part in rel.parts)


def read_text(p: Path):
    b = p.read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace").replace("\r", "")
    if b[:3] == b"\xef\xbb\xbf":
        b = b[3:]
    lines = []
    for raw in b.split(b"\n"):
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(raw.decode("cp1251", "replace"))
    return "\n".join(lines).replace("\r", "")


def find_instructions(root: Path):
    good = re.compile(r"install|установ|инсталл|инструк|readme|read|how|setup|guide|manual", re.I)
    bad = re.compile(r"licen|лиценз|notice|changelog|eula|credits|third", re.I)
    # txt внутри самого плагина (CEP-панели, встроенные библиотеки) — не инструкция по установке
    bundles = {m.parent.parent for m in root.rglob("manifest.xml") if m.parent.name.lower() == "csxs"}
    found = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".txt", ".md", ".nfo"):
            continue
        rel = p.relative_to(root)
        if is_mac(rel) or bad.search(p.stem):
            continue
        if any(b in p.parents for b in bundles) or any(
                d.lower() in ("site-packages", "node_modules") or d.lower().endswith(".dist-info")
                for d in rel.parts[:-1]):
            continue
        score = 3 if good.search(p.stem) else 0
        if p.suffix.lower() == ".md" and not score:
            continue
        text = read_text(p)
        if ABS_RE.search(text) or AE_REL_RE.search(text) or VERB_RE.search(text):
            score += 2
        # .nfo часто лишь подпись релиза — берём его, только если txt-инструкции нет
        if score and p.suffix.lower() == ".nfo":
            score -= 1
        if score:
            found.append((score, -len(rel.parts), p))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [p for _, _, p in found]


def collect_items(root: Path):
    items = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if is_mac(rel) or len(p.name) < 3:
            continue
        if p.is_file() and p.suffix.lower() in JUNK_EXT:
            continue
        if p.is_dir() and p.name.lower() in PLATFORM_DIRS:
            continue
        items.append(p)
    return items


def split_steps(text: str):
    lines = [l.rstrip() for l in text.split("\n") if not SPAM_RE.search(l)]
    steps, cur, num, expected = [], [], None, 1  # num=None — вступление до первого номера
    started = False
    for line in lines:
        m = STEP_RE.match(line)
        # нумерация бывает и с нуля: «0. install Maxon App»
        if m and (int(m.group(1)) == expected or (not started and int(m.group(1)) == 0)):
            n = int(m.group(1))
            steps.append((num, cur))
            cur, num, expected, started = [line], n, n + 1, True
        else:
            cur.append(line)
    steps.append((num, cur))
    result = []
    for n, ls in steps:
        t = "\n".join(ls).strip()
        if t:
            result.append(Step(n, t))
    return result


def clean_path(raw: str):
    s = PATH_CUT_RE.split(raw, 1)[0]
    s = s.rstrip(" .:\\/")
    return s


def make_dest(p: str):
    parts = [c for c in re.split(r"[\\/]+", p) if c]
    for i, c in enumerate(parts):
        if c.lower().startswith("adobe after effects"):
            rest = [x for x in parts[i + 1:] if x not in (".", "..")]
            if not rest or rest[0].lower() != "support files":
                rest = ["Support Files"] + rest
            return Dest("ae", "\\".join(rest))
    if re.match(r"^[A-Za-z]:", p) or p.startswith("%"):
        if len(parts) < 2:
            return None
        return Dest("abs", os.path.expandvars(p).replace("/", "\\"))
    low = p.lower()
    if low.startswith("support files"):
        return Dest("ae", "\\".join(parts))
    if "program files" in low:
        return Dest("abs", "C:\\" + "\\".join(re.split(r"[\\/]+", p[low.index("program files"):])))
    return None


def find_paths(text: str):
    """Ищет пути назначения (Windows) в тексте шага. Возвращает [(start, end, Dest)]."""
    res, off = [], 0
    for line in text.split("\n"):
        line_start, off = off, off + len(line) + 1
        body, base = line, line_start
        m = LABEL_RE.match(line)
        if m:
            if m.group(1).lower().startswith(("mac", "osx")):
                continue
            body, base = line[m.end():], line_start + m.end()
        if re.search(r"/Applications|/Library|~/|^\s*Applications[\\/]", body):
            continue
        m = ABS_RE.search(body)
        if m:
            raw = m.group(0)
        else:
            m = AE_REL_RE.search(body)
            if not m or not re.search(r"[\\/]", body[m.start():]):
                continue
            raw = body[m.start():]
        cleaned = clean_path(raw)
        dest = make_dest(cleaned)
        if dest:
            res.append((base + m.start(), base + m.start() + len(cleaned), dest))
    return res


def find_mentions(text: str, items, masks):
    chars = list(text.lower())
    for s, e in masks:
        chars[s:e] = " " * (e - s)
    low = "".join(chars)
    hits = {}

    def scan(name, item):
        pat = r"(?<![\w.\\/-])" + re.escape(name.lower()) + r"(?![\w-]|\.\w)"
        m = re.search(pat, low)
        if m and (item not in hits or m.start() < hits[item]):
            hits[item] = m.start()

    for it in items:
        scan(it.name, it)
    if not hits:  # "copy the ScaleUp plug-in" — имя без расширения
        for it in items:
            if it.is_file() and it.suffix.lower() in PLUGIN_EXT | INSTALLER_EXT and len(it.stem) >= 4:
                scan(it.stem, it)
    # одноимённые — берём самый неглубокий; вложенные в найденную папку — выкидываем
    by_name = {}
    for it in hits:
        key = it.name.lower()
        if key not in by_name or len(it.parts) < len(by_name[key].parts):
            by_name[key] = it
    chosen = set(by_name.values())
    chosen = {it for it in chosen if not any(o != it and o in it.parents for o in chosen)}
    return sorted(((hits[it], it) for it in chosen), key=lambda t: t[0])


def is_cep_folder(p: Path):
    return p.is_dir() and (p / "CSXS" / "manifest.xml").exists()


def default_dest(item: Path):
    ext = item.suffix.lower() if item.is_file() else ""
    if is_cep_folder(item) or ext == ".zxp":
        return Dest("abs", CEP_EXT, guessed=True)
    if ext in (".jsx", ".jsxbin"):
        return Dest("ae", r"Support Files\Scripts\ScriptUI Panels", guessed=True)
    if ext == ".ffx":
        return Dest("ae", r"Support Files\Presets", guessed=True)
    if ext in (".aex", ".prm", ".blaceplugin") or (item.is_dir() and any(item.rglob("*.aex"))):
        return Dest("abs", MEDIACORE, guessed=True)
    return None


def action_kind(item: Path):
    ext = item.suffix.lower() if item.is_file() else ""
    if ext in INSTALLER_EXT:
        return "run"
    if ext == ".reg":
        return "reg"
    if ext == ".zxp":
        return "zxp"
    return "copy"


def auto_payload(root: Path, items):
    cep = [it for it in items if is_cep_folder(it)]
    files = [it for it in items if it.is_file() and it.suffix.lower() in PLUGIN_EXT
             and not any(c in it.parents for c in cep)]
    return cep + files


def analyze(archive: Path, root: Path):
    """Строит план установки для уже распакованного в root архива."""
    instr = find_instructions(root)
    items = collect_items(root)
    plan = Plan(archive, root, instr[0] if instr else None, [])
    if len(instr) > 1:
        plan.warnings.append("найдено несколько инструкций, использую первую; остальные: "
                             + ", ".join(str(p.relative_to(root)) for p in instr[1:]))
    used = set()

    if plan.instruction:
        steps = split_steps(read_text(plan.instruction))
        numbered = any(s.num is not None for s in steps)
        for step in steps:
            paths = find_paths(step.text)
            mentions = find_mentions(step.text, items, [(s, e) for s, e, _ in paths])
            has_verb = bool(VERB_RE.search(step.text))
            for pos, it in mentions:
                kind = action_kind(it)
                if kind == "run":
                    if not has_verb or UNINSTALL_RE.search(it.name):
                        continue
                    step.actions.append(Action("run", it))
                elif kind == "reg":
                    step.actions.append(Action("reg", it))
                else:
                    after = [d for s, _, d in paths if s > pos]
                    before = [d for s, _, d in paths if s < pos]
                    dest = after[0] if after else (before[-1] if before else None)
                    if dest is None:
                        # «run install-as-admin on Mac» — это не для Windows
                        if not has_verb or it in used or "mac" in step.text[pos:pos + len(it.name) + 12].lower():
                            continue
                        dest = default_dest(it)
                        if dest is None:
                            plan.warnings.append(f"не понял, куда копировать «{it.name}» — пропускаю")
                            continue
                    step.actions.append(Action(kind, it, dest))
                used.add(it)
            # «скопируйте плагин в C:\...» без имени файла — берём сам плагин
            if not mentions and paths and has_verb:
                for it in auto_payload(root, items):
                    if it not in used:
                        step.actions.append(Action(action_kind(it), it, paths[0][2]))
                        used.add(it)
        if numbered:  # вводный текст до «1.» без действий — просто заголовок
            steps = [s for s in steps if s.num is not None or s.actions]
        plan.steps = steps

    if not plan.actions:
        payload = auto_payload(root, items)
        if plan.instruction:
            plan.warnings.append("в инструкции не нашлось понятных действий — ставлю по типу файлов")
        else:
            plan.warnings.append("инструкция не найдена — ставлю по типу файлов")
        step = Step(None, "")
        for it in payload:
            d = default_dest(it)
            if d:
                step.actions.append(Action(action_kind(it), it, d))
        plan.steps.append(step)
    return plan


def action_verb(a: Action):
    if a.kind == "run":
        return "запустить установщик"
    if a.kind == "reg":
        return "импортировать в реестр"
    if a.kind == "zxp":
        return "распаковать ZXP"
    return "скопировать папку" if a.item.is_dir() else "скопировать"


def action_targets(a: Action, ae_roots):
    """Куда попадёт copy/zxp: [(папка, уже_есть, папка_не_похожа_на_Adobe)]."""
    if not a.dest:
        return []
    return [(t, a.kind == "copy" and (t / a.item.name).exists(), "adobe" not in str(t).lower())
            for t in a.dest.resolve(ae_roots)]


def remaining_manual(plan: Plan):
    """Ручные шаги после последнего автоматического."""
    tail = []
    for step in reversed(plan.steps):
        if step.actions:
            break
        if step.text:
            tail.insert(0, step.text)
    return tail


# ---------------------------------------------------------------- environment

def find_ae_installs():
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Adobe"
    roots = [p for p in base.glob("Adobe After Effects*") if (p / "Support Files" / "AfterFX.exe").exists()]
    return sorted(roots, key=lambda p: p.name)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ae_running():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AfterFX.exe", "/NH"],
                       capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    return "afterfx.exe" in r.stdout.lower()


def sandboxed(p: Path, sandbox):
    """В режиме песочницы C:\\X\\Y превращается в <sandbox>\\C\\X\\Y."""
    if not sandbox:
        return p
    drive = p.drive.rstrip(":") or "_"
    return Path(sandbox) / drive / Path(*p.parts[1:])


def set_log_file(path: Path):
    global LOG_FILE
    LOG_FILE = Path(path)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(line):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- execution

def run_action(a: Action, plan: Plan, ui, ae_roots, sandbox=None):
    name = a.item.relative_to(plan.root)
    if a.kind in ("run", "reg") and sandbox:
        ui.info(f"[sandbox] пропущено: {a.kind} {name}")
        return True
    if a.kind == "run":
        ui.warn(f"Запускаю установщик {a.item.name} — пройдите его до конца, утилита подождёт...")
        cmd = ["msiexec", "/i", str(a.item)] if a.item.suffix.lower() == ".msi" else [str(a.item)]
        r = subprocess.run(cmd, cwd=a.item.parent)
        log(f"{plan.archive.name}: запуск {a.item.name} (код {r.returncode})")
        if r.returncode != 0:
            ui.warn(f"Установщик завершился с кодом {r.returncode}")
            return ui.ask("Продолжить?")
        ui.ok("установщик завершён")
        return True
    if a.kind == "reg":
        r = subprocess.run(["reg", "import", str(a.item)], capture_output=True, text=True, errors="replace",
                           creationflags=NO_WINDOW)
        log(f"{plan.archive.name}: reg import {a.item.name} (код {r.returncode})")
        if r.returncode != 0:
            ui.error(f"reg import: {(r.stderr or r.stdout).strip()}")
            return False
        ui.ok(f"импортирован {a.item.name}")
        return True

    ok = True
    for d in a.dest.resolve(ae_roots):
        d = sandboxed(d, sandbox)
        try:
            d.mkdir(parents=True, exist_ok=True)
            if a.kind == "zxp":
                with zipfile.ZipFile(a.item) as z:
                    try:
                        m = re.search(rb'ExtensionBundleId="([^"]+)"', z.read("CSXS/manifest.xml"))
                        bundle = m.group(1).decode() if m else a.item.stem
                    except KeyError:
                        bundle = a.item.stem
                    target = d / bundle
                    z.extractall(target)
            elif a.item.is_dir():
                target = d / a.item.name
                shutil.copytree(a.item, target, dirs_exist_ok=True)
            else:
                target = d / a.item.name
                shutil.copy2(a.item, target)
            ui.ok(f"{name} → {target}")
            log(f"{plan.archive.name}: {name} -> {target}")
        except PermissionError as e:
            ui.error(f"нет доступа: {e.filename or d} — нужны права администратора "
                     f"или файл занят (закройте After Effects)")
            ok = False
        except OSError as e:
            ui.error(f"{name}: {e}")
            ok = False
    return ok


def execute(plan: Plan, ui, ae_roots, sandbox=None):
    """Выполняет план по шагам. Возвращает True, если всё прошло без ошибок."""
    ok = True
    steps = plan.steps
    for i, step in enumerate(steps):
        later_actions = any(s.actions for s in steps[i + 1:])
        if step.text:
            ui.step(step.text)
        if not step.actions:
            if step.text and later_actions:
                ui.manual(step.text)
            continue
        for a in step.actions:
            if not run_action(a, plan, ui, ae_roots, sandbox):
                ok = False
                if not ui.ask("Ошибка. Продолжить установку этого архива?", default=False):
                    return False
    tail = remaining_manual(plan)
    if tail:
        ui.remaining(tail)
    return ok
