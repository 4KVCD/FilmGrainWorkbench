"""Drive the real main window through the whole workflow on the small test clip."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, condition, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_full_workflow(qapp, tools, need, grainy_source, grainy_clip, tmp_path):
    need("ffmpeg", "ffprobe", "noise_model", "mkvmerge")
    from afgs1wb.gui.main_window import MainWindow
    from afgs1wb.gui.viewer import ENCODED, GRAIN, SOURCE

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    w.show()
    idle = lambda: not (w.frame_job and w.frame_job.isRunning()) and not w.frame_timer.isActive()

    w.source_edit.setText(str(grainy_source))
    w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip))
    w._load_video("encoded")
    wait_for(qapp, lambda: idle() and SOURCE in w.state.images and ENCODED in w.state.images)
    assert not w.write_button.isEnabled()               # nothing to write before analysis
    assert w.analyse_button.isEnabled()

    w.runs_spin.setValue(3)
    w.frames_spin.setValue(6)
    w._analyse()
    wait_for(qapp, lambda: not w._busy() and w.model is not None)
    wait_for(qapp, lambda: idle() and GRAIN in w.state.images)
    assert "3 of 3 places used" in w.analysis_label.text()

    QTest.keyClick(w, Qt.Key.Key_Right)
    assert w.state.mode == GRAIN
    QTest.keyClick(w, Qt.Key.Key_Left)
    assert w.state.mode == ENCODED

    start = w.current_index
    QTest.keyClick(w, Qt.Key.Key_Period)
    wait_for(qapp, lambda: idle() and w.current_index == start + 1)

    w.luma_spin.setValue(250)
    wait_for(qapp, lambda: idle() and not w.tune_timer.isActive())
    assert w.plan.params_at(w.current_index).y_points

    w.output_edit.setText(str(tmp_path / "out.mkv"))
    w._write()
    wait_for(qapp, lambda: not w._busy() and w.written is not None)
    assert (tmp_path / "out.mkv").is_file()
    w._verify()
    wait_for(qapp, lambda: not w._busy() and "matches the preview exactly" in w.write_label.text())
    w.close()


def test_every_frame_workflow(qapp, tools, need, grainy_source, grainy_clip, tmp_path, monkeypatch):
    need("ffmpeg", "ffprobe", "noise_model", "mkvmerge")
    import afgs1wb.fullscan as fullscan
    from afgs1wb.gui.main_window import MainWindow
    from afgs1wb.gui.viewer import GRAIN

    if not fullscan.analyzer_path().is_file():
        pytest.skip("grain analyzer not built")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    w.show()
    idle = lambda: not (w.frame_job and w.frame_job.isRunning()) and not w.frame_timer.isActive()
    w.source_edit.setText(str(grainy_source))
    w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip))
    w._load_video("encoded")
    wait_for(qapp, idle)

    w.full_radio.setChecked(True)
    assert w.full_options.isVisibleTo(w) and not w.quick_options.isVisibleTo(w)
    assert w.analyse_button.text() == "Analyse every frame"
    w.gpu_check.setChecked(False)
    w._analyse()
    wait_for(qapp, lambda: not w._busy() and w.model is not None)
    wait_for(qapp, lambda: idle() and GRAIN in w.state.images)
    assert "Every frame analysed" in w.analysis_label.text()
    assert w.grain_timeline.isVisibleTo(w)
    assert "At this frame" in w.tune_label.text()

    w.grain_timeline.frame_clicked.emit(30)
    wait_for(qapp, lambda: idle() and w.current_index == 30)

    w.output_edit.setText(str(tmp_path / "every.mkv"))
    w._write()
    wait_for(qapp, lambda: not w._busy() and w.written is not None)
    w._verify()
    wait_for(qapp, lambda: not w._busy() and "matches the preview exactly" in w.write_label.text())

    # reopening the same files restores the every-frame model
    w.close()
    w2 = MainWindow(settings, tmp_path / "data")
    wait_for(qapp, lambda: w2.model is not None, timeout=30)
    assert "previous every-frame analysis" in w2.analysis_label.text()
    w2.close()
