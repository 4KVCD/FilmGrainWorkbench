// The AV1 tab: videos -> tune -> write, with a large frame viewer that
// estimates the grain live.
//
// The implementation is in three files: av1_tab.cpp (the controls, the
// settings, the videos, the jobs), av1_frames.cpp (which frame is shown, and
// loading it with its grain) and av1_write.cpp (the every-frame analysis,
// Write and Verify).
#pragma once

#include <chrono>
#include <deque>
#include <map>
#include <memory>
#include <optional>

#include <QCheckBox>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPushButton>
#include <QSettings>
#include <QSlider>
#include <QSpinBox>
#include <QSplitter>
#include <QTimer>
#include <QToolButton>

#include "fgw/alignment.h"
#include "fgw/display.h"
#include "fgw/mux.h"
#include "fgw/probe.h"
#include "fgw/timeline.h"
#include "fgw/tuning.h"
#include "frame_pane.h"
#include "job.h"
#include "plots.h"
#include "preview.h"

class QFormLayout;
class QGroupBox;
class QVBoxLayout;

namespace fgw::app {

// The last file written: what Verify compares against.
struct Written {
    WriteReport report;
    std::shared_ptr<const GrainPlan> plan;
    int seed = 0;
};

// Decoded frames kept for stepping back and forth, newest last, within a
// byte budget.
class FrameCache {
public:
    struct Key {
        bool source = false;  // a source frame (paired at an offset, maybe cropped), or an encode's
        std::string path;
        std::int64_t index = 0;
        std::optional<Crop> crop;
        bool operator==(const Key&) const = default;
    };

    std::shared_ptr<const Frame> get(const Key& key);  // and marks it used
    void put(const Key& key, std::shared_ptr<const Frame> frame);
    // These three free what they drop on a pool thread (release_later).
    void trim(std::size_t budget_bytes);
    void clear();
    void drop_source_frames();

private:
    struct Entry {
        Key key;
        std::shared_ptr<const Frame> frame;
    };
    std::deque<Entry> entries_;
};

class Av1Tab : public QMainWindow {
    Q_OBJECT
    friend struct Walkthrough;  // tests/tools/walkthrough.cpp drives the tab like a user

public:
    Av1Tab(QSettings* settings, QString data_dir, QWidget* parent = nullptr);
    ~Av1Tab() override;

    // Settings > External programs.
    void edit_tools();
    const Tools& tools() const { return tools_; }
    void log(const QString& text);
    StatusBar& status() { return *status_; }
    // Asks before closing would cancel a running job (a Write can take hours).
    bool can_close();
    // Stops every job and saves the settings; nothing is asked any more.
    void shutdown();

    bool confirm_long_runs = true;  // tests turn the question before an hours-long run off

    // For tests.
    bool busy() const { return running(job_); }
    bool loading() const { return running(frame_job_) || running(model_job_) || running(source_probe_) || running(encoded_probe_); }
    const std::optional<VideoInfo>& source() const { return source_; }
    const std::optional<VideoInfo>& encoded() const { return encoded_; }
    const std::shared_ptr<const TimelineModel>& model() const { return model_; }
    const std::optional<Written>& written() const { return written_; }
    PreviewState* preview() const { return state_; }
    void write();
    void verify();

signals:
    void tools_changed();  // the external programs were set up anew

protected:
    void closeEvent(QCloseEvent* event) override;

private:
    enum class Kind { source, encoded };

    // Layout (av1_tab.cpp).
    void build();
    QGroupBox* videos_group();
    QGroupBox* tune_group();
    QGroupBox* write_group();
    void add_log(QVBoxLayout* column);
    std::pair<QSlider*, QSpinBox*> strength_row(QFormLayout* form, const QString& title, const QString& tip);
    void install_shortcuts(QWidget* widget);

    // Settings.
    void restore();
    void save();
    void startup();
    void update_tools_banner();

    // Videos.
    void browse_video(Kind kind);
    void load_video(Kind kind);
    void video_probed(Kind kind, std::optional<VideoInfo> info, const QString& error);
    void pair_changed();
    QString pair_key() const;

    // Enabling, jobs.
    void update_enabled();
    QString write_blocker() const;
    void start_job(const QString& name, Job::Work work, const QString& busy_text);
    void cancel_job();

    // Analysis and tuning.
    void detect_offset_clicked();
    void offset_changed();
    void set_model(std::shared_ptr<const TimelineModel> model, const QString& loaded_from = {});
    void clear_model_ui();
    QString model_cache_file() const;
    void autoload_model();
    void export_table();
    Tuning tuning() const;
    void tuning_changed();
    void rebuild_plan();
    void retune();
    void update_segment_plot();

