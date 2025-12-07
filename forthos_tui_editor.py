#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.layout import HSplit, Window, DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea


# ======================================================================
# Onglet d'édition
# ======================================================================

@dataclass
class EditorTab:
    """
    Représente un fichier ouvert dans l'éditeur.
    """
    filename: Optional[str]
    display_name: str
    text_area: TextArea
    dirty: bool = False


# ======================================================================
# EditorScreen – écran F3 multi-onglets
# ======================================================================

class EditorScreen:
    """
    Éditeur texte F3, multi-onglets, avec ligne de commande inline
    en bas façon "commande" de Vim/Neovim.

    Pas de dialogs graphiques prompt_toolkit : tous les prompts
    (Save As, Open, Find, Replace, confirmation de fermeture)
    passent par la ligne de commande interne.

    API attendue par le ScreenManager :
      - .name
      - .container
      - .on_show() / .on_hide()
    """

    name: str = "editor"

    def __init__(self):
        # --- état onglets ---
        self.tabs: List[EditorTab] = []
        self.current_index: int = 0

        # messages (erreurs, infos) affichés dans la barre de status
        self._last_message: str = ""

        # état recherche / remplacement
        self._last_search_text: Optional[str] = None
        self._last_replace_text: Optional[str] = None

        # état de la ligne de commande
        self.command_mode: Optional[str] = None   # ex: 'saveas', 'open', 'find', ...
        self.command_label: str = ""              # texte explicatif affiché dans la status bar

        # Crée un premier onglet vide
        first_tab = self._create_tab(filename=None, text="")
        self.tabs.append(first_tab)
        self.current_index = 0

        # Barre d'onglets (top)
        self.tab_bar = Window(
            content=FormattedTextControl(self._render_tab_bar),
            height=1,
            style="reverse",
        )

        # Contenu éditable : TextArea de l'onglet courant
        self.editor_container = DynamicContainer(
            lambda: self.current_tab.text_area
        )

        # Ligne de commande en bas (inline)
        self.command_line = TextArea(
            text="",
            multiline=False,
            wrap_lines=False,
        )

        # Barre de status (bottom)
        self.status_bar = Window(
            content=FormattedTextControl(self._status_text),
            height=1,
            style="reverse",
        )

        # Layout global de l'éditeur
        self._container = HSplit(
            [
                self.tab_bar,
                Window(height=1, char="─"),
                self.editor_container,
                self.command_line,
                self.status_bar,
            ]
        )

    # ------------------------------------------------------------------
    # Propriété container (API compatible avec ScreenManager existant)
    # ------------------------------------------------------------------
    @property
    def container(self):
        return self._container

    @property
    def command_window(self):
        """
        Fenêtre interne associée à la ligne de commande.
        Utile pour les keybindings (détecter le focus).
        """
        return self.command_line.window

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    @property
    def current_tab(self) -> EditorTab:
        if not self.tabs:
            # sécurité : recréer un onglet au besoin
            new_tab = self._create_tab(filename=None, text="")
            self.tabs.append(new_tab)
            self.current_index = 0
        return self.tabs[self.current_index]

    def _create_tab(self, filename: Optional[str], text: str) -> EditorTab:
        """
        Crée un nouvel onglet (TextArea + gestion dirty).

        Important : on ne passe PAS buffer= à TextArea (non supporté
        par certaines versions de prompt_toolkit). On crée le TextArea
        avec text=, puis on récupère .buffer pour brancher le listener.
        """
        display_name = filename if filename else "(untitled)"

        text_area = TextArea(
            text=text,
            scrollbar=True,
            line_numbers=True,
        )

        tab = EditorTab(
            filename=filename,
            display_name=display_name,
            text_area=text_area,
            dirty=False,
        )

        # Gestion du dirty flag : dès qu'on modifie le buffer
        def _on_text_changed(_):
            if not tab.dirty:
                tab.dirty = True
                self._invalidate()

        text_area.buffer.on_text_changed += _on_text_changed

        return tab

    def _invalidate(self):
        """
        Demande un rafraîchissement visuel à l'application (safe).
        """
        try:
            app = get_app()
        except Exception:
            return
        try:
            app.invalidate()
        except Exception:
            pass

    def _set_message(self, text: str):
        self._last_message = text
        self._invalidate()

    # Ligne de commande : activation / validation / annulation -----

    def _activate_command(self, mode: str, initial_text: str = "", label: str = ""):
        """
        Active un mode de commande (saveas, open, find, replace, ...),
        pré-remplit la ligne et donne le focus à la commande.
        """
        self.command_mode = mode
        self.command_label = label
        self.command_line.text = initial_text or ""
        # place le curseur en fin
        buf = self.command_line.buffer
        buf.cursor_position = len(buf.text)
        try:
            app = get_app()
            app.layout.focus(self.command_line)
        except Exception:
            pass
        self._invalidate()

    def handle_command_enter(self):
        """
        Appelé par le keybinding ENTER quand le focus est sur la
        ligne de commande.
        """
        mode = self.command_mode
        text = self.command_line.text

        # reset état commande & focus vers l'éditeur
        self.command_mode = None
        self.command_label = ""
        self.command_line.text = ""
        try:
            app = get_app()
            app.layout.focus(self.current_tab.text_area)
        except Exception:
            pass

        if not mode:
            # pas de commande active -> rien
            return

        text_stripped = text.strip()

        # Dispatch selon le mode
        if mode == "saveas":
            if not text_stripped:
                self._set_message("Save As: cancelled")
            else:
                self._save_to_filename(text_stripped)

        elif mode == "open":
            if not text_stripped:
                self._set_message("Open: cancelled")
            else:
                self._open_file_from_path(text_stripped)

        elif mode == "close_confirm":
            if text_stripped.lower() in ("y", "yes", "o", "oui"):
                self._really_close_current_tab()
            else:
                self._set_message("Close cancelled")

        elif mode == "find":
            if not text_stripped:
                self._set_message("Find cancelled")
            else:
                self._last_search_text = text_stripped
                self._set_message(f"Searching for {text_stripped!r}")
                self.find_next(wrap=True)

        elif mode == "replace_search":
            if not text_stripped:
                self._set_message("Replace cancelled")
            else:
                self._last_search_text = text_stripped
                # deuxième étape : demande du texte de remplacement
                self._activate_command(
                    mode="replace_repl",
                    initial_text=self._last_replace_text or "",
                    label=f"Replace '{text_stripped}' with:",
                )

        elif mode == "replace_repl":
            # on accepte une chaîne vide comme remplacement
            self._last_replace_text = text
            self._replace_next_core()

        else:
            # mode inconnu : ignore
            self._set_message(f"Unknown command mode: {mode}")

    def cancel_command(self):
        """
        Annule le mode de commande courant (utilisé par ESC).
        """
        self.command_mode = None
        self.command_label = ""
        self.command_line.text = ""
        try:
            app = get_app()
            app.layout.focus(self.current_tab.text_area)
        except Exception:
            pass
        self._set_message("Command cancelled")

    # ------------------------------------------------------------------
    # Rendu : barre d'onglets + status
    # ------------------------------------------------------------------

    def _render_tab_bar(self):
        """
        Affiche les onglets, l'onglet courant marqué, et * si dirty.
        """
        if not self.tabs:
            return " [no file] "

        parts = []
        for i, tab in enumerate(self.tabs):
            prefix = "[" if i == self.current_index else " "
            suffix = "]" if i == self.current_index else " "
            name = tab.display_name or "(untitled)"
            if tab.dirty:
                name += "*"
            parts.append(f"{prefix}{name}{suffix}")

        return " ".join(parts)

    def _status_text(self) -> str:
        """
        Affiche infos sur l'onglet courant + dernier message +
        info sur la ligne de commande.
        """
        tab = self.current_tab
        name = tab.filename or "(untitled)"
        dirty = "*" if tab.dirty else ""
        msg = self._last_message or ""
        if self.command_mode:
            cmd_info = f"CMD[{self.command_mode}] {self.command_label}"
        else:
            cmd_info = ""
        shortcuts = "Ctrl-N:New  Ctrl-O:Open  Ctrl-S:Save  F6:Save As  Ctrl-W:Close  Ctrl-F:Find  F3:Next  Ctrl-R:Replace"
        return f"Editor - {name}{dirty} | {shortcuts} | {cmd_info} {msg}"

    # ------------------------------------------------------------------
    # Hooks écran
    # ------------------------------------------------------------------

    def on_show(self):
        """
        Appelé quand l'écran devient visible.
        Donne le focus à l'onglet courant.
        """
        try:
            app = get_app()
        except Exception:
            return
        try:
            app.layout.focus(self.current_tab.text_area)
        except Exception:
            pass

    def on_hide(self):
        """
        Rien de spécial pour le moment.
        """
        pass

    # ------------------------------------------------------------------
    # Gestion fichiers : New / Open / Save / Save As / Close
    # ------------------------------------------------------------------

    def new_file(self):
        """
        Crée un nouvel onglet vide et bascule dessus.
        """
        tab = self._create_tab(filename=None, text="")
        self.tabs.append(tab)
        self.current_index = len(self.tabs) - 1
        self._set_message("New file")
        self.on_show()

    # ---------- OPEN ----------

    def open_file(self, filename: Optional[str] = None):
        """
        API appelée par le keybinding.

        - Si filename est fourni : ouverture directe (synchrone).
        - Sinon : active le mode de commande 'open'.
        """
        if filename is not None:
            self._open_file_from_path(filename)
            return

        default = self.current_tab.filename or ""
        self._activate_command(
            mode="open",
            initial_text=default,
            label="Open file (ENTER to confirm, ESC to cancel)",
        )

    def _open_file_from_path(self, filename: str):
        filename = os.path.abspath(os.path.expanduser(filename))

        # Si déjà ouvert, basculer dessus
        for i, tab in enumerate(self.tabs):
            if tab.filename and os.path.abspath(tab.filename) == filename:
                self.current_index = i
                self._set_message(f"Switched to {filename}")
                self.on_show()
                return

        # Charger le contenu
        try:
            with open(filename, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            self._set_message(f"Open error: {e}")
            return

        # Créer l'onglet
        tab = self._create_tab(filename=filename, text=text)
        self.tabs.append(tab)
        self.current_index = len(self.tabs) - 1
        self._set_message(f"Opened {filename}")
        self.on_show()

    # ---------- SAVE ----------

    def save_file(self):
        """
        Sauvegarde l'onglet courant.

        - Si un nom de fichier existe déjà : save synchrone.
        - Sinon : active un prompt inline 'saveas'.
        """
        tab = self.current_tab
        if tab.filename:
            self._save_to_filename(tab.filename)
            return

        default = tab.filename or "forthos_edit.fth"
        self._activate_command(
            mode="saveas",
            initial_text=default,
            label="Save As (ENTER to confirm, ESC to cancel)",
        )

    def _save_to_filename(self, filename: str):
        tab = self.current_tab
        filename = os.path.abspath(os.path.expanduser(filename))
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(tab.text_area.text)
        except OSError as e:
            self._set_message(f"Save error: {e}")
            return

        tab.filename = filename
        tab.display_name = os.path.basename(filename)
        tab.dirty = False
        self._set_message(f"Saved {filename}")

    def save_file_as(self):
        """
        Démarre un prompt inline Save As, même si le fichier a déjà un nom.
        """
        tab = self.current_tab
        default = tab.filename or "forthos_edit.fth"
        self._activate_command(
            mode="saveas",
            initial_text=default,
            label="Save As (ENTER to confirm, ESC to cancel)",
        )

    # ---------- CLOSE ----------

    def close_current_tab(self):
        """
        Ferme l'onglet courant, avec confirmation si dirty.
        """
        if not self.tabs:
            return

        tab = self.current_tab

        if not tab.dirty:
            # Fermeture directe
            self._really_close_current_tab()
            return

        # Dirty -> prompt inline yes/no
        self._activate_command(
            mode="close_confirm",
            initial_text="",
            label=f"Discard unsaved changes in {tab.display_name}? (y/N)",
        )

    def _really_close_current_tab(self):
        if not self.tabs:
            return

        del self.tabs[self.current_index]

        if not self.tabs:
            new_tab = self._create_tab(filename=None, text="")
            self.tabs.append(new_tab)
            self.current_index = 0
        else:
            self.current_index = max(0, self.current_index - 1)

        self._set_message("Tab closed")
        self.on_show()

    # ------------------------------------------------------------------
    # Navigation entre onglets
    # ------------------------------------------------------------------

    def next_tab(self):
        """
        Onglet suivant (équivalent Ctrl-PageDown).
        """
        if not self.tabs:
            return
        self.current_index = (self.current_index + 1) % len(self.tabs)
        self._set_message("Next tab")
        self.on_show()

    def previous_tab(self):
        """
        Onglet précédent (équivalent Ctrl-PageUp).
        """
        if not self.tabs:
            return
        self.current_index = (self.current_index - 1) % len(self.tabs)
        self._set_message("Previous tab")
        self.on_show()

    # ------------------------------------------------------------------
    # Recherche / remplacement
    # ------------------------------------------------------------------

    def _current_buffer(self) -> Buffer:
        return self.current_tab.text_area.buffer

    def _default_search_text_from_cursor(self) -> str:
        """
        Si aucun terme de recherche enregistré, essaie de prendre le mot
        sous le curseur dans le buffer courant.
        """
        buf = self._current_buffer()
        doc = buf.document
        word = doc.get_word_under_cursor()
        return word or ""

    # ---------- Find (Ctrl-F) ----------

    def start_search(self):
        """
        Démarre un prompt inline pour la recherche.
        """
        default = self._last_search_text or self._default_search_text_from_cursor()
        self._activate_command(
            mode="find",
            initial_text=default,
            label="Find text (ENTER to confirm, ESC to cancel)",
        )

    # ---------- Find Next (F3) ----------

    def find_next(self, wrap: bool = True):
        """
        Trouve l'occurrence suivante du terme de recherche enregistré.
        Si wrap=True, repart du début si rien n'est trouvé après le curseur.
        """
        text = self._last_search_text
        if not text:
            self._set_message("No search text")
            return

        buf = self._current_buffer()
        doc = buf.document

        # Cherche après la position courante
        idx = doc.find(text, include_current_position=False, ignore_case=False)

        # Si pas trouvé et wrap demandé, recommencer depuis le début
        if idx is None and wrap:
            doc_start = Document(doc.text, cursor_position=0)
            idx = doc_start.find(text, include_current_position=False, ignore_case=False)

        if idx is None:
            self._set_message(f"'{text}' not found")
            return

        # Déplace le curseur sur le début du match
        new_doc = Document(doc.text, cursor_position=idx)
        buf.document = new_doc
        self._set_message(f"Found '{text}'")

    # ---------- Replace Next (Ctrl-R) ----------

    def replace_next(self):
        """
        Remplace la prochaine occurrence, avec prompts inline si besoin.

        1er Ctrl-R : demande le texte à chercher, si inconnu.
        2e étape : demande le texte de remplacement.
        Ensuite, remplacements successifs avec Ctrl-R direct.
        """
        if not self._last_search_text:
            default = self._default_search_text_from_cursor()
            self._activate_command(
                mode="replace_search",
                initial_text=default,
                label="Replace - search text (ENTER to confirm, ESC to cancel)",
            )
            return

        if self._last_replace_text is None:
            self._activate_command(
                mode="replace_repl",
                initial_text="",
                label=f"Replace '{self._last_search_text}' with (ENTER to confirm, ESC to cancel)",
            )
            return

        # Si on a déjà les deux infos, on remplace directement
        self._replace_next_core()

    def _replace_next_core(self):
        search = self._last_search_text
        repl = self._last_replace_text
        if search is None:
            self._set_message("No search text")
            return

        buf = self._current_buffer()
        doc = buf.document

        idx = doc.find(search, include_current_position=False, ignore_case=False)
        if idx is None:
            self._set_message(f"No more '{search}' to replace")
            return

        before = doc.text[:idx]
        after = doc.text[idx + len(search):]
        new_text = before + repl + after
        new_cursor = idx + len(repl)

        buf.document = Document(new_text, cursor_position=new_cursor)
        self.current_tab.dirty = True
        self._set_message(f"Replaced one '{search}'")

