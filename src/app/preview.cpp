#include "preview.h"

#include <algorithm>
#include <array>
#include <cmath>

#include <QGuiApplication>
#include <QKeySequence>
#include <QMouseEvent>
#include <QPainter>
#include <QScreen>
#include <QShortcut>
#include <QVBoxLayout>

namespace fgw::app {

namespace {

constexpr std::array<double, 14> zoom_steps = {0.125, 0.25, 1.0 / 3, 0.5, 2.0 / 3, 1.0, 1.5,
                                               2.0,   3.0,  4.0,     6.0, 8.0,     12.0, 16.0};
const QColor background(12, 14, 16);

}  // namespace

// ---------------------------------------------------------------- PreviewState

PreviewState::PreviewState(ViewNames names, QObject* parent)
    : QObject(parent), names_(std::move(names)), placeholder_("Choose a source and an encoded video to see frames.") {}

QImage PreviewState::image() const {
    const auto found = images_.find(mode_);
    return found == images_.end() ? QImage() : found->second;
}

void PreviewState::set_images(std::map<View, QImage> images, QString caption) {
    images_ = std::move(images);
    caption_ = std::move(caption);
    if (!images_.contains(mode_)) {
        mode_ = images_.contains(encoded_view) || images_.empty() ? encoded_view : images_.begin()->first;
    }
    emit changed();
}

void PreviewState::update_images(const std::map<View, QImage>& images) {
    for (const auto& [view, image] : images) {
        if (image.isNull()) {
            images_.erase(view);
        } else {
            images_[view] = image;
        }
    }
    emit changed();
}

void PreviewState::clear(QString placeholder) {
    images_.clear();
    placeholder_ = std::move(placeholder);
    emit changed();
}

bool PreviewState::set_mode(View view) {
    if (!images_.contains(view)) {
        return false;
    }
    mode_ = view;
    emit changed();
    return true;
}

// ---------------------------------------------------------------- FrameView

FrameView::FrameView(PreviewState* state, QWidget* parent)
    : QWidget(parent), state_(state), overlay_(new QLabel(this)), message_(new QLabel(this)) {
    setFocusPolicy(Qt::StrongFocus);
    setAttribute(Qt::WA_OpaquePaintEvent);  // paintEvent covers every pixel
    setCursor(Qt::OpenHandCursor);
    overlay_->setStyleSheet(
        "background: rgba(0,0,0,150); color: #f2f2f2; padding: 4px 8px; border-radius: 4px; font-size: 10pt;");
    overlay_->move(10, 10);
    message_->setAlignment(Qt::AlignCenter);
    message_->setWordWrap(true);
    message_->setStyleSheet("color: #9aa4ad; font-size: 11pt;");
    connect(state_, &PreviewState::changed, this, &FrameView::refresh);
    refresh();
}

double FrameView::fit_zoom() const {
    if (image_size_.isEmpty()) {
        return 1.0;
    }
    return std::min(static_cast<double>(width()) / image_size_.width(),
                    static_cast<double>(height()) / image_size_.height());
}

void FrameView::refresh() {
    const QImage image = state_->image();
    if (image.isNull()) {
        overlay_->hide();
        message_->setText(state_->placeholder());
        message_->setGeometry(20, 0, std::max(10, width() - 40), height());
        message_->show();
        update();
        return;
    }
    message_->hide();
    if (image.size() != image_size_) {
        const bool first = image_size_.isEmpty();
        image_size_ = image.size();
        if (fit_ || first) {
            fit();
        } else {
            clamp_offset();
        }
    }
    update_overlay();
    update();
}

void FrameView::update_overlay() {
    if (state_->image().isNull()) {
        return;
    }
    const QString zoom = fit_ ? QStringLiteral("fit") : QString::number(std::lround(zoom_ * 100)) + "%";
    overlay_->setText("<b>" + state_->names().at(state_->mode()) + "</b> &nbsp; " + state_->caption() +
                      " &nbsp; " + zoom);
    overlay_->adjustSize();
    overlay_->show();
}

void FrameView::clamp_offset() {
    const QSizeF shown = QSizeF(image_size_) * zoom_;
    // Centred along a side the picture does not fill; otherwise kept covering the view.
    const auto clamp_axis = [](double offset, double shown_length, double view_length) {
        if (shown_length <= view_length) {
            return (view_length - shown_length) / 2;
        }
        return std::clamp(offset, view_length - shown_length, 0.0);
    };
    offset_.setX(clamp_axis(offset_.x(), shown.width(), width()));
    offset_.setY(clamp_axis(offset_.y(), shown.height(), height()));
}

void FrameView::fit() {
    fit_ = true;
    zoom_ = fit_zoom();
    offset_ = {};
    clamp_offset();
    update_overlay();
    update();
}

void FrameView::zoom_at(double zoom, QPointF anchor) {
    zoom = std::clamp(zoom, zoom_steps.front(), zoom_steps.back());
    // The picture point under `anchor` stays under it.
    const QPointF picture_point = (anchor - offset_) / zoom_;
    zoom_ = zoom;
    fit_ = false;
    offset_ = anchor - picture_point * zoom_;
    clamp_offset();
    update_overlay();
    update();
}

void FrameView::set_zoom(double zoom) { zoom_at(zoom, QPointF(width() / 2.0, height() / 2.0)); }

namespace {

double next_step(double current, int direction) {
    if (direction > 0) {
        const auto up = std::find_if(zoom_steps.begin(), zoom_steps.end(), [current](double z) { return z > current * 1.001; });
        return up == zoom_steps.end() ? zoom_steps.back() : *up;
    }
    const auto down = std::find_if(zoom_steps.rbegin(), zoom_steps.rend(), [current](double z) { return z < current / 1.001; });
    return down == zoom_steps.rend() ? zoom_steps.front() : *down;
}

}  // namespace

void FrameView::step_zoom(int direction) { set_zoom(next_step(zoom_, direction)); }

void FrameView::paintEvent(QPaintEvent*) {
    QPainter painter(this);
    painter.fillRect(rect(), background);
    const QImage image = state_->image();
    if (image.isNull()) {
        return;
    }
    painter.setRenderHint(QPainter::SmoothPixmapTransform, zoom_ < 1.0);
    painter.drawImage(QRectF(offset_, QSizeF(image.size()) * zoom_), image);
}

void FrameView::resizeEvent(QResizeEvent* event) {
    QWidget::resizeEvent(event);
    if (fit_) {
        fit();
    } else {
        clamp_offset();
    }
    message_->setGeometry(20, 0, std::max(10, width() - 40), height());
}

void FrameView::wheelEvent(QWheelEvent* event) {
    zoom_at(next_step(zoom_, event->angleDelta().y() > 0 ? 1 : -1), event->position());
}

void FrameView::mousePressEvent(QMouseEvent* event) {
    if (event->button() == Qt::LeftButton) {
        dragging_ = true;
        drag_from_ = event->position();
        setCursor(Qt::ClosedHandCursor);
    }
}

void FrameView::mouseMoveEvent(QMouseEvent* event) {
    if (!dragging_) {
        return;
    }
    offset_ += event->position() - drag_from_;
    drag_from_ = event->position();
    clamp_offset();
    update();
}

void FrameView::mouseReleaseEvent(QMouseEvent* event) {
    if (event->button() == Qt::LeftButton) {
        dragging_ = false;
        setCursor(Qt::OpenHandCursor);
    }
}

void FrameView::mouseDoubleClickEvent(QMouseEvent* event) {
    if (fit_) {
        zoom_at(1.0, event->position());
    } else {
        fit();
    }
}

// ---------------------------------------------------------------- ViewerWindow

ViewerWindow::ViewerWindow(PreviewState* state, QWidget* parent)
    : QWidget(parent, Qt::Window), state_(state), view_(new FrameView(state, this)) {
    setWindowTitle("Frame viewer");
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->addWidget(view_);
    connect(state_, &PreviewState::changed, this, &ViewerWindow::update_title);
    new QShortcut(QKeySequence(Qt::Key_Escape), this, this, &QWidget::close);
}

void ViewerWindow::update_title() {
    setWindowTitle(state_->image().isNull() ? QStringLiteral("Frame viewer")
                                            : state_->names().at(state_->mode()) + " — " + state_->caption());
}

void ViewerWindow::show_sized() {
    const QRect screen = QGuiApplication::primaryScreen()->availableGeometry();
    const QImage image = state_->image();
    if (!isVisible()) {
        const QSize size = image.isNull() ? QSize(1280, 720) : image.size();
        resize(std::min(size.width(), static_cast<int>(screen.width() * 0.9)),
               std::min(size.height(), static_cast<int>(screen.height() * 0.9)));
    }
    show();
    raise();
    activateWindow();
    if (!image.isNull() && image.width() <= view_->width() + 2) {
        view_->set_zoom(1.0);
    } else {
        view_->fit();
    }
}

}  // namespace fgw::app
