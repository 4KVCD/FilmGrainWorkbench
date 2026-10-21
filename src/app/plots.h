// The AV1 tab's plots: grain strength against brightness, and grain strength
// over the whole video.
#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include <QColor>
#include <QString>
#include <QWidget>

namespace fgw::app {

// The noise sigma the decoder adds at each brightness (8-bit), for luma, Cb and Cr.
class CurvePlot : public QWidget {
    Q_OBJECT

public:
    explicit CurvePlot(QWidget* parent = nullptr);
    QSize sizeHint() const override { return {360, 170}; }

    void set_data(const std::array<double, 256>& luma, const std::array<double, 256>& cb,
                  const std::array<double, 256>& cr);
    // No curves; `text` (if given) is shown instead.
    void clear(const QString& text = {});

protected:
    void paintEvent(QPaintEvent* event) override;

private:
    struct Curve {
        std::array<double, 256> values;
        QColor color;
        double width;
    };
    std::vector<Curve> curves_;
    QString empty_text_;
};

// Grain strength over the whole video, after the strength setting; click or
// drag to jump to a frame.
class GrainTimeline : public QWidget {
    Q_OBJECT

public:
    explicit GrainTimeline(QWidget* parent = nullptr);

    // Sigma samples: `sigmas[i]` from frame `starts[i]` on.
    void set_data(std::int64_t frames, std::vector<std::int64_t> starts, std::vector<double> sigmas);
    void set_current(std::int64_t index);

signals:
    void frame_clicked(int index);

protected:
    void paintEvent(QPaintEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;

private:
    // The sigma of the frame at the start of each of `columns` pixel columns.
    std::vector<double> column_values(int columns) const;
    void emit_at(double x);

    std::int64_t frames_ = 1;
    std::vector<std::int64_t> starts_;
    std::vector<double> sigmas_;
    std::int64_t current_ = 0;
};

}  // namespace fgw::app
