#!/usr/bin/env python3
# forthos_runtime.py
#
# Couche ForthOS au-dessus du noyau forthos_vm_core.VM :
# - ForthOSVM : VM avec pid, host, inbox, thread
# - primitives ForthOS : SELF, WAIT-MSG, SPAWN, SEND
# Ce fichier ne contient PAS encore le host complet (ForthOSHost).

from __future__ import annotations

import threading
import queue
import io
import sys
import unittest
import traceback
from typing import Any, Optional, Dict, Tuple, List, Callable, TYPE_CHECKING

from forthos_vm_core import VM, Word, CodeClass, WFlags, ObjKind, ObjRef

# Petit runtime ForthOS chargé dans chaque ForthOSVM
# - helpers pour lire les champs d'un message TABLE
# - boucle INTERPRET qui consomme les messages "stdin"/"msg"/"ctrl"

FORTHOS_BOOT_SRC = """
: MSG-TYPE ( msg -- msg type$ )
  DUP "type" T@ DROP
;

: MSG-LINE ( msg -- msg line$ )
  DUP "line" T@ DROP
;

: MSG-CMD ( msg -- msg cmd$ )
  DUP "cmd" T@ DROP
;
: HANDLE-MSG ;
: HANDLE-CTRL ;
: HANDLE-LINE NIP EVALUATE-FORTH ;

: INTERPRET-ONCE ( -- )
  WAIT-MSG
  MSG-TYPE
  CASE
    "stdin" OF MSG-LINE HANDLE-LINE ENDOF 
    "msg"   OF HANDLE-MSG ENDOF
    "ctrl"  OF HANDLE-CTRL ENDOF
  ENDCASE ;

: INTERPRET-AGAIN ( -- )
  BEGIN
    INTERPRET-ONCE
  AGAIN ;
 
"""


if TYPE_CHECKING:
    from typing import TextIO


# ============================================================
# Protocole host minimal
# ============================================================

class HasStdoutHandler:
    """
    Protocole minimal pour le "host" vu par ForthOSVM.

    Le vrai host devra au moins fournir :
      - handle_stdout(pid: int, text: str) -> None
      - spawn_from(parent_vm: ForthOSVM, qref: ObjRef) -> int
      - deliver_msg(pid: int, msg: dict) -> None
    """

    def handle_stdout(self, pid: int, text: str) -> None:
        raise NotImplementedError

    def spawn_from(self, parent_vm: "ForthOSVM", qref: ObjRef) -> int:
        raise NotImplementedError

    def deliver_msg(self, pid: int, msg: Dict[str, Any]) -> None:
        raise NotImplementedError


# ============================================================
# ForthOSVM : VM + pid + host + inbox + thread
# ============================================================

