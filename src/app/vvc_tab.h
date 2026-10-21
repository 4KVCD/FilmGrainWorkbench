// The VVC grain analyzer tab: a source video, after VVenC removed its grain,
// and with vvdec's grain synthesis.
//
// Browsing shows the source alone. "Encode this scene" encodes the frames
// around the one shown with VVenC in all three film grain analysis modes:
// the selected one first, shown as soon as it is ready, the other two in the
// background, so switching modes is instant. Inside that scene every view is
// available, and a changed VVenC setting encodes the scene again.
//
// Temporary files: the tab keeps one scene (its source frames and the three
// encodes, each decoded twice) in a folder of its own in %TEMP%, deletes
// whatever is replaced as soon as nothing reads it, and the folder when it
// closes. Folders left by a crash are deleted when the next one opens.
//
// Everything slow (probing, finding black bars, decoding and converting
// frames, cutting and encoding) runs on job threads; the tab's thread only
// keeps the books and paints.
#pragma once

#include <deque>
#include <filesystem>
#include <memory>
#include <optional>

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QGroupBox>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QPointer>
#include <QPushButton>
#include <QSettings>
#include <QSpinBox>
#include <QSplitter>
#include <QTimer>

#include "fgw/display.h"
#include "fgw/probe.h"
#include "fgw/vvc_grain.h"
#include "frame_pane.h"
#include "job.h"
#include "preview.h"
#include "vvc_scene.h"

namespace fgw::app {

class VvcTab : public QMainWindow {
    Q_OBJECT
    friend struct Walkthrough;  // tests/tools/walkthrough.cpp drives the tab like a user

public:
    explicit VvcTab(QSettings* settings, const std::filesystem::path& work_parent = {}, QWidget* parent = nullptr);
    ~VvcTab() override;

    // ffmpeg and ffprobe as set in Settings > External programs (shared with the AV1 tab).
    void reload_tools();
    StatusBar& status() { return *status_; }
    const VvcTools& vvc_tools() const { return vvc_tools_; }
    // Nothing here needs asking before closing: a scene is quick to encode again.
    bool can_close() const { return true; }
    // Stops the work and deletes the temporary files.
    void shutdown();

    // For tests: the tab's state.
    const std::optional<VideoInfo>& source() const { return source_; }
    const Scene* scene() const { return scene_.get(); }
    bool busy() const { return running(job_) || running(frame_job_) || running(probe_job_); }
    PreviewState* preview() const { return state_; }
    void encode_scene();

protected:
    void closeEvent(QCloseEvent* event) override;

private:
    // Layout.
    void build();
    QGroupBox* source_group();
    QGroupBox* vvenc_group();
    QGroupBox* frame_group();
    void install_shortcuts(QWidget* widget);
    void update_tools_banner();

    // Settings.
    void restore();
    void save();
    VvcSettings current_settings() const;
    int selected_fga() const;
    void startup();

    // The source.
    void browse();
    void load_source();
    void source_probed(const VideoInfo& info);
    void crop_toggled();

    // Navigation.
    int frames_per_second() const;
    void step(int delta);
    void go_to(std::int64_t index, bool force = false);
    void settings_changed();
    void mode_changed();
    void apply_settings();

    // Scenes.
    std::optional<Crop> wanted_crop() const;
    std::pair<std::int64_t, std::int64_t> scene_range() const;
    void new_scene();
    void recode(Scene& scene, const VvcCoding& coding);
    void retire_scene();
    void remove_retired();

    // What is shown, what runs.
    void show_frame();
    void schedule();
    bool job_unwanted(const std::optional<JobKey>& key) const;
    void start_job(const JobKey& key, const QString& name, Job::Work work, std::filesystem::path folder = {});
    void start_black_bar_job();
    void start_cut_job();
    void start_encode_job(int fga);
    void job_ended();
    void job_failed(const QString& name, const JobKey& key, const QString& message);
    void stop();

    // Frames.
    Geometry geometry() const;
    std::shared_ptr<const Converter> converter_for(const Geometry& g);
    QString caption(std::int64_t index) const;
    void start_frame_job(std::int64_t index, Job::Work work);
    void render_source(std::int64_t index);
    void render_clip(std::shared_ptr<const Clip> clip, std::int64_t index);
    void frame_job_ended();
    void update_scene_label();

    // View modes.
    void set_mode(View view);
    void sync_mode_buttons();
    void open_viewer_window();

    QSettings* settings_;
    Tools tools_;
    VvcTools vvc_tools_;
    std::unique_ptr<WorkFolder> work_;
    std::optional<VideoInfo> source_;
    QString probing_;  // the path being probed
    std::optional<Crop> crop_;
    bool crop_known_ = false;  // black bars detected for this source
    std::unique_ptr<Scene> scene_;
    int scene_count_ = 0;
    bool stopped_ = false;  // Cancel pressed or an encode failed: no more work until asked
    std::vector<std::filesystem::path> retired_;  // folders to delete once nothing reads them
    QPointer<Job> job_;  // black-bar detection, a cut or an encode
    std::optional<JobKey> job_key_;
    std::filesystem::path job_folder_;  // where the running job writes
    QPointer<Job> probe_job_;
    QPointer<Job> frame_job_;
    bool frame_pending_ = false;
    std::shared_ptr<const Converter> converter_;
    std::int64_t current_index_ = 0;
    // Source frames kept for stepping back and forth (~18 MB each at 4K), newest last.
    struct CachedFrame {
        std::string path;  // the source's
        std::int64_t index;
        std::optional<Crop> crop;
        std::shared_ptr<const Frame> frame;
    };
    std::deque<CachedFrame> source_frames_;
    std::int64_t last_read_ = 0;
    View preferred_mode_ = grain_view;  // the view chosen last, restored inside a scene
    bool started_ = false;  // the controls are restored and may be saved

    PreviewState* state_;
    FramePane* pane_ = nullptr;
    ViewerWindow* viewer_window_ = nullptr;
    std::unique_ptr<StatusBar> status_;
    QTimer frame_timer_;
    QTimer settings_timer_;

    QLabel* tools_banner_ = nullptr;
    QLineEdit* source_edit_ = nullptr;
    QLabel* source_info_ = nullptr;
    QCheckBox* crop_check_ = nullptr;
    QLabel* crop_label_ = nullptr;
    QComboBox* fga_combo_ = nullptr;
    QSpinBox* strength_spin_ = nullptr;
    QDoubleSpinBox* qp_spin_ = nullptr;
    QComboBox* preset_combo_ = nullptr;
    QSpinBox* frames_spin_ = nullptr;
    QPushButton* encode_button_ = nullptr;
    QLabel* scene_label_ = nullptr;
    QLabel* tools_label_ = nullptr;
    QLabel* info_label_ = nullptr;
};

}  // namespace fgw::app
