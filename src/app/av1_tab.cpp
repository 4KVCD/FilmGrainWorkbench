// The AV1 tab: the controls, the settings, the videos, the jobs, the
// analysis and the tuning. Frames are in av1_frames.cpp, writing in av1_write.cpp.
#include "av1_tab.h"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <numeric>

#include <QCloseEvent>
#include <QDateTime>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QMessageBox>
#include <QScrollArea>
#include <QVBoxLayout>

#include "dialogs.h"
#include "fgw/alignment.h"
#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/text.h"
#include "release.h"
#include "settings.h"

namespace fgw::app {

namespace {

constexpr int frame_delay_ms = 120;     // a frame loads once stepping pauses this long
constexpr int tune_delay_ms = 150;      // the grain is synthesised again once a slider pauses
constexpr int offset_delay_ms = 400;    // "19" typed into the offset passes through 1 on the way
constexpr int max_strength_slider = 400;  // %, the slider's end; the box beside it goes further
const QString video_filter = "Video files (*.mkv *.mp4 *.mov *.webm *.ts *.m2ts);;All files (*)";
const ViewNames view_names = {{source_view, "Source"},
                              {encoded_view, "Encoded"},
                              {grain_view, "Encoded + grain"},
                              {grain_only_view, "Grain only (×4)"}};

QString qstring(const std::string& text) { return QString::fromStdString(text); }

QString strip_quotes(QString path) {
    path = path.trimmed();
    if (path.size() >= 2 && path.startsWith('"') && path.endsWith('"')) {
        path = path.mid(1, path.size() - 2);
    }
    return path;
}

QLabel* info_label(const QString& text = {}) {
    auto* label = new QLabel(text);
    label->setWordWrap(true);
    label->setObjectName("info");
    return label;
}

}  // namespace

// ---------------------------------------------------------------- FrameCache

std::shared_ptr<const Frame> FrameCache::get(const Key& key) {
    const auto found = std::find_if(entries_.begin(), entries_.end(), [&key](const Entry& e) { return e.key == key; });
    if (found == entries_.end()) {
        return nullptr;
    }
    Entry entry = std::move(*found);
    entries_.erase(found);
    entries_.push_back(std::move(entry));
    return entries_.back().frame;
}

void FrameCache::put(const Key& key, std::shared_ptr<const Frame> frame) {
    std::erase_if(entries_, [&key](const Entry& e) { return e.key == key; });
    entries_.push_back({key, std::move(frame)});
}

void FrameCache::trim(std::size_t budget_bytes) {
    std::size_t kept = 0;
    for (auto entry = entries_.rbegin(); entry != entries_.rend(); ++entry) {
        kept += entry->frame->bytes();
        if (kept > budget_bytes) {
            // This one and everything older.
            std::vector<Entry> dropped(std::make_move_iterator(entries_.begin()), std::make_move_iterator(entry.base()));
            entries_.erase(entries_.begin(), entry.base());
            release_later(std::move(dropped));
            return;
        }
    }
}

void FrameCache::clear() { release_later(std::exchange(entries_, {})); }

void FrameCache::drop_source_frames() {
    std::vector<Entry> dropped;
    std::deque<Entry> kept;
    for (Entry& entry : entries_) {
        if (entry.key.source) {
            dropped.push_back(std::move(entry));
        } else {
            kept.push_back(std::move(entry));
        }
    }
    entries_ = std::move(kept);
    release_later(std::move(dropped));
}

// ---------------------------------------------------------------- the tab

Av1Tab::Av1Tab(QSettings* settings, QString data_dir, QWidget* parent)
    : QMainWindow(parent), settings_(settings), data_dir_(std::move(data_dir)), state_(new PreviewState(view_names, this)) {
    setWindowTitle(app_title);
    migrate_settings(*settings_);
    tools_ = load_tools(*settings_);
    build();
    install_shortcuts(this);
    restore();
    frame_timer_.setSingleShot(true);
    frame_timer_.setInterval(frame_delay_ms);
    connect(&frame_timer_, &QTimer::timeout, this, [this] { request_frame(current_index_); });
    tune_timer_.setSingleShot(true);
    tune_timer_.setInterval(tune_delay_ms);
    connect(&tune_timer_, &QTimer::timeout, this, &Av1Tab::retune);
    offset_timer_.setSingleShot(true);
    offset_timer_.setInterval(offset_delay_ms);
    connect(&offset_timer_, &QTimer::timeout, this, &Av1Tab::offset_changed);
    QTimer::singleShot(0, this, &Av1Tab::startup);
}

Av1Tab::~Av1Tab() { shutdown(); }

// ---------------------------------------------------------------- layout

void Av1Tab::build() {
    auto* sidebar = new QWidget;
    auto* side = new QVBoxLayout(sidebar);
    side->setContentsMargins(12, 12, 12, 12);
    side->setSpacing(10);
    tools_banner_ = new QLabel;
    tools_banner_->setWordWrap(true);
    tools_banner_->setObjectName("warning");
    connect(tools_banner_, &QLabel::linkActivated, this, [this] { edit_tools(); });
    side->addWidget(tools_banner_);
    side->addWidget(videos_group());
    side->addWidget(tune_group());
    side->addWidget(write_group());
    add_log(side);
    side->addStretch(1);

    auto* scroll = new QScrollArea;
    scroll->setWidget(sidebar);
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setMinimumWidth(430);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    grain_timeline_ = new GrainTimeline;
    connect(grain_timeline_, &GrainTimeline::frame_clicked, this, [this](int index) { go_to(index); });
    grain_timeline_->hide();
    pane_ = new FramePane(state_, {}, grain_timeline_, shortcut_help("encoded", "encoded + grain"));
    connect(pane_, &FramePane::frame_requested, this, [this](int index) { go_to(index); });
    connect(pane_, &FramePane::step_requested, this, &Av1Tab::step);
    connect(pane_, &FramePane::mode_requested, this, &Av1Tab::set_mode);
    connect(pane_, &FramePane::popout_requested, this, &Av1Tab::open_viewer_window);

    splitter_ = new QSplitter;
    splitter_->addWidget(scroll);
    splitter_->addWidget(pane_);
    splitter_->setStretchFactor(1, 1);
    splitter_->setSizes({460, 1100});
    setCentralWidget(splitter_);
    status_ = std::make_unique<StatusBar>(this, [this] { cancel_job(); });

    connect(state_, &PreviewState::changed, this, &Av1Tab::sync_mode_buttons);
    update_estimate();
    update_enabled();
}

QGroupBox* Av1Tab::videos_group() {
    auto* box = new QGroupBox("1 · Videos");
    auto* grid = new QGridLayout(box);
    source_edit_ = new QLineEdit;
    encoded_edit_ = new QLineEdit;
    source_edit_->setPlaceholderText("Original, grainy source (e.g. the Blu-ray remux)");
    encoded_edit_->setPlaceholderText("VVC encode made from it");
    source_info_ = info_label();
    encoded_info_ = info_label();
    const std::tuple<QString, QLineEdit*, QLabel*, Kind> rows[] = {
        {"Source", source_edit_, source_info_, Kind::source}, {"Encoded VVC", encoded_edit_, encoded_info_, Kind::encoded}};
    int row = 0;
    for (const auto& [title, edit, info, kind] : rows) {
        auto* browse = new QPushButton("Browse…");
        connect(browse, &QPushButton::clicked, this, [this, kind = kind] { browse_video(kind); });
        connect(edit, &QLineEdit::editingFinished, this, [this, kind = kind] { load_video(kind); });
        grid->addWidget(new QLabel(title), row * 2, 0);
        grid->addWidget(edit, row * 2, 1);
        grid->addWidget(browse, row * 2, 2);
        grid->addWidget(info, row * 2 + 1, 1, 1, 2);
        ++row;
    }
    pair_note_ = info_label();
    auto* offset_row = new QHBoxLayout;
    offset_spin_ = new QSpinBox;
    offset_spin_->setRange(-100000, 100000);
    offset_spin_->setToolTip("Encoded frame N shows source frame N + offset. Leave at 0 unless the encode was trimmed "
                             "or has extra frames.");
    connect(offset_spin_, &QSpinBox::valueChanged, this, [this] { offset_timer_.start(); });
    detect_button_ = new QPushButton("Detect");
    detect_button_->setToolTip("Find the offset by matching frames at three places.");
    connect(detect_button_, &QPushButton::clicked, this, &Av1Tab::detect_offset_clicked);
    offset_row->addWidget(offset_spin_, 1);
    offset_row->addWidget(detect_button_);
    grid->addWidget(new QLabel("Frame offset"), 4, 0);
    grid->addLayout(offset_row, 4, 1, 1, 2);
    offset_note_ = info_label();
    offset_note_->setToolTip("How closely the source frame paired with the encoded frame shown matches it (PSNR of "
                             "the luma). The same frame matches far better than its neighbours; a low value usually "
                             "means a wrong offset.");
    grid->addWidget(offset_note_, 5, 1, 1, 2);
    grid->addWidget(pair_note_, 6, 0, 1, 3);
    return box;
}

QGroupBox* Av1Tab::tune_group() {
    auto* tune = new QGroupBox("2 · Tune grain");
    // Rows with a label sit in a form; everything else in a plain column,
    // where word-wrapped text gets the height it needs (a form row clips it).
    auto* column = new QVBoxLayout(tune);
    plot_ = new CurvePlot;
    column->addWidget(plot_);
    column->addSpacing(8);
    auto* form = new QFormLayout;
    column->addLayout(form);
    std::tie(luma_slider_, luma_spin_) =
        strength_row(form, "Luma strength", "Scales the luma grain the encode lost. 100 % = as measured.");
    std::tie(chroma_slider_, chroma_spin_) = strength_row(form, "Chroma strength", "Scales the colour grain. 0 % = none.");
    seed_spin_ = new QSpinBox;
    seed_spin_->setRange(0, 65535);
    seed_spin_->setValue(7391);
    seed_spin_->setToolTip("Base random seed. Every frame derives its own seed from it, so the grain moves; change it "
                           "only to get a different pattern.");
    connect(seed_spin_, &QSpinBox::valueChanged, this, &Av1Tab::tuning_changed);
    form->addRow("Seed", seed_spin_);
    overlap_check_ = new QCheckBox("Blend grain block edges (on in libaom)");
    overlap_check_->setChecked(true);
    overlap_check_->setToolTip("overlap_flag: blend the 32×32 grain blocks where they meet. libaom's noise model "
                               "always turns it on.");
    connect(overlap_check_, &QCheckBox::toggled, this, &Av1Tab::tuning_changed);
    clip_check_ = new QCheckBox("Clip grained picture to limited (TV) range (off in libaom)");
    clip_check_->setToolTip("clip_to_restricted_range: clip grained samples to 16–235 (chroma 16–240), scaled to the "
                            "bit depth. libaom's noise model never sets it, so grain in the darkest and brightest "
                            "areas is not cut off.");
    connect(clip_check_, &QCheckBox::toggled, this, &Av1Tab::tuning_changed);
    column->addWidget(overlap_check_);
    column->addWidget(clip_check_);
    tune_label_ = info_label();
    column->addWidget(tune_label_);
    auto* buttons = new QHBoxLayout;
    auto* reset = new QPushButton("Reset");
    reset->setToolTip("Back to 100 % luma and chroma strength.");
    connect(reset, &QPushButton::clicked, this, [this] {
        luma_spin_->setValue(100);
        chroma_spin_->setValue(100);
    });
    export_table_button_ = new QPushButton("Export grain table…");
    export_table_button_->setToolTip("Save the tuned parameters as an AOM film grain table (usable with aomenc / "
                                     "SVT-AV1 --fgs-table).");
    connect(export_table_button_, &QPushButton::clicked, this, &Av1Tab::export_table);
    buttons->addWidget(reset);
    buttons->addWidget(export_table_button_);
    column->addLayout(buttons);
    return tune;
}

QGroupBox* Av1Tab::write_group() {
    auto* write_box = new QGroupBox("3 · Write");
    auto* grid = new QGridLayout(write_box);
    output_edit_ = new QLineEdit;
    output_edit_->setPlaceholderText("Output .mkv");
    auto* browse = new QPushButton("Browse…");
    connect(browse, &QPushButton::clicked, this, &Av1Tab::browse_output);
    grid->addWidget(new QLabel("Output"), 0, 0);
    grid->addWidget(output_edit_, 0, 1);
    grid->addWidget(browse, 0, 2);
    auto* run_row = new QHBoxLayout;
    run_row->setContentsMargins(0, 0, 0, 0);
    workers_spin_ = new QSpinBox;
    workers_spin_->setRange(1, 128);
    workers_spin_->setValue(default_threads());
    workers_spin_->setToolTip("Threads for the every-frame grain analysis. All CPU threads is fastest; use fewer to "
                              "keep the PC responsive while it runs.");
    gpu_check_ = new QCheckBox("Decode source on GPU");
    gpu_check_->setChecked(true);
    gpu_check_->setToolTip("Let ffmpeg decode the source with hardware acceleration (bit-exact) to leave the CPU to "
                           "the grain analysis.");
    run_row->addWidget(workers_spin_);
    run_row->addWidget(new QLabel("threads"));
    run_row->addWidget(gpu_check_);
    run_row->addStretch(1);
    grid->addWidget(new QLabel("Analysis"), 1, 0);
    grid->addLayout(run_row, 1, 1, 1, 2);
    connect(workers_spin_, &QSpinBox::valueChanged, this, &Av1Tab::update_estimate);
    write_estimate_label_ = info_label();
    grid->addWidget(write_estimate_label_, 2, 0, 1, 3);
    analysis_label_ = info_label();
    analysis_label_->hide();
    grid->addWidget(analysis_label_, 6, 0, 1, 3);
    write_button_ = new QPushButton("Write MKV with grain metadata");
    write_button_->setProperty("primary", true);
    connect(write_button_, &QPushButton::clicked, this, &Av1Tab::write);
    grid->addWidget(write_button_, 3, 0, 1, 3);
    verify_button_ = new QPushButton("Verify with ffmpeg");
    verify_button_->setToolTip("Decode the current frame of the written file with ffmpeg and compare it to the preview.");
    connect(verify_button_, &QPushButton::clicked, this, &Av1Tab::verify);
    open_folder_button_ = new QPushButton("Open folder");
    connect(open_folder_button_, &QPushButton::clicked, this, &Av1Tab::open_folder);
    grid->addWidget(verify_button_, 4, 0, 1, 2);
    grid->addWidget(open_folder_button_, 4, 2);
    write_label_ = info_label();
    grid->addWidget(write_label_, 5, 0, 1, 3);
    return write_box;
}

// The activity log, folded away under its button.
void Av1Tab::add_log(QVBoxLayout* column) {
    log_toggle_ = new QToolButton;
    log_toggle_->setText("Activity log");
    log_toggle_->setCheckable(true);
    log_toggle_->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    log_toggle_->setArrowType(Qt::RightArrow);
    log_view_ = new QPlainTextEdit;
    log_view_->setReadOnly(true);
    log_view_->setMaximumBlockCount(2000);
    log_view_->setMinimumHeight(140);
    log_view_->hide();
    connect(log_toggle_, &QToolButton::toggled, this, [this](bool shown) {
        log_view_->setVisible(shown);
        log_toggle_->setArrowType(shown ? Qt::DownArrow : Qt::RightArrow);
    });
    column->addWidget(log_toggle_);
    column->addWidget(log_view_);
}

std::pair<QSlider*, QSpinBox*> Av1Tab::strength_row(QFormLayout* form, const QString& title, const QString& tip) {
    auto* row = new QHBoxLayout;
    auto* slider = new QSlider(Qt::Horizontal);
    slider->setRange(0, max_strength_slider);
    slider->setValue(100);
    slider->setPageStep(10);
    auto* spin = new QSpinBox;
    spin->setRange(0, 800);
    spin->setValue(100);
    spin->setSuffix(" %");
    slider->setToolTip(tip);
    spin->setToolTip(tip);
    connect(slider, &QSlider::valueChanged, spin, &QSpinBox::setValue);
    connect(spin, &QSpinBox::valueChanged, slider, [slider](int value) { slider->setValue(std::min(value, max_strength_slider)); });
    connect(spin, &QSpinBox::valueChanged, this, &Av1Tab::tuning_changed);
    row->addWidget(slider, 1);
    row->addWidget(spin);
    form->addRow(title, row);
    return {slider, spin};
}

void Av1Tab::install_shortcuts(QWidget* widget) {
    FrameView* view = widget == viewer_window_ ? viewer_window_->view() : pane_->view();
    install_viewer_shortcuts(
        widget, view, [this](View v) { set_mode(v); }, [this](int delta) { step(delta); },
        [this] { return frames_per_second(); });
}

// ---------------------------------------------------------------- settings

void Av1Tab::restore() {
    const QSettings& s = *settings_;
    if (s.contains("av1/splitter")) {
        splitter_->restoreState(s.value("av1/splitter").toByteArray());
    }
    luma_spin_->setValue(s.value("av1/tune/luma", 100).toInt());
    chroma_spin_->setValue(s.value("av1/tune/chroma", 100).toInt());
    seed_spin_->setValue(s.value("av1/tune/seed", 7391).toInt());
    overlap_check_->setChecked(s.value("av1/tune/overlap", true).toBool());
    clip_check_->setChecked(s.value("av1/tune/clip", false).toBool());
    workers_spin_->setValue(s.value("av1/analysis/threads", default_threads()).toInt());
    gpu_check_->setChecked(s.value("av1/analysis/gpu", true).toBool());
    update_estimate();
}

void Av1Tab::save() {
    QSettings& s = *settings_;
    s.setValue("av1/splitter", splitter_->saveState());
    save_tools(s, tools_);
    s.setValue("av1/source", source_edit_->text());
    s.setValue("av1/encoded", encoded_edit_->text());
    s.setValue("av1/analysis/threads", workers_spin_->value());
    s.setValue("av1/analysis/gpu", gpu_check_->isChecked());
    s.setValue("av1/tune/luma", luma_spin_->value());
    s.setValue("av1/tune/chroma", chroma_spin_->value());
    s.setValue("av1/tune/seed", seed_spin_->value());
    s.setValue("av1/tune/overlap", overlap_check_->isChecked());
    s.setValue("av1/tune/clip", clip_check_->isChecked());
}

void Av1Tab::startup() {
    update_tools_banner();
    source_edit_->setText(settings_->value("av1/source").toString());
    encoded_edit_->setText(settings_->value("av1/encoded").toString());
    if (!source_edit_->text().isEmpty()) {
        load_video(Kind::source);
    }
    if (!encoded_edit_->text().isEmpty()) {
        load_video(Kind::encoded);
    }
}

void Av1Tab::edit_tools() {
    ToolsDialog dialog(tools_, this);
    if (dialog.exec() == QDialog::Accepted) {
        tools_ = dialog.tools();
        save();
        update_tools_banner();
        update_enabled();
        emit tools_changed();
    }
}

void Av1Tab::update_tools_banner() {
    const std::vector<std::string> missing = tools_.missing();
    tools_banner_->setVisible(!missing.empty());
    if (!missing.empty()) {
        QStringList names;
        for (const std::string& name : missing) {
            names << qstring(name);
        }
        tools_banner_->setText("Missing: " + names.join(", ") + ". <a href='#'>Set up external programs…</a>");
    }
}

// ---------------------------------------------------------------- videos

void Av1Tab::browse_video(Kind kind) {
    QLineEdit* edit = kind == Kind::source ? source_edit_ : encoded_edit_;
    const QString start = edit->text().isEmpty() ? settings_->value("files/last_dir").toString() : edit->text();
    const QString path = QFileDialog::getOpenFileName(
        this, kind == Kind::source ? "Choose the source video" : "Choose the encoded VVC video", start, video_filter);
    if (!path.isEmpty()) {
        edit->setText(QDir::toNativeSeparators(path));
        settings_->setValue("files/last_dir", QFileInfo(path).absolutePath());
        load_video(kind);
    }
}

// Reads the video on a job thread (ffprobe takes up to a second): the window
// stays responsive meanwhile, and only the latest choice is used.
void Av1Tab::load_video(Kind kind) {
    QLineEdit* edit = kind == Kind::source ? source_edit_ : encoded_edit_;
    QLabel* label = kind == Kind::source ? source_info_ : encoded_info_;
    const QString path = strip_quotes(edit->text());
    const std::optional<VideoInfo>& current = kind == Kind::source ? source_ : encoded_;
    QPointer<Job>& probe_job = kind == Kind::source ? source_probe_ : encoded_probe_;
    QString& probing = probing_[kind == Kind::source ? 0 : 1];
    if ((current && qstring(utf8(current->path)) == path) || (running(probe_job) && probing == path)) {
        return;
    }
    probing = path;
    if (path.isEmpty()) {
        label->clear();
        video_probed(kind, std::nullopt, {});
        return;
    }
    if (!tools_.missing({"ffprobe"}).empty()) {
        label->setText("Set up ffprobe first (Settings → External programs).");
        video_probed(kind, std::nullopt, {});
        return;
    }
    label->setStyleSheet("");
    label->setText("Reading " + QFileInfo(path).fileName() + "…");
    const std::string ffprobe = tools_.ffprobe;
    auto* job = new Job("Reading the video", [this, kind, path, ffprobe](CancelToken& cancel, const Progress&) -> Job::Deliver {
        VideoInfo info = probe(utf8_path(path.toStdString()), ffprobe, &cancel);
        return [this, kind, path, info = std::move(info)] {
            if (probing_[kind == Kind::source ? 0 : 1] == path) {
                video_probed(kind, info, {});
            }
        };
    }, this);
    connect(job, &Job::failed, this, [this, kind, path](const QString& message) {
        if (probing_[kind == Kind::source ? 0 : 1] == path) {
            video_probed(kind, std::nullopt, message);
        }
    });
    probe_job = job;
    job->start();
}

void Av1Tab::video_probed(Kind kind, std::optional<VideoInfo> info, const QString& error) {
    QLabel* label = kind == Kind::source ? source_info_ : encoded_info_;
    if (info) {
        label->setText(qstring(info->describe()));
        label->setStyleSheet("");
    } else if (!error.isEmpty()) {
        label->setText(error.section('\n', 0, 0));
        label->setStyleSheet("color: #ff7b72;");
    }
    if (kind == Kind::source) {
        source_ = std::move(info);
    } else {
        encoded_ = std::move(info);
        if (encoded_ && encoded_->codec != "vvc") {
            label->setText(label->text() + "  —  not VVC: grain metadata can only be written into VVC.");
            label->setStyleSheet("color: #e3b341;");
        }
    }
    pair_changed();
    save();
}

void Av1Tab::pair_changed() {
    model_.reset();
    plan_.reset();
    frame_plan_.reset();
    written_.reset();
    estimates_.clear();
    frame_cache_.clear();
    crop_.reset();
    QStringList notes;
    if (encoded_) {
        const VideoInfo& enc = *encoded_;
        converter_ = std::make_shared<const Converter>(DisplayFormat{enc.bit_depth, enc.limited_range(), enc.matrix,
                                                                     enc.transfer, enc.primaries, enc.width, enc.height});
        pane_->set_frame_count(enc.frame_count());
        if (output_edit_->text().isEmpty() || output_is_auto_) {
            const std::filesystem::path auto_name =
                enc.path.parent_path() / (utf8(enc.path.stem()) + " AFGS1.mkv");
            output_edit_->setText(QDir::toNativeSeparators(qstring(utf8(auto_name))));
            output_is_auto_ = true;
        }
    }
    if (source_ && encoded_) {
        const VideoInfo& src = *source_;
        const VideoInfo& enc = *encoded_;
        crop_ = source_crop_for(src, enc);
        if (crop_) {
            notes << QString("Source is cropped to %1×%2 (offset %3,%4) to match the encode's picture.")
                         .arg(crop_->width).arg(crop_->height).arg(crop_->x).arg(crop_->y);
        }
        const int cw = crop_ ? crop_->width : src.width;
        const int ch = crop_ ? crop_->height : src.height;
        if (cw != enc.width || ch != enc.height) {
            notes << QString("Source is scaled from %1×%2 to %3×%4 for comparison; grain measured on a scaled source is "
                             "only approximate.").arg(cw).arg(ch).arg(enc.width).arg(enc.height);
        }
        if (std::abs(src.fps.value() - enc.fps.value()) > 0.01) {
            notes << QString("⚠ Frame rates differ (%1 vs %2 fps): frames cannot be matched one-to-one.")
                         .arg(src.fps.value(), 0, 'f', 3).arg(enc.fps.value(), 0, 'f', 3);
        }
        if (std::abs(src.duration - enc.duration) > 2) {
            notes << QString("Durations differ by %1 s — check the frame offset (Detect).")
                         .arg(std::abs(src.duration - enc.duration), 0, 'f', 1);
        }
    }
    pair_note_->setText(notes.join("\n"));
    pair_note_->setVisible(!notes.isEmpty());
    detected_.clear();
    offset_note_->clear();
    if (source_ && encoded_) {
        // Each pair keeps its own offset; a new pair starts at 0, not at the last pair's.
        offset_timer_.stop();
        const QSignalBlocker block(offset_spin_);
        offset_spin_->setValue(settings_->value("av1/offsets/" + pair_key(), 0).toInt());
    }
    applied_offset_ = offset_spin_->value();
    clear_model_ui();
    rebuild_plan();
    if (source_ && encoded_) {
        autoload_model();
    }
    if (encoded_) {
        go_to(current_index_ ? std::min(current_index_, encoded_->frame_count() - 1) : encoded_->frame_count() / 3, true);
    } else {
        state_->clear("Choose a source and an encoded video to see frames.");
    }
    update_estimate();
    update_enabled();
}

QString Av1Tab::pair_key() const {
    return qstring(sha1_hex(utf8(source_->path) + "|" + utf8(encoded_->path)).substr(0, 16));
}

// ---------------------------------------------------------------- enabling, jobs

void Av1Tab::update_enabled() {
    const bool working = busy();
    const bool pair = source_ && encoded_;
    for (QWidget* widget : std::initializer_list<QWidget*>{source_edit_, encoded_edit_, offset_spin_, workers_spin_, gpu_check_}) {
        widget->setEnabled(!working);
    }
    detect_button_->setEnabled(pair && !working);
    const QString blocker = write_blocker();
    write_button_->setEnabled(blocker.isEmpty() && !working);
    write_button_->setToolTip(blocker.isEmpty() ? "Analyses every frame (or reuses a finished analysis), then writes "
                                                  "the MKV with the strength set above."
                                                : blocker);
    verify_button_->setEnabled(written_.has_value() && !working);
    open_folder_button_->setEnabled(written_.has_value());
    export_table_button_->setEnabled(model_ != nullptr);
    sync_mode_buttons();
}

QString Av1Tab::write_blocker() const {
    if (!source_ || !encoded_) {
        return "Choose the source and the encoded video first.";
    }
    if (encoded_->codec != "vvc") {
        return "The encode is not VVC.";
    }
    if (!std::filesystem::is_regular_file(analyzer_path())) {
        return "The grain analyzer is missing. " + qstring(missing_program_hint());
    }
    if (!tools_.missing({"ffmpeg", "mkvmerge"}).empty()) {
        return "Needs ffmpeg and mkvmerge from MKVToolNix (Settings → External programs).";
    }
    return {};
}

void Av1Tab::start_job(const QString& name, Job::Work work, const QString& busy_text) {
    if (busy()) {
        return;
    }
    auto* job = new Job(name, std::move(work), this);
    connect(job, &Job::progressed, this, [this](double fraction, const QString& message) {
        status_->busy(fraction);
        const auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - job_started_).count();
        status_->set_text(message + QString("  (%1 s)").arg(std::lround(elapsed)));
    });
    connect(job, &Job::failed, this, [this, name](const QString& message) {
        status_->set_text(name + " failed.");
        log(name + " failed: " + message);
        QMessageBox::warning(this, name + " failed", message);
    });
    connect(job, &Job::cancelled, this, [this, name] {
        status_->set_text(name + " cancelled.");
        log(name + " cancelled.");
    });
    connect(job, &Job::ended, this, [this] {
        status_->idle();
        update_enabled();
    });
    job_ = job;
    job_started_ = std::chrono::steady_clock::now();
    status_->set_text(busy_text);
    status_->busy(0.0);
    log(busy_text);
    job->start();
    update_enabled();
}

