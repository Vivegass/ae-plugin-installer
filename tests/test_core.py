"""Тесты разбора инструкций и установки. Архивы не нужны: «распакованный архив»
собирается из файлов прямо в тесте, тексты повторяют реальные инструкции."""

import zipfile
from pathlib import Path

import pytest

from ae_plugin_installer import core

MEDIACORE = core.MEDIACORE
SPAM = ("\n\nСкачивайте бесплатно и повышайте свой уровень!\n"
        "Subscribe to CGPerec - https://t.me/cgperec and get even more free content!\n"
        "============================================================================\n")


def make(root: Path, files: dict):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return root


def plan_for(tmp_path, files):
    root = make(tmp_path / "unpacked", files)
    return core.analyze(tmp_path / "plugin.rar", root)


def actions(plan):
    return [(a.kind, a.item.name, a.dest.kind if a.dest else None, a.dest.path if a.dest else None)
            for a in plan.actions]


class RecordingUI:
    def __init__(self, answer=True):
        self.calls, self.answer = [], answer

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            return self.answer if name == "ask" else None
        return record


# ---------------------------------------------------------------- пути назначения

def test_absolute_path_on_next_line(tmp_path):
    plan = plan_for(tmp_path, {
        "Ballpoint/Install.txt": "Copy the BallPoint.aex file to the following path:\n"
                                 r"C:\Program Files\Adobe\Common\Plug-ins\7.0\MediaCore" + SPAM,
        "Ballpoint/BallPoint.aex": b"x",
    })
    assert actions(plan) == [("copy", "BallPoint.aex", "abs", MEDIACORE)]
    assert not plan.actions[0].dest.guessed


def test_ae_relative_path_is_not_cut_at_word_after(tmp_path):
    # «Adobe After Effects» раньше обрезалось на слове «After», как на союзе
    plan = plan_for(tmp_path, {
        "Win/read.txt": "Copy the AE Glass.aex file to the AE plug-ins directory, for example:\n"
                        r"....\Adobe After Effects 2026\Support Files\Plug-ins",
        "Win/AE Glass.aex": b"x",
        "Mac/AE Glass.pkg": b"x",
    })
    assert actions(plan) == [("copy", "AE Glass.aex", "ae", r"Support Files\Plug-ins")]


def test_numbered_steps_skip_mac_lines_and_keep_manual_steps(tmp_path):
    plan = plan_for(tmp_path, {
        "Install.txt": "【Script Installation】\n\n"
                       "1. Copy the Light Flares.jsxbin file to the AE script directory:\n\n"
                       r"Win: ...Adobe After Effects CC\Support Files\Scripts\ScriptUI Panels" "\\\n"
                       r"Mac: Applications\Adobe After Effects CC\Scripts\ScriptUI Panels" "\\\n\n"
                       "2. Open AE Preferences and enable *Allow Scripts to Write Files and Access Network\n\n"
                       "3. You can find the Light Flares.jsxbin script under the Window menu." + SPAM,
        "Light Flares.jsxbin": b"x",
    })
    assert [s.num for s in plan.steps] == [1, 2, 3]
    assert actions(plan) == [("copy", "Light Flares.jsxbin", "ae", r"Support Files\Scripts\ScriptUI Panels")]
    assert len(core.remaining_manual(plan)) == 2


def test_russian_cp1251_with_path_inside_sentence(tmp_path):
    text = ("1. Скопируйте файл MyFx.aex в папку C:\\Program Files\\Adobe\\Common\\Plug-ins\\7.0\\MediaCore "
            "и перезапустите AE.\r\n"
            "2. Скрипт Tool.jsx положите в ...\\Adobe After Effects 2025\\Support Files\\Scripts\r\n")
    plan = plan_for(tmp_path, {
        "MyFx/Установка.txt": text.encode("cp1251"),
        "MyFx/MyFx.aex": b"x",
        "MyFx/Tool.jsx": b"x",
    })
    assert actions(plan) == [
        ("copy", "MyFx.aex", "abs", MEDIACORE),
        ("copy", "Tool.jsx", "ae", r"Support Files\Scripts"),
    ]


