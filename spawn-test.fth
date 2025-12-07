: loop 0 BEGIN DUP . 1+ AGAIN ;
[: loop ;] CONSTANT child-entry
: spawn-child child-entry SPAWN "child: " . . CR ;
: spawn-loop BEGIN spawn-child 1- DUP 0= UNTIL ;
