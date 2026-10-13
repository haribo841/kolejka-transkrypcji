"""A persistent, visible session indicator that cannot launch duplicate workers."""
import tkinter as tk

from .ui_help import Tooltip


class SessionButton(tk.Button):
    def __init__(self, parent, text, command, help_text, status=None):
        super().__init__(parent, text=text, command=command, padx=12, pady=5,
                         font=("Segoe UI", 10, "bold"), foreground="white",
                         activeforeground="white", borderwidth=2, takefocus=True)
        self.idle_text = text
        Tooltip(self, help_text, status)
        self.show_session()

    def state(self, statespec=None):
        if statespec is not None:
            if "disabled" in statespec:
                self.configure(state="disabled")
            elif "!disabled" in statespec:
                self.configure(state="normal")
        states = ["disabled"] if self.cget("state") == "disabled" else []
        if self.cget("relief") == "sunken":
            states.append("pressed")
        return tuple(states)

    def instate(self, statespec):
        states = self.state()
        return all(item[1:] not in states if item.startswith("!") else item in states for item in statespec)

    def show_session(self, *, active=False, starting=False, stopping=False, enabled=True):
        latched = active or starting
        text = self.idle_text
        color = "#1465a3" if enabled else "#707781"
        if latched:
            text = "Uruchamianie..." if starting else "Sesja trwa"
            color = "#25723b"
            if stopping:
                text, color = "Dokończę bieżący i zatrzymam", "#916100"
        self.configure(text=text, relief="sunken" if latched else "raised",
                       background=color, activebackground=color,
                       disabledforeground="white", state="disabled" if latched or not enabled else "normal")