void Av1Tab::cancel_job() {
    if (busy()) {
        status_->cancel_button()->setEnabled(false);
        status_->set_text("Cancelling…");
        job_->cancel();
    }
}

void Av1Tab::log(const QString& text) {
    log_view_->appendPlainText(QDateTime::currentDateTime().toString("HH:mm:ss  ") + text);
}

// ---------------------------------------------------------------- analysis

void Av1Tab::detect_offset_clicked() {
    const VideoInfo source = *source_;
    const VideoInfo encoded = *encoded_;
    const std::optional<Crop> crop = crop_;
    const Tools tools = tools_;
    start_job("Offset detection", [this, source, encoded, crop, tools](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
        progress(-1, "Matching frames at three places to find the offset…");
        const DetectedOffset found = detect_offset(tools, source, encoded, crop, &cancel);
        return [this, found] {
            {
                const QSignalBlocker block(offset_spin_);
                offset_spin_->setValue(static_cast<int>(found.offset));
            }
            const QString quality = found.confidence > 0.5 ? "confident" : "not certain — check a few frames";
            const QString text = QString("Detected offset %1 frames (%2).")
                                     .arg(QString::asprintf("%+lld", static_cast<long long>(found.offset)), quality);
            status_->set_text(text);
            log(text);
            offset_timer_.stop();
            offset_changed();
            detected_ = text;  // kept on screen: the frame loads that follow update the status bar
            offset_note_->setText(text);
        };
    }, "Detecting the frame offset…");
}