class ForthOSVM(VM):
    """
    VM Forth intégrée dans ForthOS.

    Responsabilités :
    - garder une inbox (queue) de messages Python
    - exposer des primitives ForthOS (SELF, WAIT-MSG, SPAWN, SEND)
    - rediriger la sortie texte -> host.handle_stdout(pid, text)
    - tourner dans un thread dédié via start()
    """

    def __init__(self, host: HasStdoutHandler, pid: int) -> None:
        super().__init__()
        self.host: HasStdoutHandler = host
        self.pid: int = pid
        self.inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.alive: bool = True
        self.thread: Optional[threading.Thread] = None

        # primitives ForthOS
        self._install_forthos_words()
        
        # runtime ForthOS : helpers + INTERPRET
        # (on ne lance pas INTERPRET ici, on ne fait que définir les mots)
        try:
            self.interpret_line(FORTHOS_BOOT_SRC)

        except Exception as e:
            # en pratique, ça ne devrait pas arriver ; si oui, on log dans la sortie host
            tb = traceback.format_exc()
            self.host.handle_stdout(self.pid, f"[VM {self.pid} boot error] {e}\n")
            print(tb)

    # ----------------- IO : override emit -----------------

    def emit(self, text: str) -> None:
        """
        Override : toute sortie texte passe par le host.
        """
        if text:
            self.host.handle_stdout(self.pid, text)

    # ----------------- Threading -----------------

    def start(self, entry: Optional[str] = None) -> None:
        """
        Démarre le thread de cette VM.

        entry:
          - None : le thread ne fait rien et se termine immédiatement.
          - sinon : exécute une ligne Forth `entry` puis s'arrête quand
            le code Forth a fini (sauf si ce code boucle à l'infini).
        """

        def target() -> None:
            try:
                self.alive = True
                if entry is not None:
                    # Ici, tu mettras plus tard un vrai point d'entrée,
                    # typiquement "INTERPRET" ou un mot utilisateur.
                    self.interpret_line(entry)
            except Exception as e:
                self.host.handle_stdout(self.pid, f"[VM {self.pid} exception] {e}\n")
            finally:
                # Quand le mot d'entrée a fini (ou en cas d'exception),
                # la VM est considérée comme morte.
                self.alive = False

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        
    def start_with_entry_quot(self, entry_qref: ObjRef) -> None:
        """
        Variante de start() qui exécute une QUOT comme point d'entrée.

        - entry_qref : ObjRef vers une QUOT valide dans l'ObjectSpace de cette VM.
        - lance un thread qui fait :
            call_quotation(entry_qref)
            puis exécute la boucle threaded jusqu'au retour de la QUOT.
        Quand la QUOT se termine (ou en cas d'exception), self.alive passe à False.
        """
        def target() -> None:
            try:
                self.alive = True
                # Positionne l'IP sur la QUOT et exécute-la jusqu'au retour.
                self.call_quotation(entry_qref)
                self._run_threaded()
            except Exception as e:
                tb = traceback.format_exc()
                self.host.handle_stdout(self.pid, f"[VM {self.pid} exception] {e}\n")
                print(tb)
            finally:
                self.alive = False

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()


    def stop(self) -> None:
        """Marque la VM comme morte (le thread sortira de sa boucle)."""
        self.alive = False

    # ----------------- Primitives ForthOS -----------------


    def _install_forthos_words(self) -> None:
        """
        Installe / réinstalle les primitives spécifiques à ForthOS :
        SELF ( -- pid )
        WAIT-MSG ( -- msg_python )
        SEND ( msg_python pid -- )
        SPAWN ( qref -- pid )

        - premier appel (dans __init__) : crée les primitives
        - après un _env_from_dict : recolle les primitives sur les mots existants
          (mêmes tokens, même dictionnaire), pour que les QUOT compilées continuent
          à fonctionner.
        """

        def add_prim(name: str, fn: Callable, doc: str = "") -> None:
            # Si le mot existe déjà (cas après _env_from_dict), on recolle la primitive.
            w = self.dict.find_any(name)
            if w is not None:
                w.code_class = CodeClass.PRIMITIVE
                w.prim = fn
                w.flags = WFlags.NONE
                if doc:
                    w.doc = doc
                return
            # Sinon on crée une nouvelle primitive (cas initial).
            self.dict.add_primitive(name, fn, flags=WFlags.NONE, doc=doc)


        # SELF ( -- pid )
        def prim_SELF(vm: "ForthOSVM") -> None:
            vm.D.append(vm.pid)

        # WAIT-MSG ( -- msg_python )
        def prim_WAIT_MSG(vm: "ForthOSVM") -> None:
            """
            WAIT-MSG ( -- msg-table )
        
            Bloque sur vm.inbox (dict Python), le convertit en TABLE Forth
            et pousse l'ObjRef sur la pile.
            """
            msg_py = vm.inbox.get()  # dict Python
            tbl_ref = vm.objspace.py_to_table(msg_py)
            vm.D.append(tbl_ref)

        # SEND ( msg pid -- )
        def prim_SEND(vm: "ForthOSVM") -> None:
            """
            SEND ( msg-table pid -- )
        
            Convertit une TABLE Forth en dict Python et appelle host.deliver_msg(pid, dict).
            """
            pid = vm.D.pop()
            msg_tbl = vm.D.pop()
        
            if not isinstance(msg_tbl, ObjRef) or msg_tbl.kind is not ObjKind.TABLE:
                raise TypeError("SEND attend ( msg-table pid -- ) avec msg-table = TABLE")
        
            msg_py = vm.objspace.table_to_py(msg_tbl)
            vm.host.deliver_msg(pid, msg_py)

        # SPAWN ( qref -- pid )
        def prim_SPAWN(vm: "ForthOSVM") -> None:
            qref = vm.D.pop()
            if not isinstance(qref, ObjRef):
                raise TypeError("SPAWN attend un ObjRef vers une QUOT")
            pid = vm.host.spawn_from(vm, qref)
            vm.D.append(pid)


        add_prim("SELF-PID", prim_SELF, doc="( -- pid ) rend le pid de la VM courante")
        add_prim("WAIT-MSG", prim_WAIT_MSG, doc="( -- msg ) bloque jusqu'à un message dans inbox")
        add_prim("SEND", prim_SEND, doc="( msg pid -- ) envoie msg à la VM pid via host.deliver_msg")
        add_prim("SPAWN", prim_SPAWN, doc="( q -- pid ) demande au host de créer une nouvelle VM à partir de q")


