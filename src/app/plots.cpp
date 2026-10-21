#include "plots.h"

#include <algorithm>
#include <cmath>

#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QSizePolicy>

namespace fgw::app {

namespace {

const QColor luma_color(235, 235, 235);
const QColor cb_color(96, 160, 255);
const QColor cr_color(255, 110, 96);
const QColor panel(24, 27, 31);
const QColor grid_color(60, 66, 72);
const QColor text_color(140, 148, 156);
const QColor timeline_color(159, 211, 199);
const QColor current_color(255, 200, 90);

// A round axis top at or above `value`.
double nice_top(double value) {
    for (const double step : {0.5, 1.0, 2.0, 2.5, 4.0, 5.0, 8.0, 10.0, 16.0, 20.0, 32.0, 40.0, 64.0}) {
        if (value <= step) {
            return step;
        }
    }
    return std::ceil(value / 16) * 16;
}

void use_small_font(QPainter& painter) {
    QFont small = painter.font();
    small.setPointSizeF(std::max(7.0, small.pointSizeF() - 1.5));
    painter.setFont(small);
}

}  // namespace

CurvePlot::CurvePlot(QWidget* parent)
    : QWidget(parent), empty_text_("The grain of the frame shown is estimated when it loads.") {
    setMinimumHeight(170);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    setToolTip("Grain standard deviation the decoder will add at each brightness for the frame shown, in 8-bit "
               "code values, at the strength set below.\nWhite: luma. Blue/red: Cb/Cr.");
}

void CurvePlot::set_data(const std::array<double, 256>& luma, const std::array<double, 256>& cb,
                         const std::array<double, 256>& cr) {
    curves_ = {{luma, luma_color, 2.0}, {cb, cb_color, 1.5}, {cr, cr_color, 1.5}};
    update();
}

void CurvePlot::clear(const QString& text) {
    curves_.clear();
    if (!text.isEmpty()) {
        empty_text_ = text;
    }
    update();
}

void CurvePlot::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    p.fillRect(rect(), panel);
    use_small_font(p);
    if (curves_.empty()) {
        p.setPen(text_color);
        p.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap, empty_text_);
        return;
    }
    const QRectF area(34, 8, width() - 44, height() - 34);
    double top = 0.5;
    for (const Curve& curve : curves_) {
        top = std::max(top, *std::max_element(curve.values.begin(), curve.values.end()));
    }
    top = nice_top(top * 1.1);
    for (int i = 0; i < 5; ++i) {
        const double y = area.bottom() - area.height() * i / 4;
        p.setPen(QPen(grid_color, 1));
        p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y));
        p.setPen(text_color);
        p.drawText(QRectF(0, y - 8, 30, 16), Qt::AlignRight | Qt::AlignVCenter, QString::number(top * i / 4, 'f', 1));
    }
    p.setPen(text_color);
    for (const int x : {0, 64, 128, 192, 255}) {
        const double px = area.left() + area.width() * x / 255;
        p.drawText(QRectF(px - 20, area.bottom() + 2, 40, 14), Qt::AlignCenter, QString::number(x));
    }
    p.drawText(QRectF(area.left(), area.bottom() + 13, area.width(), 14), Qt::AlignCenter,
               "brightness (8-bit)   ·   σ in 8-bit code values");
    for (auto curve = curves_.rbegin(); curve != curves_.rend(); ++curve) {
        QPainterPath path;
        for (int i = 0; i < 256; ++i) {
            const QPointF point(area.left() + area.width() * i / 255,
                                area.bottom() - area.height() * std::min(curve->values[static_cast<std::size_t>(i)], top) / top);
            if (i == 0) {
                path.moveTo(point);
            } else {
                path.lineTo(point);
            }
        }
        p.setPen(QPen(curve->color, curve->width));
        p.drawPath(path);
    }
    // The legend, right-aligned: a short line, then the name, for each curve.
    double x = area.right() - 6;
    const std::pair<const char*, QColor> legend[] = {{"Luma", luma_color}, {"Cb", cb_color}, {"Cr", cr_color}};
    for (auto entry = std::rbegin(legend); entry != std::rend(legend); ++entry) {
        x -= p.fontMetrics().horizontalAdvance(entry->first);
        p.setPen(entry->second);
        p.drawText(QPointF(x, area.top() + 12), entry->first);
        x -= 16;
        p.setPen(QPen(entry->second, 2));
        p.drawLine(QPointF(x, area.top() + 8), QPointF(x + 12, area.top() + 8));
        x -= 10;
    }
}

