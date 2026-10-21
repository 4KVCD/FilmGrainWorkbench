// What both tabs share around the frame: the frame pane (view buttons,
// zoom, the frame, navigation), its keys, the status bar, and making
// QImages of frames.
#pragma once

#include <functional>
#include <map>
#include <vector>

#include <QImage>
#include <QLabel>
#include <QMainWindow>
#include <QProgressBar>
#include <QPushButton>
#include <QShortcut>
#include <QSlider>
#include <QSpinBox>
#include <QWidget>

#include "fgw/display.h"
#include "fgw/picture.h"
#include "preview.h"

namespace fgw::app {

// "← → {left} / {right} · 1–4 choose view · …": the keys, under the frame.
QString shortcut_help(const QString& left, const QString& right);

// 125.5 s -> "2:05.500"; 3725 s -> "1:02:05.000".
QString timecode(double seconds);

// The frame as the screen shows it (converted on the calling thread).
QImage to_qimage(const Converter& converter, const Frame& frame);
// The grain added to `clean`, amplified 4 times, on grey.
QImage grain_only_image(const Plane& clean, const Plane& grained, int bit_depth);

// The viewer's keys on `widget` (a tab, or a frame window): ← / → the
// picture without and with grain, 1-4 a view, , and . a frame back and on,
// PgUp / PgDn a second (`second()` frames), + - 0 F the zoom.
std::vector<QShortcut*> install_viewer_shortcuts(QWidget* widget, FrameView* view,
                                                 std::function<void(View)> set_mode,
                                                 std::function<void(int)> step, std::function<int()> second);

// View buttons and zoom, the frame, an optional strip under it, and frame
// navigation. The pane only reports what was asked for; the tab decides
// and calls show_frame() / sync_modes().
class FramePane : public QWidget {
    Q_OBJECT

public:
    FramePane(PreviewState* state, const std::map<View, QString>& tips = {}, QWidget* below_view = nullptr,
              const QString& hint = {}, QWidget* parent = nullptr);

    FrameView* view() const { return view_; }
    QSlider* slider() const { return slider_; }
    QSpinBox* frame_box() const { return frame_box_; }
    QPushButton* mode_button(View view) const { return mode_buttons_.at(view); }

    void set_frame_count(std::int64_t frames);
    // Moves the slider and the frame box to `index` without asking for it again.
    void show_frame(std::int64_t index, double seconds);
    // Enables the view buttons of `available`; checks the one shown.
    void sync_modes(const std::vector<View>& available);

signals:
    void frame_requested(int index);
    void step_requested(int delta);
    void mode_requested(View view);
    void popout_requested();

private:
    PreviewState* state_;
    FrameView* view_;
    QSlider* slider_;
    QSpinBox* frame_box_;
    QLabel* time_label_;
    std::map<View, QPushButton*> mode_buttons_;
};

// A tab's status line, with a progress bar and a Cancel button that show
// while a job runs.
class StatusBar {
public:
    StatusBar(QMainWindow* window, std::function<void()> on_cancel, int progress_width = 320);

    QLabel* label() const { return label_; }
    QPushButton* cancel_button() const { return cancel_; }
    void set_text(const QString& text) { label_->setText(text); }
    // Shows the progress (negative: busy without a percentage) and Cancel.
    void busy(double fraction = -1);
    void idle();

private:
    QLabel* label_;
    QProgressBar* progress_;
    QPushButton* cancel_;
};

}  // namespace fgw::app