// Other source frames are paired with the encode now: estimate and show them again.
void Av1Tab::offset_changed() {
    if (offset_spin_->value() == applied_offset_) {
        return;  // e.g. "19" retyped over 19 passes through 1 on the way
    }
    applied_offset_ = offset_spin_->value();
    if (source_ && encoded_) {
        settings_->setValue("av1/offsets/" + pair_key(), offset_spin_->value());
    }
    detected_.clear();
    estimates_.clear();
    rebuild_plan();
    frame_cache_.drop_source_frames();
    update_estimate();
    go_to(current_index_, true);
}

// Uses an every-frame analysis for the preview (it is what Write puts in the file).
void Av1Tab::set_model(std::shared_ptr<const TimelineModel> model, const QString& loaded_from) {
    model_ = std::move(model);
    written_.reset();
    const TimelineModel& m = *model_;
    const double scale = 1 << (m.bit_depth - 8);
    QString took;
    if (m.seconds >= 3600) {
        const auto whole = static_cast<int>(m.seconds);
        took = QString(" %1 h %2 min").arg(whole / 3600).arg((whole % 3600) / 60, 2, 10, QChar('0'));
    } else if (m.seconds >= 60) {
        took = QString(" %1 min").arg(m.seconds / 60, 0, 'f', 0);
    } else if (m.seconds > 0) {
        took = QString(" %1 s").arg(m.seconds, 0, 'f', 0);
    }
    if (!took.isEmpty()) {
        took = " The analysis took" + took +
               (m.resumed ? " after resuming (not counting the part before)." : ".");
    }
    std::vector<double> values;
    for (const double v : m.sigma_values) {
        values.push_back(v / scale);
    }
    if (values.empty()) {
        values.push_back(0.0);
    }
    std::sort(values.begin(), values.end());
    // numpy's percentile: linear between the closest ranks.
    const auto percentile = [&values](double p) {
        const double rank = p / 100 * static_cast<double>(values.size() - 1);
        const auto below = static_cast<std::size_t>(rank);
        const std::size_t above = std::min(below + 1, values.size() - 1);
        return values[below] + (values[above] - values[below]) * (rank - static_cast<double>(below));
    };
    QString text = QString("Every frame analysed: %1 frames, %2 parameter changes. Grain σ at mid-grey across the film: "
                           "median %3, 5–95 % %4–%5 (8-bit).%6")
                       .arg(qstring(with_thousands(m.frames)), qstring(with_thousands(static_cast<std::int64_t>(m.segments().size()))))
                       .arg(percentile(50), 0, 'f', 2)
                       .arg(percentile(5), 0, 'f', 2)
                       .arg(percentile(95), 0, 'f', 2)
                       .arg(took);
    const auto shifted = std::count(m.alignment.begin(), m.alignment.end(), "shifted");
    if (shifted) {
        text += QString(" ⚠ %1 of %2 alignment checks matched a neighbouring frame better (check the offset).")
                    .arg(shifted).arg(m.alignment.size());
    }
    for (const std::string& warning : m.warnings) {
        text += " ⚠ " + qstring(warning);
    }
    if (!loaded_from.isEmpty()) {
        text = "Loaded from " + loaded_from + ". " + text;
    }
    analysis_label_->setText(text);
    analysis_label_->show();
    grain_timeline_->show();
    retune();
    update_enabled();
}

