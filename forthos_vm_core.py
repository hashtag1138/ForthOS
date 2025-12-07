#!/usr/bin/env python3
# forthos_vm_core.py
#
# Noyau Forth mono-VM pour ForthOS.
# - Repris de forthvm_old (token-threaded VM complète)
# - Sans scheduler / VMManager / host REPL
# - Avec émisssion standard centralisée via VM.emit(text)
#
from __future__ import annotations
import unittest
from dataclasses import dataclass, field
from typing import Optional as _Optional
from enum import Enum, IntFlag
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union, Callable
import json, os, sys, io, time, shutil, argparse
from collections import deque
import copy

class ObjKind(Enum):
    TABLE = "TABLE"
    QUOT  = "QUOT"
    BUF   = "BUF"

@dataclass(frozen=True)
class ObjRef:
    kind: ObjKind
    oid: int
    def __hash__(self) -> int: return hash((self.kind, self.oid))

    def __repr__(self) -> str:
        return f"OBJ[{self.kind.value}:{self.oid}]"

TableKey = Union[int, str, "Word", ObjRef, Tuple[str,int]]  # ("BODY", token)
TableVal = Union[int, str, ObjRef]

@dataclass
class TableCtx:
    tref: ObjRef
    pending_key: _Optional[TableKey] = None

class PersistenceCodec:
    @staticmethod
    def encode_key(k):
        if isinstance(k, int):  return {"kind":"int",  "value":k}
        if isinstance(k, str):  return {"kind":"str",  "value":k}
        if isinstance(k, Word): return {"kind":"word", "value":k.token}
        if isinstance(k, ObjRef): return {"kind":k.kind.value.lower(), "value":k.oid}
        if isinstance(k, tuple) and k and k[0]=="BODY": return {"kind":"body", "value":k[1]}
        raise TypeError(f"Unsupported key type: {type(k)}")

    @staticmethod
    def decode_key(d, token_to_word):
        t = d.get("kind"); v = d.get("value")
        if t == "int": return v
        if t == "str": return v
        if t == "word": return token_to_word(v)
        if t == "table": return ObjRef(ObjKind.TABLE, v)
        if t == "quot":  return ObjRef(ObjKind.QUOT, v)
        if t == "buf":  return ObjRef(ObjKind.BUF, v)
        if t == "body":  return ("BODY", v)
        raise ValueError(f"Unsupported key kind: {t}")

    @staticmethod
    def encode_val(v):
        if isinstance(v, int): return {"kind":"int", "value":v}
        if isinstance(v, str): return {"kind":"str", "value":v}
        if isinstance(v, ObjRef): return {"kind":v.kind.value.lower(), "value":v.oid}
        raise TypeError(f"Unsupported value type: {type(v)}")

    @staticmethod
    def decode_val(d):
        t = d.get("kind"); v = d.get("value")
        if t == "int": return v
        if t == "str": return v
        if t == "table": return ObjRef(ObjKind.TABLE, v)
        if t == "quot":  return ObjRef(ObjKind.QUOT, v)
        if t == "buf":   return ObjRef(ObjKind.BUF,  v)
        raise ValueError(f"Unsupported value kind: {t}")
        
class ObjectSpace:
    
    """
    Holds heap-allocated objects (tables, quotations) and does *refcount* for QUOT values referenced
    by tables. (Tables themselves are not refcounted; TDROP deletes them recursively.)
    """
    def __init__(self) -> None:
        self._next_oid: int = 1
        self._tables: Dict[int, Dict[TableKey, TableVal]] = {}
        self._quots: Dict[int, Dict[str, Any]] = {}  # { oid: {"code":[int], "env":Any, "ref":int} }
        self._bufs: Dict[int, bytearray] = {}
        
    def sweep_unreached(self, vm=None) -> tuple[int, int]:
        """
        Mark & sweep for QUOT and BUF.
        - Marks objects reachable from VM stacks, PFAs, dictionary, data space,
          and recursively from all TABLEs (keys and values).
        - Sweeps QUOT with ref<=0 that are unmarked; sweeps BUF that are unmarked.
        Returns (swept_quots, swept_bufs).
        """
        roots_quots: set[int] = set()
        roots_bufs: set[int] = set()
    
        seen_tables: set[int] = set()
    
        def mark_cell(x):
            if isinstance(x, ObjRef):
                if x.kind is ObjKind.QUOT and x.oid in self._quots:
                    roots_quots.add(int(x.oid))
                elif x.kind is ObjKind.BUF and x.oid in self._bufs:
                    roots_bufs.add(int(x.oid))
                elif x.kind is ObjKind.TABLE:
                    mark_table(x)
    
        def mark_table(tref: ObjRef):
            oid = int(tref.oid)
            if oid in seen_tables:
                return
            seen_tables.add(oid)
            t = self._tables.get(oid)
            if not t:
                return
            for k, v in t.items():
                mark_cell(k)
                mark_cell(v)
    
        # From VM (if provided)
        if vm is not None:
            # Data and return stacks
            for x in list(getattr(vm, "D", [])):
                mark_cell(x)
            for x in list(getattr(vm, "R", [])):
                mark_cell(x)
            # Current and saved PFAs
            cur = getattr(vm, "_cur_pfa", None)
            if cur:
                for cell in cur:
                    mark_cell(cell)
            for pfa, _ip in list(getattr(vm, "_ip_stack", [])):
                for cell in pfa:
                    mark_cell(cell)
            # Data-space
            for cell in list(getattr(vm, "data", [])):
                mark_cell(cell)
            # Dictionary PFAs (compiled colon definitions)
            for w in vm.dict.all_words():
                if getattr(w, "code_class", None) is CodeClass.DOCOL and isinstance(w.pfa, list):
                    for cell in w.pfa:
                        mark_cell(cell)
    
        # Also traverse all tables in object space as potential roots
        for toid in list(self._tables.keys()):
            mark_table(ObjRef(ObjKind.TABLE, toid))
    
        # Sweep
        swept_q = 0
        for qid, qrec in list(self._quots.items()):
            ref = int(qrec.get("ref", 0)) if qrec else 0
            if qid not in roots_quots and ref <= 0:
                self._quots.pop(qid, None)
                swept_q += 1
    
        swept_b = 0
        for bid in list(self._bufs.keys()):
            if bid not in roots_bufs:
                self._bufs.pop(bid, None)
                swept_b += 1
    
        return (swept_q, swept_b)

    # ---- Allocation ----
    # ---- Allocation ----
    def new_table(self) -> ObjRef:
        oid = self._next_oid; self._next_oid += 1
        self._tables[oid] = {}
        return ObjRef(ObjKind.TABLE, oid)

    def new_quot(self, code: List[int], env: Optional[Any] = None) -> ObjRef:
        oid = self._next_oid; self._next_oid += 1
        self._quots[oid] = {"code": list(code), "env": env, "ref": 0}
        return ObjRef(ObjKind.QUOT, oid)

    # ---- Helpers ----
    def is_alive(self, ref: ObjRef) -> bool:
        if ref.kind is ObjKind.TABLE: return ref.oid in self._tables
        if ref.kind is ObjKind.QUOT:  return ref.oid in self._quots
        if ref.kind is ObjKind.BUF:   return ref.oid in self._bufs
        return False

    def quot_code(self, qref: ObjRef) -> List[int]:
        try:
            return self._quots[qref.oid]["code"]
        except KeyError:
            raise RuntimeError(f"Unknown quotation oid={qref.oid}")
    def inc_ref(self, val: TableVal) -> None:
        if isinstance(val, ObjRef) and val.kind is ObjKind.QUOT and val.oid in self._quots:
            self._quots[val.oid]["ref"] += 1

    def dec_ref(self, val: TableVal) -> None:
        if isinstance(val, ObjRef) and val.kind is ObjKind.QUOT and val.oid in self._quots:
            st = self._quots[val.oid]
            st["ref"] -= 1
            # keep at ref<=0; gc() will decide if unreachable

    # ---- Table ops ----
    def t_set(self, tref: ObjRef, key: TableKey, val: TableVal) -> None:
        assert isinstance(tref, ObjRef) and tref.kind is ObjKind.TABLE, "t_set: tbl must be TABLE ObjRef"
        tbl = self._tables[tref.oid]

        # normalize BODY key
        if isinstance(key, tuple) and key and key[0] == "BODY":
            # keep tuple form; VM may resolve to Word when needed
            pass
        else:
            # allow int/str/Word/ObjRef(TABLE)
            if isinstance(key, ObjRef):
                assert key.kind is ObjKind.TABLE, "t_set: key ObjRef must be TABLE"

        # value must be int/str/ObjRef(TABLE|QUOT)
        if isinstance(val, ObjRef):
            assert val.kind in (ObjKind.TABLE, ObjKind.QUOT, ObjKind.BUF), "t_set: unsupported ObjRef value kind"

        old = tbl.get(key, None)
        if isinstance(old, ObjRef):
            self.dec_ref(old)
        tbl[key] = val
        if isinstance(val, ObjRef):
            self.inc_ref(val)

    def t_get(self, tref: ObjRef, key: TableKey) -> Optional[TableVal]:
        assert isinstance(tref, ObjRef) and tref.kind is ObjKind.TABLE
        return self._tables.get(tref.oid, {}).get(key, None)

    def t_drop(self, tref: ObjRef) -> bool:
        assert isinstance(tref, ObjRef) and tref.kind is ObjKind.TABLE
        tbl = self._tables.pop(tref.oid, None)
        if tbl is None: return False
        # dec-ref of values
        for v in tbl.values():
            self.dec_ref(v)
        return True

    def t_dropkey(self, tref: ObjRef, key: TableKey) -> bool:
        tbl = self._tables.get(tref.oid)
        if not tbl: return False
        if key in tbl:
            self.dec_ref(tbl[key])
            del tbl[key]
            return True
        return False

    def t_keys(self, tref: ObjRef) -> List[TableKey]:
        assert isinstance(tref, ObjRef) and tref.kind is ObjKind.TABLE
        return list(self._tables.get(tref.oid, {}).keys())

    # ---- Persistence ----
    def to_dict(self) -> Dict[str, Any]:
        def enc_key(k: TableKey) -> Any:
            if isinstance(k, int): return {"k":"int","v":k}
            if isinstance(k, str): return {"k":"str","v":k}
            if isinstance(k, Word): return {"k":"word","v":k.token}
            if isinstance(k, ObjRef): return {"k":"obj","v":{"kind":k.kind.value,"oid":k.oid}}
            if isinstance(k, tuple) and k and k[0]=="BODY": return {"k":"body","v":int(k[1])}
            raise TypeError(f"unsupported key type: {type(k)}")

        def enc_val(v: TableVal) -> Any:
            if isinstance(v, int): return {"t":"int","v":v}
            if isinstance(v, str): return {"t":"str","v":v}
            if isinstance(v, ObjRef): return {"t":"obj","v":{"kind":v.kind.value,"oid":v.oid}}
            raise TypeError("unsupported value type")

        out = {"next_oid": self._next_oid, "tables": {}, "quots": {}}
        out["bufs"] = {}
        for oid, tbl in self._tables.items():
            out["tables"][str(oid)] = [ (enc_key(k), enc_val(v)) for k,v in tbl.items() ]
        for oid, q in self._quots.items():
            out["quots"][str(oid)] = {"code": list(q["code"]), "env": None, "ref": int(q["ref"])}
        for oid, buf in self._bufs.items():
            out["bufs"][str(oid)] = list(buf)
        return out
                
    def from_dict(self, d: Dict[str, Any], token_to_word: Callable[[int], "Word"]) -> None:
        self._next_oid = d.get("next_oid", 1)
        self._tables.clear(); self._quots.clear(); self._bufs.clear()
        # Rebuild tables (no refcount during fill)
        for oid_s, items in d.get("tables", {}).items():
            oid = int(oid_s)
            tbl = {}
            for krec, vrec in items:
                # decode key
                kt = krec["k"]
                if kt == "int":
                    key = int(krec["v"])
                elif kt == "str":
                    key = str(krec["v"])
                elif kt == "word":
                    key = token_to_word(int(krec["v"]))
                elif kt == "obj":
                    key = ObjRef(ObjKind(krec["v"]["kind"]), int(krec["v"]["oid"]))
                elif kt == "body":
                    key = ("BODY", int(krec["v"]))
                else:
                    raise ValueError("bad key record")
                # decode val
                vt = vrec["t"]
                if vt == "int":
                    val = int(vrec["v"])
                elif vt == "str":
                    val = str(vrec["v"])
                elif vt == "obj":
                    val = ObjRef(ObjKind(vrec["v"]["kind"]), int(vrec["v"]["oid"]))
                else:
                    raise ValueError("bad val record")
                tbl[key] = val
            self._tables[oid] = tbl
        # Rebuild quotations
        for oid_s, qrec in d.get("quots", {}).items():
            oid = int(oid_s)
            def _dec_code_cell(x):
                if isinstance(x, dict) and x.get("__objref__"):
                    return ObjRef(ObjKind(x["kind"]), int(x["oid"]))
                return x
            self._quots[oid] = {
                "code": [_dec_code_cell(t) for t in qrec.get("code", [])],
                "env": None,
                "ref": int(qrec.get("ref", 0))
            }
        # Rebuild refcounts from tables
        for tbl in self._tables.values():
            for v in tbl.values():
                self.inc_ref(v)
        # Rebuild buffers
        for oid_s, blist in d.get("bufs", {}).items():
            oid = int(oid_s)
            self._bufs[oid] = bytearray(int(b) & 0xFF for b in blist)
    # ---------- Conversion TABLE <-> dict pour la messagerie ----------

    # ---------- Conversion TABLE <-> dict pour la messagerie ----------

    def table_to_py(self, ref: ObjRef) -> dict:
        """
        Convertit une TABLE Forth (ObjRef) en dict Python simple.

        Conventions pour les messages ForthOS :
        - clés = str ou int
        - valeurs = str / int / float / bool / None / bytearray
          ou TABLE imbriquées (converties récursivement en dict).
        """
        if ref.kind is not ObjKind.TABLE:
            raise TypeError(f"table_to_py attend une TABLE, pas {ref.kind}")

        tbl = self._tables[ref.oid]
        out: dict = {}
        for k, v in tbl.items():
            # Clés autorisées : str ou int
            if not isinstance(k, (str, int)):
                raise TypeError("Les messages TABLE doivent avoir des clés str ou int")
            out[k] = self._value_to_py(v)
        return out

    def _value_to_py(self, v):
        """
        Conversion d'une valeur TABLE -> valeur Python pour les messages.

        - ObjRef(TABLE) -> dict récursif
        - ObjRef(BUF)   -> bytearray (snapshot du contenu)
        - scalaires Python simples (str, int, float, bool, None, bytearray) -> inchangés
        """
        # v peut être un python "nu" (int/str/bool/None/bytearray),
        # ou un ObjRef vers TABLE/BUF/QUOT/etc.
        if isinstance(v, ObjRef):
            if v.kind is ObjKind.TABLE:
                return self.table_to_py(v)
            if v.kind is ObjKind.BUF:
                # On exporte un snapshot du contenu sous forme de bytearray Python
                return bytearray(self.buf_data(v))
            # QUOT et autres restent interdits dans les messages
            raise TypeError(f"Type d'objet non supporté dans un message: {v.kind}")

        # types Python de base acceptés, y compris bytearray
        if isinstance(v, (str, int, float, bool, bytearray)) or v is None:
            return v

        raise TypeError(f"Type Python non supporté dans un message: {type(v)}")

    def py_to_table(self, obj) -> ObjRef:
        """
        Convertit un dict Python (éventuellement imbriqué) en TABLE Forth.

        Conventions :
        - clés = str ou int
        - valeurs = str / int / float / bool / None / bytearray
          ou dict imbriqués (-> TABLE imbriquées).
        """
        if not isinstance(obj, dict):
            raise TypeError(f"py_to_table attend un dict, pas {type(obj)}")

        ref = self.new_table()
        tbl = self._tables[ref.oid]
        for k, v in obj.items():
            # Clés autorisées : str ou int
            if not isinstance(k, (str, int)):
                raise TypeError("Les messages Python doivent avoir des clés str ou int")
            tbl[k] = self._py_to_value(v)
        return ref

    def _py_to_value(self, v):
        """
        Conversion d'une valeur Python -> valeur stockable dans TABLE pour les messages.

        - dict       -> TABLE imbriquée (py_to_table)
        - bytearray  -> BUF ObjRef (copie du contenu)
        - scalaires  -> stockés tels quels (str, int, float, bool, None)
        """
        if isinstance(v, dict):
            return self.py_to_table(v)

        if isinstance(v, bytearray):
            # On crée un nouveau BUF dans l’ObjectSpace et on copie le contenu
            bref = self.new_buf(len(v))
            data = self.buf_data(bref)
            data[:] = v
            return bref

        # (optionnel : si tu veux aussi accepter bytes, dé-commente ça)
        # if isinstance(v, (bytes, bytearray)):
        #     vv = bytearray(v)
        #     bref = self.new_buf(len(vv))
        #     self.buf_data(bref)[:] = vv
        #     return bref

        if isinstance(v, (str, int, float, bool)) or v is None:
            return v

        raise TypeError(f"Type Python non supporté dans un message: {type(v)}")
