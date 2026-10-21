// The AV1 tab's Write and Verify: analyse every frame, write the MKV, check
// it with ffmpeg.
#include <cmath>
#include <fstream>

#include <QDesktopServices>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QMessageBox>
#include <QUrl>

#include "av1_tab.h"
#include "fgw/afgs1.h"
#include "fgw/frames.h"
#include "fgw/fullscan.h"
#include "fgw/synth.h"
#include "fgw/text.h"

namespace fgw::app {

namespace {

constexpr double long_run_seconds = 600;  // Write asks first when the analysis would take longer

QString qstring(const std::string& text) { return QString::fromStdString(text); }
QString thousands(std::int64_t value) { return qstring(with_thousands(value)); }

// "about 3 h 49 min", "about 12 min"
QString duration_text(double seconds) {
    const auto minutes = static_cast<std::int64_t>(seconds / 60);
    const std::int64_t hours = minutes / 60;
    return hours ? QString("about %1 h %2 min").arg(hours).arg(minutes % 60, 2, 10, QChar('0'))
                 : QString("about %1 min").arg(std::max<std::int64_t>(1, minutes % 60));
}

// Keeps Windows awake on the thread that creates it, until it goes. Windows
// tracks this per thread, so the job that runs the Write holds it itself.
class KeepAwake {
public:
    KeepAwake() { keep_awake(true); }
    ~KeepAwake() { keep_awake(false); }
    KeepAwake(const KeepAwake&) = delete;
    KeepAwake& operator=(const KeepAwake&) = delete;
};

}  // namespace

std::filesystem::path Av1Tab::every_frame_folder() const {
    if (!source_ || !encoded_) {
        return {};
    }
    try {
        return utf8_path(data_dir_.toStdString()) / "every-frame" / cache_key(*source_, *encoded_, offset_spin_->value(), crop_);
    } catch (const std::filesystem::filesystem_error&) {
        return {};  // a video is gone
    }
}

std::pair<Av1Tab::AnalysisState, std::optional<std::int64_t>> Av1Tab::every_frame_state() const {
    const std::filesystem::path folder = every_frame_folder();
    if (folder.empty()) {
        return {AnalysisState::none, std::nullopt};
    }
    std::error_code error;
    if (std::filesystem::exists(folder / "done.json", error) && std::filesystem::exists(folder / "table.tbl", error)) {
        return {AnalysisState::done, std::nullopt};
    }
    const std::optional<std::int64_t> at = checkpoint_frame(folder / "checkpoint.bin");
    return at ? std::pair{AnalysisState::resumable, at} : std::pair{AnalysisState::none, std::optional<std::int64_t>()};
}

QString Av1Tab::every_frame_text() const {
    if (!std::filesystem::is_regular_file(analyzer_path())) {
        return "The grain analyzer is missing. " + qstring(missing_program_hint());
    }
    if (!encoded_ || !source_) {
        return "Writing analyses the grain of every frame, like libaom's encoder, then adds it.";
    }
    if (encoded_->codec != "vvc") {
        return "This encode is not VVC, so grain metadata cannot be written into it.";
    }
    const auto [state, at] = every_frame_state();
    if (state == AnalysisState::done) {
        return "Every frame is already analysed (" + thousands(encoded_->frame_count()) +
               " frames): writing takes a minute or two.";
    }
    const double seconds = estimate_seconds(*encoded_, workers_spin_->value(), &*source_);
    QString text = "Writing first analyses all " + thousands(encoded_->frame_count()) + " frames (" +
                   duration_text(seconds) + " on this PC), then adds the grain.";
    if (state == AnalysisState::resumable) {
        text += " A stopped analysis continues from frame " + thousands(*at) + ".";
    }
    return text + " Progress is saved about once a minute.";
}

void Av1Tab::update_estimate() { write_estimate_label_->setText(every_frame_text()); }

void Av1Tab::browse_output() {
    const QString start = !output_edit_->text().isEmpty() ? output_edit_->text()
                          : encoded_                      ? qstring(utf8(encoded_->path.parent_path()))
                                                          : QString();
    QString path = QFileDialog::getSaveFileName(this, "Write MKV", start, "Matroska video (*.mkv)");
    if (path.isEmpty()) {
        return;
    }
    if (!path.endsWith(".mkv", Qt::CaseInsensitive)) {
        path += ".mkv";
    }
    output_edit_->setText(QDir::toNativeSeparators(path));
    output_is_auto_ = false;
}

void Av1Tab::write() {
    if (!source_ || !encoded_) {
        return;
    }
    QString output_text = output_edit_->text().trimmed();
    if (output_text.size() >= 2 && output_text.startsWith('"') && output_text.endsWith('"')) {
        output_text = output_text.mid(1, output_text.size() - 2);
    }
    const std::filesystem::path output = utf8_path(output_text.toStdString());
    if (!output_text.endsWith(".mkv", Qt::CaseInsensitive)) {
        QMessageBox::warning(this, "Output", "The output must be an .mkv file.");
        return;
    }
    std::error_code error;
    if (std::filesystem::exists(output, error) && std::filesystem::equivalent(output, encoded_->path, error)) {
        QMessageBox::warning(this, "Output", "The output would overwrite the encode. Choose another name.");
        return;
    }
    if (std::filesystem::exists(output, error) &&
        QMessageBox::question(this, "Replace file?", QFileInfo(output_text).fileName() + " already exists. Replace it?") !=
            QMessageBox::Yes) {
        return;
    }
    const VideoInfo source = *source_;
    const VideoInfo encoded = *encoded_;
    const Tools tools = tools_;
    const std::optional<Crop> crop = crop_;
    const std::int64_t offset = offset_spin_->value();
    const int threads = workers_spin_->value();
    const bool gpu = gpu_check_->isChecked();
    const std::filesystem::path folder = every_frame_folder();
    const Tuning tune = tuning();
    const int seed = seed_spin_->value();
    const QString cache_file = model_cache_file();
    const bool analysed = every_frame_state().first == AnalysisState::done;
    const double seconds = estimate_seconds(encoded, threads, &source);
    if (!analysed && seconds > long_run_seconds && confirm_long_runs) {
        const auto answer = QMessageBox::question(
            this, "Analyse every frame and write?",
            "The grain metadata is measured on every frame, like libaom's encoder does. That analyses all " +
                thousands(encoded.frame_count()) + " frames first and takes " + duration_text(seconds) +
                ", using most of the CPU. The PC is kept awake while it runs.\n\nYou can keep browsing frames meanwhile. "
                "Progress is saved about once a minute; if you cancel or close the app, the next Write continues from "
                "there.");
        if (answer != QMessageBox::Yes) {
            return;
        }
    }

    struct Result {
        std::shared_ptr<const TimelineModel> model;
        std::shared_ptr<const GrainPlan> plan;
        WriteReport report;
    };
    start_job("Writing", [=, this](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
        const KeepAwake awake;
        check_writable(encoded);  // a raw .266 or a .ts would fail only after the analysis
        const QString first_step = analysed ? QString() : QString("Step 1 of 2 · ");
        const QString second_step = analysed ? QString() : QString("Step 2 of 2 · ");
        auto model = std::make_shared<const TimelineModel>(analyze_every_frame(
            tools, source, encoded, offset, crop, folder,
            [&](double f, const std::string& m) { progress(f, first_step.toStdString() + m); }, &cancel, threads, gpu));
        auto plan = std::make_shared<const TimelinePlan>(model, tune);
        WriteReport report = write_afgs1_mkv(
            tools, encoded, output, *plan, seed,
            [&](double f, const std::string& m) { progress(f, second_step.toStdString() + m); }, &cancel);
        // Saved here, not on the window's thread: a whole-film analysis takes seconds to write out.
        if (!cache_file.isEmpty()) {
            const std::filesystem::path file = utf8_path(cache_file.toStdString());
            std::filesystem::create_directories(file.parent_path());
            std::ofstream(file, std::ios::binary) << model->to_json();
        }
        auto result = std::make_shared<Result>(Result{model, plan, std::move(report)});
        return [this, result, seed] {
            // The preview now shows what was written: the every-frame parameters.
            set_model(result->model);
            const WriteReport& report = result->report;
            written_ = Written{report, result->plan, seed};
            std::error_code size_error;
            const double size = static_cast<double>(std::filesystem::file_size(report.output, size_error)) / 1e9;
            QStringList copied;
            for (const std::string& item : report.copied) {
                copied << qstring(item);
            }
            const QString also = copied.isEmpty() ? QString("the encode has nothing else to copy")
                                                  : copied.join(", ") + " copied from the encode";
            QString text = QString("Wrote %1 (%2 GB): grain metadata on all %3 pictures; %4.")
                               .arg(qstring(utf8(report.output.filename())))
                               .arg(size, 0, 'f', 2)
                               .arg(thousands(report.pictures), also);
            for (const std::string& note : report.notes) {
                text += " " + qstring(note);
            }
            write_label_->setText(text + " Use Verify to check it with ffmpeg.");
            status_->set_text("Written.");
            log(text);
            update_enabled();
        };
    }, "Writing " + QFileInfo(output_text).fileName() + "…");
    // Once it ends (written, cancelled or failed), a stopped analysis can resume: say so.
    if (job_) {
        connect(job_, &Job::ended, this, &Av1Tab::update_estimate);
    }
}

void Av1Tab::verify() {
    if (!written_ || !encoded_) {
        return;
    }
    const Written written = *written_;
    const VideoInfo encoded = *encoded_;
    const Tools tools = tools_;
    const std::int64_t index = current_index_;
    struct Comparison {
        std::int64_t frame;
        int differences[3];
        double theirs_added;
        double mine_added;
    };
    start_job("Verification", [=, this](CancelToken& cancel, const Progress& progress) -> Job::Deliver {
        progress(-1, "Decoding frame " + with_thousands(index) + " of the written file with ffmpeg…");
        const VideoInfo output = probe(written.report.output, tools.ffprobe, &cancel);
        const Frame theirs = read_frame(tools.ffmpeg, output, index, &cancel);
        const Frame clean = read_frame(tools.ffmpeg, encoded, index, &cancel);
        if (theirs.index != clean.index) {
            throw std::runtime_error("the written file gave frame " + with_thousands(theirs.index) +
                                     " where the encode gave frame " + with_thousands(clean.index));
        }
        // Near the start of a cut file the frame asked for may not decode; the
        // first decodable one comes back, and its own grain is what to compare.
        const std::int64_t n = clean.index;
        Frame mine = clean;
        if (const std::optional<GrainParams> params = written.plan->params_at(n)) {
            add_grain(mine, *params, picture_seed(written.seed, n));
        }
        Comparison c{n, {0, 0, 0}, 0, 0};
        const Plane* mine_planes[3] = {&mine.y, &mine.u, &mine.v};
        const Plane* their_planes[3] = {&theirs.y, &theirs.u, &theirs.v};
        for (int p = 0; p < 3; ++p) {
            for (std::size_t i = 0; i < mine_planes[p]->samples.size(); ++i) {
                c.differences[p] = std::max(c.differences[p],
                                            std::abs(mine_planes[p]->samples[i] - their_planes[p]->samples[i]));
            }
        }
        const auto mean_added = [&clean](const Plane& grained) {
            double total = 0;
            for (std::size_t i = 0; i < grained.samples.size(); ++i) {
                total += std::abs(grained.samples[i] - clean.y.samples[i]);
            }
            return total / static_cast<double>(grained.samples.size());
        };
        c.theirs_added = mean_added(theirs.y);
        c.mine_added = mean_added(mine.y);
        return [this, c, index] {
            const QString where = "Frame " + thousands(c.frame) +
                                  (c.frame == index ? QString()
                                                    : " (frame " + thousands(index) +
                                                          " cannot be decoded on its own; this is the first frame that can)");
            QString text;
            const int worst = std::max({c.differences[0], c.differences[1], c.differences[2]});
            if (worst == 0) {
                text = "✔ " + where + ": ffmpeg's decode of the written file matches the preview exactly " +
                       (c.theirs_added ? QString("(grain changes luma by %1 code values on average).").arg(c.theirs_added, 0, 'f', 2)
                                       : QString("(this frame gets no visible grain)."));
            } else if (c.theirs_added == 0 && c.mine_added > 0) {
                text = "✖ " + where + ": ffmpeg decoded the written file without grain. Your ffmpeg may not apply AFGS1 "
                                      "film grain to VVC (FFmpeg 9.0.1 does).";
            } else {
                text = "✖ " + where + QString(": ffmpeg differs from the preview by up to [%1, %2, %3] (Y, Cb, Cr).")
                                          .arg(c.differences[0]).arg(c.differences[1]).arg(c.differences[2]);
            }
            write_label_->setText(text);
            status_->set_text(text.startsWith("✔") ? "Verified." : "Verification found a difference.");
            log(text);
        };
    }, "Verifying with ffmpeg…");
}

void Av1Tab::open_folder() {
    if (written_) {
        QDesktopServices::openUrl(QUrl::fromLocalFile(qstring(utf8(written_->report.output.parent_path()))));
    }
}

}  // namespace fgw::app
