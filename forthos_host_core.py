#!/usr/bin/env python3
# forthos_host_core.py
#
# Noyau host logique pour ForthOS :
# - gestion des VMs ForthOSVM (création, destruction, spawn)
# - routage des messages SEND / WAIT-MSG
# - collecte de la sortie texte des VMs via stdout_queue
#
# Pas de REPL, pas de rendu terminal ici : cette couche est faite
# pour être testée unitairement et réutilisée par différentes UIs.

from __future__ import annotations

import queue
import threading
import time
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

from forthos_vm_core import ObjRef, ObjKind
from forthos_runtime import ForthOSVM, HasStdoutHandler


class ForthOSHostCore(HasStdoutHandler):
    """
    Noyau du host ForthOS, sans REPL ni I/O terminal.

    Responsabilités :
    - maintenir la liste des VMs (pid -> ForthOSVM)
    - fournir new_vm / kill_vm / spawn_from
    - router les messages SEND (deliver_msg)
    - recevoir la sortie texte des VMs (handle_stdout) et la pousser
      dans stdout_queue pour traitement par une couche supérieure.
    """

    def __init__(self) -> None:
        # VMs gérées par le host : pid -> ForthOSVM
        self.vms: Dict[int, ForthOSVM] = {}
        self._next_pid: int = 1

        # Queue pour tout ce qui vient de la sortie des VMs
        # Chaque item est un tuple (pid, text)
        self.stdout_queue: queue.Queue[Tuple[int, str]] = queue.Queue()

        # Optionnel : meta info sur les VMs (nom, etc.)
        self.meta: Dict[int, Dict[str, Any]] = {}

        # verrou si on veut manipuler vms/meta depuis plusieurs threads
        self._lock = threading.Lock()

    # ------------------------------------------------------
    # Interface attendue par ForthOSVM
    # ------------------------------------------------------

    def handle_stdout(self, pid: int, text: str) -> None:
        """
        Appelé par ForthOSVM.emit().
        On ne fait aucune I/O terminal ici, on pousse juste dans stdout_queue.
        """
        self.stdout_queue.put((pid, text))

    def deliver_msg(self, pid: int, msg: Dict[str, Any]) -> None:
        """
        Routage d'un message vers la VM ciblée.
        Utilisé par la primitive SEND (msg pid --).
        """
        with self._lock:
            vm = self.vms.get(pid)
        if vm is None:
            # Choix : ignorer, ou lever, ou logguer.
            # Ici on lève pour que ça se voie pendant les tests.
            raise KeyError(f"deliver_msg: pid {pid} inconnu")
        vm.inbox.put(msg)

    def spawn_from(self, parent_vm: ForthOSVM, qref: ObjRef) -> int:
        """
        SPAWN ( qref -- pid ) côté host.

        Modèle A : deep-copy de l'environnement Forth du parent
        (dictionnaire + ObjectSpace + data space), avec piles D/R fraîches
        dans la VM fille.

        La QUOT `qref` est utilisée comme point d'entrée : la VM enfant démarre
        dans un thread dédié qui exécute cette QUOT, puis s'arrête lorsqu'elle
        se termine (ou en cas d'exception).
        """
        # allocation d'un nouveau pid et création de la VM fille
        with self._lock:
            pid = self._next_pid
            self._next_pid += 1
        child = ForthOSVM(self, pid)

        # snapshot de l'environnement Forth du parent (modèle A)
        env = parent_vm._env_to_dict()
        child._env_from_dict(env)
        # réattacher les primitives noyau (prim = fonctions Python)
        child._install_forthos_words()
        child._install_core()

        # enregistrement de la VM fille dans le host
        with self._lock:
            self.vms[pid] = child
            self.meta.setdefault(pid, {})["name"] = f"spawned_from_{parent_vm.pid}"

        # démarrage de la VM fille sur la QUOT d'entrée
        child.start_with_entry_quot(qref)
        return pid


    # ------------------------------------------------------
    # Gestion des VMs (API host)
    # ------------------------------------------------------

    def new_vm(self, name: str = "", entry: Optional[str] = None) -> int:
        """
        Crée une nouvelle VM ForthOS et la démarre.

        name : purement informatif pour l'instant (stocké dans meta).
        entry : nom d'un mot Forth à exécuter comme point d'entrée (optionnel).
        """
        with self._lock:
            pid = self._next_pid
            self._next_pid += 1
        vm = ForthOSVM(self, pid)
        with self._lock:
            self.vms[pid] = vm
            self.meta.setdefault(pid, {})["name"] = name or f"vm{pid}"
        entry_to_run = entry
        vm.start(entry=entry_to_run)
        return pid

    def kill_vm(self, pid: int) -> None:
        """
        Arrête une VM (VM.stop()) et la retire de la table vms.
        """
        with self._lock:
            vm = self.vms.get(pid)
        if vm is None:
            return
        #on marque la vm comme morte
        vm.stop()

        with self._lock:
            self.vms.pop(pid, None)
            self.meta.pop(pid, None)
            
    def gc_dead_vms(self) -> None:
        with self._lock:
            dead = [pid for pid, vm in self.vms.items()
                    if not vm.alive or (vm.thread and not vm.thread.is_alive())]
            for pid in dead:
                self.vms.pop(pid, None)
                self.meta.pop(pid, None)


    def send_line(self, pid: int, line: str) -> None:
        """
        Envoie une ligne de texte "stdin" à une VM précise.

        Le contrat côté ForthOSVM / programme Forth :
        - un mot Forth (typiquement INTERPRET) fera WAIT-MSG
        - et traitera les messages {"type": "stdin", "line": ...}
        """
        msg = {"type": "stdin", "line": line}
        self.deliver_msg(pid, msg)

    def list_vms(self) -> List[Dict[str, Any]]:
        """
        Retourne une vue lisible des VMs pour affichage ou tests.

        Chaque entrée contient :
          - pid
          - name
          - alive
        """
        with self._lock:
            result: List[Dict[str, Any]] = []
            for pid, vm in self.vms.items():
                info = {
                    "pid": pid,
                    "name": self.meta.get(pid, {}).get("name", f"vm{pid}"),
                    "alive": vm.alive,
                }
                result.append(info)
        # tri par pid pour un ordre stable
        result.sort(key=lambda d: d["pid"])
        return result
        
    def reset(self) -> None:
            """
            Réinitialise le host :
    
            - tue toutes les VMs existantes
            - remet le compteur de pid (_next_pid) à 1
            - vide stdout_queue
    
            Ne touche pas aux couches UI (TUI, éditeur, etc.).
            """
            # tuer toutes les VMs existantes
            with self._lock:
                pids = list(self.vms.keys())
            for pid in pids:
                self.kill_vm(pid)
    
            # vider les structures internes et repartir de zéro
            with self._lock:
                self.vms.clear()
                self.meta.clear()
                self._next_pid = 1
    
            # vider la stdout_queue pour ne pas mélanger ancien / nouveau
            try:
                while True:
                    self.stdout_queue.get_nowait()
            except queue.Empty:
                pass