# ------------ Word / Dictionary ---------------

    def new_buf(self, size: int) -> ObjRef:
        if not isinstance(size, int):
            raise TypeError("new_buf: size must be int")
        if size < 0:
            raise ValueError("new_buf: negative size")
        oid = self._next_oid; self._next_oid += 1
        self._bufs[oid] = bytearray(size)
        return ObjRef(ObjKind.BUF, oid)

    def buf_data(self, bref: ObjRef) -> bytearray:
        assert isinstance(bref, ObjRef) and bref.kind is ObjKind.BUF
        return self._bufs[bref.oid]

    def buf_len(self, bref: ObjRef) -> int:
        return len(self.buf_data(bref))
class CodeClass(Enum):
    PRIMITIVE = "PRIMITIVE"
    DOCOL     = "DOCOL"
    DOVAR     = "DOVAR"
    DOCON     = "DOCON"
    DOUSER    = "DOUSER"
    DODOES    = "DODOES"

class WFlags(IntFlag):
    NONE         = 0
    IMMEDIATE    = 1 << 0
    COMPILE_ONLY = 1 << 1
    SMUDGE       = 1 << 2
    HIDDEN       = 1 << 3

Token = int

@dataclass
class Word:
    name: str
    code_class: CodeClass
    token: Token
    flags: WFlags = WFlags.NONE
    pfa: Any = None
    prim: Optional[Callable[[Any], None]] = None
    does_code: Optional[Callable[[Any], None]] = None
    doc: str = ""
    source_loc: Optional[str] = None

    def __hash__(self) -> int: return hash(self.token)
    def __eq__(self, other: object) -> bool: return isinstance(other, Word) and self.token == other.token
    def is_immediate(self) -> bool: return bool(self.flags & WFlags.IMMEDIATE)
    def is_compile_only(self) -> bool: return bool(self.flags & WFlags.COMPILE_ONLY)
    def xt(self) -> Token: return self.token
    def nt(self) -> str: return self.name
    def add_body_token(self, tok: int) -> None:
        if self.code_class is not CodeClass.DOCOL: raise TypeError(f"{self.name} not colon")
        self.pfa.append(tok)
    def set_does(self, does_code: Callable[[Any], None]) -> None:
        self.code_class = CodeClass.DODOES; self.does_code = does_code
    def execute(self, vm: Any) -> None:
        cc = self.code_class
        if cc is CodeClass.PRIMITIVE:
            if not self.prim: raise RuntimeError(f"Primitive {self.name} missing impl")
            self.prim(vm)
        elif cc is CodeClass.DOCOL: vm.enter_colon(self)
        elif cc is CodeClass.DOVAR: vm.push_body(self)
        elif cc is CodeClass.DOCON: vm.D.append(self.pfa)
        elif cc is CodeClass.DOUSER: vm.push_user_slot(self.pfa)
        elif cc is CodeClass.DODOES:
            vm.push_body(self); 
            if not self.does_code: raise RuntimeError(f"{self.name} DODOES missing impl")
            self.does_code(vm)
        else: raise RuntimeError("bad code class")
    def disasm(self, token_to_name: Callable[[Token], str]) -> str:
        if self.code_class is CodeClass.DOCOL:
            parts = []
            i = 0
            while i < len(self.pfa):
                cell = self.pfa[i]
                if isinstance(cell, int) and cell == LIT and i+1 < len(self.pfa):
                    val = self.pfa[i+1]
                    parts.append(str(val)); i += 2; continue
                if isinstance(cell, int):
                    parts.append(token_to_name(cell))
                else:
                    parts.append(repr(cell))
                i += 1
            body = " ".join(parts)
            return f": {self.name}  {body} ;"
        if self.code_class is CodeClass.PRIMITIVE: return f"primitive {self.name}"
        if self.code_class is CodeClass.DOVAR: return f"variable-like {self.name} (body={self.pfa!r})"
        if self.code_class is CodeClass.DOCON: return f"constant {self.name} (={self.pfa!r})"
        if self.code_class is CodeClass.DOUSER: return f"user {self.name} (slot {self.pfa})"
        if self.code_class is CodeClass.DODOES: return f"created {self.name} DOES> ..."
        return f"<word {self.name}>"

    # constructors
    @staticmethod
    def primitive(name: str, token: Token, prim: Callable[[Any], None], *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.PRIMITIVE, token, flags, None, prim, None, doc)
    @staticmethod
    def colon(name: str, token: Token, body_tokens: Optional[List[Token]] = None, *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.DOCOL, token, flags, list(body_tokens or []), None, None, doc)
    @staticmethod
    def variable(name: str, token: Token, initial: Any = 0, *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.DOVAR, token, flags, initial, None, None, doc)
    @staticmethod
    def constant(name: str, token: Token, value: Any, *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.DOCON, token, flags, value, None, None, doc)
    @staticmethod
    def user(name: str, token: Token, slot_index: int, *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.DOUSER, token, flags, slot_index, None, None, doc)
    @staticmethod
    def created(name: str, token: Token, data: Any = None, *, flags=WFlags.NONE, doc="") -> "Word":
        return Word(name, CodeClass.DOVAR, token, flags, data, None, None, doc)

    # persistence
    def to_dict(self) -> Dict[str, Any]:
        def enc(x: Any) -> Any:
            if isinstance(x, ObjRef): return {"__objref__": True, "kind": x.kind.value, "oid": x.oid}
            if isinstance(x, Word): return {"__wordtok__": True, "token": x.token}
            return x
        pfa = self.pfa
        if self.code_class is CodeClass.DOCOL and isinstance(pfa, list):
            ser_pfa = [ enc(x) for x in pfa ]
        else:
            ser_pfa = enc(pfa)
        return {"name":self.name,"code_class":self.code_class.value,"token":self.token,
                "flags":int(self.flags),"pfa":ser_pfa,"doc":self.doc,"source_loc":self.source_loc}
    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Word":
        def dec(x: Any) -> Any:
            if isinstance(x, dict) and x.get("__objref__"):
                return ObjRef(ObjKind(x["kind"]), int(x["oid"]))
            return x
        cc = CodeClass(d["code_class"])
        raw = d.get("pfa")
        pfa = [dec(x) for x in raw] if cc is CodeClass.DOCOL and isinstance(raw, list) else dec(raw)
        return Word(d["name"], cc, d["token"], WFlags(d.get("flags",0)), pfa, None, None, d.get("doc",""), d.get("source_loc"))

class DictError(RuntimeError): ...
class UnknownWordError(DictError): ...
class TokenSpaceError(DictError): ...

@dataclass
class WordEntry:
    wid: int
    word: Word

class WordsDictionary:
    def __init__(self, *, token_start: int = 256, reserve: Optional[Iterable[int]] = None):
        self._next_token: int = token_start
        self._by_token: Dict[int, WordEntry] = {}
        self._wordlists: Dict[int, List[Word]] = {}
        self._by_name_latest: Dict[str, WordEntry] = {}
        self._name_index: Dict[str, List[WordEntry]] = {}
        self._wid_counter: int = 0
        self._order: List[int] = []
        self._current: Optional[int] = None
        self._history: List[Tuple[int, int]] = []
        self._markers: List[int] = []
        root = self.new_wordlist()
        self.set_order([root]); self.set_current(root)
        if reserve:
            for t in reserve:
                if t in self._by_token: raise TokenSpaceError(f"Token reserved twice: {t}")
                placeholder = Word.primitive(f"__reserved_{t}__", t, prim=lambda vm: None, flags=WFlags.HIDDEN)
                self._attach_word(placeholder, root, claim_token=t)

    def new_wordlist(self) -> int:
        wid = self._wid_counter; self._wid_counter += 1
        self._wordlists[wid] = []; return wid
    def set_current(self, wid: int) -> None:
        if wid not in self._wordlists: raise DictError(f"unknown WID {wid}")
        self._current = wid
    def get_current(self) -> int:
        if self._current is None: raise DictError("no current wordlist")
        return self._current
    def set_order(self, order: List[int]) -> None:
        if not order: raise DictError("empty search order")
        for wid in order:
            if wid not in self._wordlists: raise DictError(f"unknown WID {wid}")
        self._order = list(order)
    def get_order(self) -> List[int]: return list(self._order)

    def _alloc_token(self) -> int:
        t = self._next_token
        while t in self._by_token: t += 1
        self._next_token = t + 1
        return t

    def _attach_word(self, w: Word, wid: int, *, claim_token: Optional[int] = None) -> None:
        if wid not in self._wordlists: raise DictError(f"unknown WID {wid}")
        tok = w.token if claim_token is None else claim_token
        if tok in self._by_token: raise TokenSpaceError(f"token in use: {tok}")
        self._by_token[tok] = WordEntry(wid=wid, word=w)
        self._wordlists[wid].append(w)
        stack = self._name_index.setdefault(w.name, [])
        stack.append(WordEntry(wid=wid, word=w))
        self._by_name_latest[w.name] = stack[-1]
        self._history.append((wid, tok))

    # creators
    def add_primitive(self, name: str, prim, *, flags=WFlags.NONE, doc="") -> Word:
        tok = self._alloc_token(); w = Word.primitive(name, tok, prim, flags=flags, doc=doc)
        self._attach_word(w, self.get_current()); return w
    def add_colon(self, name: str, *, flags=WFlags.NONE, doc="") -> Word:
        tok = self._alloc_token(); w = Word.colon(name, tok, [], flags=flags, doc=doc)
        self._attach_word(w, self.get_current()); return w
    def add_variable(self, name: str, initial: Any=0, *, flags=WFlags.NONE, doc="") -> Word:
        tok = self._alloc_token(); w = Word.variable(name, tok, initial, flags=flags, doc=doc)
        self._attach_word(w, self.get_current()); return w
    def add_constant(self, name: str, value: Any, *, flags=WFlags.NONE, doc="") -> Word:
        tok = self._alloc_token(); w = Word.constant(name, tok, value, flags=flags, doc=doc)
        self._attach_word(w, self.get_current()); return w
    def add_created(self, name: str, data: Any=None, *, flags=WFlags.NONE, doc="") -> Word:
        tok = self._alloc_token(); w = Word.created(name, tok, data, flags=flags, doc=doc)
        self._attach_word(w, self.get_current()); return w

    # lookup
    def find(self, name: str) -> Optional[Word]:
        for wid in self._order:
            for w in reversed(self._wordlists[wid]):
                if w.name == name and not (w.flags & (WFlags.HIDDEN | WFlags.SMUDGE)):
                    return w
        return None
    def find_any(self, name: str) -> Optional[Word]:
        we = self._by_name_latest.get(name); return None if we is None else we.word
    def find_by_token(self, tok: int) -> Optional[Word]:
        we = self._by_token.get(tok); return None if we is None else we.word

    # visibility & markers
    def hide(self, name: str) -> None:
        w = self.find_any(name); 
        if not w: raise UnknownWordError(name)
        w.flags |= WFlags.HIDDEN
    def reveal(self, name: str) -> None:
        w = self.find_any(name); 
        if not w: raise UnknownWordError(name)
        w.flags &= ~WFlags.HIDDEN
    def smudge_start(self, name: str) -> Word:
        w = self.find_any(name); 
        if not w: raise UnknownWordError(name)
        w.flags |= WFlags.SMUDGE; return w
    def smudge_end(self) -> None:
        w = self.all_words()[-1]; w.flags &= ~WFlags.SMUDGE
    def push_marker(self) -> int:
        self._markers.append(len(self._history)); return len(self._markers)-1
    def forget_to_marker(self, marker_id: int) -> None:
        if not (0 <= marker_id < len(self._markers)): raise DictError("bad marker")
        cutoff = self._markers[marker_id]
        for _ in range(len(self._history)-1, cutoff-1, -1):
            wid, tok = self._history.pop()
            we = self._by_token.pop(tok, None)
            if not we: continue
            w = we.word
            if w in self._wordlists[wid]: self._wordlists[wid].remove(w)
            stack = self._name_index.get(w.name, [])
            for j in range(len(stack)-1, -1, -1):
                if stack[j].word is w: stack.pop(j); break
            if stack: self._by_name_latest[w.name] = stack[-1]
            else:
                self._name_index.pop(w.name, None); self._by_name_latest.pop(w.name, None)
        self._markers = self._markers[:marker_id]

    # persistence
    def to_dict(self) -> Dict[str, Any]:
        return {"wordlists":{str(wid): [w.to_dict() for w in wl] for wid,wl in self._wordlists.items()},
                "order": list(self._order), "current": self._current, "next_token": self._next_token}
    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WordsDictionary":
        wd = WordsDictionary(token_start=d.get("next_token",256))
        wd._by_token.clear(); wd._wordlists.clear(); wd._by_name_latest.clear()
        wd._name_index.clear(); wd._history.clear(); wd._markers.clear(); wd._wid_counter=0
        for wid_s, wlist in d["wordlists"].items():
            wid = int(wid_s); wd._wordlists[wid]=[]; wd._wid_counter=max(wd._wid_counter,wid+1)
            for wdict in wlist:
                w = Word.from_dict(wdict)
                wd._attach_word(w, wid)
        wd._order = list(d["order"]); wd._current = d["current"]
        return wd
    # introspection
    def token_to_name(self, tok: int) -> str:
        if tok == LIT: return 'LIT'
        if tok == BRANCH: return 'BRANCH'
        if tok == ZBRANCH: return '0BRANCH'
        if tok == EXIT: return 'EXIT'
        if tok == EXECUTE: return 'EXECUTE'
        w = self.find_by_token(tok); return "<unk>" if w is None else w.name
    def all_words(self) -> List[Word]:
        out: List[Word] = []
        for wid in self._wordlists: out.extend(self._wordlists[wid])
        return out
# ------------------------------ VM Core --------------------------------------

# Reserve low tokens
LIT      = 0x10
BRANCH   = 0x11
ZBRANCH  = 0x12
EXIT     = 0x13
EXECUTE  = 0x14

# REPL dot-commands (single source of truth)
DOT_CMDS = {".bench",".bye",".dict",".dumpdata",".gc",".help",".history",".load",".objects",".rstack",".save",".see",".stack",".tag",".ver",".word"}


# ---- Small helpers (Niveau 1) ----
def is_body_handle(x) -> bool:
    return isinstance(x, tuple) and len(x) == 2 and x[0] == "BODY"

def as_word(vm, x):
    if isinstance(x, int):
        return vm.dict.find_by_token(x)
    if isinstance(x, Word):
        return x
    return None

def is_table(x) -> bool:
    return isinstance(x, ObjRef) and x.kind is ObjKind.TABLE

def assert_table(x, msg: str = "expected TABLE ObjRef"):
    assert is_table(x), msg


def is_buf(x) -> bool:
    return isinstance(x, ObjRef) and x.kind is ObjKind.BUF


def expect_buf(x, opname: str):
    if not is_buf(x):
        raise RuntimeError(f"{opname} expects BUF")



