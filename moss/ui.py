"""The look of `moss setup` and the `moss` menu: questionary for questions,
rich for colour, boxes and spinners. One small module so every screen
looks the same."""

from __future__ import annotations

import sys

try:
    import questionary
    from questionary import Choice, Separator
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except ImportError as exc:                      # pragma: no cover - install problem
    raise SystemExit(f"the menu needs two more packages ({exc.name}). Run:\n"
                     f"  pip install -r requirements.txt\n"
                     f"or reinstall with the one-line installer.") from exc

console = Console(highlight=False)
MOSS_GREEN = "#7bc47f"
STYLE = questionary.Style([
    ("qmark", f"fg:{MOSS_GREEN} bold"),
    ("question", "bold"),
    ("pointer", f"fg:{MOSS_GREEN} bold"),
    ("highlighted", f"fg:{MOSS_GREEN} bold"),
    ("selected", f"fg:{MOSS_GREEN}"),
    ("answer", f"fg:{MOSS_GREEN} bold"),
    ("instruction", "fg:#888888"),
])

__all__ = ["Back", "Choice", "Separator", "Table", "choose", "text", "secret", "confirm",
           "path", "header", "ok", "warn", "fail", "note", "spin", "pause", "console",
           "interactive"]


class Back(Exception):
    """The person pressed Ctrl+C or Esc: go back a step."""


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(q):
    answer = q.ask()
    if answer is None:
        raise Back()
    return answer


def choose(question: str, choices: list, default=None):
    """choices: plain strings, Choice(title, value) or Separator()."""
    return _ask(questionary.select(question, choices=choices, default=default, style=STYLE,
                                   qmark="›", instruction=" "))


def text(question: str, default: str = "", validate=None) -> str:
    return _ask(questionary.text(question, default=default, validate=validate, style=STYLE,
                                 qmark="›")).strip()


def secret(question: str) -> str:
    return _ask(questionary.password(question, style=STYLE, qmark="›"))


def confirm(question: str, default: bool = True) -> bool:
    return _ask(questionary.confirm(question, default=default, style=STYLE, qmark="›"))


def path(question: str, default: str = "", files: bool = True) -> str:
    return _ask(questionary.path(question, default=default, only_directories=not files,
                                 style=STYLE, qmark="›")).strip()


def header(title: str, *lines: str) -> None:
    body = "\n".join(lines) if lines else ""
    console.print(Panel(body, title=f"[bold]{title}[/]", title_align="left",
                        border_style=MOSS_GREEN, padding=(0, 2)))


def ok(msg: str) -> None:
    console.print(f"[{MOSS_GREEN}]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"[yellow]![/] {msg}")


def fail(msg: str) -> None:
    console.print(f"[red]✗[/] {msg}")


def note(msg: str) -> None:
    console.print(f"[dim]{msg}[/]")


def spin(msg: str):
    return console.status(msg, spinner="dots", spinner_style=MOSS_GREEN)


def pause() -> None:
    try:
        questionary.press_any_key_to_continue("press any key to go back", style=STYLE).ask()
    except (KeyboardInterrupt, EOFError):
        pass