@pytest.mark.parametrize("line", [
    "- `" + MEDIACORE + "`",                # Markdown: путь в обратных кавычках (Photo Mosaic)
    "**" + MEDIACORE + "**",                # выделение жирным
    "'" + MEDIACORE + "'",                  # одинарные кавычки
    "«" + MEDIACORE + "».",                 # ёлочки и точка в конце предложения
])
def test_path_wrapped_in_markup_or_quotes(tmp_path, line):
    plan = plan_for(tmp_path, {
        "Win/read.txt": "**Installation Instructions**\n"
                        "Copy the **PhotoMosaic.aex** file to the following path:\n" + line + "\n",
        "Win/PhotoMosaic.aex": b"x",
    })
    assert actions(plan) == [("copy", "PhotoMosaic.aex", "abs", MEDIACORE)]


def test_plan_marks_missing_destination_folder(tmp_path):
    plan = plan_for(tmp_path, {"read.txt": "Copy Fx.aex to " + str(tmp_path / "no such folder"),
                               "Fx.aex": b"x"})
    [(_, exists, _, missing)] = core.action_targets(plan.actions[0], [])
    assert missing and not exists


def test_file_mentioned_without_extension(tmp_path):
    plan = plan_for(tmp_path, {
        "Win/read.txt": "Simply copy the ScaleUp plug-in into the following plug-in directory:\n" + MEDIACORE,
        "Win/ScaleUp.aex": b"x",
    })
    assert actions(plan) == [("copy", "ScaleUp.aex", "abs", MEDIACORE)]


def test_several_items_and_folder_to_one_path(tmp_path):
    plan = plan_for(tmp_path, {
        "Windows/read.txt": "Copy the SPBokehAE.aex file and the DirectX_Assets folder to the following path:\n"
                            + MEDIACORE,
        "Windows/SPBokehAE.aex": b"x",
        "Windows/DirectX_Assets/genMip.cso": b"x",
    })
    assert actions(plan) == [("copy", "SPBokehAE.aex", "abs", MEDIACORE),
                             ("copy", "DirectX_Assets", "abs", MEDIACORE)]


# ---------------------------------------------------------------- установщики, реестр, ручные шаги

def test_installer_then_manual_step_then_copy_in_order(tmp_path):
    plan = plan_for(tmp_path, {
        "CK/Установка.txt": "Installation Instructions:\n\n"
                            "1. Install the plugin CorridorKey-v1.5.exe. Check the GPU box if needed.\n\n"
                            "2. During installation, modules will be downloaded online. Please wait.\n\n"
                            "3. After installation copy the CorridorKey.blaceplugin file to the following path:\n"
                            + MEDIACORE + r"\CorridorKey",
        "CK/CorridorKey-v1.5.exe": b"x",
        "CK/CorridorKey.blaceplugin": b"x",
        "CK/unistaller.exe": b"x",
    })
    assert [s.num for s in plan.steps] == [1, 2, 3]
    assert actions(plan) == [("run", "CorridorKey-v1.5.exe", None, None),
                             ("copy", "CorridorKey.blaceplugin", "abs", MEDIACORE + r"\CorridorKey")]
    assert not plan.steps[1].actions  # шаг 2 — ручной


def test_reg_file_and_cep_panel(tmp_path):
    plan = plan_for(tmp_path, {
        "kit/installation.txt": "1). add keys.reg\n\n"
                                r"2). ankistedo.kit to C:\Program Files (x86)\Common Files\Adobe\CEP\extensions" "\n\n"
                                "3). open after effects > window > extensions > ankistedokit\n",
        "kit/Add Keys.reg": "Windows Registry Editor Version 5.00\n",
        "kit/ankistedo.kit/CSXS/manifest.xml": "<ExtensionManifest/>",
        "kit/ankistedo.kit/index.html": "<html/>",
    })
    assert actions(plan) == [("reg", "Add Keys.reg", None, None),
                             ("copy", "ankistedo.kit", "abs", core.CEP_EXT)]


