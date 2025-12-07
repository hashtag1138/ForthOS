#!/usr/bin/env python3
from __future__ import annotations

import os
import threading
from typing import Dict, List, Callable, Optional

import asyncio
import queue
import shutil
from prompt_toolkit.application.current import get_app
from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.clipboard import InMemoryClipboard
try:
    # Si pyperclip est installé + xclip/xsel dispo,
    # on utilise le presse-papier système.
    from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
except ImportError:
    PyperclipClipboard = None

from forthos_tcp import TuiTCPClient
from forthos_host_repl import DOT_CMDS
from forthos_tui_editor import EditorScreen


# ======================================================================
# Complétion host + mots Forth
# ======================================================================

class HostWordCompleter(Completer):
    def __init__(self, get_words: Callable[[], list[str]],
                 get_history: Callable[[], list[str]]):
        self.get_words = get_words
        self.get_history = get_history
        self.host_cmds = [
            ":host new",
            ":host vm",
            ":host ps",
            ":host kill",
            ":host gc-dead",
            ":host logs",
            ":host read-from",
            ":host reset",
            ":host quit",
            ":host help",
        ]
        self.dot_cmds = sorted(DOT_CMDS)


    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.strip()
        word = document.get_word_before_cursor(WORD=True) or ""

        # ---------- commandes :host ----------
        if stripped.startswith(":host"):
            # 1) complétion des commandes host
            for cmd in self.host_cmds:
                if cmd.startswith(stripped):
                    # remplacer toute la ligne par la commande complète
                    yield Completion(cmd, start_position=-len(text))

            # 2) complétion dot-commands : :host .stack, .gc ...
            if stripped.startswith(":host ."):
                prefix = stripped[len(":host "):].strip()
                start = -len(text)
                for dc in self.dot_cmds:
                    if dc.startswith(prefix):
                        full = f".{dc.lstrip('.')}"
                        yield Completion(full, start_position=start)


        # ---------- mots Forth ----------
        # complétion sur le dernier "mot" tapé
        prefix = word
        start_word = -len(prefix)
        for w in self.get_words():
            if w.upper().startswith(prefix.upper()):
                yield Completion(w, start_position=start_word)




# ======================================================================
# BaseScreen + ScreenManager
# ======================================================================

class BaseScreen:
    name: str

    @property
    def container(self):
        raise NotImplementedError

    def on_show(self):
        pass

    def on_hide(self):
        pass


class ScreenManager:
    def __init__(self, screens: List[BaseScreen]):
        self.screens: Dict[str, BaseScreen] = {s.name: s for s in screens}
        self.current: BaseScreen = screens[0]

    @property
    def container(self):
        return self.current.container

    def show(self, name: str):
        if name not in self.screens:
            return
        new = self.screens[name]
        if new is self.current:
            return
        self.current.on_hide()
        self.current = new
        self.current.on_show()


# ======================================================================
# F1 : DocScreen (doc.md)
# ======================================================================

class DocScreen(BaseScreen):
    def __init__(self, filename: str):
        self.name = "doc"
        self.filename = filename
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                text = f"Erreur de lecture {filename!r}: {e}\n"
        else:
            text = f"(Fichier {filename!r} introuvable)\n"

        self.text_area = TextArea(
            text=text,
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
        )

    @property
    def container(self):
        return self.text_area

    def on_show(self):
        from prompt_toolkit.application.current import get_app
        try:
            get_app().layout.focus(self.text_area)
        except Exception:
            pass


# ======================================================================
# F2 : ConsoleScreen (host + VMs)
# ======================================================================