class VM:
 

    def __init__(self):
        self.D: List[Any] = []
        self.R: List[Any] = []
        self.objspace = ObjectSpace()
        # Sortie par défaut pour exécution REPL
        self.out = io.StringIO()

        # Data space (cells as Python objects), HERE is index
        self.data: List[Any] = []
        self.HERE: int = 0

        # version & persistence
        self.version_tag: Optional[str] = None

        # dictionary with reserved tokens
        self.dict = WordsDictionary(token_start=256, reserve=[LIT,BRANCH,ZBRANCH,EXIT,EXECUTE])

        # runtime
        self._ip_stack: List[Tuple[List[Any], int]] = []  # (pfa, ip_index)
        self._cur_pfa: Optional[List[Any]] = None
        self._ip: int = 0

        # install primitives & core words
        self._install_core()

        
        
        # compiler state
        self.compiling: bool = False
        self.current_def: Optional[Word] = None
        self.ctrl_stack: List[Tuple[str, int]] = []  # (kind, pfa_index)

        # quotation capture state for "[: ... ;]"
        self._quot_capturing: bool = False
        self._quot_buffer: List[int] = []

        # table literal state for "{: ... ;}"
        self._tbl_lit_stack: List[dict] = []   # items: {"tref": ObjRef, "pending_key": Optional[TableKey]}


        # --- Concurrency / scheduler integration ---
        # Filled by Scheduler.register_vm; used by concurrency primitives.
        self.pid = None            # type: Optional[int]
        self.alive = True          # type: bool
        self.priority = 10         # type: int  (0..255; 0 = paused)
        self.exit_reason = None    # type: Optional[str]
        # stepping flag (scheduler uses step_one to run exactly one cell)
        self._stepping = False

    # --- DOES> pending linkage ---
        self._pending_does_target = None
        self._pending_does_impl = None
  
   # --- Table literal context helpers ---
    def _ctx(self) -> TableCtx:
        assert hasattr(self, "_table_ctx") and self._table_ctx is not None, "table-literal: no context"
        return self._table_ctx

    def _ctx_require_key(self) -> None:
        if self._table_ctx.pending_key is None:
            raise RuntimeError("POP sans clé en cours dans {: ... ;}")

    def _ctx_attach(self, value: TableVal) -> None:
        tbl = self._table_ctx.tref
        key = self._table_ctx.pending_key
        self.objspace.t_set(tbl, key, value)
        self._table_ctx.pending_key = None
        
    # --- token execution loop ---
    def enter_colon(self, w: Word) -> None:
        if self._cur_pfa is not None:
            self._ip_stack.append((self._cur_pfa, self._ip))
        self._cur_pfa = w.pfa
        self._ip = 0
        # In normal (non-stepping) mode, run to completion
        if not getattr(self, '_stepping', False):
            self._run_threaded()
            
    def _run_threaded(self) -> None:
        """Main threaded execution loop.

        This function pulls cells from the current pfa and delegates either to
        the control-token handler (LIT, BRANCH, ZBRANCH, EXIT, EXECUTE) or to
        the generic word-token handler. Behavior is intentionally identical to
        the previous inlined implementation, just structured for clarity and
        easier extension/debugging.

        NOTE: If the instance has an ``alive`` attribute (like ForthOSVM),
        this loop will cooperate with it and stop when ``alive`` becomes False.
        This lets the host interrupt infinite loops via ForthOSVM.stop().
        """
        while self._cur_pfa is not None and self._ip < len(self._cur_pfa):
            # Coopération avec ForthOSVM.stop() / host.kill_vm(pid)
            if hasattr(self, "alive") and not getattr(self, "alive"):
                break

            cell = self._cur_pfa[self._ip]
            self._ip += 1
            if isinstance(cell, int) and cell in (LIT, BRANCH, ZBRANCH, EXIT, EXECUTE):
                self._execute_control_token(cell)
            else:
                self._execute_word_token(cell)


    def _execute_control_token(self, cell: int) -> None:
        """Execute a single control token (LIT/BRANCH/ZBRANCH/EXIT/EXECUTE)."""
        if cell == LIT:
            # LIT: next cell is pushed as a literal
            self.D.append(self._cur_pfa[self._ip])
            self._ip += 1
            return

        if cell == BRANCH:
            # BRANCH: unconditional jump to next cell (target index)
            target = self._cur_pfa[self._ip]
            self._ip = target
            return

        if cell == ZBRANCH:
            # ZBRANCH: jump if flag on stack is zero; ip already advanced to target
            target = self._cur_pfa[self._ip]
            self._ip += 1
            flag = self.D.pop()
            if flag == 0:
                self._ip = target
            return

        if cell == EXIT:
            # EXIT: return to previous pfa/ip, or stop if stack is empty
            if self._ip_stack:
                self._cur_pfa, self._ip = self._ip_stack.pop()
            else:
                self._cur_pfa = None
                self._ip = 0
            return

        if cell == EXECUTE:
            # EXECUTE: pop execution token and run the associated word
            xt = self.D.pop()
            w = self.dict.find_by_token(xt)
            if not w:
                raise RuntimeError("EXECUTE: unknown xt")
            w.execute(self)
            return

        # Should never happen if caller only passes known control tokens
        raise RuntimeError(f"Unknown control token {cell}")

    
    def _execute_word_token(self, cell) -> None:
        """Resolve and execute a non-control cell as a word token.
        This covers normal threaded code: each cell is expected to be an
        integer token referencing a dictionary entry.
        """
        if not isinstance(cell, int):
            raise RuntimeError(f"Bad cell in pfa: {cell!r}")
        w = self.dict.find_by_token(cell)
        if not w:
            raise RuntimeError(f"Unknown token {cell}")
        w.execute(self)
    def step_one(self) -> bool:
        """Execute exactly one threaded instruction (one cell).
        Returns True if more work *may* remain in the current pfa, False otherwise.
        Used by the external Scheduler for round-robin stepping.
        """
        if not getattr(self, "alive", True):
            return False
        if self._cur_pfa is None or self._ip >= len(self._cur_pfa):
            return False
        # Single-instruction threaded interpreter
        cell = self._cur_pfa[self._ip]
        self._ip += 1
        if isinstance(cell, int) and cell in (LIT, BRANCH, ZBRANCH, EXIT, EXECUTE):
            self._execute_control_token(cell)
        else:
            self._execute_word_token(cell)
        # Continue if there is still code to run and we're alive
        return bool(getattr(self, "alive", True) and self._cur_pfa is not None and self._ip < len(self._cur_pfa))

    def call_quotation(self, qref: ObjRef) -> None:
            if not (isinstance(qref, ObjRef) and qref.kind is ObjKind.QUOT):
                raise RuntimeError("CALL: expects QUOT")
            code = self.objspace.quot_code(qref)
            if self._cur_pfa is not None:
                self._ip_stack.append((self._cur_pfa, self._ip))
            self._cur_pfa = code
            self._ip = 0

    def push_body(self, w: Word) -> None:
            self.D.append(("BODY", w.token))

    def push_user_slot(self, idx: int) -> None:
            self.D.append(("USER", idx))

        # --- Core primitives & compile-time words ---
    def _install_core(self) -> None:
            W = self.dict
            def addp(name, prim, *, flags=WFlags.NONE, doc=""):
                w = W.find_any(name)
                if w is not None:
                    w.code_class = CodeClass.PRIMITIVE
                    w.prim = prim
                    w.flags = flags
                    if doc:
                        w.doc = doc
                    return w
                return getattr(W, 'add_primitive')(name, prim, flags=flags, doc=doc)

            # --- Concurrency / Scheduler words ---
    
            # SELF-PID ( -- pid )
            addp("SELF-PID", lambda vm: vm.D.append(int(vm.pid) if vm.pid is not None else -1),
                 doc="( -- pid ) retourne le pid courant")

            # BYE-VM ( -- ) — arrêt coopératif de SELF
            def prim_BYE_VM(vm):
                vm.alive = False
                vm.exit_reason = "normal"
            addp("BYE-VM", prim_BYE_VM, doc="( -- ) termine la VM courante")


            
                      # --- Temps / temporisation ----------------------------------
            # SLEEP ( ms -- ) : dort pendant ms millisecondes
            def prim_SLEEP(vm):
                ms = vm.D.pop()
                # Accepter int/float, mais pas des trucs exotiques
                if not isinstance(ms, (int, float)):
                    raise RuntimeError("SLEEP expects number of milliseconds")
                if ms < 0:
                    ms = 0
                # ms -> secondes
                time.sleep(ms / 1000.0)

            addp("SLEEP", prim_SLEEP, doc="( ms -- ) sleep for ms milliseconds")

            # TIME ( -- epoch ) : pousse l'époque Unix en secondes (entier)
            def prim_TIME(vm):
                vm.D.append(int(time.time()))

            addp("TIME", prim_TIME, doc="( -- epoch ) push current Unix time (seconds)")

        

            # Special tokens
            addp("LIT", lambda vm: (_ for _ in ()).throw(RuntimeError("LIT is VM-only")), flags=WFlags.HIDDEN)
            addp("BRANCH", lambda vm: (_ for _ in ()).throw(RuntimeError("BRANCH is VM-only")), flags=WFlags.HIDDEN)
            addp("0BRANCH", lambda vm: (_ for _ in ()).throw(RuntimeError("0BRANCH is VM-only")), flags=WFlags.HIDDEN)
            addp("EXIT", lambda vm: (_ for _ in ()).throw(RuntimeError("EXIT is VM-only")), flags=WFlags.HIDDEN)
            addp("EXECUTE", lambda vm: (_ for _ in ()).throw(RuntimeError("EXECUTE is VM-only")), flags=WFlags.HIDDEN)

            # Stack basics
            addp("DROP", lambda vm: vm.D.pop(), doc="( x -- )")
            addp("DUP",  lambda vm: vm.D.append(vm.D[-1]), doc="( x -- x x )")
            def prim_SWAP(vm): a=vm.D.pop(); b=vm.D.pop(); vm.D.append(a); vm.D.append(b)
            addp("SWAP", prim_SWAP, doc="( a b -- b a )")
            def prim_OVER(vm): a=vm.D[-2]; vm.D.append(a)
            addp("OVER", prim_OVER, doc="( a b -- a b a )")
            def prim_ROT(vm): a=vm.D.pop(); b=vm.D.pop(); c=vm.D.pop(); vm.D.extend([b,a,c])
            addp("ROT", prim_ROT, doc="( a b c -- b c a )")
            addp("?DUP", lambda vm: (vm.D and vm.D[-1]!=0 and vm.D.append(vm.D[-1])) or None, doc="( x -- x x | 0 )")
            def prim_2DROP(vm): vm.D.pop(); vm.D.pop()
            def prim_2DUP(vm): a=vm.D[-2]; b=vm.D[-1]; vm.D.extend([a,b])
            addp("2DROP", prim_2DROP, doc="( a b -- )")
            addp("2DUP",  prim_2DUP,  doc="( a b -- a b a b )")
            def prim_NIP(vm):
                # ( x1 x2 -- x2 ) : supprime le second depuis le haut
                x2 = vm.D.pop()
                x1 = vm.D.pop()
                vm.D.append(x2)
            addp("NIP", prim_NIP, doc="( x1 x2 -- x2 ) drop second-from-top")
            # Arithmetic
            def prim_ADD(vm):
                a = vm.D.pop()
                b = vm.D.pop()
                vm.D.append(a + b)
            addp("+", prim_ADD, doc="( a b -- a+b )")
            addp("-", lambda vm: (lambda b,a: vm.D.append(a-b))(vm.D.pop(), vm.D.pop()), doc="( a b -- a-b )")
            addp("*", lambda vm: vm.D.append(vm.D.pop()*vm.D.pop()), doc="( a b -- a*b )")
            def prim_divmod(vm):
                b = vm.D.pop(); a = vm.D.pop()
                q, r = divmod(a, b)
                vm.D.extend([q, r])
            addp("/MOD", prim_divmod, doc="( a b -- q r )")

            # Logic / comparison (true = -1, false = 0)
            def tf(cond): return -1 if cond else 0
            addp("0=", lambda vm: vm.D.append(tf(vm.D.pop()==0)), doc="( x -- flag )")
            addp("0<", lambda vm: vm.D.append(tf(vm.D.pop()<0)),  doc="( x -- flag )")
            addp("=",  lambda vm: (lambda b,a: vm.D.append(tf(a==b)))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp("<>", lambda vm: (lambda b,a: vm.D.append(tf(a!=b)))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp("<",  lambda vm: (lambda b,a: vm.D.append(tf(a<b )))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp(">",  lambda vm: (lambda b,a: vm.D.append(tf(a>b )))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp("<=", lambda vm: (lambda b,a: vm.D.append(tf(a<=b)))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp(">=", lambda vm: (lambda b,a: vm.D.append(tf(a>=b)))(vm.D.pop(), vm.D.pop()), doc="( a b -- flag )")
            addp("AND", lambda vm: vm.D.append(vm.D.pop() & vm.D.pop()), doc="( a b -- a&b )")
            addp("OR",  lambda vm: vm.D.append(vm.D.pop() | vm.D.pop()), doc="( a b -- a|b )")
            addp("XOR", lambda vm: vm.D.append(vm.D.pop() ^ vm.D.pop()), doc="( a b -- a^b )")
            addp("INVERT", lambda vm: vm.D.append(~vm.D.pop()), doc="( x -- ~x )")
            addp("1+", lambda vm: vm.D.append(vm.D.pop()+1), doc="( n -- n+1 )")
            addp("1-", lambda vm: vm.D.append(vm.D.pop()-1), doc="( n -- n-1 )")
            addp("0>", lambda vm: vm.D.append(-1 if vm.D.pop()>0 else 0), doc="( n -- flag )")

            # Data space
            addp("HERE", lambda vm: vm.D.append(vm.HERE), doc="( -- addr )")
            def prim_ALLOT(vm):
                n = vm.D.pop()
                if n < 0: raise RuntimeError("ALLOT: negative")
                for _ in range(n): vm.data.append(0)
                vm.HERE += n
            addp("ALLOT", prim_ALLOT, doc="( n -- )")
            def prim_COMMA(vm): x = vm.D.pop(); vm.data.append(x); vm.HERE += 1
            addp(",", prim_COMMA, doc="( x -- )")
            def prim_FETCH(vm):
                addr = vm.D.pop()
                if isinstance(addr, tuple) and len(addr)==2 and addr[0]=="BODY":
                    w = vm.dict.find_by_token(addr[1])
                    if not w: raise RuntimeError("@ BODY: unknown word")
                    vm.D.append(w.pfa)
                else:
                    vm.D.append(vm.data[addr])
            def prim_STORE(vm):
                addr = vm.D.pop(); val = vm.D.pop()
                if isinstance(addr, tuple) and len(addr)==2 and addr[0]=="BODY":
                    w = vm.dict.find_by_token(addr[1])
                    if not w: raise RuntimeError("! BODY: unknown word")
                    w.pfa = val
                else:
                    vm.data[addr] = val
            def prim_PSTORE(vm):
                addr = vm.D.pop(); inc = vm.D.pop()
                if isinstance(addr, tuple) and len(addr)==2 and addr[0]=="BODY":
                    w = vm.dict.find_by_token(addr[1])
                    if not w: raise RuntimeError("+! BODY: unknown word")
                    if not isinstance(w.pfa, int):
                        raise RuntimeError("+!: BODY value not int")
                    w.pfa += inc
                else:
                    vm.data[addr] += inc
            addp("@",  prim_FETCH, doc="( addr -- x )")
            addp("!",  prim_STORE, doc="( x addr -- )")
            addp("+!", prim_PSTORE, doc="( x addr -- )")
            
                    # EVALUATE-FORTH ( line$ -- )
            # Évalue une ligne de code Forth dans la VM courante.
            # Toute la sortie passe par ForthOSVM.emit -> host.handle_stdout.
            def prim_EVALUATE_FORTH(vm: "ForthOSVM") -> None:
                line = vm.D.pop()
                if not isinstance(line, str):
                    raise TypeError("EVALUATE-FORTH attend ( line$ -- ) avec line$ = str")

                depth = getattr(vm, "_eval_depth", 0)
                vm._eval_depth = depth + 1
                try:
                    vm.interpret_line(line, out=vm.out)
                finally:
                    vm._eval_depth = depth


            addp(
                "EVALUATE-FORTH",
                prim_EVALUATE_FORTH,
                doc="( line$ -- ) évalue une ligne Forth dans la VM courante",
            )


            # Literals / tick / execute
            def prim_TICK(vm):
                name = vm.next_token_from_input()
                w = vm.dict.find(name)
                if not w: raise RuntimeError(f"Unknown word: {name}")
                vm.D.append(w.xt())
            addp("'", prim_TICK, doc="( -- xt ) parse next token; push xt)")
        
                    # --- Dictionary maintenance ---
            def prim_FORGET_XT(vm):
                """( xt -- ) Forget the word with execution token xt and everything defined after it."""
                xt = vm.D.pop()
                if not isinstance(xt, int):
                    raise RuntimeError("FORGET-XT expects xt")
                W = vm.dict
                # find the first time this token was attached in history
                idx = None
                for i, (_wid, tok) in enumerate(W._history):
                    if tok == xt:
                        idx = i
                        break
                if idx is None:
                    # unknown xt: behave like standard FORGET on unknown -> error
                    raise RuntimeError("FORGET-XT: unknown xt")
                # Remove all entries from the end down to idx (inclusive)
                for _ in range(len(W._history) - 1, idx - 1, -1):
                    wid, tok = W._history.pop()
                    we = W._by_token.pop(tok, None)
                    if not we:
                        continue
                    w = we.word

                    # purge QUOT referenced in this word's PFA (immediate removal from heap)
                    try:
                        pfa = getattr(w, "pfa", None)
                        if isinstance(pfa, list):
                            for cell in pfa:
                                if isinstance(cell, ObjRef) and cell.kind is ObjKind.QUOT:
                                    vm.objspace._quots.pop(cell.oid, None)
                    except Exception:
                        pass
                    # remove from wordlist
                    if w in W._wordlists[wid]:
                        W._wordlists[wid].remove(w)
                    # remove from name stacks
                    stack = W._name_index.get(w.name, [])
                    for j in range(len(stack) - 1, -1, -1):
                        if stack[j].word is w:
                            stack.pop(j)
                            break
                    if stack:
                        W._by_name_latest[w.name] = stack[-1]
                    else:
                        W._name_index.pop(w.name, None)
                        W._by_name_latest.pop(w.name, None)
                # also drop any markers that pointed beyond the new end
                W._markers = [m for m in W._markers if m <= len(W._history)]
            addp("FORGET-XT", prim_FORGET_XT, doc="( xt -- ) forget word and later words")

            def prim_FORGET_NAME(vm):
                """( <name> -- ) Parse next token as name and forget that word and everything defined after it."""
                name = vm.next_token_from_input()
                if not name:
                    raise RuntimeError("FORGET-NAME needs a name")
                w = vm.dict.find_any(name)
                if not w:
                    raise RuntimeError(f"FORGET-NAME: unknown {name}")
                xt = w.xt()
                # Delegate to the same logic as FORGET-XT:
                W = vm.dict
                idx = None
                for i, (_wid, tok) in enumerate(W._history):
                    if tok == xt:
                        idx = i
                        break
                if idx is None:
                    raise RuntimeError("FORGET-NAME: internal error (xt not in history)")
                for _ in range(len(W._history) - 1, idx - 1, -1):
                    wid, tok = W._history.pop()
                    we = W._by_token.pop(tok, None)
                    if not we:
                        continue
                    ww = we.word
                    if ww in W._wordlists[wid]:
                        W._wordlists[wid].remove(ww)
                    stack = W._name_index.get(ww.name, [])
                    for j in range(len(stack) - 1, -1, -1):
                        if stack[j].word is ww:
                            stack.pop(j)
                            break
                    if stack:
                        W._by_name_latest[ww.name] = stack[-1]
                    else:
                        W._name_index.pop(ww.name, None)
                        W._by_name_latest.pop(ww.name, None)
                W._markers = [m for m in W._markers if m <= len(W._history)]
            addp("FORGET-NAME", prim_FORGET_NAME, doc="( <name> -- ) forget word by name (and later words)")

            # --- GC trigger (tables/quotations heap) ---
            def prim_RUNGC(vm):
                vm.objspace.sweep_unreached(vm=vm)
                vm.objspace.sweep_unreached(vm=vm)
            addp("RUNGC", prim_RUNGC, doc="( -- ) sweep unreachable quotations")

            def prim_EXECUTE(vm):
                xt = vm.D.pop()
                w = vm.dict.find_by_token(xt)
                if not w: raise RuntimeError("EXECUTE: unknown xt")
                w.execute(vm)
                if not getattr(vm, "_stepping", False) and not getattr(vm, "compiling", False):
                    vm._run_threaded()
            addp("EXECUTE", prim_EXECUTE, doc="( xt -- ) execute token")

            # Output & debug
            def prim_DOT(vm):
                x = vm.D.pop()
                vm.emit(f"{x}")
            addp(".", prim_DOT, doc="( x -- ) print")
            addp("BL", lambda vm: vm.emit(" "), doc="( -- ) print one space")
            addp("CR", lambda vm: vm.emit("\n"), doc="( -- ) Carriage return")
            addp(".S", lambda vm: vm.emit(f"<{len(vm.D)}> "+" ".join(map(str,vm.D))+" \n"), doc="( -- )")
            addp(".R", lambda vm: vm.emit(f"<{len(vm.R)}> "+" ".join(map(str,vm.R))+" \n"), doc="( -- )")

            # EXIT primitive (compile-time emission or interpret fallback)
            def prim_EXIT(vm):
                if vm.compiling:
                    vm.emit_token(EXIT)
                else:
                    if vm._cur_pfa is not None:
                        if vm._ip_stack:
                            vm._cur_pfa, vm._ip = vm._ip_stack.pop()
                        else:
                            vm._cur_pfa = None; vm._ip = 0
            addp("EXIT", prim_EXIT, doc="( -- ) return")

            # Definition / compiler control
            def prim_COLON(vm):
                name = vm.next_token_from_input()
                if not name: raise RuntimeError(": needs name")
                w = vm.dict.add_colon(name, doc=f": {name} ... ;")
                vm.dict.smudge_start(name)
                vm.current_def = w
                vm.compiling = True
            addp(":", prim_COLON, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY, doc="start colon")

            def prim_SEMI(vm):
                if not vm.compiling or vm.current_def is None:
                    raise RuntimeError("; outside colon")
                finished = vm.current_def
                vm.emit_token(EXIT)
                vm.dict.smudge_end()
                vm.current_def = None; vm.compiling = False
                # Handle DOES> linkage
                if getattr(vm, "_pending_does_impl", None) is finished and getattr(vm, "_pending_does_target", None) is not None:
                    target = vm._pending_does_target
                    impl = vm._pending_does_impl
                    def _does_run(vmm, w=impl):
                        vmm.enter_colon(w)
                    target.set_does(_does_run)
                    vm._pending_does_target = None
                    vm._pending_does_impl = None
            addp(";", prim_SEMI, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY, doc="end colon")

            addp("[", lambda vm: setattr(vm, "compiling", False), flags=WFlags.IMMEDIATE, doc="interpret state")
            addp("]", lambda vm: setattr(vm, "compiling", True),  flags=WFlags.IMMEDIATE, doc="compile state")
            addp("IMMEDIATE", lambda vm: vm.dict.smudge_end(), flags=WFlags.IMMEDIATE, doc="mark latest immediate")


            def prim_POSTPONE(vm):
                name = vm.next_token_from_input()
                w = vm.dict.find(name)
                if not w: raise RuntimeError(f"POSTPONE: unknown {name}")
                if not vm.compiling:
                    raise RuntimeError("POSTPONE outside compilation")
                vm.emit_token(w.xt())
            addp("POSTPONE", prim_POSTPONE, flags=WFlags.IMMEDIATE, doc="( <name> -- ) compile execution of <name>")

            # Control structures
            def prim_IF(vm):
                if not vm.compiling: raise RuntimeError("IF compile-only")
                vm.emit_token(ZBRANCH); hole_index = vm.emit_placeholder()
                vm.ctrl_stack.append(("IF", hole_index))
            addp("IF", prim_IF, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            def prim_ELSE(vm):
                if not vm.compiling: raise RuntimeError("ELSE compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "IF": raise RuntimeError("ELSE without IF")
                _, if_hole = vm.ctrl_stack.pop()
                vm.patch_placeholder(if_hole, vm.here_ip())
                vm.emit_token(BRANCH); hole2 = vm.emit_placeholder()
                vm.ctrl_stack.append(("ELSE", hole2))
            addp("ELSE", prim_ELSE, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            def prim_THEN(vm):
                if not vm.compiling: raise RuntimeError("THEN compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] not in ("IF","ELSE"): raise RuntimeError("THEN without IF/ELSE")
                kind, hole = vm.ctrl_stack.pop(); vm.patch_placeholder(hole, vm.here_ip())
            addp("THEN", prim_THEN, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            def prim_BEGIN(vm):
                if not vm.compiling: raise RuntimeError("BEGIN compile-only")
                vm.ctrl_stack.append(("BEGIN", vm.here_ip()))
            addp("BEGIN", prim_BEGIN, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)
            
            # AGAIN ( -- )  compile-only
            # BEGIN ... AGAIN -> boucle infinie, BRANCH vers l'adresse de BEGIN
            def prim_AGAIN(vm):
                if not vm.compiling:
                    raise RuntimeError("AGAIN compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "BEGIN":
                    raise RuntimeError("AGAIN without BEGIN")
                _, begin_ip = vm.ctrl_stack.pop()
                vm.emit_token(BRANCH)
                vm.emit_value(begin_ip)
            addp("AGAIN", prim_AGAIN, flags=WFlags.IMMEDIATE | WFlags.COMPILE_ONLY)

            def prim_UNTIL(vm):
                if not vm.compiling: raise RuntimeError("UNTIL compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "BEGIN": raise RuntimeError("UNTIL without BEGIN")
                _, begin_ip = vm.ctrl_stack.pop()
                vm.emit_token(ZBRANCH); vm.emit_value(begin_ip)
            addp("UNTIL", prim_UNTIL, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            def prim_WHILE(vm):
                if not vm.compiling: raise RuntimeError("WHILE compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "BEGIN": raise RuntimeError("WHILE without BEGIN")
                _, begin_ip = vm.ctrl_stack.pop()
                vm.emit_token(ZBRANCH); hole = vm.emit_placeholder()
                vm.ctrl_stack.append(("WHILE", hole, begin_ip))
            addp("WHILE", prim_WHILE, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            def prim_REPEAT(vm):
                if not vm.compiling: raise RuntimeError("REPEAT compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "WHILE": raise RuntimeError("REPEAT without WHILE")
                _, hole, begin_ip = vm.ctrl_stack.pop()
                vm.emit_token(BRANCH); vm.emit_value(begin_ip)
                vm.patch_placeholder(hole, vm.here_ip())
            addp("REPEAT", prim_REPEAT, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)
            
                        # ----- CASE / OF / ENDOF / ENDCASE -----
            def _tok(vm, name):
                w = vm.dict.find_any(name)
                if not w:
                    raise RuntimeError(f"missing word {name}")
                return w.xt()

            def prim_CASE(vm):
                if not vm.compiling:
                    raise RuntimeError("CASE compile-only")
                # Marqueur avec la liste des sauts 'ENDOF' à patcher à ENDCASE
                vm.ctrl_stack.append(("CASE", []))

            def prim_OF(vm):
                if not vm.compiling:
                    raise RuntimeError("OF compile-only")
                # On a ... scrutinee candidate ... sur la pile à l'exécution
                # Compile: OVER = 0BRANCH <hole> DROP
                vm.emit_token(_tok(vm,"OVER"))
                vm.emit_token(_tok(vm,"="))
                vm.emit_token(ZBRANCH)
                hole = vm.emit_placeholder()
                vm.emit_token(_tok(vm,"DROP"))  # cas 'true' : supprimer la scrutinee
                # Empiler un marqueur OF pour ENDOF
                vm.ctrl_stack.append(("OF", hole))

            def prim_ENDOF(vm):
                if not vm.compiling:
                    raise RuntimeError("ENDOF compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "OF":
                    raise RuntimeError("ENDOF without OF")
                _, hole = vm.ctrl_stack.pop()

                # 1) Compiler d'abord l'échappement de clause (pris si match)
                vm.emit_token(BRANCH)
                endhole = vm.emit_placeholder()

                # 2) Accumuler 'endhole' sur le marqueur CASE courant
                for i in range(len(vm.ctrl_stack) - 1, -1, -1):
                    kind, payload = vm.ctrl_stack[i]
                    if kind == "CASE":
                        payload.append(endhole)
                        break
                else:
                    raise RuntimeError("ENDOF without CASE")

                # 3) Patcher la cible du 0BRANCH (cas false) VERS ICI,
                #    c.-à-d. après le BRANCH (début de la clause suivante / default)
                vm.patch_placeholder(hole, vm.here_ip())



            def prim_ENDCASE(vm):
                if not vm.compiling:
                    raise RuntimeError("ENDCASE compile-only")
                if not vm.ctrl_stack or vm.ctrl_stack[-1][0] != "CASE":
                    raise RuntimeError("ENDCASE without CASE")
                _, endholes = vm.ctrl_stack.pop()
                # Patch de tous les sauts de fin vers ici
                for h in endholes:
                    vm.patch_placeholder(h, vm.here_ip())
                # Nota: on NE drop PAS la scrutinee ici.
                # Les tests attendent que le 'default' fasse explicitement DROP.
                # (Si tu préfères la sémantique ANS, ajoute vm.emit_token(_tok(vm,"DROP")) ici.)
                
            addp("CASE",   prim_CASE,   flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY,
                 doc="( x -- x ) start a CASE dispatch (compile-only)")
            addp("OF",     prim_OF,     flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY,
                 doc="( x y -- ) match clause: OVER = IF DROP ... ENDOF (compile-only)")
            addp("ENDOF",  prim_ENDOF,  flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY,
                 doc="( -- ) end of a matching clause, jumps to ENDCASE (compile-only)")
            addp("ENDCASE",prim_ENDCASE,flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY,
                 doc="( x -- ? ) close CASE; patches all branches (compile-only)")

            # RECURSE (compile xt of current def)
            def prim_RECURSE(vm):
                if not vm.compiling or not vm.current_def: raise RuntimeError("RECURSE compile-only")
                vm.emit_token(vm.current_def.xt())
            addp("RECURSE", prim_RECURSE, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY)

            # Tables
            def prim_TABLE(vm): vm.D.append(vm.objspace.new_table())
        
            def prim_TSTORE(vm): 
                # ( val key table -- )
                val = vm.D.pop(); key = vm.D.pop(); tbl = vm.D.pop()
                assert_table(tbl, "T!: third item must be TABLE")
                if isinstance(key, ObjRef) and key.kind is not ObjKind.TABLE:
                    raise RuntimeError("T!: ObjRef key must be TABLE")
                vm.objspace.t_set(tbl, key, val)
            def prim_TFETCH(vm): 
                # ( table key -- val flag )
                key = vm.D.pop(); tbl = vm.D.pop()
                assert isinstance(tbl, ObjRef) and tbl.kind is ObjKind.TABLE
                v = vm.objspace.t_get(tbl, key)
                if v is None: vm.D.extend([0,0])
                else: vm.D.extend([v,-1])
            def prim_TKEYS(vm): 
                tbl = vm.D.pop(); 
                assert isinstance(tbl, ObjRef) and tbl.kind is ObjKind.TABLE
                ks = vm.objspace.t_keys(tbl)
                for k in ks: vm.D.append(k)
                vm.D.append(len(ks))
            def prim_TDROP(vm):
                # ( table -- )
                if not vm.D:
                    raise RuntimeError("TDROP expects TABLE on stack")
                tbl = vm.D.pop()
                assert isinstance(tbl, ObjRef) and tbl.kind is ObjKind.TABLE, "TDROP expects TABLE"
                vm.objspace.t_drop(tbl)
            def prim_TDROPKEY(vm): 
                # ( table key -- flag )
                key = vm.D.pop(); tbl = vm.D.pop()
                assert isinstance(tbl, ObjRef) and tbl.kind is ObjKind.TABLE
                vm.D.append(-1 if vm.objspace.t_dropkey(tbl, key) else 0)
            addp("TABLE",   prim_TABLE,   doc="( -- table )")
            addp("T!",      prim_TSTORE,  doc="( table key val -- )")
            addp("T@",      prim_TFETCH,  doc="( table key -- val flag )")
            addp("TKEYS",   prim_TKEYS,   doc="( table -- k1 .. kn n )")
            addp("TDROP",   prim_TDROP,   doc="( table -- )")
            addp("TDROPKEY",prim_TDROPKEY,doc="( table key -- flag )")

            # Quotations
            def prim_LBRACK_COLON(vm):
                # Start capturing tokens into a temporary quotation body
                if vm._quot_capturing:
                    raise RuntimeError("nested [: not supported")
                vm._quot_capturing = True
                vm._quot_buffer = []
            addp("[:", prim_LBRACK_COLON, flags=WFlags.IMMEDIATE, doc="begin quotation ( -- q ) ... ;]")

            def prim_SEMI_BRACK(vm):
                # Finish quotation: create QUOT obj from collected buffer, push it (or compile LIT q)
                if not vm._quot_capturing:
                    raise RuntimeError(";] without [:")
                vm._quot_capturing = False
                code = list(vm._quot_buffer)
                qref = vm.objspace.new_quot(code)
                if vm.compiling:
                    vm.emit_token(LIT); vm.emit_value(qref)
                else:
                    vm.D.append(qref)
                vm._quot_buffer = []
            addp(";]", prim_SEMI_BRACK, flags=WFlags.IMMEDIATE, doc="end quotation")

            def prim_CALL(vm):
                q = vm.D.pop()
                if not (isinstance(q, ObjRef) and q.kind is ObjKind.QUOT):
                    raise RuntimeError("CALL expects QUOT")
                vm.call_quotation(q)
                if not getattr(vm, "_stepping", False) and not getattr(vm, "compiling", False):
                    vm._run_threaded()
            addp("CALL", prim_CALL, doc="( q -- ) call quotation")


            # CREATE / DOES> / BODY ops
            def prim_CREATE(vm):
                name = vm.next_token_from_input()
                if not name: raise RuntimeError("CREATE needs name")
                w = vm.dict.add_variable(name, initial=0)
                vm._last_created = w
            addp("CREATE", prim_CREATE, flags=WFlags.IMMEDIATE)

            def prim_TO_BODY(vm):  # >BODY ( xt|Word -- bodyaddr )
                x = vm.D.pop()
                if isinstance(x, int):
                    w = vm.dict.find_by_token(x)
                elif isinstance(x, Word):
                    w = x
                else:
                    w = None
                if not w: raise RuntimeError(">BODY expects xt or Word")
                vm.D.append(("BODY", w.token))
            addp(">BODY", prim_TO_BODY)

            def prim_BODY_FETCH(vm):  # BODY@ ( addr -- x )
                prim_FETCH(vm)
            addp("BODY@", prim_BODY_FETCH)

            def prim_BODY_STORE(vm):  # BODY! ( x addr -- )
                body = vm.D.pop(); val = vm.D.pop()
                if not (isinstance(body, tuple) and len(body)==2 and body[0]=="BODY"):
                    raise RuntimeError("BODY!: expects body handle")
                tok = body[1]
                w = vm.dict.find_by_token(tok)
                if not w: raise RuntimeError("BODY!: unknown body")
                old = getattr(w, 'pfa', None)
                if isinstance(old, ObjRef): vm.objspace.dec_ref(old)
                w.pfa = val
                if isinstance(val, ObjRef): vm.objspace.inc_ref(val)
            addp("BODY!", prim_BODY_STORE)

            def prim_DOES(vm):
                if not hasattr(vm, "_last_created") or vm._last_created is None:
                    raise RuntimeError("DOES> without CREATE")
                # start hidden colon body for DOES>
                target = vm._last_created
                impl = vm.dict.add_colon(f"__DOES_{target.name}_{target.token}", flags=WFlags.HIDDEN, doc=f"DOES> for {target.name}")
                vm.dict.smudge_start(impl.name)
                vm.current_def = impl
                vm.compiling = True
                vm._pending_does_target = target
                vm._pending_does_impl = impl
            addp("DOES>", prim_DOES, flags=WFlags.IMMEDIATE|WFlags.COMPILE_ONLY, doc="attach runtime to last CREATE; body ends at ';'")

            # Defining words / misc (body and constants)
            def prim_CONSTANT(vm):
                val = vm.D.pop()
                name = vm.next_token_from_input()
                if not name: raise RuntimeError("CONSTANT needs name")
                import builtins
                # bump ref if ObjRef so constants hold a strong reference
                if isinstance(val, ObjRef): vm.objspace.inc_ref(val)
                vm.dict.add_constant(name, value=val)
            addp("CONSTANT", prim_CONSTANT, flags=WFlags.IMMEDIATE)

            def prim_VARIABLE(vm):
                name = vm.next_token_from_input()
                if not name: raise RuntimeError("VARIABLE needs name")
                vm.dict.add_variable(name, initial=0)
            addp("VARIABLE", prim_VARIABLE, flags=WFlags.IMMEDIATE)

            # Truth constants
            W.add_constant("TRUE",  -1, doc="boolean true")
            W.add_constant("FALSE",  0, doc="boolean false")
            
            #  primitives buffers / strings 

            def prim_BUF(vm):
                # ( n -- buf )  crée un buffer géré (ObjRef BUF) de n octets, init=0
                n = vm.D.pop()
                if not isinstance(n, int):
                    raise RuntimeError("BUF expects integer size")
                if n < 0:
                    raise RuntimeError("BUF negative size")  # les tests cherchent "negative"
                bref = vm.objspace.new_buf(n)
                vm.D.append(bref)

            addp("BUF", prim_BUF, doc="( n -- buf ) crée un buffer géré (ObjRef BUF)")

            def prim_BUF_FETCH(vm):
                # ( buf idx -- byte )
                idx = vm.D.pop()
                bref = vm.D.pop()
                if not (isinstance(bref, ObjRef) and bref.kind is ObjKind.BUF):
                    # attendu: "BUF@ ... BUF|expects BUF"
                    raise RuntimeError("BUF@ expects BUF then index")
                if not isinstance(idx, int):
                    raise RuntimeError("BUF@ expects integer index")
                data = vm.objspace.buf_data(bref)
                if idx < 0 or idx >= len(data):
                    # attendu: "index out of range"
                    raise RuntimeError("BUF@ index out of range")
                vm.D.append(int(data[idx]))

            addp("BUF@", prim_BUF_FETCH, doc="( buf idx -- byte ) lit un octet du buffer")

            def prim_BUF_STORE(vm):
                # ( byte buf idx -- )
                idx  = vm.D.pop()
                bref = vm.D.pop()
                byte = vm.D.pop()
                if not (isinstance(bref, ObjRef) and bref.kind is ObjKind.BUF):
                    # attendu: "BUF! ... BUF|expects BUF"
                    raise RuntimeError("BUF! expects BYTE BUF INDEX (BUF first)")
                if not isinstance(idx, int):
                    raise RuntimeError("BUF! expects integer index")
                if not isinstance(byte, int):
                    raise RuntimeError("BUF! expects integer byte")
                if byte < 0 or byte > 255:
                    raise RuntimeError("BUF! expects byte 0..255")
                data = vm.objspace.buf_data(bref)
                if idx < 0 or idx >= len(data):
                    raise RuntimeError("BUF! index out of range")
                data[idx] = byte & 0xFF

            addp("BUF!", prim_BUF_STORE, doc="( byte buf idx -- ) écrit un octet dans le buffer")

            def prim_BUF_LEN(vm):
                # ( buf -- n )
                bref = vm.D.pop()
                if not (isinstance(bref, ObjRef) and bref.kind is ObjKind.BUF):
                    # attendu: "BUF-LEN ... BUF|expects BUF"
                    raise RuntimeError("BUF-LEN expects BUF")
                vm.D.append(vm.objspace.buf_len(bref))

            addp("BUF-LEN", prim_BUF_LEN, doc="( buf -- n ) longueur du buffer")

            def prim_BUF_COPY(vm):
                # ( dst src -- )  copie min(len(dst), len(src))
                src = vm.D.pop()
                dst = vm.D.pop()
                if not (isinstance(dst, ObjRef) and dst.kind is ObjKind.BUF and
                        isinstance(src, ObjRef) and src.kind is ObjKind.BUF):
                    # attendu: "BUF-COPY ... buffers|expects"
                    raise RuntimeError("BUF-COPY expects two buffers (dst src)")
                ddata = vm.objspace.buf_data(dst)
                sdata = vm.objspace.buf_data(src)
                n = min(len(ddata), len(sdata))
                # copie via tampon immuable pour indépendance
                ddata[:n] = bytes(sdata[:n])

            addp("BUF-COPY", prim_BUF_COPY, doc="( dst src -- ) copie le contenu sur la longueur commune")

            def prim_STR_TO_BUF(vm):
                # ( str -- buf )  encode UTF-8 vers nouveau BUF
                s = vm.D.pop()
                if not isinstance(s, str):
                    raise RuntimeError("STR>BUF expects string")
                b = s.encode("utf-8")
                bref = vm.objspace.new_buf(len(b))
                vm.objspace.buf_data(bref)[:len(b)] = b
                vm.D.append(bref)

            addp("STR>BUF", prim_STR_TO_BUF, doc="( str -- buf ) encode UTF-8 vers buffer géré")

            def prim_BUF_TO_STR(vm):
                # ( buf -- str )  décode UTF-8 strict
                bref = vm.D.pop()
                if not (isinstance(bref, ObjRef) and bref.kind is ObjKind.BUF):
                    raise RuntimeError("BUF>STR expects BUF")
                try:
                    s = bytes(vm.objspace.buf_data(bref)).decode("utf-8")
                except UnicodeDecodeError:
                    raise RuntimeError("BUF>STR invalid UTF-8")
                vm.D.append(s)

            addp("BUF>STR", prim_BUF_TO_STR, doc="( buf -- str ) décode UTF-8 strict")


        # --- Compiler helpers ---
    def emit_literal_or_push(self, value: Any) -> None:
            if self.compiling:
                self.emit_token(LIT)
                self.emit_value(value)
            else:
                self.D.append(value)
    def emit_token(self, tok: int) -> None:
            if not self.current_def: raise RuntimeError("no current def")
            self.current_def.add_body_token(tok)
    def emit_value(self, value: Any) -> None:
            if not self.current_def: raise RuntimeError("no current def")
            self.current_def.pfa.append(value)
    def emit_placeholder(self) -> int:
            if not self.current_def: raise RuntimeError("no current def")
            self.current_def.pfa.append(None)
            return len(self.current_def.pfa) - 1
    def patch_placeholder(self, index: int, value: int) -> None:
            assert self.current_def and 0 <= index < len(self.current_def.pfa)
            self.current_def.pfa[index] = value
    def here_ip(self) -> int:
            assert self.current_def
            return len(self.current_def.pfa)

    # --- Tokenizer / interpreter ---
    def tokenize(self, line: str) -> List[str]:
            # Supports: numbers, words, strings "..." (no escaping), comments \ ... EOL and ( ... )
            # Also recognizes [: and ;] as individual tokens.
            out: List[str] = []
            i = 0; n = len(line)
            while i < n:
                c = line[i]
                if c.isspace():
                    i += 1; continue
                if c == '\\':
                    # Heuristique: si "\\WORD" et WORD existe dans le dictionnaire,
                    # on ignore le backslash (bug f-string), sinon c'est un commentaire jusqu'à EOL.
                    j = i + 1
                    j2 = j
                    while j2 < n and not line[j2].isspace():
                        j2 += 1
                    cand = line[j:j2]
                    from_word = (cand and self.dict.find(cand) is not None)
                    if from_word:
                        out.append(cand)
                        i = j2
                        continue
                    break
                if c == '(':
                    i += 1
                    while i < n and line[i] != ')': i += 1
                    i = min(i+1, n); continue
                if c == '"':
                    i += 1; start = i
                    while i < n and line[i] != '"': i += 1
                    s = line[start:i]
                    i = min(i+1, n)
                    out.append(f'"{s}"')
                    continue
                # special [: and ;] and also {: and ;}
                if c == "[" and i+1 < n and line[i+1] == ':':
                    out.append("[:"); i += 2; continue
                if c == ";" and i+1 < n and line[i+1] == ']':
                    out.append(";]"); i += 2; continue
                if c == "{" and i+1 < n and line[i+1] == ':':
                    out.append("{:"); i += 2; continue
                if c == ";" and i+1 < n and line[i+1] == '}':
                    out.append(";}"); i += 2; continue
                # token
                start = i
                while i < n and not line[i].isspace() and line[i] not in '()"':
                    # keep ;] and [: already handled above
                    if line[i] == '\\': break
                    i += 1
                out.append(line[start:i])
            return out

    def next_token_from_input(self) -> str:
            if not hasattr(self, "_pending_tokens") or not self._pending_tokens:
                raise RuntimeError("no token available")
            return self._pending_tokens.pop(0)


    # --- Token classification helpers ----------------------------------
    def _is_string_lit(self, tok: str) -> bool:
            """Return True if tok is a double-quoted string literal."""
            return len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"'

    def _string_value(self, tok: str):
            """Strip surrounding quotes from a string literal token."""
            return tok[1:-1]

    def _try_parse_int(self, tok: str):
            """Try to parse tok as base-10 int. Return int or None."""
            try:
                return int(tok, 10)
            except ValueError:
                return None

    # --- Table-literal helpers ({: ... ;}) ------------------------------
    def _in_table_literal_mode(self) -> bool:
            return bool(getattr(self, "_tbl_lit_stack", None))

    def _current_table_ctx(self):
            return self._tbl_lit_stack[-1] if self._tbl_lit_stack else None

    def _attach_table_value(self, ctx, val) -> None:
            """Attach val to ctx['pending_key'] in ctx['tref'], with duplicate-key check."""
            key = ctx.pending_key
            if key is None:
                raise RuntimeError("aucune clé en cours dans {: ... ;}")
            tbl = self.objspace._tables[ctx.tref.oid]
            if key in tbl:
                raise RuntimeError("clé en double dans {: ... ;}")
            self.objspace.t_set(ctx.tref, key, val)
            ctx.pending_key = None

    def _handle_table_key_or_value_literal(self, ctx, value) -> None:
            """Handle a string/int literal inside a table literal."""
            if ctx.pending_key is None:
                ctx.pending_key = value
            else:
                self._attach_table_value(ctx, value)

    def _close_table_literal(self) -> None:
            """Close the current {: ... ;} context and push resulting table on the data stack."""
            finished = self._tbl_lit_stack.pop()
            # If current context still has a pending key, attach top-of-stack value
            if finished.pending_key is not None:
                val = self.D.pop()
                self._attach_table_value(finished, val)
            # push the finished table itself
            self.D.append(finished.tref)
            # Attach implicitly to parent if a key is pending in the parent context (for nested tables)
            if self._tbl_lit_stack:
                parent = self._tbl_lit_stack[-1]
                if parent.pending_key is not None:
                    val = self.D.pop()  # the just-closed child table
                    self._attach_table_value(parent, val)

    def _handle_token_in_table_literal(self, tok: str) -> bool:
            """Process tok while inside a table literal. Return True if handled."""
            ctx = self._current_table_ctx()
            # nested table
            if tok == "{:":
                t = self.objspace.new_table()
                self._tbl_lit_stack.append(TableCtx(tref=t, pending_key=None))
                return True
            # end of current table
            if tok == ";}":
                self._close_table_literal()
                return True
            # explicit POP: use top-of-stack as value for current key
            if tok == "ASSIGN":
                if ctx is None or ctx.pending_key is None:
                    raise RuntimeError("ASSIGN sans clé en cours dans {: ... ;}")
                val = self.D.pop()
                self._attach_table_value(ctx, val)
                return True
            # string key/value
            if self._is_string_lit(tok):
                s = self._string_value(tok)
                self._handle_table_key_or_value_literal(ctx, s)
                return True
            # numeric key/value
            nval = self._try_parse_int(tok)
            if nval is not None:
                self._handle_table_key_or_value_literal(ctx, nval)
                return True
            # anything else (words, etc.) is left to normal interpreter
            return False

    # --- Quotation helpers ([: ... ;]) ---------------------------------
    # --- Quotation helpers ([: ... ;]) ---------------------------------
    def _handle_token_in_quotation(self, tok: str) -> None:
            """Capture tok into current quotation buffer."""
            # --- Cas spécial : table anonyme à l'intérieur d'une quote ---
            if tok == "{:":
                # On laisse la mécanique {: ... ;} existante construire la TABLE,
                # mais en désactivant temporairement la capture de quote.
                saved_q = self._quot_capturing
                self._quot_capturing = False

                # Ouvre le {: (comme si on n'était pas dans une quote)
                self.interpret_token("{:")

                # Consomme tous les tokens jusqu'à fermeture du {: ... ;}
                while self._in_table_literal_mode():
                    if not getattr(self, "_pending_tokens", None):
                        # On restaure l'état avant de râler
                        self._quot_capturing = saved_q
                        raise RuntimeError("unterminated {: ... ;} inside quotation")
                    tk = self.next_token_from_input()
                    self.interpret_token(tk)

                # On revient en mode capture de quote
                self._quot_capturing = saved_q

                # La table construite doit être en haut de la pile de données
                if not self.D or not is_table(self.D[-1]):
                    raise RuntimeError(
                        "table literal inside quotation did not leave TABLE on stack"
                    )
                tref = self.D.pop()
                # On capture cette TABLE comme littéral dans le corps de la quote
                self._quot_buffer.extend([LIT, tref])
                return

            # strings
            if self._is_string_lit(tok):
                s = self._string_value(tok)
                self._quot_buffer.extend([LIT, s])
                return
            # number?
            val = self._try_parse_int(tok)
            if val is not None:
                self._quot_buffer.extend([LIT, val])
                return
            # THIS inside quotation captured within a table literal: capture as LIT <tref>
            if tok == "THIS" and getattr(self, "_tbl_lit_stack", None):
                self._quot_buffer.extend([LIT, self._tbl_lit_stack[-1].tref])
                return
            # word
            w = self.dict.find(tok)
            if not w:
                raise RuntimeError(f"unknown token in quotation: {tok}")
            # For quotations we always capture the word token (even if IMMEDIATE)
            self._quot_buffer.append(w.xt())

    # --- Main token interpreter ----------------------------------
    def emit(self, text: str) -> None:
        """Point central de sortie texte (hookable par ForthOS)."""
        self.out.write(text)

    def interpret_token(self, tok: str) -> None:
            # Quotation capture ongoing?
            if self._quot_capturing and tok != ";]":
                self._handle_token_in_quotation(tok)
                return

            # ----- Table literal mode ("{: ... ;}") -----
            if self._in_table_literal_mode():
                if self._handle_token_in_table_literal(tok):
                    return

            # Open a new table literal if not already in one
            if tok == "{:" and not self._in_table_literal_mode():
                t = self.objspace.new_table()
                self._tbl_lit_stack = [TableCtx(tref=t, pending_key=None)]
                return

            # Not capturing quotation
            if self._is_string_lit(tok):
                s = self._string_value(tok)
                self.emit_literal_or_push(s)
                return

            val = self._try_parse_int(tok)
            if val is not None:
                self.emit_literal_or_push(val)
                return

            # word?
            w = self.dict.find(tok)
            if not w:
                raise RuntimeError(f"unknown token: {tok}")
            if self.compiling and not w.is_immediate():
                self.emit_token(w.xt())
            else:
                w.execute(self)

                # After executing a word at top-level, drive unless compiling/stepping

                depth = getattr(self, "_eval_depth", 0)
                if not self.compiling and not getattr(self, "_stepping", False) and depth == 0:
                    self._run_threaded()

    def interpret_line(self, line: str, *, out: Optional[Any] = None) -> str:
            old_out = getattr(self, "out", None)
            local_buf = io.StringIO()
            target = out if out is not None else local_buf
            # If target is a buffer with getvalue, capture starting position to return delta
            start_len = len(target.getvalue()) if hasattr(target, "getvalue") else None
            self.out = target
            try:
                # Flux de tokens partagé pour toutes les lignes de ce bloc.
                # Il permet à {: ... ;} (y compris dans une quotation) de s'étendre
                # sur plusieurs lignes.
                self._pending_tokens = []

                for raw in line.splitlines():
                    ln = raw.strip()
                    if not ln:
                        continue
                    # Ligne Forth normale : on ajoute simplement ses tokens au flux.
                    tokens = self.tokenize(ln)
                    self._pending_tokens.extend(tokens)

                # À la fin, on exécute tous les tokens Forth restants
                while self._pending_tokens:
                    self.interpret_token(self.next_token_from_input())

                if hasattr(target, "getvalue"):
                    if start_len is not None:
                        return target.getvalue()[start_len:]
                    return target.getvalue()
                return ''
            finally:
                self.out = old_out

    # --- Persistence / utilities for dot-commands ---
    def _env_to_dict(self):
            return {
                "dict": self.dict.to_dict(),
                "objspace": self.objspace.to_dict(),
                "data": self.data,
                "here": self.HERE,
                "version_tag": self.version_tag,
                "versions": getattr(self, "_versions", []),
                "timestamp": int(time.time()),
            }

    def _env_from_dict(self, d):
            self.dict = WordsDictionary.from_dict(d["dict"])
            self.objspace = ObjectSpace()
            self.objspace.from_dict(d["objspace"], self.dict.find_by_token)
            self.data = list(d.get("data", []))
            self.HERE = int(d.get("here", len(self.data)))
            self.version_tag = d.get("version_tag")
            self._versions = list(d.get("versions", []))
            self._install_core()


    # --- Dot-commands via dispatch table (Niveau 2) ---
    def _dotcmd_dispatch(self):
            return {
                ".help": self._dot_help,
                ".stack": self._dot_stack,
                ".rstack": self._dot_rstack,
                ".dict": self._dot_dict,
                ".word": self._dot_word,
                ".see": self._dot_see,
                ".save": self._dot_save,
                ".load": self._dot_load,
                ".history": self._dot_history,
                ".tag": self._dot_tag,
                ".ver": self._dot_ver,
                ".objects": self._dot_objects,
                ".dumpdata": self._dot_dumpdata,
                ".gc": self._dot_gc,
                ".bye": self._dot_bye,
            }

    def _dot_help(self, args, out):
            # Auto-generated layout (stable two lines)
            line1 = [".stack", ".rstack", ".dict", "[.word <w>]", "[.see <w>]"]
            line2 = [".save <file>", "/ .load <file>", "/ .tag <label>", "/ .history", "/ .ver", "/ .objects", "/ .gc"]
            out.write(" ".join(line1) + "\n")
            out.write(" ".join(line2) + "\n")

    def _dot_stack(self, args, out):
            out.write(f"<{len(self.D)}> " + " ".join(map(str, self.D)) + " \n")

    def _dot_rstack(self, args, out):
            out.write(f"<{len(self.R)}> " + " ".join(map(str, self.R)) + " \n")

    def _dot_dict(self, args, out):
            wdict = self.dict
            filt = args[0] if args else None
            names = [w.name for w in wdict.all_words() if not (w.flags & (WFlags.HIDDEN | WFlags.SMUDGE))]
            if filt:
                names = [n for n in names if filt.lower() in n.lower()]
            out.write(" ".join(sorted(names)) + "\n")

    def _dot_word(self, args, out):
            if not args:
                out.write("word not found: \n"); return
            w = self.dict.find_any(args[0])
            if not w:
                out.write(f"word not found: {args[0]}\n"); return
            out.write(f"name={w.name} token={w.token} class={w.code_class.name} flags={int(w.flags)}\n")

    def _dot_see(self, args, out):
            if not args:
                out.write("unknown: \n"); return
            w = self.dict.find(args[0]) or self.dict.find_any(args[0])
            if not w:
                out.write(f"unknown: {args[0]}\n"); return
            out.write(w.disasm(self.dict.token_to_name) + "\n")

    def _dot_save(self, args, out):
            if not args:
                out.write("save error: missing filename\n"); return
            fn = args[0]
            try:
                data = self._env_to_dict()
                with open(fn, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["timestamp"]))
                self._versions = getattr(self, "_versions", [])
                self._versions.append({"kind": "snapshot", "at": ts, "file": fn})
                out.write(f"snapshot saved to {fn}\n")
            except Exception as e:
                out.write(f"save error: {e}\n")

    def _dot_load(self, args, out):
            if not args:
                out.write("load error: missing filename\n"); return
            fn = args[0]
            try:
                with open(fn, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._env_from_dict(data)
                out.write(f"loaded {fn}\n")
            except Exception as e:
                out.write(f"load error: {e}\n")

    def _dot_history(self, args, out):
            self._versions = getattr(self, "_versions", [])
            if not self._versions:
                out.write("history: (empty)\n")
            else:
                out.write("history:\n")
                for i, v in enumerate(self._versions):
                    label = v.get("label", "")
                    out.write(f"  {i:02d}: {v['kind']} {label} at {v['at']} -> {v.get('file','')}\n")

    def _dot_tag(self, args, out):
            label = " ".join(args) if args else ""
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self.version_tag = label
            self._versions = getattr(self, "_versions", [])
            self._versions.append({"kind": "tag", "label": label, "at": ts})
            out.write(f"tagged {label}\n")

    def _dot_ver(self, args, out):
            out.write(f"version={self.version_tag or '(none)'}\n")

    def _dot_objects(self, args, out):

        n_tbl = len(self.objspace._tables)

        n_q   = len(self.objspace._quots)

        n_b   = len(self.objspace._bufs)

        # First line kept for legacy tests expecting prefix 'TABLES:'

        out.write("TABLES: " + ",".join(map(str, sorted(self.objspace._tables.keys()))) + " | "

                  "QUOTS(ref): " + ",".join(f"{oid}:{self.objspace._quots[oid].get('ref',0)}" for oid in sorted(self.objspace._quots.keys())) + " | "

                  "BUFS: " + ",".join(map(str, sorted(self.objspace._bufs.keys()))) + "\n")

        # Second line provides numeric counters for parser-based tests

        out.write(f"TABLE {n_tbl} | QUOT {n_q} | BUF {n_b}\n")

    def _dot_dumpdata(self, args, out):
            n = int(args[0]) if args else 32
            out.write("DATA: " + " ".join(map(str, self.data[:n])) + "\n")

    def _dot_gc(self, args, out):
            q,b = self.objspace.sweep_unreached(vm=self)
            pass
    def _dot_bye(self, args, out):
            return

    # --- Bind a pure-dispatch handle_dot_command to VM (keeps outputs identical) ---
    def handle_dot_command(self, line: str, out):
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            h = self._dotcmd_dispatch().get(cmd)
            if not h:
                out.write(f"unknown dot-cmd: {cmd}")
                return
            h(args, out)
            
####################################################################
# LES TESTS CI DESSOUS NE DOIVENT JAMAIS ETRE MODIFIÉ. ILS SONT LA POUR VALIDER L'IMPLÉMENTATION PLUS HAUT ET SERVENT DE RÉFÉRENCE.
# NE JAMAIS MODIFIER LES TEST.

class TestForthVM(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = VM()
        self.out = io.StringIO()

    def feed(self, src: str) -> str:
        return self.vm.interpret_line(src, out=self.out)

    def test_quot_in_table_and_gc_forth_only(self):
        # 1) Crée une table nommée (persistable), stocke une quotation, supprime la clé
        # 2) Vérifie en Forth que la clé n’existe plus: T@ renvoie 0 0 → on imprime "0 0"
        src = """
        TABLE CONSTANT foo
        foo "q" [: 1 ;] T!
        foo "q" TDROPKEY
        foo "q" T@ . BL .    \\ affiche "0 0" (val=0 puis flag=0)
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "0 0")
        
    def test_quot_reference_count(self):
        src = """
        [: 41 1 + . ;] CONSTANT q1
        TABLE CONSTANT t1
        t1 "quote" q1 T!
        t1 "quote" T@ DROP CALL \\ afficher 42
        """
        out = self.feed(src)
        self.assertIn("42", out)
        out1 = self.feed("q1 CALL \\ affiche 42")
        self.assertIn("42", out1)
        out2 = self.feed(""" 
        "quote" t1 TDROP    \\ ici la clée contenant une référence a la quote est supprimée
        q1 CALL             \\ mais ici la constant q1 contient encore la quote et q1 CALL dois retourner 42
        """)
        self.assertIn("42", out2)
        
    def test_gc_keeps_quot_referenced_by_constant_forth_only(self):
        src = r"""
        \ table + quotation + constant
        TABLE CONSTANT foo
        [: 41 1 + ;]  CONSTANT Q   \ Q pointe la quotation (-> 42)

        \ on met aussi la quotation dans la table, puis on enlève la clé
        foo "q" Q T!
        foo "q" TDROPKEY

        \ GC mark&sweep : ne doit PAS libérer la quotation tant que Q existe
        RUNGC

        \ preuve 1 : l’appel via Q fonctionne encore
        Q CALL . BL

        \ preuve 2 : la clé a bien disparu de la table
        foo "q" T@ . BL . BL
        """
        out = self.feed(src)
        # Q CALL . -> 42  ;  foo "q" T@ . . -> 0 0
        self.assertEqual(out.strip(), "42 0 0")

    def test_arith_and_logic(self):
        out = self.feed("1 2 + . BL\n5 2 - . BL\n6 3 * . BL\n7 3 /MOD . BL .\n")
        self.assertEqual(out.strip(), "3 3 18 1 2")
        out = self.feed("0 0= . BL\n -1 0< . BL\n 3 5 < . BL\n 5 3 > . BL\n 4 4 = . BL\n 4 5 <> .\n")
        self.assertEqual(out.strip(), "-1 -1 -1 -1 -1 -1")

    def test_memory(self):
        out = self.feed("HERE 3 ALLOT HERE SWAP - . BL\n 42 , HERE 1- @ . BL\n 5 HERE 1- +! HERE 1- @ .\n")
        self.assertEqual(out.strip(), "3 42 47")

    def test_colon_and_recurse(self):
        src = """
        : SQR DUP * ;
        7 SQR . BL
        : FACT ( n -- n! ) DUP 1 > IF DUP 1- RECURSE * ELSE DROP 1 THEN ;
        5 FACT . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "49 120")

    def test_if_else_then(self):
        out = self.feed(": NEGATE ( n -- -n ) 0 SWAP - ; : ABS ( n -- |n| ) DUP 0< IF NEGATE ELSE DUP THEN ; 5 ABS . BL -7 ABS .")
        self.assertEqual(out.strip(), "5 7")
        
    def test_begin_again_forth_only(self):
        src = r"""
        : LOOP3 ( -- )
          0
          BEGIN
            1+
            DUP . BL        \ affiche 1 2 3
            DUP 3 = IF EXIT THEN
          AGAIN
        ;
        LOOP3
        """
        out = self.feed(src)   # feed utilise vm.interpret_line(..., out=self.out)
        self.assertEqual(out.strip(), "1 2 3")

    def test_begin_until_while_repeat(self):
        src = """
        : COUNTDOWN ( n -- ) BEGIN DUP . BL 1- DUP 0= UNTIL DROP ;
        3 COUNTDOWN
        : SUMTO ( n -- s ) 0 SWAP BEGIN DUP 0> WHILE SWAP OVER + SWAP 1- REPEAT DROP ;
        5 SUMTO .
        """
        out = self.feed(src)
        # COUNTDOWN prints: 3 2 1 ; SUMTO 1+2+3+4+5 = 15
        self.assertEqual(out.strip(), "3 2 1 15")

    def test_tables_forth_only(self):
        src = """
        TABLE               
        DUP  1  100  T!     \\ t[1] := 100  ; keep a copy of t
        DUP  "name" "alice" T!  \\ t["name"] := "alice"
        : W 1234 ;
        DUP  ' W  1234  T!      \\ t[xt(W)] := 1234
        DUP  1      T@ . BL . BL      \\ expect: 100  -1
        DUP  "name" T@ . BL . BL     \\ expect: alice -1
        DUP  ' W    T@ . BL . BL     \\ expect: 1234 -1
        TKEYS .                 \\ print number of keys
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "-1 100 -1 alice -1 1234 3")

    def test_tdrop_and_tdropkey_forth_only(self):
        src = """
        TABLE            \\ t
        DUP  1 "x" T!    \\ t[1] := "x"
        DUP  1 T@ . BL . BL     \\ prints: "x" -1
        DUP  1 TDROPKEY . BL \\ -1
        DUP  1 T@ . BL . BL     \\ 0 0
        TDROP
        """
        out = self.feed(src)
        self.assertTrue(out.strip().startswith("-1 x -1 0 0"))
        #il manques ici la verification que la table n'existe plus

    def test_anonymous_table(self):
        src = """
        {: 
            "i" 0 
            "i++" [: THIS "i" THIS "i" THIS "i" T@ DROP 1 + T! T@ DROP . BL ;] 
        ;} CONSTANT t
        t "i++" T@ DROP CALL
        """
        out = self.feed(src)
        self.assertIn("1", out)
        src1 = """
        t "i++" T@ DROP CALL
        """
        out1 = self.feed(src1)
        self.assertIn("2", out1)
        
    def test_create_does_and_body(self):
        src = """
        CREATE ACC
        ' ACC >BODY 0 SWAP BODY!
        DOES>   DUP BODY@ 1+  SWAP BODY! ;
        ACC
        ACC
        ' ACC >BODY BODY@ .
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "2")

    def test_postpone_immediate_and_nonimmediate(self):
        src = """
        : IM 123 . BL ; IMMEDIATE
        : P POSTPONE IM ;
        P
        : ADD1 POSTPONE 1+ ;
        5 ADD1 .
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "123 6")
        
    def test_variable_create_and_use_with_body_ops(self):
        src = """
        VARIABLE X
        X @ . BL              \\ -> 0
        10 X !
        X @ . BL           \\ -> 10
        5 X +!
        X @ . BL         \\ -> 15
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "0 10 15")

    def test_constant_create_and_use(self):
        src = """
        123 CONSTANT C
        C . BL
        : ADDC C + ;
        7 ADDC .
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "123 130")
    
    def test_nested_tables_forth_only(self):
        src = """
        TABLE CONSTANT PERSONNE
        TABLE CONSTANT ADR
        ADR "ville" "trets" T!
        ADR "code"  13530   T!
        PERSONNE "adresse" ADR T!

        PERSONNE "adresse" T@ DROP
        DUP "ville" T@ . BL . BL
        DUP "code"  T@ . BL .
        DROP
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "-1 trets -1 13530")
        
    def test_nested_anonymous_tables_forth_only(self):
        src = """
        {:     \\ nouvelle table anonyme 
            "nom" "Terki"        \\ CLEF VALEUR
            "prenom" "Sofian"    
            "adresse"     {:    \\CLEF VALEUR(nouvelle table imbriqué)
                        "ville" "Trets"
                        "code" 13530 
                        ;} \\ fin de la table imbriqué
            ;}    \\ fin de la table
            
        CONSTANT Sofian ( Objref -- )    \\ la table anonyme était sur la pile et maintenant dans la constante Sofian
        Sofian "nom" T@ DROP . \\ -> "Terki"
        """
        out = self.feed(src)
        self.assertIn(out, "Terki")
        out2 = self.feed('Sofian "adresse" T@ DROP "code" T@ DROP . \\ -> 13530')
        self.assertIn(out2, "13530")
        
    def test_anonymous_tables_and_quote_call_forth_only(self):
        src = """
        TABLE CONSTANT t
        t "i" 0 T!
        t "i++" 
            [: t "i" t "i" t "i" T@ DROP 1 + T! T@ DROP . ;]    \\ i est lu, incrémenté puis sauvegardé
            T!
        t "i++" T@ DROP CALL 
        """
        out = self.feed(src)
        self.assertIn(out, "1")
        src1 = """
        {: 
            "i" 0 
            "i++" [: THIS "i" THIS "i" THIS "i" T@ DROP 1 + T! T@ DROP . ;]  \\ self ici permet de référencer la table en cour de construction :lit i, incrémente, sauvegarde
        ;}

        "i++" T@ DROP CALL

        """
        out1 = self.feed(src1)
        self.assertIn(out1, "1")
        
    def test_purge_word_with_quote(self):
        self.vm = VM()
        out = self.feed(': foo "test" . ; foo')
        self.assertIn(out, "test")
        out = self.feed(': inc [: 1 + . ;] CALL ; 1 inc ')
        self.assertIn(out, "2")
        out = self.feed(': dec [: 1 - . ;] CALL ; 1 dec ')
        self.assertIn(out, "0")
        self.assertEqual(len(self.vm.objspace._quots), 2)               # nombre de quote, 1 dans inc et 1 dans dec
        self.feed("' inc FORGET-XT RUNGC")                                    # enleve le mot inc du dictionaire et tout les mots définit apres lui, donc ici dec rungc lance le garbage collector
        with self.assertRaisesRegex(RuntimeError, r"unknown token: dec"):    # dec n'est plus présent
            self.feed(" 1 dec")
        with self.assertRaisesRegex(RuntimeError, r"unknown token: inc"):    # inc a été aussi supprimé
            self.feed(" 1 inc")
        self.assertEqual(len(self.vm.objspace._quots), 0)               # inc et dec on été suprimé donc les quotes on été garbage collected
        out = self.feed("foo")                                          # foo est toujours disponible dans le dictionaire
        self.assertIn(out, "test")
        self.feed("FORGET-NAME foo")
        with self.assertRaisesRegex(RuntimeError, r"unknown token: foo"):    # inc a été aussi supprimé
            self.feed("foo")
            
    def test_inlined_quote_refcounted_in_colon(self):
        self.vm = VM()
        self.assertIn(self.feed(': foo [: "test" . ;] CALL ;'), "test")   #quote dans foo retourne test avant RUNGC
        self.assertIn(self.feed('RUNGC foo'), "test")    # quote dans foo retrourne encore test avec RUNGC

    def test_evaluate_forth_simple(self):
        # EVALUATE-FORTH doit exécuter une ligne simple "1 2 + ."
        src = r'"1 2 + ." EVALUATE-FORTH'
        out = self.feed(src)
        self.assertEqual(out.strip(), "3")
        
    def test_evaluate_forth_inside_colon(self):
        # EVALUATE-FORTH est appelé dans le corps d’un mot colon.
        # On vérifie que la ligne complète "2 4 * ." est exécutée.
        src = r"""
        : INNER  "2 4 * ." EVALUATE-FORTH ;
        INNER
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "8")
        
    def test_evaluate_forth_nested_via_inner_word(self):
        # Imbrication :
        #   1) EVALUATE-FORTH interprète la ligne "INNER"
        #   2) INNER appelle lui-même EVALUATE-FORTH sur "2 4 * ."
        src = r"""
        : INNER  "2 4 * ." EVALUATE-FORTH ;
        "INNER" EVALUATE-FORTH
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "8")    
        
    def test_time_returns_increasing_epoch(self):
        """
        TIME ( -- epoch ) doit pousser un entier positif (epoch Unix),
        et deux appels espacés par un petit SLEEP doivent être non décroissants.
        """
        src = r"""
        TIME . BL
        50 SLEEP
        TIME . BL
        """
        out = self.feed(src)
        # On récupère les nombres imprimés
        parts = [p for p in out.strip().split() if p.lstrip("-").isdigit()]
        self.assertGreaterEqual(len(parts), 2, msg=f"TIME output too short: {out!r}")
        t1, t2 = map(int, parts[:2])

        self.assertGreater(t1, 0, "TIME should return a positive epoch")
        self.assertLessEqual(t1, t2, "Second TIME should be >= first TIME")

    def test_sleep_delays_roughly_expected_duration(self):
        """
        SLEEP ( ms -- ) doit bloquer la VM pendant au moins la durée demandée.
        On mesure côté Python pour éviter de dépendre de TIME dans le test.
        """
        start = time.time()
        # 200 ms → on attend au moins ~0.18 s, mais on laisse une marge haute large
        self.feed("200 SLEEP")
        elapsed = time.time() - start

        self.assertGreaterEqual(
            elapsed, 0.18,
            msg=f"SLEEP 200ms too short: elapsed={elapsed:.3f}s"
        )
        self.assertLess(
            elapsed, 1.0,
            msg=f"SLEEP 200ms took suspiciously long: elapsed={elapsed:.3f}s"
        )

    def test_sleep_negative_duration_is_clamped_to_zero(self):
        """
        SLEEP doit gérer les durées négatives de façon sûre (clamp à 0 dans l'implémentation proposée).
        Ce test vérifie juste qu'un SLEEP négatif ne bloque pas.
        """
        start = time.time()
        self.feed("-100 SLEEP")
        elapsed = time.time() - start

        self.assertLess(
            elapsed, 0.05,
            msg=f"SLEEP -100ms should return almost immediately, elapsed={elapsed:.3f}s"
        )
    


class TestCASEOF(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = VM()
        self.out = io.StringIO()

    def feed(self, src: str) -> str:
        return self.vm.interpret_line(src, out=self.out)
        
    def test_case_of_single_match(self):
        src = """
        : FOO ( n -- m )
            CASE
                1 OF 100 ENDOF
                ( default )
                DROP 999
            ENDCASE
        ;

        1 FOO . BL
        2 FOO . BL
        """
        out = self.feed(src)
        # 1 -> 100 (branche OF)
        # 2 -> 999 (branche par défaut)
        self.assertEqual(out.strip(), "100 999")
        
    def test_case_of_multiple_branches_int(self):
        src = """
        : MAP3 ( n -- m )
            CASE
                1 OF 10 ENDOF
                2 OF 20 ENDOF
                3 OF 30 ENDOF
                ( default )
                DROP 99
            ENDCASE
        ;

        1 MAP3 . BL
        2 MAP3 . BL
        3 MAP3 . BL
        4 MAP3 . BL
        """
        out = self.feed(src)
        # 1 -> 10, 2 -> 20, 3 -> 30, 4 -> 99 (default)
        self.assertEqual(out.strip(), "10 20 30 99")
    
    def test_case_of_string_keys(self):
        src = """
        : MSG-KIND ( addr -- code )
            CASE
                "ping" OF 1 ENDOF
                "pong" OF 2 ENDOF
                "stop" OF 3 ENDOF
                ( default )
                DROP -1
            ENDCASE
        ;

        "ping" MSG-KIND . BL
        "pong" MSG-KIND . BL
        "stop" MSG-KIND . BL
        "unknown" MSG-KIND . BL
        """
        out = self.feed(src)
        # ping -> 1, pong -> 2, stop -> 3, unknown -> -1
        self.assertEqual(out.strip(), "1 2 3 -1")
    
    def test_case_of_stack_balance(self):
        src = """
        : PLUS-OR-MINUS ( n f -- res )
            \\ si f=0 => +1 ; si f<>0 => -1
            CASE
                0 OF DROP 1 ENDOF
                ( default )
                DROP -1
            ENDCASE
        ;

        5 0 PLUS-OR-MINUS . BL   \\ -> 1
        5 1 PLUS-OR-MINUS . BL   \\ -> -1
        5 42 PLUS-OR-MINUS . BL  \\ -> -1
        """
        out = self.feed(src)
        # trois appels, chacun retourne une seule valeur
        self.assertEqual(out.strip(), "1 -1 -1")
        
    def test_case_of_with_if_guard(self):
        src = r"""
        : SIGN-CLASSIFY ( n -- code )
          \ garde négatif
          DUP 0< IF
            DROP -1 EXIT
          THEN
          \ ici n >= 0 : on distingue 0 du reste avec CASE
          CASE
            0 OF               \ n == 0 ? (OF a déjà DROP la scrutinee en cas de match)
              0 ENDOF
            \ default : n > 0
            DROP 1             \ (en non-match, la scrutinee est encore là → on la jette)
          ENDCASE
        ;

        -5 SIGN-CLASSIFY . BL
        0  SIGN-CLASSIFY . BL
        7  SIGN-CLASSIFY . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "-1 0 1")

    def test_case_of_message_table_pattern_simplified(self):
        src = """
        \\ helpers très simples pour extraire des champs de table
        : MSG-TYPE ( msg -- msg type )
            DUP "msg" T@ DROP ;
        : HANDLE ( msg -- code )
            MSG-TYPE          \\ msg type
            CASE
                "ping" OF DROP 1 ENDOF
                "pong" OF DROP 2 ENDOF
                ( default )
                DROP -1
            ENDCASE
        ;

        {: "msg" "ping" ;} HANDLE . BL
        {: "msg" "pong" ;} HANDLE . BL
        {: "msg" "other" ;} HANDLE . BL
        """
        out = self.feed(src)
        # ping -> 1, pong -> 2, autre -> -1
        self.assertEqual(out.strip(), "1 2 -1")

# 1) Default explicite (vérifie que ENDCASE ne DROP pas la scrutinee)
    def test_case_default_explicit(self):
        src = r"""
        : F ( x -- y )
          CASE
            1 OF 10 ENDOF
            \ default: x != 1
            DROP 99
          ENDCASE
        ;
        1 F . BL   \ -> 10
        2 F . BL   \ -> 99 (default doit faire le DROP lui-même)
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "10 99")

        # 2) Sans default: la scrutinee doit rester en pile si aucune clause ne matche
    def test_case_without_default_leaves_scrutinee(self):
        src = r"""
        : G ( x -- y )
          CASE
            2 OF 20 ENDOF
            3 OF 30 ENDOF
          ENDCASE
        ;
        4 G . BL   \ rien ne matche -> on imprime la scrutinee (4)
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "4")

        # 3) Clauses multiples (entiers) + default
    def test_case_multi_int_then_default(self):
        src = r"""
        : H ( n -- m )
          CASE
            10 OF 1 ENDOF
            20 OF 2 ENDOF
            30 OF 3 ENDOF
            \ default
            DROP 9
          ENDCASE
        ;
        10 H . BL  20 H . BL  30 H . BL  7 H . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "1 2 3 9")

        # 4) Chaînes + entiers mélangés (vérifie '=' et le flux)
    def test_case_mixed_string_and_int(self):
        src = r"""
        : TYPETAG>CODE ( x -- code )
          CASE
            "ok" OF    1 ENDOF
            "err" OF  -1 ENDOF
            0 OF       0 ENDOF
            \ default
            DROP 99
          ENDCASE
        ;
        "ok"  TYPETAG>CODE . BL
        "err" TYPETAG>CODE . BL
        0     TYPETAG>CODE . BL
        42    TYPETAG>CODE . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "1 -1 0 99")

        # 5) Unicité d’exécution: si une clause matche, la suivante ne s’exécute pas
    def test_case_only_one_clause_runs(self):
        src = r"""
        : ONCE ( n -- m )
          CASE
            1 OF  100 ENDOF
            1 OF  200 ENDOF  \ ne doit jamais s'exécuter si la 1ère a matché
            \ default
            DROP 999
          ENDCASE
        ;
        1 ONCE . BL   \ -> 100
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "100")

        # 6) Interaction avec IF/THEN (sans ELSE) autour d’un CASE
    def test_if_then_guard_then_case(self):
        src = r"""
        : SIGN-CLASSIFY2 ( n -- code )
          DUP 0< IF  DROP -1 EXIT  THEN   \ n < 0 → on sort
          CASE
            0 OF       0       ENDOF      \ n == 0
            DROP 1                         \ default: n > 0 (scrutinee laissée → on la jette)
          ENDCASE
        ;
        -5 SIGN-CLASSIFY2 . BL
        0  SIGN-CLASSIFY2 . BL
        7  SIGN-CLASSIFY2 . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "-1 0 1")

        # 7) CASE imbriqué (CASE dans une branche OF)
    def test_nested_case(self):
        src = r"""
        : NEST ( a b -- code )
        \ On case d'abord sur b, puis on case sur a (imbriqué).
        CASE                     \ scrutinee = b (c'est b qui est au top)
        0 OF                   \ b == 0
          \ Ici la scrutinee b a été droppée par OF ; il reste a
          CASE                 \ scrutinee = a
            0 OF 10 ENDOF      \ a == 0 -> 10
            DROP 11            \ default (a != 0) -> 11
          ENDCASE
        ENDOF
        1 OF                   \ b == 1
          CASE                 \ scrutinee = a
            0 OF 11 ENDOF      \ a == 0 -> 11
            DROP 20            \ default (a != 0) -> 20
          ENDCASE
        ENDOF
        \ default pour b (ni 0 ni 1)
        DROP                   \ jeter b ; scrutinee suivante = a
        CASE
          0 OF 11 ENDOF        \ a == 0 -> 11
          DROP 21              \ a != 0 -> 21
        ENDCASE
        ENDCASE
        ;

        0 0 NEST . BL    \ -> 10
        0 5 NEST . BL    \ -> 11
        9 1 NEST . BL    \ -> 20
        9 7 NEST . BL    \ -> 21
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "10 11 20 21")

        # 8) Vérifie que la branche 'OF' ne double-DROP pas (OF a déjà droppé la scrutinee)
    def test_case_of_does_drop_once(self):
        src = r"""
        : DROP-ONCE ( n -- m )
          CASE
            5 OF      \ en match, OF a déjà DROP la scrutinee
              123     \ pas de DROP ici !
            ENDOF
            \ default
            DROP 0
          ENDCASE
        ;
        5 DROP-ONCE . BL   \ -> 123
        6 DROP-ONCE . BL   \ -> 0
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "123 0")

        # 9) “Pas de default” + IF autour : on force un résultat si rien ne matche
    def test_case_no_default_then_if_fallback(self):
        src = r"""
    : FALLBACK ( x -- y )
      DUP CASE
        "A" OF  DROP 1 EXIT  ENDOF   \ match A → résultat immédiat
        "B" OF  DROP 2 EXIT  ENDOF   \ match B → résultat immédiat
      ENDCASE
      \ ici seulement si A/B n'ont pas matché : on a encore "x x" (scrutinee + copie)
      2DROP 42
    ;

        "A" FALLBACK . BL
        "B" FALLBACK . BL
        "Z" FALLBACK . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "1 2 42")
        
class TestBYTEARRAY(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = VM()
        self.out = io.StringIO()

    def feed(self, src: str) -> str:
        return self.vm.interpret_line(src, out=self.out)
     
    def test_buf_len_and_zero_init_forth_only(self):
        src = r"""
        4 BUF CONSTANT B

        B BUF-LEN . BL          \ -> 4

        B 0 BUF@ . BL           \ -> 0
        B 1 BUF@ . BL           \ -> 0
        B 2 BUF@ . BL           \ -> 0
        B 3 BUF@ . BL           \ -> 0
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "4 0 0 0 0")

    def test_buf_store_and_load_forth_only(self):
        src = r"""
        4 BUF CONSTANT B

        65 B 0 BUF!             \ 'A'
        66 B 1 BUF!             \ 'B'
        67 B 2 BUF!             \ 'C'
        68 B 3 BUF!             \ 'D'

        B 0 BUF@ . BL
        B 1 BUF@ . BL
        B 2 BUF@ . BL
        B 3 BUF@ . BL
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "65 66 67 68")
 
    def test_str_to_buf_and_back_forth_only(self):
        src = r"""
        "HELLO" STR>BUF
        DUP BUF-LEN . BL        \ -> 5
        BUF>STR .               \ -> HELLO
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "5 HELLO")

    def test_str_to_buf_exposes_ascii_bytes_forth_only(self):
        src = r"""
        "ABC" STR>BUF CONSTANT B

        B 0 BUF@ . BL           \ 'A' = 65
        B 1 BUF@ . BL           \ 'B' = 66
        B 2 BUF@ . BL           \ 'C' = 67
        """
        out = self.feed(src)
        self.assertEqual(out.strip(), "65 66 67")

    def test_buf_copy_independent_src_and_dst_forth_only(self):
        src = r"""
        4 BUF CONSTANT SRC
        4 BUF CONSTANT DST

        \ SRC := "ABCD" (65 66 67 68)
        65 SRC 0 BUF!
        66 SRC 1 BUF!
        67 SRC 2 BUF!
        68 SRC 3 BUF!

        \ DST := copie de SRC
        DST SRC BUF-COPY

        \ on modifie seulement DST[1] = 'Z' = 90
        90 DST 1 BUF!

        \ on affiche SRC puis DST pour vérifier
        SRC 0 BUF@ . BL
        SRC 1 BUF@ . BL
        SRC 2 BUF@ . BL
        SRC 3 BUF@ . BL

        DST 0 BUF@ . BL
        DST 1 BUF@ . BL
        DST 2 BUF@ . BL
        DST 3 BUF@ .
        """
        out = self.feed(src)
        # SRC : 65 66 67 68
        # DST : 65 90 67 68
        self.assertEqual(out.strip(), "65 66 67 68 65 90 67 68")
        
    def test_buf_persist_roundtrip_simple_forth_only(self):
        # STR>BUF -> save -> load -> BUF>STR
        src = r'''
        "HELLO" STR>BUF CONSTANT B
        '''
        self.feed(src)
        out = self.feed('B BUF>STR .')
        self.assertEqual(out.strip(), "HELLO")

    def test_buf_persist_inside_table_and_len_forth_only(self):
        # On stocke un BUF dans une TABLE, on sauve, on recharge, on retrouve le contenu et la longueur
        src = r'''
        TABLE CONSTANT T
        "ABC" STR>BUF CONSTANT B
        T "buf" B T!              \ T["buf"] = B
    
        T "buf" T@ DROP . BL           \ doit imprimer un ObjRef (tolérant côté asserion)
        T "buf" T@ DROP BUF-LEN . BL   \ -> 3
        T "buf" T@ DROP BUF>STR .
        '''
        out = self.feed(src)
        self.assertTrue("TABLE" in out or "QUOT" in out or "OBJ" in out)  # robuste selon ton .objects/print
        self.assertIn("3", out)                                           # longueur
        self.assertTrue(out.strip().endswith("ABC"))                      # contenu

    def test_buf_negative_size_raises_forth_only(self):
        src = r'''
        -1 BUF DROP
        '''
        with self.assertRaisesRegex(RuntimeError, "negative size|negative"):
            self.feed(src)

    def test_buf_at_oob_raises_forth_only(self):
        src = r'''
        2 BUF CONSTANT B
        B  2 BUF@ .               \ index == len -> OOB
        '''
        with self.assertRaisesRegex(RuntimeError, "index out of range|BUF@"):
            self.feed(src)

    def test_buf_len_wrong_type_raises_forth_only(self):
        src = r'''
        TABLE CONSTANT T
        T BUF-LEN .               \ BUF-LEN attend un BUF
        '''
        with self.assertRaisesRegex(RuntimeError, "BUF-LEN.*BUF|expects BUF"):
            self.feed(src)

    def test_buf_at_wrong_type_raises_forth_only(self):
        src = r'''
        TABLE CONSTANT T
        T 0 BUF@ .
        '''
        with self.assertRaisesRegex(RuntimeError, "BUF@.*BUF|expects BUF"):
            self.feed(src)

    def test_buf_store_wrong_type_raises_forth_only(self):
        src = r'''
        TABLE CONSTANT T
        65 T 0 BUF!               \ 1er arg OK (byte), 2e pas un BUF
        '''
        with self.assertRaisesRegex(RuntimeError, "BUF!.*BUF|expects BUF"):
            self.feed(src)

    def test_buf_copy_wrong_types_raises_forth_only(self):
        src = r'''
        TABLE CONSTANT T
        1 BUF CONSTANT B
        T B BUF-COPY
        '''
        with self.assertRaisesRegex(RuntimeError, "BUF-COPY.*buffers|expects"):
            self.feed(src)

    def test_buf_copy_different_sizes_copies_min_len_forth_only(self):
        # Ce n’est pas un test d’erreur : on verrouille la sémantique "min(len)"
        src = r'''
        "ABCDE" STR>BUF CONSTANT S
        3 BUF CONSTANT D
        D S BUF-COPY
        D 0 BUF@ . BL
        D 1 BUF@ . BL
        D 2 BUF@ .
        '''
        out = self.feed(src)
        self.assertEqual(out.strip(), "65 66 67")  # 'A' 'B' 'C'
        
class TestObjectsAndGcCounts(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = VM()
        self.out = io.StringIO()

    def feed(self, src: str) -> str:
        return self.vm.interpret_line(src, out=self.out)

    # --------- Helpers -------------------------------------------------
    def _last_output(self) -> str:
        val = self.out.getvalue()
        self.out.truncate(0); self.out.seek(0)
        return val

    def _parse_counts(self, txt: str) -> dict:
        # tolérant: "TABLE: 2", "TABLE 2", "TABLE=2" etc.
        import re
        kinds = ["TABLE", "QUOT", "BUF", "BUFF", "BYTEARRAY"]
        counts = {}
        for k in kinds:
            m = re.search(rf"(?m)^{k}\s+(\d+)(?=\s*(?:\||$))", txt)

            if m:
                counts[k] = int(m.group(1))
        return counts

    def _has_word(self, name: str) -> bool:
        return self.vm.dict.find(name) is not None
        
    def test_buf_gc_drop_from_stack_forth_only(self):
        # Crée un buffer puis le retire de la pile => plus aucune racine.
        # Après RUNGC, aucun buffer ne doit rester vivant.
        self.vm = VM()
        self.assertEqual(len(self.vm.objspace._bufs), 0)

        out = self.feed('4 BUF DROP')
        self.assertEqual(len(self.vm.objspace._bufs), 1)  # alloué

        self.feed('RUNGC')  # collecte
        self.assertEqual(len(self.vm.objspace._bufs), 0)

    def test_buf_gc_after_tdropkey_forth_only(self):
        self.vm = VM()
        self.assertEqual(len(self.vm.objspace._bufs), 0)

        src = r'''
        TABLE CONSTANT T
        "ABC" STR>BUF CONSTANT B
        T "msg" B T!       \ T["msg"] = B, unique référence
        '''
        self.feed(src)
        self.assertEqual(len(self.vm.objspace._bufs), 1)

        # On enlève la clé => B n'a plus de racine
        self.feed('T "msg" TDROPKEY DROP RUNGC')
        self.assertEqual(len(self.vm.objspace._bufs), 0)

    def test_buf_gc_after_tdrop_table_forth_only(self):
        self.vm = VM()

        src = r'''
        TABLE CONSTANT T
        "DATA" STR>BUF CONSTANT B
        T "buf" B T!
        '''
        self.feed(src)
        self.assertEqual(len(self.vm.objspace._bufs), 1)

        # TDROP supprime la table (dernière racine), puis RUNGC ramasse le BUF
        self.feed('T TDROP RUNGC')
        self.assertEqual(len(self.vm.objspace._bufs), 0)

    def test_buf_gc_copy_then_release_owners_forth_only(self):
        self.vm = VM()
        src = r'''
        TABLE CONSTANT T
        "ABCDE" STR>BUF CONSTANT S
        3 BUF CONSTANT D
        D S BUF-COPY
        T "dst" D T!
        T "src" S T!
        '''
        self.feed(src)
        self.assertEqual(len(self.vm.objspace._bufs), 2)  # S et D vivants

        # Retirer "src" puis GC -> 'S' n'a plus de racine => collecté ; 'D' vit encore
        self.feed('T "src" TDROPKEY DROP RUNGC')
        self.assertEqual(len(self.vm.objspace._bufs), 1)

        # Retirer "dst" puis GC -> plus aucune racine => tout collecté
        self.feed('T "dst" TDROPKEY DROP RUNGC')
        self.assertEqual(len(self.vm.objspace._bufs), 0)

    def test_buf_gc_after_forget_word_and_rungc_forth_only(self):
        self.vm = VM()

        # Mot 'make' qui crée un buffer et le range dans T["buf"]
        src = r'''
        TABLE CONSTANT T
        : make T "buf" "HELLO" STR>BUF T! ;
        make
        '''
        self.feed(src)
        self.assertEqual(len(self.vm.objspace._bufs), 1)

        # FORGET le mot 'make' n'efface pas T, donc le BUF est encore référencé
        self.feed("' make FORGET-XT RUNGC")
        self.assertEqual(len(self.vm.objspace._bufs), 1)  # toujours là (référence depuis T)

        # On enlève la référence, puis GC => le buffer disparaît
        self.feed('T "buf" TDROPKEY DROP RUNGC')
        self.assertEqual(len(self.vm.objspace._bufs), 0)

    def test_buf_gc_does_not_collect_roots_forth_only(self):
        self.vm = VM()
        src = r'''
        TABLE CONSTANT T
        "keep" STR>BUF CONSTANT B
        T "root" B T!
        RUNGC        \ rien ne doit être collecté
        '''
        self.feed(src)
        self.assertEqual(len(self.vm.objspace._bufs), 1)  # encore référencé
    
    # --------- QUOT: création puis libération par TDROP + .gc ----------
    @unittest.skip("ne pas implementer pour le moment")
    def test_quot_counts_drop_after_tdrop_and_gc(self):
	    # baseline
	    self.feed(".objects"); base = self._parse_counts(self._last_output())
	    base_q = base.get("QUOT", 0)

	    # Crée une table T, y stocke une quotation Q => Q référencée
	    self.feed("{: ;}")           # push table
	    self.feed(' "q" [: 1 2 + . ;] T! ')
	    self.feed(".objects"); with_q = self._parse_counts(self._last_output())
	    self.assertGreaterEqual(with_q.get("QUOT", 0), base_q + 1,
	                            "QUOT devrait augmenter après insertion dans TABLE")

	    # Supprime la table => plus de ref vers Q, puis .gc pour sweep
	    self.feed("TDROP")
	    self.feed("RUNGC")
	    self.feed(".objects"); after_gc = self._parse_counts(self._last_output())

	    # Attendu: QUOT retombe (au moins) à la valeur de départ
	    self.assertLessEqual(after_gc.get("QUOT", 0), with_q.get("QUOT", 0))
	    self.assertGreaterEqual(after_gc.get("QUOT", 0), base_q)
	    
class TestTableInQuote(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = VM()
        self.out = io.StringIO()

    def feed(self, src: str) -> str:
        return self.vm.interpret_line(src, out=self.out)
        
    def test_tableinquote(self):
        src = """
        [: 
            {: "msg" "hello" ;} 
            "msg" T@ DROP . \\ affiche "hello"
        ;] CALL
        """
        out = self.feed(src)
        self.assertIn(out, "hello")
        
    def test_tableinquote(self):
        src = """
        [: 
            {: "msg" "hello" 
                "from" SELF-PID
            ;} 
            "msg" T@ DROP . \\ affiche "hello"
        ;] CALL
        """
        out = self.feed(src)
        self.assertIn(out, "hello")
        
class TestEnvSnapshot_ModelA(unittest.TestCase):
    """
    Modèle A : deep-copy de l'environnement Forth (dict, objspace, data space…)
    mais piles D/R fraîches dans la VM clonée.
    """

    def _run_forth(self, vm: VM, src: str) -> str:
        """Utilitaire local pour exécuter du Forth dans une VM donnée."""
        out = io.StringIO()
        vm.interpret_line(src, out=out)
        return out.getvalue()

    def test_snapshot_modelA_stacks_are_fresh(self):
        """
        - On prépare une VM avec un peu d'état (mot, TABLE, BUF).
        - On remplit explicitement les piles D/R côté parent.
        - On reconstruit une deuxième VM via _env_from_dict.
        - Les piles de la nouvelle VM doivent être vides (modèle A).
        """
        parent = VM()

        # Environnement Forth minimal : un mot, une table, un buffer
        src_init = r'''
        : INC 1 + ;
        TABLE CONSTANT T
        T DUP 1 100 T!          \ T[1] = 100
        "HELLO" STR>BUF CONSTANT B
        '''
        self._run_forth(parent, src_init)

        # On force un peu de bruit sur les piles du parent
        parent.D.extend([10, 20])
        parent.R.append("ret-marker")

        # Snapshot Python de l'environnement
        env = parent._env_to_dict()
        child = VM()
        child._env_from_dict(env)

        # Modèle A : les piles du clone doivent être neuves
        self.assertEqual(child.D, [], "La pile de données de la VM clonée doit être vide")
        self.assertEqual(child.R, [], "La pile de retour de la VM clonée doit être vide")

        # Mais l'environnement Forth (mot / table / buffer) est bien recopié
        out_parent = self._run_forth(
            parent,
            '5 INC . BL  T 1 T@ . BL . BL  B BUF>STR .'
        )
        out_child = self._run_forth(
            child,
            '5 INC . BL  T 1 T@ . BL . BL  B BUF>STR .'
        )

        # Même sémantique dans les deux VM
        self.assertEqual(out_parent.strip(), out_child.strip())
        self.assertIn("6", out_parent)          # 5 INC -> 6
        self.assertIn("100", out_parent)       # T[1] = 100
        self.assertIn("HELLO", out_parent)     # contenu du buffer

    def test_snapshot_modelA_deepcopy_tables_and_bufs(self):
        """
        - On prépare une table et un buffer dans le parent.
        - On clone l'env dans une seconde VM.
        - On modifie table + buffer dans l'enfant.
        - Le parent ne doit PAS voir ces modifications (deep-copy ObjSpace).
        """
        parent = VM()

        src_init = r'''
        TABLE CONSTANT T
        T DUP 1 111 T!          \ T[1] = 111
        "HELLO" STR>BUF CONSTANT B
        '''
        self._run_forth(parent, src_init)

        # Snapshot -> nouvelle VM
        env = parent._env_to_dict()
        child = VM()
        child._env_from_dict(env)

        # Avant mutation : les deux VM voient la même chose
        out_parent_before = self._run_forth(
            parent,
            'T 1 T@ . BL . BL  B BUF>STR .'
        )
        out_child_before = self._run_forth(
            child,
            'T 1 T@ . BL . BL  B BUF>STR .'
        )
        self.assertEqual(out_parent_before.strip(), out_child_before.strip())
        self.assertIn("111", out_parent_before)
        self.assertIn("HELLO", out_parent_before)

        # On modifie la TABLE et le BUF uniquement dans l'enfant
        src_child_mut = r'''
        T DUP 1 222 T!          \ T[1] = 222 dans l'enfant
        88 B 0 BUF!             \ 'X' (88) à la position 0 du buffer dans l'enfant
        '''
        self._run_forth(child, src_child_mut)

        out_child_after = self._run_forth(
            child,
            'T 1 T@ . BL . BL  B BUF>STR .'
        )
        out_parent_after = self._run_forth(
            parent,
            'T 1 T@ . BL . BL  B BUF>STR .'
        )

        # Dans l'enfant, la valeur de la table a changé
        self.assertIn("222", out_child_after)
        self.assertNotIn("111", out_child_after)

        # Dans le parent, l'ancienne valeur est intacte
        self.assertIn("111", out_parent_after)
        # Et les sorties parent/enfant divergent bien après mutation
        self.assertNotEqual(out_parent_after.strip(), out_child_after.strip())

    def test_snapshot_modelA_metadata_is_copied_but_not_shared(self):
        """
        - On prépare un tag + historique de versions.
        - Le clone récupère la même valeur au snapshot.
        - Les modifications ultérieures de version_tag dans l'enfant
          ne doivent pas affecter le parent.
        """
        parent = VM()
        parent.version_tag = "parent-env"
        parent._versions = ["v0", "v1"]

        env = parent._env_to_dict()
        child = VM()
        child._env_from_dict(env)

        # Le clone voit le même état au moment du snapshot
        self.assertEqual(child.version_tag, "parent-env")
        self.assertEqual(child._versions, ["v0", "v1"])

        # Mais les modifications sont indépendantes
        child.version_tag = "child-env"
        child._versions.append("v2-child")

        self.assertEqual(parent.version_tag, "parent-env")
        self.assertEqual(parent._versions, ["v0", "v1"])
        self.assertEqual(child.version_tag, "child-env")
        self.assertEqual(child._versions, ["v0", "v1", "v2-child"])

       
def test_all():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
    
if __name__ == "__main__" :
    test_all()