def test_mac_only_item_is_skipped_quietly(tmp_path):
    plan = plan_for(tmp_path, {
        "A/Installation method.txt": "1. Copy the com.test.panel folder to the following location:\n"
                                     "Win : " + core.CEP_EXT + "\\\n"
                                     "2. Run Add Keys.reg on Win and install-as-admin on Mac.\n",
        "A/Add Keys.reg": "Windows Registry Editor Version 5.00\n",
        "A/install-as-admin/run.sh": "#!/bin/sh",
        "A/com.test.panel/CSXS/manifest.xml": "<ExtensionManifest/>",
        "A/com.test.panel/server/README.txt": "How to install this python package",
    })
    assert actions(plan) == [("copy", "com.test.panel", "abs", core.CEP_EXT),
                             ("reg", "Add Keys.reg", None, None)]
    assert plan.warnings == []  # нет ни «не понял, куда копировать», ни «несколько инструкций»


# ---------------------------------------------------------------- выбор инструкции

def test_without_instruction_installs_by_file_type(tmp_path):
    plan = plan_for(tmp_path, {
        "Panel/CSXS/manifest.xml": "<ExtensionManifest/>",
        "Other.aex": b"x",
        "Tool.jsxbin": b"x",
    })
    got = sorted(actions(plan))
    assert got == sorted([("copy", "Panel", "abs", core.CEP_EXT),
                          ("copy", "Other.aex", "abs", MEDIACORE),
                          ("copy", "Tool.jsxbin", "ae", r"Support Files\Scripts\ScriptUI Panels")])
    assert all(a.dest.guessed for a in plan.actions)
    assert "инструкция не найдена" in plan.warnings[0]


def test_nfo_is_read_and_steps_may_start_from_zero(tmp_path):
    plan = plan_for(tmp_path, {
        "R/VR.nfo": "\tTool 1.0 (Win64)\n\n\tInstallation sequence\n\t0. install Maxon App\n\t1. install Tool\n",
        "R/Setup_x64.exe": b"x",
    })
    assert plan.instruction.name == "VR.nfo"
    assert [s.num for s in plan.steps if s.text] == [0, 1]
    assert plan.actions == []


def test_txt_instruction_wins_over_release_nfo(tmp_path):
    plan = plan_for(tmp_path, {
        "L/1nv3rt.nfo": "! imfamous !\nvisit for more https://example.org\n",
        "L/Install.txt": "1. Copy the Light Flares.jsxbin file to the AE script directory:\n"
                         r"Win: ...Adobe After Effects CC\Support Files\Scripts\ScriptUI Panels",
        "L/Light Flares.jsxbin": b"x",
    })
    assert plan.instruction.name == "Install.txt"


def test_split_steps_drops_spam_and_needs_sequential_numbers():
    steps = core.split_steps("1. Copy A.aex\nversion 2.5 GB\n3. not a step\n2. Copy B.aex" + SPAM)
    assert [s.num for s in steps] == [1, 2]
    assert "t.me" not in steps[-1].text


# ---------------------------------------------------------------- выполнение

def test_plan_survives_json_roundtrip(tmp_path):
    plan = plan_for(tmp_path, {"read.txt": "Copy X.aex to " + MEDIACORE, "X.aex": b"x"})
    again = core.Plan.from_dict(plan.to_dict())
    assert actions(again) == actions(plan)
    assert again.root == plan.root