# ============================================================
# FakeHost pour tests unitaires de ForthOSVM
# ============================================================

class FakeHost(HasStdoutHandler):
    """
    Host factice pour tester ForthOSVM sans implémenter tout ForthOSHost.
    """

    def __init__(self) -> None:
        self.stdout_events: List[Tuple[int, str]] = []
        self.spawn_calls: List[Tuple[ForthOSVM, ObjRef, int]] = []
        self.msg_events: List[Tuple[int, Dict[str, Any]]] = []
        self._next_pid: int = 2

    def handle_stdout(self, pid: int, text: str) -> None:
        self.stdout_events.append((pid, text))

    def spawn_from(self, parent_vm: ForthOSVM, qref: ObjRef) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self.spawn_calls.append((parent_vm, qref, pid))
        return pid

    def deliver_msg(self, pid: int, msg: Dict[str, Any]) -> None:
        self.msg_events.append((pid, msg))

####################################################################
# LES TESTS CI DESSOUS NE DOIVENT JAMAIS ETRE MODIFIÉ. ILS SONT LA POUR VALIDER L'IMPLÉMENTATION PLUS HAUT ET SERVENT DE RÉFÉRENCE.
# NE JAMAIS MODIFIER LES TEST.
# ============================================================
# Tests unitaires ForthOSVM (sans vrai host)
# ============================================================

