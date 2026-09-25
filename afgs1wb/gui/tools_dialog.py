"""Dialog for locating the external programs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)

from ..tools import Tools, detect_tools

PURPOSE = {
    "ffmpeg": "decodes frames",
    "ffprobe": "reads video properties",
    "mkvmerge": "writes the output MKV (MKVToolNix)",
}
WHERE = {
    "mkvmerge": "Install MKVToolNix from mkvtoolnix.download.",
    "ffmpeg": "Any recent build with VVC decoding (e.g. gyan.dev full build).",
    "ffprobe": "Comes with ffmpeg.",
}


class ToolsDialog(QDialog):
    def __init__(self, tools: Tools, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("External programs")
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)
        intro = QLabel("The workbench drives these programs. Paths are found automatically "
                       "where possible and remembered.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid = QGridLayout()
        self.edits: dict[str, QLineEdit] = {}
        self.status: dict[str, QLabel] = {}
        for row, name in enumerate(Tools.LABELS):
            label = QLabel(f"<b>{Tools.LABELS[name]}</b><br><small>{PURPOSE[name]}</small>")
            edit = QLineEdit(getattr(tools, name))
            edit.setToolTip(WHERE[name])
            edit.textChanged.connect(self._refresh)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, n=name: self._browse(n))
            status = QLabel()
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(browse, row, 2)
            grid.addWidget(status, row, 3)
            self.edits[name], self.status[name] = edit, status
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        detect = buttons.addButton("Find automatically", QDialogButtonBox.ButtonRole.ActionRole)
        detect.clicked.connect(self._detect)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _browse(self, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Locate {Tools.LABELS[name]}",
                                              self.edits[name].text(), "Programs (*.exe);;All files (*)")
        if path:
            self.edits[name].setText(path)

    def _detect(self) -> None:
        found = detect_tools()
        for name, edit in self.edits.items():
            if not Path(edit.text()).is_file() and getattr(found, name):
                edit.setText(getattr(found, name))

    def _refresh(self) -> None:
        for name, edit in self.edits.items():
            ok = Path(edit.text()).is_file()
            self.status[name].setText("✔ found" if ok else "✖ missing")
            self.status[name].setStyleSheet(f"color: {'#6fcf8f' if ok else '#ff7b72'};")

    def tools(self) -> Tools:
        return Tools(**{name: edit.text().strip() for name, edit in self.edits.items()})