GrainTimeline::GrainTimeline(QWidget* parent) : QWidget(parent) {
    setMinimumHeight(64);
    setMaximumHeight(90);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    setCursor(Qt::PointingHandCursor);
    setToolTip("Grain strength (σ at mid-grey, 8-bit code values) over the whole video, after the strength "
               "setting. Click or drag to jump to that frame.");
}

void GrainTimeline::set_data(std::int64_t frames, std::vector<std::int64_t> starts, std::vector<double> sigmas) {
    frames_ = std::max<std::int64_t>(1, frames);
    starts_ = std::move(starts);
    sigmas_ = std::move(sigmas);
    update();
}

void GrainTimeline::set_current(std::int64_t index) {
    current_ = index;
    update();
}

std::vector<double> GrainTimeline::column_values(int columns) const {
    std::vector<double> values(static_cast<std::size_t>(columns), 0.0);
    if (starts_.empty()) {
        return values;
    }
    for (int c = 0; c < columns; ++c) {
        const double frame = static_cast<double>(frames_) * c / columns;  // numpy.linspace(0, frames, columns + 1)[c]
        const auto after = std::upper_bound(starts_.begin(), starts_.end(), frame,
                                            [](double f, std::int64_t start) { return f < static_cast<double>(start); });
        const auto i = std::clamp<std::ptrdiff_t>((after - starts_.begin()) - 1, 0,
                                                   static_cast<std::ptrdiff_t>(sigmas_.size()) - 1);
        values[static_cast<std::size_t>(c)] = sigmas_[static_cast<std::size_t>(i)];
    }
    return values;
}

void GrainTimeline::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    p.fillRect(rect(), panel);
    const QRectF area(34, 6, width() - 40, height() - 12);
    const std::vector<double> values = column_values(std::max(2, static_cast<int>(area.width())));
    // The axis top: the 99.5th percentile (as numpy's, interpolated), with room.
    std::vector<double> sorted = values;
    std::sort(sorted.begin(), sorted.end());
    const double rank = 0.995 * static_cast<double>(sorted.size() - 1);
    const auto below = static_cast<std::size_t>(rank);
    const double percentile =
        sorted[below] + (sorted[std::min(below + 1, sorted.size() - 1)] - sorted[below]) * (rank - static_cast<double>(below));
    const double top = std::max(0.5, percentile * 1.15);
    p.setPen(text_color);
    use_small_font(p);
    p.drawText(QRectF(0, area.top() - 4, 30, 14), Qt::AlignRight, QString::number(top, 'f', 1));
    p.drawText(QRectF(0, area.bottom() - 10, 30, 14), Qt::AlignRight, "0");
    QPainterPath path(QPointF(area.left(), area.bottom()));
    for (std::size_t i = 0; i < values.size(); ++i) {
        const double x = area.left() + area.width() * static_cast<double>(i) / static_cast<double>(values.size() - 1);
        path.lineTo(QPointF(x, area.bottom() - area.height() * std::min(values[i], top) / top));
    }
    path.lineTo(QPointF(area.right(), area.bottom()));
    QColor fill = timeline_color;
    fill.setAlpha(90);
    p.fillPath(path, fill);
    p.setPen(QPen(timeline_color, 1));
    p.drawPath(path);
    const double x = area.left() + area.width() * static_cast<double>(current_) / static_cast<double>(std::max<std::int64_t>(1, frames_ - 1));
    p.setPen(QPen(current_color, 2));
    p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()));
}

void GrainTimeline::emit_at(double x) {
    const double area_width = std::max(1, width() - 40);
    const double fraction = std::clamp((x - 34) / area_width, 0.0, 1.0);
    emit frame_clicked(static_cast<int>(std::lround(fraction * static_cast<double>(frames_ - 1))));
}

void GrainTimeline::mousePressEvent(QMouseEvent* event) { emit_at(event->position().x()); }

void GrainTimeline::mouseMoveEvent(QMouseEvent* event) {
    if (event->buttons() & Qt::LeftButton) {
        emit_at(event->position().x());
    }
}

}  // namespace fgw::app