void Av1Tab::clear_model_ui() {
    grain_timeline_->hide();
    analysis_label_->hide();
    plot_->clear("The grain of the frame shown is estimated when it loads.");
    tune_label_->clear();
    write_label_->clear();
    state_->update_images({{grain_view, QImage()}, {grain_only_view, QImage()}});
}

// The saved every-frame analysis of this pair: reopening it restores the analysis.
QString Av1Tab::model_cache_file() const {
    if (!source_ || !encoded_) {
        return {};
    }
    FileStamp stamp;
    try {
        stamp = file_stamp(encoded_->path);
    } catch (const std::filesystem::filesystem_error&) {
        return {};  // the encode is gone
    }
    const std::string key = utf8(source_->path) + "|" + utf8(encoded_->path) + "|" + std::to_string(stamp.size) + "|" +
                            std::to_string(stamp.mtime);
    return QDir(data_dir_).filePath("models/" + qstring(sha1_hex(key).substr(0, 16)) + ".every-frame.json");
}

// Loads this pair's saved every-frame analysis in the background: a whole
// film is ~150,000 parameter sets. The window stays usable meanwhile,
// showing the live per-frame estimates until it arrives.
void Av1Tab::autoload_model() {
    const QString candidate = model_cache_file();
    if (candidate.isEmpty() || !QFileInfo(candidate).isFile()) {
        return;
    }
    const std::pair<std::string, std::string> pair = {utf8(source_->path), utf8(encoded_->path)};
    analysis_label_->setText("Loading the saved every-frame analysis of these files…");
    analysis_label_->show();
    const auto still_here = [this, pair] {
        return source_ && encoded_ && std::pair{utf8(source_->path), utf8(encoded_->path)} == pair && model_ == nullptr;
    };
    auto* job = new Job("Loading the saved analysis", [this, candidate, still_here](CancelToken&, const Progress&) -> Job::Deliver {
        std::ifstream file(utf8_path(candidate.toStdString()), std::ios::binary);
        const std::string text((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
        auto model = std::make_shared<const TimelineModel>(TimelineModel::from_json(text));
        return [this, model, still_here] {
            if (!still_here()) {
                return;  // another pair now, or a Write finished first
            }
            if (model->kind != "every-frame") {
                log("The saved analysis could not be read: not an every-frame analysis");
                analysis_label_->hide();
                return;
            }
            set_model(model, "the previous every-frame analysis");
        };
    }, this);
    connect(job, &Job::failed, this, [this, still_here](const QString& message) {
        if (still_here()) {
            log("The saved analysis could not be read: " + message);
            analysis_label_->hide();
        }
    });
    model_job_ = job;
    job->start();
}

void Av1Tab::export_table() {
    if (!plan_ || !model_) {
        return;
    }
    const QString path = QFileDialog::getSaveFileName(this, "Export AOM film grain table", "grain.tbl",
                                                      "Film grain table (*.tbl *.txt)");
    if (path.isEmpty()) {
        return;
    }
    std::vector<TableEntry> entries = model_->grain_table();
    for (std::size_t i = 0; i < entries.size(); ++i) {
        entries[i].params = plan_->params_at(model_->segments()[i].start).value_or(entries[i].params);
    }
    std::ofstream(utf8_path(path.toStdString()), std::ios::binary) << format_grain_table(entries);
    log(QString("Exported grain table (%1 entries) to %2").arg(qstring(with_thousands(static_cast<std::int64_t>(entries.size()))), path));
}

// ---------------------------------------------------------------- tuning

Tuning Av1Tab::tuning() const {
    return Tuning{luma_spin_->value() / 100.0, chroma_spin_->value() / 100.0, overlap_check_->isChecked(),
                  clip_check_->isChecked(), seed_spin_->value()};
}

void Av1Tab::tuning_changed() {
    if (encoded_) {
        tune_timer_.start();
    }
}

// Every-frame parameters when analysed, else the live per-frame estimates, at the set strength.
void Av1Tab::rebuild_plan() {
    frame_plan_.reset();
    if (model_) {
        plan_ = std::make_shared<TimelinePlan>(model_, tuning());
    } else if (encoded_) {
        frame_plan_ = std::make_shared<FramePlan>(estimates_, tuning(),
                                                  encoded_->subsampling_x == 1 && encoded_->subsampling_y == 1);
        plan_ = frame_plan_;
    } else {
        plan_.reset();
    }
}

void Av1Tab::retune() {
    if (!encoded_) {
        return;
    }
    rebuild_plan();
    if (model_) {
        const double scale = 1 << (model_->bit_depth - 8);
        std::vector<double> sigmas;
        for (const double v : model_->sigma_values) {
            sigmas.push_back(v / scale * tuning().luma_strength);
        }
        grain_timeline_->set_data(model_->frames, model_->sigma_frames, std::move(sigmas));
        grain_timeline_->set_current(current_index_);
    }
    update_segment_plot();
    save();
    render_grain();
}

// The brightness curves of the parameters in use at the current frame.
void Av1Tab::update_segment_plot() {
    if (!plan_ || !encoded_) {
        return;
    }
    const VideoInfo& enc = *encoded_;
    const std::int64_t index = current_index_;
    const std::optional<GrainParams> params = plan_->params_at(index);
    if (!params) {
        plot_->clear(source_ ? "Estimating the grain of this frame…" : "Choose the source video to estimate the grain.");
        tune_label_->clear();
        return;
    }
    const Sigmas sigmas = entry_sigmas(*params, enc.bit_depth, enc.subsampling_x, enc.subsampling_y);
    const auto in_8bit = [&enc](const std::array<double, 256>& values) {
        std::array<double, 256> out{};
        for (std::size_t i = 0; i < values.size(); ++i) {
            out[i] = sigma_in_8bit_units(values[i], enc.bit_depth);
        }
        return out;
    };
    const std::array<double, 256> luma = in_8bit(sigmas.y);
    const double mid = params->apply_grain && !params->y_points.empty() ? luma[128] : 0.0;
    plot_->set_data(luma, in_8bit(sigmas.cb), in_8bit(sigmas.cr));
    const QString strength = QString("%1 %").arg(tuning().luma_strength * 100, 0, 'f', 0);
    QString text;
    bool clipped = false;
    if (model_) {
        const Segment& segment = model_->segment_at(index);
        const std::int64_t span = segment.end - segment.start;
        // libaom's model moves on almost every frame, so most frames have their own parameters.
        const QString whose = span == 1 ? QString("the every-frame parameters measured for this frame")
                                        : QString("every-frame parameters shared by frames %1–%2, %3 s")
                                              .arg(qstring(with_thousands(segment.start)),
                                                   qstring(with_thousands(segment.end - 1)))
                                              .arg(static_cast<double>(span) / model_->fps.value(), 0, 'f', 1);
        text = QString("At this frame (%1): luma grain σ %2 at mid-grey (8-bit) with %3 strength.")
                   .arg(whose).arg(mid, 0, 'f', 2).arg(strength);
        clipped = plan_->clipped();
    } else if (!params->apply_grain) {
        text = "No grain estimate for this frame: it has too few flat areas to measure.";
    } else {
        text = QString("Estimated from this frame: luma grain σ %1 at mid-grey (8-bit) with %2 strength. Writing "
                       "analyses every frame the same way, carrying the estimate through each shot like libaom's "
                       "encoder.").arg(mid, 0, 'f', 2).arg(strength);
        clipped = frame_plan_ && frame_plan_->clipped_at(index);
    }
    if (clipped) {
        text += " ⚠ strength capped by the format's maximum.";
    }
    tune_label_->setText(text);
}

// ---------------------------------------------------------------- view modes

void Av1Tab::set_mode(View view) {
    if (!state_->set_mode(view)) {
        if (view == source_view) {
            status_->set_text("Choose the source video to see it here.");
        } else if (view == grain_view || view == grain_only_view) {
            status_->set_text("The grain appears once this frame's estimate is ready.");
        }
    }
    sync_mode_buttons();
}

// The views the frame has, and the grain views whenever there is a pair: they follow the frame.
void Av1Tab::sync_mode_buttons() {
    std::vector<View> available;
    for (const auto& [view, image] : state_->images()) {
        available.push_back(view);
    }
    if (source_ && encoded_) {
        available.push_back(grain_view);
        available.push_back(grain_only_view);
    }
    pane_->sync_modes(available);
}

void Av1Tab::open_viewer_window() {
    if (viewer_window_ == nullptr) {
        viewer_window_ = new ViewerWindow(state_, this);
        install_shortcuts(viewer_window_);
        if (settings_->contains("av1/viewer_geometry")) {
            viewer_window_->restoreGeometry(settings_->value("av1/viewer_geometry").toByteArray());
        }
    }
    viewer_window_->show_sized();
}

// ---------------------------------------------------------------- closing

bool Av1Tab::can_close() {
    if (!busy()) {
        return true;
    }
    const QString extra = job_->name() == "Writing"
                              ? " The grain analysis so far is kept; the next Write continues from there."
                              : QString();
    return QMessageBox::question(this, "Work in progress", job_->name() + " is still running. Cancel it and quit?" +
                                                               extra) == QMessageBox::Yes;
}

void Av1Tab::shutdown() {
    if (shut_down_) {
        return;
    }
    shut_down_ = true;
    for (const auto& [job, seconds] : {std::pair{job_, 10}, std::pair{frame_job_, 5}, std::pair{source_probe_, 5},
                                       std::pair{encoded_probe_, 5}}) {
        if (running(job)) {
            job->cancel();
            job->wait(static_cast<unsigned long>(seconds) * 1000);
        }
    }
    if (running(model_job_)) {
        model_job_->wait(15000);  // reading a saved analysis cannot be interrupted
    }
    if (viewer_window_ != nullptr) {
        settings_->setValue("av1/viewer_geometry", viewer_window_->saveGeometry());
        viewer_window_->close();
    }
    save();
}

void Av1Tab::closeEvent(QCloseEvent* event) {
    if (!can_close()) {
        event->ignore();
        return;
    }
    shutdown();
    event->accept();
}

}  // namespace fgw::app
