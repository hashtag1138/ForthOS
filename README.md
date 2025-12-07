# ForthOS – Interactive Forth VM Playground

ForthOS is an experimental environment for running multiple Forth-like virtual machines (VMs) as independent actors, controlled from a text-based user interface (TUI). It is designed as a playground for VM design, actor-style concurrency, and interactive programming.

The project provides:

- A token-threaded Forth VM with an object space (tables, buffers, strings)
- An actor/message model between VMs and the host
- A TCP server exposing the host as a remote service
- A full-screen TUI with:
  - F1: Documentation view (this file)
  - F2: Console + VM list
  - F3: Multi-file code editor

When launched through `forthos_tui_prototype.py`, this README is intended to be shown in the F1 “Doc” screen.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Quick Start](#quick-start)
5. [TUI Overview](#tui-overview)
   - [Screens (F1/F2/F3)](#screens-f1f2f3)
   - [Console Screen (F2)](#console-screen-f2)
   - [Editor Screen (F3)](#editor-screen-f3)
6. [Host Commands (`:host ...`)](#host-commands-host-)
7. [Dot Commands (`.stack`, `.dict`, …)](#dot-commands-stack-dict-)
8. [ForthOS Language Basics](#forthos-language-basics)
   - [Stack and Arithmetic](#stack-and-arithmetic)
   - [Definitions and Control Flow](#definitions-and-control-flow)
   - [Strings](#strings)
   - [Tables](#tables)
   - [Binary Buffers](#binary-buffers)
9. [Actor Model and Messaging](#actor-model-and-messaging)
10. [Snapshots and Persistence](#snapshots-and-persistence)
11. [Example Session](#example-session)
12. [Development Notes and Tests](#development-notes-and-tests)
13. [License](#license)

---

## Overview

ForthOS combines a Forth-like language and a small actor runtime:

- Each VM runs its own Forth dictionary, stacks, and object space.
- The host process manages VM lifecycles and routes messages between them.
- A TCP server exposes the host so that the TUI can run as a separate process.
- The TUI offers:
  - a documentation view (F1),
  - a multi-VM console (F2),
  - and a simple multi-tab editor (F3).

The system is meant as a sandbox: it is not a full ANS Forth, but a pragmatic Forth-style environment for experiments.

---

## Architecture

The repository is roughly organized as follows:

### `forthos_vm_core.py`
Core Forth VM implementation:  
- Data stack / return stack  
- Dictionary and compilation  
- Token-threaded execution  
- Built-in primitives  
- Object space (tables, buffers, strings)  
- Dot commands  
- Snapshot serialization helpers  

### `forthos_runtime.py`
Higher-level Forth runtime written *in Forth*:  
- Interpret loop (`INTERPRET-AGAIN`, `INTERPRET-ONCE`, etc.)  
- Message helpers (`SEND`, `WAIT-MSG`, `MSG-TYPE`, …)  
- Spawn helpers  
- Boot code for new VMs  

### `forthos_host_core.py`
Host manager responsible for:  
- Creating/killing VMs  
- Delivering messages between VMs  
- Mapping dot-commands  
- Handling stdout/stderr pumps  
- Handling VM snapshots  

### `forthos_tcp.py`
TCP server exposing host RPC:  
- Start with `python forthos_tcp.py serve`  
- JSON-based line protocol  
- `TuiTCPClient` wraps remote calls so the TUI behaves as if connected to a local host  

### `forthos_tui_prototype.py`
Full-screen TUI built with `prompt_toolkit`:  
- F1 doc viewer  
- F2 console (logs, VM list, input line, completion)  
- F3 editor  
- Persistent history  
- Completion across host commands, dot commands and Forth words  

### `forthos_tui_editor.py`
Editor implementation for F3:  
- Tabs  
- New/Open/Save/Save As  
- Close with confirmation  
- Clipboard (pyperclip when available, fallback otherwise)  
- Search  

### Example files
- `spawn-test.fth` showcases spawn + messaging.

---

## Installation

### Requirements
- Python 3.10+  
- Packages: `prompt_toolkit`, `pyperclip`  

Install:

```bash
pip install prompt_toolkit pyperclip
```

---

## Quick Start

### 1. Start the Host
```
python forthos_tcp.py serve
```

### 2. Start the TUI
```
python forthos_tui_prototype.py
```

### 3. Try simple Forth
```
1 2 + .
```

Define a word:
```
: SQUARE ( n -- n2 ) DUP * ;
5 SQUARE .
```

---

## TUI Overview

### Screens (F1/F2/F3)

- **F1 – Doc:** shows README.md  
- **F2 – Console:** logs, VM list, prompt  
- **F3 – Editor:** multi-tab editor  

### Console Screen (F2)
- VM logs panel  
- VM list  
- Prompt with history  
- Completion engine (cycles with Tab)

### Editor Screen (F3)
Key bindings:
- Ctrl-N new  
- Ctrl-O open  
- Ctrl-S save  
- F6 save-as  
- Ctrl-W close  
- F7/F8 switch tabs  
- Alt-C copy / Alt-V paste / Alt-X cut  
- F3 = find-next  

---

## Host Commands (`:host ...`)

```
:host new [name]
:host vm PID
:host ps
:host kill PID
:host gc-dead
:host logs [PID]
:host read-from "file"
:host .stack
:host reset
:host quit
```

Example:
```
:host new worker
:host vm 3
:host ps
:host read-from "spawn-test.fth"
```

---

## Dot Commands

Available dot-commands include:

```
.stack .rstack .dict .gc .history .objects .dumpdata .help
.load .save .see .word .bench .ver .bye
```

Examples:
```
.stack
.dict
.word SQUARE
.see INTERPRET-AGAIN
```

---

## ForthOS Language Basics

### Stack and Arithmetic
```
DROP DUP SWAP OVER ROT -ROT NIP TUCK
+ - * / MOD
= < > 0= 0< 0>
```

### Definitions & Control Flow
```
: NAME ... ;
IF ... ELSE ... THEN
BEGIN ... UNTIL
BEGIN ... WHILE ... REPEAT
CASE ... OF ... ENDOF ... ENDCASE
RECURSE
```

### Strings
```
S" Hello" TYPE
```

### Tables

Literal syntax:
```
{: "name" "Alice" "age" 42 ;}
```

Nested:
```
{:
  "user" {: "name" "Alice" "age" 42 ;}
  "active" -1
;}
```

Key ops:
```
T! ( val key table -- )
T@ ( table key -- val flag )
TKEYS ( table -- keys n )
TDROP, TDROPKEY
ASSIGN ( inside table literal )
```

### Buffers
Binary buffers with read/write operations and conversion helpers.

---

## Actor Model & Messaging

### Core words
```
SELF-PID
SPAWN
SEND
WAIT-MSG
MSG-TYPE MSG-FROM MSG-LINE MSG-DATA
```

### Spawn example
```
[: 
  SELF-PID ." child PID=" . CR
  BEGIN
    WAIT-MSG DUP MSG-TYPE S" line" STR= IF
      DUP MSG-LINE TYPE CR
    THEN DROP
  AGAIN
;] SPAWN CONSTANT CHILD
```

Send message:
```
TABLE CONSTANT M
M S" type" S" line" T!
M S" from" SELF-PID T!
M S" line" S" Hello child" T!
M CHILD SEND
```

---

## Snapshots & Persistence

```
.save "snapshot.json"
.load "snapshot.json"
```

Used to persist dictionaries + object space.

---

## Example Session

```
1 2 + .          \ → 3
: DOUBLE DUP + ;
7 DOUBLE .       \ → 14
```

Tables:
```
{: "name" "Alice" "age" 42 ;} CONSTANT USER
USER S" name" T@ DROP TYPE
```

Spawn:
```
[: WAIT-MSG MSG-LINE TYPE CR AGAIN ;] SPAWN CONSTANT C
```

Send to child:
```
TABLE CONSTANT T
T S" type" S" line" T!
T S" line" S" ping" T!
T C SEND
```

---

## Development Notes & Tests

- Each module contains `unittest` suites runnable via:
```
python MODULE.py
```
- `forthos_tcp.py test` runs TCP tests.
- Designed to be hackable and simple to extend.

---

## License

Choose a license (GNU GPL)
