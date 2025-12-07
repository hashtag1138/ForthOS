#!/usr/bin/env python3
# forth_host_repl.py
#
# REPL host pour ForthOS :
# - utilise ForthOSHostCore (gestion des VMs, messages, stdout_queue)
# - toutes les commandes host commencent par:  :host ...
# - les lignes sans préfixe sont envoyées telles quelles à la VM courante
#
# Commandes host prévues:
#   :host new [name]        -> crée une VM entry=INTERPRET-AGAIN, devient courante
#   :host vm PID            -> change de VM courante
#   :host ps                -> liste des VMs (pid, name, alive)
#   :host kill PID          -> tue une VM
#   :host logs [PID]        -> affiche les logs de PID (ou de la VM courante)
#   :host quit              -> quitte le REPL
#
#   :host .stack / :host .gc / :host .objects / etc.
#       -> exécute une dot-command sur la VM courante, en appelant
#          le module de dot-commands (ou, par défaut, vm.handle_dot_command).
#
# Particularité : REPL multi-threade
#   - thread principal : lecture stdin + commandes host (PromptSession)
#   - thread de fond   : vidage stdout_queue + affichage + logs
#
# Tests intégrés :
#   python forth_host_repl.py --test

from __future__ import annotations

import io
import sys
import threading
import queue
import shlex
from typing import Dict, List

from forthos_host_core import ForthOSHostCore
from forthos_runtime import ForthOSVM

# --- module de dot-commands ---
# Si un module séparé existe (par ex. forthos_dot_commands.py), on le préfère.
# Sinon, on se rabat sur l’implémentation embarquée dans forthos_vm_core.VM.
try:
    # module optionnel, à toi de le créer si tu veux vraiment externaliser les dot-cmds
    from forthos_dot_commands import DOT_CMDS, run_dot_command  # type: ignore
except ImportError:
    # fallback : on utilise DOT_CMDS + handle_dot_command définis dans forthos_vm_core
    from forthos_vm_core import DOT_CMDS  # type: ignore

    def run_dot_command(vm: ForthOSVM, line: str, out: io.StringIO) -> None:
        """
        Adapter minimal : délègue à vm.handle_dot_command(line, out)
        pour garder le comportement existant des dot-commands.
        """
        if not hasattr(vm, "handle_dot_command"):
            out.write("dot-commands not supported by this VM\n")
            return
        vm.handle_dot_command(line, out)


