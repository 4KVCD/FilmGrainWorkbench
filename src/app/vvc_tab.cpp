#include "vvc_tab.h"

#include <algorithm>
#include <cmath>
#include <future>
#include <set>

#include <QCloseEvent>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QKeySequence>
#include <QScrollArea>
#include <QShortcut>
#include <QVBoxLayout>

#include "fgw/frames.h"
#include "fgw/text.h"
#include "release.h"
#include "settings.h"

namespace fgw::app {

namespace {

constexpr int prefetch = 4;               // source frames decoded per seek while browsing
constexpr std::size_t source_cache = 16;  // source frames kept for stepping back and forth
constexpr int frame_delay_ms = 120;       // a frame is shown once stepping pauses this long
constexpr int settings_delay_ms = 600;    // spin boxes change step by step: encode after the last step
const QString video_filter = "Video files (*.mkv *.mp4 *.mov *.webm *.ts *.m2ts *.y4m);;All files (*)";

const ViewNames view_names = {{source_view, "Source"},
                              {encoded_view, "VVC, grain removed"},
                              {grain_view, "VVC + grain synthesis"},
                              {grain_only_view, "Grain only (×4)"}};
const std::map<View, QString> view_tips = {
    {source_view, "The source frame (cropped like the encode)."},
    {encoded_view, "What VVenC coded: its MCTF pre-filter and the quantisation removed the grain. Decoded by vvdec "
                   "with film grain synthesis off."},
    {grain_view, "The same picture decoded by vvdec with film grain synthesis on, from the FGC SEI message VVenC's "
                 "film grain analysis wrote."},
    {grain_only_view, "The grain vvdec adds, amplified 4×, on mid-grey."}};

QString qstring(const std::string& text) { return QString::fromStdString(text); }

QString first_line(const QString& text) { return text.section('\n', 0, 0); }

}  // namespace

VvcTab::VvcTab(QSettings* settings, const std::filesystem::path& work_parent, QWidget* parent)
    : QMainWindow(parent),
      settings_(settings),
      tools_(load_tools(*settings)),
      vvc_tools_(find_vvc_tools()),
      work_(std::make_unique<WorkFolder>(work_parent)),
      state_(new PreviewState(view_names, "Choose a source video to see frames.", this)) {
    setWindowTitle("VVC Grain Analyzer — VVenC film grain analysis + vvdec synthesis");
    build();
    restore();
    frame_timer_.setSingleShot(true);
    frame_timer_.setInterval(frame_delay_ms);
    connect(&frame_timer_, &QTimer::timeout, this, &VvcTab::show_frame);
    settings_timer_.setSingleShot(true);
    settings_timer_.setInterval(settings_delay_ms);
    connect(&settings_timer_, &QTimer::timeout, this, &VvcTab::apply_settings);
    QTimer::singleShot(0, this, &VvcTab::startup);
}

VvcTab::~VvcTab() { shutdown(); }

// ---------------------------------------------------------------- layout

void VvcTab::build() {
    auto* sidebar = new QWidget;
    auto* side = new QVBoxLayout(sidebar);
    side->setContentsMargins(12, 12, 12, 12);
    side->setSpacing(10);
    tools_banner_ = new QLabel;
    tools_banner_->setWordWrap(true);
    tools_banner_->setObjectName("warning");
    side->addWidget(tools_banner_);
    side->addWidget(source_group());
    side->addWidget(vvenc_group());
    side->addWidget(frame_group());
    side->addStretch(1);

    auto* scroll = new QScrollArea;
    scroll->setWidget(sidebar);
    scroll->setWidgetResizable(true);
    scroll->setMinimumWidth(440);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    pane_ = new FramePane(state_, view_tips, nullptr, shortcut_help("VVC", "VVC + grain"));
    connect(pane_, &FramePane::frame_requested, this, [this](int index) { go_to(index); });
    connect(pane_, &FramePane::step_requested, this, &VvcTab::step);
    connect(pane_, &FramePane::mode_requested, this, &VvcTab::set_mode);
    connect(pane_, &FramePane::popout_requested, this, &VvcTab::open_viewer_window);

    auto* splitter = new QSplitter;
    splitter->addWidget(scroll);
    splitter->addWidget(pane_);
    splitter->setStretchFactor(1, 1);
    splitter->setSizes({470, 1100});
    setCentralWidget(splitter);
    status_ = std::make_unique<StatusBar>(this, [this] { stop(); }, 240);

    connect(state_, &PreviewState::changed, this, &VvcTab::sync_mode_buttons);
    install_shortcuts(this);
    update_tools_banner();
}

QGroupBox* VvcTab::source_group() {
    auto* box = new QGroupBox("1 · Source");
    auto* grid = new QGridLayout(box);
    source_edit_ = new QLineEdit;
    source_edit_->setPlaceholderText("Original, grainy video (e.g. the Blu-ray remux)");
    connect(source_edit_, &QLineEdit::editingFinished, this, &VvcTab::load_source);
    auto* browse_button = new QPushButton("Browse…");
    connect(browse_button, &QPushButton::clicked, this, &VvcTab::browse);
    source_info_ = new QLabel;
    source_info_->setObjectName("info");
    source_info_->setWordWrap(true);
    crop_check_ = new QCheckBox("Remove black bars, as an encode would");
    crop_check_->setChecked(true);
    crop_check_->setToolTip("Encode only the picture area. Black bars are flat and would take part in VVenC's grain "
                            "analysis otherwise.");
    connect(crop_check_, &QCheckBox::toggled, this, &VvcTab::crop_toggled);
    crop_label_ = new QLabel;
    crop_label_->setObjectName("info");
    grid->addWidget(source_edit_, 0, 0);
    grid->addWidget(browse_button, 0, 1);
    grid->addWidget(source_info_, 1, 0, 1, 2);
    grid->addWidget(crop_check_, 2, 0, 1, 2);
    grid->addWidget(crop_label_, 3, 0, 1, 2);
    return box;
}

QGroupBox* VvcTab::vvenc_group() {
    auto* box = new QGroupBox("2 · VVenC");
    auto* form = new QFormLayout(box);
    fga_combo_ = new QComboBox;
    for (const FgaMode& mode : fga_modes) {
        fga_combo_->addItem(mode.label, mode.value);
    }
    fga_combo_->setToolTip("--fga: which film grain analysis writes the FGC SEI message. 3 is shown only by decoders "
                           "with the auto-regressive model (vvdec).");
    strength_spin_ = new QSpinBox;
    strength_spin_->setRange(0, 400);
    strength_spin_->setValue(100);
    strength_spin_->setSingleStep(10);
    strength_spin_->setSuffix(" %");
    strength_spin_->setToolTip("--fgastrength: grain strength in percent of what the analysis measured.");
    qp_spin_ = new QDoubleSpinBox;
    qp_spin_->setRange(0, 63);
    qp_spin_->setValue(28);
    qp_spin_->setDecimals(1);
    qp_spin_->setSingleStep(1);
    qp_spin_->setToolTip("--qp: the higher, the more grain the coding removes.");
    preset_combo_ = new QComboBox;
    for (const std::string& preset : vvenc_presets) {
        preset_combo_->addItem(qstring(preset));
    }
    frames_spin_ = new QSpinBox;
    frames_spin_->setRange(9, 129);
    frames_spin_->setValue(33);
    frames_spin_->setToolTip("Frames Encode this scene encodes, centred on the frame shown. VVenC analyses the grain "
                             "once per GOP (32 frames), so 33 covers one GOP and the next.");
    form->addRow("Film grain analysis", fga_combo_);
    form->addRow("Strength", strength_spin_);
    form->addRow("QP", qp_spin_);
    form->addRow("Preset", preset_combo_);
    form->addRow("Scene length", frames_spin_);
    connect(fga_combo_, &QComboBox::currentIndexChanged, this, &VvcTab::mode_changed);
    connect(preset_combo_, &QComboBox::currentIndexChanged, this, &VvcTab::settings_changed);
    connect(strength_spin_, &QSpinBox::valueChanged, this, &VvcTab::settings_changed);
    connect(qp_spin_, &QDoubleSpinBox::valueChanged, this, &VvcTab::settings_changed);
    connect(frames_spin_, &QSpinBox::valueChanged, this, &VvcTab::settings_changed);
    encode_button_ = new QPushButton("Encode this scene  [E]");
    encode_button_->setProperty("primary", true);
    encode_button_->setToolTip("Encode the frames around the one shown with VVenC, then show them without and with "
                               "vvdec's grain synthesis.");
    connect(encode_button_, &QPushButton::clicked, this, &VvcTab::encode_scene);
    form->addRow(encode_button_);
    scene_label_ = new QLabel;
    scene_label_->setObjectName("info");
    scene_label_->setWordWrap(true);
    form->addRow(scene_label_);
    tools_label_ = new QLabel;
    tools_label_->setObjectName("info");
    tools_label_->setWordWrap(true);
    form->addRow(tools_label_);
    return box;
}

QGroupBox* VvcTab::frame_group() {
    auto* box = new QGroupBox("3 · This frame");
    auto* column = new QVBoxLayout(box);
    info_label_ = new QLabel("Choose a source video.");
    info_label_->setWordWrap(true);
    info_label_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    QFont mono("Consolas");
    mono.setStyleHint(QFont::Monospace);
    info_label_->setFont(mono);
    column->addWidget(info_label_);
    return box;
}

void VvcTab::install_shortcuts(QWidget* widget) {
    FrameView* view = widget == viewer_window_ ? viewer_window_->view() : pane_->view();
    install_viewer_shortcuts(
        widget, view, [this](View v) { set_mode(v); }, [this](int delta) { step(delta); },
        [this] { return frames_per_second(); });
    new QShortcut(QKeySequence("E"), widget, this, &VvcTab::encode_scene);
}

void VvcTab::reload_tools() {
    tools_ = load_tools(*settings_);
    update_tools_banner();
    if (!source_ && !running(probe_job_) && !source_edit_->text().trimmed().isEmpty()) {
        load_source();  // waiting for ffmpeg and ffprobe
    }
}

void VvcTab::update_tools_banner() {
    QStringList problems;
    const std::vector<std::string> external = tools_.missing({"ffmpeg", "ffprobe"});
    if (!external.empty()) {
        QStringList names;
        for (const std::string& name : external) {
            names << qstring(name);
        }
        problems << "Missing: " + names.join(", ") + " (Settings → External programs).";
    }
    const std::vector<std::string> bundled = vvc_tools_.missing();
    if (!bundled.empty()) {
        QStringList names;
        for (const std::string& name : bundled) {
            names << qstring(name);
        }
        problems << "Missing: " + names.join(", ") + ". " + qstring(missing_program_hint());
    }
    tools_banner_->setVisible(!problems.isEmpty());
    tools_banner_->setText(problems.join(" "));
    tools_label_->setText("vvencapp: " + (vvc_tools_.vvencapp.empty() ? QString("—") : qstring(vvc_tools_.vvencapp)) +
                          "\nvvdecapp: " + (vvc_tools_.vvdecapp.empty() ? QString("—") : qstring(vvc_tools_.vvdecapp)));
}

// ---------------------------------------------------------------- settings

void VvcTab::restore() {
    const QSettings& s = *settings_;
    fga_combo_->setCurrentIndex(std::max(0, fga_combo_->findData(s.value("vvc/fga", 2).toInt())));
    strength_spin_->setValue(s.value("vvc/strength", 100).toInt());
    qp_spin_->setValue(s.value("vvc/qp", 28).toDouble());
    preset_combo_->setCurrentText(s.value("vvc/preset", "faster").toString());
    frames_spin_->setValue(s.value("vvc/frames", 33).toInt());
    crop_check_->setChecked(s.value("vvc/crop", true).toBool());
}

void VvcTab::save() {
    if (!started_) {
        return;  // restoring the controls fires their change signals: they would save an empty source
    }
    QSettings& s = *settings_;
    s.setValue("vvc/source", source_edit_->text());
    s.setValue("vvc/fga", fga_combo_->currentData());
    s.setValue("vvc/strength", strength_spin_->value());
    s.setValue("vvc/qp", qp_spin_->value());
    s.setValue("vvc/preset", preset_combo_->currentText());
    s.setValue("vvc/frames", frames_spin_->value());
    s.setValue("vvc/crop", crop_check_->isChecked());
}

VvcSettings VvcTab::current_settings() const {
    return VvcSettings{selected_fga(),
                       VvcCoding{strength_spin_->value(), qp_spin_->value(), preset_combo_->currentText().toStdString()},
                       frames_spin_->value()};
}

int VvcTab::selected_fga() const { return fga_combo_->currentData().toInt(); }

void VvcTab::startup() {
    started_ = true;
    const QString saved = settings_->value("vvc/source").toString();
    if (!saved.isEmpty()) {
        source_edit_->setText(saved);
        load_source();
    }
}

// ---------------------------------------------------------------- the source

void VvcTab::browse() {
    const QString start = source_edit_->text().isEmpty() ? settings_->value("files/last_dir").toString()
                                                         : source_edit_->text();
    const QString path = QFileDialog::getOpenFileName(this, "Choose the source video", start, video_filter);
    if (!path.isEmpty()) {
        source_edit_->setText(QDir::toNativeSeparators(path));
        settings_->setValue("files/last_dir", QFileInfo(path).absolutePath());
        load_source();
    }
}

void VvcTab::load_source() {
    QString path = source_edit_->text().trimmed();
    if (path.size() >= 2 && path.startsWith('"') && path.endsWith('"')) {
        path = path.mid(1, path.size() - 2);
    }
    if ((source_ && qstring(utf8(source_->path)) == path) || (running(probe_job_) && probing_ == path)) {
        return;
    }
    retire_scene();  // a running job is cancelled when its result arrives
    release_later(std::exchange(source_frames_, {}));
    converter_.reset();
    source_.reset();
    crop_.reset();
    crop_known_ = false;
    crop_label_->clear();
    probing_ = path;
    if (path.isEmpty()) {
        source_info_->clear();
        state_->clear("Choose a source video to see frames.");
        return;
    }
    if (const std::vector<std::string> missing = tools_.missing({"ffmpeg", "ffprobe"}); !missing.empty()) {
        // Else the probe runs an empty program path: "Program not found:  ([WinError 87] ...)".
        probing_.clear();  // read once they are set up (reload_tools)
        source_info_->setText("Set up ffmpeg and ffprobe first (Settings → External programs).");
        source_info_->setStyleSheet("color: #ff7b72;");
        state_->clear("ffmpeg and ffprobe are needed to show frames.");
        return;
    }
    source_info_->setStyleSheet("");
    source_info_->setText("Reading " + QFileInfo(path).fileName() + "…");
    // Probed on a job thread; the result is used only if no other video was
    // chosen meanwhile.
    const std::string ffprobe = tools_.ffprobe;
    auto* job = new Job("Reading the source", [this, path, ffprobe](CancelToken& cancel, const Progress&) -> Job::Deliver {
        VideoInfo info = probe(utf8_path(path.toStdString()), ffprobe, &cancel);
        return [this, path, info = std::move(info)] {
            if (probing_ == path) {
                source_probed(info);
            }
        };
    }, this);
    connect(job, &Job::failed, this, [this, path](const QString& message) {
        if (probing_ != path) {
            return;
        }
        source_info_->setText(first_line(message));
        source_info_->setStyleSheet("color: #ff7b72;");
        state_->clear("The source could not be read.");
    });
    probe_job_ = job;
    job->start();
}

void VvcTab::source_probed(const VideoInfo& info) {
    source_ = info;
    source_info_->setStyleSheet("");
    source_info_->setText(qstring(info.describe()));
    pane_->set_frame_count(info.frame_count());
    save();
    go_to(info.frame_count() / 3, true);
}

void VvcTab::crop_toggled() {
    save();
    release_later(std::exchange(source_frames_, {}));
    retire_scene();
    if (source_) {
        converter_.reset();
        show_frame();
    }
}

// ---------------------------------------------------------------- navigation

int VvcTab::frames_per_second() const {
    return source_ ? static_cast<int>(std::lround(source_->fps.value())) : 24;
}

void VvcTab::step(int delta) {
    if (source_) {
        go_to(current_index_ + delta);
    }
}

void VvcTab::go_to(std::int64_t index, bool force) {
    if (!source_) {
        return;
    }
    index = std::clamp<std::int64_t>(index, 0, source_->frame_count() - 1);
    if (index == current_index_ && !force) {
        return;
    }
    current_index_ = index;
    pane_->show_frame(index, source_->time_of(index));
    frame_timer_.start();
}

void VvcTab::settings_changed() {
    save();
    if (source_) {
        update_scene_label();
        settings_timer_.start();
    }
}

void VvcTab::mode_changed() {
    save();
    if (source_) {
        update_scene_label();
        show_frame();
    }
}

// Inside the encoded scene, new strength, QP or preset encode its three
// modes again. Outside it nothing is encoded, and a new scene length waits
// for Encode this scene: it decides which frames the next scene holds.
void VvcTab::apply_settings() {
    if (scene_ && scene_->covers(current_index_)) {
        recode(*scene_, current_settings().coding);
    }
    show_frame();
}

// ---------------------------------------------------------------- scenes

std::optional<Crop> VvcTab::wanted_crop() const {
    return crop_check_->isChecked() ? crop_ : std::nullopt;
}

std::pair<std::int64_t, std::int64_t> VvcTab::scene_range() const {
    const std::int64_t length = std::min<std::int64_t>(frames_spin_->value(), source_->frame_count());
    const std::int64_t start =
        std::clamp<std::int64_t>(current_index_ - length / 2, 0, source_->frame_count() - length);
    return {start, length};
}

void VvcTab::encode_scene() {
    if (!source_ || !vvc_tools_.missing().empty()) {
        return;
    }
    const auto [start, length] = scene_range();
    if (scene_ && scene_->holds(current_index_, length, wanted_crop())) {
        // The same frames: continue a stopped scene, or encode it with the
        // current settings, keeping its cut and every mode already encoded.
        stopped_ = false;
        recode(*scene_, current_settings().coding);
        preferred_mode_ = grain_view;
        show_frame();
        return;
    }
    new_scene();
}

void VvcTab::new_scene() {
    const auto [start, length] = scene_range();
    retire_scene();
    ++scene_count_;
    scene_ = std::make_unique<Scene>();
    scene_->ident = scene_count_;
    scene_->start = start;
    scene_->frames = length;
    scene_->crop = wanted_crop();
    scene_->base = current_settings().coding;
    scene_->folder = work_->path() / ("scene-" + std::to_string(scene_count_));
    stopped_ = false;
    preferred_mode_ = grain_view;
    show_frame();
}

// Encodes the scene's modes again with other coding settings; the current
// ones stay on screen (as `older`) until each replacement is ready.
void VvcTab::recode(Scene& scene, const VvcCoding& coding) {
    if (coding == scene.base) {
        return;
    }
    stopped_ = false;
    for (auto& [fga, clip] : scene.clips) {
        if (const auto old = scene.older.find(fga); old != scene.older.end()) {
            retired_.push_back(old->second->folder);
        }
        scene.older[fga] = clip;
    }
    scene.clips.clear();
    scene.base = coding;
    remove_retired();
}

void VvcTab::retire_scene() {
    if (scene_) {
        retired_.push_back(scene_->folder);
        scene_.reset();
        remove_retired();
    }
}

// Deletes replaced files, unless a frame is being read from them or a job
// still writes there.
void VvcTab::remove_retired() {
    if (running(frame_job_)) {
        return;  // again when that frame job ends
    }
    const std::filesystem::path busy = running(job_) ? job_folder_ : std::filesystem::path();
    std::vector<std::filesystem::path> keep;
    for (const std::filesystem::path& folder : retired_) {
        const auto relative = busy.empty() ? std::filesystem::path() : busy.lexically_relative(folder);
        const bool written_to = !busy.empty() && !relative.empty() && *relative.begin() != "..";
        if (written_to) {
            keep.push_back(folder);  // again when that job ends
        } else {
            std::error_code ignored;
            std::filesystem::remove_all(folder, ignored);
        }
    }
    retired_ = std::move(keep);
}

// ---------------------------------------------------------------- what is shown, what runs

// Shows the current frame: the selected mode's encode inside the scene, the source otherwise.
void VvcTab::show_frame() {
    if (!source_ || !work_) {  // no work folder: shut down
        return;
    }
    if (crop_check_->isChecked() && !crop_known_) {
        // The whole picture meanwhile; the cropped one follows when the bars are known.
        if (running(job_) && job_unwanted(job_key_)) {
            job_->cancel();  // an earlier scene's encode would hold the detection up; its end shows this again
        }
        start_black_bar_job();
        render_source(current_index_);
        return;
    }
    schedule();
    if (scene_ && scene_->covers(current_index_)) {
        const int fga = selected_fga();
        std::shared_ptr<const Clip> clip;
        if (const auto found = scene_->clips.find(fga); found != scene_->clips.end()) {
            clip = found->second;
        } else if (const auto old = scene_->older.find(fga); old != scene_->older.end()) {
            clip = old->second;
        }
        if (clip && clip->covers(current_index_)) {
            render_clip(clip, current_index_);
            return;
        }
    }
    render_source(current_index_);
}

// Starts the scene's next piece of work, or cancels a job whose result is no longer wanted.
void VvcTab::schedule() {
    if (running(job_)) {
        if (job_unwanted(job_key_)) {
            job_->cancel();  // its end schedules again
        }
        return;
    }
    if (!scene_ || stopped_) {
        return;
    }
    const std::optional<JobKey> key = scene_->next_work(selected_fga());
    if (!key) {
        return;
    }
    if (key->kind == JobKey::Kind::cut) {
        start_cut_job();
    } else {
        start_encode_job(key->fga);
    }
}

// The running job's result is not needed, or it holds up the mode chosen in
// the list: a background encode of another mode is cancelled while the
// chosen mode is missing, so the chosen one shows a few seconds sooner; the
// cancelled one is encoded again afterwards.
bool VvcTab::job_unwanted(const std::optional<JobKey>& key) const {
    if (!key || key->kind == JobKey::Kind::find_black_bars) {
        return false;
    }
    if (!scene_ || !scene_->wants(*key)) {
        return true;
    }
    const int selected = selected_fga();
    return key->kind == JobKey::Kind::encode && key->fga != selected && !scene_->clips.contains(selected) && !stopped_;
}

void VvcTab::start_job(const JobKey& key, const QString& name, Job::Work work, std::filesystem::path folder) {
    if (running(job_) || !work_) {  // no work folder: shut down
        return;
    }
    job_key_ = key;
    job_folder_ = std::move(folder);
    auto* job = new Job(name, std::move(work), this);
    connect(job, &Job::progressed, this, [this](double, const QString& message) { status_->set_text(message); });
    connect(job, &Job::failed, this, [this, name, key](const QString& message) { job_failed(name, key, message); });
    connect(job, &Job::cancelled, this, [this] {
        if (stopped_) {  // Cancel pressed; other cancels are rescheduling
            status_->set_text("Cancelled.");
        }
    });
    connect(job, &Job::ended, this, &VvcTab::job_ended);
    job_ = job;
    status_->busy();
    update_scene_label();
    job->start();
}

void VvcTab::start_black_bar_job() {
    const VideoInfo source = *source_;
    const std::string ffmpeg = tools_.ffmpeg;
    start_job(JobKey{}, "Black bar detection", [this, source, ffmpeg](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
        progress(-1, "Looking for black bars…");
        const std::optional<Crop> crop = detect_black_bars(ffmpeg, source, &cancel);
        return [this, crop, path = source.path] {
            if (!source_ || source_->path != path) {
                return;  // found for a video no longer loaded
            }
            crop_ = crop;
            crop_known_ = true;
            if (scene_ && !scene_->cut) {
                // Encode this scene was pressed before the bars were known: its
                // frames are not cut yet, so it is cut without them.
                scene_->crop = wanted_crop();
            }
            crop_label_->setText(crop ? QString("Picture area %1×%2 at (%3, %4)").arg(crop->width).arg(crop->height)
                                            .arg(crop->x).arg(crop->y)
                                      : QString("No black bars found."));
        };
    });
}

void VvcTab::start_cut_job() {
    const VideoInfo source = *source_;
    const std::string ffmpeg = tools_.ffmpeg;
    const int ident = scene_->ident;
    const std::int64_t start = scene_->start;
    const std::int64_t frames = scene_->frames;
    const std::optional<Crop> crop = scene_->crop;
    const std::filesystem::path folder = scene_->folder;
    start_job(JobKey{JobKey::Kind::cut, ident, {}, 0}, "Reading the scene",
              [this, source, ffmpeg, ident, start, frames, crop, folder](CancelToken& cancel, const Progress& progress)
                  -> Job::Deliver {
                  SceneCut cut = cut_scene(ffmpeg, source, start, frames, crop, folder, &cancel, progress);
                  return [this, ident, cut = std::move(cut)] {
                      if (scene_ && scene_->ident == ident) {
                          scene_->cut = cut;  // otherwise the scene's folder is retired already
                      }
                  };
              },
              folder);
}

void VvcTab::start_encode_job(int fga) {
    const SceneCut cut = *scene_->cut;
    const VvcSettings settings = scene_->settings(fga);
    const VvcTools tools = vvc_tools_;
    const int ident = scene_->ident;
    start_job(JobKey{JobKey::Kind::encode, ident, settings.coding, fga}, QString("Encoding --fga %1").arg(fga),
              [this, cut, settings, tools, ident, fga](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
                  auto clip = std::make_shared<const Clip>(fgw::encode_scene(tools, cut, settings, &cancel, progress));
                  return [this, clip, settings, ident, fga] {
                      if (!scene_ || scene_->ident != ident || !(scene_->base == settings.coding)) {
                          retired_.push_back(clip->folder);  // the settings or the scene changed meanwhile
                          return;
                      }
                      if (const auto old = scene_->older.find(fga); old != scene_->older.end()) {
                          retired_.push_back(old->second->folder);
                          scene_->older.erase(old);
                      }
                      scene_->clips[fga] = clip;
                      QStringList left;
                      for (const FgaMode& mode : fga_modes) {
                          if (!scene_->clips.contains(mode.value)) {
                              left << QString::number(mode.value);
                          }
                      }
                      status_->set_text(
                          QString("--fga %1 encoded in %2 s (%3 kb/s)").arg(fga).arg(clip->encode_seconds, 0, 'f', 1)
                              .arg(qstring(with_thousands(std::llround(clip->kbps())))) +
                          (left.isEmpty() ? QString("; all three modes are ready.")
                                          : "; --fga " + left.join(", ") + " next, in the background."));
                  };
              },
              cut.folder);
}

void VvcTab::job_ended() {
    status_->idle();
    job_key_.reset();
    job_folder_.clear();
    remove_retired();
    update_scene_label();
    QTimer::singleShot(0, this, &VvcTab::show_frame);  // the next piece of work, or the frame asked for meanwhile
}

void VvcTab::job_failed(const QString& name, const JobKey& key, const QString& message) {
    status_->set_text(name + " failed.");
    if (key.kind == JobKey::Kind::find_black_bars) {
        crop_known_ = true;  // show the whole picture rather than try again
        crop_label_->setText("Black bar detection failed: " + first_line(message));
        return;
    }
    stopped_ = true;  // not again until asked
    state_->clear(name + " failed:\n\n" + message);
}

// The Cancel button: stops the scene's work until Encode this scene or a setting asks again.
void VvcTab::stop() {
    if (running(job_)) {
        stopped_ = true;
        job_->cancel();
    }
}

// ---------------------------------------------------------------- frames

// 10-bit 4:2:0 at the picture size encoded: what VVenC codes and what is shown.
Geometry VvcTab::geometry() const {
    const std::optional<Crop> crop = wanted_crop();
    return Geometry{crop ? crop->width : source_->width, crop ? crop->height : source_->height, 10, 1, 1};
}

std::shared_ptr<const Converter> VvcTab::converter_for(const Geometry& g) {
    if (!converter_ || converter_->format().width != g.width || converter_->format().height != g.height) {
        converter_ = std::make_shared<const Converter>(DisplayFormat{g.bit_depth, source_->limited_range(),
                                                                     source_->matrix, source_->transfer,
                                                                     source_->primaries, g.width, g.height});
    }
    return converter_;
}

QString VvcTab::caption(std::int64_t index) const {
    return "frame " + qstring(with_thousands(index)) + " · " + timecode(source_->time_of(index));
}

void VvcTab::start_frame_job(std::int64_t index, Job::Work work) {
    if (!work_) {  // shut down
        return;
    }
    auto* job = new Job("Frame", std::move(work), this);
    connect(job, &Job::failed, this, [this, index](const QString& message) {
        state_->clear("Could not show frame " + qstring(with_thousands(index)) + ":\n\n" + message);
    });
    connect(job, &Job::ended, this, &VvcTab::frame_job_ended);
    frame_job_ = job;
    job->start();
}

// Shows the source frame alone (no encode): browsing for a scene.
void VvcTab::render_source(std::int64_t index) {
    if (running(frame_job_)) {
        frame_pending_ = true;
        return;
    }
    frame_pending_ = false;
    const VideoInfo source = *source_;
    const std::optional<Crop> crop = wanted_crop();
    const Geometry g = geometry();
    const std::string ffmpeg = tools_.ffmpeg;
    const std::shared_ptr<const Converter> converter = converter_for(g);
    const std::string path = utf8(source.path);
    std::shared_ptr<const Frame> cached;
    for (const CachedFrame& entry : source_frames_) {
        if (entry.path == path && entry.index == index && entry.crop == crop) {
            cached = entry.frame;
        }
    }
    const bool backwards = index < last_read_;
    last_read_ = index;
    const std::int64_t first = backwards ? std::max<std::int64_t>(0, index - prefetch + 1) : index;
    start_frame_job(index, [this, source, path, crop, g, ffmpeg, converter, cached, index, first](
                               CancelToken& cancel, const Progress&) -> Job::Deliver {
        std::vector<std::shared_ptr<const Frame>> frames;
        if (cached) {
            frames.push_back(cached);
        } else {
            for (Frame& frame : read_frames(ffmpeg, source, first, prefetch, &cancel, g, crop)) {
                frames.push_back(std::make_shared<const Frame>(std::move(frame)));
            }
        }
        const auto shown = std::find_if(frames.begin(), frames.end(), [index](const auto& f) { return f->index == index; });
        if (shown == frames.end()) {
            throw std::runtime_error("frame " + with_thousands(index) +
                                     " could not be decoded (a cut file starts with pictures that need earlier frames)");
        }
        QImage image = to_qimage(*converter, **shown);
        return [this, path, index, crop, frames = std::move(frames), image = std::move(image)] {
            if (!source_ || utf8(source_->path) != path) {
                return;  // another source was chosen meanwhile
            }
            for (const auto& frame : frames) {
                std::erase_if(source_frames_, [&](const CachedFrame& e) {
                    return e.path == path && e.index == frame->index && e.crop == crop;
                });
                source_frames_.push_back({path, frame->index, crop, frame});
            }
            while (source_frames_.size() > source_cache) {
                release_later(std::move(source_frames_.front()));
                source_frames_.pop_front();
            }
            if (index != current_index_) {
                frame_pending_ = true;
                return;
            }
            state_->set_images({{source_view, image}}, caption(index));
            const bool encoding_here = scene_ && scene_->covers(index) && !stopped_;
            info_label_->setText(encoding_here
                                     ? QString("Encoding --fga %1 for this scene…").arg(selected_fga())
                                     : QString("Source frame. Press Encode this scene (E) to encode the frames around it "
                                               "with VVenC and see them without and with the grain synthesis."));
            if (!running(job_)) {
                status_->set_text("Frame " + qstring(with_thousands(index)) + ".");
            }
            update_scene_label();
        };
    });
}

namespace {

QString frame_text(const Clip& clip, std::int64_t index, const FrameStats& stats) {
    const GrainSet* grain = clip.grain_at(index);
    const QString signalled = grain ? qstring(grain->describe()) : QString("no film grain parameters");
    std::set<std::string> sets;
    for (const auto& set : clip.grain) {
        if (set) {
            sets.insert(set->describe());
        }
    }
    const std::size_t n_sets = sets.size();
    const VvcSettings& s = clip.settings;
    return "Signalled (FGC SEI) for this frame:\n" + signalled + "\n\nMeasured on this frame, 8-bit units:\n" +
           QString::asprintf("  removed by VVenC  σ  Y %5.2f  Cb %5.2f  Cr %5.2f\n", stats.removed[0], stats.removed[1],
                             stats.removed[2]) +
           QString::asprintf("  added by vvdec    σ  Y %5.2f  Cb %5.2f  Cr %5.2f\n", stats.added[0], stats.added[1],
                             stats.added[2]) +
           QString::asprintf("  colour shift      mean Y %+.3f  Cb %+.3f  Cr %+.3f\n\n", stats.shift[0], stats.shift[1],
                             stats.shift[2]) +
           "Scene: frames " + qstring(with_thousands(clip.start)) + "–" +
           qstring(with_thousands(clip.start + clip.count - 1)) + ", " + QString::number(n_sets) + " parameter set" +
           (n_sets != 1 ? "s" : "") + "; --fga " + QString::number(s.fga) + ", " + QString::number(s.coding.strength) +
           " %, QP " + QString::asprintf("%g", s.coding.qp) + ", " + qstring(s.coding.preset) + "; " +
           qstring(with_thousands(std::llround(clip.kbps()))) + " kb/s, encoded in " +
           QString::number(clip.encode_seconds, 'f', 1) + " s.";
}

}  // namespace

// Shows the frame from an encoded scene: source, decoded, decoded + grain, grain only.
void VvcTab::render_clip(std::shared_ptr<const Clip> clip, std::int64_t index) {
    if (running(frame_job_)) {
        frame_pending_ = true;
        return;
    }
    frame_pending_ = false;
    const std::shared_ptr<const Converter> converter = converter_for(clip->geometry);
    const std::string path = utf8(source_->path);
    start_frame_job(index, [this, clip, path, index, converter](CancelToken&, const Progress&) -> Job::Deliver {
        const Frame source = clip->frame(ClipPicture::source, index);
        const Frame decoded = clip->frame(ClipPicture::decoded, index);
        const Frame grained = clip->frame(ClipPicture::grained, index);
        // The four pictures on threads of their own: each converts in bands as well.
        auto source_image = std::async(std::launch::async, [&] { return to_qimage(*converter, source); });
        auto decoded_image = std::async(std::launch::async, [&] { return to_qimage(*converter, decoded); });
        auto grained_image = std::async(std::launch::async, [&] { return to_qimage(*converter, grained); });
        QImage only = grain_only_image(decoded.y, grained.y, clip->geometry.bit_depth);
        const FrameStats stats = frame_stats(source, decoded, grained);
        std::map<View, QImage> images = {{source_view, source_image.get()},
                                         {encoded_view, decoded_image.get()},
                                         {grain_view, grained_image.get()},
                                         {grain_only_view, std::move(only)}};
        QString text = frame_text(*clip, index, stats);
        return [this, path, index, images = std::move(images), text = std::move(text)] {
            if (!source_ || utf8(source_->path) != path) {
                return;  // another source was chosen meanwhile
            }
            if (index != current_index_) {
                frame_pending_ = true;
                return;
            }
            state_->set_images(images, caption(index));
            state_->set_mode(preferred_mode_);
            update_scene_label();
            info_label_->setText(text);
            if (!running(job_)) {
                status_->set_text("Frame " + qstring(with_thousands(index)) + ".");
            }
        };
    });
}

void VvcTab::frame_job_ended() {
    remove_retired();
    if (frame_pending_) {
        show_frame();
    }
}

void VvcTab::update_scene_label() {
    if (!source_) {
        scene_label_->clear();
        return;
    }
    const std::optional<JobKey> key = running(job_) ? job_key_ : std::nullopt;
    const auto [start, length] = scene_range();
    const auto range_text = [](std::int64_t first, std::int64_t count) {
        return qstring(with_thousands(first)) + "–" + qstring(with_thousands(first + count - 1));
    };
    QString text = "Encodes frames " + range_text(start, length) + " (" +
                   QString::number(static_cast<double>(length) / source_->fps.value(), 'f', 1) + " s).";
    if (scene_) {
        const std::int64_t first = scene_->cut ? scene_->cut->start : scene_->start;
        const std::int64_t count = scene_->cut ? scene_->cut->count : scene_->frames;
        QStringList modes;
        for (const FgaMode& mode : fga_modes) {
            QString state = stopped_ ? "stopped" : "waiting";
            if (scene_->clips.contains(mode.value)) {
                state = "ready";
            } else if (key && key->kind == JobKey::Kind::encode && key->scene == scene_->ident && key->fga == mode.value) {
                state = "encoding";
            }
            modes << QString("--fga %1 %2").arg(mode.value).arg(state);
        }
        const bool here = scene_->covers(current_index_);
        text = (here ? "This scene" : "Encoded scene") + QString(": frames ") + range_text(first, count) + ". " +
               modes.join(" · ") + ".";
        if (here) {
            if (!(scene_->base == current_settings().coding)) {
                text += " Encoded with other settings than these: press Encode this scene to use them.";
            } else if (!scene_->older.empty() && !scene_->clips.contains(selected_fga())) {
                text += " Shown with the earlier settings until the new encode is ready.";
            }
            if (frames_spin_->value() != scene_->frames) {
                text += QString(" The new scene length (%1) applies when Encode this scene is pressed again.")
                            .arg(frames_spin_->value());
            }
        } else {
            text += " Encode this scene would encode frames " + range_text(start, length) + ".";
        }
    }
    scene_label_->setText(text);
}

// ---------------------------------------------------------------- view modes

void VvcTab::set_mode(View view) {
    if (state_->set_mode(view)) {
        preferred_mode_ = view;
    } else if (view != source_view && source_) {
        status_->set_text("Press Encode this scene (E) to see the VVC views of this frame.");
    }
    sync_mode_buttons();
}

void VvcTab::sync_mode_buttons() {
    std::vector<View> available;
    for (const auto& [view, image] : state_->images()) {
        available.push_back(view);
    }
    pane_->sync_modes(available);
}

void VvcTab::open_viewer_window() {
    if (viewer_window_ == nullptr) {
        viewer_window_ = new ViewerWindow(state_, this);
        install_shortcuts(viewer_window_);
        if (settings_->contains("vvc/viewer_geometry")) {
            viewer_window_->restore_place(settings_->value("vvc/viewer_geometry").toByteArray());
        }
    }
    viewer_window_->show_sized();
}

// ---------------------------------------------------------------- closing

void VvcTab::shutdown() {
    if (!work_) {
        return;  // done already
    }
    save();
    frame_timer_.stop();  // each would start work
    settings_timer_.stop();
    for (const QPointer<Job>& job : {job_, frame_job_, probe_job_}) {
        stop_for_good(job, 10000);
    }
    if (viewer_window_ != nullptr) {
        settings_->setValue("vvc/viewer_geometry", viewer_window_->saveGeometry());
        viewer_window_->close();
    }
    retire_scene();
    work_.reset();  // deletes the work folder
}

void VvcTab::closeEvent(QCloseEvent* event) {
    shutdown();
    event->accept();
}

}  // namespace fgw::app