class TestForthOSVM_IO(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_dot_and_cr_emit_stdout_events(self) -> None:
        self.vm.interpret_line("42 . CR")
        self.assertEqual(self.host.stdout_events, [
            (1, "42"),
            (1, "\n"),
        ])


class TestForthOSVM_WaitMsg(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_wait_msg_pushes_table_on_stack(self) -> None:
        # On prépare un message Python
        msg = {"type": "ping", "value": 123}
        self.vm.inbox.put(msg)

        word = self.vm.dict.find("WAIT-MSG")
        word.execute(self.vm)

        # La pile doit contenir une TABLE
        self.assertEqual(len(self.vm.D), 1)
        top = self.vm.D[-1]
        self.assertIsInstance(top, ObjRef)
        self.assertEqual(top.kind, ObjKind.TABLE)

        # Et la TABLE doit être équivalente au dict d'origine
        as_py = self.vm.objspace.table_to_py(top)
        self.assertEqual(as_py, msg)
        
class TestForthOSVM_Send(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_send_converts_table_to_dict_and_calls_host(self) -> None:
        # TABLE Forth créée côté Python pour le test
        msg_py = {"text": "hello", "n": 42}
        tbl_ref = self.vm.objspace.py_to_table(msg_py)

        self.vm.D.append(tbl_ref)
        self.vm.D.append(0)  # message pour le host

        word = self.vm.dict.find("SEND")
        word.execute(self.vm)

        # FakeHost.deliver_msg doit avoir été appelé avec pid=0 et le même dict
        self.assertEqual(len(self.host.msg_events), 1)
        pid, msg = self.host.msg_events[0]
        self.assertEqual(pid, 0)
        self.assertEqual(msg, msg_py)
        
class TestForthOSVM_WaitAndSendHello(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_quote_waits_and_sends_hello_to_host(self) -> None:
        src = r""" 
        [: 
            WAIT-MSG DROP 
            {: "text" "hello world" ;} 0 SEND   \\ 0 == Host pid 
        ;] CALL
        """

        self.vm.start(entry=src)

        import time
        time.sleep(0.05)  # la QUOT doit atteindre WAIT-MSG

        # on envoie un message Python (dict) dans la inbox
        self.vm.inbox.put({"type": "ping"})

        # On attend que SEND soit exécuté
        timeout = 1.0
        start = time.time()
        while not self.host.msg_events and time.time() - start < timeout:
            time.sleep(0.01)

        self.assertTrue(self.host.msg_events, "aucun message envoyé au host")

        pid, msg = self.host.msg_events[0]
        self.assertEqual(pid, 0)
        self.assertEqual(msg.get("text"), "hello world")


class TestForthOSVM_Spawn(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_spawn_calls_host_and_pushes_pid(self) -> None:
        # fake ObjRef pour le test
        qref = ObjRef(42, ObjKind.QUOT)  # QUOT factice
        self.vm.D.append(qref)

        word = self.vm.dict.find("SPAWN")
        word.execute(self.vm)

        self.assertEqual(len(self.host.spawn_calls), 1)
        parent, qref_called, pid = self.host.spawn_calls[0]
        self.assertIs(parent, self.vm)
        self.assertIs(qref_called, qref)
        self.assertEqual(pid, 2)
        self.assertEqual(self.vm.D[-1], 2)
        
class TestForthOSVM_InterpretLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_interpret_stdin_line_prints_3(self) -> None:
        # On envoie un message stdin dans la inbox
        self.vm.inbox.put({"type": "stdin", "line": "1 2 + ."})

        # On exécute une seule itération de la boucle serveur
        self.vm.interpret_line("INTERPRET-ONCE")

        txt = "".join(t for (pid, t) in self.host.stdout_events if pid == 1)
        self.assertIn("3", txt)


class TestForthOSVM_MessageTypes(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.vm = ForthOSVM(self.host, pid=1)

    def test_send_supports_int_key_and_bytearray_value(self) -> None:
        # dict Python avec clé int et valeur bytearray
        payload = bytearray(b"abc\x00\xff")
        msg_py = {
            1: payload,         # clé int
            "n": 42,            # clé str
        }

        # On passe par py_to_table pour utiliser la conversion standard
        tbl_ref = self.vm.objspace.py_to_table(msg_py)

        # ( msg pid -- ) pour SEND
        self.vm.D.append(tbl_ref)
        self.vm.D.append(0)  # 0 = host dans notre convention de test

        word = self.vm.dict.find("SEND")
        word.execute(self.vm)

        # Un seul message doit avoir été envoyé au host
        self.assertEqual(len(self.host.msg_events), 1)
        pid, msg = self.host.msg_events[0]

        self.assertEqual(pid, 0)
        # Les clés et types doivent être préservés
        self.assertIn(1, msg)
        self.assertEqual(msg[1], payload)
        self.assertIsInstance(msg[1], bytearray)
        self.assertEqual(msg["n"], 42)

    def test_wait_msg_converts_bytearray_back_to_buf(self) -> None:
        # Côté host / Python, on envoie un dict avec bytearray vers la inbox
        payload = bytearray(b"hello")
        msg_py = {
            "type": "custom",
            99: payload,
        }
        self.vm.inbox.put(msg_py)

        # WAIT-MSG doit pousser une TABLE sur la pile
        word = self.vm.dict.find("WAIT-MSG")
        word.execute(self.vm)

        self.assertEqual(len(self.vm.D), 1)
        top = self.vm.D[-1]
        self.assertIsInstance(top, ObjRef)
        self.assertEqual(top.kind, ObjKind.TABLE)

        # On repasse par table_to_py : on doit retrouver le même dict
        roundtrip = self.vm.objspace.table_to_py(top)
        self.assertIn(99, roundtrip)
        self.assertIsInstance(roundtrip[99], bytearray)
        self.assertEqual(roundtrip[99], payload)
        self.assertEqual(roundtrip["type"], "custom")

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
