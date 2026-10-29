// The VVC tab's "Add the grain to an encode": VVenC's film grain analysis of
// the source, written into the user's own VVC encode as film grain
// characteristics SEI messages, one MKV per analysis mode. Nothing is
// encoded: the encode's pictures stay as they are.

#include <fstream>

#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QMessageBox>

#include "fgw/alignment.h"
#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/mux.h"
#include "fgw/text.h"
#include "fgw/vvc_fga.h"
#include "settings.h"
#include "vvc_tab.h"

namespace fgw::app {

namespace {

QString qstring(const std::string& text) { return QString::fromStdString(text); }

const QString encode_filter = "VVC encodes (*.mkv *.mp4 *.mov);;All files (*)";

QString without_quotes(QString path) {
    path = path.trimmed();
    if (path.size() >= 2 && path.startsWith('"') && path.endsWith('"')) {
        path = path.mid(1, path.size() - 2);
    }
    return path;
}

// What an analysis depends on, to use it again for a write with the same:
// both files as they are on disk, the pairing, the settings and the VVenC
// that analysed.
std::string analysis_key(const VideoInfo& source, const VideoInfo& encoded, std::int64_t offset,
                         const std::optional<Crop>& crop, const VvcCoding& coding, const std::string& vvencapp) {
    std::string key;
    for (const std::filesystem::path& file : {source.path, encoded.path, utf8_path(vvencapp)}) {
        std::error_code error;
        const auto size = std::filesystem::file_size(file, error);
        const auto time = std::filesystem::last_write_time(file, error).time_since_epoch().count();
        key += utf8(file) + "|" + std::to_string(size) + "|" + std::to_string(time) + "|";
    }
    const auto [first, last] = frame_range(encoded, source, offset);
    key += std::to_string(offset) + "|" + std::to_string(first) + "|" + std::to_string(last) + "|" +
           format("%g", coding.qp) + "|" + coding.preset + "|" + std::to_string(coding.strength);
    if (crop) {
        key += format("|%d,%d,%d,%d", crop->width, crop->height, crop->x, crop->y);
    }
    return key;
}

// Whether the two frame rates pair frames one to one.
bool same_frame_rate(const VideoInfo& source, const VideoInfo& encoded) {
    return std::abs(source.fps.value() - encoded.fps.value()) <= 0.01;
}

// How the source is read to match the encode's picture, and what stops a pairing.
QString pair_notes(const VideoInfo& source, const VideoInfo& encoded) {
    QStringList notes;
    const std::optional<Crop> crop = source_crop_for(source, encoded);
    if (crop) {
        notes << QString("The source is cropped to %1×%2 to match the encode's picture.").arg(crop->width).arg(crop->height);
    }
    const int width = crop ? crop->width : source.width;
    const int height = crop ? crop->height : source.height;
    if (width != encoded.width || height != encoded.height) {
        notes << QString("The source is scaled from %1×%2 to the encode's %3×%4 for the analysis.")
                     .arg(width).arg(height).arg(encoded.width).arg(encoded.height);
    }
    if (!same_frame_rate(source, encoded)) {
        notes << QString("⚠ The frame rates differ (%1 and %2 fps): the frames cannot be paired.")
                     .arg(source.fps.value(), 0, 'f', 3).arg(encoded.fps.value(), 0, 'f', 3);
    }
    if (vvenc_hdr_mode(source) != vvenc_hdr_mode(encoded)) {
        notes << QString("The source is %1 and the encode is tagged %2: the grain is measured on the source's "
                         "pictures as they are.")
                     .arg(vvenc_hdr_mode(source) == "off" ? "SDR" : "HDR")
                     .arg(vvenc_hdr_mode(encoded) == "off" ? "SDR (or not at all)" : "HDR");
    }
    return notes.join("\n");
}

// The last analysis, kept in the app's data folder (one, as the AV1 tab keeps its own): vvencapp's output and,
// in a text file, its key, first frame and frame count. A write with the same key uses it again, also in a
// later session: an analysis of a film can take hours.
struct KeptAnalysis {
    std::filesystem::path segments;
    std::filesystem::path key;
};

KeptAnalysis kept_analysis_files() {
    const std::filesystem::path folder = utf8_path(data_folder().toStdString());
    return {folder / "vvc-fga-analysis.266", folder / "vvc-fga-analysis.txt"};
}

std::shared_ptr<const FgaAnalysis> read_kept_analysis(const KeptAnalysis& files, const std::string& key) {
    std::ifstream text(files.key);
    std::string kept_key;
    FgaAnalysis analysis;
    if (!std::getline(text, kept_key) || kept_key != key || !(text >> analysis.first >> analysis.count)) {
        return nullptr;
    }
    std::ifstream stream(files.segments, std::ios::binary);
    const std::vector<std::uint8_t> data((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    try {
        analysis.segments = read_fga_segments(data);
    } catch (const std::invalid_argument&) {
        return nullptr;
    }
    return std::make_shared<const FgaAnalysis>(std::move(analysis));
}

void keep_analysis(const KeptAnalysis& files, const std::filesystem::path& segments, const std::string& key,
                   const FgaAnalysis& analysis) {
    std::error_code error;
    std::filesystem::create_directories(files.key.parent_path(), error);
    // The old key goes first: a copy that fails or is cut short (the app killed) must not leave the new
    // segments, or part of them, under the old analysis's key.
    if (std::filesystem::remove(files.key, error); error) {
        return;
    }
    std::filesystem::copy_file(segments, files.segments, std::filesystem::copy_options::overwrite_existing, error);
    if (!error) {
        std::ofstream(files.key) << key << "\n" << analysis.first << " " << analysis.count << "\n";
    }
}

// Removes a folder when it goes, whatever the outcome.
struct RemovedFolder {
    std::filesystem::path path;
    ~RemovedFolder() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};


}  // namespace

QGroupBox* VvcTab::write_group() {
    auto* box = new QGroupBox("4 · Add the grain to your encode");
    auto* grid = new QGridLayout(box);
    auto* intro = new QLabel("VVenC analyses the source's grain with the strength, QP and preset above, without "
                             "encoding, and writes it into your VVC encode: one MKV per analysis mode, the encode's "
                             "pictures unchanged.");
    intro->setObjectName("info");
    intro->setWordWrap(true);
    encode_edit_ = new QLineEdit;
    encode_edit_->setPlaceholderText("Your VVC encode of this source (MKV, MP4 or MOV)");
    connect(encode_edit_, &QLineEdit::editingFinished, this, &VvcTab::load_encode);
    encode_browse_ = new QPushButton("Browse…");
    connect(encode_browse_, &QPushButton::clicked, this, &VvcTab::browse_encode);
    encode_info_ = new QLabel;
    encode_info_->setObjectName("info");
    encode_info_->setWordWrap(true);
    offset_label_ = new QLabel;
    offset_label_->setToolTip("Source frame shown by the encode's frame 0. Detect finds it, up to ±48 frames, by "
                              "matching frames.");
    detect_button_ = new QPushButton("Detect");
    connect(detect_button_, &QPushButton::clicked, this, &VvcTab::detect_offset);
    auto* offset_row = new QHBoxLayout;
    offset_row->addWidget(offset_label_, 1);
    offset_row->addWidget(detect_button_);
    auto* modes_row = new QHBoxLayout;
    modes_row->addWidget(new QLabel("Modes"));
    for (std::size_t i = 0; i < std::size(fga_modes); ++i) {
        write_checks_[i] = new QCheckBox(QString("--fga %1").arg(fga_modes[i].value));
        write_checks_[i]->setChecked(true);
        write_checks_[i]->setToolTip(fga_modes[i].label);
        connect(write_checks_[i], &QCheckBox::toggled, this, [this] {
            save();
            update_write_controls();
        });
        modes_row->addWidget(write_checks_[i]);
    }
    modes_row->addStretch(1);
    output_edit_ = new QLineEdit;
    output_edit_->setPlaceholderText("Next to the encode");
    output_edit_->setToolTip("The folder the files are written to, named after the encode: "
                             "\"<encode> - VVenC fga N.mkv\". Empty: the encode's folder.");
    connect(output_edit_, &QLineEdit::editingFinished, this, &VvcTab::save);
    output_browse_ = new QPushButton("Browse…");
    connect(output_browse_, &QPushButton::clicked, this, &VvcTab::browse_output);
    auto* output_row = new QHBoxLayout;
    output_row->addWidget(new QLabel("Output folder"));
    output_row->addWidget(output_edit_, 1);
    output_row->addWidget(output_browse_);
    write_button_ = new QPushButton("Write");
    write_button_->setToolTip("Analyse the source frames the encode shows and write the encode with each chosen "
                              "mode's grain into the output folder.");
    connect(write_button_, &QPushButton::clicked, this, &VvcTab::write_or_cancel);
    write_label_ = new QLabel;
    write_label_->setObjectName("info");
    write_label_->setWordWrap(true);
    write_label_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    grid->addWidget(intro, 0, 0, 1, 2);
    grid->addWidget(encode_edit_, 1, 0);
    grid->addWidget(encode_browse_, 1, 1);
    grid->addWidget(encode_info_, 2, 0, 1, 2);
    grid->addLayout(offset_row, 3, 0, 1, 2);
    grid->addLayout(modes_row, 4, 0, 1, 2);
    grid->addLayout(output_row, 5, 0, 1, 2);
    grid->addWidget(write_button_, 6, 0, 1, 2);
    grid->addWidget(write_label_, 7, 0, 1, 2);
    return box;
}

void VvcTab::browse_encode() {
    const QString start = encode_edit_->text().isEmpty() ? settings_->value("files/last_dir").toString()
                                                         : encode_edit_->text();
    const QString path = QFileDialog::getOpenFileName(this, "Choose your VVC encode", start, encode_filter);
    if (!path.isEmpty()) {
        encode_edit_->setText(QDir::toNativeSeparators(path));
        settings_->setValue("files/last_dir", QFileInfo(path).absolutePath());
        load_encode();
    }
}

void VvcTab::browse_output() {
    const QString start = !without_quotes(output_edit_->text()).isEmpty() ? without_quotes(output_edit_->text())
                          : encoded_ ? qstring(utf8(encoded_->path.parent_path()))
                                     : settings_->value("files/last_dir").toString();
    const QString folder = QFileDialog::getExistingDirectory(this, "Choose where to write the files", start);
    if (!folder.isEmpty()) {
        output_edit_->setText(QDir::toNativeSeparators(folder));
        save();
    }
}

void VvcTab::load_encode() {
    const QString path = without_quotes(encode_edit_->text());
    if ((encoded_ && qstring(utf8(encoded_->path)) == path) || (running(encode_probe_job_) && probing_encode_ == path)) {
        return;
    }
    encoded_.reset();
    probing_encode_ = path;
    save();
    update_write_controls();
    if (path.isEmpty()) {
        encode_info_->clear();
        return;
    }
    if (const std::vector<std::string> missing = tools_.missing({"ffmpeg", "ffprobe"}); !missing.empty()) {
        probing_encode_.clear();
        encode_info_->setText("Set up ffmpeg and ffprobe first (Settings → External programs).");
        return;
    }
    encode_info_->setText("Reading " + QFileInfo(path).fileName() + "…");
    const std::string ffprobe = tools_.ffprobe;
    auto* job = new Job("Reading the encode", [this, path, ffprobe](CancelToken& cancel, const Progress&) -> Job::Deliver {
        VideoInfo info = probe(utf8_path(path.toStdString()), ffprobe, &cancel);
        if (info.codec != "vvc") {
            throw std::invalid_argument("its video is " + info.codec + ", not VVC");
        }
        check_writable(info);  // a raw .266 or a .ts has no vvcC record to write packets with
        return [this, path, info = std::move(info)] {
            if (probing_encode_ == path) {
                encoded_ = info;
                update_write_controls();
            }
        };
    }, this);
    connect(job, &Job::failed, this, [this, path](const QString& message) {
        if (probing_encode_ == path) {
            encode_info_->setText("Cannot use this encode: " + message);
        }
    });
    encode_probe_job_ = job;
    job->start();
}

void VvcTab::update_write_controls() {
    if (source_ && encoded_) {
        // The offset belongs to the pair it was set for: a different pair starts at 0.
        const QString pair = qstring(utf8(source_->path)) + "|" + qstring(utf8(encoded_->path));
        if (pair != offset_pair_) {
            const bool restored_pair = offset_pair_.isEmpty() && settings_->value("vvc/encode_offset_pair") == pair;
            offset_pair_ = pair;
            if (!restored_pair && offset_ != 0) {
                set_offset(0);
                write_label_->setText("Another source or encode: the frame offset is 0 again. Detect finds it.");
            }
            save();
        }
    }
    if (encoded_) {
        const QString notes = source_ ? pair_notes(*source_, *encoded_) : QString();
        encode_info_->setText(qstring(encoded_->describe()) + (notes.isEmpty() ? QString() : "\n" + notes));
    }
    const bool busy = running(write_job_);
    const bool any_mode = std::any_of(std::begin(write_checks_), std::end(write_checks_),
                                      [](const QCheckBox* check) { return check->isChecked(); });
    const bool paired = source_ && encoded_ && same_frame_rate(*source_, *encoded_);
    write_button_->setText(busy ? "Cancel" : "Write");
    write_button_->setEnabled(busy || (paired && any_mode));
    detect_button_->setEnabled(!busy && paired);
    encode_edit_->setEnabled(!busy);
    encode_browse_->setEnabled(!busy);
    output_edit_->setEnabled(!busy);
    output_browse_->setEnabled(!busy);
    for (QCheckBox* check : write_checks_) {
        check->setEnabled(!busy);
    }
}

// The file a mode is written to: in the output folder (or next to the encode), named after the encode.
// prepare_write refuses a relative output folder.
std::filesystem::path VvcTab::output_for(int fga) const {
    const std::filesystem::path& encode = encoded_->path;
    const QString chosen = without_quotes(output_edit_->text());
    const std::filesystem::path folder = chosen.isEmpty() ? encode.parent_path() : utf8_path(chosen.toStdString());
    return folder / utf8_path(utf8(encode.stem()) + " - VVenC fga " + std::to_string(fga) + ".mkv");
}

void VvcTab::start_write_job(const QString& name, Job::Work work) {
    auto* job = new Job(name, std::move(work), this);
    connect(job, &Job::progressed, this, [this](double fraction, const QString& message) {
        write_label_->setText(fraction >= 0 ? QString("%1 % · %2").arg(std::lround(fraction * 100)).arg(message)
                                            : message);
    });
    connect(job, &Job::failed, this, [this, name](const QString& message) {
        // The first line in the sidebar; all of it, which can be long (a program's log), in a message box.
        const QString first_line = message.section('\n', 0, 0);
        write_label_->setText(name + " failed: " + first_line);
        if (message.contains('\n')) {
            auto* box = new QMessageBox(QMessageBox::Warning, name + " failed", first_line, QMessageBox::Ok, this);
            box->setDetailedText(message);
            box->setAttribute(Qt::WA_DeleteOnClose);
            box->open();
        }
    });
    connect(job, &Job::cancelled, this, [this] { write_label_->setText("Cancelled."); });
    connect(job, &Job::ended, this, &VvcTab::update_write_controls);
    write_job_ = job;
    job->start();
    update_write_controls();
}

void VvcTab::detect_offset() {
    if (!source_ || !encoded_ || running(write_job_)) {
        return;
    }
    const VideoInfo source = *source_;
    const VideoInfo encoded = *encoded_;
    const std::optional<Crop> crop = source_crop_for(source, encoded);
    const Tools tools = tools_;
    start_write_job("Offset detection", [this, source, encoded, crop, tools](CancelToken& cancel,
                                                                            const Progress& progress) -> Job::Deliver {
        progress(-1, "Matching frames at three places to find the offset…");
        const DetectedOffset found = fgw::detect_offset(tools, source, encoded, crop, &cancel);
        return [this, found, source_path = source.path, encoded_path = encoded.path] {
            if (!source_ || !encoded_ || source_->path != source_path || encoded_->path != encoded_path) {
                return;  // found for videos no longer loaded
            }
            set_offset(static_cast<int>(found.offset));
            write_label_->setText(QString("Detected offset %1 frames (%2).")
                                      .arg(QString::asprintf("%+lld", static_cast<long long>(found.offset)))
                                      .arg(found.confidence > 0.5 ? "confident" : "not certain — check a few frames"));
        };
    });
}

void VvcTab::set_offset(int offset) {
    offset_ = offset;
    offset_label_->setText(offset == 0 ? QString("Frame offset: 0")
                                       : QString::asprintf("Frame offset: %+d", offset));
    save();
}

void VvcTab::write_or_cancel() {
    if (running(write_job_)) {
        write_job_->cancel();
        return;
    }
    if (!source_ || !encoded_) {
        return;
    }
    if (const std::optional<QString> problem = prepare_write()) {
        write_label_->setText(*problem);
        return;
    }
    std::vector<int> modes;
    for (std::size_t i = 0; i < std::size(fga_modes); ++i) {
        if (write_checks_[i]->isChecked()) {
            modes.push_back(fga_modes[i].value);
        }
    }
    std::vector<std::filesystem::path> outputs;
    QStringList existing;
    for (const int fga : modes) {
        outputs.push_back(output_for(fga));
        std::error_code error;
        if (std::filesystem::exists(outputs.back(), error)) {
            existing << qstring(utf8(outputs.back().filename()));
        }
    }
    if (!existing.isEmpty() && confirm_replace_ &&
        QMessageBox::question(this, "Replace the files?", "These files exist already and would be replaced:\n\n" +
                                                             existing.join("\n")) != QMessageBox::Yes) {
        return;
    }
    const VideoInfo source = *source_;
    const VideoInfo encoded = *encoded_;
    const std::optional<Crop> crop = source_crop_for(source, encoded);  // the encode's picture, not the scenes' setting
    const std::int64_t offset = offset_;
    const VvcCoding coding = current_settings().coding;
    const Tools tools = tools_;
    const VvcTools vvc_tools = vvc_tools_;
    const std::filesystem::path folder = work_->path() / "write";
    const std::string key = analysis_key(source, encoded, offset, crop, coding, vvc_tools.vvencapp);
    const KeptAnalysis kept_files = kept_analysis_files();
    // What the job did, read once it has ended, whatever its outcome.
    struct Done {
        bool analysed = false;  // an analysis is kept for the next write
        std::vector<std::filesystem::path> written;
    };
    auto done = std::make_shared<Done>();
    save();
    start_write_job("Adding the grain", [=, this](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
        const KeepAwake awake;
        std::shared_ptr<const FgaAnalysis> analysis = read_kept_analysis(kept_files, key);
        const bool reused = analysis != nullptr;
        // Each step shows its own progress: the analysis takes hours on a film and a write minutes, so no
        // weighting of them into one percentage would match the time, and a job percentage beside the analysis's
        // frame count contradicted it.
        const std::size_t steps = modes.size() + (reused ? 0 : 1);
        std::size_t step = 0;
        const auto step_progress = [&](const std::string& what) {
            const std::string prefix =
                "Step " + std::to_string(++step) + " of " + std::to_string(steps) + " · " + what;
            return [&progress, prefix](double f, const std::string& m) { progress(f, prefix + m); };
        };
        if (!analysis) {
            const RemovedFolder work{folder};
            std::filesystem::create_directories(folder);
            analysis = std::make_shared<const FgaAnalysis>(analyse_film_grain(
                tools, vvc_tools, source, encoded, offset, crop, coding, folder, step_progress(""), &cancel));
            keep_analysis(kept_files, folder / "fga-segments.266", key, *analysis);
        }
        done->analysed = true;
        std::vector<std::string> notes;
        for (std::size_t i = 0; i < modes.size(); ++i) {
            const Progress write_progress = step_progress("--fga " + std::to_string(modes[i]) + ": ");
            write_progress(0, "writing " + utf8(outputs[i].filename()) + "…");
            const WriteReport report = write_fgc_mkv(tools, encoded, outputs[i], *analysis, modes[i], write_progress, &cancel);
            done->written.push_back(outputs[i]);
            for (const std::string& note : report.notes) {
                if (std::find(notes.begin(), notes.end(), note) == notes.end()) {
                    notes.push_back(note);
                }
            }
        }
        return [this, frames = analysis->count, reused, outputs, notes] {
            QStringList names;
            for (const std::filesystem::path& output : outputs) {
                names << qstring(utf8(output.filename()));
            }
            QString text = QString("%1 %2 frames and wrote, in %3:\n%4")
                               .arg(reused ? "Used the kept analysis of" : "Analysed")
                               .arg(qstring(with_thousands(frames)),
                                    QDir::toNativeSeparators(qstring(utf8(outputs.front().parent_path()))),
                                    names.join("\n"));
            for (const std::string& note : notes) {
                text += "\n" + qstring(note);
            }
            write_label_->setText(text);
        };
    });
    // A failed or cancelled write: which files it had written, and whether the analysis is kept.
    connect(write_job_, &Job::ended, this, [this, done, total = outputs.size()] {
        if (done->written.size() == total) {
            return;  // all written
        }
        QString text = write_label_->text();
        if (!done->written.empty()) {
            QStringList names;
            for (const std::filesystem::path& output : done->written) {
                names << qstring(utf8(output.filename()));
            }
            text += "\nWritten before that: " + names.join(", ") + ".";
        }
        if (done->analysed) {
            text += "\nThe analysis is kept: Write again uses it.";
        }
        write_label_->setText(text);
    });
}

bool VvcTab::can_close() {
    if (!running(write_job_) || write_job_->name() != "Adding the grain") {
        return true;
    }
    return QMessageBox::question(this, "Work in progress",
                                 "Adding the grain is still running. Cancel it and quit? A finished analysis is "
                                 "kept for the next Write.") == QMessageBox::Yes;
}

// Why a write could not succeed, found before hours of analysis; nothing if it can start. Makes the output
// folder.
std::optional<QString> VvcTab::prepare_write() {
    if (!same_frame_rate(*source_, *encoded_)) {
        return QString("The encode's frame rate differs from the source's: its frames cannot be paired.");
    }
    if (!vvenc_frame_rate(encoded_->fps)) {
        return QString("VVenC does not take the encode's frame rate (%1 fps).").arg(encoded_->fps.value(), 0, 'f', 3);
    }
    if (const std::vector<std::string> missing = tools_.missing({"ffmpeg", "ffprobe", "mkvmerge"}); !missing.empty()) {
        return QString("Set up %1 first (Settings → External programs).").arg(qstring(missing.front()));
    }
    std::error_code error;
    if (vvc_tools_.vvencapp.empty() || !std::filesystem::is_regular_file(utf8_path(vvc_tools_.vvencapp), error)) {
        return QString("Not found: vvencapp. ") + qstring(missing_program_hint());
    }
    try {
        frame_range(*encoded_, *source_, offset_);
    } catch (const std::invalid_argument&) {
        return QString("At this frame offset the encode shows no frame of the source.");
    }
    // The output folder: a full path, made if need be, where a file can be written.
    const std::filesystem::path folder = output_for(1).parent_path();
    if (folder.is_relative()) {
        return QString("Choose the output folder with its full path (or leave it empty: next to the encode).");
    }
    std::filesystem::create_directories(folder, error);
    const std::filesystem::path probe_file = folder / ".fgw-write-test";
    const bool writable = std::ofstream(probe_file).good();
    std::filesystem::remove(probe_file, error);
    if (!writable) {
        return "Cannot write to the output folder " + QDir::toNativeSeparators(qstring(utf8(folder))) + ".";
    }
    return std::nullopt;
}

}  // namespace fgw::app