class ConsoleScreen(BaseScreen):
    def __init__(self):
        self.name = "console"

        # Host ForthOS réel
        try:
            self.host = TuiTCPClient("127.0.0.1", 4242)
        except OSError as e:
            raise SystemExit(
                f"Impossible de se connecter au host TCP (127.0.0.1:4242) : {e}\n"
                "Lance d'abord : python forthos_tcp.py serve"
            )

        self.current_pid = self.host.new_vm("main", entry="INTERPRET-AGAIN")
        
        # Taille max du log par VM = 1 "écran"
        cols, rows = shutil.get_terminal_size((80, 24))
        # Tu peux ajuster (par ex. rows - 4 si tu as des barres en haut/bas)
        self.max_log_chars = cols * rows

        # Logs par VM
        self._logs: Dict[int, List[str]] = {}

        # --- NEW: PIDs muets ---
        self.muted_pids: set[int] = set()

        # Queue interne pour les logs à afficher dans le TUI
        self._ui_log_queue: "queue.Queue[tuple[int, str]]" = queue.Queue()

        # Zone de logs (mutable !)
        self.log_area = TextArea(
            text="ForthOS console TUI (F2)\nTape Forth ou :host help\n\n",
            read_only=False,
            scrollbar=True,
            wrap_lines=True,
        )

        # Liste de VMs : colonne étroite
        self.vm_list = Window(
            content=FormattedTextControl(self._render_vm_list),
            always_hide_cursor=True,
            wrap_lines=False,
            width=Dimension(preferred=22, max=26),  # ~ajusté au texte
        )
                # Logs par VM
        self._logs: Dict[int, List[str]] = {}

        # Liste de VMs : colonne étroite
        self.vm_list = Window(
            content=FormattedTextControl(self._render_vm_list),
            always_hide_cursor=True,
            wrap_lines=False,
            width=Dimension(preferred=22, max=26),
        )

        # === Historique de commandes (mémoire + disque) ===
        self._history: list[str] = []
        self._history_file = os.path.expanduser("~/.forthos_history")

        if os.path.exists(self._history_file):
            try:
                with open(self._history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        cmd = line.rstrip("\n")
                        if cmd:
                            self._history.append(cmd)
            except OSError:
                pass


        # Completer basé sur mots Forth + historique
        self.completer = HostWordCompleter(self._get_vm_words, self._get_history)

        # Input buffer + fenêtre
        self.input_buffer = Buffer(
            completer=self.completer,
            multiline=False,
        )

        # Pré-remplir l'historique du buffer pour ↑ / ↓
        for cmd in self._history:
            self.input_buffer.history.append_string(cmd)


        self.input_window = Window(
            content=BufferControl(buffer=self.input_buffer, focusable=True),
            height=1,
        )
        
        # Ligne de suggestions de complétion (5 prochains candidats max)
        self.completion_window = Window(
            content=FormattedTextControl(self._render_completion_preview),
            height=1,
        )

        # Prompt
        self.prompt_window = Window(
            content=FormattedTextControl(self._prompt_text),
            height=1,
        )

        # Barre de status
        self.status_bar = Window(
            content=FormattedTextControl(
                lambda: "F1: Doc | F2: Console | F3: Editor | Ctrl-C/Ctrl-Q: Quit"
            ),
            height=1,
            style="reverse",
        )
        
        #layout console
        self._container = HSplit(
            [
                VSplit(
                    [
                        self.log_area,
                        Window(width=1, char="│"),
                        self.vm_list,
                    ]
                ),
                Window(height=1, char="─"),
                self.prompt_window,
                self.input_window,
                self.completion_window,   # <- nouvelle ligne de suggestions
                self.status_bar,
            ]
        )


        # Thread de lecture stdout_queue
        self._stop_event = threading.Event()
        self._stdout_thread = threading.Thread(
            target=self._stdout_worker, daemon=True
        )
        self._stdout_thread.start()

    @property
    def container(self):
        return self._container

    def on_show(self):
        from prompt_toolkit.application.current import get_app
        try:
            get_app().layout.focus(self.input_window)
        except Exception:
            pass

    # --- Helpers host / VM ---

    def _prompt_text(self) -> str:
        return f"[pid={self.current_pid}] forthos> "

    def _get_vm(self, pid: int):
        return self.host.vms.get(pid)

    def _get_vm(self, pid: int):
        """
        En mode 100% TCP, on ne voit plus les objets VM Python.
        On peut éventuellement retourner les métadonnées d'une VM (via list_vms),
        mais on n'expose plus vm.dict ici.
        """
        try:
            vms = self.host.list_vms()
        except Exception:
            return None
        for info in vms:
            if info.get("pid") == pid:
                return info
        return None

    def _get_vm_words(self) -> List[str]:
        """
        Retourne la liste des mots Forth connus de la VM courante
        pour la complétion. En mode TCP, c'est le serveur qui fournit
        la liste via RPC get_words.
        """
        try:
            return self.host.get_words(self.current_pid)
        except Exception:
            return []
        
    def _render_completion_preview(self) -> str:
        """
        Affiche jusqu'à 5 suggestions de complétion pour l'input courant.
        On ne change pas la logique de complétion (Tab reste identique),
        on expose juste les candidats sous forme de liste compacte.
        """
        text = self.input_buffer.text
        if not text.strip():
            return ""

        doc = Document(
            text,
            self.input_buffer.cursor_position,
        )
        event = CompleteEvent(completion_requested=True)

        try:
            completions = list(self.completer.get_completions(doc, event))
        except Exception:
            return ""

        if not completions:
            return ""

        # On affiche les 5 premiers candidats possibles
        labels = [c.text for c in completions[:5]]

        # Option simple : juste les lister séparés par deux espaces
        return "  ".join(labels)



    def _render_vm_list(self) -> str:
        vms = self.host.list_vms()
        lines = [" VMs:", " PID  ALIVE  NAME", "-------------------"]
        for info in vms:
            mark = ">" if info["pid"] == self.current_pid else " "
            alive = "Y" if info.get("alive") else "N"
            lines.append(f"{mark} {info['pid']:3d}   {alive}    {info.get('name','')}")
        if len(lines) == 3:
            lines.append(" (no VM)")
        return "\n".join(lines)

    # --- Logs ---

    def _append_log(self, text: str) -> None:
        buf = self.log_area.buffer

        # Toujours se recaler à la fin avant d'insérer
        buf.cursor_position = len(buf.text)
        buf.insert_text(text)

        # Rolling log UI : on ne garde que max_log_chars dans la zone de log
        extra = len(buf.text) - self.max_log_chars
        if extra > 0:
            # On coupe le début (les lignes les plus anciennes)
            buf.text = buf.text[extra:]
            buf.cursor_position = len(buf.text)



    def _stdout_worker(self) -> None:
        """
        Thread backend qui lit host.stdout_queue et pousse les logs
        vers la queue interne _ui_log_queue, sans toucher au TUI.
        """

        q = self.host.stdout_queue
        while not self._stop_event.is_set():
            try:
                pid, text = q.get(timeout=0.1)
            except queue.Empty:
                continue

            # --- NEW: ignorer les VMs muettes ---
            if pid in self.muted_pids:
                # On consomme quand même l'item pour vider la queue,
                # mais on ne le stocke ni ne l'affiche.
                continue

            # Mémoriser le log brut par pid (utile pour recharger une VM)
            chunks = self._logs.setdefault(pid, [])
            chunks.append(text)

            # Rolling log: on ne garde que les max_log_chars derniers caractères
            joined = "".join(chunks)
            if len(joined) > self.max_log_chars:
                joined = joined[-self.max_log_chars:]
                # On remet sous forme d'une seule chunk pour éviter d'accumuler des petites chaînes
                self._logs[pid] = [joined]


            # On laisse le TUI décider quoi afficher
            self._ui_log_queue.put((pid, text))
            
    async def pump_logs(self) -> None:
        """
        Tâche asynchrone exécutée dans l'event loop prompt_toolkit.
        Elle lit _ui_log_queue (alimentée par le thread backend)
        et met à jour la zone de log.
        """


        app = get_app()

        while True:
            # On essaie de vider la queue sans bloquer trop longtemps
            updated = False
            try:
                while True:
                    pid, text = self._ui_log_queue.get_nowait()
                    # On n'affiche que les logs de la VM courante
                    if pid == self.current_pid:
                        self._append_log(text)
                        updated = True
            except queue.Empty:
                pass

            if updated:
                try:
                    app.invalidate()
                except Exception:
                    pass

            # Petite pause pour laisser respirer l'event loop
            await asyncio.sleep(0.05)


                
    def _get_history(self) -> list[str]:
        return self._history

    def _add_to_history(self, line: str) -> None:
        """Ajoute la ligne à l'historique persistant (liste + fichier).
        La gestion de l'historique interactif (↑/↓) est faite par
        prompt_toolkit via Buffer.reset(append_to_history=True).
        """
        line = line.rstrip("\n")
        if not line.strip():
            return

        # éviter les doublons immédiats (optionnel)
        if self._history and self._history[-1] == line:
            return

        # Historique en mémoire (pour complétion éventuelle, etc.)
        self._history.append(line)

        # persistance disque simple (une ligne par commande)
        try:
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            # On ignore les erreurs de fichier, pour ne pas casser le REPL.
            pass


    # --- Entrée utilisateur ---

    def handle_enter(self) -> None:
        """Appelée quand l'utilisateur appuie sur Entrée en console."""
        line = self.input_buffer.text

        # Ligne vide : on reset juste le buffer pour rester cohérent
        # avec le comportement standard de prompt_toolkit.
        if not line.strip():
            self.input_buffer.reset()
            return

        # Historique persistant (liste + fichier)
        self._add_to_history(line)

        # Écho de la commande dans le log de la VM courante
        pid_for_log = self.current_pid
        prefix = f"[pid={pid_for_log}] "
        echoed = prefix + line + "\n"
        self._logs.setdefault(pid_for_log, []).append(echoed)
        self._append_log(echoed)

        # Routage réel vers host / VM
        if line.strip().startswith(":host"):
            self._handle_host_command(line)
        else:
            try:
                self.host.send_line(self.current_pid, line)
            except KeyError:
                self._append_log(
                    f"[host] cannot send to dead VM {self.current_pid}\n"
                )

        # Maintenant, on laisse prompt_toolkit gérer son propre historique
        # interactif (↑/↓) et l'état interne du buffer.
        # append_to_history est appelé à travers reset(append_to_history=True),
        # ce qui met à jour `history` et reconstruit `_working_lines` proprement.
        self.input_buffer.reset(append_to_history=True)


    # --- Commandes :host ... ---

    def _handle_host_command(self, line: str) -> None:
        import shlex

        rest = line.strip()[len(":host") :].strip()
        if not rest:
            self._print_help()
            return

        try:
            parts = shlex.split(rest)
        except ValueError as e:
            self._append_log(f"[host] parse error: {e}\n")
            return

        if not parts:
            self._print_help()
            return

        cmd = parts[0]
        args = parts[1:]


        # :host vm PID
        if cmd == "vm":
            if not args:
                self._append_log(f"[host] current VM pid={self.current_pid}\n")
                return
            try:
                pid = int(args[0])
            except ValueError:
                self._append_log("[host] usage: :host vm PID\n")
                return

            try:
                vms = self.host.list_vms()
            except Exception as e:
                self._append_log(f"[host] error: {e}\n")
                return

            existing_pids = {info["pid"] for info in vms}
            if pid not in existing_pids:
                self._append_log(f"[host] no such VM pid={pid}\n")
                return

            self.current_pid = pid
            self._append_log(f"[host] switched to pid={pid}\n")
            return


        # :host new [name]
        if cmd == "new":
            name = args[0] if args else "vm"
            pid = self.host.new_vm(name, entry="INTERPRET-AGAIN")
            self.current_pid = pid
            self._append_log(f"[host] NEW pid={pid} name={name!r}\n")
            return

        # :host vm PID
        if cmd == "vm":
            if not args:
                self._append_log(f"[host] current VM pid={self.current_pid}\n")
                return
            try:
                pid = int(args[0])
            except ValueError:
                self._append_log("[host] usage: :host vm PID\n")
                return
            if pid not in self.host.vms:
                self._append_log(f"[host] no such VM pid={pid}\n")
                return
            self.current_pid = pid
            self._append_log(f"[host] switched to pid={pid}\n")
            return

        # :host ps
        if cmd == "ps":
            vms = self.host.list_vms()
            self._append_log("PID   ALIVE  NAME\n")
            self._append_log("-------------------\n")
            for info in vms:
                alive = "Y" if info.get("alive") else "N"
                self._append_log(
                    f"{info['pid']:3d}   {alive}    {info.get('name','')}\n"
                )
            if not vms:
                self._append_log("(no VM)\n")
            return

        # :host kill PID
        if cmd == "kill":
            if not args:
                self._append_log("[host] usage: :host kill PID\n")
                return
            try:
                pid = int(args[0])
            except ValueError:
                self._append_log("[host] usage: :host kill PID\n")
                return
            # --- NEW: marquer le pid comme muet AVANT le kill ---
            self.muted_pids.add(pid)
            
            self.host.kill_vm(pid)
            if pid == self.current_pid:
                remaining = self.host.list_vms()
                if remaining:
                    self.current_pid = remaining[0]["pid"]
                    self._append_log(
                        f"[host] killed {pid}, switched to {self.current_pid}\n"
                    )
                else:
                    self._append_log(f"[host] killed {pid}, no VMs left\n")
            else:
                self._append_log(f"[host] killed {pid}\n")
            return
            
        if cmd == "gc-dead":
            n = self.host.gc_dead_vms()
            # enlever les PIDs qui n'existent plus
            alive_pids = {info["pid"] for info in self.host.list_vms()}
            self.muted_pids.intersection_update(alive_pids)
            self._append_log(f"[host] gc-dead: removed {n} dead VM(s)\n")
            return

        # :host logs [PID]
        if cmd == "logs":
            if args:
                try:
                    pid = int(args[0])
                except ValueError:
                    self._append_log("[host] usage: :host logs [PID]\n")
                    return
            else:
                pid = self.current_pid
            chunks = self._logs.get(pid, [])
            if not chunks:
                self._append_log(f"[host] no logs for pid={pid}\n")
                return
            self._append_log(f"[host] logs for pid={pid}:\n")
            for c in chunks:
                self._append_log(c)
            if chunks and not chunks[-1].endswith("\n"):
                self._append_log("\n")
            return

        # :host read-from "file"
        if cmd == "read-from":
            if not args:
                self._append_log("[host] usage: :host read-from \"file\"\n")
                return
            filename = args[0]
            if (filename.startswith('"') and filename.endswith('"')) or (
                filename.startswith("'") and filename.endswith("'")
            ):
                filename = filename[1:-1]
            if not os.path.exists(filename):
                self._append_log(f"[host] file not found: {filename!r}\n")
                return
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    for raw in f:
                        ln = raw.rstrip("\n")
                        if not ln.strip():
                            continue
                        self._append_log(f"[host] exec: {ln}\n")
                        if ln.strip().startswith(":host"):
                            self._handle_host_command(ln)
                        else:
                            try:
                                self.host.send_line(self.current_pid, ln)
                            except KeyError:
                                self._append_log(
                                    f"[host] cannot send to dead VM {self.current_pid}\n"
                                )
            except OSError as e:
                self._append_log(f"[host] read-from error: {e}\n")
            return
        # :host reset
        if cmd == "reset":
            # 1) reset logique du host (tue les VMs, remet _next_pid et vide stdout_queue)
            self.host.reset()

            # 2) reset des logs côté TUI
            self._logs.clear()
            from prompt_toolkit.document import Document
            self.log_area.buffer.document = Document(
                "ForthOS console TUI (F2)\n[host] reset\n\n"
            )

            # 3) recréer la VM de base comme au démarrage
            pid = self.host.new_vm("main", entry="INTERPRET-AGAIN")
            self.current_pid = pid
            self._append_log(f"[host] NEW pid={pid} name='main'\n")
            return

        # :host quit
        if cmd == "quit":
            from prompt_toolkit.application.current import get_app
            self._append_log("bye.\n")
            try:
                self._stop_event.set()
            except Exception:
                pass
            get_app().exit()
            return

        # help / inconnu
        if cmd in ("help", "?"):
            self._print_help()
            return
            
        # dot-commands (exécutés côté host via RPC)
        if cmd.startswith("."):
            try:
                out = self.host.dot_command(self.current_pid, rest)
            except Exception as e:
                self._append_log(f"[host] dot-command error: {e}\n")
            else:
                if out:
                    self._append_log(out)
            return


        self._append_log(f"[host] unknown host command: {cmd!r}\n")
        self._print_help()

    def _print_help(self) -> None:
        self._append_log("Host commands (prefix :host):\n")
        self._append_log("  :host new [name]        - create VM running INTERPRET-AGAIN\n")
        self._append_log("  :host vm PID            - switch current VM\n")
        self._append_log("  :host ps                - list VMs\n")
        self._append_log("  :host kill PID          - kill VM\n")
        self._append_log("  :host gc-dead          - remove dead VMs from host tables\n")
        self._append_log("  :host logs [PID]        - show logs for PID (or current)\n")
        self._append_log("  :host read-from \"file\"  - execute host+Forth commands from file\n")
        self._append_log("  :host .stack/.gc/...    - run VM dot-command on current VM\n")
        self._append_log("  :host quit              - exit TUI\n")


# ======================================================================
# Application principale
# ======================================================================

def main():
    doc_screen = DocScreen("README.md")
    console_screen = ConsoleScreen()
    editor_screen = EditorScreen()

    manager = ScreenManager([console_screen, doc_screen, editor_screen])

    root_container = DynamicContainer(lambda: manager.container)
    layout = Layout(root_container)

    kb = KeyBindings()

    @kb.add("f1")
    def _(event):
        manager.show("doc")

    @kb.add("f2")
    def _(event):
        manager.show("console")

    @kb.add("f3")
    def _(event):
        # Si on est déjà dans l'éditeur, F3 = Find next
        if manager.current is editor_screen:
            editor_screen.find_next()
        else:
            manager.show("editor")

    @kb.add("c-c")
    @kb.add("c-q")
    def _(event):
        event.app.exit()

    @kb.add("enter")
    def _(event):
        app = event.app
        # En console avec focus sur la ligne d'input -> traiter comme commande
        if manager.current is console_screen and app.layout.current_window is console_screen.input_window:
            console_screen.handle_enter()
        elif manager.current is editor_screen and app.layout.current_window is editor_screen.command_window:
            # validation de la ligne de commande de l'éditeur
            editor_screen.handle_command_enter()
        else:
            # ailleurs (doc, éditeur...) -> comportement texte simple : nouvelle ligne
            event.current_buffer.insert_text("\n")

    @kb.add("escape")
    def _(event):
        app = event.app
        if manager.current is editor_screen and app.layout.current_window is editor_screen.command_window:
            editor_screen.cancel_command()

    # --- raccourcis de l'éditeur (F3) ---

    @kb.add("c-n")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.new_file()

    @kb.add("c-o")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.open_file()

    @kb.add("c-s")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.save_file()

    # Save As séparé (par ex. F6)
    @kb.add("f6")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.save_file_as()

    @kb.add("c-w")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.close_current_tab()

    # ---- Navigation entre onglets (éditeur) ---
    @kb.add("f7")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.previous_tab()

    @kb.add("f8")
    def _(event):
        if manager.current is editor_screen:
            editor_screen.next_tab()
            
        # --- Copier / coller avec Alt-C / Alt-V ---

    @kb.add("escape", "c")  # Alt-C
    def _(event):
        """
        Copier la sélection courante dans le presse-papier partagé
        (et système si disponible).
        """
        buf = event.app.current_buffer
        data = buf.copy_selection()
        if data is not None:
            event.app.clipboard.set_data(data)

    @kb.add("escape", "v")  # Alt-V
    def _(event):
        """
        Coller depuis le presse-papier partagé (et système si on l'utilise).
        """
        buf = event.app.current_buffer
        data = event.app.clipboard.get_data()
        if data is not None:
            buf.paste_clipboard_data(data)

    @kb.add("escape", "x")  # Alt-X
    def _(event):
        """
        Couper : coupe la sélection (Buffer.cut_selection)
        et la place aussi dans le presse-papier de l'application.
        """
        buf = event.app.current_buffer
        data = buf.cut_selection()  # supprime + renvoie la sélection
        if data is not None:
            event.app.clipboard.set_data(data)





    # Choix du presse-papier : système si possible, sinon en mémoire
    if PyperclipClipboard is not None:
        clipboard = PyperclipClipboard()
    else:
        clipboard = InMemoryClipboard()
 
    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        clipboard=clipboard,
    )

    # Focus initial sur la console
    layout.focus(console_screen.input_window)
    

    #  Enregistrer un callback qui sera appelé une fois l'event loop démarrée
    def start_log_pump() -> None:
        # Ici, l'event loop tourne déjà, on peut créer une tâche asynchrone
        app.create_background_task(console_screen.pump_logs())

    app.pre_run_callables.append(start_log_pump)
    
    app.run()


if __name__ == "__main__":
    main()

