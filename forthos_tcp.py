#!/usr/bin/env python3
# forthos_tcp.py
#
# Deux classes :
#  - HostTCPServer : wrappe ForthOSHostCore derrière un serveur TCP JSON-Lines
#  - TuiTCPClient  : client TCP qui expose la même interface que ForthOSHostCore
#
# Idée : la TUI remplace ForthOSHostCore() par TuiTCPClient("127.0.0.1", 4242)
#       et continue d'utiliser host.new_vm / host.send_line / host.list_vms / host.reset
#       + host.stdout_queue comme avant.

from __future__ import annotations

import json
import queue
import socket
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

from forthos_host_core import ForthOSHostCore
from forthos_host_repl import run_dot_command



# ======================================================================
# HostTCPServer : process host
# ======================================================================

class HostTCPServer:
    """
    Serveur TCP qui expose une instance de ForthOSHostCore via un protocole
    JSON-Lines très simple.

    - Une seule connexion client (la TUI) pour commencer.
    - Les messages stdout des VMs sont poussés en asynchrone vers le client.
    - Les opérations host sont exposées via des RPC : new_vm, kill_vm, list_vms,
      send_line, reset.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4242,
        host_core: Optional[ForthOSHostCore] = None,
    ) -> None:
        self.host_addr = host
        self.port = port
        self.host_core = host_core if host_core is not None else ForthOSHostCore()
        self._sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

    def serve_forever(self) -> None:
        """Boucle principale du serveur : accepte un client et le gère."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host_addr, self.port))
        self._sock.listen(1)
        print(f"[HostTCPServer] listening on {self.host_addr}:{self.port}")

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = self._sock.accept()
                except OSError:
                    # Socket fermé pendant accept() ?
                    if self._stop_event.is_set():
                        break
                    raise
                print(f"[HostTCPServer] client connected from {addr}")
                try:
                    # On isole les erreurs du handler pour ne pas faire tomber le serveur
                    try:
                        self._handle_client(conn)
                    except Exception as e:
                        print(f"[HostTCPServer] error in client handler: {e!r}")
                finally:
                    conn.close()
                    print(f"[HostTCPServer] client disconnected")
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


    def _handle_client(self, conn: socket.socket) -> None:
        """
        Gère un client :
          - un thread pour forward stdout_queue vers le socket
          - le thread courant lit les RPC et renvoie les résultats
        """
        conn_file_r = conn.makefile("r", encoding="utf-8", newline="\n")
        conn_file_w = conn.makefile("w", encoding="utf-8", newline="\n")    

        stop_local = threading.Event()

        def stdout_forwarder() -> None:
            """Forward host_core.stdout_queue -> messages JSON stdout."""
            q = self.host_core.stdout_queue
            while not stop_local.is_set() and not self._stop_event.is_set():
                try:
                    pid, text = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                msg = {"kind": "stdout", "pid": pid, "text": text}
                try:
                    conn_file_w.write(json.dumps(msg) + "\n")
                    conn_file_w.flush()
                except OSError:
                    # client déconnecté
                    break

        th = threading.Thread(target=stdout_forwarder, daemon=True)
        th.start()

        # Boucle de réception des RPC
        for line in conn_file_r:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                # on ignore les lignes invalides
                print(f"[HostTCPServer] JSON error: {e} for line={line!r}")
                continue

            if msg.get("kind") != "rpc":
                # protocole inconnu : on ignore
                continue

            rpc_id = msg.get("id")
            op = msg.get("op")
            args = msg.get("args") or {}

            response: Dict[str, Any] = {"kind": "rpc_result", "id": rpc_id}
            try:
                result = self._dispatch_rpc(op, args)
                response["ok"] = True
                response["result"] = result
            except Exception as e:
                response["ok"] = False
                response["error"] = str(e)

            try:
                conn_file_w.write(json.dumps(response) + "\n")
                conn_file_w.flush()
            except OSError:
                break

        # fin de connexion
        stop_local.set()
        th.join(timeout=0.5)

    def _dispatch_rpc(self, op: str, args: Dict[str, Any]) -> Any:
        """
        Route une opération RPC vers ForthOSHostCore.
        """
        if op == "new_vm":
            name = args.get("name", "")
            entry = args.get("entry")
            pid = self.host_core.new_vm(name=name, entry=entry)
            return {"pid": pid}

        elif op == "kill_vm":
            pid = int(args["pid"])
            self.host_core.kill_vm(pid)
            return {"ok": True}

        elif op == "list_vms":
            # le host_core renvoie déjà une liste de dict
            return self.host_core.list_vms()

        elif op == "send_line":
            pid = int(args["pid"])
            line = str(args["line"])
            self.host_core.send_line(pid, line)
            return {"ok": True}

        elif op == "reset":
            self.host_core.reset()
            return {"ok": True}

        elif op == "gc_dead_vms":
            # retourne juste le nombre de VMs supprimées
            n = self.host_core.gc_dead_vms()
            return int(n)

        elif op == "get_words":
            """
            Renvoie la liste des mots Forth connus d'une VM (pour complétion).
            """
            pid = int(args["pid"])
            vm = self.host_core.vms.get(pid)  # type: ignore[attr-defined]
            if vm is None:
                return []
            try:
                words = vm.dict.all_words()
            except Exception:
                return []

            seen = set()
            out: list[str] = []
            for w in words:
                name = getattr(w, "name", None)
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append(name)

            out.sort(key=str.upper)
            return out

        elif op == "dot_command":
            """
            Exécute un dot-command sur la VM donnée et renvoie la sortie texte.
            """
            pid = int(args["pid"])
            cmd = str(args.get("cmd", ""))
            vm = self.host_core.vms.get(pid)  # type: ignore[attr-defined]
            if vm is None:
                raise ValueError(f"No such VM pid={pid}")

            import io
            buf = io.StringIO()
            run_dot_command(vm, cmd, buf)
            return buf.getvalue()

        else:
            raise ValueError(f"Unknown RPC op: {op!r}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None


# Petit main optionnel pour lancer le host en serveur
def main_host_server() -> None:
    srv = HostTCPServer()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[HostTCPServer] interrupted, stopping...")
        srv.stop()


# ======================================================================
# TuiTCPClient : côté TUI, même API que ForthOSHostCore
# ======================================================================

class TuiTCPClient:
    """
    Client TCP pour la TUI. Expose (en gros) la même API que ForthOSHostCore :

        host = TuiTCPClient("127.0.0.1", 4242)
        pid = host.new_vm("main", entry="INTERPRET-AGAIN")
        host.send_line(pid, "1 2 + .")
        vms = host.list_vms()
        host.reset()

    Et fournit une stdout_queue qui contient (pid, text) comme le host réel.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4242) -> None:
        self.host_addr = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host_addr, self.port))
        self._file_r = self._sock.makefile("r", encoding="utf-8", newline="\n")
        self._file_w = self._sock.makefile("w", encoding="utf-8", newline="\n")

        # stdout_queue publique, pour que la TUI branche son _stdout_worker dessus
        self.stdout_queue: queue.Queue[Tuple[int, str]] = queue.Queue()

        # gestion des réponses RPC (id -> queue)
        self._next_id = 1
        self._pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._pending_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop, daemon=True
        )
        self._receiver_thread.start()

    # ---------- API "type ForthOSHostCore" ----------

    def new_vm(self, name: str = "", entry: Optional[str] = None) -> int:
        res = self._rpc_call("new_vm", {"name": name, "entry": entry})
        return int(res["pid"])

    def kill_vm(self, pid: int) -> None:
        self._rpc_call("kill_vm", {"pid": int(pid)})

    def list_vms(self) -> List[Dict[str, Any]]:
        res = self._rpc_call("list_vms", {})
        # le serveur renvoie déjà une liste de dict
        return list(res)

    def send_line(self, pid: int, line: str) -> None:
        self._rpc_call("send_line", {"pid": int(pid), "line": line})

    def reset(self) -> None:
        self._rpc_call("reset", {})
    
    def gc_dead_vms(self) -> int:
        """
        Demande au host de supprimer les VMs mortes.
        Renvoie le nombre de VMs supprimées.
        """
        res = self._rpc_call("gc_dead_vms", {})
        return int(res)

    def get_words(self, pid: int) -> List[str]:
        """
        Récupère la liste des mots Forth d'une VM (pour la complétion).
        """
        res = self._rpc_call("get_words", {"pid": int(pid)})
        # le serveur renvoie déjà une liste de str
        return list(res)

    def dot_command(self, pid: int, cmd: str) -> str:
        """
        Exécute un dot-command côté host et renvoie la sortie texte.
        """
        res = self._rpc_call("dot_command", {"pid": int(pid), "cmd": cmd})
        return str(res)


    # ---------- Impl interne RPC & réception ----------

    def _rpc_call(self, op: str, args: Dict[str, Any], timeout: float = 5.0) -> Any:
        with self._pending_lock:
            rpc_id = self._next_id
            self._next_id += 1
            q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
            self._pending[rpc_id] = q

        msg = {"kind": "rpc", "id": rpc_id, "op": op, "args": args}
        try:
            self._file_w.write(json.dumps(msg) + "\n")
            self._file_w.flush()
        except OSError as e:
            raise RuntimeError(f"TuiTCPClient: write failed: {e}")

        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"TuiTCPClient: no response for rpc id={rpc_id}")

        if not resp.get("ok", False):
            raise RuntimeError(
                f"TuiTCPClient RPC error for {op}: {resp.get('error')}"
            )
        return resp.get("result")

    def _receiver_loop(self) -> None:
        """
        Boucle qui lit en permanence le socket et :
          - route les rpc_result vers les queues en attente
          - met les stdout dans self.stdout_queue
        """
        for line in self._file_r:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = msg.get("kind")
            if kind == "rpc_result":
                rpc_id = msg.get("id")
                with self._pending_lock:
                    q = self._pending.pop(rpc_id, None)
                if q is not None:
                    q.put(msg)
            elif kind == "stdout":
                pid = int(msg.get("pid", -1))
                text = str(msg.get("text", ""))
                self.stdout_queue.put((pid, text))
            else:
                # on ignore les autres types pour l'instant
                pass

        # socket fermé => on arrête
        self._stop_event.set()

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        self._receiver_thread.join(timeout=0.5)


# ======================================================================
# Tests unitaires pour HostTCPServer / TuiTCPClient
# ======================================================================

class FakeHostCore:
    """
    Faux ForthOSHostCore pour tester HostTCPServer / TuiTCPClient sans
    dépendre de l'implémentation réelle de la VM.
    """

    def __init__(self) -> None:
        self.stdout_queue: "queue.Queue[Tuple[int, str]]" = queue.Queue()
        self.vms: Dict[int, Dict[str, Any]] = {}
        self._next_pid = 1
        self.calls: List[Tuple[Any, ...]] = []

    def new_vm(self, name: str = "", entry: Optional[str] = None) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self.vms[pid] = {"pid": pid, "name": name, "entry": entry}
        self.calls.append(("new_vm", name, entry))
        return pid

    def kill_vm(self, pid: int) -> None:
        self.calls.append(("kill_vm", pid))
        self.vms.pop(pid, None)

    def list_vms(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_vms",))
        return list(self.vms.values())

    def send_line(self, pid: int, line: str) -> None:
        self.calls.append(("send_line", pid, line))
        # Simule une sortie de VM sur stdout
        self.stdout_queue.put((pid, line))

    def reset(self) -> None:
        self.calls.append(("reset",))
        self.vms.clear()


class TestHostTCPIntegration(unittest.TestCase):
    def setUp(self) -> None:
        # Hôte et port libres
        self.host = "127.0.0.1"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, 0))
            self.port = s.getsockname()[1]

        self.fake_core = FakeHostCore()
        self.server = HostTCPServer(
            host=self.host,
            port=self.port,
            host_core=self.fake_core,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()

        # Création du client avec quelques tentatives (serveur peut mettre un peu
        # de temps à se lier au port).
        self.client = self._create_client_with_retry(self.host, self.port)

    def tearDown(self) -> None:
        self.client.close()
        self.server.stop()
        self.server_thread.join(timeout=1.0)

    def _create_client_with_retry(
        self, host: str, port: int, retries: int = 20, delay: float = 0.05
    ) -> TuiTCPClient:
        last_err: Optional[BaseException] = None
        for _ in range(retries):
            try:
                return TuiTCPClient(host, port)
            except OSError as e:
                last_err = e
                time.sleep(delay)
        assert last_err is not None
        raise last_err

    def test_new_vm_and_list_vms(self) -> None:
        pid = self.client.new_vm(name="vm1", entry="ENTRY")
        self.assertEqual(pid, 1)
        self.assertIn(("new_vm", "vm1", "ENTRY"), self.fake_core.calls)

        vms = self.client.list_vms()
        self.assertEqual(len(vms), 1)
        self.assertEqual(vms[0]["pid"], pid)
        self.assertEqual(vms[0]["name"], "vm1")
        self.assertIn(("list_vms",), self.fake_core.calls)

    def test_send_line_forwards_to_stdout(self) -> None:
        pid = self.client.new_vm(name="vm1")
        # Envoyer une ligne, le FakeHostCore la pousse dans stdout_queue,
        # le serveur la forward au client.
        test_line = "HELLO"
        self.client.send_line(pid, test_line)

        recv_pid, text = self.client.stdout_queue.get(timeout=1.0)
        self.assertEqual(recv_pid, pid)
        self.assertEqual(text, test_line)
        self.assertIn(("send_line", pid, test_line), self.fake_core.calls)

    def test_reset_and_kill_vm(self) -> None:
        pid1 = self.client.new_vm(name="vm1")
        pid2 = self.client.new_vm(name="vm2")
        self.client.kill_vm(pid1)
        self.client.reset()

        self.assertIn(("kill_vm", pid1), self.fake_core.calls)
        self.assertIn(("reset",), self.fake_core.calls)

        vms = self.client.list_vms()
        # Après reset(), FakeHostCore.vms est vidé
        self.assertEqual(vms, [])

    def test_unknown_rpc_raises(self) -> None:
        # On force un RPC avec op inconnu, ce qui doit faire lever un RuntimeError
        with self.assertRaises(RuntimeError):
            self.client._rpc_call("unknown_op", {})


if __name__ == "__main__":
    # Si on lance "python forthos_tcp.py test" => exécute les tests.
    # Sinon : lance le serveur host.
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(sys.modules[__name__])
        runner = unittest.TextTestRunner(verbosity=2)
        runner.run(suite)
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        main_host_server()
        
    else: print(f'Usage: python {sys.argv[0]} [test|serve]')

