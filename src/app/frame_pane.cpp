#include "frame_pane.h"

#include <cmath>

#include <QButtonGroup>
#include <QHBoxLayout>
#include <QKeySequence>
#include <QSizePolicy>
#include <QStatusBar>
#include <QVBoxLayout>

namespace fgw::app {

QString shortcut_help(const QString& left, const QString& right) {
    return "← → " + left + " / " + right +
           " · 1–4 choose view · , . previous / next frame · PgUp PgDn ±1 s · wheel or + − zoom · 0 = 100 % · "
           "F fit · drag to pan";
}

QString timecode(double seconds) {
    const int hours = static_cast<int>(seconds / 3600);
    const double rest = std::fmod(seconds, 3600.0);
    const int minutes = static_cast<int>(rest / 60);
    const double secs = std::fmod(rest, 60.0);
    return hours ? QString::asprintf("%d:%02d:%06.3f", hours, minutes, secs)
                 : QString::asprintf("%d:%06.3f", minutes, secs);
}

QImage to_qimage(const Converter& converter, const Frame& frame) {
    QImage image(frame.geometry.width, frame.geometry.height, QImage::Format_RGB32);
    converter.to_rgb(frame, {reinterpret_cast<std::uint32_t*>(image.bits()), image.bytesPerLine() / 4});
    return image;
}

QImage grain_only_image(const Plane& clean, const Plane& grained, int bit_depth) {
    QImage image(clean.width, clean.height, QImage::Format_RGB32);
    grain_only(clean, grained, bit_depth, {reinterpret_cast<std::uint32_t*>(image.bits()), image.bytesPerLine() / 4});
    return image;
}

std::vector<QShortcut*> install_viewer_shortcuts(QWidget* widget, FrameView* view, std::function<void(View)> set_mode,
                                                 std::function<void(int)> step, std::function<int()> second) {
    std::vector<QShortcut*> shortcuts;
    const auto add = [&](const QKeySequence& key, std::function<void()> action) {
        shortcuts.push_back(new QShortcut(key, widget, widget, std::move(action)));
    };
    add(QKeySequence(Qt::Key_Left), [set_mode] { set_mode(encoded_view); });
    add(QKeySequence(Qt::Key_Right), [set_mode] { set_mode(grain_view); });
    for (const View view_key : all_views) {
        add(QKeySequence(QString::number(static_cast<int>(view_key) + 1)), [set_mode, view_key] { set_mode(view_key); });
    }
    add(QKeySequence(","), [step] { step(-1); });
    add(QKeySequence("."), [step] { step(1); });
    add(QKeySequence(Qt::Key_PageUp), [step, second] { step(-second()); });
    add(QKeySequence(Qt::Key_PageDown), [step, second] { step(second()); });
    add(QKeySequence("+"), [view] { view->step_zoom(1); });
    add(QKeySequence("="), [view] { view->step_zoom(1); });
    add(QKeySequence("-"), [view] { view->step_zoom(-1); });
    add(QKeySequence("0"), [view] { view->set_zoom(1.0); });
    add(QKeySequence("F"), [view] { view->fit(); });
    return shortcuts;
}

namespace {

QPushButton* flat_button(const QString& text, QWidget* parent) {
    auto* button = new QPushButton(text, parent);
    button->setFocusPolicy(Qt::NoFocus);
    return button;
}

}  // namespace

FramePane::FramePane(PreviewState* state, const std::map<View, QString>& tips, QWidget* below_view,
                     const QString& hint, QWidget* parent)
    : QWidget(parent), state_(state), view_(new FrameView(state, this)) {
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(0, 8, 8, 8);

    auto* bar = new QHBoxLayout;
    auto* group = new QButtonGroup(this);
    for (const View view_key : all_views) {
        auto* button = flat_button(state->names().at(view_key) + "  [" + QString::number(static_cast<int>(view_key) + 1) + "]",
                                   this);
        button->setCheckable(true);
        if (const auto tip = tips.find(view_key); tip != tips.end()) {
            button->setToolTip(tip->second);
        }
        connect(button, &QPushButton::clicked, this, [this, view_key] { emit mode_requested(view_key); });
        group->addButton(button);
        mode_buttons_[view_key] = button;
        bar->addWidget(button);
    }
    bar->addStretch(1);
    const std::pair<QString, std::function<void()>> zooms[] = {
        {"Fit", [this] { view_->fit(); }},
        {"100%", [this] { view_->set_zoom(1); }},
        {"200%", [this] { view_->set_zoom(2); }},
        {"400%", [this] { view_->set_zoom(4); }}};
    for (const auto& [text, action] : zooms) {
        auto* button = flat_button(text, this);
        connect(button, &QPushButton::clicked, this, action);
        bar->addWidget(button);
    }
    auto* popout = flat_button("Open in window", this);
    popout->setToolTip("Show the frame on its own in a separate, resizable window.");
    connect(popout, &QPushButton::clicked, this, &FramePane::popout_requested);
    bar->addWidget(popout);
    layout->addLayout(bar);

    view_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    layout->addWidget(view_, 1);
    if (below_view != nullptr) {
        layout->addWidget(below_view);
    }

    auto* navigation = new QHBoxLayout;
    auto* previous = flat_button("◀", this);
    auto* following = flat_button("▶", this);
    for (auto [button, delta] : {std::pair{previous, -1}, std::pair{following, 1}}) {
        button->setFixedWidth(34);
        connect(button, &QPushButton::clicked, this, [this, delta = delta] { emit step_requested(delta); });
    }
    slider_ = new QSlider(Qt::Horizontal, this);
    slider_->setFocusPolicy(Qt::NoFocus);
    connect(slider_, &QSlider::valueChanged, this, &FramePane::frame_requested);
    frame_box_ = new QSpinBox(this);
    frame_box_->setRange(0, 0);
    frame_box_->setKeyboardTracking(false);
    connect(frame_box_, &QSpinBox::valueChanged, this, &FramePane::frame_requested);
    time_label_ = new QLabel("0:00.000", this);
    time_label_->setMinimumWidth(90);
    navigation->addWidget(previous);
    navigation->addWidget(slider_, 1);
    navigation->addWidget(following);
    navigation->addWidget(new QLabel("Frame", this));
    navigation->addWidget(frame_box_);
    navigation->addWidget(time_label_);
    layout->addLayout(navigation);
    if (!hint.isEmpty()) {
        auto* label = new QLabel(hint, this);
        label->setObjectName("info");
        layout->addWidget(label);
    }
}

void FramePane::set_frame_count(std::int64_t frames) {
    const int last = static_cast<int>(std::max<std::int64_t>(0, frames - 1));
    const QSignalBlocker block_slider(slider_);
    const QSignalBlocker block_box(frame_box_);
    slider_->setMaximum(last);
    frame_box_->setMaximum(last);
}

void FramePane::show_frame(std::int64_t index, double seconds) {
    const QSignalBlocker block_slider(slider_);
    const QSignalBlocker block_box(frame_box_);
    slider_->setValue(static_cast<int>(index));
    frame_box_->setValue(static_cast<int>(index));
    time_label_->setText(timecode(seconds));
}

void FramePane::sync_modes(const std::vector<View>& available) {
    for (const auto& [view_key, button] : mode_buttons_) {
        button->setEnabled(std::find(available.begin(), available.end(), view_key) != available.end());
        button->setChecked(view_key == state_->mode() && !state_->images().empty());
    }
}

StatusBar::StatusBar(QMainWindow* window, std::function<void()> on_cancel, int progress_width)
    : label_(new QLabel("Ready.", window)), progress_(new QProgressBar(window)), cancel_(new QPushButton("Cancel", window)) {
    progress_->setMaximumWidth(progress_width);
    progress_->setTextVisible(true);
    progress_->hide();
    cancel_->hide();
    QObject::connect(cancel_, &QPushButton::clicked, window, std::move(on_cancel));
    QStatusBar* bar = window->statusBar();
    bar->addWidget(label_, 1);
    bar->addPermanentWidget(progress_);
    bar->addPermanentWidget(cancel_);
}

void StatusBar::busy(double fraction) {
    if (fraction < 0) {
        progress_->setRange(0, 0);
    } else {
        progress_->setRange(0, 1000);
        progress_->setValue(static_cast<int>(fraction * 1000));
    }
    progress_->show();
    cancel_->show();
    cancel_->setEnabled(true);
}

void StatusBar::idle() {
    progress_->hide();
    cancel_->hide();
}

}  // namespace fgw::app