def test_execute_in_sandbox_copies_files_and_skips_reg(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LOG_FILE", tmp_path / "log.txt")
    plan = plan_for(tmp_path, {
        "kit/installation.txt": "1. add keys.reg\n2. Copy the Fx.aex file and the Assets folder to " + MEDIACORE,
        "kit/keys.reg": "Windows Registry Editor Version 5.00\n",
        "kit/Fx.aex": b"aex",
        "kit/Assets/a.bin": b"bin",
    })
    sandbox = tmp_path / "sandbox"
    ui = RecordingUI()
    assert core.execute(plan, ui, ae_roots=[], sandbox=str(sandbox))
    target = core.sandboxed(Path(MEDIACORE), str(sandbox))
    assert (target / "Fx.aex").read_bytes() == b"aex"
    assert (target / "Assets" / "a.bin").read_bytes() == b"bin"
    assert any(name == "info" and "[sandbox]" in args[0] for name, args in ui.calls)


def test_extract_zip_with_cyrillic_names(tmp_path):
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Плагин/Установка.txt", "Copy Fx.aex to " + MEDIACORE)
        z.writestr("Плагин/Fx.aex", b"x")
    out = tmp_path / "out"
    core.extract(archive, out)
    assert (out / "Плагин" / "Установка.txt").exists()
    plan = core.analyze(archive, out)
    assert actions(plan) == [("copy", "Fx.aex", "abs", MEDIACORE)]


# ---------------------------------------------------------------- распаковщики

class FakeTool:
    """Подставной распаковщик: behaviour — 'ok', 'fail', 'password' или 'empty'."""

    def __init__(self, name, behaviour, only_rar=False):
        self.name, self.behaviour, self.only_rar, self.called = name, behaviour, only_rar, False

    def handles(self, archive):
        return archive.suffix.lower() == ".rar" if self.only_rar else True

    def run(self, archive, dest, password=None):
        self.called = True
        if self.behaviour == "fail":
            raise RuntimeError("не смог")
        if self.behaviour == "password":
            raise core.PasswordRequired("Wrong password")
        if self.behaviour == "ok":
            (dest / "file.aex").write_bytes(b"x")


def test_extract_falls_back_to_next_tool(tmp_path):
    first, empty, second = FakeTool("A", "fail"), FakeTool("B", "empty"), FakeTool("C", "ok")
    used = core.extract(tmp_path / "x.rar", tmp_path / "out", extractors=[first, empty, second])
    assert used == "C" and first.called and empty.called
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["file.aex"]


def test_password_error_stops_the_chain(tmp_path):
    first, second = FakeTool("A", "password"), FakeTool("B", "ok")
    with pytest.raises(RuntimeError, match="нужен пароль"):
        core.extract(tmp_path / "x.7z", tmp_path / "out", extractors=[first, second])
    assert not second.called
    with pytest.raises(RuntimeError, match="неверный пароль"):
        core.extract(tmp_path / "x.7z", tmp_path / "out", password="bad", extractors=[FakeTool("A", "password")])


def test_rar_only_tool_is_skipped_for_other_formats(tmp_path):
    unrar = FakeTool("UnRAR", "ok", only_rar=True)
    with pytest.raises(RuntimeError, match="нет распаковщика"):
        core.extract(tmp_path / "x.7z", tmp_path / "out", extractors=[unrar])
    assert not unrar.called


def test_zip_without_external_tools_uses_python(tmp_path):
    archive = tmp_path / "p.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Fx.aex", b"x")
    assert core.extract(archive, tmp_path / "out", extractors=[]) == "Python"


def test_extractor_command_lines():
    a, d = Path(r"C:\in\p.rar"), Path(r"C:\out")
    unrar = core.Extractor("UnRAR", "unrar", "UnRAR.exe")
    assert "-p-" in unrar.command(a, d, None) and "-p123" in unrar.command(a, d, "123")
    assert unrar.command(a, d, None)[-1] == "C:\\out\\"
    assert "-inul" in core.Extractor("WinRAR", "winrar", "WinRAR.exe").command(a, d, None)
    assert "-p" in core.Extractor("7-Zip", "7z", "7z.exe").command(a, d, None)  # пустой пароль: не спрашивать
    assert not unrar.handles(Path("p.7z")) and unrar.handles(Path("p.RAR"))


@pytest.mark.parametrize("name, expected", [
    ("plugin.rar", True), ("plugin.ZIP", True), ("a.part1.rar", True), ("a.part2.rar", False),
    ("readme.txt", False), ("plugin.aex", False),
])
def test_is_archive(name, expected):
    assert core.is_archive(Path(name)) is expected
