// The frame viewer: what each view shows, the zoomable view of it, and the
// stand-alone frame window. The tab's view and the frame window share one
// PreviewState.
#pragma once

#include <map>

#include <QImage>
#include <QLabel>
#include <QObject>
#include <QPointF>
#include <QString>
#include <QWidget>

namespace fgw::app {

// The four views of a frame.
enum View : int { source_view = 0, encoded_view = 1, grain_view = 2, grain_only_view = 3 };
inline constexpr View all_views[] = {source_view, encoded_view, grain_view, grain_only_view};

using ViewNames = std::map<View, QString>;

// The pictures of the frame shown and which one is shown.
class PreviewState : public QObject {
    Q_OBJECT

public:
    explicit PreviewState(ViewNames names, QObject* parent = nullptr);

    const ViewNames& names() const { return names_; }
    const std::map<View, QImage>& images() const { return images_; }
    View mode() const { return mode_; }
    const QString& caption() const { return caption_; }
    const QString& placeholder() const { return placeholder_; }
    // The picture shown; null when there is none.
    QImage image() const;
    bool has(View view) const { return images_.contains(view); }

    // A new frame's pictures. Keeps the view shown when the frame has it.
    void set_images(std::map<View, QImage> images, QString caption);
    // Adds or replaces some of the frame's pictures (null images remove them).
    void update_images(const std::map<View, QImage>& images);
    // No pictures; `placeholder` is shown instead.
    void clear(QString placeholder);
    // Shows `view`; false if the frame has no such picture.
    bool set_mode(View view);

signals:
    void changed();

private:
    ViewNames names_;
    std::map<View, QImage> images_;
    View mode_ = encoded_view;
    QString caption_;
    QString placeholder_;
};

// A zoomable, pannable view of the picture shown: smooth below 100 %,
// nearest-neighbour from 100 % up so single grains stay crisp.
class FrameView : public QWidget {
    Q_OBJECT

public:
    explicit FrameView(PreviewState* state, QWidget* parent = nullptr);

    void fit();
    void set_zoom(double zoom);
    // One step up (+1) or down (-1) the zoom steps.
    void step_zoom(int direction);
    double zoom() const { return zoom_; }
    bool fitted() const { return fit_; }

protected:
    void paintEvent(QPaintEvent* event) override;
    void resizeEvent(QResizeEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void mouseDoubleClickEvent(QMouseEvent* event) override;

private:
    void refresh();
    void zoom_at(double zoom, QPointF anchor);
    double fit_zoom() const;
    void clamp_offset();
    void update_overlay();

    PreviewState* state_;
    QLabel* overlay_;
    QLabel* message_;
    QSize image_size_;
    double zoom_ = 1.0;
    bool fit_ = true;
    QPointF offset_;  // the picture's top-left corner in the widget, in widget pixels
    QPointF drag_from_;
    bool dragging_ = false;
};

// A plain window holding only the frame.
class ViewerWindow : public QWidget {
    Q_OBJECT

public:
    explicit ViewerWindow(PreviewState* state, QWidget* parent = nullptr);
    FrameView* view() const { return view_; }
    // Opens at 100 % if the frame fits on screen, otherwise as large as sensible.
    void show_sized();

private:
    void update_title();

    PreviewState* state_;
    FrameView* view_;
};

}  // namespace fgw::app