class HostREPL:
    """
    REPL texte multi-thread par-dessus ForthOSHostCore.

    - gère les VMs via :host new / vm / ps / kill / logs / quit
    - envoie les lignes Forth à la VM courante via host.send_line
    - intercepte :host .stack, :host .gc, etc. et appelle run_dot_command(...)
    - thread de fond pour drainer stdout_queue en continu.
    """

    def __init__(self) -> None:
        self.host = ForthOSHostCore()
        # VM principale : boucle INTERPRET-AGAIN côté Forth
        self.current_pid: int = self.host.new_vm("main", entry="INTERPRET-AGAIN")

        # logs bruts par pid (tout ce qui sort via host.handle_stdout)
        self._logs: Dict[int, List[str]] = {}

        # gestion du thread de vidage de stdout
        self._stop_event: threading.Event | None = None
        self._stdout_thread: threading.Thread | None = None

        # verrou pour sérialiser les prints et limiter le chaos
        self._print_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Utilitaires internes
    # ------------------------------------------------------------------

    def _get_vm(self, pid: int) -> ForthOSVM | None:
        return self.host.vms.get(pid)

    def _drain_stdout(self, *, block: bool = False, timeout: float = 0.1) -> None:
        """
        Vide stdout_queue du host, affiche sur le terminal et stocke dans les logs.

        - block=False : utilise get_nowait() jusqu'à queue.Empty (pour les tests, ou flush ponctuel)
        - block=True  : commence par un get(timeout=...), puis enchaîne en mode non bloquant
        """
        q = self.host.stdout_queue

        def _get_one(first_block: bool) -> tuple[int, str] | None:
            try:
                if first_block:
                    return q.get(timeout=timeout)
                else:
                    return q.get_nowait()
            except queue.Empty:
                return None

        # premier élément (optionnellement bloquant)
        first = _get_one(block)
        if first is None:
            return

        pid, text = first
        self._handle_stdout_item(pid, text)

        # puis enchaîne en non bloquant
        while True:
            item = _get_one(False)
            if item is None:
                break
            pid, text = item
            self._handle_stdout_item(pid, text)

    def _handle_stdout_item(self, pid: int, text: str) -> None:
        # stocke dans les logs *dans tous les cas*
        self._logs.setdefault(pid, []).append(text)

        # n'affiche à l'écran QUE la VM focus
        if pid != self.current_pid:
            # sortie silencieuse (mais loguée) pour les VMs non focus
            return

        with self._print_lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def _stdout_worker(self) -> None:
        """
        Thread de fond : drainer stdout_queue en continu,
        tant que _stop_event n'est pas déclenché.
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            # on bloque un peu pour ne pas tourner à vide
            self._drain_stdout(block=True, timeout=0.1)

    def _print_ps(self) -> None:
        vms = self.host.list_vms()
        if not vms:
            print("(no VMs)")
            return
        print(" PID   ALIVE  NAME")
        print(" ----  -----  ----------------")
        for info in vms:
            pid = info["pid"]
            alive = "yes" if info["alive"] else "no"
            name = info.get("name", f"vm{pid}")
            cur_mark = "*" if pid == self.current_pid else " "
            print(f"{cur_mark}{pid:4d}  {alive:5s}  {name}")

    def _print_logs(self, pid: int) -> None:
        buf = self._logs.get(pid)
        if not buf:
            print(f"(no logs for pid {pid})")
            return
        print(f"--- logs for pid {pid} ---")
        sys.stdout.write("".join(buf))
        if not buf[-1].endswith("\n"):
            sys.stdout.write("\n")
        print(f"--- end logs for pid {pid} ---")
        
    def _send_line_to_current_vm(self, line: str) -> None:
        """
        Envoie une ligne Forth à la VM courante, avec vérification de l'état.
        Préviens si la VM est inexistante ou morte.
        """
        vm = self._get_vm(self.current_pid)
        if vm is None:
            print("no current VM; use :host new or :host vm.")
            return
        if hasattr(vm, "alive") and not vm.alive:
            print(f"VM {self.current_pid} is dead; use :host new or :host vm to select a live VM.")
            return
        try:
            self.host.send_line(self.current_pid, line)
        except KeyError as e:
            print(f"send_line error: {e}")


    # ------------------------------------------------------------------
    # Commandes :host ...
    # ------------------------------------------------------------------

    def _handle_host_command(self, line: str) -> bool:
        """
        Traite une ligne commençant par ':host '.
        Retourne True si une commande host a été reconnue/traitée.
        Peut lever SystemExit pour :host quit.
        """
        rest = line.strip()[len(":host") :].strip()
        if not rest:
            self._print_help()
            return True

        # Utilise shlex pour respecter les guillemets, ex:
        # :host read-from "mon script.fos"
        try:
            parts = shlex.split(rest)
        except ValueError as e:
            print(f"parse error in :host command: {e}")
            return True

        if not parts:
            self._print_help()
            return True

        cmd = parts[0]
        args = parts[1:]

        # 1) dot-commands : :host .stack, :host .gc, ...
        if cmd in DOT_CMDS or cmd.startswith("."):
            vm = self._get_vm(self.current_pid)
            if vm is None:
                print("no current VM")
                return True
            if hasattr(vm, "alive") and not vm.alive:
                print(f"VM {self.current_pid} is dead; cannot run dot-command.")
                return True

            out = io.StringIO()
            run_dot_command(vm, rest, out)
            sys.stdout.write(out.getvalue())
            sys.stdout.flush()
            return True

                # 1) lecture de script: :host read-from "filename"
        if cmd == "read-from":
            if not args:
                print('usage: :host read-from "filename"')
                return True

            filename = args[0]
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    for raw in f:
                        ln = raw.rstrip("\n")

                        # ignore lignes vides et commentaires shell-style
                        if not ln.strip():
                            continue
                        if ln.lstrip().startswith("#"):
                            continue

                        if ln.strip().startswith(":host"):
                            # on re-route la ligne complète vers le parser host
                            self._handle_host_command(ln)
                        else:
                            # ligne Forth normale -> VM courante
                            self._send_line_to_current_vm(ln)
            except OSError as e:
                print(f"read-from: cannot open {filename!r}: {e}")
            return True

        
        # 2) commandes host "classiques"
        if cmd == "new":
            name = args[0] if args else "vm"
            pid = self.host.new_vm(name, entry="INTERPRET-AGAIN")
            self.current_pid = pid
            print(f"NEW pid={pid} name={name!r}")
            return True

        if cmd == "vm":
            if not args:
                print(f"current VM pid={self.current_pid}")
                return True
            try:
                pid = int(args[0])
            except ValueError:
                print("usage: :host vm PID")
                return True
            if pid not in self.host.vms:
                print(f"no such VM pid={pid}")
                return True
            self.current_pid = pid
            print(f"switched to pid={pid}")
            return True

        if cmd == "ps":
            self._print_ps()
            return True

        if cmd == "kill":
            if not args:
                print("usage: :host kill PID")
                return True
            try:
                pid = int(args[0])
            except ValueError:
                print("usage: :host kill PID")
                return True
            self.host.kill_vm(pid)
            # si on a tué la VM courante, on essaie de choisir un autre pid
            if pid == self.current_pid:
                remaining = self.host.list_vms()
                if remaining:
                    self.current_pid = remaining[0]["pid"]
                    print(f"killed pid={pid}, switched to pid={self.current_pid}")
                else:
                    print(f"killed pid={pid}, no VMs left")
            else:
                print(f"killed pid={pid}")
            return True

        if cmd == "logs":
            if args:
                try:
                    pid = int(args[0])
                except ValueError:
                    print("usage: :host logs [PID]")
                    return True
            else:
                pid = self.current_pid
            self._print_logs(pid)
            return True

        if cmd in ("quit", "exit"):
            # demande au thread stdout de s'arrêter
            if self._stop_event is not None:
                self._stop_event.set()
            # kill toutes les VMs proprement
            for info in list(self.host.list_vms()):
                self.host.kill_vm(info["pid"])
            print("bye.")
            raise SystemExit(0)

        if cmd in ("help", "?"):
            self._print_help()
            return True

        print(f"unknown host command: {cmd!r}")
        self._print_help()
        return True

    def _print_help(self) -> None:
        print("Host commands (prefix with :host):")
        print("  :host new [name]           - create VM running INTERPRET-AGAIN")
        print("  :host vm PID               - switch current VM")
        print("  :host ps                   - list VMs")
        print("  :host kill PID             - kill VM")
        print("  :host logs [PID]           - show logs for PID (or current)")
        print("  :host read-from \"file\"     - execute host+Forth commands from file")
        print("  :host .stack/.gc/...       - run VM dot-command on current VM")
        print("  :host quit                 - exit REPL")

    # ------------------------------------------------------------------
    # Boucle principale (PromptSession + patch_stdout)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Boucle principale (PromptSession + patch_stdout)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Boucle REPL interactive basée sur prompt_toolkit, avec complétion :
        - commandes :host ...
        - dot-commands (.stack, .gc, ...)
        - fichiers pour :host read-from
        - mots Forth du dictionnaire de la VM courante.
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.patch_stdout import patch_stdout
            from prompt_toolkit.completion import Completer, Completion, PathCompleter
        except ImportError:  # pragma: no cover
            print("prompt_toolkit n'est pas installé. Fais : pip install prompt_toolkit")
            sys.exit(1)

        print("ForthOS Host REPL")
        print("Type Forth code to send it to the current VM.")
        print("Use :host ... for host / dot-commands.  (:host help for help)")

        # démarrage du thread de fond pour drainer stdout
        self._stop_event = threading.Event()
        self._stdout_thread = threading.Thread(
            target=self._stdout_worker, daemon=True
        )
        self._stdout_thread.start()

        # -------- Completer --------
        outer = self
        host_cmds = [
            "new",
            "vm",
            "ps",
            "kill",
            "logs",
            "read-from",
            "quit",
            "help",
        ]
        dot_cmds = sorted(DOT_CMDS)
        path_completer = PathCompleter(expanduser=True)

        class ForthOSCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                stripped = text.lstrip()

                # --- Complétion des commandes :host ---
                if stripped.startswith(":host"):
                    after = stripped[len(":host"):].lstrip()
                    # Exemple de lignes:
                    #   ":host "            -> after = ""
                    #   ":host ne"          -> after = "ne"
                    #   ":host read-from f" -> after = "read-from f"

                    # Pas encore de commande tapée -> proposer la liste
                    if not after:
                        for name in host_cmds:
                            yield Completion(name, start_position=0)
                        for dc in dot_cmds:
                            yield Completion(dc, start_position=0)
                        return

                    parts = after.split()
                    cmd_fragment = parts[0]

                    # On complète le *nom* de commande (:host ne<Tab> -> new)
                    if len(parts) == 1:
                        start_pos = -len(cmd_fragment)
                        for name in host_cmds:
                            if name.startswith(cmd_fragment):
                                yield Completion(name, start_position=start_pos)
                        for dc in dot_cmds:
                            if dc.startswith(cmd_fragment):
                                yield Completion(dc, start_position=start_pos)
                        return

                    # Ici on a au moins "cmd arg..."
                    cmd = parts[0]

                    # Cas particulier : :host read-from <filename>
                    if cmd == "read-from":
                        # On délègue au PathCompleter qui complète le dernier "mot"
                        for c in path_completer.get_completions(document, complete_event):
                            yield c
                        return

                    # Pour les autres commandes host, pas encore de complétion d'arguments
                    return

                # --- Complétion des mots Forth (ligne envoyée à la VM) ---
                vm = outer._get_vm(outer.current_pid)
                if vm is None:
                    return

                word_before = document.get_word_before_cursor(WORD=True)
                if not word_before:
                    return

                prefix = word_before
                start_pos = -len(prefix)

                try:
                    all_words = vm.dict.all_words()
                except Exception:
                    return

                # On complète de manière case-insensitive sur les noms
                seen = set()
                for w in all_words:
                    name = getattr(w, "name", None)
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    if name.upper().startswith(prefix.upper()):
                        yield Completion(name, start_position=start_pos)

        session = PromptSession(completer=ForthOSCompleter())

        # -------- Boucle REPL --------
        try:
            # patch_stdout intercepte tous les prints / writes (y compris du thread stdout)
            # et redessine proprement la ligne de prompt.
            with patch_stdout():
                while True:
                    try:
                        prompt = f"[pid={self.current_pid}] forthos> "
                        line = session.prompt(prompt)
                    except EOFError:
                        print("\nEOF -> quitting.")
                        if self._stop_event is not None:
                            self._stop_event.set()
                        break
                    except KeyboardInterrupt:
                        print("\nKeyboardInterrupt (Ctrl-C). Use ':host quit' to exit.")
                        continue

                    if not line.strip():
                        continue

                    if line.strip().startswith(":host"):
                        try:
                            self._handle_host_command(line)
                        except SystemExit:
                            return
                        continue

                    # Ligne Forth normale -> envoyée à la VM courante (avec check vm morte)
                    self._send_line_to_current_vm(line)

        finally:
            if self._stop_event is not None:
                self._stop_event.set()


def main() -> None:
    repl = HostREPL()
    repl.run()


# ======================================================================
# Tests intégrés (python forth_host_repl.py --test)
# ======================================================================

import unittest
import time
from contextlib import redirect_stdout


class TestHostREPL_Basics(unittest.TestCase):
    def setUp(self):
        # On ne lance pas repl.run(), on utilise uniquement l’API interne
        self.repl = HostREPL()

    def test_host_new_creates_vm_and_sets_current(self):
        initial_pid = self.repl.current_pid
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host new worker")
        out = buf.getvalue()

        # On a bien loggé la création
        self.assertIn("NEW pid=", out)

        # Le pid courant a changé et la VM existe dans le host
        self.assertNotEqual(self.repl.current_pid, initial_pid)
        self.assertIn(self.repl.current_pid, self.repl.host.vms)

    def test_host_vm_switch(self):
        # VM principale déjà créée par HostREPL.__init__
        pid_main = self.repl.current_pid

        # On crée une deuxième VM
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host new worker")
        pid_worker = self.repl.current_pid
        self.assertNotEqual(pid_main, pid_worker)

        # On revient sur la VM principale
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(f":host vm {pid_main}")
        self.assertEqual(self.repl.current_pid, pid_main)

    def test_host_ps_lists_vms(self):
        # On crée au moins une VM de plus
        self.repl._handle_host_command(":host new worker")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host ps")
        out = buf.getvalue()

        # Vérifie qu'on voit bien au moins un PID et un header
        self.assertIn("PID", out)
        self.assertIn("ALIVE", out)
        self.assertIn("NAME", out)


class TestHostREPL_LogsAndDotCommands(unittest.TestCase):
    def setUp(self):
        self.repl = HostREPL()

    def test_host_logs_collects_output_from_stdout_queue(self):
        pid = self.repl.current_pid

        # On simule une sortie générée par la VM
        self.repl.host.handle_stdout(pid, "hello\n")

        buf = io.StringIO()
        with redirect_stdout(buf):
            # _drain_stdout lit stdout_queue et alimente _logs
            self.repl._drain_stdout()
        out = buf.getvalue()

        # La sortie est apparue à l'écran...
        self.assertIn("hello", out)
        # ...et est stockée dans les logs de la VM
        self.assertIn(pid, self.repl._logs)
        self.assertIn("hello\n", "".join(self.repl._logs[pid]))

        # :host logs doit afficher ces logs
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host logs")
        logs_out = buf.getvalue()
        self.assertIn("logs for pid", logs_out)
        self.assertIn("hello", logs_out)

    def test_host_dot_stack_on_current_vm(self):
        pid = self.repl.current_pid

        # On envoie un bout de Forth simple pour remplir la pile
        # (la VM tourne INTERPRET-AGAIN et lit les messages "stdin")
        self.repl.host.send_line(pid, "1 2 3")

        # Laisse un tout petit peu de temps au thread VM pour traiter le message
        time.sleep(0.05)

        # On appelle la dot-command via le REPL: :host .stack
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host .stack")
        out = buf.getvalue()

        # .stack doit montrer la pile ; on reste permissif sur le format exact
        self.assertIn("1", out)
        self.assertIn("2", out)
        self.assertIn("3", out)

    def test_host_quit_raises_systemexit(self):
        # :host quit déclenche un SystemExit pour sortir de la boucle run()
        with self.assertRaises(SystemExit):
            self.repl._handle_host_command(":host quit")
    
    def test_warn_when_sending_to_dead_vm(self):
        pid = self.repl.current_pid
        vm = self.repl._get_vm(pid)
        # On simule une VM morte
        setattr(vm, "alive", False)

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._send_line_to_current_vm("1 2 3")
        out = buf.getvalue()

        self.assertIn("dead", out.lower())

    def test_warn_when_dotcmd_on_dead_vm(self):
        pid = self.repl.current_pid
        vm = self.repl._get_vm(pid)
        setattr(vm, "alive", False)

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.repl._handle_host_command(":host .stack")
        out = buf.getvalue()

        self.assertIn("dead", out.lower())

    
if __name__ == "__main__":
    if "--test" in sys.argv:
        # Nettoie sys.argv pour unittest
        sys.argv = [sys.argv[0]]
        unittest.main()
    else:
        main()