    // Frames (av1_frames.cpp).
    int frames_per_second() const;
    void step(int delta);
    void go_to(std::int64_t index, bool force = false);
    QString caption(std::int64_t index) const;
    void request_frame(std::int64_t index);
    void show_match(std::int64_t index, const FrameMatch& match);
    std::shared_ptr<const Frame> source_frame_of(std::int64_t index);
    bool grain_possible(std::int64_t index);
    void frame_job_ended();
    void render_grain();

    // View modes.
    void set_mode(View view);
    void sync_mode_buttons();
    void open_viewer_window();

    // Writing (av1_write.cpp).
    std::filesystem::path every_frame_folder() const;
    enum class AnalysisState { done, resumable, none };
    std::pair<AnalysisState, std::optional<std::int64_t>> every_frame_state() const;
    QString every_frame_text() const;
    void update_estimate();
    void browse_output();
    void open_folder();

    QSettings* settings_;
    QString data_dir_;
    Tools tools_;
    std::optional<VideoInfo> source_;
    std::optional<VideoInfo> encoded_;
    QString probing_[2];  // the path being read, per Kind
    std::optional<Crop> crop_;
    std::shared_ptr<const TimelineModel> model_;   // the every-frame analysis, once there is one
    std::map<std::int64_t, GrainParams> estimates_;  // live single-frame estimates otherwise
    std::shared_ptr<GrainPlan> plan_;
    std::shared_ptr<FramePlan> frame_plan_;  // plan_, when it is the live estimates'
    std::optional<Written> written_;
    QPointer<Job> job_;
    QPointer<Job> frame_job_;
    QPointer<Job> model_job_;  // reads a saved analysis in the background
    QPointer<Job> source_probe_;
    QPointer<Job> encoded_probe_;
    std::optional<std::int64_t> pending_index_;
    bool grain_dirty_ = false;
    FrameCache frame_cache_;
    std::shared_ptr<const Converter> converter_;
    std::int64_t current_index_ = 0;
    std::int64_t last_requested_ = 0;
    std::chrono::steady_clock::time_point job_started_;
    QString detected_;          // Detect's result for the current pair and offset
    int applied_offset_ = 0;    // the offset the frames shown and the estimates use
    // A frame waiting for its grain: shown with it, for someone comparing grain.
    std::optional<std::pair<std::int64_t, std::map<View, QImage>>> held_;
    bool output_is_auto_ = true;  // the output name follows the encode until one is chosen
    bool shut_down_ = false;

    PreviewState* state_;
    FramePane* pane_ = nullptr;
    ViewerWindow* viewer_window_ = nullptr;
    std::unique_ptr<StatusBar> status_;
    QTimer frame_timer_;
    QTimer tune_timer_;
    QTimer offset_timer_;

    QLabel* tools_banner_ = nullptr;
    QLineEdit* source_edit_ = nullptr;
    QLineEdit* encoded_edit_ = nullptr;
    QLabel* source_info_ = nullptr;
    QLabel* encoded_info_ = nullptr;
    QLabel* pair_note_ = nullptr;
    QSpinBox* offset_spin_ = nullptr;
    QPushButton* detect_button_ = nullptr;
    QLabel* offset_note_ = nullptr;
    CurvePlot* plot_ = nullptr;
    QSlider* luma_slider_ = nullptr;
    QSpinBox* luma_spin_ = nullptr;
    QSlider* chroma_slider_ = nullptr;
    QSpinBox* chroma_spin_ = nullptr;
    QSpinBox* seed_spin_ = nullptr;
    QCheckBox* overlap_check_ = nullptr;
    QCheckBox* clip_check_ = nullptr;
    QLabel* tune_label_ = nullptr;
    QPushButton* export_table_button_ = nullptr;
    QLineEdit* output_edit_ = nullptr;
    QSpinBox* workers_spin_ = nullptr;
    QCheckBox* gpu_check_ = nullptr;
    QLabel* write_estimate_label_ = nullptr;
    QLabel* analysis_label_ = nullptr;
    QPushButton* write_button_ = nullptr;
    QPushButton* verify_button_ = nullptr;
    QPushButton* open_folder_button_ = nullptr;
    QLabel* write_label_ = nullptr;
    QToolButton* log_toggle_ = nullptr;
    QPlainTextEdit* log_view_ = nullptr;
    GrainTimeline* grain_timeline_ = nullptr;
    QSplitter* splitter_ = nullptr;
};

}  // namespace fgw::app