####################################################################
# LES TESTS CI DESSOUS NE DOIVENT JAMAIS ETRE MODIFIÉ. ILS SONT LA POUR VALIDER L'IMPLÉMENTATION PLUS HAUT ET SERVENT DE RÉFÉRENCE.
# NE JAMAIS MODIFIER LES TEST.
# ============================================================
# Tests unitaires pour ForthOSHostCore
# ============================================================

class TestForthOSHostCore_NewVm(unittest.TestCase):
    def test_new_vm_creates_vm_and_starts_thread(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("main")
        self.assertEqual(pid, 1)
        vms = host.list_vms()
        self.assertEqual(len(vms), 1)
        self.assertEqual(vms[0]["pid"], 1)
        self.assertEqual(vms[0]["name"], "main")

        # vérifier que la VM existe dans host.vms
        vm = host.vms[pid]
        self.assertIsInstance(vm, ForthOSVM)
        # donner un très léger délai pour que le thread démarre (et potentiellement termine)
        time.sleep(0.05)
        # on vérifie simplement qu'un objet Thread a bien été créé
        self.assertIsNotNone(vm.thread)


class TestForthOSHostCore_Stdout(unittest.TestCase):
    def test_handle_stdout_pushes_to_stdout_queue(self) -> None:
        host = ForthOSHostCore()
        pid = 1  # pid arbitraire pour le test unitaire du host

        # on simule deux appels venant d'une VM : "42 " puis "\\n"
        host.handle_stdout(pid, "42 ")
        host.handle_stdout(pid, "\\n")

        # vider stdout_queue
        items: List[Tuple[int, str]] = []
        while True:
            try:
                items.append(host.stdout_queue.get_nowait())
            except queue.Empty:
                break

        # on s'attend à "42 " puis "\\n" de la part de pid
        self.assertEqual(items, [
            (pid, "42 "),
            (pid, "\\n"),
        ])


class TestForthOSHostCore_SendLine(unittest.TestCase):
    def test_send_line_enqueues_stdin_message(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("stdin_test")
        vm = host.vms[pid]

        # on vide l'inbox s'il y a quoi que ce soit
        try:
            while True:
                vm.inbox.get_nowait()
        except queue.Empty:
            pass

        host.send_line(pid, "1 2 + .")

        msg = vm.inbox.get(timeout=0.1)
        self.assertEqual(msg["type"], "stdin")
        self.assertEqual(msg["line"], "1 2 + .")


class TestForthOSHostCore_DeliverMsg(unittest.TestCase):
    def test_deliver_msg_puts_message_in_inbox(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("msg_test")
        vm = host.vms[pid]

        msg = {"type": "custom", "payload": 123}
        host.deliver_msg(pid, msg)

        got = vm.inbox.get(timeout=0.1)
        self.assertEqual(got, msg)

    def test_deliver_msg_unknown_pid_raises(self) -> None:
        host = ForthOSHostCore()
        with self.assertRaises(KeyError):
            host.deliver_msg(999, {"type": "x"})


class TestForthOSHostCore_Spawn(unittest.TestCase):
    def test_spawn_from_creates_new_vm(self) -> None:
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        # QUOT factice pour le test ; la sémantique profonde viendra plus tard
        qref = ObjRef(1, ObjKind.QUOT)
        pid_child = host.spawn_from(parent_vm, qref)

        self.assertNotEqual(pid_child, pid_parent)
        vms = host.list_vms()
        pids = {info["pid"] for info in vms}
        self.assertIn(pid_parent, pids)
        self.assertIn(pid_child, pids)

        child_vm = host.vms[pid_child]
        self.assertIsInstance(child_vm, ForthOSVM)
        # La longévité (alive) dépend du code Forth d'entrée (entry);
        # ici, on se contente de vérifier que la VM a bien été créée.
        # On n'impose pas que le thread soit encore vivant.

    def test_kill_vm_removes_vm_and_stops_thread(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("to_kill")
        vm = host.vms[pid]
        # On laisse un peu de temps au thread pour démarrer/terminer
        time.sleep(0.05)
        # Un objet Thread doit exister
        self.assertIsNotNone(vm.thread)

        host.kill_vm(pid)
        vms = host.list_vms()
        self.assertEqual(len(vms), 0)
        # La VM doit être marquée comme morte
        self.assertFalse(vm.alive)

class TestForthOSHostCore_InterpretLoop(unittest.TestCase):
    def test_interpret_evaluates_stdin_line_once(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("interpret", entry="INTERPRET-ONCE")   # démarre sur INTERPRET par défaut

        # On laisse un peu de temps au thread pour arriver sur WAIT-MSG
        time.sleep(0.05)

        # On envoie une ligne Forth toute simple
        host.send_line(pid, "1 2 + .")

        # On attend que la VM ait eu le temps de traiter le message
        time.sleep(0.1)

        # On collecte tout ce qui a été émis
        items: List[Tuple[int, str]] = []
        while True:
            try:
                items.append(host.stdout_queue.get_nowait())
            except queue.Empty:
                break

        # On concatène ce qui vient de cette VM
        txt = "".join(text for (p, text) in items if p == pid)

        # On vérifie que "3" a été imprimé
        self.assertIn("3", txt)
        
    def test_interpret_evaluates_stdin_line_begin(self) -> None:
        host = ForthOSHostCore()
        pid = host.new_vm("interpretonce", entry="INTERPRET-AGAIN")  

        # On laisse un peu de temps au thread pour arriver sur WAIT-MSG
        time.sleep(0.5)

        # On envoie une ligne Forth toute simple
        host.send_line(pid, "1 2 + .")

        # On attend que la VM ait eu le temps de traiter le message
        time.sleep(0.1)

        # On collecte tout ce qui a été émis
        items: List[Tuple[int, str]] = []
        while True:
            try:
                items.append(host.stdout_queue.get_nowait())
            except queue.Empty:
                break

        # On concatène ce qui vient de cette VM
        txt = "".join(text for (p, text) in items if p == pid)

        # On vérifie que "3" a été imprimé
        self.assertIn("3", txt)
        
class TestForthOSHostCore_Spawn_ModelA(unittest.TestCase):
    """
    Modèle A côté host :
      - SPAWN clone l'environnement Forth (dict + objspace + data)
      - mais les piles D/R de la VM fille sont fraîches
      - et l'ObjectSpace est deep-copié (tables / buffers indépendants)
    """

    # petit helper local pour récupérer la sortie d'un pid donné
    def _drain_stdout_for(self, host: ForthOSHostCore, pid: int) -> str:
        chunks: List[str] = []
        while True:
            try:
                got_pid, txt = host.stdout_queue.get_nowait()
            except queue.Empty:
                break
            if got_pid == pid:
                chunks.append(txt)
        return "".join(chunks)

    def test_spawn_modelA_env_copied_stacks_fresh(self) -> None:
        """
        - On crée une VM parent.
        - On y définit INC, une TABLE T et un BUF B.
        - On met du bruit sur les piles du parent.
        - SPAWN crée une VM enfant.
        - Les piles de l'enfant sont vides (modèle A),
          mais l'environnement Forth (mots / tables / buffers) est identique
          au moment du snapshot (INC, T, B disponibles).
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        # Environnement Forth côté parent
        src_init = r"""
        : INC 1 + ;
        TABLE CONSTANT T
        T DUP 1 100 T!          \ T[1] = 100
        "HELLO" STR>BUF CONSTANT B
        """
        parent_vm.interpret_line(src_init)

        # Du bruit sur les piles du parent
        parent_vm.D.extend([10, 20])
        parent_vm.R.append("ret-marker")

        # SPAWN (modèle A)
        qref = ObjRef(1, ObjKind.QUOT)  # QUOT factice pour l'instant
        pid_child = host.spawn_from(parent_vm, qref)
        self.assertNotEqual(pid_child, pid_parent)

        child_vm = host.vms[pid_child]
        self.assertIsInstance(child_vm, ForthOSVM)

        # Modèle A : piles fraîches dans l'enfant
        self.assertEqual(child_vm.D, [], "pile D de l'enfant doit être vide")
        self.assertEqual(child_vm.R, [], "pile R de l'enfant doit être vide")

        # On vide toute sortie éventuellement en attente
        while True:
            try:
                host.stdout_queue.get_nowait()
            except queue.Empty:
                break

        # On vérifie que parent et enfant partagent la même sémantique visible
        # pour INC, T et B.
        parent_vm.interpret_line("5 INC . BL  T 1 T@ DROP . BL  B BUF>STR .")
        out_parent = self._drain_stdout_for(host, pid_parent)

        child_vm.interpret_line("5 INC . BL  T 1 T@ DROP . BL  B BUF>STR .")
        out_child = self._drain_stdout_for(host, pid_child)

        self.assertEqual(out_parent.strip(), out_child.strip())
        self.assertIn("6", out_parent)       # 5 INC -> 6
        self.assertIn("100", out_parent)     # T[1] = 100
        self.assertIn("HELLO", out_parent)   # contenu du buffer

    def test_spawn_modelA_deepcopy_tables_and_bufs(self) -> None:
        """
        - On crée T et B dans la VM parent.
        - On SPAWN -> VM enfant.
        - Avant mutation, parent et enfant voient T/B identiques.
        - On modifie T et B uniquement dans l'enfant.
        - Le parent ne voit pas ces modifications : deep-copy de l'ObjectSpace.
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        src_init = r"""
        TABLE CONSTANT T
        T DUP 1 111 T!          \ T[1] = 111
        "HELLO" STR>BUF CONSTANT B
        """
        parent_vm.interpret_line(src_init)

        qref = ObjRef(1, ObjKind.QUOT)
        pid_child = host.spawn_from(parent_vm, qref)
        child_vm = host.vms[pid_child]

        # Vide stdout avant de mesurer
        while True:
            try:
                host.stdout_queue.get_nowait()
            except queue.Empty:
                break

        # Avant mutation : même vue sur T et B
        parent_vm.interpret_line("T 1 T@ DROP . BL  B BUF>STR .")
        out_parent_before = self._drain_stdout_for(host, pid_parent)

        child_vm.interpret_line("T 1 T@ DROP . BL  B BUF>STR .")
        out_child_before = self._drain_stdout_for(host, pid_child)

        self.assertEqual(out_parent_before.strip(), out_child_before.strip())
        self.assertIn("111", out_parent_before)
        self.assertIn("HELLO", out_parent_before)

        # Mutations dans l'enfant uniquement
        child_vm.interpret_line(
            "T DUP 1 222 T!   88 B 0 BUF!"  # T[1]=222 et premier octet de B=88 ('X')
        )

        # Mesure après mutation
        child_vm.interpret_line("T 1 T@ DROP . BL  B BUF>STR .")
        out_child_after = self._drain_stdout_for(host, pid_child)

        parent_vm.interpret_line("T 1 T@ DROP . BL  B BUF>STR .")
        out_parent_after = self._drain_stdout_for(host, pid_parent)

        # Dans l'enfant, T[1] a changé
        self.assertIn("222", out_child_after)
        self.assertNotIn("111", out_child_after)

        # Dans le parent, T[1] reste 111
        self.assertIn("111", out_parent_after)
        # Les sorties divergent bien
        self.assertNotEqual(out_parent_after.strip(), out_child_after.strip())

    def test_spawn_modelA_metadata_copied_but_not_shared(self) -> None:
        """
        - On configure version_tag et _versions sur la VM parent.
        - SPAWN -> VM enfant.
        - L'enfant récupère ces valeurs au snapshot.
        - Les modifications ultérieures ne sont pas partagées.
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        parent_vm.version_tag = "parent-env"
        parent_vm._versions = ["v0", "v1"]

        qref = ObjRef(1, ObjKind.QUOT)
        pid_child = host.spawn_from(parent_vm, qref)
        child_vm = host.vms[pid_child]

        # Au moment du spawn, l'enfant doit voir le même état
        self.assertEqual(child_vm.version_tag, "parent-env")
        self.assertEqual(child_vm._versions, ["v0", "v1"])

        # Modif côté enfant uniquement
        child_vm.version_tag = "child-env"
        child_vm._versions.append("v2-child")

        # Parent inchangé
        self.assertEqual(parent_vm.version_tag, "parent-env")
        self.assertEqual(parent_vm._versions, ["v0", "v1"])

        # Enfant modifié
        self.assertEqual(child_vm.version_tag, "child-env")
        self.assertEqual(child_vm._versions, ["v0", "v1", "v2-child"])

    def test_spawn_modelA_entry_quote_runs_and_vm_dies(self) -> None:
        """
        La QUOT passée à SPAWN sert de point d'entrée :

          - le code de la QUOT est exécuté automatiquement dans la VM enfant
          - la VM enfant meurt quand la QUOT se termine
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        # On définit un mot CHILD-MAIN et une QUOT CHILD-ENTRY qui l'appelle.
        src = r"""
        : CHILD-MAIN   "child-ok" .  ;
        [: CHILD-MAIN ;] CONSTANT CHILD-ENTRY
        """
        parent_vm.interpret_line(src)

        # On récupère la QUOT via la constante CHILD-ENTRY
        parent_vm.interpret_line("CHILD-ENTRY")
        qref = parent_vm.D.pop()

        self.assertIsInstance(qref, ObjRef)
        self.assertEqual(qref.kind, ObjKind.QUOT)

        # SPAWN (modèle A) sur cette QUOT
        pid_child = host.spawn_from(parent_vm, qref)
        child_vm = host.vms[pid_child]

        # On attend que l'enfant ait le temps d'exécuter la QUOT et de mourir.
        timeout = 1.0
        start = time.time()
        out_child = ""
        while time.time() - start < timeout:
            out_child += self._drain_stdout_for(host, pid_child)
            # On sort dès qu'on a vu la sortie ET que la VM est morte
            if "child-ok" in out_child and not child_vm.alive:
                break
            time.sleep(0.01)

        self.assertIn("child-ok", out_child)
        self.assertFalse(child_vm.alive, "la VM enfant devrait être morte après la QUOT")


    def test_spawn_modelA_entry_quote_sees_snapshot_env(self) -> None:
        """
        La QUOT d'entrée voit l'environnement du parent au moment du SPAWN.

        - On définit INC et une constante N dans le parent.
        - On crée CHILD-ENTRY = [: N INC . ;]
        - On SPAWN sur cette QUOT.
        - L'enfant doit afficher 41 (N=40, INC = 1+).
        - On modifie ensuite INC et d'autres choses dans le parent : ça ne doit pas
          empêcher l'enfant d'avoir déjà exécuté sa QUOT avec le snapshot initial.
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        src = r"""
        : INC 1 + ;
        40 CONSTANT N
        [: N INC . ;] CONSTANT CHILD-ENTRY
        """
        parent_vm.interpret_line(src)

        # On récupère la QUOT d'entrée
        parent_vm.interpret_line("CHILD-ENTRY")
        qref = parent_vm.D.pop()
        self.assertIsInstance(qref, ObjRef)
        self.assertEqual(qref.kind, ObjKind.QUOT)

        # SPAWN sur cette QUOT
        pid_child = host.spawn_from(parent_vm, qref)
        child_vm = host.vms[pid_child]

        # On change l'environnement du parent APRÈS le spawn, pour vérifier
        # que ça n'interfère pas avec l'exécution déjà démarrée dans l'enfant.
        parent_vm.interpret_line(": INC 100 + ;  999 CONSTANT N2")

        timeout = 1.0
        start = time.time()
        out_child = ""
        while time.time() - start < timeout:
            out_child += self._drain_stdout_for(host, pid_child)
            # si on a déjà vu "41", on peut sortir
            if "41" in out_child:
                break
            # si la VM est morte et qu'il n'y a toujours rien, inutile d'attendre plus
            if not child_vm.alive and out_child:
                break
            time.sleep(0.01)

        # 40 INC -> 41 avec la première définition de INC.
        self.assertIn("41", out_child, f"sortie enfant inattendue: {out_child!r}")

class TestForthOSHostCore_Spawn_Messages(unittest.TestCase):
    def test_child_sends_message_back_to_parent_forth_only(self) -> None:
        """
        Intégration simple :
          - le parent définit une QUOT CHILD-ENTRY purement en Forth
          - CHILD-ENTRY est utilisée comme entry du SPAWN
          - l'enfant envoie un message "hello-from-child" au parent via SEND
          - le test lit le message directement dans inbox du parent

        Toute la logique est en Forth (SELF, TABLE, SEND, SPAWN).
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        src = r"""
        \ PARENT = pid du parent figé dans l'env
        SELF-PID CONSTANT PARENT

        \ CHILD-ENTRY : entry de la VM enfant
        \ construit un message TABLE et l'envoie au parent
        [: 
            {: "type" "msg"
               "to"    PARENT ASSIGN
               "body" "hello-from-child"
             ;} DUP "from" SELF-PID T!
            PARENT SEND
        ;] CONSTANT CHILD-ENTRY

        \ SPAWN ( q -- pid ) : on crée l'enfant
        CHILD-ENTRY SPAWN DROP
        """

        parent_vm.interpret_line(src)

        # On attend le message de l'enfant dans inbox du parent
        msg = parent_vm.inbox.get(timeout=1.0)

        self.assertEqual(msg.get("type"), "msg")
        self.assertEqual(msg.get("body"), "hello-from-child")
        self.assertEqual(msg.get("to"), pid_parent)

        # L'enfant doit avoir un pid différent du parent
        self.assertNotEqual(msg.get("from"), pid_parent)
        self.assertIn(msg.get("from"), host.vms)

        # On peut aussi vérifier que la VM enfant a fini
        child_pid = msg["from"]
        child_vm = host.vms[child_pid]
        # Laisse un peu de temps à l'entry-quot pour se terminer
        time.sleep(0.05)
        self.assertFalse(child_vm.alive)
        

    def test_parent_child_ping_pong_once_forth_only(self) -> None:
        """
        Intégration ping/pong :

          - le parent crée PARENT = pid du parent
          - il définit une entry CHILD-ENTRY purement en Forth :
                WAIT-MSG
                répond "pong" au parent avec SEND
          - SPAWN CHILD-ENTRY -> CHILD constant = pid de l'enfant
          - le parent construit un message "ping" et l'envoie à CHILD
          - le test lit dans inbox du parent un "pong" venant de l'enfant

        Toute la logique d'échange est en Forth (WAIT-MSG, SEND, TABLE, SELF, SPAWN).
        """
        host = ForthOSHostCore()
        pid_parent = host.new_vm("parent")
        parent_vm = host.vms[pid_parent]

        src = r"""
        \ PARENT = pid du parent, vu par le parent et par l'enfant (snapshot env)
        SELF-PID CONSTANT PARENT

        \ CHILD-ENTRY : entry de l'enfant
        \  - attend un message dans son inbox
        \  - ignore le contenu
        \  - renvoie "pong" au parent
        [: 
            WAIT-MSG      \ ( msg-table ) on ne l'analyse pas ici
            DROP          \ on jette la table : on veut juste synchroniser

            {: "type" "msg"
               "to"   PARENT ASSIGN
               "body" "pong"
             ;} DUP "from" SELF-PID T!
            PARENT SEND
        ;] CONSTANT CHILD-ENTRY

        \ On SPAWN sur CHILD-ENTRY, et on garde le pid dans CHILD
        CHILD-ENTRY SPAWN CONSTANT CHILD

        \ Le parent envoie un "ping" à l'enfant
        {: "type" "msg"
           "from" PARENT  ASSIGN
           "to"   CHILD  ASSIGN
           "body" "ping"
         ;}
        CHILD SEND
        """

        parent_vm.interpret_line(src)

        # On attend la réponse "pong" dans inbox du parent
        msg = parent_vm.inbox.get(timeout=1.0)

        self.assertEqual(msg.get("type"), "msg")
        self.assertEqual(msg.get("body"), "pong")
        self.assertEqual(msg.get("to"), pid_parent)

        child_pid = msg.get("from")
        self.assertIn(child_pid, host.vms)

        # Laisse une petite marge pour la fin de la QUOT d'entrée
        time.sleep(0.05)
        self.assertFalse(host.vms[child_pid].alive)

# ============================================================
# Runner de tests
# ============================================================

def test_all() -> None:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)


if __name__ == "__main__":
    test_all()
